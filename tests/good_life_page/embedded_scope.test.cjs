"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const workspace = path.resolve(__dirname, "../../plugins/astrbot_plugin_good_life/pages/我会去好好生活/life");
const settle = () => new Promise(resolve => setImmediate(resolve));

function element(tag = "", textContent = "") {
  const listeners = new Map();
  return {tag, textContent, children:[], dataset:{}, value:"", checked:false, open:false,
    classList:{toggle() {}}, parentElement:{classList:{toggle() {}}},
    addEventListener(type, fn) { listeners.set(type, fn); },
    fire(type, event) { return listeners.get(type)?.(event); },
    dispatchEvent(event) { this.fire(event.type, event); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.textContent = ""; if (tag === "select") this.value = ""; },
    close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
  };
}
const textOf = node => node.textContent + node.children.map(textOf).join("");

async function boot() {
  const elements = new Map(), byId = id => {
    if (!elements.has(id)) elements.set(id, element(["retry-id", "dream-retry"].includes(id) ? "select" : ""));
    return elements.get(id);
  };
  const tabs = ["archive", "reading", "social", "interests", "sleep", "receipts"].map(id => {
    const button = element("button"); button.dataset.tab = id; byId(id).id = id; return button;
  });
  const archives = ["diary", "reading_note", "social_note"].map(kind => {
    const button = element("button"); button.dataset.archiveKind = kind; return button;
  });
  const document = Object.assign(element(), {documentElement:element(), getElementById:byId, createElement:element,
    querySelector:selector => tabs.find(b => selector === `[data-tab="${b.dataset.tab}"]`) ||
      archives.find(b => selector === `[data-archive-kind="${b.dataset.archiveKind}"]`),
    querySelectorAll(selector) {
      if (selector === "[data-tab]") return tabs;
      if (selector === "[data-archive-kind]") return archives;
      if (selector === ".tab-panel") return tabs.map(b => byId(b.dataset.tab));
      if (selector === "button, select") return [...elements.values(), ...tabs, ...archives];
      return [];
    },
  });
  byId("config-form").querySelectorAll = () => [byId("enabled"), byId("timezone")];
  byId("config-form").reset = () => { byId("enabled").checked = false; byId("timezone").value = "Asia/Shanghai"; };
  const sent = [], files = [], receipts = [], timers = new Map();
  const parent = {postMessage(message) { sent.push(JSON.parse(JSON.stringify(message))); }};
  const window = Object.assign(element(), {crypto:{randomUUID:() => "op-" + (receipts.length + 1)}});
  let timerId = 0;
  const env = vm.createContext({window, document, parent, CSS:{escape:value => value},
    CustomEvent:class { constructor(type, {detail}) { this.type = type; this.detail = detail; } },
    Option:class { constructor(text, value) { Object.assign(this, element("option", text), {value}); } },
    FileReader:class { readAsDataURL(file) { files.push({name:file.name, finish:() => {
      this.result = "data:application/epub+zip;base64,YQ=="; this.onload();
    }}); } },
    setTimeout(fn, delay) { timers.set(++timerId, {fn, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  for (const name of ["embedded-bridge.js", "app.js"]) {
    vm.runInContext(fs.readFileSync(path.join(workspace, name), "utf8"), env, {filename:name});
  }
  const bridge = window.AstrBotPluginPage, post = bridge.apiPost;
  bridge.apiPost = (...args) => { const receipt = post(...args); receipts.push(receipt); return receipt; };
  const message = data => window.fire("message", {source:parent, data:{channel:"companion-workspace", ...data}});
  const scope = (readGeneration, role, section = "reading", active = true) => message({kind:"context", context:{section,
    display:{role_name:role, subject_name:`${role} participant`}, readGeneration, active}});
  scope(0, "A");
  await settle();
  return {byId, bridge, scope, files, receipts, timers,
    requests:method => sent.filter(item => item.kind === "request" && item.method === method),
    reply(request, data) { message({kind:"response", requestId:request.requestId, ok:true, data}); },
    import(...names) {
      const input = byId("epub-file");
      input.files = names.map(name => ({name, size:4}));
      input.value = names[0];
      return input.fire("change", {target:input});
    },
  };
}

function snapshot(role) {
  return {view_token:`token-${role}`, host_state:"running", config:{enabled:true, timezone:`zone-${role}`},
    next_date:`date-${role}`, next_activity:`cursor-${role}`, next_diary_at:"2026-11-18T04:00:00Z",
    journals:[{kind:"reading_note", local_date:"2026-11-18", body:`Note ${role}`, book_title:`Book ${role}`,
      start_offset:0, end_offset:5}],
    activities:[{title:`Book ${role}`, kind:"reading", state:"active"},
      {title:`Sleep ${role}`, kind:"sleep"}, {title:`Browsing ${role}`, kind:"social"}],
    reading_plan:{state:"selected", title:`Book ${role}`}, profile_hobbies:`Hobby ${role}`,
    interests:[{name:`Interest ${role}`, status:"curious"}], disliked_items:[{title:`Disliked ${role}`}],
    recent_exposures:[{title:`Exposure ${role}`}], sleep_schedule:{enabled:true, physiology:{sleep_pressure:30}},
    receipts:[{operation_id:`receipt-${role}`, state:"failed"}],
  };
}

test("changing the mapped role clears the iframe synchronously and replaces paginated reads", async () => {
  const app = await boot(), $ = app.byId;
  const [initial] = app.requests("GET");
  assert.equal(initial.readGeneration, 0);
  app.reply(initial, snapshot("A"));
  await settle();
  assert.equal($("role").textContent, "A");
  assert.match(textOf($("journal-detail")), /Note A/);
  assert.match(textOf($("reading-list")), /Book A/);
  assert.equal($("timezone").value, "zone-A");
  $("timezone").value = "old draft";
  $("config-form").fire("input");
  $("add-text").fire("click");
  assert.equal($("editor").open, true);
  $("text-body").value = "old modal draft";
  $("retry-id").value = "receipt-A";
  $("older").fire("click");
  $("more-activities").fire("click");
  const [lateRead, queuedRead] = app.requests("GET").slice(1);
  assert.deepEqual(queuedRead.body, {compact:"1", limit:20, view:"reading", archive_kind:"reading_note", note_cursor:"date-A", after_activity:"cursor-A"});
  const rejected = assert.rejects(app.bridge.apiGet("life", {}), {code:"scope_changed"});
  assert.equal([...app.timers.values()].some(timer => timer.delay === 3000), false, "in-flight refresh replaces the old poll");

  app.reply(queuedRead, snapshot("queued A"));
  app.scope(1, "B", "browsing");
  assert.equal($("role").textContent, "—");
  assert.equal($("participant").textContent, "—");
  for (const id of ["reading-plan", "next-routines", "reading-list", "sleep-list", "social-list", "receipt-list",
    "profile-hobbies", "interest-list", "disliked-item-list", "recent-exposure-list"]) assert.equal(textOf($(id)), "", id);
  assert.doesNotMatch(textOf($("journal-detail")), /Note A/);
  assert.equal($("editor").open, false);
  assert.equal($("text-body").value, "");
  assert.equal($("timezone").value, "Asia/Shanghai");
  assert.equal($("timezone").disabled, true);
  assert.equal($("enabled").checked, false);
  assert.equal($("retry-form").hidden, true);
  assert.equal($("retry-id").value, "");
  for (const id of ["newer", "older", "first-activities", "more-activities"]) assert.equal($(id).disabled, true, id);
  assert.equal($("social").hidden, false);
  assert.equal(app.timers.size, 1, "only role B's read timer survives");
  const read = app.requests("GET").at(-1);
  assert.equal(read.readGeneration, 1);
  assert.deepEqual(read.body, {compact:"1", limit:20, view:"reading", archive_kind:"social_note"});

  app.reply(lateRead, snapshot("late A"));
  await rejected;
  await settle();
  assert.equal($("notice").textContent, "");
  assert.equal($("role").textContent, "—");
  app.reply(read, snapshot("B"));
  await settle();
  assert.equal($("role").textContent, "B");
  assert.equal($("timezone").value, "zone-B");
  assert.match(textOf($("social-list")), /Browsing B/);
});

test("idle reading_enabled does not poll and a hidden wallet workspace does not read", async () => {
  const app = await boot();
  const idle = {...snapshot("A"), activities:[], running:[], config:{enabled:true, reading_enabled:true}};
  app.reply(app.requests("GET").at(-1), idle);
  await settle();
  assert.equal([...app.timers.values()].some(timer => timer.delay === 3000), false);
  const count = app.requests("GET").length;
  app.scope(0, "A", "settings", false);
  app.byId("refresh").fire("click");
  await settle();
  assert.equal(app.requests("GET").length, count);
  app.scope(0, "A", "reading", true);
  assert.equal(app.requests("GET").length, count + 1);
  app.reply(app.requests("GET").at(-1), {...idle, refresh_active:true});
  await settle();
  assert.equal([...app.timers.values()].some(timer => timer.delay === 3000), true);
  app.scope(0, "A", "settings", false);
  assert.equal([...app.timers.values()].some(timer => timer.delay === 3000), false);
});

test("same-generation context retains the iframe draft and lets an EPUB batch finish", async () => {
  const app = await boot(), $ = app.byId;
  app.reply(app.requests("GET").at(-1), snapshot("A"));
  await settle();
  $("timezone").value = "unsaved draft";
  $("config-form").fire("input");
  const importing = app.import("first.epub", "second.epub");
  app.files[0].finish();
  await settle();
  app.scope(0, "A");
  assert.equal($("timezone").value, "unsaved draft");
  assert.equal($("refresh").disabled, true);
  assert.equal(app.requests("GET").length, 1);
  app.reply(app.requests("POST")[0], {state:"succeeded", title:"First", activity_ids:["one"]});
  await settle();
  app.files[1].finish();
  await settle();
  const second = app.requests("POST")[1];
  assert.equal(second.body.view_token, "token-A");
  app.reply(second, {state:"succeeded", title:"Second", activity_ids:["two"]});
  await settle();
  app.reply(app.requests("GET").at(-1), snapshot("A"));
  await importing;
  assert.equal($("timezone").value, "unsaved draft");
  assert.match($("notice").textContent, /已导入 2 \/ 2 本/);
});

test("role changes retain sent EPUB receipts and stop unsent files from crossing into the new role", async () => {
  const app = await boot(), $ = app.byId;
  app.reply(app.requests("GET").at(-1), snapshot("A"));
  await settle();
  const firstImport = app.import("A1.epub", "A2.epub");
  app.files[0].finish();
  await settle();
  const [first] = app.requests("POST");
  assert.equal(first.path, "life/command");
  assert.equal(first.timeoutMs, 120000);
  assert.equal(Object.hasOwn(first, "readGeneration"), false);
  assert.deepEqual(first.body, {action:"add_epub", operation_key:"op-1", view_token:"token-A",
    filename:"A1.epub", epub_base64:"YQ=="});
  assert.equal($("refresh").disabled, true);

  app.scope(1, "B");
  const secondRead = app.requests("GET").at(-1);
  assert.equal(secondRead.readGeneration, 1, "role B refreshes even with an EPUB POST pending");
  app.reply(secondRead, snapshot("B"));
  await settle();
  const secondImport = app.import("B1.epub", "B2.epub");
  app.files[1].finish();
  await settle();
  const firstReceipt = {state:"succeeded", title:"A1", activity_ids:["a1"]};
  app.reply(first, firstReceipt);
  assert.deepEqual(await app.receipts[0], firstReceipt);
  await firstImport;
  assert.deepEqual(app.files.map(file => file.name), ["A1.epub", "B1.epub"]);
  assert.equal($("refresh").disabled, true, "the old completion does not unlock role B's import");
  assert.equal($("epub-file").value, "B1.epub");
  assert.match($("notice").textContent, /B1\.epub/);

  const second = app.requests("POST").at(-1);
  assert.equal(second.body.view_token, "token-B");
  app.reply(second, {state:"succeeded", title:"B1", activity_ids:["b1"]});
  await settle();
  assert.equal(app.files[2].name, "B2.epub");
  app.scope(2, "C");
  const thirdRead = app.requests("GET").at(-1);
  assert.equal(thirdRead.readGeneration, 2);
  app.reply(thirdRead, snapshot("C"));
  await settle();
  app.files[2].finish();
  await secondImport;
  assert.equal(app.requests("POST").length, 2, "the file read for role B never becomes a role C POST");
  assert.equal(app.requests("GET").length, 3, "old EPUB completions do not reload the new role");
  assert.equal($("role").textContent, "C");
  assert.equal($("notice").textContent, "");
});

test("reading notes and shelf fragments are grouped by book, newest book open, 其他 last", async () => {
  const app = await boot(), $ = app.byId;
  const part = (id, title, state, updated_at, start_offset, end_offset) =>
    ({activity_id:id, kind:"reading", title, state, updated_at, start_offset, end_offset});
  const note = (id, book_title, completed_at, start_offset, end_offset) => ({kind:"reading_note", activity_id:id,
    local_date:completed_at.slice(0, 10), completed_at, book_title, start_offset, end_offset, body:`Body ${id}`});
  app.reply(app.requests("GET").at(-1), {...snapshot("A"),
    activities:[part("o1", "Old · 1–10 / 100 字符", "completed", "2026-11-10T00:00:00Z", 0, 10),
      part("n2", "New · 11–20 / 40 字符", "planned", "2026-11-20T00:00:00Z", 10, 20),
      part("n1", "New · 1–10 / 40 字符", "completed", "2026-11-19T00:00:00Z", 0, 10),
      {title:"Browsing A", kind:"social"}],
    journals:[note("n1", "New", "2026-11-19T00:00:00Z", 0, 10), note("x", "", "2026-11-21T00:00:00Z", 0, 5),
      note("o1", "Old", "2026-11-10T00:00:00Z", 0, 10), note("n0", "New", "2026-11-18T00:00:00Z", 0, 10)]});
  await settle();
  const title = group => group.children[0].children[0].textContent;

  const shelf = $("reading-list").children;
  assert.deepEqual(shelf.map(title), ["《New》", "《Old》"]);
  assert.deepEqual(shelf.map(group => group.open), [true, false]);
  assert.match(textOf(shelf[0].children[0]), /2 段 · 已读 25%/);
  assert.deepEqual(shelf[0].children[1].children.map(card => card.children[0].textContent),
    ["片段 1–10 / 40 字符", "片段 11–20 / 40 字符"], "fragments follow the book's order");
  assert.match(textOf($("social-list")), /Browsing A/, "browsing stays a flat list");

  let notes = $("journal-list").children;
  assert.deepEqual(notes.map(title), ["《New》", "《Old》", "其他"]);
  assert.deepEqual(notes.map(group => group.open), [true, false, false]);
  assert.match(textOf(notes[0].children[0]), /2 篇 .* 读到 10 \/ 40 字/);
  assert.deepEqual(notes[0].children[1].children.map(link => link.textContent),
    ["2026-11-18 · 第 1–10 字", "2026-11-19 · 第 1–10 字"], "notes within a book run oldest first");
  assert.match(textOf($("journal-detail")), /Body n1/);

  // A group the reader collapsed stays collapsed when the list redraws; the group
  // holding the newly selected note opens.
  notes[0].children[0].fire("click");
  notes[1].children[1].children[0].fire("click");
  notes = $("journal-list").children;
  assert.deepEqual(notes.map(group => group.open), [false, true, false]);
  assert.match(textOf($("journal-detail")), /Body o1/);
});
