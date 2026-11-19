"use strict";
/* 钱包页在 app.js 里的接线：切换角色后 life/wallet 先于 life/state 返回时，
 * state 到达后钱包要按角色的规划时区重绘，且不重复请求。
 * 用最小 DOM 替身运行真实的 app.js 与 wallet-view.js；其他视图模块用桩。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..", "..");
const page = path.join(root, "plugins", "astrbot_plugin_good_life", "pages", "我会去好好生活");
const run = file => vm.runInThisContext(fs.readFileSync(file, "utf8"), {filename:file});

function element(id) {
  const listeners = {};
  return {id, innerHTML:"", textContent:"", hidden:false, disabled:false, value:"", dataset:{}, style:{}, listeners,
    parentElement:{toggleAttribute() {}, setAttribute() {}}, contentWindow:{postMessage() {}},
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }, setAttribute() {}, toggleAttribute() {},
    replaceChildren() {}, append() {}, contains:() => false, querySelector:() => null};
}

test("wallet re-renders in the role's timezone when life/state arrives after life/wallet", async () => {
  const elements = new Map(), byId = id => { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); };
  const nav = [{dataset:{page:"wallet"}, setAttribute() {}}];
  globalThis.window = globalThis;
  globalThis.document = {getElementById:byId, createElement:tag => element(tag), querySelectorAll:() => nav, querySelector:() => null, activeElement:null,
    addEventListener() {}, hidden:false, documentElement:{dataset:{theme:"walnut"}}};
  globalThis.CSS = {escape:value => value};
  globalThis.setInterval = () => 0;
  globalThis.addEventListener = () => {};
  /* 每个请求都挂起，由测试决定返回顺序。 */
  const pending = new Map(), calls = [];
  const deferred = key => new Promise(resolve => pending.set(key, resolve));
  const scope = "targets/t/r/";
  globalThis.AstrBotPluginPage = {
    ready:() => Promise.resolve(),
    apiGet(route) {
      calls.push(route);
      if (route === "status") return Promise.resolve({runtime_ready:true});
      if (route === "targets") return Promise.resolve({installation_id:"i", operator_id:"o", targets:[{id:"t", name:"窗口", roles:[{token:"r", name:"角色"}]}]});
      return deferred(route);
    },
    apiPost:() => Promise.reject(new Error("read only")),
  };
  const editor = {setScope() {}, setStatus() {}, markup:() => "", invalidate() {}};
  globalThis.GoodLifeIcons = {hydrate() {}, icon:() => ""};
  globalThis.CompanionCalendarEditor = {create:() => editor};
  globalThis.CompanionStateEditor = {create:() => editor};
  globalThis.GoodLifeOverview = {now:() => "", rest:() => "", stateGrid:() => "", schedule:() => "",
    calendarRequest:() => ({start:"2026-11-17", end:"2026-11-18"}),
    zoneOf:({state, life}) => state?.settings?.planning_timezone || life?.config?.timezone || "Asia/Shanghai"};
  globalThis.GoodLifeCalendarView = {initial:() => ({year:2026, month:10, selected:"2026-11-17"}), month:() => "", day:() => ""};
  globalThis.GoodLifeWorkspace = "";
  globalThis.GoodLifeTimetablePage = {create:() => ({show() {}, reset() {}, render() {}})};
  run(path.join(page, "icons.js")); globalThis.GoodLifeIcons.hydrate = () => {};
  run(path.join(page, "wallet-view.js"));
  run(path.join(page, "app.js"));
  const settle = () => new Promise(resolve => setTimeout(resolve, 0));
  await settle();
  assert.ok(pending.has(scope + "life") && pending.has(scope + "state"), "角色已选中，life 与 state 正在读取");

  byId("navigation").listeners.click[0]({target:{closest:() => nav[0]}});
  await settle();
  assert.ok(pending.has(scope + "life/wallet"));
  pending.get(scope + "life/wallet")({enabled:true, currency:"CNY", minor_unit_digits:2, balance_minor:100, reserved_minor:0,
    available_minor:100, reservations:[], transactions:[], next_before:null});
  await settle();
  assert.match(byId("wallet").innerHTML, /按 Asia\/Shanghai 显示/, "state 未到时用后备时区");

  pending.get(scope + "life")({activities:[], config:{}});
  pending.get(scope + "state")({settings:{planning_timezone:"America/New_York"}, calendar:{items:[]}});
  await settle(); await settle();
  assert.match(byId("wallet").innerHTML, /按 America\/New_York 显示/, "state 到达后钱包按角色时区重绘");
  assert.equal(calls.filter(route => route === scope + "life/wallet").length, 1, "重绘不重复请求钱包");
});
