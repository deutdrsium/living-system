"""Synthetic official-v5-shaped HTTP only. Never uses an Amap key or network."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import socket
import sqlite3
import threading
import traceback
from unittest.mock import AsyncMock, patch

import pytest
import test_city_trip as support
from test_kernel_support import NOW, m
from good_life.activities.city_trip import CityActivities, CityTrip, city_trip_template
from good_life.city.amap import (AmapConfig, AmapError, AmapNavigation, StablePlace,
    UsageGuard, ENDPOINT, MAX_RESPONSE_BYTES, _Credential, _http, _parse_plan, navigation_from_config)
from good_life.city.coordinates import gcj02_to_wgs84, wgs84_to_gcj02
from good_life.city.navigation import OfflineNavigation
from good_life.graph_packages import seal_package
from good_life.kernel.codec import KernelError, encode, wire

SECRET = "mock-amap-secret-never-record-this"


def response(duration=300, fare="0.06", *, walking=40):
    # cost fields are v5; walking/bus detail is inherited from the v3 schema
    # linked by the official v5 page. No real user places or captured traffic.
    return {"status": "1", "infocode": "10000", "info": "OK", "route": {"transits": [{
        "cost": {"duration": str(duration), "transit_fee": fare}, "segments": [{
            "walking": {"duration": str(walking), "distance": "20", "steps": []},
            "bus": {"buslines": [{"type": "地铁线路", "duration": "120", "name": "unused-private-name"}]},
            "railway": {"via_stops": [], "alters": [], "spaces": []}, "taxi": []}]}]}}


@pytest.fixture
def city():
    instance = support.CityTests()
    instance.setUp()
    try:
        yield instance
    finally:
        instance.doCleanups()
        UsageGuard._next.clear()


def adapter(city, *, transport=None, poi_transport=None, places=None, clock=None, **config):
    config.setdefault("requests_per_second", 10)
    return navigation_from_config(city.store, "test-home", currency="CNY",
        places=places or (StablePlace("museum", 121.4, 31.2, "WGS84"),),
        config=AmapConfig(enabled=True, api_key=SECRET, city_code="021", **config),
        transport=transport or AsyncMock(return_value=response()), poi_transport=poi_transport,
        clock=clock or (lambda: NOW))


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize("wgs,gcj", [
    ((116.397128, 39.916527), (116.403372494, 39.917930749)),
    ((121.473701, 31.230416), (121.478224058, 31.228473742)),
    ((113.264385, 23.129112), (113.269714582, 23.126435336)),
    ((-0.1276, 51.5072), (-0.1276, 51.5072)),
])
def test_coordinate_reference_and_inverse(wgs, gcj):
    assert wgs84_to_gcj02(*wgs) == pytest.approx(gcj, abs=2e-6)
    assert gcj02_to_wgs84(*gcj) == pytest.approx(wgs, abs=2e-6)


@pytest.mark.parametrize("system", ["", "unknown", "BD09", "GCJ-02", None])
def test_unknown_or_wrong_destination_coordinates_rejected(system):
    with pytest.raises(KernelError):
        StablePlace("museum", 121, 31, system)


@pytest.mark.parametrize("lon,lat", [(float("nan"), 31), (181, 31), (121, float("inf")), (True, 31)])
def test_invalid_coordinates_rejected(lon, lat):
    with pytest.raises(KernelError):
        StablePlace("museum", lon, lat, "WGS84")


def test_adapter_rejects_unvalidated_coordinate_record(city):
    from types import SimpleNamespace
    manual = city.adapter()
    invalid = SimpleNamespace(place_id="museum", lon=121, lat=31, coordinate_system="unknown")
    with pytest.raises(KernelError, match="G04"):
        AmapNavigation(manual, (invalid,), AmapConfig(enabled=True, api_key=SECRET, city_code="021"),
                       storage=city.storage, transport=AsyncMock())


def test_full_gateway_comparison_and_request_minimization(city):
    transport = AsyncMock(side_effect=[response(600), response(180)])
    nav = adapter(city, transport=transport)
    options = run(nav.aquote_options("home", "museum", NOW))
    assert len(options) == 2
    best = options[0]
    assert best.segments[0].destination == "far"
    assert best.duration_s == 120 + 180 + 120
    assert best.cost_minor == 6
    assert best.requested_at == NOW
    assert best.evidence[0]["provider_duration_s"] == 180
    assert best.evidence[0]["legs"][0] == {"mode": "walk", "duration_s": 40}
    assert best.assumptions
    assert all(q.graph_digest == city.store.latest("test-home")["digest"] for q in options)
    for call in transport.await_args_list:
        params = call.args[0]
        assert set(params) == {"key", "origin", "destination", "city1", "city2", "date", "time",
                               "show_fields", "AlternativeRoute", "strategy", "output"}
        assert params["show_fields"] == "cost"
        assert params["city1"] == params["city2"] == "021"
        assert params["time"] == "16-01" or params["time"] == "16-02"
        assert params["origin"] != "121.000000,31.000000"
    assert SECRET not in repr(nav.config)
    assert SECRET not in encode(best)
    assert "unused-private-name" not in encode(best)
    assert "unused-private-name" not in repr(nav._cache)


@pytest.mark.parametrize("fare", [None, "", [], "0"])
def test_missing_fare_is_unknown_and_cannot_reserve(city, fare):
    nav = adapter(city, transport=AsyncMock(return_value=response(fare=fare)), max_gateways=1)
    out = run(nav.aquote("home", "museum", NOW))
    assert out.cost_minor is None
    assert not out.fare_known
    with pytest.raises(KernelError, match="票价未知") as error:
        city_trip_template(out, out)
    assert error.value.code == "provider_unavailable"


@pytest.mark.parametrize("fare,expected", [("0", None), ("2.50", 250), ("2.501", 251)])
def test_fare_yuan_to_fen(fare, expected):
    plan = response(fare=fare)["route"]["transits"][0]
    assert _parse_plan(plan, {"metro"})[1] == expected


def test_documented_segment_cost_and_missing_component():
    plan = response()["route"]["transits"][0]
    plan["cost"].pop("transit_fee")
    plan["segments"][0]["cost"] = {"transit_fee": "2.5"}
    assert _parse_plan(plan, {"metro"})[1] == 250
    second = deepcopy(plan["segments"][0])
    second.pop("cost")
    plan["segments"].append(second)
    plan["cost"]["duration"] = "600"
    assert _parse_plan(plan, {"metro"})[1] is None


@pytest.mark.parametrize("mutation", ["negative-fare", "nan-fare", "missing-duration", "short-duration", "taxi", "railway", "unknown-mode", "bad-walking"])
def test_malformed_or_unsupported_results_fail_closed(city, mutation):
    payload = response()
    plan = payload["route"]["transits"][0]
    part = plan["segments"][0]
    if mutation == "negative-fare": plan["cost"]["transit_fee"] = "-1"
    if mutation == "nan-fare": plan["cost"]["transit_fee"] = "NaN"
    if mutation == "missing-duration": plan["cost"].pop("duration")
    if mutation == "short-duration": plan["cost"]["duration"] = "1"
    if mutation == "taxi": part["taxi"] = {"price": "5"}
    if mutation == "railway": part["railway"]["id"] = "train"
    if mutation == "unknown-mode": part["bus"]["buslines"][0]["type"] = "hovercraft"
    if mutation == "bad-walking": part["walking"]["duration"] = []
    nav = adapter(city, transport=AsyncMock(return_value=payload), max_gateways=1)
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == "amap_response"


def test_candidate_cap_closed_edge_and_mode_filter(city):
    package = support.home_graph(2, closed=True)
    # The closest gateway is unreachable; far is bus-only and cannot take metro.
    package["gateways"][1]["modes"] = ["bus"]
    city.confirm(seal_package(package))
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1)
    with pytest.raises(AmapError):
        run(nav.aquote("home", "museum", NOW))
    assert transport.await_count == 1


def test_gateway_cap_and_cache_ttl_departure_bucket_direction(city):
    current = [NOW]
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1, cache_ttl_s=120, clock=lambda: current[0])
    first = run(nav.aquote("home", "museum", NOW))
    second = run(nav.aquote("home", "museum", NOW + timedelta(seconds=1)))
    assert transport.await_count == 1
    assert first.requested_at == second.requested_at
    run(nav.aquote("home", "museum", NOW + timedelta(seconds=61)))
    assert transport.await_count == 2
    run(nav.aquote("museum", "home", NOW))
    assert transport.await_count == 3
    current[0] += timedelta(seconds=120)
    run(nav.aquote("home", "museum", NOW))
    assert transport.await_count == 4


def test_daily_quota_survives_adapter_recreation_and_new_day(city):
    current = [NOW]
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1, daily_quota=1, clock=lambda: current[0])
    run(nav.aquote("home", "museum", NOW))
    nav = adapter(city, transport=transport, max_gateways=1, daily_quota=1, clock=lambda: current[0])
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == "amap_daily_quota"
    assert transport.await_count == 1
    # New SQLite connection, not just in-memory guard reuse.
    city.reopen()
    city.store = support.GraphPackageStore(city.storage, enabled=True)
    nav = adapter(city, transport=transport, max_gateways=1, daily_quota=1, clock=lambda: current[0])
    with pytest.raises(AmapError):
        run(nav.aquote("home", "museum", NOW))
    current[0] += timedelta(days=1)
    run(nav.aquote("home", "museum", current[0]))
    assert transport.await_count == 2


def test_rate_limit_reserves_spaced_slots(city):
    nav = adapter(city, max_gateways=1, requests_per_second=2)
    nav.guard.monotonic = lambda: 10
    sleep = AsyncMock()
    with patch("good_life.city.amap.asyncio.sleep", sleep):
        run(nav.guard.acquire())
        run(nav.guard.acquire())
        run(nav.guard.acquire())
    assert [call.args[0] for call in sleep.await_args_list] == [.5, 1]


@pytest.mark.parametrize("code,reason", [("10001", "amap_bad_key"), ("10009", "amap_bad_key"),
    ("10003", "amap_quota"), ("10044", "amap_quota"), ("40000", "amap_quota"),
    ("20000", "amap_invalid_params"), ("20001", "amap_invalid_params"),
    ("10026", "amap_account_banned"), ("10041", "amap_permission"),
    ("10045", "amap_quota"), ("40003", "amap_quota"), ("20011", "amap_permission"),
    ("10004", "amap_key_suspended")])
def test_definite_errors_never_retry_or_leak(city, caplog, code, reason):
    transport = AsyncMock(return_value={"status": "0", "infocode": code, "info": SECRET})
    nav = adapter(city, transport=transport)
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == reason
    assert not error.value.transient
    assert transport.await_count == 1
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    assert SECRET not in str(city.storage._connection.execute("SELECT * FROM life_metadata").fetchall())


def test_transient_retry_is_bounded_and_attempts_counted(city):
    transport = AsyncMock(return_value={"status": "0", "infocode": "10016", "info": SECRET})
    nav = adapter(city, transport=transport, max_gateways=1, retries=2)
    with patch("good_life.city.amap.asyncio.sleep", AsyncMock()):
        with pytest.raises(AmapError) as error:
            run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == "amap_busy"
    assert error.value.transient
    assert transport.await_count == 3
    raw = city.storage._connection.execute("SELECT value FROM life_metadata WHERE key='city_amap_usage'").fetchone()[0]
    assert json.loads(raw)["attempts"] == 3


def test_timeout_and_async_loop_remains_responsive(city):
    async def hangs(*_):
        await asyncio.Event().wait()
    nav = adapter(city, transport=hangs, max_gateways=1, retries=0, timeout_s=.1)
    ticks = []
    async def scenario():
        task = asyncio.create_task(nav.aquote("home", "museum", NOW))
        await asyncio.sleep(.01)
        ticks.append("scheduler-ran")
        with pytest.raises(AmapError) as error:
            await task
        assert error.value.reason_code == "amap_timeout"
        with pytest.raises(AmapError, match="await"):
            nav.quote("home", "museum", NOW)
    run(scenario())
    assert ticks == ["scheduler-ran"]


def test_transport_exception_is_redacted_and_cancellation_propagates(city):
    nav = adapter(city, transport=AsyncMock(side_effect=RuntimeError(SECRET)), max_gateways=1, retries=0)
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", "museum", NOW))
    assert SECRET not in str(error.value)
    nav.transport = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run(nav.aquote("home", "museum", NOW))


@pytest.mark.parametrize("age,requests", [(55, 1), (56, 2), (110, 2)])
def test_cache_keeps_full_activation_window_and_slack(city, age, requests):
    current = [NOW]
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1, departure_bucket_s=300,
                  clock=lambda: current[0])
    first = run(nav.aquote("home", "museum", NOW))
    current[0] += timedelta(seconds=age)
    outbound = run(nav.aquote("home", "museum", current[0]))
    assert transport.await_count == requests
    assert (outbound.requested_at == first.requested_at) is (requests == 1)
    assert outbound.valid_until >= current[0] + timedelta(seconds=65)
    inbound = run(nav.aquote("museum", "home", current[0] + timedelta(seconds=outbound.duration_s)))
    template = city_trip_template(outbound, inbound)
    city.registry.register_template(template)
    prepared = city.prepared(template, now=current[0])
    assert CityActivities(city.kernel)._retire_missed_preparing(city.scope, current[0] + timedelta(seconds=60)) == ()
    started = city.kernel.start(city.scope, prepared.instance.instance_id,
        expected_revision=prepared.instance.revision, command_id="cached-start", now=current[0] + timedelta(seconds=60))
    assert started.status == m.ActivityStatus.RUNNING


def test_short_cache_ttl_never_reuses_activation_starved_entry(city):
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1, cache_ttl_s=30)
    run(nav.aquote("home", "museum", NOW))
    run(nav.aquote("home", "museum", NOW))
    assert transport.await_count == 2


@pytest.mark.parametrize("mode", ["地铁线路", "普通公交线路"])
def test_zero_segment_fare_is_unknown(city, mode):
    payload = response(fare=None)
    segment = payload["route"]["transits"][0]["segments"][0]
    segment["bus"]["buslines"][0]["type"] = mode
    segment["cost"] = {"transit_fee": "0"}
    nav = adapter(city, transport=AsyncMock(return_value=payload), max_gateways=1)
    quote = run(nav.aquote("home", "museum", NOW))
    assert quote.cost_minor is None and not quote.fare_known
    with pytest.raises(KernelError, match="票价未知"):
        city_trip_template(quote, quote)


@pytest.mark.parametrize("fee_missing", [False, True])
def test_walk_only_plan_keeps_known_zero_fare(city, fee_missing):
    payload = response(fare="0")
    plan = payload["route"]["transits"][0]
    if fee_missing:
        del plan["cost"]["transit_fee"]
    plan["segments"][0]["bus"] = {}
    nav = adapter(city, transport=AsyncMock(return_value=payload), max_gateways=1)
    outbound = run(nav.aquote("home", "museum", NOW))
    inbound = run(nav.aquote("museum", "home", NOW + timedelta(seconds=outbound.duration_s)))
    assert outbound.fare_known and outbound.cost_minor == 0
    assert all(segment.mode == "walk" for segment in outbound.segments)
    assert city_trip_template(outbound, inbound).params["reserve_minor"] == 0


@pytest.mark.parametrize("outbound_amap,retire", [(False, False), (True, False), (False, True)])
def test_manual_fallback_mixed_trip_uses_amap_activation_window(city, outbound_amap, retire):
    current = [NOW]
    payloads = iter([response(), {"status": "0", "infocode": "10001"}] if outbound_amap
                    else [{"status": "0", "infocode": "10001"}, response()])
    async def transport(*_):
        current[0] += timedelta(seconds=1)
        return next(payloads)
    nav = AmapNavigation(city.adapter(),
        (StablePlace("museum", 121, 31, "WGS84"), StablePlace("safe-stop", 121, 31, "WGS84")),
        AmapConfig(enabled=True, api_key=SECRET, city_code="021", manual_fallback=True, max_gateways=1),
        storage=city.storage, transport=transport, clock=lambda: current[0])
    outbound = run(nav.aquote("home", "museum", NOW))
    inbound = run(nav.aquote("museum", "home", NOW + timedelta(seconds=outbound.duration_s)))
    assert (outbound.provider == "amap-transit-v5@1") is outbound_amap
    assert (inbound.provider == "amap-transit-v5@1") is not outbound_amap
    template = city_trip_template(outbound, inbound)
    city.registry.register_template(template)
    prepared = city.prepared(template, now=current[0])
    activities = CityActivities(city.kernel)
    assert activities._retire_missed_preparing(city.scope, current[0] + timedelta(seconds=1)) == ()
    missed = NOW + timedelta(seconds=61)
    with pytest.raises(KernelError) as error:
        CityTrip().prepare(template, missed)
    assert error.value.code == "no_time_window"
    if retire:
        with pytest.raises(KernelError, match="高德报价的启动窗口"):
            CityTrip().start(prepared.instance, missed)
        retired, = activities._retire_missed_preparing(city.scope, missed)
        assert retired.status == m.ActivityStatus.CANCELLED
        assert "高德报价的启动窗口" in retired.checkpoint["explanations"][-1]["message"]
        assert city.quote() == (100, 0, 100)
    else:
        started = city.kernel.start(city.scope, prepared.instance.instance_id,
            expected_revision=prepared.instance.revision, command_id="mixed-start", now=current[0] + timedelta(seconds=1))
        assert started.status == m.ActivityStatus.RUNNING


def test_quota_sqlite_writer_wait_does_not_block_event_loop(city):
    path = city.storage._connection.execute("PRAGMA database_list").fetchone()[2]
    blocker = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    fallback_fired = threading.Event()
    def fallback_release():
        fallback_fired.set()
        blocker.rollback()
    timer = threading.Timer(2, fallback_release)
    timer.start()
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1)
    async def scenario():
        task = asyncio.create_task(nav.aquote("home", "museum", NOW))
        try:
            await asyncio.sleep(.05)
            assert not fallback_fired.is_set(), "SQLite blocked the asyncio scheduler"
            assert not task.done()
            assert transport.await_count == 0
        finally:
            blocker.rollback()
            await task
    try:
        run(scenario())
    finally:
        timer.cancel()
        timer.join()
        blocker.close()
    assert transport.await_count == 1


def test_concurrent_quotes_wait_for_other_quota_transaction(city):
    holding, queued, release = threading.Event(), threading.Event(), threading.Event()
    original = city.storage.transaction
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1)

    @contextmanager
    def slow_transaction():
        if holding.is_set():
            queued.set()
            with original() as db:
                yield db
        else:
            with original() as db:
                holding.set()
                assert release.wait(5), "test failed to release quota transaction"
                yield db

    async def scenario():
        outbound = asyncio.create_task(nav.aquote("home", "museum", NOW))
        try:
            assert await asyncio.to_thread(holding.wait, 2)
            inbound = asyncio.create_task(nav.aquote("museum", "home", NOW + timedelta(hours=1)))
            reached_worker = await asyncio.to_thread(queued.wait, 2)
        finally:
            release.set()
        results = await asyncio.gather(outbound, inbound, return_exceptions=True)
        assert reached_worker and [getattr(q, "destination", None) for q in results] == ["museum", "home"], results

    with patch.object(city.storage, "transaction", slow_transaction):
        run(scenario())
    assert transport.await_count == 2
    usage = city.storage._connection.execute("SELECT value FROM life_metadata WHERE key='city_amap_usage'").fetchone()[0]
    assert json.loads(usage)["attempts"] == 2


@pytest.mark.parametrize("failure", [ValueError("life transaction is already active"),
    sqlite3.OperationalError("database is locked"), ValueError(SECRET)])
def test_storage_failures_never_retry_or_call_transport(city, failure):
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, retries=2)
    with patch.object(city.storage, "transaction", side_effect=failure) as transaction:
        with pytest.raises(AmapError) as error:
            run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == "amap_storage"
    assert not error.value.transient
    assert SECRET not in str(error.value)
    assert transaction.call_count == 1
    transport.assert_not_called()


@pytest.mark.parametrize("context", ["transaction", "read_snapshot"])
def test_caller_held_storage_context_is_rejected_without_deadlock(city, context):
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, retries=2)
    with getattr(city.storage, context)():
        with pytest.raises(AmapError) as error:
            run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == "amap_storage"
    assert not error.value.transient
    transport.assert_not_called()


def test_request_key_is_redacted_in_transport_and_traceback_locals(city):
    async def fails(params, _):
        assert SECRET not in str(params)
        assert SECRET not in repr(params["key"])
        assert SECRET not in str(params["key"])
        raise AmapError("amap_no_route", "synthetic failure")
    nav = adapter(city, transport=fails, max_gateways=1)
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", "museum", NOW))
    frames = traceback.walk_tb(error.value.__traceback__)
    for frame, _ in frames:
        if frame.f_code.co_name in ("_plans", "fails"):
            assert SECRET not in repr(frame.f_locals["params"])


@pytest.mark.parametrize("host", ["restapi.amap.com", "203.0.113.1", "2001:db8::1"])
def test_network_guard_blocks_nonloopback_dns_and_sockets(host):
    with pytest.raises(AssertionError, match="Non-loopback"):
        socket.getaddrinfo(host, 443)
    with pytest.raises(AssertionError, match="Non-loopback"):
        socket.gethostbyname(host)
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
        with pytest.raises(AssertionError, match="Non-loopback"):
            sock.connect((host, 443))
        with pytest.raises(AssertionError, match="Non-loopback"):
            sock.connect_ex((host, 443))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="Non-loopback"):
            sock.sendto(b"synthetic", (host, 53))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_network_guard_allows_loopback_dns(host):
    assert socket.getaddrinfo(host, 0)


def test_empty_response_rejected(city):
    nav = adapter(city, transport=AsyncMock(return_value={"status": "1", "route": {"transits": []}}), max_gateways=1)
    with pytest.raises(AmapError) as error:
        run(nav.aquote("home", "museum", NOW))
    assert error.value.reason_code == "amap_no_route"


@pytest.mark.parametrize("enabled,key", [(False, SECRET), (True, ""), (False, ""), (True, "  ")])
def test_flag_off_or_no_key_uses_unchanged_offline(city, enabled, key):
    before = city.storage._connection.execute("SELECT * FROM life_metadata").fetchall()
    transport = AsyncMock(side_effect=AssertionError("network forbidden"))
    manual = city.adapter()
    nav = navigation_from_config(city.store, "test-home", currency="CNY",
        places=(StablePlace("museum", 121, 31, "WGS84"), StablePlace("safe-stop", 121, 31, "WGS84")),
        services=manual.services, config=AmapConfig(enabled=enabled, api_key=key), transport=transport)
    assert type(nav) is OfflineNavigation
    assert nav.quote("home", "museum", NOW) == manual.quote("home", "museum", NOW)
    assert city.storage._connection.execute("SELECT * FROM life_metadata").fetchall() == before
    transport.assert_not_called()


def test_home_segment_never_leaves_confirmed_graph(city):
    transport = AsyncMock(side_effect=AssertionError("network forbidden"))
    nav = adapter(city, transport=transport)
    assert run(nav.aquote("near", "far", NOW)) == city.adapter().quote("near", "far", NOW)
    transport.assert_not_called()


def test_manual_fallback_is_explicit(city):
    manual = city.adapter()
    nav = AmapNavigation(manual, (StablePlace("museum", 121, 31, "WGS84"), StablePlace("safe-stop", 121, 31, "WGS84")),
        AmapConfig(enabled=True, api_key=SECRET, city_code="021", manual_fallback=True), storage=city.storage,
        transport=AsyncMock(return_value={"status": "0", "infocode": "10001"}), clock=lambda: NOW)
    assert run(nav.aquote("home", "museum", NOW)) == manual.quote("home", "museum", NOW)


def test_wallet_persisted_quote_and_restart_never_requery(city):
    transport = AsyncMock(return_value=response())
    nav = adapter(city, transport=transport, max_gateways=1)
    out = nav.quote("home", "museum", NOW)
    back = nav.quote("museum", "home", NOW + timedelta(seconds=out.duration_s + 60))
    template = city_trip_template(out, back, stay_s=60)
    assert template.params["reserve_minor"] == 12
    city.registry.register_template(template)
    started = city.running(template)
    assert city.quote() == (100, 12, 88)
    instance = city.advance(started, 250)
    assert city.quote() == (94, 6, 88)
    pinned = instance.checkpoint["params"]
    city.reopen()
    with patch("socket.socket", side_effect=AssertionError("network forbidden")):
        recovered = CityActivities(city.kernel).recover(city.scope, now=NOW + timedelta(seconds=300))[0]
        assert recovered.checkpoint["params"] == pinned
        final = city.kernel.advance(city.scope, recovered.instance_id, expected_revision=recovered.revision,
            command_id="finish-amap", now=NOW + timedelta(seconds=300 + recovered.planned_duration_s - recovered.elapsed_s))
    assert final.status == m.ActivityStatus.COMPLETED
    assert city.quote() == (88, 0, 88)
    assert len([t for t in city.wallet.transactions(city.scope.owner_key) if t.kind == "spend"]) == 2
    assert transport.await_count == 2
    assert SECRET not in encode(final)
    # Search every SQLite text cell, including candidates, templates/checkpoints,
    # ledger/outbox and metadata, for accidental credential persistence.
    for (table,) in city.storage._connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        rows = city.storage._connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"').fetchall()
        assert SECRET not in str([tuple(row) for row in rows])


def test_expired_material_rejected_on_new_prepare_but_not_resume(city):
    nav = adapter(city, max_gateways=1)
    out = nav.quote("home", "museum", NOW)
    back = nav.quote("museum", "home", NOW + timedelta(seconds=out.duration_s))
    template = city_trip_template(out, back)
    template = replace(template, params={**template.params, "outbound": {**template.params["outbound"],
        "requested_at": wire(NOW - timedelta(seconds=121)), "valid_until": wire(NOW - timedelta(seconds=1))}})
    with pytest.raises(KernelError, match="过期"):
        CityTrip().prepare(template, NOW)


def test_network_latency_has_bounded_activation_window(city):
    current = [NOW]
    async def advances_clock(*_):
        current[0] += timedelta(seconds=1)
        return response()
    nav = adapter(city, transport=advances_clock, clock=lambda: current[0], max_gateways=1)
    out = run(nav.aquote("home", "museum", NOW))
    back = run(nav.aquote("museum", "home", NOW + timedelta(seconds=out.duration_s)))
    template = city_trip_template(out, back)
    city.registry.register_template(template)
    prepared = city.prepared(template, now=current[0])
    assert prepared.gate.allowed
    # Ordinary ticks must not retire the Amap preparing plan after one second.
    activities = CityActivities(city.kernel)
    assert activities._retire_missed_preparing(city.scope, current[0] + timedelta(seconds=1)) == ()
    started = city.kernel.start(city.scope, prepared.instance.instance_id,
        expected_revision=prepared.instance.revision, command_id="latency-start", now=current[0])
    assert started.status == m.ActivityStatus.RUNNING
    with pytest.raises(KernelError):
        CityTrip().prepare(template, NOW + timedelta(seconds=61))


def test_amap_missed_activation_retires_without_charge(city):
    nav = adapter(city, max_gateways=1)
    out = nav.quote("home", "museum", NOW)
    back = nav.quote("museum", "home", NOW + timedelta(seconds=out.duration_s))
    template = city_trip_template(out, back)
    city.registry.register_template(template)
    prepared = city.prepared(template)
    retired = CityActivities(city.kernel).recover(city.scope, now=NOW + timedelta(seconds=61))
    assert retired[0].status == m.ActivityStatus.CANCELLED
    assert city.quote() == (100, 0, 100)


def test_cancel_stops_at_routable_stable_place(city):
    nav = adapter(city, max_gateways=1)
    out = nav.quote("home", "museum", NOW)
    back = nav.quote("museum", "home", NOW + timedelta(seconds=out.duration_s))
    template = city_trip_template(out, back)
    city.registry.register_template(template)
    instance = city.advance(city.running(template), 210)
    requested = city.kernel.cancel(city.scope, instance.instance_id, expected_revision=instance.revision,
        command_id="cancel-amap", now=NOW + timedelta(seconds=210))
    assert requested.status == m.ActivityStatus.RUNNING
    final = city.advance(requested, out.duration_s)
    assert final.status == m.ActivityStatus.CANCELLED
    assert final.checkpoint["location"]["place_id"] == "museum"
    assert city.quote() == (94, 0, 94)
    assert run(nav.aquote("museum", "home", NOW + timedelta(seconds=out.duration_s))).destination == "home"
    assert "amap-stop-" not in encode(final)


def test_gateway_mode_only_applies_to_adjacent_vehicle(city):
    package = support.home_graph(2)
    for gateway in package["gateways"]:
        gateway["modes"] = ["bus"]
    city.confirm(seal_package(package))
    payload = response(500)
    plan = payload["route"]["transits"][0]
    first = deepcopy(plan["segments"][0])
    first["bus"]["buslines"][0]["type"] = "普通公交线路"
    plan["segments"].insert(0, first)
    inbound_payload = deepcopy(payload)
    inbound_payload["route"]["transits"][0]["segments"].reverse()
    transport = AsyncMock(side_effect=[payload, inbound_payload])
    nav = adapter(city, transport=transport, max_gateways=1)
    out = run(nav.aquote("home", "museum", NOW))
    inbound = run(nav.aquote("museum", "home", NOW + timedelta(seconds=out.duration_s)))
    assert out.fare_known and inbound.fare_known
    assert [leg["mode"] for leg in out.evidence[0]["legs"]] == ["walk", "bus", "walk", "metro"]
    assert out.segments[-1].mode == inbound.segments[0].mode == "bus"


def test_local_rate_queue_bound_and_clock_rollback(city):
    current = [NOW]
    nav = adapter(city, clock=lambda: current[0], requests_per_second=1, timeout_s=.1)
    nav.guard.monotonic = lambda: 10
    run(nav.guard.acquire())
    with pytest.raises(AmapError) as error:
        run(nav.guard.acquire())
    assert error.value.reason_code == "amap_local_rate_limit"
    UsageGuard._next.clear()
    current[0] -= timedelta(days=1)
    with pytest.raises(AmapError) as error:
        run(nav.guard.acquire())
    assert error.value.reason_code == "amap_clock"


def test_rate_wait_crossing_midnight_charges_actual_send_day(city):
    from datetime import datetime, timezone
    current = [datetime(2026, 11, 16, 15, 59, 59, tzinfo=timezone.utc)]
    nav = adapter(city, clock=lambda: current[0], requests_per_second=1)
    nav.guard.monotonic = lambda: 10
    run(nav.guard.acquire())
    async def crosses_midnight(_):
        current[0] += timedelta(seconds=2)
    with patch("good_life.city.amap.asyncio.sleep", crosses_midnight):
        run(nav.guard.acquire())
    raw = city.storage._connection.execute("SELECT value FROM life_metadata WHERE key='city_amap_usage'").fetchone()[0]
    assert json.loads(raw) == {"day": "2026-11-17", "attempts": 1}


def test_known_fare_option_preferred_and_explicit_unknown_fare_fallback(city):
    payload = response(fare=None)
    payload["route"]["transits"].append(response(600)["route"]["transits"][0])
    nav = adapter(city, transport=AsyncMock(return_value=payload), max_gateways=1)
    assert nav.quote("home", "museum", NOW).fare_known
    manual = city.adapter()
    nav = AmapNavigation(manual, (StablePlace("museum", 121, 31, "WGS84"), StablePlace("safe-stop", 121, 31, "WGS84")),
        AmapConfig(enabled=True, api_key=SECRET, city_code="021", manual_fallback=True, max_gateways=1),
        storage=city.storage, transport=AsyncMock(return_value=response(fare=None)), clock=lambda: NOW)
    assert run(nav.aquote("home", "museum", NOW)) == manual.quote("home", "museum", NOW)


def test_config_from_plugin_hides_key():
    config = AmapConfig.from_plugin({"amap_navigation_enabled": True, "amap_navigation_key": SECRET,
                                   "amap_navigation_city_code": "021"})
    assert config.enabled and config.city_code == "021" and config.api_key == SECRET
    assert SECRET not in repr(config)


@pytest.mark.parametrize("status,size,reason", [(200, None, None), (302, None, "amap_http"),
    (429, None, "amap_http"), (500, None, "amap_http"), (200, MAX_RESPONSE_BYTES + 1, "amap_response")])
def test_aiohttp_transport_fixed_endpoint_and_bounds(status, size, reason):
    captured = {}
    class Content:
        async def iter_chunked(self, _):
            yield json.dumps(response()).encode()
    class Response:
        content_length = size
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
    Response.status = status
    class Session:
        def __init__(self, **kwargs): captured["session"] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def get(self, endpoint, **kwargs):
            captured.update(endpoint=endpoint, **kwargs)
            return Response()
    with patch("aiohttp.ClientSession", Session):
        if reason:
            with pytest.raises(AmapError) as error:
                run(_http({"key": _Credential(SECRET)}, 5))
            assert error.value.reason_code == reason
        else:
            assert run(_http({"key": _Credential(SECRET)}, 5))["status"] == "1"
    assert captured["endpoint"] == ENDPOINT
    assert captured["params"]["key"] == SECRET
    assert captured["allow_redirects"] is False
    assert captured["session"]["trust_env"] is False
    assert captured["session"]["timeout"].total == 5
