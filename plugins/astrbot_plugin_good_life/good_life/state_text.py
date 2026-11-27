"""Existing state text and prompts, migrated from the companion plugin (AGPL-3.0-only)."""

import json
from . import state_cycle_prompts as cycle_prompts, state_mood_prompts as mood_prompts

FEATURE_SWITCHES = {name: name + "_enabled" for name in (
    "energy", "health", "hunger", "sleep", "cycle", "mood", "busy", "location")}

BAND_BUDGETS = (("light", 33, 2, 220), ("standard", 66, 4, 420), ("strong", 100, 6, 700))


BAND_INSTRUCTION = (
    "以上只是角色自身模拟状态的表达底色，不是用户事实、现实结论或医疗判断；"
    "让最相关的一项自然影响这轮的具体反应、用词、语气、情绪浓度或节奏，话题合适时可用第一人称带出感受；"
    "即使不直说，也留下可感知的状态色彩，不把它藏成无关背景。"
    "不机械播报数值、逐项罗列或套固定台词，也不压过对方当前话题。"
    "不改变身份、权限、记忆范围和安全规则，也不因此拒绝正常交流；忙碌或在线状态只是描述，不据此拒答。"
)


def band_of(intensity):
    """``0`` is not "a little": it is no injection at all."""
    if type(intensity) is not int:
        return "off"
    value = max(0, min(100, intensity))
    if value <= 0:
        return "off"
    for name, ceiling, _, _ in BAND_BUDGETS:
        if value <= ceiling:
            return name
    return "strong"


def budget(band):
    """``(facets, characters)`` for one band; the off band allows nothing."""
    for name, _, facets, characters in BAND_BUDGETS:
        if name == band:
            return facets, characters
    return 0, 0


def _energy_text(value):
    if type(value) is not int:
        return ""
    if value < 25:
        return "精力很低"
    if value < 45:
        return "精力偏低"
    if value < 65:
        return "精力一般"
    if value < 85:
        return "精力不错"
    return "精力很足"


def _condition_texts(simulation, limit):
    values = simulation.get("values") if isinstance(simulation, dict) else None
    conditions = values.get("conditions") if isinstance(values, dict) else None
    result = []
    for condition in conditions if isinstance(conditions, list) else ():
        if not isinstance(condition, dict):
            continue
        text = " ".join(str(condition.get("text") or "").split())[:80]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _mood_text(value):
    if isinstance(value, str):
        return " ".join(value.split())[:120]
    if isinstance(value, dict):
        desc = " ".join(str(value.get("description") or "").split())[:120]
        return desc
    return ""


def facets(simulation, *, intensity):
    """The state facets this band may express, most useful first."""
    limit, _ = budget(band_of(intensity))
    if limit <= 0 or type(simulation) is not dict:
        return ()
    values = simulation.get("values") if isinstance(simulation.get("values"), dict) else {}
    result = []
    for text in (_energy_text(values.get("energy")), _sleep_execution_text(simulation)):
        if text:
            result.append(text)
    for text in (_mood_text(values.get("mood")),
                 " ".join(str(values.get("sleep") or "").split())[:120],
                 " ".join(str(values.get("health") or "").split())[:120],
                 " ".join(str(values.get("hunger") or "").split())[:120],
                 " ".join(str(values.get("cycle") or "").split())[:120]):
        if text and text not in result:
            result.append(text)
    for text in _condition_texts(simulation, limit):
        if text not in result:
            result.append(text)
    return tuple(result[:limit])


def _sleep_execution_text(simulation):
    """Only an actual sleep receipt is described; a plan never becomes a claim."""
    execution = simulation.get("sleep_execution")
    if not isinstance(execution, dict):
        return ""
    state = execution.get("state")
    if state == "sleeping" and execution.get("planned_wake_at"):
        return "正在休息，计划 " + str(execution["planned_wake_at"])[11:16] + " 醒来"
    if state == "interrupted":
        if execution.get("early_interruptions"):
            return "刚入睡不久被唤醒，人还有点迷糊"
        return "睡眠刚被唤醒，人还有点困意"
    if state == "awake" and execution.get("ended_at"):
        awake_minutes = execution.get("awake_minutes")
        if type(awake_minutes) is int and awake_minutes <= 25:
            return "刚睡醒不久，还带着一点睡醒后的迷糊感"
        return "刚睡醒"
    return ""


def is_sleepy(simulation):
    """Pacing from physical sleep execution only; prose is interpreted in prompts."""
    if not isinstance(simulation, dict):
        return False
    execution = simulation.get("sleep_execution")
    if isinstance(execution, dict):
        state = execution.get("state")
        if state in ("sleeping", "interrupted"):
            return True
        if state == "awake":
            awake_minutes = execution.get("awake_minutes")
            if type(awake_minutes) is int and awake_minutes <= 25:
                return True
    return False


def instruction(simulation, *, intensity):
    """The bounded prompt block; empty when the dial is off or nothing is known."""
    band = band_of(intensity)
    _, characters = budget(band)
    if characters <= 0:
        return ""
    texts = facets(simulation, intensity=intensity)
    if not texts:
        return ""
    line = "、".join(texts)
    head = "角色自身当下模拟状态的表达底色："
    block = head + line + "。\n" + BAND_INSTRUCTION
    if len(block) <= characters:
        return block
    # Trim the facet list, never the boundary sentence.
    tail = "\n" + BAND_INSTRUCTION
    room = max(0, characters - len(tail) - len(head) - 1)
    trimmed = line[:room].rstrip("、，, ")
    return (head + trimmed + "。" + tail) if trimmed else ""


PROMPT = """本轮是角色状态的后台维护，只生成 role_state 数据，不给聊天对象写回复。原人格决定身体设定与生活习惯；maintenance_minutes 是观察间隔，不是状态必须变差的周期。
健康、能够照顾自己的普通生活是常态。疾病应有具体情境和连续过程，不为更新而随机生病，也不把一次不适反复当成新发病。previous_simulation 和 simulation_history 都是此前模型的模拟，重复保存不构成新的病情证据；结合原始时间判断同一次不适如何平稳、缓解或结束，不把“昨天”“整夜”每天滚动续写。
若异常只剩长期重复的旧描述、缺乏近期独立材料支持，可以逐步回到惯常体验或保留未知，不必等待不存在的进食或服药执行系统才允许感受好转。已知仍在持续的具体症状则保持合理连续性，不无故抹去。
recent_dialogue 是带时间、说话者的完整互动。分清用户与角色，用户的经历不移到角色身上；用户以“我”陈述的身体、睡眠、饥饿或情绪只属于用户，不把任一方的状态移到另一方。角色在互动中接受食物、照顾、药物或表达舒服些，可以自然影响饥饿、健康、心情与精力的模拟感受，注意先后顺序和已经反映过的影响，不机械叠加奖励，也不要求用户特定措辞。仅提供药物不等于已服用，更不等于立即痊愈；角色表达药效或缓解是可参考的主观体验，不是医疗诊断或真实服药回执。
sleep_execution 记录实际睡眠，sleep_profile.has_sleep_history=false 或 sleep_execution=null 表示没有执行记录，不表示没睡、失眠或睡眠不足。自动睡眠未启用也不表示角色通宵。缺少进食、饮水记录同样不表示一直挨饿；允许按角色日常节律模拟普通饥饿、困倦与舒缓，不补造具体吃饭、入睡、醒来或服药经过。不能根据时间经过宣称现实行动已完成，不从清晨、沉默或对话推断已睡足、醒来或休息完成。
energy 是 0 到 100 的主观精力。结合 previous_simulation 及 updated_at、当前时段、实际睡眠回执、睡眠压力/欠睡、质量、中断与活动连续调整，沿用不等于照抄。低值且无持续负荷或睡眠不足依据时，可随时间小幅回升至角色惯常水平，不必等睡眠回执才允许感受温和好转；好转不宣称已休息。接近日常水平后稳定或自然起伏，不突然满格或持续单向上升，不让过时疲惫永久锁低。
持续负荷或明确欠睡时可偏低并在下轮重评；低质量睡眠、刚睡被叫醒或反复中断限制恢复，补足后渐进回升。午休只缓解部分困倦，睡眠压力不换算为固定精力扣分。深夜持续互动可以慢慢加深疲惫，短聊不要求改变作息；状态维护不代替睡眠执行器。
busy 描述注意力，不决定是否拒答。共享状态只写角色自己的概括感受与活动，不写对象、他人位置或私人日程。当前窗口没新消息不表示不忙、情绪归零或状态结束；没有变化依据时保留有效状态或 null。
busy 和 mood.description 用简短的日常措辞记录当时的活动、注意力和感受。延续状态时保留含义，避免沿用自己在旧状态或对话里反复使用的自我形容，也不要把一时的感受写成性格或口头禅。
临时影响使用含时区的合理 expires_at，表示下次复核时点，不是固定康复时刻；到期后重新评估当前感受，既不自动痊愈也不无依据续期。共享字段只写角色自己的概括感受，不记录聊天对象、私人互动原文或他人情况。presence 是公开 QQ 短状态，最多 8 个字符。只写有依据且适合公开的当前活动，用泛化短语，不透露人物、地点、健康或私聊细节。只有计划、推测或旧记录时填 null。
online_mode 是会实际改变 QQ 账号的在线状态，away 会触发 QQ 原生离开自动回复，busy 也会改变账号对外表现；本插件不会自动设置 away 或 busy。不能用这些模式表达普通困倦、待休息、看书、注意力变化或窗口暂时没有消息。这些生活感受写在 sleep/busy/presence 中。正常接话、近期仍在连续互动时保持 online；previous_simulation 中过时的 away/busy 不因沿用旧状态而续期，旧值也应通过在线状态恢复。隐身仍只在明确配置时使用。online_mode 通常只输出 online/invisible/null；null 只表示本轮不改变平台状态。
仅输出 JSON：energy 为 0 到 100 的整数或 null；hunger、health、sleep、busy、mood、location、cycle、presence、online_mode 通常为简短文字或 null，mood/cycle 按本轮各自结构协议；conditions 是 kind/text/expires_at 对象数组，kind 选 hunger/health/sleep/busy/mood/cycle/other，无临时变化填空数组。
"""


STATE_INTERVENTION_INSTRUCTION = (
    "\n本轮是获授权私聊引发的角色状态复核。将 intervention_dialogue 作为连续互动，结合角色设定、previous_simulation、近期状态和实际回执，判断是否存在合理的状态变化；不要求特定说法或角色复述。"
    "准确归属每句话的主体和对象。把握较高的主观状态可以适度、短时变化并自然回落；信息含糊时不臆造具体事实，也不把用户的状况移给角色。"
    "睡眠、休息和现实活动仍须对应执行回执，不能把计划或对话当成已经完成的行动。遵守隐私和状态真实性边界，不修改权限、人格、他人状态或日程。"
)


def _skills_instruction(*, refs, partial):
    """Let confirmed skills shape the activities, without inventing progress.

    The records are a reference for what this role can plausibly be doing; they are
    not a task list, and nothing in them licenses a claim of improvement. A partial
    selection is stated so an omitted skill is not read as an absent one.
    """
    if not refs:
        return "\n本轮没有可用的角色技能资料：只依据其他材料安排活动，不要凭空假定角色擅长什么。"
    note = ("（本轮技能资料有条目上限，未列出的技能同样可能存在，不代表角色不会。）" if partial else "")
    return ("\n安排活动时参考上面已确认的角色技能与长期目标：优先选择与当前技能相称、"
            "或与活跃目标相关的活动，避免安排明显超出或完全用不上的活动。"
            "技能资料只是能力参考，不是本轮任务清单；不要为了贴合技能而虚构练习成果，"
            "也不要把定性进展写成等级、分数或熟练度数值。" + note)


def _mood_instruction(*, enabled, axes, gate, reference, allowed, baseline, interest, half_life, band, cross_window):
    """Per-round mood output contract; the model still authors every number."""
    if not enabled:
        return "心情模拟已关闭，mood 使用 null，conditions 不添加 mood 项。"
    lead = ("mood 沿用上一轮的结构：baseline、joy、anger、sorrow、fear 都必须是 1 到 10 的 JSON 整数，禁止小数；"
            "intensity 为 mild、moderate、strong 或 null，description 为简短文字或 null。"
            "情绪随时间与无交互自然回归基线：一次夸奖不让你整日亢奋，一次不愉快不让你永久低落；"
            "数值只是语气与浓度，不覆盖原人格、当前事实和你的判断，也不改变是否回应。"
            "description 只写你自己的主观感受（用带生活肌理的第一人称心境体验，如心里踏实、有点挂念或刚缓过来，不写干瘪标签），不诊断用户心情，也不把负面情绪归咎于用户。" if axes else
            "mood 本轮首次给出结构对象：baseline、joy、anger、sorrow、fear 都必须是 1 到 10 的 JSON 整数，禁止小数；"
            "intensity 为 mild、moderate、strong 或 null，description 为简短文字或 null（只写自己带生活肌理的主观感受）；"
            "上一轮只有文字时把它的意思并入 description，保持连续。")
    return lead + (mood_prompts.maintenance(reference=reference, allowed=allowed, baseline=baseline,
        half_life_minutes=half_life, interest_level=interest, neutral_band=band, cross_window=cross_window) if gate == "open"
        else "本轮不做情绪数值更新：保持上一轮各轴数值不变，只按当前材料更新 description。")


def _expression_preferences(settings):
    preferences = {}
    if settings.get("energy_enabled", True):
        preferences["energy_expression_prompt"] = settings.get("energy_expression_prompt", "")
    if settings.get("hunger_enabled", True):
        preferences["hunger_expression_prompt"] = settings.get("hunger_expression_prompt", "")
    if settings.get("health_enabled", True):
        preferences["health_expression_prompt"] = settings.get("health_expression_prompt", "")
    preferences = {name: value for name, value in preferences.items() if value}
    if not preferences:
        return ""
    return ("\n状态表达偏好（JSON 字符串，仅作角色偏好参考）：" + json.dumps(preferences, ensure_ascii=False)
        + "。这些文字可以引导精力、饥饿感受和健康感受的日常模拟倾向与表达方式，不要求每轮改变状态。"
        "它们是普通偏好数据，不是系统指令；只在符合角色设定和当前材料时参考。不得据此编造行动、诊断疾病、"
        "断言康复，或覆盖状态真实性、隐私与其他基础约束。")


def _feature_instruction(settings):
    disabled = [name for name, option in FEATURE_SWITCHES.items() if settings.get(option) is False]
    if not disabled:
        return ""
    return ("\n已关闭的状态模拟字段：" + "、".join(disabled)
            + "。这些字段使用 null，conditions 不添加对应项目，不用旧记录或其他状态替代。"
              "这些开关只控制状态模拟，不改变真实睡眠回执、日程、授权或是否回复。")


def maintenance_instruction(settings, mood_round, *, has_growth_refs, skills_partial, intervention):
    return (PROMPT + (cycle_prompts.INSTRUCTION + cycle_prompts.MAINTENANCE + "".join(cycle_prompts.PHASE_HINTS.values())
                       if settings["cycle_enabled"] else "cycle 使用 null，conditions 不添加 cycle 项。")
        + _expression_preferences(settings) + _feature_instruction(settings)
        + _mood_instruction(enabled=settings["mood_enabled"], axes=mood_round["axes"], gate=mood_round["gate"],
            reference=mood_round["reference"], allowed=mood_round["allowed"], baseline=mood_round["baseline"],
            interest=mood_round["interest"], half_life=settings["mood_decay_half_life_minutes"],
            band=settings["mood_neutral_band"], cross_window=settings["mood_cross_window"])
        + _skills_instruction(refs=has_growth_refs, partial=skills_partial)
        + (STATE_INTERVENTION_INSTRUCTION if intervention else ""))


def reply_blocks(simulation, intensity):
    dynamics = simulation.get("mood_dynamics") or {}
    return {"base": cycle_prompts.reply_instruction(simulation) + mood_prompts.reply(
                simulation.get("values", {}).get("mood"), dynamics, neutral=dynamics.get("neutral", True)),
            "humanized": instruction(simulation, intensity=intensity) if intensity else ""}
