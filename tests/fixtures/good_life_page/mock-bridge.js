/* 本地预览用的 AstrBot 插件页桥接替身（只在 serve_preview.py 下替换 /api/plugin/page/bridge-sdk.js）。
 * 地址参数：
 *   wallet = ok | empty | disabled | error | more-error（第二批读取失败一次）
 *   delay  = 每次请求的延迟毫秒数（看读取中状态），默认 300
 *   page   = 启动后直接切到的板块，例如 wallet */
"use strict";
(() => {
  if (window.parent !== window) return; /* 内嵌生活工作区有自己的桥接，这里不覆盖。 */
  const params = new URLSearchParams(location.search), scenario = params.get("wallet") || "ok";
  const delay = Number(params.get("delay") ?? 300), fixtures = globalThis.GoodLifeWalletFixtures;
  let failedMore = false;
  const wait = value => new Promise(resolve => setTimeout(() => resolve(value), delay));
  const fail = code => wait().then(() => { throw Object.assign(new Error("模拟读取失败"), {code}); });
  const scope = "targets/dev-target/dev-role/";
  function wallet(query = {}) {
    if (scenario === "error") return fail("wallet_unavailable");
    const set = fixtures[scenario === "more-error" ? "ok" : scenario] || fixtures.ok;
    const limit = Number(query.limit);
    if (!Number.isInteger(limit) || limit < 1 || limit > 50) return fail("invalid_limit");
    if (!query.before) return wait(structuredClone(set.first));
    if (scenario === "more-error" && !failedMore) { failedMore = true; return fail("bridge_timeout"); }
    return set.pages[query.before] ? wait(structuredClone(set.pages[query.before])) : fail("invalid_cursor");
  }
  const routes = {
    status:() => ({runtime_ready:true}),
    targets:() => ({installation_id:"dev", operator_id:"dev", targets:[{id:"dev-target", name:"私聊窗口（预览）", roles:[{token:"dev-role", name:"预览角色"}]}]}),
    [scope + "state"]:() => ({settings:{planning_timezone:"Asia/Shanghai"}, calendar:{items:[]}, occurrences:{items:[]}, state:{values:{}}}),
    [scope + "life"]:() => ({activities:[], config:{timezone:"Asia/Shanghai"}}),
    [scope + "calendar/occurrences"]:() => ({items:[], timezone:"Asia/Shanghai"}),
    [scope + "life/activities"]:() => ({items:[], next_cursor:null}),
  };
  window.AstrBotPluginPage = {
    ready:() => Promise.resolve(),
    apiGet(path, query) {
      console.info("[mock] GET", path, query);
      if (path === scope + "life/wallet") return wallet(query);
      return routes[path] ? wait(routes[path]()) : fail("not_found");
    },
    apiPost:path => fail("read_only_preview:" + path),
  };
  const start = params.get("page");
  if (start) addEventListener("load", () => document.querySelector(`[data-page="${CSS.escape(start)}"]`)?.click());
})();
