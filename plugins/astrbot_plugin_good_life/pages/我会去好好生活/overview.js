/* 生活总览：此刻活动、作息、状态网格与今日安排。
 *
 * 只投影 app.js 已经读到的两份数据：`state`（状态与日历，/state）与
 * `life`（活动与作息，/life）。今日安排与状态网格的判定沿用陪伴小屋右栏
 * （pages/陪伴小屋/home-sidebar.js）的规则，两边对同一份数据给出同一种读法：
 *   - 日历期次、生活活动、自动睡眠与日记计划合成当天时间线；已结束的活动在落幕区保留结果；
 *   - 只有日历与活动都完整读到时才计算「未安排时段」，否则不把空白说成空闲；
 *   - 自由文字状态不能可靠归类时保留原文，不推断成正常或异常。
 * 本文件只生成标记，不发请求、不持有状态。 */
"use strict";
window.GoodLifeOverview = (() => {
  const icon = (name, cls) => window.GoodLifeIcons.icon(name, cls);
  const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const text = value => typeof value === "string" ? value.trim() : "";
  const stamp = value => typeof value === "number" ? value
    : typeof value === "string" && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
  const kinds = {sleep:"睡眠休息", reading:"阅读", diary:"整理日记", dream:"梦境", social:"上网浏览", class:"课次", study:"学习", city_trip:"城市出行", campus_trip:"校内出行", purchase:"购买"};
  const kindIcons = {sleep:"sleep", reading:"book", social:"compass", diary:"book", dream:"sleep"};
  const dayMinutes = 24 * 60;

  function zoneOf({state, life}) {
    const zone = state?.settings?.planning_timezone || life?.config?.timezone || "Asia/Shanghai";
    try { new Intl.DateTimeFormat("zh-CN", {timeZone:zone}); return zone; }
    catch { return "Asia/Shanghai"; }
  }
  const format = (value, zone, options) => value == null ? ""
    : new Intl.DateTimeFormat("zh-CN", {timeZone:zone, ...options}).format(new Date(value));
  const clock = (value, zone) => format(value, zone, {hour:"2-digit", minute:"2-digit", hourCycle:"h23"});
  function snapshotTime(input, zone) {
    const updated = stamp(input.snapshotUpdatedAt);
    return updated == null ? "" : `更新于 <time datetime="${new Date(updated).toISOString()}">${e(clock(updated, zone))}</time>`;
  }
  function dayKey(value, zone) {
    const at = stamp(value);
    if (at == null) return "";
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {timeZone:zone, year:"numeric", month:"2-digit", day:"2-digit"})
      .formatToParts(new Date(at)).filter(part => part.type !== "literal").map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function calendarRequest(input, now) {
    const start = dayKey(now, zoneOf(input));
    const end = new Date(Date.parse(start) + 86400000).toISOString().slice(0, 10);
    return {start, end};
  }
  // The API does not expose its current clock. Near role-local midnight an explicit
  // range response can cover the requested date while the server is on a different day.
  const staleCalendar = (input, now, zone) => {
    const {start, end, timezone} = input.state?.occurrences || {};
    const today = dayKey(now, zone), minute = minuteOfDay(now, zone);
    return minute <= 15 || minute >= dayMinutes - 15 || timezone !== zone
      || typeof start !== "string" || typeof end !== "string" || today < start || today >= end;
  };
  function minuteOfDay(value, zone) {
    const at = stamp(value);
    if (at == null) return null;
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {timeZone:zone, hour:"2-digit", minute:"2-digit", hourCycle:"h23"})
      .formatToParts(new Date(at)).filter(part => part.type !== "literal").map(part => [part.type, part.value]));
    return Number(parts.hour) % 24 * 60 + Number(parts.minute);
  }
  function dayMinute(value, now, zone) {
    const at = stamp(value);
    if (at == null) return null;
    const target = dayKey(at, zone), today = dayKey(now, zone);
    return target < today ? 0 : target > today ? dayMinutes : minuteOfDay(at, zone);
  }
  const sameDay = (value, now, zone) => value != null && dayKey(value, zone) === dayKey(now, zone);
  const overlapsToday = (start, end, now, zone) => {
    const today = dayKey(now, zone);
    return start != null && end != null && end > start && dayKey(start, zone) <= today && dayKey(end - 1, zone) >= today;
  };
  const moment = (value, now, zone) =>
    (sameDay(value, now, zone) ? "" : format(value, zone, {month:"long", day:"numeric"}) + " ") + clock(value, zone);
  function windowText(item, now, zone) {
    if (item.start == null) return "时间待定";
    const start = moment(item.start, now, zone);
    if (item.end == null) return start;
    return start + " – " + (dayKey(item.start, zone) === dayKey(item.end, zone) ? clock(item.end, zone) : moment(item.end, now, zone));
  }
  function minuteLabel(value) {
    if (value >= dayMinutes) return "24:00";
    const minutes = Math.max(0, Math.floor(value));
    return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  }
  function durationLabel(value) {
    const minutes = Math.max(0, Math.round(value));
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return hours ? `${hours} 小时${rest ? ` ${rest} 分` : ""}` : `${rest} 分钟`;
  }
  const activities = life => [
    ...(Array.isArray(life?.activities) ? life.activities : []),
    ...(Array.isArray(life?.overview_activities?.items) ? life.overview_activities.items : []),
  ];
  const outcomes = {cancelled:"已取消", skipped:"已跳过", failed:"失败", unknown:"结果待确认"};
  const outcomeSource = item => item.outcome?.source === "simulated" ? "模拟" : "";
  const lifePartialOf = life => !!(life?.next_activity || life?.overview_activities?.partial);
  const withSource = (label, item) => [label, outcomeSource(item)].filter(Boolean).join(" · ");
  function projectedOutcome(item) {
    const label = outcomes[item.outcome?.state];
    if (!label) return null;
    const reason = text(item.outcome.reason_label);
    const code = item.outcome.reason_code;
    const caption = code === "cancelled" || code === "class_absent" ? reason || label
      : !reason || reason === label || code === "result_unknown" ? label : `${label} · ${reason}`;
    return {label:caption, detail:item.outcome.state === "cancelled" ? text(item.checkpoint) : "", summary:"查看记录"};
  }
  function activityOutcome(item) {
    const projected = item.kind !== "reading" ? projectedOutcome(item) : null;
    if (projected) return projected;
    if (item.state === "cancelled") return {label:"已取消", detail:text(item.checkpoint), summary:"查看记录"};
    // 阅读的执行结果与活动生命周期分别投影；失败后，活动本身可能仍是可重试的 paused。
    if (item.kind !== "reading" || item.state === "completed") return null;
    const label = item.task?.state === "failed" ? "失败" : item.task?.state === "unknown" ? "待确认" : "";
    return label ? {label, detail:text(item.task.reason), summary:"查看原因"} : null;
  }
  function outcomeDetails(item, input, section) {
    if (!item.outcome?.detail) return "";
    const key = `${section}:${item.key}`;
    return `<details data-overview-reason="${e(key)}"${input.openReasons?.has(key) ? " open" : ""}>
      <summary><small>${e(item.outcome.summary)}</small></summary><div class="gl-caption">${e(item.outcome.detail)}</div></details>`;
  }
  const sleepOf = ({state, life}) => state?.body_sleep || life?.body_sleep || state?.state?.sleep_execution;
  const planOf = ({state, life}) => life?.sleep_schedule || state?.sleep_schedule;

  /* === 状态网格 === */
  function vital(kind, raw, {sleeping=false, disabled=false, loading=false, unreadable=false}={}) {
    const item = (visual, glyph, label, tone="quiet") => ({visual, glyph, label, tone});
    if (disabled) return item("disabled", kind, "未启用");
    if (kind === "energy" && typeof raw === "number") {
      if (raw >= 75) return item("energy-full", "energy-full", "充足", "ok");
      if (raw >= 40) return item("energy-medium", "energy", "尚可", "ok");
      if (raw >= 20) return item("energy-low", "energy-low", "偏低", "warn");
      return item("energy-depleted", "energy-low", "较低", "risk");
    }
    if (kind === "sleep" && sleeping) return item("sleeping", "sleep", "睡眠中", "rest");
    if (!raw) return item("unknown", kind, loading ? "读取中" : unreadable ? "待读取" : "未记录");
    if (/^(未知|不清楚|未记录|暂无|尚未记录|未设置|待确认)$/.test(raw)) return item("unknown", kind, "未知");
    if (kind === "health") {
      if (/^(稳定|正常|健康|良好|状态良好|无不适|没有不适|身体无不适)$/.test(raw)) return item("well", "health", "良好", "ok");
      if (/恢复中|康复中|正在恢复|逐渐恢复|正在好转|有所好转/.test(raw)) return item("recovering", "health", "恢复中", "ok");
      const symptoms = raw.replace(/(?:没有|暂无|并无|未见|没有出现|未出现|不再|无|没|不)(?:明显|任何)?(?:不适|疼痛|头痛|发热|发烧|感冒|虚弱|生病)/g, "");
      if (/不舒服|不适|生病|发烧|发热|疼痛|头痛|感冒|虚弱|不稳定|欠佳/.test(symptoms)) return item("strained", "health", "不适", "warn");
      if (/稳定|正常|健康|良好|(?:没有|暂无|无)(?:明显|任何)?不适/.test(raw)) return item("well", "health", "良好", "ok");
    } else if (kind === "hunger") {
      if (/吃撑|太饱|过饱|饱胀|有点撑/.test(raw)) return item("full", "hunger", "偏饱", "warn");
      if (/^(不饿|没有饥饿感|已吃饱|吃饱了|饱腹|饱足|已饱|适中)$/.test(raw)) return item("sated", "hunger", "不饿", "ok");
      if (/没吃饱|未吃饱|有点饿|饥饿|很饿|饿了|^饿$|饥肠辘辘/.test(raw)) return item("hungry", "hunger", "饿了", "warn");
    } else if (kind === "sleep") {
      if (/^(清醒|精神|精神饱满|不困|没有困意|已醒|醒着)$/.test(raw)) return item("awake", "sun", "清醒", "ok");
      if (/难以入睡|无法入睡|失眠|睡不着|辗转|睡眠不足/.test(raw)) return item("restless", "sleep", "睡不好", "warn");
      if (/困倦|犯困|想睡|有些困|很困|疲惫|^困$/.test(raw)) return item("sleepy", "sleep", "困倦", "warn");
      if (/休息|入睡|睡着|睡眠中/.test(raw)) return item("rest", "sleep", "休息感受", "rest");
    }
    return item("recorded", kind, raw);
  }
  function stateGrid(input) {
    const {state, life, loading=false} = input;
    const values = state?.state?.values || {}, settings = state?.settings || {};
    const sleeping = sleepOf(input)?.state === "sleeping" || activities(life).some(item => item.kind === "sleep" && item.state === "active");
    const rhythmLabels = {awake:"清醒", sleepy:"逐渐困倦", winding_down:"准备休息", recovering:"醒来恢复"};
    const energy = typeof values.energy === "number" && Number.isFinite(values.energy) && values.energy >= 0 && values.energy <= 100 ? values.energy : null;
    const unreadable = !state || state.state?.state === "unavailable";
    const cycleNames = {menstrual:"经期", follicular:"卵泡期", pre_ovulation:"排卵前", ovulation:"排卵期", luteal:"黄体期", pms:"经前期", recovery:"恢复期"};
    const timeline = state?.state?.cycle_timeline;
    const cycle = timeline?.applicability === "not_applicable" ? "不适用" : timeline?.applicability === "applicable"
      ? `${cycleNames[timeline.phase] || "周期中"}${Number.isInteger(timeline.phase_day) ? ` · 第 ${timeline.phase_day} 天` : ""}` : text(values.cycle);
    const mood = text(typeof values.mood === "string" ? values.mood : values.mood?.description);
    const rows = [["energy","能量",energy], ["health","健康",text(values.health)], ["hunger","饥饿",text(values.hunger)],
      ["mood","心情",mood], ["sleep","睡眠",sleeping ? "睡眠中" : text(values.sleep) || rhythmLabels[planOf(input)?.rhythm?.phase]],
      ["cycle","生理期",cycle], ["busy","忙碌",text(values.busy)], ["location","位置",text(values.location)]];
    const maintained = settings.enabled !== false;
    const cells = rows.map(([kind, label, raw]) => {
      const disabled = !maintained || settings[kind + "_enabled"] === false;
      const item = vital(kind, raw, {sleeping, disabled, loading, unreadable});
      const detail = disabled ? "该项维护未启用" : kind === "energy" && typeof raw === "number" ? `${raw} / 100` : raw || item.label;
      return `<li class="gl-vital" data-kind="${kind}" data-visual="${item.visual}" data-tone="${item.tone}"
        ${kind === "energy" && energy != null ? `style="--gl-level:${energy}%"` : ""} title="${e(label + " · " + detail)}">
        <span class="gl-vital-glyph">${icon(item.glyph)}</span><span class="gl-vital-label">${label}</span>
        <strong class="gl-vital-value">${e(item.label)}</strong></li>`;
    }).join("");
    const updated = stamp(state?.state?.updated_at), now = input.now ?? Date.now(), zone = zoneOf(input);
    const caption = loading ? "正在读取角色状态…" : unreadable ? "暂时无法读取角色状态，可稍后刷新。"
      : [maintained ? "" : "状态自动维护未启用", updated != null ? `更新于 ${moment(updated, now, zone)}` : ""].filter(Boolean).join(" · ");
    return `<ul class="gl-vitals" aria-label="此刻的状态">${cells}</ul>${caption ? `<p class="gl-caption">${e(caption)}</p>` : ""}`;
  }

  /* === 今日安排 === */
  function agenda(input) {
    const {state, life} = input, now = input.now ?? Date.now(), zone = zoneOf(input), items = [], seen = new Set();
    const add = item => { if (!seen.has(item.key)) { seen.add(item.key); items.push(item); } };
    for (const item of state?.occurrences?.items || []) {
      const start = stamp(item.start), end = stamp(item.end);
      if (!overlapsToday(start, end, now, zone)) continue;
      const outcome = projectedOutcome(item);
      if (item.state === "cancelled" || item.state === "skipped") {
        if (outcome) add({key:`calendar:${item.id}:${item.sequence}`,
          title:text(item.title) || "未命名安排", start, end, outcomeAt:stamp(item.outcome_at),
          label:outcome.label, outcome, tone:"elapsed", order:1});
        continue;
      }
      const kind = item.kind === "user_report" ? "你的安排" : item.kind === "commitment" ? "约定" : "角色计划";
      const phase = end <= now ? item.state === "proposed" ? "待确认 · 时段已过" : "时段已过"
        : item.state === "proposed" ? "待确认" : start <= now ? "当前计划时段" : "计划安排";
      const tone = end <= now ? "elapsed" : item.state === "proposed" ? "proposed" : start <= now ? "current" : "planned";
      add({key:`calendar:${item.id}:${item.sequence}`, title:text(item.title) || "未命名安排", start, end,
        detail:text(item.detail), label:`${kind} · ${phase}`, tone, order:1});
    }
    for (const item of activities(life)) {
      const started = stamp(item.started_at), updated = stamp(item.updated_at), due = stamp(item.due_at), ends = stamp(item.ends_at);
      const key = `activity:${item.activity_id}`, title = text(item.title) || kinds[item.kind] || "个人活动";
      const outcome = activityOutcome(item);
      const active = item.state === "active", completed = item.state === "completed", paused = item.state === "paused";
      if (outcome && !active) {
        if (sameDay(updated ?? started, now, zone) || overlapsToday(started, ends, now, zone)) {
          add({key, title, start:started ?? updated, end:ends ?? updated, outcomeAt:updated,
            label:withSource(outcome.label, item), outcome, tone:"elapsed", order:2});
        }
        continue;
      }
      const planned = item.state === "planned";
      if (planned && (item.kind === "reading" && due == null
        || !sameDay(due ?? started, now, zone) && !overlapsToday(due ?? started, ends, now, zone))) continue;
      if ((paused || completed) && !sameDay(updated, now, zone)) continue;
      if (!active && !completed && !paused && !planned) continue;
      const late = planned && due != null && due <= now;
      add({key, title, start:planned ? due ?? started : started ?? updated,
        end:ends ?? (completed || paused ? updated : active && due != null && due > now ? due : null),
        detail:text(item.checkpoint) || text(item.reflection), outcome,
        label:withSource(active ? ["进行中", outcome?.label].filter(Boolean).join(" · ")
          : completed ? "已完成" : paused ? "已暂停" : late ? "计划时间已过 · 状态待更新" : "待开始", item),
        tone:active ? "active" : completed ? "completed" : paused ? "paused" : late ? "elapsed" : "planned", order:active ? 0 : 2});
    }
    const sleep = sleepOf(input), plan = planOf(input);
    if (sleep?.state === "sleeping" && !activities(life).some(item => item.kind === "sleep" && item.state === "active")) {
      add({key:"body:sleep", title:"睡眠休息", start:stamp(sleep.started_at), end:stamp(sleep.planned_wake_at),
        label:"睡眠中", tone:"active", order:0});
    }
    const nextSleep = stamp(plan?.next_sleep_at);
    if (plan?.enabled && nextSleep != null && nextSleep >= now && sameDay(nextSleep, now, zone)) {
      add({key:"routine:sleep", title:"睡眠休息", start:nextSleep, end:stamp(plan.next_wake_at),
        label:plan.executor_state === "available" ? "自动睡眠计划" : "睡眠计划 · 等待授权窗口", tone:"planned", order:2});
    }
    const nextDiary = stamp(life?.next_diary_at);
    if (life?.config?.diary_enabled && nextDiary != null && nextDiary >= now && sameDay(nextDiary, now, zone)) {
      add({key:"routine:diary", title:"整理今日日记", start:nextDiary, end:null, label:"自动日记计划", tone:"planned", order:2});
    }
    return items.sort((a, b) => (a.start ?? Infinity) - (b.start ?? Infinity) || a.order - b.order || a.key.localeCompare(b.key));
  }
  /* 空档只在数据完整时计算：从此刻起，相邻两项之间、末项到 24:00 之间。 */
  function timelineRows(items, now, zone, complete) {
    let cursor = minuteOfDay(now, zone), known = true;
    const rows = [];
    for (const item of items) {
      const start = dayMinute(item.start, now, zone);
      if (complete && known && start != null && start > cursor) rows.push({type:"gap", start:cursor, end:start, current:rows.length === 0, next:item});
      rows.push({type:"event", item});
      const end = dayMinute(item.end, now, zone);
      if (end != null) cursor = Math.max(cursor, end);
      else if (item.order === 0 && (item.start == null || item.start <= now)) known = false;
      else if (start != null) cursor = Math.max(cursor, start);
    }
    if (complete && known && cursor < dayMinutes) rows.push({type:"gap", start:cursor, end:dayMinutes, current:rows.length === 0});
    return rows;
  }
  function schedule(input) {
    const {state, life, loading=false} = input, now = input.now ?? Date.now(), zone = zoneOf(input);
    const items = agenda(input), notes = [];
    const calendarRead = Array.isArray(state?.occurrences?.items), lifeRead = Array.isArray(life?.activities);
    const lifePartial = lifePartialOf(life), calendarStale = staleCalendar(input, now, zone);
    const coverage = state?.occurrences || {}, planning = state?.planning, result = planning?.daily_result;
    const planningIncomplete = planning?.enabled && planning.calendar_enabled
      && (planning.running || ["failed","expired","dead_letter","cancelled","uncertain","blocked","deferred"].includes(result?.state));
    if (!loading) {
      if (!calendarRead) notes.push("日历暂未读取");
      else if (calendarStale) notes.push("日历快照尚未覆盖今天");
      if (!lifeRead) notes.push("生活活动暂未读取");
      else if (lifePartial) notes.push("部分生活活动未读取");
      if (coverage.skipped) notes.push(`${coverage.skipped} 条日历记录来源不可用，已跳过`);
      if (coverage.scan_truncated || coverage.occurrences_truncated) notes.push("日历记录超过读取上限，部分未显示");
      else if (coverage.partial && !coverage.skipped) notes.push("部分日历安排未完整读取");
      if (planningIncomplete) notes.push(planning.running ? "今日规划正在生成" : "今日规划尚未生成");
    }
    const complete = calendarRead && !calendarStale && lifeRead && !lifePartial && !coverage.partial && !coverage.skipped
      && !coverage.scan_truncated && !coverage.occurrences_truncated && !planningIncomplete;
    const elapsed = items.filter(item => item.tone === "elapsed" || item.tone === "completed" || item.end != null && item.end <= now && item.order !== 0)
      .sort((a, b) => (b.end ?? b.start ?? -Infinity) - (a.end ?? a.start ?? -Infinity));
    const live = items.filter(item => !elapsed.includes(item));
    const rows = timelineRows(live, now, zone, complete)
      .filter(row => row.type !== "gap" || row.current || row.end - row.start >= 45);
    let number = 0;
    const event = (item, numbered = true) => {
      const current = item.order === 0 || item.tone === "current";
      return `<li class="gl-agenda-item" data-tone="${item.tone}"${current ? ' aria-current="time"' : ""}>
        <span class="gl-agenda-no" aria-hidden="true">${numbered ? String(++number).padStart(2, "0") : "·"}</span>
        <div class="gl-agenda-body"><time${item.start == null ? "" : ` datetime="${new Date(item.start).toISOString()}"`}>${e(windowText(item, now, zone))}</time>
        <strong>${e(item.title)}</strong><small>${e(item.label)}</small>${item.detail ? `<small class="gl-agenda-detail">${e(item.detail)}</small>` : ""}
        ${outcomeDetails(item, input, "agenda")}</div></li>`;
    };
    const gap = row => `<li class="gl-agenda-gap"${row.current ? ' data-current="true" aria-current="time"' : ""}>
      <span class="gl-agenda-no" aria-hidden="true"></span><div class="gl-agenda-body">
      <time>${minuteLabel(row.start)} – ${minuteLabel(row.end)} · ${e(durationLabel(row.end - row.start))}</time>
      <strong>${row.current ? live.length ? "此刻未安排" : "接下来没有安排" : "未安排时段"}</strong>
      <small>${row.next ? `下一项 ${e(clock(row.next.start, zone))} ${e(row.next.title)}` : "当天暂无后续安排"}</small></div></li>`;
    const emptyTitle = loading ? "正在读取日程…" : !calendarRead || !lifeRead ? "日程暂不可读取"
      : lifePartial || calendarStale ? "今日安排尚未完整读取" : elapsed.length ? "今日安排已全部过去" : "今日暂无安排";
    const list = rows.length ? rows.map(row => row.type === "event" ? event(row.item) : gap(row)).join("")
      : `<li class="gl-agenda-empty">${e(emptyTitle)}</li>`;
    const past = elapsed.length ? `<details class="gl-agenda-past"${input.pastOpen ? " open" : ""}><summary>已落幕 ${elapsed.length} 项</summary>
      <ol class="gl-agenda">${elapsed.map(item => event(item, false)).join("")}</ol></details>` : "";
    const updated = input.showSnapshotTime ? snapshotTime(input, zone) : "";
    const meta = `<span>今日 ${items.length} 项${elapsed.length ? ` · 已落幕 ${elapsed.length}` : ""}</span><span>按 ${e(zone)} 显示${updated ? ` · ${updated}` : ""}</span>`;
    return `<div class="gl-agenda-meta">${meta}</div>
      ${!complete && !loading && calendarRead && lifeRead ? '<p class="gl-caption gl-warn">日程数据不完整，空白时段暂不判断为空闲。</p>' : ""}
      <ol class="gl-agenda" aria-label="今日安排时间线">${list}</ol>${past}
      ${notes.length ? `<p class="gl-caption gl-warn" role="status">${e(notes.join("；"))}</p>` : ""}`;
  }

  /* === 此刻活动 === */
  function now(input) {
    const {life, loading=false} = input, at = input.now ?? Date.now(), zone = zoneOf(input);
    const running = activities(life).filter(item => item.state === "active" || item.state === "unknown" && !item.ends_at || item.state === "paused"
      && (!activityOutcome(item) || item.task?.state === "unknown"));
    const sleep = sleepOf(input);
    const cards = running.map(item => {
      const started = stamp(item.started_at), due = stamp(item.due_at), outcome = activityOutcome(item);
      const meta = [kinds[item.kind] || "生活活动", item.state === "paused" ? "已暂停" : item.state === "active" ? "进行中" : "",
        outcome?.label, outcomeSource(item),
        started != null ? `${moment(started, at, zone)} 开始` : "", due != null && due > at ? `预计 ${moment(due, at, zone)} 结束` : ""].filter(Boolean);
      const note = text(item.checkpoint) || text(item.description);
      return `<div class="gl-now-item" data-state="${e(item.state)}"><span class="gl-now-glyph">${icon(item.state === "paused" ? "pause" : kindIcons[item.kind] || "activity")}</span>
        <div><strong>${e(text(item.title) || text(item.description) || kinds[item.kind] || "生活活动")}</strong>
        <small>${e(meta.join(" · "))}</small>${note && note !== text(item.title) ? `<p>${e(note)}</p>` : ""}
        ${outcomeDetails({key:`activity:${item.activity_id}`, outcome}, input, "now")}</div></div>`;
    });
    if (sleep?.state === "sleeping" && !running.some(item => item.kind === "sleep")) {
      cards.push(`<div class="gl-now-item" data-state="active"><span class="gl-now-glyph">${icon("sleep")}</span><div><strong>睡眠休息</strong>
        <small>${e(["睡眠中", stamp(sleep.started_at) != null ? `${moment(stamp(sleep.started_at), at, zone)} 入睡` : "",
          stamp(sleep.planned_wake_at) != null ? `计划 ${moment(stamp(sleep.planned_wake_at), at, zone)} 醒来` : ""].filter(Boolean).join(" · "))}</small></div></div>`);
    }
    if (!cards.length && loading) return '<p class="gl-empty">正在读取…</p>';
    if (!cards.length && !Array.isArray(life?.activities)) return '<p class="gl-empty">生活活动暂未读取。</p>';
    const items = agenda(input);
    const currentKeys = new Set(running.map(item => `activity:${item.activity_id}`));
    const latest = items.filter(item => item.outcome && item.tone === "elapsed" && !currentKeys.has(item.key)
      && item.outcomeAt != null && item.outcomeAt <= at)
      .sort((a, b) => b.outcomeAt - a.outcomeAt)[0];
    if (!cards.length) {
      const next = items.find(item => item.start != null && item.start > at && item.tone !== "elapsed");
      const lifePartial = lifePartialOf(life), schedulePartial = lifePartial || staleCalendar(input, at, zone);
      cards.push(`<div class="gl-now-item" data-state="idle"><span class="gl-now-glyph">${icon("clock")}</span><div>
        <strong>${lifePartial ? "当前活动尚未完整读取" : "此刻没有进行中的活动"}</strong>
        <small>${next ? `${schedulePartial ? "已读取的下一项" : "下一项"} · ${e(moment(next.start, at, zone))} ${e(next.title)}`
          : schedulePartial ? "今天后续的安排暂不确定" : "今天接下来暂无安排"}</small></div></div>`);
    }
    if (latest) cards.push(`<div class="gl-agenda-body"><small>最近活动 · ${e(latest.title)} · ${e(latest.label)}</small>
      ${outcomeDetails(latest, input, "now")}</div>`);
    const updated = snapshotTime(input, zone);
    if (updated) cards.push(`<p class="gl-caption">${updated}</p>`);
    return cards.join("");
  }

  /* === 作息与睡眠 === */
  function rest(input) {
    const at = input.now ?? Date.now(), zone = zoneOf(input), plan = planOf(input), body = sleepOf(input);
    if (!plan && !body) return `<p class="gl-empty">${input.loading ? "正在读取…" : "作息信息暂未读取。"}</p>`;
    const phases = {sleeping:"睡眠中", interrupted:"睡眠被打断", recovering:"刚醒来，正在恢复", winding_down:"准备休息", sleepy:"逐渐困倦", awake:"清醒"};
    const phase = body?.state === "sleeping" ? "sleeping" : body?.state === "interrupted" ? "interrupted" : plan?.rhythm?.phase || "awake";
    const until = stamp(plan?.rhythm?.until) ?? stamp(plan?.active_wake_at);
    const facts = [];
    const fact = (label, value) => { if (value) facts.push(`<div><dt>${label}</dt><dd>${e(value)}</dd></div>`); };
    const nextSleep = stamp(plan?.next_sleep_at), nextWake = stamp(plan?.next_wake_at);
    fact("自动入睡", !plan ? "" : !plan.enabled ? "未启用" : plan.executor_state === "available" ? `每天约 ${plan.time || "—"}` : "等待授权私聊窗口");
    fact("下次入睡", nextSleep != null ? moment(nextSleep, at, zone) : plan?.enabled ? "未安排" : "");
    fact("计划醒来", nextWake != null ? moment(nextWake, at, zone) : "");
    const recommended = Number(plan?.recommended_duration_minutes);
    fact("推荐睡眠", Number.isFinite(recommended) && recommended > 0 ? durationLabel(recommended) : "");
    const profile = plan?.physiology || {};
    fact("睡眠压力", profile.sleep_pressure == null ? "" : `${Math.round(profile.sleep_pressure)} / 100`);
    fact("欠睡", profile.has_sleep_history ? durationLabel(profile.sleep_debt_minutes || 0) : "");
    const tone = ["sleeping", "interrupted", "winding_down", "sleepy"].includes(phase) ? "rest" : "ok";
    return `<div class="gl-rest-head" data-tone="${tone}"><span class="gl-now-glyph">${icon(tone === "rest" ? "sleep" : "sun")}</span>
      <div><strong>${e(phases[phase] || "清醒")}</strong>${until != null && until > at ? `<small>持续到 ${e(moment(until, at, zone))}</small>` : ""}</div></div>
      ${facts.length ? `<dl class="gl-facts">${facts.join("")}</dl>` : ""}`;
  }

  return {zoneOf, calendarRequest, now, rest, stateGrid, schedule, agenda, timelineRows};
})();
