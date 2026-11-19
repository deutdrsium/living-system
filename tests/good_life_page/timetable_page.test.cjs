"use strict";
/* 课表页控制器（pages/我会去好好生活/timetable-page.js）：读取顺序、命令体与 revision 接力、
 * 过期版本与开关关闭时的处理、换角色后旧响应作废。用真实的 timetable-view.js，接口与 DOM 用替身。 */
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
const COURSE = {course_id:"math", name:"高等数学", kind:"required", credits:4, teacher_ref:null};
const slot = (id, weekday, over = {}) => ({slot_id:id, term_id:"fall", course:COURSE, weekday, start_time:"08:00", end_time:"09:40",
  weeks:[1, 2, 3], week_rule:"all", place_id:"osm:way/1", exceptions:[], source:"manual", schema_version:1, revision:2, ...over});
const SESSION = {session_id:"s-next", slot_id:"mon", term_id:"fall", course_id:"math", course_name:"高等数学",
  start_at:"2026-09-14T00:00:00Z", end_at:"2026-09-14T01:40:00Z", local_date:"2026-09-14", place_id:"osm:way/1",
  revision:1, cancelled:false, source:"manual"};
const PLACES = {academics_enabled:true, classes_enabled:false, graph:{graph_id:"campus", version:1},
  places:[{place_id:"osm:way/1", name:"第三教学楼", type:"university"}, {place_id:"osm:way/2", name:"图书馆", type:"library"}]};

const COMMAND_KEYS = ["action", "command_id", "expected_revision", "id", "kind", "record"];
const TERM_KEYS = ["academic_year", "end_date", "holidays", "makeup_days", "source", "start_date", "term_id", "timezone", "week_starts"];
const ENTRY_KEYS = ["course", "end_time", "exceptions", "place_id", "slot_id", "source", "start_time", "term_id", "week_rule", "weekday", "weeks"];
const COURSE_KEYS = ["course_id", "credits", "kind", "name", "teacher_ref"];
const EXCEPTION_KEYS = ["cancelled", "date", "end_time", "place_id", "start_time"];
const COURSE_UPDATE_KEYS = ["course_id", "credits", "kind", "name", "teacher_ref", "term_id"];

/* 替身：按路径返回固定读取结果，命令按给定的回执或错误依次返回。
 * receipts 里的函数在命令到达时调用，返回的 Promise 决定回执何时到；timetableRevisions 依次覆盖课程读取的 revision。 */
function harness({terms = [TERM], entries = [slot("mon", 1)], items = [SESSION], places = PLACES, termError, receipts = [],
  timetableRevisions = [], scope = () => "role-a"} = {}) {
  const calls = [], listeners = {};
  let revision = 7;
  const element = {innerHTML:"", addEventListener(type, fn) { listeners[type] = fn; }, querySelector:() => null};
  async function api(method, route, body, pinned) {
    if (method !== "POST") calls.push({method, route, body:structuredClone(body)});
    if (method === "POST") {
      /* 与宿主 academics_routes._command 一样逐层比对字段，多一个少一个都拒绝。 */
      assert.deepEqual(Object.keys(body).sort(), body.action === "delete" ? COMMAND_KEYS.filter(key => key !== "record") : COMMAND_KEYS);
      if (body.record) {
        assert.deepEqual(Object.keys(body.record).sort(), {term:TERM_KEYS, course:COURSE_UPDATE_KEYS}[body.kind] || ENTRY_KEYS);
        if (body.kind === "timetable") {
          assert.deepEqual(Object.keys(body.record.course).sort(), COURSE_KEYS);
          for (const item of body.record.exceptions) assert.deepEqual(Object.keys(item).sort(), EXCEPTION_KEYS);
        }
      }
      calls.push({method, route, body:structuredClone(body), scope:pinned});
      let next = receipts.shift();
      if (typeof next === "function") next = await next(body);
      if (next instanceof Error) throw next;
      revision = body.expected_revision + 1;
      if (body.kind === "term" && body.action === "create") terms.push({...body.record, schema_version:1, revision});
      return {schema_version:1, revision, kind:body.kind, id:body.id, deleted:body.action === "delete", ...(next || {})};
    }
    if (route === "academics/places") return structuredClone(places);
    if (route === "academics/term") { if (termError) throw termError; return {schema_version:1, revision, terms:structuredClone(terms)}; }
    if (route === "academics/timetable") return {schema_version:1, revision:timetableRevisions.length ? timetableRevisions.shift() : revision, term_id:body.term_id, entries:structuredClone(entries)};
    if (route === "academics/occurrences") return {schema_version:1, revision, term_id:body.term_id, timezone:"Asia/Shanghai", start:body.start, end:body.end, items:structuredClone(items)};
    throw new Error("unexpected " + route);
  }
  let counter = 0;
  const controller = P.create({root:element, api, scope, zone:() => "Asia/Shanghai", now:() => NOW, random:() => String(++counter).padStart(4, "0")});
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
  const target = attrs => ({closest:selector => {
    const name = /^\[([^\]=]+)\]$/.exec(selector)?.[1];
    return name && Object.hasOwn(attrs, name) ? {getAttribute:key => attrs[key]} : null;
  }, matches:() => false});
  const form = (kind, fields) => ({dataset:{ttForm:kind}, elements:Object.entries(fields).map(([name, value]) => ({name, value}))});
  return {calls, element, controller, settle, listen:(type, event) => listeners[type](event),
    click: async attrs => { listeners.click({target:target(attrs)}); await settle(); },
    submit: async (kind, fields) => {
      const f = form(kind, fields);
      let prevented = false;
      listeners.submit({target:{closest:() => f}, preventDefault() { prevented = true; }});
      await settle();
      assert.ok(prevented, "提交被页面接管");
    },
    posts:() => calls.filter(call => call.method === "POST").map(call => call.body),
    scopes:() => calls.filter(call => call.method === "POST").map(call => call.scope),
    reads:() => calls.filter(call => call.method === "GET").map(call => call.route)};
}

test("show() reads places, terms, the selected term's timetable and the current week", async () => {
  const h = harness();
  h.controller.show();
  await h.settle();
  assert.deepEqual(h.reads(), ["academics/places", "academics/term", "academics/timetable", "academics/occurrences"]);
  assert.deepEqual(h.calls[2].body, {term_id:"fall"});
  assert.deepEqual(h.calls[3].body, {term_id:"fall", start:"2026-09-07", end:"2026-09-14"}, "今天在第 1 周");
  assert.equal(h.controller.state.revision, 7);
  assert.match(h.element.innerHTML, /第 1 周/);
  assert.match(h.element.innerHTML, /上课功能还没开启/);
  await h.click({"data-tt-week":"2"});
  assert.deepEqual(h.calls.at(-1).body, {term_id:"fall", start:"2026-09-14", end:"2026-09-21"});
  assert.match(h.element.innerHTML, /data-tt-session="s-next"/);
});

test("academics disabled (missing_dependency) renders a calm read-only notice", async () => {
  const h = harness({termError:Object.assign(new Error("academics is disabled"), {code:"missing_dependency"}),
    places:{...PLACES, academics_enabled:false, classes_enabled:false}});
  h.controller.show();
  await h.settle();
  assert.equal(h.controller.state.status, "disabled");
  assert.match(h.element.innerHTML, /课表功能还没开启/);
  assert.deepEqual(h.reads(), ["academics/places", "academics/term"]);
});

test("creating a term posts a full Term record at the read revision and selects it", async () => {
  const h = harness({terms:[], entries:[]});
  h.controller.show(); await h.settle();
  assert.match(h.element.innerHTML, /还没有学期/);
  await h.click({"data-tt-new-term":""});
  assert.match(h.element.innerHTML, /data-tt-form="term"/);
  await h.submit("term", {academic_year:"2026 秋", start_date:"2026-09-07", weeks:"2", timezone:"Asia/Shanghai", holidays:"", makeup_days:""});
  assert.deepEqual(h.posts(), [{command_id:"page-0002", expected_revision:7, action:"create", kind:"term", id:"term-0001",
    record:{term_id:"term-0001", academic_year:"2026 秋", timezone:"Asia/Shanghai", start_date:"2026-09-07", end_date:"2026-09-21",
      week_starts:["2026-09-07", "2026-09-14"], holidays:[], makeup_days:[], source:"manual"}}]);
  assert.equal(h.controller.state.termId, "term-0001");
  assert.equal(h.reads().filter(route => route === "academics/term").length, 2, "保存后整页重读");
  assert.match(h.element.innerHTML, /学期已创建/);
});

test("a new course posts one timetable create with the first weekly time and place", async () => {
  const h = harness({entries:[]});
  h.controller.show(); await h.settle();
  await h.click({"data-tt-new-course":""});
  await h.submit("course", {name:"线性代数", kind:"elective", credits:"3", teacher:"王老师", weekday:"2", week_rule:"all",
    start_time:"10:00", end_time:"11:40", weeks:"1-16", place_id:"osm:way/2"});
  const [body] = h.posts();
  assert.equal(body.action, "create");
  assert.equal(body.kind, "timetable");
  assert.equal(body.expected_revision, 7);
  assert.deepEqual(body.record.course, {course_id:"course-0001", name:"线性代数", kind:"elective", credits:3, teacher_ref:"王老师"});
  assert.equal(body.record.slot_id, "slot-0002");
  assert.equal(body.id, "slot-0002");
  assert.deepEqual([body.record.weekday, body.record.start_time, body.record.end_time, body.record.place_id, body.record.weeks.length],
    [2, "10:00", "11:40", "osm:way/2", 16]);
});

test("invalid input never sends a command and explains in Chinese", async () => {
  const h = harness();
  h.controller.show(); await h.settle();
  await h.click({"data-tt-edit-slot":"mon"});
  await h.submit("slot", {weekday:"1", week_rule:"all", start_time:"10:00", end_time:"09:00", weeks:"1-3", place_id:"osm:way/1"});
  assert.deepEqual(h.posts(), []);
  assert.match(h.element.innerHTML, /下课时间要晚于上课时间/);
});

test("adjusting one session updates its slot with a dated exception", async () => {
  const h = harness();
  h.controller.show(); await h.settle();
  await h.click({"data-tt-week":"2"});
  await h.click({"data-tt-session":"s-next"});
  assert.match(h.element.innerHTML, /data-tt-form="session"/);
  await h.submit("session", {start_time:"10:00", end_time:"11:40", place_id:"osm:way/2"});
  const [body] = h.posts();
  assert.deepEqual([body.action, body.kind, body.id, body.expected_revision], ["update", "timetable", "mon", 7]);
  assert.deepEqual(body.record.exceptions, [{date:"2026-09-14", cancelled:false, start_time:"10:00", end_time:"11:40", place_id:"osm:way/2"}]);
  assert.equal(body.record.schema_version, undefined, "读取对象的 schema_version / revision 不能进命令体");
  assert.equal(body.record.revision, undefined);
  assert.match(h.element.innerHTML, /这节课已调整/);
});

test("cancelling and restoring one session", async () => {
  const exception = {date:"2026-09-14", cancelled:false, start_time:"10:00", end_time:"11:40", place_id:null};
  const h = harness({entries:[slot("mon", 1, {exceptions:[exception]})]});
  h.controller.show(); await h.settle();
  await h.click({"data-tt-week":"2"});
  await h.click({"data-tt-session":"s-next"});
  await h.click({"data-tt-session-cancel":""});
  assert.deepEqual(h.posts()[0].record.exceptions, [{date:"2026-09-14", cancelled:true, start_time:null, end_time:null, place_id:null}]);
  await h.click({"data-tt-week":"2"});
  await h.click({"data-tt-session":"s-next"});
  await h.click({"data-tt-session-restore":""});
  assert.deepEqual(h.posts()[1].record.exceptions, []);
  assert.equal(h.posts()[1].expected_revision, 8, "第二条命令用第一条回执后的 revision");
});

test("renaming a course with several weekly times sends one course update that keeps the course id", async () => {
  const h = harness({entries:[slot("mon", 1), slot("wed", 3)]});
  h.controller.show(); await h.settle();
  await h.click({"data-tt-edit-course":"math"});
  await h.submit("course", {name:"高等数学 A", kind:"required", credits:"4", teacher:""});
  assert.deepEqual(h.posts(), [{command_id:"page-0001", expected_revision:7, action:"update", kind:"course", id:"math",
    record:{term_id:"fall", course_id:"math", name:"高等数学 A", kind:"required", credits:4, teacher_ref:null}}]);
  assert.match(h.element.innerHTML, /课程已保存/);
});

test("a single-slot course is renamed with the same course update", async () => {
  const h = harness();
  h.controller.show(); await h.settle();
  await h.click({"data-tt-edit-course":"math"});
  await h.submit("course", {name:"数学分析", kind:"required", credits:"5", teacher:""});
  assert.deepEqual(h.posts().map(body => [body.action, body.kind, body.id, body.record.name, body.record.credits]),
    [["update", "course", "math", "数学分析", 5]]);
});

test("deleting a course deletes each slot with chained revisions after an inline confirm", async () => {
  const h = harness({entries:[slot("mon", 1), slot("wed", 3)]});
  h.controller.show(); await h.settle();
  await h.click({"data-tt-confirm":"course:math"});
  assert.match(h.element.innerHTML, /删除后，这门课以后的课都会取消/);
  assert.deepEqual(h.posts(), [], "只确认，还没删除");
  await h.click({"data-tt-delete-course":"math"});
  assert.deepEqual(h.posts(), [{command_id:"page-0001", expected_revision:7, action:"delete", kind:"timetable", id:"mon"},
    {command_id:"page-0002", expected_revision:8, action:"delete", kind:"timetable", id:"wed"}]);
});

test("a role switch during a multi-step save stops before the next command and never uses the new role", async () => {
  let release, role = "role-a";
  const pending = new Promise(resolve => { release = resolve; });
  const h = harness({entries:[slot("mon", 1), slot("wed", 3)], receipts:[() => pending], scope:() => role});
  h.controller.show(); await h.settle();
  await h.click({"data-tt-delete-course":"math"});
  assert.deepEqual(h.posts().map(body => body.id), ["mon"], "第一条还在路上");
  role = "role-b";
  h.controller.reset();
  release();
  await h.settle();
  assert.deepEqual(h.posts().map(body => [body.id, body.expected_revision]), [["mon", 7]], "第二条没有发出");
  assert.deepEqual(h.scopes(), ["role-a"], "已发出的那条固定用开始保存时的角色");
  assert.equal(h.controller.state.status, "idle", "旧保存的结果不写进新角色的页面");
});

test("term writes use the term read's revision and refresh both reads when the timetable read is newer", async () => {
  const h = harness({timetableRevisions:[9]});
  h.controller.show(); await h.settle();
  assert.deepEqual([h.controller.state.termRevision, h.controller.state.revision], [7, 9]);
  const form = {academic_year:"2026 秋（改）", start_date:"2026-09-07", weeks:"16", timezone:"Asia/Shanghai", holidays:"", makeup_days:""};
  await h.click({"data-tt-edit-term":"fall"});
  const before = h.reads().length;
  await h.submit("term", form);
  assert.deepEqual(h.posts(), [], "两次读取的 revision 不同，不发命令");
  assert.deepEqual(h.reads().slice(before), ["academics/places", "academics/term", "academics/timetable", "academics/occurrences"]);
  assert.match(h.element.innerHTML, /数据刚有变化，已重新读取，请再确认一次/);
  await h.click({"data-tt-edit-term":"fall"});
  await h.submit("term", form);
  assert.deepEqual(h.posts().map(body => [body.kind, body.action, body.expected_revision, body.record.academic_year]),
    [["term", "update", 7, "2026 秋（改）"]], "重读后一致，用学期读取的 revision");
});

test("a gap page between teaching weeks queries its own dates", async () => {
  const gappy = {...TERM, start_date:"2026-09-01", end_date:"2026-10-03", week_starts:["2026-09-07", "2026-09-21"]};
  const h = harness({terms:[gappy]});
  h.controller.show(); await h.settle();
  assert.deepEqual(h.calls.at(-1).body, {term_id:"fall", start:"2026-09-07", end:"2026-09-14"}, "今天在第 1 周，是第 2 页");
  await h.click({"data-tt-week":"3"});
  assert.deepEqual(h.calls.at(-1).body, {term_id:"fall", start:"2026-09-14", end:"2026-09-21"});
  assert.match(h.element.innerHTML, /非教学周/);
  await h.click({"data-tt-week":"5"});
  assert.deepEqual(h.calls.at(-1).body, {term_id:"fall", start:"2026-09-28", end:"2026-10-03"});
  await h.click({"data-tt-week":"6"});
  assert.equal(h.calls.at(-1).body.start, "2026-09-28", "超出最后一页不翻");
});

test("a term that still has courses is not deleted", async () => {
  const h = harness();
  h.controller.show(); await h.settle();
  await h.click({"data-tt-delete-term":"fall"});
  assert.deepEqual(h.posts(), []);
  assert.match(h.element.innerHTML, /请先删掉课程/);
});

test("stale_version reloads everything and asks to confirm again", async () => {
  const stale = Object.assign(new Error("academic revision changed"), {code:"stale_version"});
  const h = harness({receipts:[stale]});
  h.controller.show(); await h.settle();
  const before = h.reads().length;
  await h.click({"data-tt-edit-slot":"mon"});
  await h.submit("slot", {weekday:"1", week_rule:"all", start_time:"08:10", end_time:"09:40", weeks:"1-3", place_id:"osm:way/1"});
  assert.equal(h.posts().length, 1);
  assert.deepEqual(h.reads().slice(before), ["academics/places", "academics/term", "academics/timetable", "academics/occurrences"]);
  assert.match(h.element.innerHTML, /数据刚有变化，已重新读取，请再确认一次/);
  assert.equal(h.controller.state.busy, false);
});

test("reset() after a role switch drops responses that arrive late", async () => {
  const h = harness();
  h.controller.show();
  h.controller.reset();
  await h.settle();
  assert.equal(h.controller.state.status, "idle");
  assert.deepEqual(h.controller.state.terms, []);
  assert.match(h.element.innerHTML, /正在读取课表/);
});

test("typing in the place search filters the options and keeps what was already typed", async () => {
  const h = harness({entries:[]});
  h.controller.show(); await h.settle();
  await h.click({"data-tt-new-course":""});
  const form = {dataset:{ttForm:"course"}, elements:[{name:"name", value:"线性代数"}, {name:"place_id", value:""}]};
  const filter = {value:"图书", closest:() => form, matches:selector => selector === "[data-tt-place-filter]"};
  h.listen("input", {target:filter});
  assert.match(h.element.innerHTML, /value="线性代数"/, "已填的课程名保留");
  assert.match(h.element.innerHTML, /<option value="osm:way\/2">图书馆<\/option>/);
  assert.doesNotMatch(h.element.innerHTML, /第三教学楼/);
  const before = h.element.innerHTML;
  h.element.innerHTML = "untouched";
  h.listen("change", {target:filter});
  assert.equal(h.element.innerHTML, "untouched", "失焦的 change 不重绘");
  h.element.innerHTML = before;
});

test("values() reads named, enabled form controls only", () => {
  assert.deepEqual(P.values({elements:[{name:"a", value:"1"}, {name:"", value:"x"}, {name:"b", value:"2", disabled:true}]}), {a:"1"});
});
