"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const page = path.resolve(__dirname, "../../plugins/astrbot_plugin_good_life/pages/我会去好好生活");
const source = name => fs.readFileSync(path.join(page, name), "utf8");
const rolePath = role => `targets/t/${role}`;
const minute = 60000;

function element() {
  const listeners = new Map();
  return {innerHTML:"", textContent:"", value:"", dataset:{},
    parentElement:{toggleAttribute() {}, setAttribute() {}}, contentWindow:{postMessage() {}},
    addEventListener(type, fn) { listeners.set(type, fn); },
    fire(type, event) { return listeners.get(type)?.(event); },
    setAttribute() {}, replaceChildren() {}, append() {}, contains:() => false, querySelector:() => null};
}

async function boot(at="2026-11-18T01:00:00Z") {
  const elements = new Map(), byId = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const document = Object.assign(element(), {getElementById:byId, createElement:element, visibilityState:"visible",
    documentElement:{dataset:{}}, querySelectorAll:() => []});
  let now = Date.parse(at), timerId = 0, onMessage, connectionError;
  const timers = new Map(), reads = [], writes = [], scopes = [], statuses = [], refreshes = [], failures = [], frameMessages = [], editorApis = [], editors = [];
  const frame = byId("life-frame").contentWindow;
  frame.postMessage = message => frameMessages.push(message);
  const context = vm.createContext({document, CSS:{escape:value => value},
    Date:class extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } },
    setInterval() {}, addEventListener(type, fn) { if(type === "message")onMessage=fn; },
    setTimeout(fn, delay) { timers.set(++timerId, {fn, at:now + delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    AstrBotPluginPage:{
      ready:async () => {},
      apiGet(route, body) {
        if (route === "status") {
          if (connectionError) throw new Error(connectionError);
          return {runtime_ready:true};
        }
        if (route === "targets") return {installation_id:"i", operator_id:"o", targets:[{id:"t", name:"窗口",
          roles:[{token:"a", name:"角色 A"}, {token:"b", name:"角色 B"}]}]};
        return new Promise((resolve, reject) => reads.push({route, body, resolve, reject}));
      },
      apiPost(route, body, options) { return new Promise(resolve => writes.push({route, body, options, resolve})); },
    },
    GoodLifeIcons:{hydrate() {}, icon:() => ""},
    GoodLifeWallet:{render:({entry, zone}) => entry?.label ?? zone, query:() => ({}), normalize:data => ({status:"ok", label:data.label})}, GoodLifeWorkspace:"",
    GoodLifeTimetablePage:{create:() => ({show() {}, reset() {}, render() {}})},
  });
  function editor({api, refresh, failed}) {
    editorApis.push(api); refreshes.push(refresh); failures.push(failed);
    const instance = {dirty:false, setScope:scope => scopes.push({...scope}), setStatus(value) {
      instance.status = value;
      statuses.push(value);
    }, markup:() => "", click:() => false, invalidate() {}};
    editors.push(instance);
    return instance;
  }
  context.window = context;
  context.CompanionCalendarEditor = context.CompanionStateEditor = {create:editor};
  for (const name of ["overview.js", "calendar-view.js", "app.js"]) vm.runInContext(source(name), context, {filename:name});
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await settle();
  return {byId, reads, writes, scopes, statuses, refreshes, failures, editorApis, editors, settle, frameMessages, document,
    selectDay(date) { byId("calendar-month").fire("click", {target:{closest:selector => selector === "[data-cal-day]" ? {dataset:{calDay:date}} : null}}); },
    selectEditorPage(cursor) { byId("calendar-editor").fire("click", {target:{closest:selector => selector === "[data-state-page]" ? {dataset:{statePage:cursor}} : null}}); },
    selectPage(page) { byId("navigation").fire("click", {target:{closest:() => ({dataset:{page}})}}); },
    frameMessage(data) { return onMessage({source:frame, data:{channel:"companion-workspace", ...data}}); },
    choose(role) { byId("role").value = role; byId("role").fire("change"); },
    visibility(value) { document.visibilityState = value; document.fire("visibilitychange"); },
    failConnection(message) { connectionError=message; },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [id, timer] = next;
        timers.delete(id);
        now = timer.at;
        timer.fn();
        await settle();
      }
      now = end;
    },
  };
}

function life(tag) {
  return {activities:[{activity_id:tag, title:`活动 ${tag}`, state:"active", started_at:"2026-11-18T00:00:00Z"}]};
}
function state(tag) {
  return {state:{values:{mood:`状态 ${tag}`}}, occurrences:{items:[{id:tag, title:`计划 ${tag}`,
    start:"2026-11-18T05:00:00Z", end:"2026-11-18T06:00:00Z"}]}};
}
async function resolveSnapshot(app, index, role, tag) {
  const [lifeRead, stateRead] = app.reads.slice(index, index + 2);
  assert.deepEqual([lifeRead?.route, stateRead?.route], [`${rolePath(role)}/life`, `${rolePath(role)}/state`]);
  lifeRead.resolve(life(tag));
  stateRead.resolve(state(tag));
  await app.settle();
}
function assertSnapshot(app, tag, time) {
  assert.match(app.byId("current").innerHTML, new RegExp(`活动 ${tag}`));
  assert.match(app.byId("body-state").innerHTML, new RegExp(`状态 ${tag}`));
  assert.match(app.byId("upcoming").innerHTML, new RegExp(`计划 ${tag}`));
  assert.match(app.byId("current").innerHTML, new RegExp(`更新于 <time[^>]+>${time}</time>`));
}

test("delayed initial life and state cannot overwrite a newer role or either editor", async () => {
  const app = await boot();
  app.choose("b");
  await resolveSnapshot(app, 2, "b", "B");
  await app.advance(minute / 2);
  assert.equal(app.reads[0].route, `${rolePath("a")}/life`);
  assert.deepEqual({...app.reads[0].body}, {overview:"1"});
  app.reads[0].resolve({...life("A"), overview_activities:{items:[{activity_id:"private-purchase",
    title:"角色 A 的购买", state:"active", kind:"purchase", outcome:{state:"in_progress", source:"simulated"}}]}});
  app.reads[1].resolve(state("A"));
  await app.settle();
  assert.doesNotMatch(app.byId("current").innerHTML, /角色 A 的购买/);
  assert.match(app.byId("topbar-scope").textContent, /角色 B/);
  assertSnapshot(app, "B", "09:00");
  assert.doesNotMatch(app.byId("current").innerHTML + app.byId("body-state").innerHTML, /(?:活动|状态) A/);
  assert.deepEqual(app.scopes, Array(2).fill({installation_id:"i", operator_id:"o", path:`${rolePath("b")}/state`}));
  assert.deepEqual(app.statuses, Array(2).fill(state("B")));
});

test("a poll is fenced even when switching away and back to the same endpoint", async () => {
  const app = await boot();
  await resolveSnapshot(app, 0, "a", "first");
  await app.advance(minute);
  app.choose("b");
  app.choose("a");
  await resolveSnapshot(app, 6, "a", "return");
  app.reads[5].reject(new Error("stale state"));
  app.reads[4].reject(new Error("stale life"));
  await app.advance(minute);
  await resolveSnapshot(app, 8, "a", "fresh-poll");
  assertSnapshot(app, "fresh-poll", "09:02");
  await resolveSnapshot(app, 2, "a", "stale-poll");
  assertSnapshot(app, "fresh-poll", "09:02");
  assert.equal(app.byId("notice").textContent, "");
  assert.equal(app.statuses.length, 6, "only current-role snapshots update the clean editors");
  assert.deepEqual(app.statuses.slice(-2), [state("fresh-poll"), state("fresh-poll")]);
});

test("polling waits for both reads, then backs off after an error until a successful snapshot", async () => {
  const app = await boot();
  await resolveSnapshot(app, 0, "a", "initial");
  await app.advance(minute);
  assert.equal(app.reads.length, 4);
  app.reads[3].reject(new Error("offline"));
  await app.advance(3 * minute);
  assert.equal(app.reads.length, 4, "a failed state read must still wait for its pending life read");
  app.reads[2].resolve(life("partial"));
  await app.settle();
  assertSnapshot(app, "initial", "09:00");
  await app.advance(2 * minute - 1);
  assert.equal(app.reads.length, 4);
  await app.advance(1);
  await resolveSnapshot(app, 4, "a", "recovered");
  assertSnapshot(app, "recovered", "09:06");
  await app.advance(minute);
  assert.equal(app.reads.length, 8, "success resets the interval to one minute");
  assert.deepEqual(app.writes, []);
});

test("a hidden tab pauses polling and resumes an overdue poll when visible", async () => {
  const app = await boot();
  app.visibility("hidden");
  await resolveSnapshot(app, 0, "a", "initial");
  await app.advance(3 * minute);
  assert.equal(app.reads.length, 2);
  app.visibility("visible");
  await app.advance(0);
  assert.equal(app.reads.length, 4);
  app.visibility("hidden");
  await resolveSnapshot(app, 2, "a", "first-poll");
  await app.advance(3 * minute);
  assert.equal(app.reads.length, 4, "finishing an in-flight poll while hidden does not schedule another");
  app.visibility("visible");
  await app.advance(0);
  await resolveSnapshot(app, 4, "a", "resumed");
  assertSnapshot(app, "resumed", "09:06");
});

test("overview and schedule publish whole batches, independently of editor refresh and pagination", async () => {
  const app = await boot();
  app.reads[1].resolve(state("initial"));
  await app.settle();
  assert.doesNotMatch(app.byId("body-state").innerHTML + app.byId("calendar-day").innerHTML, /initial/);
  app.reads[0].resolve(life("initial"));
  await app.settle();
  assertSnapshot(app, "initial", "09:00");
  assert.match(app.byId("calendar-day").innerHTML, /计划 initial/);
  await app.advance(minute);
  const refreshed = app.refreshes[0]();
  assert.equal(app.reads.length, 4);
  app.reads[2].resolve(life("polled"));
  await app.settle();
  assertSnapshot(app, "initial", "09:00");
  assert.match(app.byId("calendar-day").innerHTML, /活动 initial/);
  app.reads[3].resolve(state("polled"));
  await app.settle();
  assert.equal(app.reads.length, 5);
  assert.deepEqual(app.statuses, [state("initial"), state("initial"), state("polled"), state("polled")]);
  app.reads[4].resolve(state("editor"));
  assert.deepEqual(await refreshed, state("editor"));
  app.selectEditorPage("next-page");
  assert.equal(app.reads[5].body.after_id, "next-page");
  app.reads[5].resolve(state("next-page"));
  await app.settle();
  assertSnapshot(app, "polled", "09:01");
  assert.match(app.byId("calendar-day").innerHTML, /更新于 <time[^>]+>09:01<\/time>/);
  assert.match(app.byId("calendar-day").innerHTML, /活动 polled[\s\S]*计划 polled/);
  assert.doesNotMatch(app.byId("calendar-day").innerHTML, /editor|next-page/);
});

test("iframe GET is fenced before routing and before replying, and POST retains its receipt", async () => {
  const app = await boot();
  await app.frameMessage({kind:"ready"});
  const first = app.frameMessages.at(-1).context.readGeneration;
  const request = {kind:"request", method:"GET", path:"life", body:{}, readGeneration:first};
  const a = app.frameMessage({...request, requestId:"a"});
  const aError = app.frameMessage({...request, requestId:"a-error"});
  const post = app.frameMessage({kind:"request", method:"POST", path:"life/command", requestId:"write",
    body:{action:"add_epub", operation_key:"original"}, timeoutMs:120000});
  assert.equal(app.writes[0].route, `${rolePath("a")}/life/command`);
  app.choose("b");
  const changed = app.frameMessages.at(-1);
  assert.equal(changed.kind, "context");
  assert.equal(changed.context.display.role_name, "角色 B");
  assert.notEqual(changed.context.readGeneration, first);
  assert.equal(app.reads.length, 6);
  await app.frameMessage({...request, requestId:"late-request"});
  assert.equal(app.reads.length, 6);
  const b = app.frameMessage({...request, requestId:"b", readGeneration:changed.context.readGeneration});
  assert.equal(app.reads[6].route, `${rolePath("b")}/life`);
  app.reads[6].resolve(life("B"));
  await b;
  app.reads[2].resolve(life("A"));
  app.reads[3].reject(new Error("old-role error"));
  app.writes[0].resolve({state:"succeeded"});
  await Promise.all([a, aError, post]);
  const replies = app.frameMessages.filter(message => message.kind === "response");
  assert.deepEqual(replies.map(({requestId, ok}) => [requestId, ok]), [["b", true], ["write", true]]);
  assert.deepEqual(replies[1].data, {state:"succeeded"});
  assert.equal(app.writes[0].options.timeoutMs, 120000);
});

test("calendar disclosures preserve open state and keyboard focus through a paired refresh", async () => {
  const app = await boot();
  const payload = {activities:[{activity_id:"cancel", title:"取消的活动", state:"cancelled", checkpoint:"活动已取消",
    started_at:"2026-11-18T00:00:00Z", updated_at:"2026-11-18T00:30:00Z"}]};
  app.reads[0].resolve(payload);
  app.reads[1].resolve(state("initial"));
  await app.settle();
  app.selectPage("calendar");
  const day = app.byId("calendar-day"), selector = '[data-overview-reason="agenda:activity:cancel"] > summary';
  const reason = {dataset:{overviewReason:"agenda:activity:cancel"}, open:true};
  const summary = {matches:value => value === "summary", parentElement:reason, hasAttribute:() => false};
  const focused = [];
  day.fire("toggle", {target:{matches:() => true, open:true}});
  day.fire("toggle", {target:reason});
  day.contains = value => value === summary;
  day.querySelector = value => value === selector ? {focus:() => focused.push(value)} : null;
  app.document.activeElement = summary;
  await app.advance(minute);
  const pair = app.reads.slice(-2);
  pair[0].resolve(payload);
  pair[1].resolve(state("polled"));
  await app.settle();
  assert.match(day.innerHTML, /<details class="gl-agenda-past" open>/);
  assert.match(day.innerHTML, /<details data-overview-reason="agenda:activity:cancel" open>/);
  assert.ok(focused.length > 0);
  assert.ok(focused.every(value => value === selector));
  assert.doesNotMatch(app.byId("upcoming").innerHTML, /data-overview-reason="agenda:activity:cancel" open/);
});

test("successful paired poll clears the initial read error while keeping save and connection errors", async () => {
  for (const other of ["", "save error", "connection error"]) {
    const app = await boot();
    app.reads[0].reject(new Error("initial read error"));
    app.reads[1].resolve(state("editor-only"));
    await app.settle();
    const notice = app.byId("notice");
    assert.equal(notice.textContent, "initial read error");
    if (other === "save error")app.failures[0](new Error(other));
    if (other === "connection error") {
      // A failed explicit reconnect reports its own error while the role's poll can still recover.
      app.failConnection(other);
      await app.byId("refresh").fire("click");
    }
    await app.advance(2 * minute);
    await resolveSnapshot(app, 2, "a", "recovered");
    assert.equal(notice.textContent, other);
    assert.equal(notice.hidden, !other);
  }
});

test("refreshing a non-default mapped role preserves the iframe generation and outstanding operations", async () => {
  const app = await boot();
  app.choose("b");
  await resolveSnapshot(app, 2, "b", "initial-B");
  await app.frameMessage({kind:"ready"});
  const {readGeneration} = app.frameMessages.at(-1).context;
  const pending = app.frameMessage({kind:"request", method:"GET", path:"life", body:{}, requestId:"keep-read", readGeneration});
  const importing = app.frameMessage({kind:"request", method:"POST", path:"life/command", requestId:"keep-import",
    body:{action:"add_epub", operation_key:"epub-batch"}, timeoutMs:120000});
  await app.byId("refresh").fire("click");
  const refreshed = app.frameMessages.at(-1);
  assert.equal(refreshed.context.readGeneration, readGeneration);
  assert.equal(refreshed.context.display.role_name, "角色 B");
  assert.equal(app.byId("role").value, "b");
  assert.equal(app.statuses.length, 2, "same role retains editor state");
  app.reads[4].resolve(life("iframe-B"));
  app.writes[0].resolve({state:"succeeded"});
  await Promise.all([pending, importing]);
  assert.deepEqual(app.frameMessages.filter(msg=>msg.kind === "response").map(msg=>[msg.requestId, msg.ok]),
    [["keep-read", true], ["keep-import", true]]);
  await app.advance(0);
  await resolveSnapshot(app, 5, "b", "refreshed-B");
});

test("confirmed saves start a pair immediately when idle and share its state even when queued", async () => {
  for (const inFlight of ["none", "pagination", "poll"]) {
    const app = await boot();
    await resolveSnapshot(app, 0, "a", "initial");
    const polling = inFlight === "poll", paging = inFlight === "pagination";
    if (polling) await app.advance(minute);
    if (paging) app.selectEditorPage("stale-page");
    const editor = polling ? 1 : 0;
    const save = app.editorApis[editor]("POST", `${rolePath("a")}/state/command`, {action:"configure"});
    app.writes[0].resolve({operation_id:"saved"});
    await save;
    const editorRefresh = app.refreshes[editor]();
    assert.equal(app.reads.length, paging ? 5 : 4, "no timer advance or standalone state read is needed");
    if (polling) {
      assert.equal(app.reads[2].route, `${rolePath("a")}/life`);
      app.reads[2].reject(new Error("transient life failure"));
      app.reads[3].resolve(state("before-save"));
      await app.settle();
    }
    const [lifeRead, stateRead] = app.reads.slice(-2);
    assert.deepEqual([lifeRead.route, stateRead.route], [`${rolePath("a")}/life`, `${rolePath("a")}/state`]);
    stateRead.resolve(state("saved"));
    assert.deepEqual(await editorRefresh, state("saved"));
    assert.deepEqual(app.editors[editor].status, state("saved"));
    assertSnapshot(app, "initial", "09:00");
    assert.match(app.byId("calendar-day").innerHTML, /计划 initial/);
    lifeRead.resolve(life("saved"));
    await app.settle();
    assertSnapshot(app, "saved", polling ? "09:01" : "09:00");
    assert.match(app.byId("calendar-day").innerHTML, /计划 saved/);
    if (paging) {
      app.reads[2].resolve({...state("stale-page"), request_cursor:"stale-page"});
      await app.settle();
      assert.deepEqual(app.editors[editor].status, state("saved"));
    }
    assert.equal(app.reads.length, polling ? 6 : paging ? 5 : 4);
  }
});

test("matching range metadata cannot verify a three-minute-fast browser near role-local midnight", async () => {
  const timezone = "Asia/Shanghai";
  const serverAt = Date.parse("2026-11-18T15:59:00Z");
  for (const [browserAt, start, end, complete] of [
    ["2026-11-18T15:48:00Z", "2026-11-18", "2026-11-19", false],
    [new Date(serverAt + 3 * minute).toISOString(), "2026-11-19", "2026-11-20", false],
    ["2026-11-18T16:16:00Z", "2026-11-19", "2026-11-20", true],
  ]) {
    const app = await boot(browserAt);
    app.reads[0].resolve({activities:[]});
    app.reads[1].resolve({settings:{planning_timezone:timezone}, occurrences:{items:[]}});
    await app.settle();
    const range = app.reads[2];
    assert.equal(range.route, `${rolePath("a")}/calendar/occurrences`);
    assert.deepEqual({...range.body}, {start, end, include_cancelled:"1"});
    // The range endpoint echoes the requested date, even when it is tomorrow on the server.
    const first = Date.parse(browserAt) + 30 * minute;
    range.resolve({start, end, timezone, items:[{id:"slot", title:"计划",
      start:new Date(first).toISOString(), end:new Date(first + 30 * minute).toISOString()}]});
    await app.settle();
    const html = app.byId("upcoming").innerHTML;
    assert.equal(/不完整/.test(html), !complete);
    assert.equal(/gl-agenda-gap/.test(html), complete);
  }
});

test("verified state reaches the editors and failed month projection before life settles", async () => {
  const app = await boot();
  app.selectPage("calendar");
  app.reads[2].reject(new Error("month failed"));
  await app.settle();
  assert.match(app.byId("calendar-month").innerHTML, /读取失败/);
  const inventory = {calendar:{items:[{title:"已有安排", schedule:{anchor:"2026-11-18T02:00:00Z", frequency:"once"}}]}};
  app.reads[1].resolve(inventory);
  await app.settle();
  assert.deepEqual(app.statuses, [inventory, inventory]);
  assert.match(app.byId("calendar-month").innerHTML, /推算 1 次/);
  assert.match(app.byId("calendar-day").innerHTML, /正在读取日程/);
  app.reads[0].reject(new Error("life failed"));
  await app.settle();
  assert.match(app.byId("calendar-month").innerHTML, /推算 1 次/);
  assert.match(app.byId("calendar-day").innerHTML, /日程暂不可读取/);
});

test("a newer state updates month fallback before life settles while dirty editors retain their page", async () => {
  const app = await boot();
  await resolveSnapshot(app, 0, "a", "initial");
  app.selectPage("calendar");
  app.reads[2].reject(new Error("month failed"));
  await app.settle();
  app.selectEditorPage("page-2");
  const inventory = (tag, count, cursor="") => ({...state(tag), request_cursor:cursor, calendar:{items:
    Array.from({length:count}, (_, index) => ({id:`${tag}-${index}`, title:tag,
      schedule:{anchor:"2026-11-18T02:00:00Z", frequency:"once"}}))}});
  const pageTwo = inventory("page-2", 1, "page-2");
  app.reads[3].resolve(pageTwo);
  await app.settle();
  assert.match(app.byId("calendar-month").innerHTML, /推算 1 次/);
  app.editors[0].dirty = true;
  await app.advance(minute);
  app.reads[5].resolve(inventory("polled", 2));
  await app.settle();
  assert.match(app.byId("calendar-month").innerHTML, /推算 2 次/);
  assert.match(app.byId("calendar-day").innerHTML, /计划 initial/);
  assert.deepEqual(app.editors.map(editor => editor.status), [pageTwo, pageTwo]);
  app.reads[4].resolve(life("polled"));
  await app.settle();
  assert.match(app.byId("calendar-day").innerHTML, /计划 polled/);

  app.editors[0].dirty = false;
  await app.advance(minute);
  const clean = inventory("clean", 3);
  app.reads[7].resolve(clean);
  await app.settle();
  assert.match(app.byId("calendar-month").innerHTML, /推算 3 次/);
  assert.deepEqual(app.editors.map(editor => editor.status), [clean, clean]);
});

test("same-role manual refresh reloads date and wallet caches and fences previously pending results", async () => {
  const app = await boot();
  await resolveSnapshot(app, 0, "a", "initial");
  const activity = suffix => ({items:[{activity_id:suffix, title:`历史 ${suffix}`, kind:"reading", state:"completed",
    updated_at:"2026-11-17T03:00:00Z"}]});
  const month = suffix => ({items:[{id:suffix, title:`日程 ${suffix}`, start:"2026-11-17T03:00:00Z", end:"2026-11-17T04:00:00Z"}]});
  app.selectPage("wallet");
  const oldWallet = app.reads.at(-1);
  app.selectPage("calendar");
  app.selectDay("2026-11-17");
  const [oldMonth, oldDay] = app.reads.slice(-2);
  await app.frameMessage({kind:"ready"});
  const {readGeneration} = app.frameMessages.at(-1).context;
  const count = app.frameMessages.length;
  await app.byId("refresh").fire("click");
  const [refreshLife, refreshState, freshMonth, freshDay] = app.reads.slice(5);
  assert.deepEqual([refreshLife.route, refreshState.route], [`${rolePath("a")}/life`, `${rolePath("a")}/state`]);
  assert.equal(freshMonth.route, oldMonth.route);
  assert.deepEqual(freshMonth.body, oldMonth.body);
  assert.equal(freshDay.route, oldDay.route);
  assert.deepEqual(freshDay.body, oldDay.body);
  assert.deepEqual(app.frameMessages.slice(count).map(msg => [msg.kind, msg.context?.readGeneration]), [["context", readGeneration]]);
  freshMonth.resolve(month("fresh"));
  freshDay.resolve(activity("fresh"));
  oldMonth.resolve(month("stale"));
  oldDay.resolve(activity("stale"));
  await app.settle();
  assert.match(app.byId("calendar-day").innerHTML, /日程 fresh[\s\S]*历史 fresh/);
  assert.doesNotMatch(app.byId("calendar-day").innerHTML, /stale/);
  app.selectPage("wallet");
  const freshWallet = app.reads.at(-1);
  assert.equal(freshWallet.route, oldWallet.route);
  assert.notEqual(freshWallet, oldWallet);
  freshWallet.resolve({label:"fresh wallet"});
  await app.settle();
  oldWallet.resolve({label:"stale wallet"});
  await app.settle();
  assert.equal(app.byId("wallet").innerHTML, "fresh wallet");
});

test("successful reads clear only matching errors in their role", async () => {
  const app = await boot();
  app.reads[0].reject(new Error("A life error"));
  app.reads[1].reject(new Error("A state error"));
  await app.settle();
  const notice = app.byId("notice");
  assert.match(notice.textContent, /A life error.*A state error/);
  const editorRefresh = app.refreshes[0]();
  app.reads[2].resolve(state("state-recovered"));
  await editorRefresh;
  assert.equal(notice.textContent, "A life error");
  await app.advance(2 * minute);
  app.reads[4].reject(new Error("A state failed again"));
  app.reads[3].resolve(life("life-recovered"));
  await app.settle();
  assert.equal(notice.textContent, "A state failed again");
  app.choose("b");
  assert.equal(notice.textContent, "");
});

test("wallet uses a verified life timezone when state fails and prefers a verified planning timezone", async () => {
  const app = await boot();
  app.selectPage("wallet");
  app.reads[2].resolve({});
  app.reads[0].resolve({...life("initial"), config:{timezone:"America/Los_Angeles"}});
  await app.settle();
  assert.equal(app.byId("wallet").innerHTML, "America/Los_Angeles");
  app.reads[1].reject(new Error("state failed"));
  await app.settle();
  assert.equal(app.byId("wallet").innerHTML, "America/Los_Angeles");
  await app.advance(2 * minute);
  app.reads[4].resolve({...state("recovered"), settings:{planning_timezone:"Europe/Paris"}});
  await app.settle();
  assert.equal(app.byId("wallet").innerHTML, "Europe/Paris");
  app.reads[3].reject(new Error("life failed"));
  app.reads[5].reject(new Error("range failed"));
  await app.settle();
  assert.equal(app.byId("wallet").innerHTML, "Europe/Paris");
});
