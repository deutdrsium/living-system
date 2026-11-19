"""Pure W6 study rules. Navigation and online work time are caller inputs."""
from collections.abc import Mapping
from typing import Protocol

from ..academics.study import timestamp_key
from ..kernel.codec import integer, require


class StudyWalk(Protocol):
    async def arrived(self, owner_key: str, context_key: str, cursor: Mapping) -> bool:
        """Confirm arrival for this cursor. False leaves the walk step pending."""
        ...


GENERATOR_VERSION = 1
KINDS = ("draft", "note", "review")


def allocate_window(chunks, *, window_s, travel_s=0, departure_s=0):
    """Reserve overhead once and distribute the remaining seconds, earliest due first."""
    window_s = integer(window_s)
    travel = min(window_s, integer(travel_s))
    departure = min(window_s - travel, integer(departure_s))
    available = window_s - travel - departure
    remaining = available
    items, seen = [], set()
    for chunk in sorted(chunks, key=lambda item: (timestamp_key(item), item["assignment_id"], item["chunk_index"])):
        identity = (chunk["assignment_id"], chunk["chunk_index"])
        require(identity not in seen, "duplicate study chunk")
        seen.add(identity)
        work = integer(chunk["remaining_work_s"])
        allocated = min(remaining, work)
        items.append({"assignment_id": identity[0], "chunk_index": identity[1],
                      "allocated_s": allocated, "shortfall_s": work - allocated})
        remaining -= allocated
    return {"window_s": window_s, "travel_reserved_s": travel, "departure_reserved_s": departure,
            "overhead_shortfall_s": travel_s + departure_s - travel - departure,
            "zero_window": window_s == 0, "zero_work_window": available == 0,
            "allocated_s": available - remaining, "shortfall_s": sum(item["shortfall_s"] for item in items),
            "unused_s": remaining, "items": items}


def work_milestone(required_s, baseline_s, index):
    return baseline_s + ((required_s - baseline_s) * (index + 1) + len(KINDS) - 1) // len(KINDS)


def generate_v1(kind, unit, assignment):
    """Deterministic fictional text. Source bodies are never generator inputs."""
    topic = unit["topic"]
    concept = next(iter(unit["concepts"]), topic)
    hint = next(iter(unit["questions"]), f"{concept}在这个例子中起什么作用？")
    example = "虚构材料：青石书屋试行延长借阅期。夜班读者获得更多阅读时间，热门书的预约队列也变长了。"
    title, text, next_step = {
        "draft": ("练习草稿", f"围绕「{assignment['title']}」，我尝试用「{concept}」分析「{topic}」。\n\n"
                  f"{example}\n这两种变化同时出现。草稿暂把它们视为不同读者的体验，接下来要比较两组预约等待时间。",
                  "从草稿中圈出主张和依据，并写一个可能的反例。"),
        "note": ("学习笔记", f"主题：{topic}\n概念：{concept}\n{example}\n"
                 "区分观察和解释：等待变长是这个虚构案例的观察；延长借阅期是否是主因，仍需比较。",
                 "合上笔记，用自己的话复述观察和解释的区别。"),
        "review": ("复习问题", f"主题：{topic}\n{example}\n1. {hint}\n"
                   f"2. 怎样用「{concept}」解释案例？哪条新证据会改变你的解释？",
                   "先独立作答，再回看笔记补上遗漏的依据。"),
    }[kind]
    return {"kind": kind, "title": title, "text": text, "next_step": next_step}
