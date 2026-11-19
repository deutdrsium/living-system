"""Opt-in Amap v5 transit quotes; no runtime registration or location inference.

Await aquote/aquote_options outside the kernel and database transactions. quote
is the compatible synchronous port for non-event-loop callers only. Transport
uses the host's aiohttp pattern, imported lazily so offline imports need no SDK.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from functools import partial
import json
import math
import re
import threading
import time
from zoneinfo import ZoneInfo
from weakref import WeakKeyDictionary

from ..contracts.models import ReasonCode
from ..kernel.codec import KernelError, digest, freeze, moment, name, require
from .coordinates import coordinates, wgs84_to_gcj02
from .navigation import OfflineNavigation, RouteQuote, Segment
from .poi import AmapPoiPlace, POI_ENDPOINT, POI_LIMIT, PoiLists, PoiSearchResult, PoiStore, parse_pois

ENDPOINT = "https://restapi.amap.com/v5/direction/transit/integrated"
PROVIDER = "amap-transit-v5@1"
MAX_RESPONSE_BYTES = 1024 * 1024
CHINA = ZoneInfo("Asia/Shanghai")
# Leave the full 60s activation window plus 5s for preparing the trip.
CACHE_ACTIVATION_RESERVE_S = 65
_STOP_GATEWAY_ERRORS = {"amap_bad_key", "amap_quota", "amap_invalid_params", "amap_account_banned",
                   "amap_permission", "amap_key_suspended", "amap_daily_quota", "amap_clock",
                   "amap_storage"}


@dataclass(frozen=True, slots=True, repr=False)
class _Credential:
    value: str

    def __repr__(self):
        return "<redacted-amap-key>"

    __str__ = __repr__


@dataclass(frozen=True)
class AmapRouteQuote(RouteQuote):
    """Provider-local extension leaves offline wire facts/identities unchanged."""
    requested_at: datetime | None = None
    fare_known: bool = True
    assumptions: tuple[str, ...] = ()
    evidence: tuple = ()

    @property
    def cost_minor(self):
        return super().cost_minor if self.fare_known else None


class AmapError(KernelError):
    """Only local, allowlisted explanations; never provider info, URL or key."""
    def __init__(self, reason, message, *, transient=False):
        super().__init__(ReasonCode.PROVIDER_UNAVAILABLE, message)
        self.reason_code = reason
        self.transient = transient


def _error(reason, message, transient=False):
    return AmapError(reason, message, transient=transient)


def _require_unlocked_storage(storage):
    # CPython RLock ownership is thread-local. Workers can queue behind other
    # threads, but cannot run while their awaiting caller owns this lock.
    if storage._lock._is_owned():
        raise _error("amap_storage", "调用方正在持有生活存储锁，请在事务外请求高德")


@dataclass(frozen=True)
class AmapConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    city_code: str = ""
    max_gateways: int = 3
    timeout_s: float = 5
    retries: int = 1
    cache_ttl_s: int = 120
    departure_bucket_s: int = 60
    requests_per_second: float = 2
    daily_quota: int = 100
    uncertainty_s: int = 120
    uncertainty_ratio: float = .2
    manual_fallback: bool = False
    # The original transit opt-in covers coordinates, not outbound search terms.
    public_poi_search_enabled: bool = False

    @classmethod
    def from_plugin(cls, config):
        """Read plugin config without copying it into a quote or activity."""
        return cls(enabled=config.get("amap_navigation_enabled", False),
                   api_key=config.get("amap_navigation_key", ""),
                   city_code=config.get("amap_navigation_city_code", ""),
                   public_poi_search_enabled=config.get("amap_poi_search_enabled", False))

    def validate(self):
        require(all(type(flag) is bool for flag in (self.enabled, self.manual_fallback, self.public_poi_search_enabled)),
                "invalid Amap flags")
        require(type(self.api_key) is str, "invalid Amap credential")
        require(type(self.city_code) is str and re.fullmatch(r"[0-9]{3,4}", self.city_code), "高德需要 citycode（如 021），不是 adcode")
        for value, low, high in ((self.max_gateways, 1, 5), (self.retries, 0, 2),
                                (self.cache_ttl_s, 1, 300), (self.departure_bucket_s, 1, 300),
                                (self.daily_quota, 1, 100000), (self.uncertainty_s, 60, 3600)):
            require(type(value) is int and low <= value <= high, "invalid Amap limits")
        for value, low, high in ((self.timeout_s, .1, 30), (self.requests_per_second, .1, 10),
                                (self.uncertainty_ratio, 0, 1)):
            require(type(value) in (float, int) and math.isfinite(value) and low <= value <= high, "invalid Amap limits")


@dataclass(frozen=True)
class StablePlace:
    place_id: str
    lon: float
    lat: float
    coordinate_system: str
    city_code: str = ""

    def __post_init__(self):
        name(self.place_id)
        require(self.coordinate_system == "WGS84", "外部地点必须显式使用 WGS84（G04）")
        coordinates(self.lon, self.lat, self.coordinate_system)
        require(type(self.city_code) is str and (not self.city_code or re.fullmatch(r"[0-9]{3,4}", self.city_code)), "invalid place citycode")


class UsageGuard:
    """One shared guard per storage: persist attempts, never credentials/locations.

    All attempts, including retries/failures, consume the conservative daily
    budget before HTTP. No database transaction is held across external awaits.
    The aggregate metadata survives adapter recreation and process restart.
    """
    _lock = threading.Lock()
    _next = WeakKeyDictionary()

    def __init__(self, storage, config, *, clock, monotonic=time.monotonic):
        self.storage, self.config, self.clock, self.monotonic = storage, config, clock, monotonic

    async def acquire(self):
        _require_unlocked_storage(self.storage)
        with self._lock:
            now = self.monotonic()
            slot = max(now, self._next.get(self.storage, now))
            if slot - now > self.config.timeout_s:
                raise _error("amap_local_rate_limit", "本地高德请求限流，请稍后再试")
            # Same storage object is shared by all city embedders in one process.
            self._next[self.storage] = slot + 1 / self.config.requests_per_second
        if slot > now:
            await asyncio.sleep(slot - now)
        # Re-evaluate the calendar after waiting: a midnight queue must charge
        # the day on which the request will actually be sent.
        try:
            await asyncio.to_thread(self._charge_attempt)
        except AmapError:
            raise
        except Exception:
            raise _error("amap_storage", "高德额度存储不可用，已停止请求") from None

    def _charge_attempt(self):
        # SQLite BEGIN IMMEDIATE and the storage lock may wait for seconds.
        # Charge the actual day after any such wait, without blocking asyncio.
        with self.storage.transaction() as db:
            day = moment(self.clock()).astimezone(CHINA).date().isoformat()
            row = db.execute("SELECT value FROM life_metadata WHERE key='city_amap_usage'").fetchone()
            usage = json.loads(row[0]) if row else {"day": day, "attempts": 0}
            if usage["day"] != day:
                # Clock rollback must not reset today's quota.
                if usage["day"] > day:
                    raise _error("amap_clock", "本地时间回退，已暂停高德请求")
                usage = {"day": day, "attempts": 0}
            if usage["attempts"] >= self.config.daily_quota:
                raise _error("amap_daily_quota", "本地高德日额度已用尽；可使用显式手工路线")
            usage["attempts"] += 1
            db.execute("INSERT INTO life_metadata(key,value) VALUES ('city_amap_usage',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(usage),))


async def _http(params, timeout_s, *, endpoint=ENDPOINT):
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            # Credentials stay wrapped in caller frames and injected transports.
            # Only the actual HTTP boundary receives their plaintext value.
            async with session.get(endpoint, params={**params, "key": params["key"].value}, allow_redirects=False) as response:
                if response.status != 200:
                    raise _error("amap_http", "高德 HTTP 服务不可用", response.status in (408, 429) or response.status >= 500)
                if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                    raise _error("amap_response", "高德响应过大，已拒绝")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise _error("amap_response", "高德响应过大，已拒绝")
                    chunks.append(chunk)
        return json.loads(b"".join(chunks))
    except AmapError:
        raise
    except (asyncio.TimeoutError, aiohttp.ClientError):
        raise _error("amap_timeout", "高德请求超时或网络不可用", True) from None
    except (ValueError, UnicodeError):
        raise _error("amap_response", "高德响应格式无效") from None


def _status(payload):
    if not isinstance(payload, dict):
        raise _error("amap_response", "高德响应格式无效")
    if payload.get("status") == "1" and payload.get("infocode", "10000") == "10000":
        return
    code = str(payload.get("infocode", ""))
    if code in {"10001", "10002", "10005", "10006", "10007", "10008", "10009", "10011", "10012", "10013"}:
        raise _error("amap_bad_key", "高德 Key 无效或未获 Web服务 授权；请检查控制台配置")
    # Official meanings: https://lbs.amap.com/api/webservice/guide/tools/info
    if code == "10026":
        raise _error("amap_account_banned", "高德账号已被封禁；请在控制台申诉")
    if code in {"10041", "20011"}:
        raise _error("amap_permission", "高德接口权限已过期或缺少海外地图权限；请检查控制台")
    if code in {"10003", "10010", "10044", "10045", "40000", "40002", "40003"}:
        raise _error("amap_quota", "高德额度或服务余额不可用；请检查控制台")
    if code == "10004":
        # A suspended key only recovers next minute; no retry for this quote.
        raise _error("amap_key_suspended", "高德 Key 因每分钟访问超限已暂停，请下一分钟再试")
    if code in {"10014", "10015", "10016", "10017", "10019", "10020", "10021"}:
        raise _error("amap_busy", "高德暂时繁忙或限流，请稍后再试", True)
    if code in {"20000", "20001", "20002", "20003"}:
        raise _error("amap_invalid_params", "高德拒绝路线参数；请检查坐标、城市编码和出发时间")
    raise _error("amap_provider", "高德未返回可用路线；请检查覆盖和服务授权")


def _seconds(value, *, zero=False):
    if type(value) is int:
        result = value
    elif type(value) is str and re.fullmatch(r"[0-9]{1,7}", value):
        result = int(value)
    else:
        raise ValueError("duration unavailable")
    if not (0 if zero else 1) <= result <= 7 * 86400:
        raise ValueError("duration out of bounds")
    return result


def _fare(value):
    if value is None or value == "" or value == []:
        return None
    if type(value) not in (str, int, float) or len(str(value)) > 32:
        raise ValueError("invalid fare")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 100000:
            raise ValueError("invalid fare")
        # CNY yuan -> fen, round up, never under-reserve a fractional fen.
        return int((amount * 100).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation:
        raise ValueError("invalid fare") from None


def _cost(record):
    value = record.get("cost", {})
    return value if isinstance(value, dict) else {}


def _parse_plan(plan, allowed_modes, *, gateway_at_origin=True):
    """v5 cost.duration/transit_fee; inherited v3 walking and busline details.

    No raw response/geometry is retained. Only normalized mode/time facts leave
    this boundary. A missing fare stays None; unknown modes never become a bus.
    """
    duration = _seconds(_cost(plan).get("duration"))
    segments = plan.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100:
        raise ValueError("no segments")
    legs, segment_fares = [], []
    for part in segments:
        if not isinstance(part, dict) or part.get("taxi"):
            raise ValueError("unsupported segment")
        railway = part.get("railway")
        if railway and (not isinstance(railway, dict) or any(railway.values())):
            raise ValueError("railway unsupported")
        walking = part.get("walking")
        if walking:
            if not isinstance(walking, dict):
                raise ValueError("walking unavailable")
            seconds = _seconds(walking.get("duration", _cost(walking).get("duration")), zero=True)
            if seconds:
                legs.append(("walk", seconds))
        bus = part.get("bus") or {}
        lines = bus.get("buslines", []) if isinstance(bus, dict) else None
        if not isinstance(lines, list):
            raise ValueError("bus unavailable")
        if lines:
            # buslines lists alternatives for this segment, not serial legs.
            line = lines[0]
            kind = line.get("type")
            if kind in ("地铁线路", "轻轨线路"):
                mode = "metro"
            elif kind in ("普通公交线路", "公交线路", "快速公交系统", "旅游专线", "机场巴士"):
                mode = "bus"
            else:
                raise ValueError("unknown bus mode")
            seconds = _seconds(line.get("duration", _cost(line).get("duration")))
            legs.append((mode, seconds))
            # Zero is not evidence that a bus/metro ride is free.
            segment_fares.append(_fare(_cost(part).get("transit_fee")) or None)
    if not legs:
        raise ValueError("no route legs")
    vehicle_modes = [mode for mode, _ in legs if mode in ("bus", "metro")]
    if vehicle_modes and vehicle_modes[0 if gateway_at_origin else -1] not in allowed_modes:
        raise ValueError("incompatible gateway mode")
    if sum(seconds for _, seconds in legs) > duration:
        raise ValueError("inconsistent provider duration")
    fare = _fare(_cost(plan).get("transit_fee"))
    if fare is None and segment_fares and all(value is not None for value in segment_fares):
        fare = sum(segment_fares)
    if vehicle_modes and fare == 0:
        fare = None
    elif not vehicle_modes and fare is None:
        fare = 0
    return duration, fare, tuple(legs)


def _provider_coordinates(place):
    if isinstance(place, AmapPoiPlace):
        return place.poi.gcj02
    return wgs84_to_gcj02(place.lon, place.lat)


def transit_parameters(origin, destination, departure_at, config):
    """Allowlist outgoing fields; stable place IDs never leave the process."""
    first = _provider_coordinates(origin)
    second = _provider_coordinates(destination)
    china_time = moment(departure_at).astimezone(CHINA)
    return {"key": _Credential(config.api_key), "origin": "%.5f,%.5f" % first,
        "destination": "%.5f,%.5f" % second,
        "city1": origin.city_code or config.city_code,
        "city2": destination.city_code or config.city_code,
        "date": china_time.strftime("%Y-%m-%d"), "time": china_time.strftime("%H-%M"),
        "show_fields": "cost", "AlternativeRoute": "5", "strategy": "0", "output": "json"}


class AmapNavigation:
    def __init__(self, offline, places, config, *, storage, transport=None, poi_transport=None, clock=None):
        require(isinstance(offline, OfflineNavigation), "Amap requires the confirmed offline graph")
        config.validate()
        require(config.enabled and config.api_key.strip(), "Amap is disabled or has no key")
        require(offline.currency == "CNY", "Amap fares require a CNY wallet")
        self.offline, self.config = offline, config
        places = tuple(places)
        require(all(isinstance(place, StablePlace) for place in places),
                "外部地点必须是显式校验的 WGS84 StablePlace（G04）")
        self.places = {place.place_id: place for place in places}
        require(len(self.places) == len(places) and set(self.places) == set(offline.external_places), "stable external places must match offline identities")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transport = transport or _http
        self._poi_transport = poi_transport or partial(_http, endpoint=POI_ENDPOINT)
        self.guard = UsageGuard(storage, config, clock=self.clock)
        self._poi_store = PoiStore(storage)
        self._poi_lists = PoiLists(config.cache_ttl_s)
        self._cache = {}
        self._cache_lock = threading.Lock()

    async def asearch_pois(self, public_place_name, *, city_code=None):
        """Search a public place name after the trusted host has enabled POI egress."""
        if not self.config.public_poi_search_enabled:
            return PoiSearchResult("error", reason_code="amap_poi_disabled")
        if city_code is None:
            city_code = self.config.city_code
        if (type(city_code) is not str or not re.fullmatch(r"[0-9]{3,4}", city_code)
                or type(public_place_name) is not str or not 1 <= len(public_place_name) <= 80
                or not public_place_name.isprintable()
                or not re.fullmatch(r"[\w ()（）·.'’\-]+", public_place_name)
                or not any(char.isalpha() for char in public_place_name)
                or self.config.api_key.strip() in public_place_name):
            return PoiSearchResult("error", reason_code="amap_poi_invalid_query")
        keyword = public_place_name.strip()
        cache_key = digest((city_code, keyword))
        items = self._poi_lists.cached(cache_key)
        if items is None:
            params = {"key": _Credential(self.config.api_key), "keywords": keyword, "region": city_code,
                      "city_limit": "true", "page_size": str(POI_LIMIT), "page_num": "1", "output": "json"}
            try:
                payload, _ = await self._request(params, self._poi_transport)
                items = parse_pois(payload, city_code, moment(self.clock()), self.config.api_key.strip())
            except AmapError as error:
                return PoiSearchResult("error", reason_code=error.reason_code)
            except ValueError:
                return PoiSearchResult("error", reason_code="amap_poi_response")
            self._poi_lists.cache(cache_key, items)
        if items:
            try:
                _require_unlocked_storage(self.guard.storage)
                items = await asyncio.to_thread(self._poi_store.resolve_saved, items)
            except Exception:
                return PoiSearchResult("error", reason_code="amap_storage")
        return self._poi_lists.issue(items, moment(self.clock()))

    def select_poi(self, list_token, *, index=None, poi_id=None):
        """Persist exactly one item from this adapter's live list (zero-based index)."""
        if not self.config.public_poi_search_enabled:
            raise _error("amap_poi_disabled", "高德地点搜索未启用")
        if type(list_token) is not str or len(list_token) != 32:
            raise _error("amap_poi_list_unavailable", "地点列表不存在或已过期，请重新搜索")
        items = self._poi_lists.resolve(list_token)
        if items is None:
            raise _error("amap_poi_list_unavailable", "地点列表不存在或已过期，请重新搜索")
        if type(index) is int and poi_id is None and 0 <= index < len(items):
            item = items[index]
        elif index is None and type(poi_id) is str:
            item = next((item for item in items if item.poi_id == poi_id), None)
        else:
            item = None
        if item is None:
            raise _error("amap_poi_not_in_list", "只能选择当前高德地点列表中的一项")
        try:
            place = self._poi_store.save(item)
        except Exception:
            raise _error("amap_storage", "高德地点存储不可用，未能确认所选地点") from None
        if place is None:
            raise _error("amap_poi_list_unavailable", "所选地点已被保存为其他位置，请重新搜索后确认")
        return place

    def quote(self, origin, destination, departure_at):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aquote(origin, destination, departure_at))
        raise _error("amap_async_required", "事件循环中必须 await navigation.aquote，不能同步阻塞调度器")

    async def aquote(self, origin, destination, departure_at):
        options = await self.aquote_options(origin, destination, departure_at)
        chosen = next((quote for quote in options if getattr(quote, "fare_known", True)), options[0])
        if not getattr(chosen, "fare_known", True):
            fallback = self._manual_quote(origin, destination, departure_at)
            if fallback is not None:
                return fallback
        return chosen

    def _manual_quote(self, origin, destination, departure_at):
        if not self.config.manual_fallback:
            return None
        known = set(self.offline.nodes) | set(self.places)
        if origin in known and destination in known:
            try:
                return self.offline.quote(origin, destination, departure_at)
            except KernelError:
                pass  # Preserve the provider's specific failure or unknown fare.
        return None

    async def aquote_options(self, origin, destination, departure_at):
        try:
            return await self._options(origin, destination, moment(departure_at))
        except AmapError:
            fallback = self._manual_quote(origin, destination, departure_at)
            if fallback is not None:
                return (fallback,)
            raise

    async def _external(self, place_id):
        place = self.places.get(place_id)
        if place is None and place_id.startswith("amap-poi:"):
            _require_unlocked_storage(self.guard.storage)
            try:
                place = await asyncio.to_thread(self._poi_store.lookup, place_id)
            except Exception:
                raise _error("amap_storage", "高德地点存储不可用，已停止路线查询") from None
        require(place is not None, "unknown/equal route endpoints")
        return place

    async def _options(self, origin, destination, departure_at):
        nodes = self.offline.nodes
        require(type(origin) is str and type(destination) is str and origin != destination,
                "unknown/equal route endpoints")
        if origin in nodes and destination in nodes:
            return (self.offline.quote(origin, destination, departure_at),)
        if origin not in nodes and destination not in nodes:
            first = await self._external(origin)
            second = await self._external(destination)
            plans, requested_at = await self._plans(first, second, departure_at)
            return self._sort(self._build_quote(origin, destination, departure_at, requested_at, plan,
                                                first, second) for plan in plans)
        outward = origin in nodes
        external = await self._external(destination if outward else origin)
        candidates = []
        timed_access = self.offline.access_policy is not None
        availability = self.offline._availability_for(departure_at, departure_at + timedelta(days=7))
        for gateway in self.offline.graph["gateways"]:
            modes = set(gateway["modes"]) & {"bus", "metro"}
            place_id = gateway["place_id"]
            if not modes or not availability(nodes[place_id], node=True).intervals:
                continue
            local_origin, local_destination = (origin, place_id) if outward else (place_id, destination)
            try:
                local = () if local_origin == local_destination else self.offline.quote(local_origin, local_destination, departure_at).segments
            except KernelError:
                if timed_access and not outward:
                    # The gate or path may open by the time the external leg arrives.
                    candidates.append((math.inf, gateway["gateway_id"], gateway, (), modes))
                continue
            local_s = sum(s.duration_s for s in local)
            if timed_access and outward and not self.offline._decision(
                    nodes[place_id], departure_at + timedelta(seconds=local_s), node=True).allowed:
                continue
            candidates.append((local_s, gateway["gateway_id"], gateway, local, modes))
        candidates.sort(key=lambda row: (row[0], row[1]))
        candidates = candidates[:self.config.max_gateways]
        if not candidates:
            raise _error("amap_no_gateway", "没有可达且模式兼容的常住图出口")
        options, last_error = [], None
        for local_s, _, gateway, local, modes in candidates:
            try:
                api_departure = departure_at + timedelta(seconds=local_s if outward else 0)
                gateway_place = StablePlace(gateway["place_id"], gateway["lon"], gateway["lat"],
                                            gateway["coordinate_system"], self.config.city_code)
                first, second = (gateway_place, external) if outward else (external, gateway_place)
                gateway_id = gateway["gateway_id"]
                plans, requested_at = await self._plans(first, second, api_departure,
                    gateway_id=gateway_id, modes=modes, outward=outward)
                for plan in plans:
                    try:
                        options.append(self._build_quote(origin, destination, departure_at, requested_at, plan,
                            first, second, gateway_id=gateway_id, local=local, outward=outward))
                    except KernelError:
                        last_error = _error("amap_no_gateway", "到达时没有可通行的常住图入口或接驳路径")
            except AmapError as error:
                last_error = error
                # Bad credentials/quota/params apply to every gateway; don't burn
                # more calls. A timeout or route miss may differ by gateway.
                if error.reason_code in _STOP_GATEWAY_ERRORS:
                    break
        if not options:
            raise last_error or _error("amap_no_route", "高德没有可用公交方案；可使用显式手工路线")
        return self._sort(options)

    @staticmethod
    def _sort(options):
        return tuple(sorted(options, key=lambda q: (q.duration_s, q.cost_minor if q.cost_minor is not None else math.inf, q.route_id)))

    def _build_quote(self, origin, destination, departure_at, requested_at, plan, first, second,
                     *, gateway_id=None, local=(), outward=True):
        duration, fare, legs = plan
        # At least 120s covers minute-resolution requests and activation latency.
        margin = max(120, self.config.uncertainty_s, math.ceil(duration * self.config.uncertainty_ratio))
        waiting = duration - sum(seconds for _, seconds in legs)
        vehicles = [mode for mode, _ in legs if mode in ("bus", "metro")]
        mode = vehicles[0 if outward else -1] if vehicles else "walk"
        # API stops are not confirmed safe stops. Execute one coarse stage.
        city = Segment(first.place_id, second.place_id, mode, duration + margin,
                       fare if fare is not None else 0, margin, PROVIDER)
        if gateway_id is not None and not outward and self.offline.access_policy is not None:
            arrival = departure_at + timedelta(seconds=city.duration_s)
            gateway = self.offline.nodes[second.place_id]
            require(self.offline._decision(gateway, arrival, node=True).allowed, "gateway is closed at arrival")
            if second.place_id != destination:
                local = self.offline.quote(second.place_id, destination, arrival).segments
        parts = (*local, city) if outward else (city, *local)
        assumptions = ("estimated-not-live-vehicle-position", "simulated-online-execution",
            "fare-estimate-charged-on-external-stage-entry", "waiting-location-unknown",
            "safe-stop-only-at-configured-endpoint", "activation-window-s:60")
        details = {"provider_duration_s": duration, "fare_minor": fare, "waiting_s": waiting,
                   "legs": tuple({"mode": mode, "duration_s": seconds} for mode, seconds in legs)}
        if gateway_id is not None:
            assumptions += ("gateway-candidates-capped:" + str(self.config.max_gateways),)
            details["gateway_id"] = gateway_id
        assumptions += ("uncertainty-margin-s:" + str(margin), "request-time-rounded-to-minute", "no-geometry-retained")
        evidence = freeze((details,))
        graph = self.offline.graph
        identity = digest((origin, destination, departure_at, requested_at, graph["digest"], parts, evidence))
        return AmapRouteQuote(identity, origin, destination, departure_at,
            requested_at + timedelta(seconds=self.config.cache_ttl_s), parts, "CNY",
            graph["graph_id"], graph["version"], graph["digest"], graph["attribution"],
            PROVIDER, requested_at, fare is not None, assumptions, evidence)

    async def _plans(self, first, second, departure_at, *, gateway_id=None, modes=None, outward=True):
        bucket = int(departure_at.timestamp()) // self.config.departure_bucket_s
        key = (first.place_id, second.place_id, gateway_id, outward, bucket)
        now = moment(self.clock())
        with self._cache_lock:
            self._cache = {key: value for key, value in self._cache.items()
                           if value[1] + timedelta(seconds=self.config.cache_ttl_s)
                           >= now + timedelta(seconds=CACHE_ACTIVATION_RESERVE_S)}
            cached = self._cache.get(key)
        if cached:
            return cached
        params = transit_parameters(first, second, departure_at, self.config)
        payload, requested_at = await self._request(params, self.transport)
        route = payload.get("route")
        plans = route.get("transits") if isinstance(route, dict) else None
        if not isinstance(plans, list) or not plans:
            raise _error("amap_no_route", "高德没有返回公交方案；不可凭空生成路线")
        # Only allowlisted immutable facts enter the short-lived cache.
        parsed = []
        allowed_modes = {"bus", "metro"} if modes is None else modes
        for plan in plans[:10]:
            try:
                parsed.append(_parse_plan(plan, allowed_modes, gateway_at_origin=outward))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if not parsed:
            raise _error("amap_response", "高德路线字段缺失、模式不兼容或时间不一致，已拒绝该方案")
        result = (tuple(parsed), requested_at)
        with self._cache_lock:
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    async def _request(self, params, transport):
        for attempt in range(self.config.retries + 1):
            try:
                await self.guard.acquire()
                requested_at = moment(self.clock())
                # Also bound injected transports. Cancellation is propagated.
                payload = await asyncio.wait_for(transport(params, self.config.timeout_s), self.config.timeout_s)
                _status(payload)
                return payload, requested_at
            except asyncio.TimeoutError:
                error = _error("amap_timeout", "高德请求超时，请稍后再试", True)
            except AmapError as caught:
                error = caught
            except Exception:
                # Transport libraries may embed full request URLs in exceptions.
                error = _error("amap_transport", "高德请求失败，详情已隐藏以保护凭据", True)
            if not error.transient or attempt == self.config.retries:
                raise error from None
            await asyncio.sleep(.25 * 2**attempt)


def navigation_from_config(store, graph_id, *, currency, places=(), services=(), config=None, **adapter_options):
    """Explicit embedding factory. Off/no key is exactly OfflineNavigation.

    No config schema installation, scheduler registration or request happens
    here. The host still owns authorization and invoking this opt-in component.
    """
    config = config or AmapConfig()
    places = tuple(places)
    offline = OfflineNavigation(store, graph_id, currency=currency,
        external_places=tuple(place.place_id for place in places), services=services)
    if not config.enabled or not config.api_key.strip():
        return offline
    return AmapNavigation(offline, places, config, storage=store.storage, **adapter_options)
