const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() {
    this.listeners = new Map();
    this.children = [];
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
  }
  addEventListener(name, callback) {
    const previous = this.listeners.get(name);
    this.listeners.set(name, previous ? event => { previous(event); return callback(event); } : callback);
  }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  querySelectorAll() { return this.children; }
  setAttribute() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  click() { this.listeners.get("click")?.({preventDefault() {}}); }
  submit() { this.listeners.get("submit")?.({preventDefault() {}}); }
}

test("life polls only active work and requests the visible compact page", async () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "pages", "日记与生活", "app.js"), "utf8");
  const elements = new Map(), byId = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const interestTab = new Element(); interestTab.dataset.tab = "interests";
  const readingArchive = new Element(); readingArchive.dataset.archiveKind = "reading_note";
  const document = {hidden:false, getElementById:byId, createElement:() => new Element(), addEventListener() {},
    querySelectorAll: selector => selector === "[data-tab]" ? [interestTab] : selector === "[data-archive-kind]" ? [readingArchive] : []};
  const requests = [], timers = new Map(); let timerId = 0;
  let status = {role_id:"role", host_state:"running", config:{enabled:true, reading_enabled:true},
    journals:[], activities:[], running:[], receipts:[], interests:[], next_note_cursor:"cursor"};
  const bridge = {ready:async () => {}, getContext:() => ({}), apiGet:async (_path, query) => {
    requests.push(JSON.parse(JSON.stringify(query))); return status;
  }};
  vm.runInNewContext(source, {window:{AstrBotPluginPage:bridge}, document, Option:class extends Element {},
    setTimeout(fn, ms) { timers.set(++timerId, {fn, ms}); return timerId; }, clearTimeout(id) { timers.delete(id); }});
  await new Promise(setImmediate);
  assert.deepEqual(requests[0], {compact:"1", limit:20, view:"archive", archive_kind:"diary"});
  assert.equal(timers.size, 0, "enabled automatic reading is not active work");
  status = {...status, refresh_active:true};
  byId("refresh").click(); await new Promise(setImmediate);
  assert.equal([...timers.values()][0].ms, 3000);
  status = {...status, refresh_active:false};
  readingArchive.click(); await new Promise(setImmediate);
  byId("older").click(); await new Promise(setImmediate);
  assert.equal(requests.at(-1).note_cursor, "cursor");
  interestTab.click(); await new Promise(setImmediate);
  assert.equal(requests.at(-1).view, "interests");
  assert.equal(timers.size, 0);
});

test("interest controls send scoped revision and view token", async () => {
  const sourcePath = path.join(__dirname, "..", "pages", "日记与生活", "app.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const ids = new Set([...source.matchAll(/\$\("([^"]+)"\)/g)].map(match => match[1]));
  const elements = new Map([...ids].map(id => [id, new Element()]));
  const document = {hidden:false, getElementById:id => elements.get(id) || new Element(),
    createElement:() => new Element(), querySelectorAll:() => [], addEventListener() {}};
  const status = {role_id:"role-test", host_state:"running", config:{enabled:true}, journals:[],
    activities:[], receipts:[], running:[], view_token:"general", edit_token:"edit", interests:[
      {interest_id:"abc", name:"鸟鸣", status:"curious", summary:"感兴趣", evidence:[],
       revision:3, view_token:"interest-token"}]};
  const requests = [];
  const bridge = {ready:async () => bridge, getContext:() => ({display:{}}), apiGet:async () => status,
    apiPost:async (_endpoint, body) => {requests.push(body); return {};}};
  vm.runInNewContext(source, {window:{crypto:{randomUUID:() => "id"},AstrBotPluginPage:bridge},
    document, Option:class extends Element {}, setTimeout, clearTimeout}, {filename:sourcePath});
  await new Promise(setImmediate); await new Promise(setImmediate);
  const card = elements.get("interest-list").children[1];
  // Collapsed: a <details> whose summary carries the name, stage and one line.
  assert.equal(card.className, "activity fold");
  assert.equal(card.open, false);
  const [summary, body] = card.children;
  const head = summary.children[0];
  assert.equal(head.children[0].textContent, "鸟鸣");
  assert.equal(head.children[1].textContent, "正在探索");
  assert.equal(summary.children[1].textContent, "感兴趣");
  // Expanded body keeps the full details and the actions.
  const actions = body.children[body.children.length - 1];
  actions.children[0].click();
  await new Promise(setImmediate);
  assert.equal(requests[0].action, "interest_pause");
  assert.equal(requests[0].view_token, "interest-token");
  assert.equal(requests[0].expected_revision, 3);
});

test("compact interests fetch trails on expansion and unchanged polls preserve the card and input", async () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "pages", "日记与生活", "app.js"), "utf8");
  const elements = new Map(), byId = id => {
    if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id);
  };
  const tab = new Element(); tab.dataset.tab = "interests";
  const document = {hidden:false, getElementById:byId, createElement:() => new Element(), addEventListener() {},
    querySelectorAll: selector => selector === "[data-tab]" ? [tab] : []};
  const interest = {interest_id:"a".repeat(64), revision:3, view_token:"token", name:"鸟鸣", status:"curious", compact:true};
  const status = {role_id:"role", host_state:"running", config:{}, journals:[], activities:[], receipts:[], running:[], interests:[interest]};
  const reads = [];
  const bridge = {ready:async () => {}, getContext:() => ({}), apiGet:async (_path, query) => {
    reads.push(JSON.parse(JSON.stringify(query)));
    return query.interest_id ? {...status, interests:[{...interest, compact:false, evidence:[{reason:"实际感受", occurred_at:"2026-11-22T00:00:00Z"}]}]} : status;
  }};
  vm.runInNewContext(source, {window:{AstrBotPluginPage:bridge}, document, Option:class extends Element {}, setTimeout() {}, clearTimeout() {}});
  await new Promise(setImmediate);
  tab.click(); await new Promise(setImmediate);
  const card = byId("interest-list").children[1];
  assert.equal(reads.filter(query => query.interest_id).length, 0);
  card.open = true;
  await card.listeners.get("toggle")();
  assert.equal(reads.at(-1).interest_id, interest.interest_id);
  const body = card.children[1], actions = body.children.find(child => child.className === "actions");
  actions.children[1].click(); actions.children[2].value = "未保存的名称";
  byId("refresh").click(); await new Promise(setImmediate);
  assert.equal(byId("interest-list").children[1], card);
  assert.equal(card.open, true);
  assert.equal(actions.children[2].value, "未保存的名称");
  assert.equal(reads.filter(query => query.interest_id).length, 1);
});

test("life workspace sends with getRandomValues and localizes command errors", async () => {
  const sourcePath = path.join(__dirname, "..", "pages", "日记与生活", "app.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const ids = new Set([...source.matchAll(/\$\("([^"]+)"\)/g)].map((match) => match[1]));
  const elements = new Map([...ids].map((id) => [id, new Element()]));
  const document = {
    hidden: false,
    getElementById: (id) => elements.get(id) || new Element(),
    createElement: () => new Element(),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const requests = [];
  const status = {
    role_id: "role-test",
    host_state: "running",
    config: {
      enabled: true, diary_enabled: true, sleep_enabled: true, reading_enabled: false,
      derive_enabled: false, timezone: "UTC", diary_time: "22:00", sleep_time: "23:00",
      sleep_timezone: "UTC", sleep_variation_minutes: 0, sleep_window_minutes: 60,
      sleep_minutes: 480, reading_chunk_chars: 1500, reading_daily_segments: 2,
    },
    journals: [], activities: [], receipts: [], running: [],
    next_diary_at: null, next_sleep_at: null, next_date: null, next_activity: null,
    view_token: "view-test", edit_token: "edit-test", sleep_schedule: null,
  };
  const bridge = {
    ready: async () => bridge,
    getContext: () => ({display: {role_name: "测试角色"}}),
    apiGet: async () => status,
    apiPost: async (endpoint, body) => {
      requests.push({endpoint, body});
      return {status: "error", data: {code: "life_command_invalid"}};
    },
  };
  const webCrypto = {
    getRandomValues(bytes) {
      bytes.fill(10);
      return bytes;
    },
  };
  const sandbox = {
    window: {crypto: webCrypto, AstrBotPluginPage: bridge},
    document,
    Option: class extends Element {
      constructor(label, value) { super(); this.textContent = label; this.value = value; }
    },
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(source, sandbox, {filename: sourcePath});
  await new Promise(setImmediate);
  await new Promise(setImmediate);

  elements.get("start-sleep").click();
  elements.get("editor-form").submit();
  await new Promise(setImmediate);

  assert.equal(requests.length, 1);
  assert.equal(requests[0].endpoint, "life/command");
  assert.match(requests[0].body.operation_key, /^(?:[0-9a-f]{32}|life-[0-9]+-[a-z0-9]+)$/);
  assert.equal(elements.get("notice").textContent, "生活记录或操作信息已变化，请刷新后重新尝试。");
});

test("diary settings do not submit retired reading pace fields", async () => {
  const sourcePath = path.join(__dirname, "..", "pages", "日记与生活", "app.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const ids = new Set([...source.matchAll(/\$\("([^"]+)"\)/g)].map(match => match[1]));
  const config = {enabled:true, diary_enabled:true, sleep_enabled:false, reading_enabled:false,
    derive_enabled:true, timezone:"UTC", diary_time:"22:00", sleep_time:"23:00", sleep_timezone:"UTC",
    sleep_minutes:480, sleep_variation_minutes:20, sleep_window_minutes:120,
    reading_chunk_chars:1500, reading_daily_segments:2,
    social_existing_percent:60, social_related_percent:25, social_new_percent:15};
  Object.keys(config).forEach(id => ids.add(id));
  const elements = new Map([...ids].map(id => [id, new Element()]));
  const document = {hidden:false, getElementById:id => elements.get(id) || new Element(),
    createElement:() => new Element(), querySelectorAll:() => [], addEventListener() {}};
  const status = {role_id:"role-test", host_state:"running", config, journals:[], activities:[], receipts:[],
    running:[], view_token:"view", edit_token:"edit"};
  const requests = [];
  const bridge = {ready:async () => bridge, getContext:() => ({display:{role_name:"测试角色"}}),
    apiGet:async () => status, apiPost:async (_endpoint, body) => {requests.push(body); return {};}};
  vm.runInNewContext(source, {window:{crypto:{randomUUID:() => "id"},AstrBotPluginPage:bridge},
    document, Option:class extends Element {}, setTimeout, clearTimeout}, {filename:sourcePath});
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  elements.get("diary_time").value = "21:30";
  elements.get("reading_chunk_chars").value = "1200";
  elements.get("config-form").submit();
  await new Promise(setImmediate);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].changes.diary_time, "21:30");
  assert.equal(requests[0].changes.enabled, true);
  assert.equal(requests[0].changes.diary_enabled, true);
  assert.equal(requests[0].changes.derive_enabled, true);
  assert.equal(requests[0].changes.timezone, "UTC");
  assert.deepEqual(Object.keys(requests[0].changes).sort(),
    ["enabled", "diary_enabled", "derive_enabled", "timezone", "diary_time"].sort());
});

test("EPUB selection uses the role-scoped life command and reports the queued segments", async () => {
  const sourcePath = path.join(__dirname, "..", "pages", "日记与生活", "app.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const ids = new Set([...source.matchAll(/\$\("([^"]+)"\)/g)].map((match) => match[1]));
  const elements = new Map([...ids].map((id) => [id, new Element()]));
  const document = {hidden: false, getElementById: id => elements.get(id) || new Element(),
    createElement: () => new Element(), querySelectorAll: () => [], addEventListener() {}};
  const status = {role_id: "role-test", host_state: "running", config: {enabled: true, reading_enabled: false},
    journals: [], activities: [], receipts: [], running: [], view_token: "view-test", edit_token: "edit-test"};
  const requests = [];
  const bridge = {ready: async () => bridge, getContext: () => ({display: {role_name: "测试角色"}}),
    apiGet: async () => status,
    apiPost: async (endpoint, body, options) => { requests.push({endpoint, body, options});
      return {title: "测试书", activity_ids: ["one", "two"]}; }};
  class FileReader { readAsDataURL() { this.result = "data:application/epub+zip;base64,YWJj"; this.onload(); } }
  const sandbox = {window: {crypto: {randomUUID: () => "random-id"}, AstrBotPluginPage: bridge}, document,
    FileReader, Option: class extends Element {}, setTimeout, clearTimeout};
  vm.runInNewContext(source, sandbox, {filename: sourcePath});
  await new Promise(setImmediate);
  const target = {files: [{name: "novel.epub", size: 3}], value: "novel.epub"};
  await elements.get("epub-file").listeners.get("change")({target});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].endpoint, "life/command");
  assert.equal(requests[0].body.action, "add_epub");
  assert.equal(requests[0].body.view_token, "view-test");
  assert.equal(requests[0].body.epub_base64, "YWJj");
  assert.equal(requests[0].options.timeoutMs, 120000);
  assert.equal(elements.get("notice").textContent, "已导入 1 / 1 本。");
  assert.equal(target.value, "");
});

test("multiple EPUBs and folders import independently and report failures", async () => {
  const sourcePath = path.join(__dirname, "..", "pages", "日记与生活", "app.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const ids = new Set([...source.matchAll(/\$\("([^"]+)"\)/g)].map(match => match[1]));
  const elements = new Map([...ids].map(id => [id, new Element()]));
  const document = {hidden: false, getElementById: id => elements.get(id) || new Element(),
    createElement: () => new Element(), querySelectorAll: () => [], addEventListener() {}};
  const status = {role_id: "role-test", host_state: "running", config: {enabled: true},
    journals: [], activities: [], receipts: [], running: [], view_token: "view-test", edit_token: "edit-test"};
  const requests = [];
  const bridge = {ready: async () => bridge, getContext: () => ({display: {role_name: "测试角色"}}),
    apiGet: async () => status, apiPost: async (_endpoint, body) => {
      requests.push(body);
      return body.filename === "bad.epub" ? {status: "error", data: {code: "life_epub_invalid"}} : {title: "好书"};
    }};
  let sequence = 0;
  class FileReader { readAsDataURL() { this.result = "data:application/epub+zip;base64,YWJj"; this.onload(); } }
  const sandbox = {window: {crypto: {randomUUID: () => "id-" + (++sequence)}, AstrBotPluginPage: bridge},
    document, FileReader, Option: class extends Element {}, setTimeout, clearTimeout};
  vm.runInNewContext(source, sandbox, {filename: sourcePath});
  await new Promise(setImmediate);
  const target = {files: [{name: "good.epub", size: 3}, {name: "bad.epub", size: 3},
                           {name: "notes.txt", size: 3}], value: "chosen"};
  await elements.get("epub-folder").listeners.get("change")({target});
  assert.deepEqual(requests.map(r => r.filename), ["good.epub", "bad.epub"]);
  assert.notEqual(requests[0].operation_key, requests[1].operation_key);
  assert.match(elements.get("notice").textContent, /已导入 1 \/ 2 本/);
  assert.match(elements.get("notice").textContent, /已跳过 1 个非 EPUB 文件/);
  assert.match(elements.get("notice").textContent, /bad\.epub/);
  assert.equal(target.value, "");
});

test("reading notes on the same day remain separately selectable and read-only", async () => {
  const sourcePath = path.join(__dirname, "..", "pages", "日记与生活", "app.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const ids = new Set([...source.matchAll(/\$\("([^"]+)"\)/g)].map(match => match[1]));
  const elements = new Map([...ids].map(id => [id, new Element()]));
  const readingTab = new Element();
  readingTab.dataset.archiveKind = "reading_note";
  const document = {hidden: false, getElementById: id => elements.get(id) || new Element(),
    createElement: () => new Element(),
    querySelectorAll: selector => selector === "[data-archive-kind]" ? [readingTab] : [], addEventListener() {}};
  const note = (id, body) => ({kind: "reading_note", activity_id: id, local_date: "2026-11-13",
    state: "active", book_title: "一本书", start_offset: 0, end_offset: 10, excerpt: "实际原文", body});
  const status = {role_id: "role-test", host_state: "running", config: {},
    journals: [note("one", "第一条感想"), note("two", "第二条感想")], activities: [], receipts: [], running: []};
  const bridge = {ready: async () => bridge, getContext: () => ({display: {role_name: "测试角色"}}),
    apiGet: async () => status};
  const sandbox = {window: {crypto: {}, AstrBotPluginPage: bridge}, document,
    Option: class extends Element {}, setTimeout, clearTimeout};
  vm.runInNewContext(source, sandbox, {filename: sourcePath});
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  readingTab.click();
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  const list = elements.get("journal-list");
  assert.equal(list.children.length, 2);
  list.children[1].click();
  const detail = elements.get("journal-detail");
  const text = detail.children.map(item => item.textContent).join(" ");
  assert.match(text, /读书笔记/);
  assert.match(text, /实际原文/);
  assert.match(text, /第二条感想/);
  assert.doesNotMatch(text, /第一条感想/);
  assert.equal(detail.children.some(item => item.textContent === "删除"), false);
});
