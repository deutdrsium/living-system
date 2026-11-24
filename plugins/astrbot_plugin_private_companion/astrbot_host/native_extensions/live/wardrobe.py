"""Choose individual wardrobe items and reflect them in the loaded VTS model."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)
LAYERS = {
    "top": ("ParamTop", 7), "mid": ("ParamMid", 4),
    "bottom": ("ParamBottom", 7), "outer": ("ParamOuter", 0),
    "shoes": ("ParamShoes", None), "bag": ("ParamBag", 0),
    "ensemble": ("ParamEnsemble", 0), "hat": ("ParamHat", 0),
    "scarf": ("ParamScarf", 0),
}
INITIAL = {
    "top": "shirt_gray_long", "mid": "sweater_cream",
    "bottom": "skirt_pleated_bluegray", "outer": None,
    "shoes": "shoes_loafers_brown", "bag": None, "ensemble": None,
    "hat": None, "scarf": None,
}
PREFIX = "FourSeasonsWardrobe.Wardrobe_"
SEASONAL = {f"FourSeasonsWardrobe.Outfit{name}.exp3.json"
            for name in ("Autumn", "Winter", "Summer", "Home")}
CONTROL = re.compile(r"<wardrobe\s*>(.*?)</wardrobe\s*>", re.IGNORECASE | re.DOTALL)
ITEM_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def expression(item_id):
    return PREFIX + item_id + ".exp3.json"


def catalogue(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    model_id = data.get("model_id")
    if not isinstance(model_id, str) or not re.fullmatch(r"[a-f0-9]{32}", model_id):
        raise ValueError("invalid wardrobe model id")
    items = {}
    for item in data.get("items", []):
        if not isinstance(item, dict):
            raise ValueError("invalid wardrobe item")
        item_id, layer = item.get("id"), item.get("layer")
        if (not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id)
                or item_id in items or layer not in LAYERS
                or item.get("model_parameter") != LAYERS[layer][0]
                or type(item.get("model_value")) is not int
                or not isinstance(item.get("name"), str)):
            raise ValueError("invalid wardrobe item mapping")
        items[item_id] = item
    if not items:
        raise ValueError("empty wardrobe")
    return model_id, items


def normalize(selection, items):
    if not isinstance(selection, dict) or set(selection) != set(LAYERS):
        raise ValueError("wardrobe choice must contain every layer")
    result = dict(selection)
    for layer, value in result.items():
        if value is None:
            if layer == "shoes":
                raise ValueError("shoes must be selected")
        elif not isinstance(value, str) or value not in items or items[value]["layer"] != layer:
            raise ValueError("unknown item for wardrobe layer")
    if result["ensemble"] is not None:
        result.update(top=None, mid=None, bottom=None)
    elif result["top"] is None or result["bottom"] is None:
        raise ValueError("an ordinary outfit needs a top and bottom")
    selected = {value for value in result.values() if value is not None}
    for item_id in selected:
        conflicts = items[item_id].get("conflicts_with", [])
        if not isinstance(conflicts, list) or selected.intersection(conflicts):
            raise ValueError("conflicting wardrobe items")
    return result


def extract(text):
    choices = []

    def remove(match):
        try:
            choices.append(json.loads(match.group(1)))
        except (TypeError, ValueError):
            pass
        return ""

    cleaned = CONTROL.sub(remove, text)
    return (choices[-1] if choices else None), re.sub(r"\n{3,}", "\n\n", cleaned).strip()


class WardrobeController:
    def __init__(self, runtime):
        self.runtime = runtime
        self.catalog_path = runtime.data_dir / "wardrobe-catalog.json"
        self.state_path = runtime.data_dir / "wardrobe-current.json"
        self._selection = None
        self._dirty = True
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_socket = None
        self._last_model_id = ""
        self._last_model_age = None
        self._last_error = ""

    def _read(self):
        model_id, items = catalogue(self.catalog_path)
        if self._selection is None:
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))["selected"]
            except FileNotFoundError:
                saved = INITIAL
            self._selection = normalize(saved, items)
        return model_id, items

    def instruction(self):
        try:
            _, items = self._read()
            selection = normalize(self._selection, items)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            logger.warning("[Live2D衣柜] 读取衣柜失败: %s", type(error).__name__)
            return ""
        columns = ("id", "name", "season", "warmth", "formality", "colors", "is_accent",
                   "favorite", "conflicts_with", "acquired", "gift_from")
        rows = {layer: [[item.get(key) for key in columns] for item in items.values()
                        if item["layer"] == layer] for layer in LAYERS}
        status = "等待同步到模型" if self._dirty else "上次已同步到模型"
        return (
            "## Live2D 衣柜\n衣物属于你，可以根据已知天气、季节、场合、个人喜好和对话自然选择。"
            "未知偏好或来源不要编造；日常没有换衣理由时维持当前搭配。"
            "只有想换装时，在回复末尾附一行 <wardrobe>JSON</wardrobe>，程序会隐藏并执行；"
            "JSON 必须同时包含 top、mid、bottom、outer、shoes、bag、ensemble、hat、scarf，"
            "每层填该层一个单品 id，不穿的层填 null，鞋必须有 id。"
            "普通搭配必须有 top 和 bottom、ensemble 为 null；选连衣裙或睡衣时 top、mid、bottom 填 null。"
            "不要选择冲突单品，不要把选择意图说成已确认的设备执行结果。\n"
            f"当前搭配（{status}）：{json.dumps(selection, ensure_ascii=False, separators=(',', ':'))}\n"
            f"单品字段顺序：{','.join(columns)}；warmth 0–5，formality 0–3，favorite 0–1 或 null。\n"
            f"可选单品：{json.dumps(rows, ensure_ascii=False, separators=(',', ':'))}"
        )

    def checked_selection(self, payload):
        _, items = self._read()
        return normalize(payload, items)

    async def select(self, payload, *, expected_current=None):
        async with self._lock:
            try:
                _, items = self._read()
                selection = normalize(payload, items)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                logger.warning("[Live2D衣柜] 忽略无效搭配: %s", type(error).__name__)
                return False
            if (expected_current is not None and self._selection != expected_current
                    and self._selection != selection):
                return False
            if selection != self._selection or not self.state_path.exists():
                content = {"selected": selection, "updated_at": datetime.now(timezone.utc).isoformat()}
                temporary = self.state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self.state_path)
                self._selection = selection
                self._dirty = True
            self._wake.set()
            return True

    def _desktop_owns_wardrobe(self):
        desktop = getattr(getattr(self.runtime, "controller", None), "desktop_api", None)
        return desktop is not None and desktop.owns_wardrobe()

    async def _apply(self, items):
        if self._desktop_owns_wardrobe():
            return False
        selection = normalize(self._selection, items)
        wanted = {expression(value if value is not None else f"none_{layer}")
                  for layer, value in selection.items()}
        available = await self.runtime.vts.get_expressions()
        by_file = {row.get("file"): row for row in available if isinstance(row, dict)}
        if not wanted.issubset(by_file):
            raise RuntimeError("模型缺少衣柜表达式，请重新加载模型")
        changes = [(filename, False) for filename, row in by_file.items()
                   if row.get("active") and filename not in wanted
                   and (str(filename).startswith(PREFIX) or filename in SEASONAL)]
        changes += [(filename, True) for filename in sorted(wanted) if not by_file[filename].get("active")]
        for filename, active in changes:
            if self._desktop_owns_wardrobe():
                return False
            await self.runtime.vts.set_expression(filename, active=active, fade_time=0)
        return True

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                async with self._lock:
                    model_id, items = self._read()
                    connected = not self._desktop_owns_wardrobe() and await self.runtime._check_and_reconnect()
                    if connected:
                        info = await self.runtime.vts.get_model_info()
                        active_id = info.get("modelID") if info.get("modelLoaded") else ""
                        age = info.get("timeSinceModelLoaded")
                        socket = getattr(self.runtime.vts, "_ws", None)
                        reloaded = (active_id != self._last_model_id or socket is not self._last_socket
                                    or isinstance(age, (int, float)) and self._last_model_age is not None
                                    and age < self._last_model_age)
                        if active_id == model_id and (self._dirty or reloaded):
                            self._dirty = not await self._apply(items)
                            self._last_error = ""
                        elif active_id != model_id:
                            self._dirty = True
                        self._last_model_id, self._last_model_age, self._last_socket = active_id, age, socket
                    else:
                        self._dirty = True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._dirty = True
                message = f"{type(error).__name__}: {error}"
                if message != self._last_error:
                    logger.warning("[Live2D衣柜] 同步失败: %s", message)
                    self._last_error = message
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if not self.catalog_path.exists():
            return
        task = asyncio.create_task(self._run(), name="companion-v2:wardrobe")
        self.runtime._l2d_tasks.add(task)
        task.add_done_callback(self.runtime._l2d_tasks.discard)
