"""Synthetic Amap POIs. The city network guard also applies to this module."""
from datetime import timedelta
import json
from unittest.mock import AsyncMock

import pytest

from test_amap_navigation import SECRET, adapter, city, response, run
from test_kernel_support import NOW, m
from good_life.activities.city_trip import CityActivities, city_trip_template
from good_life.city.amap import AmapConfig, AmapError
from good_life.city.coordinates import gcj02_to_wgs84
from good_life.city.poi import POI_ENDPOINT, POI_LIMIT
from good_life.graph_package_storage import GraphPackageStore
from good_life.kernel.codec import KernelError


def poi(poi_id="SYNTH01", *, name="Synthetic Museum", city_code="021", location="121.40212,31.20123"):
    return {"id": poi_id, "name": name, "location": location, "citycode": city_code, "address": "discard this"}


def search_response(*pois):
    return {"status": "1", "infocode": "10000", "count": str(len(pois)), "pois": list(pois)}


def poi_adapter(city, poi_transport=None, **kwargs):
    return adapter(city, poi_transport=poi_transport, public_poi_search_enabled=True, **kwargs)


def test_poi_opt_in_and_invalid_queries_do_not_call_amap(city):
    transport = AsyncMock()
    nav = adapter(city, poi_transport=transport)
    assert run(nav.asearch_pois("Synthetic Museum")).reason_code == "amap_poi_disabled"
    assert AmapConfig.from_plugin({"amap_poi_search_enabled": True}).public_poi_search_enabled
    nav = poi_adapter(city, transport)
    for term, code in [("", "021"), ("Chat\nmessage", "021"), ("121.1,31.1", "021"),
                       ("Synthetic Museum", "310000"), ("A" * 81, "021"), (SECRET, "021")]:
        assert run(nav.asearch_pois(term, city_code=code)).reason_code == "amap_poi_invalid_query"
    transport.assert_not_called()
    assert nav._poi_store.reader.one("SELECT value FROM life_metadata WHERE key='city_amap_usage'", ()) is None


def test_http_sends_only_public_name_city_and_key_with_fixed_controls(city, monkeypatch):
    captured = {}

    class Response:
        status = 200
        content_length = None

        @property
        def content(self):
            return self

        async def iter_chunked(self, _):
            yield json.dumps(search_response(poi())).encode()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def get(self, endpoint, **kwargs):
            captured.update(endpoint=endpoint, **kwargs)
            return Response()

    monkeypatch.setattr("aiohttp.ClientSession", Session)
    result = run(poi_adapter(city).asearch_pois(" Synthetic Museum "))
    assert result.status == "ok"
    assert captured == {"endpoint": POI_ENDPOINT, "allow_redirects": False,
        "params": {"key": SECRET, "keywords": "Synthetic Museum", "region": "021", "city_limit": "true",
                   "page_size": str(POI_LIMIT), "page_num": "1", "output": "json"}}
    assert "discard this" not in repr(result)


def test_only_live_list_members_are_persisted(city):
    transport = AsyncMock(side_effect=[search_response(*(poi(f"SYNTH{i:02}") for i in range(15))),
                                      search_response(poi("OTHER01"))])
    nav = poi_adapter(city, transport)
    current = [100.0]
    nav._poi_lists.monotonic = lambda: current[0]
    result = run(nav.asearch_pois("Synthetic Museum"))
    assert result.status == "ok" and len(result.items) == POI_LIMIT and result.expires_at > NOW
    cached = run(nav.asearch_pois("Synthetic Museum"))
    assert cached.list_token != result.list_token and cached.items == result.items
    assert transport.await_count == 1
    other = run(nav.asearch_pois("Synthetic Gallery"))
    for selector in ({"poi_id": "SYNTH10"}, {"poi_id": other.items[0].poi_id}, {"index": -1},
                     {"index": True}, {"index": POI_LIMIT}, {"index": 0, "poi_id": "SYNTH00"}, {}):
        with pytest.raises(AmapError) as error:
            nav.select_poi(result.list_token, **selector)
        assert error.value.reason_code == "amap_poi_not_in_list"
    assert nav._poi_store.lookup("amap-poi:invented") is None
    assert nav._poi_store.reader.one("SELECT 1 FROM sqlite_master WHERE name='life_city_amap_places'", ()) is None
    selected = nav.select_poi(result.list_token, index=0)
    assert selected == nav.select_poi(cached.list_token, poi_id="SYNTH00")
    assert (selected.lon, selected.lat) == pytest.approx(gcj02_to_wgs84(*selected.poi.gcj02))
    assert selected.coordinate_system == "WGS84"
    assert selected.poi.retrieved_at == NOW
    current[0] += (result.expires_at - NOW).total_seconds()
    with pytest.raises(AmapError) as error:
        nav.select_poi(result.list_token, index=1)
    assert error.value.reason_code == "amap_poi_list_unavailable"
    assert city.storage._connection.execute("SELECT COUNT(*) FROM life_city_amap_places").fetchone()[0] == 1


def test_selected_place_is_reused_across_restart_even_if_provider_fields_change(city):
    nav = poi_adapter(city, AsyncMock(return_value=search_response(poi())))
    first = run(nav.asearch_pois("Synthetic Museum"))
    saved = nav.select_poi(first.list_token, index=0)
    city.reopen()
    city.store = GraphPackageStore(city.storage, enabled=True)
    refreshed = poi_adapter(city, AsyncMock(return_value=search_response(poi(name="Changed name", location="121.41,31.22"))),
                            clock=lambda: NOW + timedelta(hours=1))
    with pytest.raises(AmapError) as error:
        refreshed.select_poi(first.list_token, index=0)
    assert error.value.reason_code == "amap_poi_list_unavailable"
    second = run(refreshed.asearch_pois("Changed name"))
    assert second.items == (saved.poi,)
    assert refreshed.select_poi(second.list_token, poi_id=saved.poi.poi_id) == saved
    quote = run(refreshed.aquote("home", saved.place_id, NOW + timedelta(hours=1)))
    assert quote.destination == saved.place_id
    assert refreshed.transport.await_args.args[0]["destination"] == "%.5f,%.5f" % second.items[0].gcj02
    row = city.storage._connection.execute("SELECT * FROM life_city_amap_places").fetchone()
    assert SECRET not in repr(tuple(row)) and "discard this" not in repr(tuple(row))


def test_older_list_cannot_select_different_coordinates_for_a_newly_saved_poi(city):
    transport = AsyncMock(side_effect=[search_response(poi()), search_response(poi(location="121.41,31.22"))])
    nav = poi_adapter(city, transport)
    first = run(nav.asearch_pois("Synthetic Museum"))
    other = run(nav.asearch_pois("Museum Alternate Name"))
    assert first.items[0].gcj02 != other.items[0].gcj02
    saved = nav.select_poi(first.list_token, index=0)
    with pytest.raises(AmapError) as error:
        nav.select_poi(other.list_token, index=0)
    assert error.value.reason_code == "amap_poi_list_unavailable"
    refreshed = run(nav.asearch_pois("Museum Alternate Name"))
    assert refreshed.items == (saved.poi,)
    assert nav.select_poi(refreshed.list_token, index=0) == saved
    assert transport.await_count == 2


@pytest.mark.parametrize("payload,status,reason", [
    (search_response(), "empty", "amap_poi_empty"),
    ({"status": "1", "count": "0"}, "empty", "amap_poi_empty"),
    ({"status": "0", "infocode": "10001", "info": SECRET}, "error", "amap_bad_key"),
    ({"status": "0", "infocode": "10003", "info": SECRET}, "error", "amap_quota"),
    ({"status": "1", "pois": "unexpected"}, "error", "amap_poi_response"),
    (search_response(poi(city_code="010")), "error", "amap_poi_response"),
    (search_response(poi(location="NaN,31")), "error", "amap_poi_response"),
    (search_response(poi(name=SECRET)), "error", "amap_poi_response"),
])
def test_poi_empty_quota_and_errors_are_explicit(city, caplog, payload, status, reason):
    transport = AsyncMock(return_value=payload)
    nav = poi_adapter(city, transport, manual_fallback=True)
    result = run(nav.asearch_pois("Synthetic Museum"))
    assert (result.status, result.reason_code, result.list_token, result.items) == (status, reason, None, ())
    assert transport.await_count == 1 and SECRET not in caplog.text and SECRET not in repr(result)


def test_poi_and_transit_share_daily_quota(city):
    transport = AsyncMock(return_value=search_response(poi()))
    nav = poi_adapter(city, transport, daily_quota=1, max_gateways=1)
    first = run(nav.asearch_pois("Synthetic Museum"))
    target = nav.select_poi(first.list_token, index=0)
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", target.place_id, NOW))
    assert error.value.reason_code == "amap_daily_quota"
    assert run(nav.asearch_pois("Other name")).reason_code == "amap_daily_quota"
    assert transport.await_count == 1
    nav.transport.assert_not_called()


def test_external_round_trip_quotes_and_checkpoint_recover(city):
    search = AsyncMock(side_effect=[search_response(poi()),
                                    search_response(poi("SYNTH02", city_code="010", location="116.20123,39.00123"))])
    route = AsyncMock(return_value=response())
    nav = poi_adapter(city, search, transport=route)
    first = run(nav.asearch_pois("Synthetic Museum"))
    second = run(nav.asearch_pois("Synthetic Gallery", city_code="010"))
    origin = nav.select_poi(first.list_token, index=0)
    target = nav.select_poi(second.list_token, index=0)
    with pytest.raises(KernelError, match="unknown/equal"):
        run(nav.aquote(origin.place_id, "amap-poi:invented", NOW))
    route.assert_not_called()

    # A replacement adapter can plan from persisted picks without POI opt-in or a fresh search.
    city.reopen()
    city.store = GraphPackageStore(city.storage, enabled=True)
    nav = adapter(city, transport=route)
    out = run(nav.aquote(origin.place_id, target.place_id, NOW))
    back = run(nav.aquote(target.place_id, origin.place_id, NOW + timedelta(seconds=out.duration_s + 60)))
    assert route.await_count == 2 and search.await_count == 2
    for quote, start, end, call in zip((out, back), (origin, target), (target, origin), route.await_args_list):
        assert len(quote.segments) == 1
        assert (quote.segments[0].origin, quote.segments[0].destination) == (start.place_id, end.place_id)
        assert "gateway_id" not in quote.evidence[0]
        assert call.args[0]["origin"] == "%.5f,%.5f" % start.poi.gcj02
        assert call.args[0]["destination"] == "%.5f,%.5f" % end.poi.gcj02
        assert (call.args[0]["city1"], call.args[0]["city2"]) == (start.city_code, end.city_code)
        assert start.place_id not in repr(call) and start.poi.poi_id not in repr(call)

    city.city.locate(city.scope.owner_key, origin.place_id, expected_revision=1)
    template = city_trip_template(out, back, stay_s=60)
    city.registry.register_template(template)
    started = city.running(template)
    underway = city.advance(started, 250)
    pinned = underway.checkpoint["params"]
    city.reopen()
    recovered = CityActivities(city.kernel).recover(city.scope, now=NOW + timedelta(seconds=300))[0]
    assert recovered.checkpoint["params"] == pinned
    final = city.advance(recovered, 300 + recovered.planned_duration_s - recovered.elapsed_s)
    assert final.status == m.ActivityStatus.COMPLETED
    assert final.checkpoint["location"]["place_id"] == origin.place_id
    assert city.quote() == (88, 0, 88)
    assert route.await_count == 2 and search.await_count == 2
