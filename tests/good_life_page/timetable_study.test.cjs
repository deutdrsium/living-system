"use strict";
/* 课表页的「学习」区（只读）：展开一门课时才读大纲、作业、进度、成绩与自习记录；空状态与读不到时的提示；
 * 点开一份自习记录看全文。用真实的 timetable-view.js / timetable-page.js，接口与 DOM 用替身。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..", "..");
const page = path.join(root, "plugins", "astrbot_plugin_good_life", "pages", "我会去好好生活");
globalThis.window = globalThis;
for (const file of ["icons.js", "timetable-view.js", "timetable-page.js"])
  vm.runInThisContext(fs.readFileSync(path.join(page, file), "utf8"), {filename:file});
const P = globalThis.GoodLifeTimetablePage;

const NOW = Date.parse("2026-09-08T02:00:00Z");
const weeks = Array.from({length:16}, (_, i) => new Date(Date.parse("2026-09-07T00:00:00Z") + i * 7 * 86400000).toISOString().slice(0, 10));
const TERM = {term_id:"fall", academic_year:"2026 秋", timezone:"Asia/Shanghai", start_date:"2026-09-07", end_date:"2026-12-28",
  week_starts:weeks, holidays:[], makeup_days:[], source:"manual", schema_version:1, revision:1};
const SPRING = {...TERM, term_id:"spring", academic_year:"2027 春", start_date:"2027-02-22", end_date:"2027-06-14",
  week_starts:weeks.map(day => day.replace("2026", "2027"))};
const course = (course_id, name) => ({course_id, name, kind:"required", credits:3, teacher_ref:null});
const slot = (id, c, weekday) => ({slot_id:id, term_id:"fall", course:c, weekday, start_time:"08:00", end_time:"09:40",
  weeks:[1, 2, 3], week_rule:"all", place_id:"osm:way/1", exceptions:[], source:"manual", schema_version:1, revision:2});
const MATH = course("math", "高等数学"), ART = course("art", "艺术史");
const UNITS = [
  {unit_id:"u2", term_id:"fall", course_id:"math", week:2, topic:"导数", concepts:["切线", "变化率"], materials:[], questions:[], assignment_ids:["a1"], source:"manual"},
  {unit_id:"u1", term_id:"fall", course_id:"math", week:1, topic:"极限", concepts:[], materials:[], questions:[], assignment_ids:[], source:"manual"},
];
const ASSIGNMENTS = [
  {assignment_id:"a2", term_id:"fall", course_id:"math", unit_id:null, title:"期中复习", due_at:"2026-10-30T15:59:00Z", chunk_work_s:1800, estimated_work_s:5400, source:"manual"},
  {assignment_id:"a1", term_id:"fall", course_id:"math", unit_id:"u2", title:"导数习题", due_at:"2026-09-20T15:59:00Z", chunk_work_s:1800, estimated_work_s:3600, source:"manual"},
];
const PROGRESS = [
  {assignment_id:"a1", chunk_id:"c1", chunk_index:0, due_at:"2026-09-20T15:59:00Z", required_work_s:1800, completed_work_s:1800, remaining_work_s:0, status:"completed", revision:2, provenance:"simulated", evaluation:"ok"},
  {assignment_id:"a1", chunk_id:"c2", chunk_index:1, due_at:"2026-09-20T15:59:00Z", required_work_s:1800, completed_work_s:600, remaining_work_s:1200, status:"in_progress", revision:1, provenance:"simulated", evaluation:"ok"},
];
const NOTE = {schema_version:1, generator_version:1, artifact_id:"study_n1", assignment_id:"a1", chunk_id:"c1", chunk_index:0, kind:"note",
  title:"导数笔记", revision:1, updated_at:"2026-09-12T13:05:00Z", source:"simulated", availability:"available"};
const FULL = {...NOTE, text:"切线斜率就是导数。\n<b>别忘了</b>链式法则。", next_step:"把第 3 题再做一遍。"};

const missing = () => Object.assign(new Error("route not found"), {code:"not_found"});
/* routes 里的值可以是对象、Error，或按请求体返回其一的函数；没给的学习路由按「路由不存在」处理。 */
function harness(routes = {}, {terms = [TERM]} = {}) {
  const calls = [], listeners = {};
  const element = {innerHTML:"", addEventListener(type, fn, capture) { listeners[type] = fn; if (type === "toggle") assert.equal(capture, true); },
    querySelector:() => null};
  const base = {
    "academics/places": {academics_enabled:true, classes_enabled:true, graph:null, places:[]},
    "academics/term": () => ({schema_version:1, revision:7, terms:structuredClone(terms)}),
    "academics/timetable": body => ({schema_version:1, revision:7, term_id:body.term_id,
      entries:body.term_id === "fall" ? [slot("mon", MATH, 1), slot("tue", ART, 2)] : [slot("wed", ART, 3)]}),
    "academics/occurrences": body => ({schema_version:1, revision:7, term_id:body.term_id, timezone:"Asia/Shanghai", start:body.start, end:body.end, items:[]}),
    "academics/syllabus": body => ({schema_version:1, revision:7, ...body, units:body.course_id === "math" ? UNITS : []}),
    "academics/assignments": body => ({schema_version:1, revision:7, ...body, assignments:body.course_id === "math" ? ASSIGNMENTS : []}),
    "academics/study_progress": body => ({schema_version:1, revision:7, ...body, items:body.course_id === "math" ? PROGRESS : []}),
    "academics/grades": body => ({schema_version:1, revision:7, ...body, grades:[]}),
  };
  async function api(method, route, body) {
    assert.equal(method, "GET", "学习区只读");
    calls.push({route, body:structuredClone(body)});
    const handler = Object.hasOwn(routes, route) ? routes[route] : base[route];
    if (handler === undefined) throw missing();
    const value = typeof handler === "function" ? handler(body, route) : handler;
    if (value instanceof Error) throw value;
    return structuredClone(value);
  }
  const controller = P.create({root:element, api, scope:() => "role-a", zone:() => "Asia/Shanghai", now:() => NOW, random:() => "0001"});
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
  const target = attrs => ({closest:selector => {
    const name = /^\[([^\]=]+)\]$/.exec(selector)?.[1];
    return name && Object.hasOwn(attrs, name) ? {getAttribute:key => attrs[key]} : null;
  }, matches:() => false});
  return {calls, element, controller, settle,
    reads:() => calls.map(call => call.route), listen:(type, event) => listeners[type](event),
    toggle: async (key, open = true) => { listeners.toggle({target:{open, getAttribute:name => name === "data-tt-open" ? key : null}}); await settle(); },
    click: async attrs => { listeners.click({target:target(attrs)}); await settle(); },
    change: async (attrs, value) => { listeners.change({target:{value, matches:selector => selector === "[data-tt-term]"}}); await settle(); }};
}
const BASE_READS = ["academics/places", "academics/term", "academics/timetable", "academics/occurrences"];
const html = h => h.element.innerHTML;
const count = (text, needle) => text.split(needle).length - 1;

test("study data loads only when a course is expanded, once per course, and grades once per term", async () => {
  const h = harness({"academics/study_artifacts": body => ({schema_version:1, items:body.assignment_id === "a1" ? [NOTE] : [], next_before:null})});
  h.controller.show(); await h.settle();
  assert.deepEqual(h.reads(), BASE_READS, "没展开时不读学习数据");
  assert.match(html(h), /<details class="gl-tt-study-course" data-tt-open="course:math">/);
  assert.match(html(h), /<strong>高等数学<\/strong><small>展开看大纲、作业和自习记录<\/small>/, "标题用课程名称");

  await h.toggle("course:math");
  assert.deepEqual(h.reads().slice(4).sort(), ["academics/assignments", "academics/grades", "academics/study_artifacts", "academics/study_artifacts",
    "academics/study_progress", "academics/syllabus"]);
  assert.deepEqual(h.calls.find(call => call.route === "academics/syllabus").body, {term_id:"fall", course_id:"math"});
  assert.deepEqual(h.calls.find(call => call.route === "academics/grades").body, {term_id:"fall"});
  assert.deepEqual(h.calls.filter(call => call.route === "academics/study_artifacts").map(call => call.body).sort((a, b) => a.assignment_id.localeCompare(b.assignment_id)),
    [{assignment_id:"a1", limit:20}, {assignment_id:"a2", limit:20}]);
  const out = html(h);
  assert.match(out, /data-tt-open="course:math" open>/, "重绘后保持展开");
  assert.match(out, /2 项作业 · 最近截止 9月20日 23:59/);
  assert.ok(out.indexOf("导数习题") < out.indexOf("期中复习"), "作业按截止时间排");
  assert.match(out, /截止 9月20日 周日 23:59 · 第 2 周 · 导数/);
  assert.match(out, /已做 40 分钟 \/ 1 小时 · 1\/2 段完成/);
  assert.match(out, /--tt-done:67%/);
  assert.match(out, /进行中/);
  assert.match(out, /预计 1 小时 30 分，还没开始做。/, "没有分段时显示预计用时");
  assert.match(out, /data-tt-artifact="study_n1"[^>]*aria-expanded="false"/);
  assert.match(out, /<strong>导数笔记<\/strong><small>笔记 · 9月12日 21:05 更新<\/small>/);
  assert.equal(count(out, "还没有自习记录。"), 1, "没有记录的那项作业单独说一句");
  assert.match(out, /大纲 · 2 个单元/);
  assert.ok(out.indexOf("第 1 周") < out.indexOf("第 2 周"), "大纲按周排");
  assert.match(out, /切线、变化率/);
  assert.match(out, /成绩还没发布。/);

  /* 收起、再展开、其他重绘：不再重读。另一门课只读它自己的，不再读成绩。 */
  await h.toggle("course:math", false);
  h.controller.render();
  assert.doesNotMatch(html(h), /data-tt-open="course:math" open/);
  await h.toggle("course:math");
  await h.click({"data-tt-week":"2"});
  assert.match(html(h), /data-tt-open="course:math" open>/, "翻周后仍展开");
  const before = h.reads().length;
  await h.toggle("syllabus:math");
  h.controller.render();
  assert.match(html(h), /data-tt-open="syllabus:math" open>/);
  await h.toggle("course:art");
  const later = h.reads().slice(before);
  assert.deepEqual(later.sort(), ["academics/assignments", "academics/study_progress", "academics/syllabus"]);
  assert.equal(h.reads().filter(route => route === "academics/syllabus").length, 2);
});

test("a course with nothing yet shows one calm line per part", async () => {
  const h = harness({"academics/study_artifacts": {schema_version:1, items:[], next_before:null},
    "academics/study_progress": body => ({schema_version:1, revision:7, ...body, items:[]})});
  h.controller.show(); await h.settle();
  await h.toggle("course:art");
  let out = html(h);
  assert.match(out, /还没有作业<\/small>/);
  assert.match(out, /这门课还没有作业。/);
  assert.match(out, /这门课还没有录入大纲。/);
  assert.match(out, /成绩还没发布。/);
  /* 有作业、但都还没有自习记录：在列表下面只说一次。 */
  await h.toggle("course:math");
  out = html(h);
  assert.equal(count(out, "还没有自习记录，上过课、开始写作业后会出现在这里。"), 1);
  assert.doesNotMatch(out, /还没有自习记录。/);
  assert.equal(count(out, "还没开始做。"), 2);
});

test("opening an artifact reads its full text; clicking again or 收起 closes it", async () => {
  const h = harness({"academics/study_artifacts": body => ({schema_version:1, items:body.assignment_id === "a1" ? [NOTE] : [], next_before:null}),
    "academics/study_artifacts/study_n1": FULL});
  h.controller.show(); await h.settle();
  await h.toggle("course:math");
  await h.click({"data-tt-artifact":"study_n1"});
  assert.deepEqual(h.calls.at(-1), {route:"academics/study_artifacts/study_n1", body:{}});
  let out = html(h);
  assert.match(out, /data-tt-artifact="study_n1"[^>]*aria-expanded="true"/);
  assert.match(out, /切线斜率就是导数。\n&lt;b&gt;别忘了&lt;\/b&gt;链式法则。/, "全文原样显示并转义");
  assert.match(out, /<strong>下一步<\/strong><span>把第 3 题再做一遍。<\/span>/);
  await h.click({"data-tt-artifact-close":""});
  assert.doesNotMatch(html(h), /gl-tt-artifact-open/);
  await h.click({"data-tt-artifact":"study_n1"});
  assert.match(html(h), /gl-tt-artifact-open/);
  await h.click({"data-tt-artifact":"study_n1"});
  out = html(h);
  assert.doesNotMatch(out, /gl-tt-artifact-open/, "再点一次收起");
  assert.match(out, /data-tt-open="course:math" open>/);
});

test("blocked artifacts, paging, released grades", async () => {
  const pages = {undefined:{schema_version:1, items:[NOTE], next_before:"study_n1"},
    study_n1:{schema_version:1, items:[{...NOTE, artifact_id:"study_n0", title:"旧笔记", kind:"draft"}], next_before:null}};
  const h = harness({"academics/study_artifacts": body => body.assignment_id === "a1" ? pages[body.before] : {schema_version:1, items:[], next_before:null},
    "academics/study_artifacts/study_n1": {...NOTE, availability:"blocked", text:null, next_step:null},
    "academics/grades": {schema_version:1, revision:7, term_id:"fall", grades:[{term_id:"fall", course_id:"math", course_name:"高等数学",
      assignment_id:"a2", assessment_at:"2026-12-20T02:00:00Z", release_at:"2026-12-25T02:00:00Z", score:88, letter:"A-", rule_version:1,
      provenance:"simulated", components:{attendance:90, review:85, luck:2}, weights:{attendance:0.4, review:0.5, luck:0.1}}]}});
  h.controller.show(); await h.settle();
  await h.toggle("course:math");
  let out = html(h);
  assert.match(out, /<span class="gl-chip is-ok">88 · A-<\/span>/, "标题行显示已发布的成绩");
  assert.match(out, /<span>成绩<\/span><strong>88<\/strong><em>A-<\/em><small>12月25日发布<\/small>/);
  assert.match(out, /data-tt-artifacts-more="a1"/);
  await h.click({"data-tt-artifacts-more":"a1"});
  assert.deepEqual(h.calls.at(-1).body, {assignment_id:"a1", limit:20, before:"study_n1"});
  out = html(h);
  assert.match(out, /旧笔记<\/strong><small>作业稿/);
  assert.doesNotMatch(out, /data-tt-artifacts-more/);
  await h.click({"data-tt-artifact":"study_n1"});
  assert.match(html(h), /这份记录用到的材料现在不能读，暂时看不到全文。/);
});

test("missing or disabled routes give calm messages instead of breaking the page", async () => {
  /* study_artifacts 没有注册（学习未开启）：整门课只说一次，作业照常显示。 */
  let h = harness();
  h.controller.show(); await h.settle();
  await h.toggle("course:math");
  let out = html(h);
  assert.match(out, /导数习题/);
  assert.equal(count(out, "自习记录暂时看不到，可以稍后再试。"), 1);
  assert.match(out, /data-tt-open="course:math" open>/);

  /* 大纲和作业都读不到：一句话加重新读取；重新读取成功后正常显示。 */
  let fail = true;
  const off = Object.assign(new Error("academics is disabled"), {code:"missing_dependency"});
  h = harness({"academics/syllabus": body => fail ? off : {schema_version:1, revision:7, ...body, units:[]},
    "academics/assignments": body => fail ? off : {schema_version:1, revision:7, ...body, assignments:[]},
    "academics/study_progress": off, "academics/grades": off});
  h.controller.show(); await h.settle();
  await h.toggle("course:art");
  out = html(h);
  assert.match(out, /这门课的大纲和作业还没开启。/);
  assert.match(out, /data-tt-study-retry="art"/);
  fail = false;
  await h.click({"data-tt-study-retry":"art"});
  out = html(h);
  assert.match(out, /这门课还没有作业。/);
  assert.match(out, /成绩还没开启。/);
});

test("switching term drops the expanded state and cached study data", async () => {
  const h = harness({"academics/study_artifacts": {schema_version:1, items:[], next_before:null}}, {terms:[TERM, SPRING]});
  h.controller.show(); await h.settle();
  await h.toggle("course:math");
  assert.match(html(h), /data-tt-open="course:math" open>/);
  await h.change({}, "spring");
  assert.equal(h.controller.state.termId, "spring");
  assert.doesNotMatch(html(h), / open>/);
  const before = h.reads().length;
  await h.toggle("course:art");
  assert.ok(h.calls.slice(before).some(call => call.route === "academics/grades" && call.body.term_id === "spring"), "新学期重新读成绩");
  h.controller.reset();
  assert.equal(h.controller.state.study, null);
});

test("clicking a <summary> records the new state before the browser's late toggle event", async () => {
  const h = harness({"academics/study_artifacts": {schema_version:1, items:[], next_before:null}});
  h.controller.show(); await h.settle();
  const details = {open:false, getAttribute:name => name === "data-tt-open" ? "course:math" : null};
  const summary = {closest:selector => selector === "summary" ? {parentElement:details} : null};
  h.listen("click", {target:summary});
  assert.deepEqual(h.controller.state.study.open, ["course:math"], "点下去就记下展开");
  await h.settle();
  const reads = h.reads().length;
  assert.match(html(h), /data-tt-open="course:math" open>/);
  /* 浏览器随后发来的 toggle（不管落在新元素还是旧元素上）都不再重复读取。 */
  details.open = true;
  await h.toggle("course:math");
  assert.equal(h.reads().length, reads);
  /* 再点一次收起。 */
  h.listen("click", {target:summary});
  assert.deepEqual(h.controller.state.study.open, []);
});
