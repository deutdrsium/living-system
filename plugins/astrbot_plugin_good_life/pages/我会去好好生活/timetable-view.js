/* 课表页：学期、课程与每周上课时间，以及单节课的调整 / 停课。
 *
 * 这里只有纯函数：日期换算、academics 记录与命令的拼装、改动后果的说明文字、页面标记。
 * 不发请求、不持有状态；请求与状态在 timetable-page.js。
 *   - 课程信息嵌在每个上课时间（timetable slot）里，同一门课的各个时间共用 course 对象；
 *   - 单节课的调整写成该时间的 exceptions：只能改当天的时间与地点，或停课；
 *   - 任何改动都会让受影响的课次换新版本，旧的上课提醒随之作废，表单上直接写明。 */
"use strict";
window.GoodLifeTimetable = (() => {
  const icon = name => window.GoodLifeIcons.icon(name);
  const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const text = value => typeof value === "string" ? value.trim() : "";
  const WEEKDAYS = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const RULES = {all:"每周", odd:"单周", even:"双周"};
  const KINDS = {required:"必修", elective:"选修"};
  const DEFAULT_ZONE = "Asia/Shanghai";

  /* === 日期（本地日期字符串 YYYY-MM-DD，用 UTC 运算，不受浏览器时区影响）=== */
  const DATE = /^\d{4}-\d{2}-\d{2}$/, TIME = /^([01]\d|2[0-3]):[0-5]\d$/;
  function isDate(value) {
    if (typeof value !== "string" || !DATE.test(value)) return false;
    const at = new Date(value + "T00:00:00Z");
    return Number.isFinite(at.getTime()) && at.toISOString().slice(0, 10) === value;
  }
  const isTime = value => typeof value === "string" && TIME.test(value);
  const addDays = (day, n) => new Date(Date.parse(day + "T00:00:00Z") + n * 86400000).toISOString().slice(0, 10);
  const daysBetween = (a, b) => Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400000);
  const isoWeekday = day => { const d = new Date(day + "T00:00:00Z").getUTCDay(); return d === 0 ? 7 : d; };
  const minutes = value => isTime(value) ? Number(value.slice(0, 2)) * 60 + Number(value.slice(3)) : NaN;
  function validZone(zone) {
    if (typeof zone !== "string" || !zone) return DEFAULT_ZONE;
    try { new Intl.DateTimeFormat("en-US", {timeZone:zone}); return zone; } catch { return DEFAULT_ZONE; }
  }
  /* 某一时刻在给定时区的日期与时刻。 */
  function localParts(at, zone) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone:validZone(zone), year:"numeric", month:"2-digit",
      day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).formatToParts(new Date(at)).map(p => [p.type, p.value]));
    return {date:`${parts.year}-${parts.month}-${parts.day}`, time:`${parts.hour}:${parts.minute}`};
  }
  const shortDate = day => isDate(day) ? `${Number(day.slice(5, 7))}月${Number(day.slice(8))}日` : "";

  /* === 学期 === */
  const weekCount = term => Array.isArray(term?.week_starts) ? term.week_starts.length : 0;
  /* 某天在第几教学周；不在任何教学周时返回 0。 */
  function weekOf(term, day) {
    const starts = Array.isArray(term?.week_starts) ? term.week_starts : [];
    for (let i = starts.length - 1; i >= 0; i--) {
      const offset = daysBetween(starts[i], day);
      if (offset >= 0 && offset < 7) return i + 1;
      if (offset >= 7) return 0;
    }
    return 0;
  }
  /* 课表按「页」翻：每个教学周一页，教学周之间、第一周之前、最后一周之后的日子也按最多 7 天一页排进去，
     学期里每一天都能看到（调休补课可能落在这些空档里）。每页 {start, end, week}，end 不含，week 为 0 表示非教学周。 */
  function windows(term) {
    const out = [], last = isDate(term?.end_date) ? term.end_date : null, starts = Array.isArray(term?.week_starts) ? term.week_starts : [];
    if (!last || !isDate(term.start_date)) return out;
    let cursor = term.start_date;
    const fill = until => { while (cursor < until) { const end = addDays(cursor, 7) < until ? addDays(cursor, 7) : until; out.push({start:cursor, end, week:0}); cursor = end; } };
    starts.forEach((start, i) => {
      fill(start);
      const end = addDays(start, 7) < last ? addDays(start, 7) : last;
      out.push({start, end, week:i + 1});
      cursor = end;
    });
    fill(last);
    return out;
  }
  /* 第 n 页的课次查询区间 [start, end)。 */
  const weekRange = (term, page) => windows(term)[page - 1];
  /* 打开课表时默认显示的页：今天所在的页；学期还没开始显示第一个教学周，已结束显示最后一个教学周。 */
  function defaultWeek(term, today) {
    const pages = windows(term);
    if (!pages.length) return 0;
    const current = pages.findIndex(item => item.start <= today && today < item.end) + 1;
    if (current) return current;
    const teaching = pages.map((item, i) => item.week ? i + 1 : 0).filter(Boolean);
    return today < pages[0].start ? teaching[0] || 1 : teaching[teaching.length - 1] || pages.length;
  }
  const pageOf = (term, day) => windows(term).findIndex(item => item.start <= day && day < item.end) + 1;
  /* 默认学期：今天在其中的那个，否则开始最晚的那个。 */
  function defaultTerm(terms, today) {
    const list = Array.isArray(terms) ? terms : [];
    return list.find(term => term.start_date <= today && today < term.end_date)
      || [...list].sort((a, b) => String(b.start_date).localeCompare(String(a.start_date)))[0] || null;
  }
  const termLabel = term => text(term?.academic_year) || "未命名学期";

  /* 「2026-10-01, 2026-10-02」或每行一个 → 日期数组；有一项不是日期就报出它。 */
  function parseDates(raw) {
    const items = String(raw ?? "").split(/[\s,，、;；]+/).map(item => item.trim()).filter(Boolean);
    const bad = items.find(item => !isDate(item));
    if (bad) throw fail(`「${bad}」不是日期，请写成 2026-10-01 这样。`);
    return [...new Set(items)].sort();
  }
  /* 每行「补课日期 原上课日期」。 */
  function parseMakeups(raw) {
    return String(raw ?? "").split(/\n+/).map(line => line.trim()).filter(Boolean).map(line => {
      const parts = line.split(/[\s,，→>=]+/).filter(Boolean);
      if (parts.length !== 2 || !parts.every(isDate)) throw fail(`调休「${line}」要写成：补课日期 原上课日期。`);
      return {date:parts[0], teaching_date:parts[1]};
    });
  }
  const fail = message => Object.assign(new Error(message), {code:"invalid_input"});

  /* 学期表单 → Term 记录。周数与第一周没改时保留原来的 week_starts / end_date（可能不是整齐的每 7 天）。 */
  function termRecord(form, existing, ids) {
    const name = text(form.academic_year);
    if (!name) throw fail("请填写学期名称。");
    const start = text(form.start_date), weeks = Number(form.weeks);
    if (!isDate(start)) throw fail("请选择第一周的第一天。");
    if (!Number.isInteger(weeks) || weeks < 1 || weeks > 53) throw fail("周数要在 1 到 53 之间。");
    const zone = text(form.timezone) || DEFAULT_ZONE;
    const keep = existing && existing.week_starts?.[0] === start && weekCount(existing) === weeks;
    const week_starts = keep ? [...existing.week_starts] : Array.from({length:weeks}, (_, i) => addDays(start, i * 7));
    const end_date = keep ? existing.end_date : addDays(start, weeks * 7);
    /* 学期可以早于第一教学周开始（比如报到周）；第一教学周没改时保留原来的开始日。 */
    const start_date = existing && existing.week_starts?.[0] === start ? existing.start_date : start;
    return {term_id:existing?.term_id || ids.term(), academic_year:name, timezone:zone, start_date, end_date, week_starts,
      holidays:parseDates(form.holidays), makeup_days:parseMakeups(form.makeup_days), source:"manual"};
  }
  function termForm(term, zone) {
    return term ? {academic_year:term.academic_year, start_date:term.week_starts?.[0] || term.start_date, weeks:weekCount(term),
      timezone:term.timezone, holidays:(term.holidays || []).join("\n"),
      makeup_days:(term.makeup_days || []).map(item => `${item.date} ${item.teaching_date}`).join("\n")}
      : {academic_year:"", start_date:"", weeks:16, timezone:validZone(zone), holidays:"", makeup_days:""};
  }

  /* === 上课周 === */
  /* 「1-16」「1-8, 10-16」「3,5,7」→ 升序周号；超出学期周数时报错。 */
  function parseWeeks(raw, count) {
    const out = new Set(), source = String(raw ?? "").replace(/[－—–~～至到]/g, "-");
    const parts = source.split(/[\s,，、;；]+/).filter(Boolean);
    if (!parts.length) throw fail("请填写上课周，比如 1-16。");
    for (const part of parts) {
      const match = /^(\d{1,2})(?:-(\d{1,2}))?$/.exec(part);
      if (!match) throw fail(`上课周「${part}」看不懂，请写成 1-16 或 1,3,5。`);
      const a = Number(match[1]), b = Number(match[2] ?? match[1]);
      if (a < 1 || b < a || b > count) throw fail(`上课周要在 1 到 ${count} 之间。`);
      for (let n = a; n <= b; n++) out.add(n);
    }
    return [...out].sort((x, y) => x - y);
  }
  /* 周号数组 → 「1–8、10–16」。 */
  function formatWeeks(weeks) {
    const list = [...new Set(Array.isArray(weeks) ? weeks : [])].filter(Number.isInteger).sort((a, b) => a - b), runs = [];
    for (const n of list) {
      const last = runs[runs.length - 1];
      if (last && n === last[1] + 1) last[1] = n; else runs.push([n, n]);
    }
    return runs.map(([a, b]) => a === b ? String(a) : `${a}-${b}`).join(",");
  }
  const weeksLabel = weeks => `第 ${formatWeeks(weeks).replace(/-/g, "–").replace(/,/g, "、")} 周`;

  /* === 课程与上课时间 === */
  function courseRecord(form, existing, ids) {
    const name = text(form.name);
    if (!name) throw fail("请填写课程名称。");
    const credits = text(form.credits) === "" ? 0 : Number(form.credits);
    if (!Number.isFinite(credits) || credits < 0 || credits > 100) throw fail("学分要在 0 到 100 之间。");
    return {course_id:existing?.course_id || ids.course(), name, kind:form.kind === "elective" ? "elective" : "required",
      credits, teacher_ref:text(form.teacher) || null};
  }
  function slotRecord(form, {term, course, existing, ids}) {
    const weekday = Number(form.weekday);
    if (!Number.isInteger(weekday) || weekday < 1 || weekday > 7) throw fail("请选择星期几。");
    const start = text(form.start_time), end = text(form.end_time);
    if (!isTime(start) || !isTime(end)) throw fail("请填写上课和下课时间。");
    if (minutes(end) <= minutes(start)) throw fail("下课时间要晚于上课时间。");
    const place = text(form.place_id);
    if (!place) throw fail("请选择上课地点。");
    const rule = Object.hasOwn(RULES, form.week_rule) ? form.week_rule : "all";
    const record = {slot_id:existing?.slot_id || ids.slot(), term_id:term.term_id, course, weekday, start_time:start, end_time:end,
      weeks:parseWeeks(form.weeks, weekCount(term)), week_rule:rule, place_id:place, exceptions:[], source:"manual"};
    /* 改了星期或上课周后，落在已经不上这门课那天的单独调整会被后端拒绝，这里一并去掉。 */
    if (existing) record.exceptions = entryRecord(existing).exceptions.filter(item => teaches(term, record, item.date));
    return record;
  }
  /* 这个上课时间在某个实际日期上是否有课（含调休：补课日按原上课日的星期与周次算）。 */
  function teaches(term, slot, date) {
    const moved = (term.makeup_days || []).find(item => item.date === date);
    const replaced = (term.makeup_days || []).some(item => item.teaching_date === date) ? null : date;
    const logical = moved ? moved.teaching_date : replaced;
    if (!logical || (term.holidays || []).includes(date) || isoWeekday(logical) !== slot.weekday) return false;
    const starts = term.week_starts || [];
    const week = starts.findIndex(start => { const d = daysBetween(start, logical); return d >= 0 && d < 7; }) + 1;
    if (!week || !slot.weeks.includes(week)) return false;
    return slot.week_rule === "all" || (slot.week_rule === "odd") === (week % 2 === 1);
  }
  const slotForm = (slot, term) => slot
    ? {weekday:slot.weekday, start_time:slot.start_time, end_time:slot.end_time, weeks:formatWeeks(slot.weeks), week_rule:slot.week_rule, place_id:slot.place_id}
    : {weekday:1, start_time:"08:00", end_time:"09:40", weeks:weekCount(term) ? `1-${weekCount(term)}` : "", week_rule:"all", place_id:""};
  /* 课表里的上课时间按课程分组，组内按星期、时间排序。 */
  function courses(entries) {
    const groups = new Map();
    for (const entry of Array.isArray(entries) ? entries : []) {
      const id = entry?.course?.course_id;
      if (!id) continue;
      if (!groups.has(id)) groups.set(id, {course:entry.course, slots:[]});
      groups.get(id).slots.push(entry);
    }
    const order = (a, b) => a.weekday - b.weekday || String(a.start_time).localeCompare(String(b.start_time));
    return [...groups.values()].map(group => ({...group, slots:group.slots.sort(order)}))
      .sort((a, b) => order(a.slots[0], b.slots[0]) || a.course.name.localeCompare(b.course.name));
  }

  /* 读回来的上课时间带着 schema_version / revision；命令只接受 Entry 的字段，原样回传会被拒绝。 */
  const entryRecord = (slot, course = slot.course) => ({slot_id:slot.slot_id, term_id:slot.term_id,
    course:{course_id:course.course_id, name:course.name, kind:course.kind, credits:course.credits, teacher_ref:course.teacher_ref},
    weekday:slot.weekday, start_time:slot.start_time, end_time:slot.end_time, weeks:[...slot.weeks], week_rule:slot.week_rule,
    place_id:slot.place_id, exceptions:slot.exceptions.map(item => ({date:item.date, cancelled:item.cancelled,
      start_time:item.start_time, end_time:item.end_time, place_id:item.place_id})), source:slot.source});

  /* === 单节课的调整 === */
  /* 把 change 写进 slot 当天的 exception：{cancelled} 或 {start_time, end_time, place_id}；
     和原安排一样的字段写 null；没有任何变化时删掉这条 exception（恢复原样）。 */
  function withException(slot, date, change) {
    const others = entryRecord(slot).exceptions.filter(item => item.date !== date);
    let next = null;
    if (change?.cancelled) next = {date, cancelled:true, start_time:null, end_time:null, place_id:null};
    else if (change) {
      const start = text(change.start_time), end = text(change.end_time), place = text(change.place_id);
      if (!isTime(start) || !isTime(end)) throw fail("请填写这节课的上课和下课时间。");
      if (minutes(end) <= minutes(start)) throw fail("下课时间要晚于上课时间。");
      if (!place) throw fail("请选择这节课的地点。");
      const sameTime = start === slot.start_time && end === slot.end_time;
      next = {date, cancelled:false, start_time:sameTime ? null : start, end_time:sameTime ? null : end,
        place_id:place === slot.place_id ? null : place};
      if (sameTime && next.place_id === null) next = null;
    }
    const exceptions = next ? [...others, next].sort((a, b) => a.date.localeCompare(b.date)) : others;
    return {...entryRecord(slot), exceptions};
  }
  const exceptionOf = (slot, date) => slot?.exceptions?.find(item => item.date === date) || null;

  /* === 命令 === */
  function command(action, kind, id, record, revision, commandId) {
    const body = {command_id:commandId, expected_revision:revision, action, kind, id};
    if (action !== "delete") body.record = record;
    return body;
  }

  /* === 地点 === */
  function placeIndex(setup) {
    const map = new Map();
    for (const place of setup?.places || []) map.set(place.place_id, place);
    return map;
  }
  const placeName = (index, id) => index.get(id)?.name || text(id) || "地点未定";
  /* 同名地点后面补类型，让下拉里能分清。 */
  function placeOptions(setup, query = "") {
    const list = setup?.places || [], counts = new Map();
    for (const place of list) counts.set(place.name, (counts.get(place.name) || 0) + 1);
    const q = text(query).toLowerCase();
    return list.filter(place => !q || place.name.toLowerCase().includes(q) || String(place.type).toLowerCase().includes(q))
      .map(place => ({value:place.place_id, label:counts.get(place.name) > 1 ? `${place.name}（${place.type}）` : place.name}));
  }

  /* === 说明文字 === */
  /* 读不到开关时按「会有提醒」说，宁可多提醒一句。 */
  const reminderNote = setup => setup?.classes_enabled === false ? "上课功能还没开，暂时没有提醒" : "原来的提醒会作废";
  function sessionPreview(session, slot, form, setup, zone) {
    const index = placeIndex(setup), times = sessionTimes(session, zone);
    if (form?.cancelled) return `${shortDate(session.local_date)}这节课会停上，${reminderNote(setup)}。`;
    const start = text(form?.start_time), end = text(form?.end_time), place = text(form?.place_id);
    if (session.cancelled) return `这节课恢复上课：${start}–${end}，${placeName(index, place)}。`;
    const changes = [];
    if (start && end && (start !== times.start || end !== times.end)) changes.push(`${start}–${end}`);
    if (place && place !== session.place_id) changes.push(placeName(index, place));
    if (!changes.length) {
      const back = slot && exceptionOf(slot, session.local_date) && start === slot.start_time && end === slot.end_time && place === slot.place_id;
      return back ? `这节课会恢复成原来的安排，${reminderNote(setup)}。` : "还没有改动。";
    }
    return `这节课改到 ${changes.join("，")}，${reminderNote(setup)}。`;
  }
  const slotNote = (setup, isNew) => isNew ? "保存后，从现在起按这个时间上课。"
    : `保存后，以后的每节都按新安排；已经上过的不变，${reminderNote(setup)}。`;
  const deleteNote = (setup, what) => `删除后，${what}以后的课都会取消；已经上过的会留下记录，${reminderNote(setup)}。`;

  /* 后端理由 → 简短中文。认不出的原样附上。 */
  const REASONS = [
    [/revision changed|already exists or is unavailable/, "数据刚有变化，已重新读取，请再确认一次。"],
    [/overlap/, "和别的课时间冲突了。"],
    [/week outside|teaching weeks/, "上课周超出了学期的周数。"],
    [/delete the term's timetable slots first/, "这个学期里还有课程，请先删掉课程。"],
    [/exception must address/, "有一次单独调整的那天已经没有这节课了，请先把那次恢复原样。"],
    [/ambiguous or nonexistent/, "这个时间在该时区不存在（夏令时），换个时间。"],
    [/timezone cannot change/, "已经有课开始过，学期时区不能再改。"],
    [/timezone must be/, "时区写法不对，比如 Asia/Shanghai。"],
    [/holiday|makeup/, "放假或调休日期不对，要在学期之内且不能重复。"],
    [/term range/, "学期最长一年。"],
    [/record limit/, "课太多了，最多 8 个学期、128 个上课时间。"],
    [/inconsistent metadata/, "同一门课的信息不一致，请刷新后再试。"],
    [/academics is disabled/, "课表功能还没开启，现在不能保存。"],
  ];
  function reason(error) {
    if (error?.code === "invalid_input") return error.message;
    const raw = String(error?.message || "");
    for (const [pattern, message] of REASONS) if (pattern.test(raw)) return message;
    return raw ? `没有保存：${raw}` : "没有保存，请稍后再试。";
  }

  /* === 课次 === */
  function sessionTimes(session, zone) {
    const start = Date.parse(session.start_at), end = Date.parse(session.end_at);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return {start:"", end:"", startMin:NaN, endMin:NaN};
    const a = localParts(start, zone).time, b = localParts(end, zone).time;
    return {start:a, end:b, startMin:minutes(a), endMin:minutes(b)};
  }
  const started = (session, now) => Date.parse(session.start_at) <= now;

  /* === 标记 === */
  const head = (title, note, action = "") => `<header class="gl-card-head"><h2>${e(title)}</h2>${note ? `<span class="gl-card-note">${e(note)}</span>` : ""}${action}</header>`;
  const empty = (glyph, title, body, action = "") => `<div class="gl-empty-state">${icon(glyph)}<div><strong>${e(title)}</strong>${body ? `<p>${e(body)}</p>` : ""}${action}</div></div>`;
  const button = (label, attrs, cls = "gl-chip-button") => `<button type="button" class="${cls}" ${attrs}>${e(label)}</button>`;
  const field = (label, control, hint = "", wide = false) => `<label class="gl-tt-field${wide ? " is-wide" : ""}"><span>${e(label)}</span>${control}${hint ? `<small>${e(hint)}</small>` : ""}</label>`;
  const input = (name, value, attrs = "") => `<input name="${name}" value="${e(value)}" ${attrs}>`;
  const select = (name, options, value) => `<select name="${name}">${options.map(([v, label]) =>
    `<option value="${e(v)}"${String(v) === String(value) ? " selected" : ""}>${e(label)}</option>`).join("")}</select>`;

  function placeControl(setup, value, filter = "") {
    const options = placeOptions(setup, filter);
    if (!(setup?.places || []).length)
      return field("地点", input("place_id", value, 'required maxlength="256" placeholder="地点代号"'),
        "还没有确认的校园地图，只能手填地点代号；去上课时可能找不到路。", true);
    const known = placeIndex(setup).has(value);
    const list = [...(value && !known ? [{value, label:`当前：${value}`}] : []), ...options];
    const current = list.some(item => item.value === value) ? value : "";
    return `<div class="gl-tt-field is-wide gl-tt-place"><span>地点</span>
      <input type="search" data-tt-place-filter value="${e(filter)}" placeholder="搜索楼名" aria-label="搜索地点">
      <select name="place_id" required aria-label="上课地点"><option value=""${current ? "" : " selected"}>${list.length ? "选择地点" : "没有找到，换个词试试"}</option>${list.map(item =>
        `<option value="${e(item.value)}"${item.value === current ? " selected" : ""}>${e(item.label)}</option>`).join("")}</select>
      <small>地点来自已确认的校园地图。</small></div>`;
  }
  const actions = (busy, submit, extra = "") => `<div class="gl-tt-actions">
    <button type="submit" class="gl-tt-primary"${busy ? " disabled" : ""}>${e(busy ? "正在保存…" : submit)}</button>
    ${extra}${button("取消", 'data-tt-close')}</div>`;
  const note = (message, tone = "") => `<p class="gl-tt-consequence"${tone ? ` data-tone="${tone}"` : ""}>${icon(tone === "risk" ? "alert" : "bell")}<span>${e(message)}</span></p>`;

  function termPanel(state, ctx) {
    const term = state.panel.termId ? state.terms.find(item => item.term_id === state.panel.termId) : null;
    const form = state.draft || termForm(term, ctx.zone);
    const confirm = term && state.confirm === "term:" + term.term_id;
    const remove = term ? (confirm
      ? `<div class="gl-tt-confirm" role="group" aria-label="确认删除学期"><span>删掉这个学期？</span>${button("确定删除", 'data-tt-delete-term="' + e(term.term_id) + '"', "gl-chip-button is-danger")}${button("算了", "data-tt-unconfirm")}</div>`
      : button("删除学期", 'data-tt-confirm="term:' + e(term.term_id) + '"', "gl-chip-button is-danger")) : "";
    return `<form class="gl-tt-form" data-tt-form="term">${head(term ? "编辑学期" : "新建学期")}
      <div class="gl-tt-fields">
        ${field("学期名称", input("academic_year", form.academic_year, 'required maxlength="256" placeholder="2026–2027 秋季"'), "", true)}
        ${field("第一周第一天", input("start_date", form.start_date, 'type="date" required'))}
        ${field("周数", input("weeks", form.weeks, 'type="number" min="1" max="53" required inputmode="numeric"'))}
        ${field("放假日期", `<textarea name="holidays" rows="2" placeholder="2026-10-01 2026-10-02">${e(form.holidays)}</textarea>`, "这些天不上课。可以不填。", true)}
        ${field("调休补课", `<textarea name="makeup_days" rows="2" placeholder="2026-10-10 2026-10-08">${e(form.makeup_days)}</textarea>`, "每行：补课那天 原来上课那天。可以不填。", true)}
        ${field("时区", input("timezone", form.timezone, 'required maxlength="64" spellcheck="false"'), "一般不用改。")}
      </div>
      ${term ? note(`改了周数或日期，以后的课会重新排，${reminderNote(state.setup)}。`) : ""}
      ${actions(state.busy, term ? "保存学期" : "创建学期", remove)}</form>`;
  }

  function coursePanel(state) {
    const group = state.panel.courseId ? courses(state.entries).find(item => item.course.course_id === state.panel.courseId) : null;
    const term = currentTerm(state), course = group?.course;
    const form = state.draft || (course ? {name:course.name, kind:course.kind, credits:String(course.credits), teacher:course.teacher_ref || ""}
      : {name:"", kind:"required", credits:"", teacher:"", ...slotForm(null, term)});
    const fields = `${field("课程名称", input("name", form.name, 'required maxlength="256" placeholder="比如 高等数学"'), "", true)}
      ${field("类型", select("kind", [["required", "必修"], ["elective", "选修"]], form.kind))}
      ${field("学分", input("credits", form.credits, 'type="number" min="0" max="100" step="0.5" inputmode="decimal" placeholder="0"'))}
      ${field("老师", input("teacher", form.teacher, 'maxlength="256" placeholder="可以不填"'), "", true)}`;
    return `<form class="gl-tt-form" data-tt-form="course">${head(course ? "编辑课程" : "添加课程")}
      <div class="gl-tt-fields">${fields}</div>
      ${course ? note(`课程信息改了，以后的每节课都会更新，${reminderNote(state.setup)}。`)
        : `<h3 class="gl-tt-subhead">第一个上课时间</h3><div class="gl-tt-fields">${slotFields(state, form)}</div>${note(slotNote(state.setup, true))}`}
      ${actions(state.busy, course ? "保存课程" : "添加课程")}</form>`;
  }

  function slotFields(state, form) {
    const weekdays = WEEKDAYS.slice(1).map((label, i) => [i + 1, label]);
    const count = weekCount(currentTerm(state));
    return `${field("星期", select("weekday", weekdays, form.weekday))}
      ${field("单双周", select("week_rule", Object.entries(RULES), form.week_rule))}
      ${field("上课", input("start_time", form.start_time, 'type="time" required'))}
      ${field("下课", input("end_time", form.end_time, 'type="time" required'))}
      ${field("上课周", input("weeks", form.weeks, 'required placeholder="1-16" inputmode="numeric"'), `本学期共 ${count} 周。可写 1-8,10-16。`, true)}
      ${placeControl(state.setup, form.place_id, state.placeFilter)}`;
  }

  function slotPanel(state) {
    const slot = state.panel.slotId ? state.entries.find(item => item.slot_id === state.panel.slotId) : null;
    const group = courses(state.entries).find(item => item.course.course_id === (slot?.course.course_id || state.panel.courseId));
    const form = state.draft || slotForm(slot, currentTerm(state));
    const adjusted = slot?.exceptions.length ? ` 改星期或上课周时，不再有课那天的单独调整会一并清除。` : "";
    return `<form class="gl-tt-form" data-tt-form="slot">${head(slot ? "改上课时间" : "加一个上课时间", group?.course.name || "")}
      <div class="gl-tt-fields">${slotFields(state, form)}</div>
      ${note(slotNote(state.setup, !slot) + adjusted)}
      ${actions(state.busy, slot ? "保存" : "添加")}</form>`;
  }

  function sessionPanel(state, ctx) {
    const session = state.occurrences?.items?.find(item => item.session_id === state.panel.sessionId);
    if (!session) return `<div class="gl-tt-form">${head("这节课")}<p class="gl-empty">这节课已不在本周，请重新选择。</p>
      <div class="gl-tt-actions">${button("关闭", "data-tt-close")}</div></div>`;
    const zone = currentTerm(state)?.timezone, times = sessionTimes(session, zone), index = placeIndex(state.setup);
    const slot = state.entries?.find(item => item.slot_id === session.slot_id) || null;
    const exception = exceptionOf(slot, session.local_date);
    const chips = [session.cancelled ? `<span class="gl-chip is-muted">已停课</span>` : "",
      exception && !session.cancelled ? `<span class="gl-chip is-ok">已调整</span>` : ""].join("");
    const facts = `<dl class="gl-tt-facts"><div><dt>日期</dt><dd>${e(shortDate(session.local_date))} ${e(WEEKDAYS[isoWeekday(session.local_date)])}</dd></div>
      <div><dt>时间</dt><dd>${e(times.start)}–${e(times.end)}</dd></div><div><dt>地点</dt><dd>${e(placeName(index, session.place_id))}</dd></div></dl>`;
    const title = head(session.course_name || "这节课", "", chips ? `<span class="gl-chips">${chips}</span>` : "");
    /* 上课时间改过星期或上课周后，旧日子的课次只留作记录：单独调整只能落在现在还有课的日子上。 */
    const scheduled = slot && currentTerm(state) && teaches(currentTerm(state), slot, session.local_date);
    if (started(session, ctx.now) || !slot || !scheduled) {
      const why = !slot ? "这个上课时间已经删掉了，这节课只留作记录。"
        : !scheduled ? "这节课已经不在现在的上课安排里，只留作记录。" : "这节课已经开始或上过了，不能再改。";
      return `<div class="gl-tt-form">${title}${facts}<p class="gl-caption">${e(why)}</p><div class="gl-tt-actions">${button("关闭", "data-tt-close")}</div></div>`;
    }
    const form = state.draft || {start_time:times.start, end_time:times.end, place_id:session.place_id};
    const restore = exception ? button("恢复原样", "data-tt-session-restore") : "";
    const cancel = session.cancelled ? "" : button("这次停课", "data-tt-session-cancel", "gl-chip-button is-danger");
    return `<form class="gl-tt-form" data-tt-form="session">${title}${facts}
      <p class="gl-caption">只改这一天：可以换时间、换地点，或者停一次。</p>
      <div class="gl-tt-fields">${field("上课", input("start_time", form.start_time, 'type="time" required'))}
        ${field("下课", input("end_time", form.end_time, 'type="time" required'))}
        ${placeControl(state.setup, form.place_id, state.placeFilter)}</div>
      <p class="gl-tt-consequence" data-tt-preview aria-live="polite">${icon("bell")}<span>${e(sessionPreview(session, slot, form, state.setup, zone))}</span></p>
      ${actions(state.busy, session.cancelled ? "恢复上课并保存" : "保存这次调整", cancel + restore)}</form>`;
  }

  const currentTerm = state => state.terms?.find(item => item.term_id === state.termId) || null;

  function termCard(state, ctx) {
    const term = currentTerm(state), today = localParts(ctx.now, term?.timezone || ctx.zone).date;
    const choose = state.terms.length > 1 ? `<label class="gl-tt-term-pick"><span class="gl-sr">选择学期</span>${select("term", state.terms.map(item => [item.term_id, termLabel(item)]), state.termId).replace("<select", "<select data-tt-term")}</label>` : "";
    const flag = state.setup && !state.setup.classes_enabled
      ? `<p class="gl-tt-flag">${icon("pause")}<span>上课功能还没开启。课表可以先录好，开启后才会按课表去上课、发提醒。</span></p>` : "";
    if (!term) return `<article class="gl-card gl-tt-term">${head("学期")}
      ${empty("calendar", "还没有学期", "先建一个学期：写上第一周从哪天开始、一共几周，然后再加课程。", button("新建学期", "data-tt-new-term", "gl-chip-button gl-tt-cta"))}${flag}</article>`;
    const current = weekOf(term, today);
    const status = current ? `今天是第 ${current} 周` : today < term.start_date ? "还没开学" : today >= term.end_date ? "已经结束" : "今天不在教学周";
    const facts = [`${shortDate(term.week_starts[0])}开始`, `共 ${weekCount(term)} 周`,
      term.holidays.length ? `放假 ${term.holidays.length} 天` : "", term.makeup_days.length ? `调休 ${term.makeup_days.length} 天` : ""].filter(Boolean);
    return `<article class="gl-card gl-tt-term">
      <header class="gl-card-head"><h2>${e(termLabel(term))}</h2><span class="gl-tt-term-tools">${choose}${button("编辑", 'data-tt-edit-term="' + e(term.term_id) + '"')}${button("新建学期", "data-tt-new-term")}</span></header>
      <p class="gl-tt-term-meta"><strong>${e(status)}</strong>${facts.map(item => `<span>${e(item)}</span>`).join("")}</p>${flag}</article>`;
  }

  function weekCard(state, ctx) {
    const term = currentTerm(state);
    if (!term) return "";
    const count = windows(term).length, week = state.week, today = localParts(ctx.now, term.timezone).date;
    /* 课程读回来之前还没定下看哪一页。 */
    if (!(week >= 1 && week <= count)) return `<article class="gl-card gl-tt-week" aria-label="每周课表"><p class="gl-empty" aria-busy="true">正在读取这一周的课…</p></article>`;
    const range = weekRange(term, week), occ = state.occurrences, here = pageOf(term, today);
    const nav = `<div class="gl-tt-weeknav">
      <button type="button" class="gl-icon-button" data-tt-week="${week - 1}" aria-label="上一周"${week <= 1 ? " disabled" : ""}>${icon("chevron-left")}</button>
      <span class="gl-tt-weeklabel"><strong>${range.week ? `第 ${range.week} 周` : "非教学周"}</strong><small>${e(shortDate(range.start))}–${e(shortDate(addDays(range.end, -1)))}</small></span>
      <button type="button" class="gl-icon-button" data-tt-week="${week + 1}" aria-label="下一周"${week >= count ? " disabled" : ""}>${icon("chevron")}</button>
      ${here && here !== week ? button("回到本周", 'data-tt-week="' + here + '"') : ""}</div>`;
    let body;
    if (!occ || occ.status === "loading") body = `<p class="gl-empty" aria-busy="true">正在读取这一周的课…</p>`;
    else if (occ.status !== "ok") body = empty("alert", "这一周的课暂时读不到", occ.code ? `错误代码：${occ.code}` : "", button("重新读取", "data-tt-retry"));
    else body = grid(state, term, range, today);
    return `<article class="gl-card gl-tt-week" aria-label="每周课表">${nav}${body}</article>`;
  }

  /* 一周的格子：左侧时间刻度 + 7 列；课块按时刻定位。窄屏由 CSS 改成按天的列表。 */
  function grid(state, term, range, today) {
    const zone = term.timezone, index = placeIndex(state.setup);
    const days = Array.from({length:Math.max(1, daysBetween(range.start, range.end))}, (_, i) => addDays(range.start, i));
    const items = state.occurrences.items.map(item => ({item, ...sessionTimes(item, zone)})).filter(item => Number.isFinite(item.startMin));
    const first = Math.min(8 * 60, ...items.map(item => item.startMin)), last = Math.max(18 * 60, ...items.map(item => item.endMin));
    const top = Math.floor(first / 60) * 60, bottom = Math.ceil(last / 60) * 60, span = bottom - top;
    const hours = Array.from({length:span / 60 + 1}, (_, i) => top + i * 60);
    const holidays = new Set(term.holidays);
    const slots = new Map((state.entries || []).map(slot => [slot.slot_id, slot]));
    const column = day => {
      const list = items.filter(item => item.item.local_date === day).sort((a, b) => a.startMin - b.startMin);
      const blocks = list.map(({item, start, end, startMin, endMin}) => {
        const changed = !!exceptionOf(slots.get(item.slot_id), item.local_date);
        const state_ = item.cancelled ? "cancelled" : changed ? "changed" : "normal";
        const label = `${item.course_name}，${start}–${end}，${placeName(index, item.place_id)}${item.cancelled ? "，已停课" : changed ? "，已调整" : ""}`;
        return `<button type="button" class="gl-tt-block" data-state="${state_}" data-tt-session="${e(item.session_id)}"
          style="--tt-top:${((startMin - top) / span * 100).toFixed(3)}%;--tt-h:${((endMin - startMin) / span * 100).toFixed(3)}%" aria-label="${e(label)}">
          <strong>${e(item.course_name)}</strong><small>${e(start)}–${e(end)}</small><small>${e(placeName(index, item.place_id))}</small>
          ${item.cancelled ? `<em>停课</em>` : changed ? `<em>调整</em>` : ""}</button>`;
      }).join("");
      const tag = holidays.has(day) ? `<small class="gl-tt-tag">放假</small>` : "";
      return `<div class="gl-tt-day"${day === today ? " data-today" : ""}${list.length ? "" : " data-empty"}>
        <div class="gl-tt-dayhead"><strong>${e(WEEKDAYS[isoWeekday(day)])}</strong><small>${e(shortDate(day))}</small>${tag}</div>
        <div class="gl-tt-daybody">${blocks || `<p class="gl-tt-none">没有课</p>`}</div></div>`;
    };
    const scale = `<div class="gl-tt-scale" aria-hidden="true">${hours.map(m =>
      `<span style="--tt-top:${((m - top) / span * 100).toFixed(3)}%">${String(m / 60).padStart(2, "0")}:00</span>`).join("")}</div>`;
    const lines = `<div class="gl-tt-lines" aria-hidden="true">${hours.map(m => `<i style="--tt-top:${((m - top) / span * 100).toFixed(3)}%"></i>`).join("")}</div>`;
    const total = items.filter(item => !item.item.cancelled).length;
    return `<div class="gl-tt-grid" style="--tt-hours:${span / 60}">${scale}<div class="gl-tt-days">${lines}${days.map(column).join("")}</div></div>
      <p class="gl-caption">${total ? `这周 ${total} 节课。点一节课可以单独调整或停课。` : "这周没有课。"}按 ${e(zone)} 显示。</p>`;
  }

  function courseCard(state) {
    const term = currentTerm(state);
    if (!term) return "";
    const add = button("添加课程", "data-tt-new-course", "gl-chip-button gl-tt-cta");
    if (!state.entries) return `<article class="gl-card gl-tt-courses">${head("课程")}<p class="gl-empty" aria-busy="true">正在读取课程…</p></article>`;
    const groups = courses(state.entries), index = placeIndex(state.setup);
    if (!groups.length) return `<article class="gl-card gl-tt-courses">${head("课程")}${empty("book", "还没有课程", "添加课程时，顺便填上它每周什么时候、在哪儿上。", add)}</article>`;
    const rows = groups.map(({course, slots}) => {
      const id = course.course_id, confirm = state.confirm === "course:" + id;
      const chips = [KINDS[course.kind] || "", Number.isFinite(course.credits) && course.credits ? `${course.credits} 学分` : "", text(course.teacher_ref)].filter(Boolean)
        .map(item => `<span class="gl-chip">${e(item)}</span>`).join("");
      const times = slots.map(slot => {
        const sure = state.confirm === "slot:" + slot.slot_id, changed = slot.exceptions.length;
        return `<li class="gl-tt-slot"><div><strong>${e(WEEKDAYS[slot.weekday])} ${e(slot.start_time)}–${e(slot.end_time)}</strong>
          <small>${e(placeName(index, slot.place_id))} · ${e(weeksLabel(slot.weeks))}${slot.week_rule !== "all" ? ` · ${e(RULES[slot.week_rule])}` : ""}${changed ? ` · ${changed} 次单独调整` : ""}</small></div>
          <span class="gl-tt-row-tools">${sure
            ? `<span class="gl-tt-confirm" role="group" aria-label="确认删除上课时间"><span>${e(deleteNote(state.setup, "这个时间"))}</span>${button("确定删除", 'data-tt-delete-slot="' + e(slot.slot_id) + '"', "gl-chip-button is-danger")}${button("算了", "data-tt-unconfirm")}</span>`
            : `${button("改", 'data-tt-edit-slot="' + e(slot.slot_id) + '" aria-label="改' + e(WEEKDAYS[slot.weekday] + slot.start_time) + '"')}${slots.length > 1 ? button("删", 'data-tt-confirm="slot:' + e(slot.slot_id) + '" aria-label="删除这个上课时间"') : ""}`}</span></li>`;
      }).join("");
      return `<li class="gl-tt-course"><div class="gl-tt-course-head"><div><strong>${e(course.name)}</strong><span class="gl-chips">${chips}</span></div>
        <span class="gl-tt-row-tools">${button("编辑", 'data-tt-edit-course="' + e(id) + '"')}${button("加时间", 'data-tt-new-slot="' + e(id) + '"')}${confirm ? "" : button("删除", 'data-tt-confirm="course:' + e(id) + '"', "gl-chip-button is-danger")}</span></div>
        ${confirm ? `<div class="gl-tt-confirm" role="group" aria-label="确认删除课程"><span>${e(deleteNote(state.setup, "这门课"))}</span>${button("确定删除", 'data-tt-delete-course="' + e(id) + '"', "gl-chip-button is-danger")}${button("算了", "data-tt-unconfirm")}</div>` : ""}
        <ul class="gl-tt-slots">${times}</ul></li>`;
    }).join("");
    return `<article class="gl-card gl-tt-courses">${head("课程", `${groups.length} 门`, add)}<ul class="gl-tt-course-list">${rows}</ul></article>`;
  }

  /* === 学习（只读）：每门课的大纲、作业与进度、角色的自习记录、已发布的成绩 ===
   * state.study = {termId, open:[键], courses:{课程编号:读取结果}, grades, artifacts:{作业编号:列表}, artifact:打开的那份}。
   * 展开的 <details> 用 data-tt-open 的键记在 state.study.open 里，重绘后照样展开。 */
  const ARTIFACT_KINDS = {draft:"作业稿", note:"笔记", review:"复习"};
  const freshStudy = termId => ({termId:termId ?? null, open:[], courses:{}, grades:null, artifacts:{}, artifact:null});
  const studyOf = state => state.study?.termId === state.termId ? state.study : freshStudy(state.termId);
  function moment(at, zone, withWeekday = false) {
    const time = Date.parse(at);
    if (!Number.isFinite(time)) return "";
    const parts = localParts(time, zone);
    return `${shortDate(parts.date)}${withWeekday ? " " + WEEKDAYS[isoWeekday(parts.date)] : ""} ${parts.time}`;
  }
  function duration(seconds) {
    const total = Math.max(0, Math.round((Number(seconds) || 0) / 60));
    if (total < 60) return `${total} 分钟`;
    return `${Math.floor(total / 60)} 小时${total % 60 ? ` ${total % 60} 分` : ""}`;
  }
  /* 一项作业的进度：把它的各段工作加起来。没有分段记录时 chunks 为空。 */
  function progressOf(assignment, items, now) {
    const chunks = (Array.isArray(items) ? items : []).filter(item => item.assignment_id === assignment.assignment_id);
    const required = chunks.reduce((sum, item) => sum + (Number(item.required_work_s) || 0), 0);
    const completed = chunks.reduce((sum, item) => sum + (Number(item.completed_work_s) || 0), 0);
    const done = chunks.length > 0 && chunks.every(item => item.status === "completed");
    const started = completed > 0 || chunks.some(item => item.status !== "pending");
    const late = !done && Date.parse(assignment.due_at) <= now;
    const status = done ? ["done", "已完成", "is-ok"] : late ? ["late", "已过截止", "is-warn"] : started ? ["active", "进行中", ""] : ["todo", "未开始", "is-muted"];
    return {chunks, required, completed, finished:chunks.filter(item => item.status === "completed").length, status};
  }
  const quiet = (message, extra = "") => `<p class="gl-tt-study-quiet">${e(message)}${extra}</p>`;
  const unavailable = (what, code) => code === "missing_dependency" ? `${what}还没开启。` : `${what}暂时看不到，可以稍后再试。`;
  const gradeOf = (study, courseId) => study.grades?.status === "ok" ? study.grades.items.find(item => item.course_id === courseId) || null : null;

  function artifactDetail(view, zone) {
    let body;
    if (view.status === "loading") body = `<p class="gl-empty" aria-busy="true">正在打开…</p>`;
    else if (view.status !== "ok") body = quiet("这份记录暂时打不开，可以稍后再试。");
    else if (view.record.availability !== "available" || typeof view.record.text !== "string")
      body = quiet("这份记录用到的材料现在不能读，暂时看不到全文。");
    else body = `<div class="gl-tt-artifact-text">${e(view.record.text)}</div>${text(view.record.next_step)
      ? `<p class="gl-tt-artifact-next"><strong>下一步</strong><span>${e(view.record.next_step)}</span></p>` : ""}`;
    const title = view.record?.title || "自习记录";
    return `<div class="gl-tt-artifact-open" role="region" aria-label="${e(title)}">${body}
      <div class="gl-tt-actions">${button("收起", 'data-tt-artifact-close')}</div></div>`;
  }
  function artifactList(list, study, assignmentId, zone) {
    if (!list || list.status === "loading") return `<p class="gl-empty" aria-busy="true">正在读取自习记录…</p>`;
    if (list.status !== "ok") return quiet(unavailable("自习记录", list.code));
    if (!list.items.length) return quiet("还没有自习记录。");
    const rows = list.items.map(item => {
      const opened = study.artifact?.id === item.artifact_id;
      const meta = [ARTIFACT_KINDS[item.kind] || text(item.kind), moment(item.updated_at, zone) && `${moment(item.updated_at, zone)} 更新`].filter(Boolean).join(" · ");
      return `<li><button type="button" class="gl-tt-artifact" data-tt-artifact="${e(item.artifact_id)}" aria-expanded="${opened}">
        <strong>${e(item.title || "自习记录")}</strong><small>${e(meta)}</small>${icon("chevron")}</button>${opened ? artifactDetail(study.artifact, zone) : ""}</li>`;
    }).join("");
    const more = list.next ? button(list.more ? "正在读取…" : "更早的记录", `data-tt-artifacts-more="${e(assignmentId)}"${list.more ? " disabled" : ""}`) : "";
    return `<ul class="gl-tt-artifacts" aria-label="自习记录">${rows}</ul>${more}`;
  }

  function assignmentBlock(data, study, zone, now) {
    if (!Array.isArray(data.assignments)) return quiet(unavailable("作业", data.codes?.assignments));
    if (!data.assignments.length) return quiet("这门课还没有作业。");
    const units = new Map((data.units || []).map(unit => [unit.unit_id, unit]));
    const lists = data.assignments.map(item => study.artifacts[item.assignment_id]);
    /* 所有作业都还没有记录（或都读不到）时只在列表下面说一次，不在每项下面重复。 */
    const allEmpty = lists.every(list => list?.status === "ok" && !list.items.length);
    const allOff = lists.every(list => list && list.status === "error");
    const shared = allEmpty || allOff;
    const rows = [...data.assignments].sort((a, b) => String(a.due_at).localeCompare(String(b.due_at))).map(item => {
      const progress = progressOf(item, data.progress, now), [state_, label, tone] = progress.status, unit = units.get(item.unit_id);
      const facts = [`截止 ${moment(item.due_at, zone, true) || "未定"}`, unit ? `第 ${unit.week} 周 · ${unit.topic}` : ""].filter(Boolean).join(" · ");
      let work = "";
      if (!Array.isArray(data.progress)) work = "";
      else if (progress.chunks.length) {
        const share = progress.required ? Math.min(100, Math.round(progress.completed / progress.required * 100)) : 0;
        work = `<div class="gl-tt-progress" role="img" aria-label="完成 ${share}%"><span style="--tt-done:${share}%"></span></div>
          <small class="gl-tt-work">已做 ${e(duration(progress.completed))} / ${e(duration(progress.required))} · ${progress.finished}/${progress.chunks.length} 段完成</small>`;
      } else if (Number(item.estimated_work_s) > 0) work = `<small class="gl-tt-work">预计 ${e(duration(item.estimated_work_s))}，还没开始做。</small>`;
      return `<li class="gl-tt-assignment" data-state="${state_}">
        <div class="gl-tt-assignment-head"><strong>${e(item.title || "作业")}</strong><span class="gl-chip${tone ? " " + tone : ""}">${e(label)}</span></div>
        <small class="gl-tt-assignment-facts">${e(facts)}</small>${work}
        ${shared ? "" : artifactList(study.artifacts[item.assignment_id], study, item.assignment_id, zone)}</li>`;
    }).join("");
    const note = allEmpty ? quiet("还没有自习记录，上过课、开始写作业后会出现在这里。")
      : allOff ? quiet(unavailable("自习记录", lists[0].code)) : "";
    return `<ul class="gl-tt-assignments">${rows}</ul>${note}`;
  }

  function syllabusBlock(data, open, courseId) {
    if (!Array.isArray(data.units)) return quiet(unavailable("大纲", data.codes?.units));
    if (!data.units.length) return quiet("这门课还没有录入大纲。");
    const key = "syllabus:" + courseId;
    const units = [...data.units].sort((a, b) => (a.week - b.week) || String(a.topic).localeCompare(String(b.topic)));
    const rows = units.map(unit => `<li><span class="gl-tt-unit-week">第 ${e(unit.week)} 周</span><div><strong>${e(unit.topic)}</strong>${
      (unit.concepts || []).length ? `<small>${e(unit.concepts.join("、"))}</small>` : ""}</div></li>`).join("");
    return `<details class="gl-tt-syllabus" data-tt-open="${e(key)}"${open.has(key) ? " open" : ""}>
      <summary>大纲 · ${units.length} 个单元</summary><ol class="gl-tt-units">${rows}</ol></details>`;
  }

  function studyCourse(course, study, open, zone, now) {
    const id = course.course_id, key = "course:" + id, data = study.courses[id], grade = gradeOf(study, id);
    let meta = "展开看大纲、作业和自习记录";
    if (data?.status === "ok" && Array.isArray(data.assignments)) {
      const next = data.assignments.filter(item => Date.parse(item.due_at) > now && progressOf(item, data.progress, now).status[0] !== "done")
        .sort((a, b) => String(a.due_at).localeCompare(String(b.due_at)))[0];
      meta = data.assignments.length ? `${data.assignments.length} 项作业${next ? ` · 最近截止 ${moment(next.due_at, zone)}` : ""}` : "还没有作业";
    }
    let body;
    if (!data || data.status === "loading") body = `<p class="gl-empty" aria-busy="true">正在读取大纲和作业…</p>`;
    else if (data.status !== "ok") body = quiet(unavailable("这门课的大纲和作业", data.code), " " + button("重新读取", `data-tt-study-retry="${e(id)}"`));
    else {
      const grades = study.grades;
      const gradeLine = grade ? `<p class="gl-tt-grade"><span>成绩</span><strong>${e(grade.score)}</strong>${grade.letter ? `<em>${e(grade.letter)}</em>` : ""}${
        moment(grade.release_at, zone) ? `<small>${e(shortDate(localParts(Date.parse(grade.release_at), zone).date))}发布</small>` : ""}</p>`
        : grades?.status === "ok" ? quiet("成绩还没发布。") : grades?.status === "error" ? quiet(unavailable("成绩", grades.code)) : "";
      body = `${gradeLine}<section class="gl-tt-study-part"><h3>作业</h3>${assignmentBlock(data, study, zone, now)}</section>
        <section class="gl-tt-study-part">${syllabusBlock(data, open, id)}</section>`;
    }
    const chip = grade ? `<span class="gl-chip is-ok">${e(grade.score)}${grade.letter ? ` · ${e(grade.letter)}` : ""}</span>` : "";
    return `<details class="gl-tt-study-course" data-tt-open="${e(key)}"${open.has(key) ? " open" : ""}>
      <summary><span class="gl-tt-study-name"><strong>${e(course.name)}</strong><small>${e(meta)}</small></span>${chip}${icon("chevron")}</summary>
      <div class="gl-tt-study-body">${body}</div></details>`;
  }

  function studyCard(state, ctx) {
    const term = currentTerm(state);
    if (!term || !state.entries) return "";
    const groups = courses(state.entries);
    if (!groups.length) return "";
    const study = studyOf(state), open = new Set(study.open);
    return `<article class="gl-card gl-tt-study" aria-label="学习">${head("学习", "大纲、作业和角色的自习记录，只读")}
      <div class="gl-tt-study-list">${groups.map(({course}) => studyCourse(course, study, open, term.timezone, ctx.now)).join("")}</div></article>`;
  }

  function panel(state, ctx) {
    if (!state.panel) return "";
    const body = {term:termPanel, course:coursePanel, slot:slotPanel, session:sessionPanel}[state.panel.kind]?.(state, ctx) || "";
    return `<aside class="gl-card gl-tt-panel" aria-label="编辑">${body}</aside>`;
  }

  /* 整个课表页。state 见 timetable-page.js；ctx = {now, zone}。 */
  function render(state, ctx = {}) {
    const input = {now:ctx.now ?? Date.now(), zone:validZone(ctx.zone)};
    if (!state || state.status === "idle" || state.status === "loading")
      return `<article class="gl-card gl-tt-only" aria-busy="true">${head("课表")}<p class="gl-empty">正在读取课表…</p></article>`;
    if (state.status === "disabled")
      return `<article class="gl-card gl-tt-only">${head("课表")}${empty("pause", "课表功能还没开启",
        "需要先在插件设置里打开「学业」（academics_enabled），才能在这里录入课表。现在不能保存。")}</article>`;
    if (state.status !== "ready")
      return `<article class="gl-card gl-tt-only" role="status">${head("课表")}${empty("alert", "课表暂时读不到",
        state.code ? `错误代码：${state.code}。可以稍后再试。` : "可以稍后再试。", button("重新读取", "data-tt-reload", "gl-chip-button gl-tt-cta"))}</article>`;
    const message = state.message ? `<p class="gl-tt-message" role="status" data-tone="${state.message.tone || "ok"}">${e(state.message.text)}</p>` : "";
    return `<div class="gl-tt"${state.panel ? " data-panel" : ""}>${message}${termCard(state, input)}${weekCard(state, input)}${panel(state, input)}${courseCard(state)}${studyCard(state, input)}</div>`;
  }

  return {freshStudy, studyOf, progressOf, WEEKDAYS, isDate, isTime, addDays, isoWeekday, localParts, weekOf, windows, pageOf, teaches, weekRange, defaultWeek, defaultTerm, parseDates, parseMakeups,
    termRecord, termForm, parseWeeks, formatWeeks, courseRecord, slotRecord, slotForm, courses, entryRecord, withException, exceptionOf, command,
    placeOptions, sessionPreview, sessionTimes, reason, reminderNote, render, sessionPanel};
})();
