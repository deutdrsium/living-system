/* 日程页：月历 + 所选日期时间线。
 *
 * 只投影 app.js 已读到的数据：
 *   - 月历与非今天的日程来自宿主的日期范围接口（calendar/occurrences，按月读取），
 *     实例由后端按重复规则与改期 / 跳过例外生成，不再受日程记录分批读取的限制；
 *     input.months(key) 给出某月（"YYYY-MM"）的读取结果 {status, data, code}；
 *   - 今天的时间线直接用总览的 GoodLifeOverview.schedule()，和总览同一种读法；
 *   - 非今天的日期另外显示该日的生活活动历史（life/activities，按日读取），
 *     input.history 为所选日期的读取结果 {status, items, next_cursor, more, code}；
 *   - 日期范围接口读取失败时，才退回按已读取那一批日程记录的重复规则推算，
 *     并在界面上明确标注「推算」与「可能不完整」。
 * 后端返回的 partial / skipped / *_truncated 都会如实提示，不把不完整的月历当作完整。
 * 本文件只生成标记，不发请求、不持有状态。 */
"use strict";
window.GoodLifeCalendarView = (() => {
  const icon = name => window.GoodLifeIcons.icon(name);
  const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const text = value => typeof value === "string" ? value.trim() : "";
  const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
  const kinds = {role_plan:"角色计划", user_report:"你的安排", commitment:"约定"};
  const activityKinds = {reading:"阅读", sleep:"睡眠", social:"浏览"};
  const activityIcons = {reading:"book", sleep:"sleep", social:"compass"};
  const activityStates = {planned:"待开始", active:"进行中", paused:"已暂停", completed:"已完成", cancelled:"已取消"};
  const pad = value => String(value).padStart(2, "0");
  const keyOf = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;
  const parseKey = key => { const [y, m, d] = key.split("-").map(Number); return {y, m:m - 1, d}; };
  const utcDay = key => { const {y, m, d} = parseKey(key); return Date.UTC(y, m, d); };
  const addDays = (key, days) => { const at = new Date(utcDay(key) + days * 86400000); return keyOf(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate()); };
  const daysIn = (y, m) => new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  const monthKey = key => key.slice(0, 7);
  const count = value => Number.isSafeInteger(value) && value > 0 ? value : Array.isArray(value) ? value.length : 0;

  function validZone(zone) {
    if (typeof zone !== "string" || !zone) return null;
    try { new Intl.DateTimeFormat("en-US", {timeZone:zone}); return zone; } catch { return null; }
  }
  /* ISO 时刻在某时区的当地日期与钟点。 */
  function wall(value, zone) {
    const at = typeof value === "string" ? Date.parse(value) : typeof value === "number" ? value : NaN;
    if (!Number.isFinite(at)) return null;
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone:zone, year:"numeric", month:"2-digit", day:"2-digit",
      hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).formatToParts(new Date(at)).filter(p => p.type !== "literal").map(p => [p.type, p.value]));
    return {key:`${parts.year}-${parts.month}-${parts.day}`, time:`${parts.hour === "24" ? "00" : parts.hour}:${parts.minute}`};
  }
  function todayKey(now, zone) { return wall(now, zone).key; }
  /* 某月读取结果里可用的时区：优先用接口回传的规划时区。 */
  const zoneFor = (entry, fallback) => validZone(entry?.data?.timezone) || fallback;

  /* === 接口实例（calendar/occurrences） === */
  /* 把一条实例展开到它覆盖的当地日期上（跨午夜的实例在两天都显示）。缺开始时间的实例无法定位，跳过。 */
  function occurrenceDays(item, zone) {
    const start = wall(item?.start, zone);
    if (!start) return [];
    const endAt = typeof item.end === "string" ? Date.parse(item.end) : NaN;
    const startAt = Date.parse(item.start);
    const last = Number.isFinite(endAt) && endAt > startAt ? wall(endAt - 1, zone)?.key || start.key : start.key;
    const keys = [start.key];
    for (let key = start.key, guard = 0; key < last && guard < 31; guard++) { key = addDays(key, 1); keys.push(key); }
    return keys;
  }
  function occurrenceEntries(entry, zone) {
    const items = Array.isArray(entry?.data?.items) ? entry.data.items : [];
    const out = [];
    for (const item of items) {
      if (!item || typeof item !== "object" || item.state === "cancelled") continue;
      const start = wall(item.start, zone);
      if (!start) continue;
      const end = Date.parse(item.end) > Date.parse(item.start) ? wall(item.end, zone) : null;
      for (const key of occurrenceDays(item, zone)) {
        /* 结束在后一天时：首日写「次日 HH:MM」，续接的那天从 00:00 起。 */
        const endText = !end ? "" : end.key === key ? end.time : key === start.key && end.key === addDays(key, 1) ? "次日 " + end.time : "";
        out.push({key, item, source:"api", sequence:item.sequence, zone, end:endText,
          time:key === start.key ? start.time : "00:00", continued:key !== start.key});
      }
    }
    return out;
  }

  /* === 后备：按已读取那一批日程记录的重复规则推算 === */
  /* 某条日程在第 sequence 次的当地日期（与后端 Schedule.at 的步进一致）。 */
  function sequenceKey(anchorKey, frequency, interval, sequence) {
    const delta = sequence * interval;
    if (frequency === "daily") return addDays(anchorKey, delta);
    if (frequency === "weekly") return addDays(anchorKey, delta * 7);
    const {y, m, d} = parseKey(anchorKey), months = delta * (frequency === "yearly" ? 12 : 1);
    const total = y * 12 + m + months, year = Math.floor(total / 12), month = total % 12;
    return keyOf(year, month, Math.min(d, daysIn(year, month)));
  }
  function durationEnd(time, minutes) {
    if (!minutes) return "";
    const [h, m] = time.split(":").map(Number), total = h * 60 + m + minutes;
    return total >= 1440 ? "次日 " + pad(Math.floor(total % 1440 / 60)) + ":" + pad(total % 60) : pad(Math.floor(total / 60)) + ":" + pad(total % 60);
  }
  /* 日期区间 [from, to]（含）内的期次。 */
  function project(item, from, to, fallbackZone) {
    const schedule = item?.schedule;
    if (!schedule || item.state === "cancelled") return [];
    const zone = validZone(schedule.timezone_name) || fallbackZone, anchor = wall(schedule.anchor, zone);
    if (!anchor) return [];
    const frequency = ["once", "daily", "weekly", "monthly", "yearly"].includes(schedule.frequency) ? schedule.frequency : "once";
    const interval = Number.isSafeInteger(schedule.interval) && schedule.interval >= 1 ? schedule.interval : 1;
    const limit = Number.isSafeInteger(schedule.count) && schedule.count >= 1 ? schedule.count : frequency === "once" ? 1 : Infinity;
    const exceptions = item.exceptions && typeof item.exceptions === "object" ? item.exceptions : {};
    const duration = Number.isInteger(item.duration_minutes) && item.duration_minutes > 0 ? item.duration_minutes : null;
    const out = [];
    const push = (sequence, key, time, moved) => { if (key >= from && key <= to) out.push({item, source:"projected", sequence, key, time,
      end:durationEnd(time, duration), zone, moved, detail:text(item.details?.[String(sequence)]) || text(item.detail)}); };
    let start = 0;
    if (frequency === "daily" || frequency === "weekly") {
      const step = interval * (frequency === "weekly" ? 7 : 1);
      start = Math.max(0, Math.floor((utcDay(from) - utcDay(anchor.key)) / 86400000 / step) - 1);
    } else if (frequency !== "once") {
      const a = parseKey(anchor.key), f = parseKey(from), step = interval * (frequency === "yearly" ? 12 : 1);
      start = Math.max(0, Math.floor(((f.y - a.y) * 12 + f.m - a.m) / step) - 1);
    }
    for (let sequence = start, guard = 0; sequence < limit && guard < 80; sequence++, guard++) {
      const key = frequency === "once" ? anchor.key : sequenceKey(anchor.key, frequency, interval, sequence);
      if (key > to) break;
      if (Object.hasOwn(exceptions, String(sequence))) continue;
      push(sequence, key, anchor.time, false);
      if (frequency === "once") break;
    }
    /* 改期的期次可能落在区间外的序号上，单独看一遍。 */
    for (const [sequence, moved] of Object.entries(exceptions)) {
      const at = moved ? wall(moved, zone) : null;
      if (at && Number(sequence) < limit) push(Number(sequence), at.key, at.time, true);
    }
    return out;
  }
  const projectAll = (state, from, to, zone) => (state?.calendar?.items || []).flatMap(item => project(item, from, to, zone));
  const order = (a, b) => a.key.localeCompare(b.key) || a.time.localeCompare(b.time) || String(a.item.title ?? "").localeCompare(String(b.item.title ?? ""));

  /* 区间 [from, to] 的条目：每个月优先用接口结果；读取失败的月份用推算后备。
     返回 {entries, fallback, loading, failed}，后两者为月份键数组。 */
  function collect(input, from, to, zone) {
    const entries = [], fallback = [], loading = [], failed = [];
    const months = [];
    for (let key = from.slice(0, 7) + "-01"; monthKey(key) <= monthKey(to); ) {
      months.push(monthKey(key)); const {y, m} = parseKey(key); key = keyOf(m === 11 ? y + 1 : y, (m + 1) % 12, 1);
    }
    for (const month of months) {
      const entry = input.months?.(month), first = month + "-01", last = keyOf(...(({y, m}) => [y, m, daysIn(y, m)])(parseKey(first)));
      const lo = from > first ? from : first, hi = to < last ? to : last;
      if (entry?.status === "ok") entries.push(...occurrenceEntries(entry, zoneFor(entry, zone)).filter(item => item.key >= lo && item.key <= hi));
      else if (entry?.status === "error") {
        failed.push(month);
        if (input.state) { fallback.push(month); entries.push(...projectAll(input.state, lo, hi, zone)); }
      } else loading.push(month);
    }
    return {entries:entries.sort(order), fallback, loading, failed};
  }
  /* 后端回报的不完整情况，转成一句提示；字段缺失时什么也不说。 */
  function coverageNotes(entry) {
    const data = entry?.status === "ok" ? entry.data || {} : null, notes = [];
    if (!data) return notes;
    const skipped = count(data.skipped) || count(data.skipped_reasons) ||
      (data.skipped_reasons && typeof data.skipped_reasons === "object" ? Object.values(data.skipped_reasons).reduce((sum, value) => sum + count(value), 0) : 0);
    if (skipped) notes.push(`${skipped} 条日程的来源当前不可读取，未计入。`);
    if (data.scan_truncated || data.occurrences_truncated) notes.push("本月安排超过单次读取上限，部分未显示。");
    else if (data.partial && !skipped) notes.push("部分安排未完整读取。");
    return notes;
  }
  const incomplete = entry => entry?.status === "ok" && !!(entry.data?.partial || entry.data?.scan_truncated || entry.data?.occurrences_truncated
    || count(entry.data?.skipped) || count(entry.data?.skipped_reasons));

  /* === 月历 === */
  function month(input) {
    const {view} = input, zone = window.GoodLifeOverview.zoneOf(input), today = todayKey(input.now ?? Date.now(), zone);
    const year = view.year, mon = view.month, first = keyOf(year, mon, 1), last = keyOf(year, mon, daysIn(year, mon));
    const lead = (new Date(utcDay(first)).getUTCDay() + 6) % 7, gridStart = addDays(first, -lead);
    const cells = Math.ceil((lead + daysIn(year, mon)) / 7) * 7, gridEnd = addDays(gridStart, cells - 1);
    /* 网格两端的邻月日期只在那个月已读到时打点，不为它们单独发请求。 */
    const own = input.months?.(monthKey(first));
    const {entries} = collect({...input, months:key => key === monthKey(first) ? own : input.months?.(key)?.status === "ok" ? input.months(key) : undefined},
      gridStart, gridEnd, zone);
    const byDay = new Map();
    for (const entry of entries) byDay.set(entry.key, [...(byDay.get(entry.key) || []), entry]);
    const days = Array.from({length:cells}, (_, index) => {
      const key = addDays(gridStart, index), {d, m} = parseKey(key), list = byDay.get(key) || [];
      const known = m === mon ? own?.status === "ok" || own?.status === "error" && !!input.state : input.months?.(monthKey(key))?.status === "ok";
      const label = `${m + 1}月${d}日${key === today ? "，今天" : ""}${list.length ? `，${list.length} 项安排` : known ? "，无安排" : ""}`;
      return `<button type="button" class="gl-cal-day" data-cal-day="${key}"${m !== mon ? ' data-outside="true"' : ""}${key === today ? ' data-today="true"' : ""}
        aria-pressed="${key === view.selected}" aria-label="${e(label)}"><span class="gl-cal-num">${d}</span>
        ${list.length ? `<span class="gl-cal-marks" aria-hidden="true">${list.slice(0, 3).map(entry => `<i data-kind="${e(entry.item.kind)}"></i>`).join("")}${list.length > 3 ? `<b>+${list.length - 3}</b>` : ""}</span>` : ""}</button>`;
    }).join("");
    const inMonth = entries.filter(entry => entry.key >= first && entry.key <= last && !entry.continued).length;
    const notes = [];
    let sum = "";
    if (!own || own.status === "loading") { notes.push("正在读取本月日程…"); sum = "读取中"; }
    else if (own.status === "ok") { notes.push(...coverageNotes(own)); sum = `本月${incomplete(own) ? "至少" : ""} ${inMonth} 次`; }
    else {
      notes.push(input.state ? "本月日程读取失败，月历暂按已读取那一批日程的重复规则推算，可能不完整。" : "本月日程读取失败。");
      sum = input.state ? `推算 ${inMonth} 次` : "读取失败";
    }
    return `<header class="gl-cal-head"><h2 class="gl-cal-title">${year} 年 ${mon + 1} 月</h2>
        <div class="gl-cal-nav" role="group" aria-label="切换月份">
          <button type="button" class="gl-icon-button" data-cal-month="-1" aria-label="上个月">${icon("chevron-left")}</button>
          <button type="button" class="gl-chip-button" data-cal-today>今天</button>
          <button type="button" class="gl-icon-button" data-cal-month="1" aria-label="下个月">${icon("chevron")}</button></div></header>
      <div class="gl-cal-grid" role="group" aria-label="${year} 年 ${mon + 1} 月"${!own || own.status === "loading" ? ' aria-busy="true"' : ""}>
        ${weekdays.map(day => `<span class="gl-cal-weekday" aria-hidden="true">${day}</span>`).join("")}${days}</div>
      <p class="gl-cal-legend"><span><i data-kind="role_plan"></i>角色计划</span><span><i data-kind="user_report"></i>你的安排</span><span><i data-kind="commitment"></i>约定</span>
        <span class="gl-cal-sum">${e(sum)}</span></p>
      ${notes.length ? `<p class="gl-caption${own?.status === "error" || incomplete(own) ? " gl-warn" : ""}" role="status">${e(notes.join(" "))}${own?.status === "error"
        ? ` <button type="button" class="gl-text-button" data-cal-retry="month">重新读取</button>` : ""}</p>` : ""}`;
  }

  /* === 所选日期：日程 === */
  function calendarList(input, key, today, zone) {
    const entry = input.months?.(monthKey(key)), {entries, fallback} = collect(input, key, key, zone);
    if (!entry || entry.status === "loading") return {meta:"读取中", markup:`<p class="gl-empty">正在读取这天的日程…</p>`};
    if (entry.status === "error" && !input.state)
      return {meta:"读取失败", markup:`<p class="gl-caption gl-warn" role="status">这天的日程读取失败。 <button type="button" class="gl-text-button" data-cal-retry="month">重新读取</button></p>`};
    const past = key < today;
    const list = entries.length ? entries.map((item, index) => {
      const title = text(item.item.title) || "未命名安排", detail = item.source === "api" ? text(item.item.detail) : item.detail;
      const label = [kinds[item.item.kind] || "日程", item.item.state === "proposed" ? "待确认" : "", item.moved ? "本次已改期" : "",
        item.continued ? "前一天开始" : "", item.zone !== zone ? item.zone : ""].filter(Boolean).join(" · ");
      const end = item.end;
      return `<li class="gl-agenda-item" data-tone="${past ? "elapsed" : item.item.state === "proposed" ? "proposed" : "planned"}"><span class="gl-agenda-no" aria-hidden="true">${pad(index + 1)}</span>
        <div class="gl-agenda-body"><time>${e(item.time)}${end ? " – " + e(end) : ""}</time><strong>${e(title)}</strong><small>${e(label)}</small>
        ${detail ? `<small class="gl-agenda-detail">${e(detail)}</small>` : ""}</div></li>`;
    }).join("") : `<li class="gl-agenda-empty">${past ? "这天没有日程安排" : "这天还没有安排"}</li>`;
    const notes = fallback.length ? ["日程读取失败，以下按已读取那一批日程的重复规则推算，可能不完整。"] : coverageNotes(entry);
    return {meta:`${entries.length} 项${fallback.length ? "（推算）" : incomplete(entry) ? "（可能不完整）" : ""}`,
      markup:`<ol class="gl-agenda" aria-label="日程">${list}</ol>${notes.length ? `<p class="gl-caption gl-warn" role="status">${e(notes.join(" "))}${fallback.length
        ? ` <button type="button" class="gl-text-button" data-cal-retry="month">重新读取</button>` : ""}</p>` : ""}`};
  }

  /* === 所选日期：生活活动历史（life/activities） === */
  function checkpointText(item) {
    const raw = text(item.checkpoint);
    if (!raw) return "";
    if (!/^[[{]/.test(raw)) return raw;
    try { const parsed = JSON.parse(raw); return text(parsed?.subjective_reflection); } catch { return ""; }
  }
  function historyList(input, zone, day) {
    const history = input.history;
    if (!history || history.status === "loading") return {meta:"读取中", markup:`<p class="gl-empty">正在读取这天的生活记录…</p>`};
    if (history.status === "error")
      return {meta:"读取失败", markup:`<p class="gl-caption gl-warn" role="status">这天的生活记录读取失败。 <button type="button" class="gl-text-button" data-cal-retry="history">重新读取</button></p>`};
    const historyZone = validZone(history.timezone) || zone;
    /* 接口返回的是原始修订（同一活动可能有多条），按 updated_at 倒序；这里每个活动只显示最新一条。 */
    const latest = new Map();
    for (const item of Array.isArray(history.items) ? history.items : []) {
      if (!item || typeof item !== "object" || !activityKinds[item.kind]) continue;
      const id = text(item.activity_id) || `revision-${latest.size}`;
      if (latest.has(id)) latest.get(id).revisions++;
      else latest.set(id, {item, revisions:1});
    }
    const rows = [...latest.values()].sort((a, b) => (Date.parse(a.item.started_at ?? a.item.updated_at) || 0) - (Date.parse(b.item.started_at ?? b.item.updated_at) || 0));
    const list = rows.length ? rows.map(({item, revisions}) => {
      const start = wall(item.started_at, historyZone), updated = wall(item.updated_at, historyZone), due = wall(item.due_at, historyZone);
      const ended = ["completed", "cancelled", "paused"].includes(item.state) ? updated : item.state === "active" ? due : null;
      /* 时间相对所选日期标注：前一晚开始的睡眠写「前一天 23:50 – 07:40」，跨到次日结束的写「次日」。 */
      const rel = (at, key) => (at.key < key ? "前一天 " : at.key > key ? "次日 " : "") + at.time;
      const when = start ? `${rel(start, day)}${ended && (ended.time !== start.time || ended.key !== start.key) ? " – " + rel(ended, day) : ""}`
        : updated ? `${rel(updated, day)} 更新` : "时间未记录";
      const title = text(item.title) || activityKinds[item.kind] + "记录";
      const label = [activityKinds[item.kind], activityStates[item.state] || "", revisions > 1 ? `当天更新 ${revisions} 次` : ""].filter(Boolean).join(" · ");
      const detail = item.unavailable_reason ? "原来源已不可用，内容已隐藏。" : text(item.reflection) || checkpointText(item);
      const tone = item.state === "completed" ? "completed" : item.state === "active" ? "active" : item.state === "paused" ? "paused" : "elapsed";
      return `<li class="gl-agenda-item" data-tone="${tone}"><span class="gl-agenda-no gl-agenda-glyph" aria-hidden="true">${icon(activityIcons[item.kind])}</span>
        <div class="gl-agenda-body"><time>${e(when)}</time><strong>${e(title)}</strong><small>${e(label)}</small>
        ${detail ? `<small class="gl-agenda-detail">${e(detail)}</small>` : ""}</div></li>`;
    }).join("") : `<li class="gl-agenda-empty">这天没有阅读、浏览或睡眠记录</li>`;
    const more = history.next_cursor ? `<button type="button" class="gl-text-button" data-cal-history-more${history.more ? " disabled" : ""}>${history.more ? "正在读取…" : "读取这天更多记录"}</button>` : "";
    const moreError = history.moreError ? `<p class="gl-caption gl-warn" role="status">更多记录读取失败，可再试一次。</p>` : "";
    return {meta:`${rows.length} 项${history.next_cursor ? "，还有更多" : ""}`,
      markup:`<ol class="gl-agenda" aria-label="生活记录">${list}</ol>${moreError}${more ? `<p class="gl-caption">${more}</p>` : ""}`};
  }

  /* === 所选日期的时间线 === */
  function day(input) {
    const {view} = input, zone = window.GoodLifeOverview.zoneOf(input), now = input.now ?? Date.now();
    const today = todayKey(now, zone), key = view.selected || today, {m, d} = parseKey(key);
    const weekday = weekdays[(new Date(utcDay(key)).getUTCDay() + 6) % 7];
    const relation = key === today ? "今天" : key === addDays(today, 1) ? "明天" : key === addDays(today, -1) ? "昨天" : `周${weekday}`;
    const head = `<header class="gl-card-head"><h2>${m + 1} 月 ${d} 日 <small class="gl-cal-rel">${relation}</small></h2>
      <span class="gl-card-note">${key === today ? "实时安排" : key < today ? "日程与生活记录" : "已排日程"}</span></header>`;
    if (key === today) return head + window.GoodLifeOverview.schedule(input);
    const plans = calendarList(input, key, today, zone), history = historyList(input, zone, key);
    return head + `<div class="gl-agenda-meta"><span>日程 ${e(plans.meta)}</span><span>按 ${e(zone)} 显示</span></div>${plans.markup}
      <h3 class="gl-cal-subhead">生活记录 <small>${e(history.meta)}</small></h3>${history.markup}`;
  }

  /* 当前时区下的今天，用来初始化与「今天」按钮。 */
  function initial(input) {
    const key = todayKey(input.now ?? Date.now(), window.GoodLifeOverview.zoneOf(input)), {y, m} = parseKey(key);
    return {year:y, month:m, selected:key};
  }
  function shift(view, delta) {
    const total = view.year * 12 + view.month + delta;
    return {...view, year:Math.floor(total / 12), month:total % 12};
  }
  function select(view, key) { const {y, m} = parseKey(key); return {year:y, month:m, selected:key}; }
  /* 某月的请求范围 [start, end)：本月 1 日到下月 1 日，最多 31 个本地日期。 */
  function monthRange(view) {
    const start = keyOf(view.year, view.month, 1), end = addDays(keyOf(view.year, view.month, daysIn(view.year, view.month)), 1);
    return {key:monthKey(start), start, end};
  }
  const today = input => todayKey(input.now ?? Date.now(), window.GoodLifeOverview.zoneOf(input));

  return {month, day, initial, shift, select, project, monthRange, today, monthKey};
})();
