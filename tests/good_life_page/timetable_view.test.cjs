"use strict";
/* 课表页纯函数（pages/我会去好好生活/timetable-view.js）：学期 / 课程 / 上课时间记录、
 * 单节课调整、命令体、说明文字与各状态的标记。由 tests/test_good_life_wallet_page.py 用 `node --test` 运行。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..", "..");
const page = path.join(root, "plugins", "astrbot_plugin_good_life", "pages", "我会去好好生活");
globalThis.window = globalThis;
for (const file of ["icons.js", "timetable-view.js"]) vm.runInThisContext(fs.readFileSync(path.join(page, file), "utf8"), {filename:file});
const T = globalThis.GoodLifeTimetable;

/* 与后端 academics_routes.TERM / ENTRY / EXCEPTION 的字段一一对应。 */
const TERM_KEYS = ["academic_year", "end_date", "holidays", "makeup_days", "source", "start_date", "term_id", "timezone", "week_starts"];
const ENTRY_KEYS = ["course", "end_time", "exceptions", "place_id", "slot_id", "source", "start_time", "term_id", "week_rule", "weekday", "weeks"];
const COURSE_KEYS = ["course_id", "credits", "kind", "name", "teacher_ref"];
const EXCEPTION_KEYS = ["cancelled", "date", "end_time", "place_id", "start_time"];
const keys = value => Object.keys(value).sort();
const ids = {term:() => "term-new", course:() => "course-new", slot:() => "slot-new"};

const TERM = T.termRecord({academic_year:"2026 秋", start_date:"2026-09-07", weeks:"16", timezone:"Asia/Shanghai",
  holidays:"2026-10-01\n2026-10-02", makeup_days:"2026-10-10 2026-10-08"}, null, ids);
const COURSE = {course_id:"math", name:"高等数学", kind:"required", credits:4, teacher_ref:null};
const SLOT = {slot_id:"math-mon", term_id:"term-new", course:COURSE, weekday:1, start_time:"08:00", end_time:"09:40",
  weeks:[1, 2, 3], week_rule:"all", place_id:"osm:way/1", exceptions:[], source:"manual"};
const SETUP = {academics_enabled:true, classes_enabled:true, graph:{graph_id:"campus", version:3},
  places:[{place_id:"osm:way/1", name:"第三教学楼", type:"university"}, {place_id:"osm:way/2", name:"图书馆", type:"library"},
    {place_id:"osm:way/3", name:"图书馆", type:"building"}]};
const NOW = Date.parse("2026-09-08T02:00:00Z"); // 周二 10:00（上海），第 1 周
const session = (over = {}) => ({session_id:"s1", slot_id:"math-mon", term_id:"term-new", course_id:"math", course_name:"高等数学",
  start_at:"2026-09-14T00:00:00Z", end_at:"2026-09-14T01:40:00Z", local_date:"2026-09-14", place_id:"osm:way/1",
  revision:1, cancelled:false, source:"manual", ...over});

test("termRecord builds the exact backend Term shape with a 7-day week map and exclusive end", () => {
  assert.deepEqual(keys(TERM), TERM_KEYS);
  assert.equal(TERM.term_id, "term-new");
  assert.equal(TERM.source, "manual");
  assert.equal(TERM.week_starts.length, 16);
  assert.deepEqual(TERM.week_starts.slice(0, 3), ["2026-09-07", "2026-09-14", "2026-09-21"]);
  assert.equal(TERM.start_date, "2026-09-07");
  assert.equal(TERM.end_date, "2026-12-28", "16 周后的那天，不含");
  assert.deepEqual(TERM.holidays, ["2026-10-01", "2026-10-02"]);
  assert.deepEqual(TERM.makeup_days, [{date:"2026-10-10", teaching_date:"2026-10-08"}]);
});

test("termRecord keeps an irregular week map when the first week and count are unchanged", () => {
  const existing = {...TERM, term_id:"fall", week_starts:["2026-09-07", "2026-09-21"], end_date:"2026-09-28"};
  const same = T.termRecord({...T.termForm(existing), academic_year:"改名"}, existing, ids);
  assert.equal(same.term_id, "fall");
  assert.deepEqual(same.week_starts, ["2026-09-07", "2026-09-21"]);
  assert.equal(same.end_date, "2026-09-28");
  const longer = T.termRecord({...T.termForm(existing), weeks:3}, existing, ids);
  assert.deepEqual(longer.week_starts, ["2026-09-07", "2026-09-14", "2026-09-21"]);
});

test("termRecord rejects missing names, bad dates, week counts and makeup lines in Chinese", () => {
  const base = {academic_year:"x", start_date:"2026-09-07", weeks:16, timezone:"", holidays:"", makeup_days:""};
  for (const [change, message] of [[{academic_year:" "}, /学期名称/], [{start_date:"2026-02-30"}, /第一周/],
    [{weeks:0}, /1 到 53/], [{weeks:54}, /1 到 53/], [{holidays:"10月1日"}, /不是日期/], [{makeup_days:"2026-10-10"}, /补课日期 原上课日期/]])
    assert.throws(() => T.termRecord({...base, ...change}, null, ids), error => error.code === "invalid_input" && message.test(error.message));
  assert.equal(T.termRecord(base, null, ids).timezone, "Asia/Shanghai", "时区留空用默认");
});

test("parseWeeks and formatWeeks round-trip ranges and fence the term", () => {
  assert.deepEqual(T.parseWeeks("1-3, 5，8～9", 16), [1, 2, 3, 5, 8, 9]);
  assert.deepEqual(T.parseWeeks("3 1 2 2", 16), [1, 2, 3]);
  assert.equal(T.formatWeeks([1, 2, 3, 5, 8, 9]), "1-3,5,8-9");
  for (const bad of ["", "0", "17", "5-3", "单周", "1-2-3"])
    assert.throws(() => T.parseWeeks(bad, 16), error => error.code === "invalid_input", bad);
});

test("slotRecord and courseRecord build the exact backend Entry shape", () => {
  const course = T.courseRecord({name:" 高等数学 ", kind:"required", credits:"4", teacher:""}, null, ids);
  assert.deepEqual(keys(course), COURSE_KEYS);
  assert.deepEqual(course, {course_id:"course-new", name:"高等数学", kind:"required", credits:4, teacher_ref:null});
  const slot = T.slotRecord({weekday:"3", start_time:"10:00", end_time:"11:40", weeks:"1-16", week_rule:"odd", place_id:"osm:way/2"},
    {term:TERM, course, ids});
  assert.deepEqual(keys(slot), ENTRY_KEYS);
  assert.equal(slot.slot_id, "slot-new");
  assert.equal(slot.term_id, TERM.term_id);
  assert.equal(slot.weekday, 3);
  assert.equal(slot.weeks.length, 16);
  assert.equal(slot.week_rule, "odd");
  assert.deepEqual(slot.exceptions, []);
  assert.equal(slot.source, "manual");
  const edited = T.slotRecord({...T.slotForm(SLOT), start_time:"08:10"}, {term:TERM, course:COURSE, existing:{...SLOT, exceptions:[{date:"2026-09-14", cancelled:true, start_time:null, end_time:null, place_id:null}]}, ids});
  assert.equal(edited.slot_id, "math-mon", "修改沿用原编号");
  assert.equal(edited.exceptions.length, 1, "修改上课时间不丢掉单次调整");
  for (const [change, message] of [[{end_time:"07:00"}, /晚于/], [{place_id:""}, /地点/], [{weeks:"20"}, /1 到 16/], [{weekday:"8"}, /星期/]])
    assert.throws(() => T.slotRecord({...T.slotForm(SLOT), ...change}, {term:TERM, course:COURSE, ids}), error => message.test(error.message));
  assert.throws(() => T.courseRecord({name:"x", credits:"101"}, null, ids), /学分/);
  assert.equal(T.courseRecord({name:"x", kind:"elective", credits:"", teacher:"王老师"}, COURSE, ids).course_id, "math");
});

test("editing a slot keeps only adjustments that still fall on one of its teaching dates", () => {
  const ex = date => ({date, cancelled:true, start_time:null, end_time:null, place_id:null});
  /* 周四的课：10-08（周四）调休到 10-10（周六）上；10-15 是第 6 周周四；10-01 放假。 */
  const thursday = {...SLOT, weekday:4, weeks:Array.from({length:16}, (_, i) => i + 1),
    exceptions:[ex("2026-10-10"), ex("2026-10-08"), ex("2026-10-15"), ex("2026-10-01")]};
  const kept = T.slotRecord(T.slotForm(thursday), {term:TERM, course:COURSE, existing:thursday, ids});
  assert.deepEqual(kept.exceptions.map(item => item.date), ["2026-10-10", "2026-10-15"]);
  const moved = T.slotRecord({...T.slotForm(thursday), weekday:"1"}, {term:TERM, course:COURSE, existing:thursday, ids});
  assert.deepEqual(moved.exceptions, [], "换到周一后原来周四的调整都不适用");
  const odd = T.slotRecord({...T.slotForm(thursday), week_rule:"odd"}, {term:TERM, course:COURSE, existing:thursday, ids});
  assert.deepEqual(odd.exceptions.map(item => item.date), ["2026-10-10"], "10-08 在第 5 周（单周），10-15 在第 6 周（双周）");
});

test("withException writes one exception per date, nulls unchanged fields and restores when nothing differs", () => {
  const moved = T.withException(SLOT, "2026-09-14", {start_time:"10:00", end_time:"11:40", place_id:"osm:way/1"});
  assert.deepEqual(moved.exceptions, [{date:"2026-09-14", cancelled:false, start_time:"10:00", end_time:"11:40", place_id:null}]);
  assert.deepEqual(keys(moved.exceptions[0]), EXCEPTION_KEYS);
  assert.deepEqual(SLOT.exceptions, [], "不改原记录");
  const room = T.withException(moved, "2026-09-14", {start_time:"08:00", end_time:"09:40", place_id:"osm:way/2"});
  assert.deepEqual(room.exceptions, [{date:"2026-09-14", cancelled:false, start_time:null, end_time:null, place_id:"osm:way/2"}]);
  const cancelled = T.withException(room, "2026-09-21", {cancelled:true});
  assert.deepEqual(cancelled.exceptions.map(item => [item.date, item.cancelled]), [["2026-09-14", false], ["2026-09-21", true]]);
  assert.deepEqual(T.withException(cancelled, "2026-09-14", {start_time:"08:00", end_time:"09:40", place_id:"osm:way/1"}).exceptions.map(item => item.date), ["2026-09-21"]);
  assert.deepEqual(T.withException(cancelled, "2026-09-21", null).exceptions.map(item => item.date), ["2026-09-14"]);
  assert.throws(() => T.withException(SLOT, "2026-09-14", {start_time:"10:00", end_time:"09:00", place_id:"osm:way/1"}), /晚于/);
});

test("records built from read entries drop read-only metadata the command boundary rejects", () => {
  const read = {...SLOT, schema_version:1, revision:9, course:{...COURSE, internal:"x"},
    exceptions:[{date:"2026-09-14", cancelled:true, start_time:null, end_time:null, place_id:null, internal:"x"}]};
  for (const record of [T.entryRecord(read), T.withException(read, "2026-09-21", {cancelled:true}),
    T.slotRecord(T.slotForm(read), {term:TERM, course:COURSE, existing:read, ids})]) {
    assert.deepEqual(keys(record), ENTRY_KEYS);
    assert.deepEqual(keys(record.course), COURSE_KEYS);
    for (const item of record.exceptions) assert.deepEqual(keys(item), EXCEPTION_KEYS);
  }
  assert.equal(T.entryRecord(read, {...COURSE, course_id:"other"}).course.course_id, "other");
});

test("command bodies match the academics command whitelist", () => {
  assert.deepEqual(T.command("create", "term", "t", TERM, 4, "page-1"),
    {command_id:"page-1", expected_revision:4, action:"create", kind:"term", id:"t", record:TERM});
  assert.deepEqual(T.command("delete", "timetable", "s", SLOT, 5, "page-2"),
    {command_id:"page-2", expected_revision:5, action:"delete", kind:"timetable", id:"s"});
});

test("week helpers find the teaching week and clamp the range to the term end", () => {
  assert.equal(T.weekOf(TERM, "2026-09-07"), 1);
  assert.equal(T.weekOf(TERM, "2026-09-20"), 2);
  assert.equal(T.weekOf(TERM, "2026-09-06"), 0);
  assert.equal(T.defaultWeek(TERM, "2026-08-01"), 1);
  assert.equal(T.defaultWeek(TERM, "2027-02-01"), 16);
  assert.deepEqual(T.weekRange(TERM, 1), {start:"2026-09-07", end:"2026-09-14", week:1});
  assert.deepEqual(T.weekRange({...TERM, end_date:"2026-09-10"}, 1), {start:"2026-09-07", end:"2026-09-10", week:1});
  const spring = {...TERM, term_id:"spring", start_date:"2027-02-20", end_date:"2027-07-01"};
  assert.equal(T.defaultTerm([TERM, spring], "2026-10-01").term_id, TERM.term_id);
  assert.equal(T.defaultTerm([TERM, spring], "2027-08-01").term_id, "spring");
});

/* 第一周之前、两个教学周之间、最后一周之后都有日子。 */
const GAPPY = {...TERM, term_id:"gappy", start_date:"2026-09-01", end_date:"2026-10-03", week_starts:["2026-09-07", "2026-09-21"]};

test("windows page through every term date, including gaps before, between and after teaching weeks", () => {
  const pages = T.windows(GAPPY);
  assert.deepEqual(pages, [
    {start:"2026-09-01", end:"2026-09-07", week:0}, {start:"2026-09-07", end:"2026-09-14", week:1},
    {start:"2026-09-14", end:"2026-09-21", week:0}, {start:"2026-09-21", end:"2026-09-28", week:2},
    {start:"2026-09-28", end:"2026-10-03", week:0}]);
  for (let day = GAPPY.start_date; day < GAPPY.end_date; day = T.addDays(day, 1))
    assert.equal(pages.filter(item => item.start <= day && day < item.end).length, 1, day + " 恰好在一页里");
  assert.equal(T.pageOf(GAPPY, "2026-09-16"), 3);
  assert.equal(T.defaultWeek(GAPPY, "2026-09-16"), 3, "空档里的今天也能直接看到");
  assert.equal(T.defaultWeek(GAPPY, "2026-08-01"), 2, "学期没开始时显示第一个教学周");
});

test("termRecord keeps a stored start date earlier than the unchanged first teaching week", () => {
  const existing = {...GAPPY, term_id:"fall"};
  assert.equal(T.termRecord({...T.termForm(existing), academic_year:"改名"}, existing, ids).start_date, "2026-09-01");
  assert.equal(T.termRecord({...T.termForm(existing), weeks:3}, existing, ids).start_date, "2026-09-01", "只改周数也保留");
  assert.equal(T.termRecord({...T.termForm(existing), start_date:"2026-09-14"}, existing, ids).start_date, "2026-09-14", "改了第一周就跟着改");
});

test("placeOptions filters by name or type and labels duplicate names", () => {
  assert.deepEqual(T.placeOptions(SETUP).map(item => item.label), ["第三教学楼", "图书馆（library）", "图书馆（building）"]);
  assert.deepEqual(T.placeOptions(SETUP, "教学").map(item => item.value), ["osm:way/1"]);
  assert.deepEqual(T.placeOptions(SETUP, "LIBRARY").map(item => item.value), ["osm:way/2"]);
});

test("sessionPreview says plainly what will happen to the class and its reminder", () => {
  const s = session();
  assert.equal(T.sessionPreview(s, SLOT, {start_time:"10:00", end_time:"11:40", place_id:"osm:way/2"}, SETUP, "Asia/Shanghai"),
    "这节课改到 10:00–11:40，图书馆，原来的提醒会作废。");
  assert.equal(T.sessionPreview(s, SLOT, {start_time:"08:00", end_time:"09:40", place_id:"osm:way/1"}, SETUP, "Asia/Shanghai"), "还没有改动。");
  assert.equal(T.sessionPreview(s, SLOT, {cancelled:true}, SETUP, "Asia/Shanghai"), "9月14日这节课会停上，原来的提醒会作废。");
  const off = {...SETUP, classes_enabled:false};
  assert.match(T.sessionPreview(s, SLOT, {start_time:"10:00", end_time:"11:40", place_id:"osm:way/1"}, off, "Asia/Shanghai"), /上课功能还没开，暂时没有提醒/);
  assert.match(T.sessionPreview(s, SLOT, {start_time:"10:00", end_time:"11:40", place_id:"osm:way/1"}, null, "Asia/Shanghai"), /原来的提醒会作废/, "读不到开关时按会提醒说");
  assert.match(T.sessionPreview({...s, cancelled:true}, SLOT, {start_time:"08:00", end_time:"09:40", place_id:"osm:way/1"}, SETUP, "Asia/Shanghai"), /^这节课恢复上课/);
});

test("reason maps backend failures to short Chinese and keeps unknown text", () => {
  assert.equal(T.reason({code:"stale_version", message:"academic revision changed"}), "数据刚有变化，已重新读取，请再确认一次。");
  assert.equal(T.reason({message:"timetable slots overlap"}), "和别的课时间冲突了。");
  assert.equal(T.reason({message:"delete the term's timetable slots first"}), "这个学期里还有课程，请先删掉课程。");
  assert.equal(T.reason({message:"something new"}), "没有保存：something new");
});

const ready = over => ({status:"ready", setup:SETUP, revision:3, terms:[TERM], termId:TERM.term_id, entries:[SLOT], week:2,
  occurrences:{key:"k", status:"ok", items:[session()]}, panel:null, draft:null, placeFilter:"", confirm:null, busy:false, message:null, ...over});

test("render shows the disabled, loading and error states", () => {
  assert.match(T.render({status:"loading"}), /正在读取课表/);
  const disabled = T.render({status:"disabled"});
  assert.match(disabled, /课表功能还没开启/);
  assert.match(disabled, /现在不能保存/);
  assert.doesNotMatch(disabled, /data-tt-new-term/);
  assert.match(T.render({status:"error", code:"life_dashboard_invalid"}), /错误代码：life_dashboard_invalid[\s\S]*data-tt-reload/);
});

test("render without a term invites creating one and notes when classes are off", () => {
  const html = T.render(ready({terms:[], termId:null, entries:null, occurrences:null, setup:{...SETUP, classes_enabled:false}}), {now:NOW});
  assert.match(html, /还没有学期/);
  assert.match(html, /data-tt-new-term/);
  assert.match(html, /上课功能还没开启。课表可以先录好/);
  assert.doesNotMatch(T.render(ready({terms:[], termId:null}), {now:NOW}), /上课功能还没开启/);
});

test("render draws the weekly grid with positioned, labelled blocks and the course list", () => {
  const changed = {...SLOT, exceptions:[{date:"2026-09-14", cancelled:false, start_time:"10:00", end_time:"11:40", place_id:null}]};
  const html = T.render(ready({entries:[changed], occurrences:{key:"k", status:"ok", items:[
    session({start_at:"2026-09-14T02:00:00Z", end_at:"2026-09-14T03:40:00Z"}),
    session({session_id:"s2", local_date:"2026-09-16", start_at:"2026-09-16T00:00:00Z", end_at:"2026-09-16T01:40:00Z", cancelled:true})]}}), {now:NOW});
  assert.match(html, /第 2 周/);
  assert.match(html, /9月14日–9月20日/);
  assert.match(html, /data-tt-week="1"/);
  assert.match(html, /回到本周/);
  assert.equal((html.match(/class="gl-tt-day"/g) || []).length, 7);
  assert.match(html, /data-state="changed" data-tt-session="s1"[\s\S]*?--tt-top:20\.000%;--tt-h:16\.667%/, "10:00–11:40 落在 08:00–18:00 的格子里");
  assert.match(html, /aria-label="高等数学，10:00–11:40，第三教学楼，已调整"/);
  assert.match(html, /data-state="cancelled" data-tt-session="s2"/);
  assert.match(html, /这周 1 节课/);
  assert.match(html, /周一 08:00–09:40[\s\S]*第三教学楼 · 第 1–3 周 · 1 次单独调整/);
  assert.match(html, /2026 秋/);
  assert.match(html, /放假 2 天/);
});

test("render escapes course names and place labels", () => {
  const evil = {...SLOT, course:{...COURSE, name:"<img src=x onerror=alert(1)>"}};
  const html = T.render(ready({entries:[evil], occurrences:{key:"k", status:"ok", items:[session({course_name:"<b>x</b>"})]},
    setup:{...SETUP, places:[{place_id:"osm:way/1", name:'"><script>', type:"x"}]}}), {now:NOW});
  assert.doesNotMatch(html, /<img|<script|<b>x/);
  assert.match(html, /&lt;img src=x/);
});

test("session panel offers adjust, cancel and restore, and locks classes that already started", () => {
  const exception = {...SLOT, exceptions:[{date:"2026-09-14", cancelled:false, start_time:"10:00", end_time:"11:40", place_id:null}]};
  const html = T.render(ready({entries:[exception], panel:{kind:"session", sessionId:"s1"}}), {now:NOW});
  assert.match(html, /data-tt-form="session"/);
  assert.match(html, /只改这一天/);
  assert.match(html, /data-tt-session-cancel/);
  assert.match(html, /data-tt-session-restore/);
  assert.match(html, /data-tt-preview/);
  assert.match(html, /<option value="osm:way\/1" selected>第三教学楼<\/option>/);
  const past = T.render(ready({panel:{kind:"session", sessionId:"s1"}}), {now:Date.parse("2026-09-14T00:30:00Z")});
  assert.match(past, /已经开始或上过了，不能再改/);
  assert.doesNotMatch(past, /data-tt-form="session"/);
  const orphan = T.render(ready({entries:[], panel:{kind:"session", sessionId:"s1"}}), {now:NOW});
  assert.match(orphan, /上课时间已经删掉了/);
});

test("a gap page between teaching weeks is labelled and can be paged to and from", () => {
  const html = T.render(ready({terms:[GAPPY], termId:"gappy", entries:[], week:3, occurrences:{key:"k", status:"ok", items:[]}}), {now:NOW});
  assert.match(html, /非教学周/);
  assert.match(html, /9月14日–9月20日/);
  assert.match(html, /data-tt-week="2"/);
  assert.match(html, /data-tt-week="4"/);
});

test("a session whose date the current slot schedule no longer teaches is read-only", () => {
  const html = T.render(ready({entries:[{...SLOT, weekday:3}], panel:{kind:"session", sessionId:"s1"}}), {now:NOW});
  assert.match(html, /这节课已经不在现在的上课安排里，只留作记录/);
  assert.doesNotMatch(html, /data-tt-form="session"|data-tt-session-cancel|data-tt-session-restore/);
  const weeks = T.render(ready({entries:[{...SLOT, weeks:[1, 3]}], panel:{kind:"session", sessionId:"s1"}}), {now:NOW});
  assert.match(weeks, /只留作记录/, "第 2 周不再上");
  assert.match(T.render(ready({panel:{kind:"session", sessionId:"s1"}}), {now:NOW}), /data-tt-form="session"/, "仍在安排里就能改");
});

test("forms explain consequences and fall back to a typed place when no campus graph is confirmed", () => {
  const slot = T.render(ready({panel:{kind:"slot", slotId:"math-mon"}}), {now:NOW});
  assert.match(slot, /以后的每节都按新安排；已经上过的不变，原来的提醒会作废/);
  const noGraph = T.render(ready({setup:{...SETUP, graph:null, places:[]}, panel:{kind:"course"}}), {now:NOW});
  assert.match(noGraph, /还没有确认的校园地图，只能手填地点代号/);
  assert.match(noGraph, /<input name="place_id"/);
  const unknown = T.render(ready({entries:[{...SLOT, place_id:"old-room"}], panel:{kind:"slot", slotId:"math-mon"}}), {now:NOW});
  assert.match(unknown, /<option value="old-room" selected>当前：old-room<\/option>/);
  const confirm = T.render(ready({confirm:"course:math"}), {now:NOW});
  assert.match(confirm, /删除后，这门课以后的课都会取消；已经上过的会留下记录，原来的提醒会作废/);
  assert.match(confirm, /data-tt-delete-course="math"/);
  const term = T.render(ready({panel:{kind:"term", termId:TERM.term_id}}), {now:NOW});
  assert.match(term, /value="16"/);
  assert.match(term, /2026-10-10 2026-10-08/);
});
