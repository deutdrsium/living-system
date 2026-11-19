/* Account setup stays inside the authenticated cottage page bridge. */
"use strict";
window.CompanionSocialPage = (() => {
  const product = window.CompanionProduct;
  const names = {bilibili:"B 站", xiaohongshu:"小红书", zhihu:"知乎", douban:"豆瓣", brightdata:"Google 搜索 · Bright Data"};
  const stateLabel = key => ({missing:"未保存", unverified:"已保存 · 尚未验证", passed:"已保存 · 上次验证通过",
    failed:"已保存 · 上次验证未通过", unavailable:"已保存 · 验证暂不可用"}[status.verification?.[key]] || "状态待检查");
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  let root, status, path = "", scope = "", notice = "", busy = false, qr = "", timer, generation = 0;
  const active = n => root?.isConnected && n === generation && scope === product.scopeKey();
  function selectedPath() {
    const route = product.role()?.social_api, prefix = "/astrbot_plugin_private_companion/";
    const result = typeof route === "string" && route.startsWith(prefix) ? route.slice(prefix.length) : "";
    return result && !/[?#%\\:]/.test(result) && !result.split("/").some(part => !part || part === "." || part === "..") ? result : "";
  }
  async function request(method, body) {
    if (!path) throw new Error("social route unavailable");
    return product.request(method, path + (method === "GET" ? "" : "/command"), body, {timeoutMs: 90000});
  }
  function render() {
    if (!root) return;
    if (!status) {
      root.innerHTML = `<p role="status">${esc(notice || "正在读取账号状态…")}</p>${notice ? '<button class="button small" data-social="refresh">重新读取</button>' : ""}`;
      return;
    }
    const rows = Object.entries(names).map(([key, name]) => {
      let buttons = ["bilibili", "xiaohongshu", "zhihu"].includes(key)
        ? `<button class="button small" data-social="login" data-platform="${key}">扫码登录</button> <button class="button small" data-social="check" data-platform="${key}">检查登录</button>`
        : `<label>${key === "douban" ? "豆瓣网页 Cookie" : "Bright Data API Token"}<input type="password" autocomplete="off" data-secret="${key}" placeholder="只在此处填写，不会显示已保存的内容"></label> <button class="button small" data-social="credential" data-platform="${key}">保存</button> <button class="button small" data-social="check" data-platform="${key}">${key === "douban" ? "验证会话" : "测试 Google（1 次请求）"}</button>`;
      if (key === "zhihu") buttons += `<label>知乎站内搜索授权 · ${stateLabel("zhihu_search")}<input type="password" autocomplete="off" data-secret="zhihu_search" placeholder="知乎开放平台 Access Secret"></label> <button class="button small" data-social="zhihu_search" data-platform="zhihu_search">验证并保存</button>`;
      if (key === "zhihu") {
        const info = status.zhihu_session;
        const fields = info ? Object.entries(info.cookies || {}).map(([name, exists]) => `${esc(name)}：${exists ? "可用" : "缺失或不适用"}`).join("；") : "暂无会话信息";
        buttons += `<small>已连接账号：${esc(info?.account?.name || "未保存")} ${esc(info?.account?.profileUrl || "")}；保存时间：${info?.savedAt ? esc(new Date(info.savedAt).toLocaleString()) : "未知"}；User-Agent：${info?.userAgentSaved ? "已保存" : "未保存"}；${fields}${info?.consistent === false ? "；会话更新未完成，请重新导入" : ""}</small>
        <label>角色的知乎会话 JSON（保留 Cookie 域名和有效期）<textarea autocomplete="off" data-secret="zhihu_session" placeholder="粘贴仅含知乎 cookies 和 origins 的浏览器 storageState JSON"></textarea></label>
        <label>同一浏览器的 User-Agent<input type="text" autocomplete="off" data-secret="zhihu_ua"></label>
        <button class="button small" data-social="zhihu_session" data-platform="zhihu_session">验证并保存完整会话</button>
        <label>或粘贴知乎 Cookie 请求头（域名和有效期将无法保留）<input type="password" autocomplete="off" data-secret="zhihu_cookie"></label>
        <button class="button small" data-social="zhihu_cookie_ua" data-platform="zhihu_cookie">验证并连接知乎</button>`;
      }
      if (key === "brightdata") buttons += `<label>Bright Data 搜索区名称<input type="text" autocomplete="off" data-secret="google_zone" value="${esc(status.google_zone || "mcp_serp")}"></label> <button class="button small" data-social="google_zone" data-platform="google_zone">保存搜索区</button>`;
      const blocked = status.blocked?.includes(key) ? `<small>平台曾要求验证或限流，互动已暂停；请先处理平台提示。</small><button class="button small" data-social="resume" data-platform="${key}">处理后恢复互动</button>` : "";
      const accountIssue = key === "zhihu" && status.verification?.zhihu === "failed" ? '<small>知乎账号读取目前暂停；请先检查登录状态。</small>' : "";
      return `<div class="perception-setting"><strong>${esc(name)} · ${stateLabel(key)}</strong><small>${esc(status.capabilities[key])}</small><div>${buttons}</div>${accountIssue}${blocked}</div>`;
    }).join("");
    root.innerHTML = `<p>首次设置账号时会为当前私聊角色启用自主浏览；角色会在日程空档按自己的兴趣决定是否浏览。已读内容可以进入她的日记与记忆。</p>
      <button class="button small" data-social="enabled" data-enabled="${!status.enabled}">${status.enabled ? "暂停自主浏览" : "启用自主浏览"}</button>
      <span> ${status.enabled ? "当前角色已启用" : "当前角色未启用"}</span>
      ${rows}<p class="perception-note">验证状态反映上一次检查结果，会话仍可能过期。豆瓣在浏览器登录后，可从浏览器开发者工具中复制豆瓣请求的 Cookie，粘贴到上方安全入口。Bright Data 填写 API Token。不要把凭据发到聊天里。</p>
      ${qr ? `<img src="${esc(qr)}" alt="账号登录二维码" style="width:220px;max-width:100%">` : ""}
      <p role="status" aria-live="polite">${esc(notice)}</p>`;
    for (const button of root.querySelectorAll("button")) button.disabled = busy;
  }
  async function load(ticket) {
    try { const value = await request("GET"); if (active(ticket)) {status = value; render();} }
    catch (_) { if (active(ticket)) {notice = "账号状态暂不可用，请确认已登录管理界面并选择角色的私聊角色。"; render();} }
  }
  async function check(platform, ticket, refresh = false) {
    const result = await request("POST", {action:"check", platform});
    if (!active(ticket)) return;
    notice = result.message || "状态检查完成";
    if (typeof result.qr === "string" && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(result.qr)) qr = result.qr;
    if (result.logged_in || result.terminal) { clearTimeout(timer); timer = null; qr = ""; }
    if (result.logged_in || refresh) await load(ticket);
    render();
    return result;
  }
  async function clicked(event) {
    const button = event.target.closest("[data-social]");
    if (!button || busy) return;
    const ticket = generation, action = button.dataset.social, platform = button.dataset.platform;
    const value = ["credential", "zhihu_search", "zhihu_cookie_ua", "zhihu_session", "google_zone"].includes(action) ? root.querySelector(`[data-secret="${platform}"]`)?.value : null;
    const userAgent = ["zhihu_cookie_ua", "zhihu_session"].includes(action) ? root.querySelector('[data-secret="zhihu_ua"]')?.value?.trim() : null;
    busy = true;
    try {
      if (action === "refresh") {
        scope = product.scopeKey(); path = selectedPath(); notice = ""; await load(ticket);
      } else if (action === "credential") {
        if (!value?.trim()) { notice = "请先填写凭据。"; return; }
        await request("POST", {action, platform, value});
        notice = "凭据已保存，尚未验证是否有效。";
        await load(ticket);
      } else if (action === "zhihu_search") {
        if (!value?.trim()) { notice = "请先填写知乎开放平台 Access Secret。"; return; }
        await request("POST", {action, value});
        notice = "知乎搜索密钥已验证并保存。"; await load(ticket);
      } else if (action === "zhihu_cookie_ua") {
        if (!value?.trim()) { notice = "请先填写已登录知乎的 Cookie。"; return; }
        if (!userAgent) { notice = "请填写同一浏览器的 User-Agent。"; return; }
        const result = await request("POST", {action, value, userAgent});
        notice = result.message || "知乎连接检查已结束。"; await load(ticket);
      } else if (action === "zhihu_session") {
        if (!value?.trim() || !userAgent) { notice = "请填写会话 JSON 和同一浏览器的 User-Agent。"; return; }
        let storageState; try { storageState = JSON.parse(value); } catch (_) { notice = "会话 JSON 格式无效。"; return; }
        const result = await request("POST", {action, storageState, userAgent});
        notice = result.message || "知乎连接检查已结束。"; await load(ticket);
      } else if (action === "google_zone") {
        if (!value?.trim()) { notice = "请填写搜索区名称。"; return; }
        await request("POST", {action, value:value.trim()});
        notice = "搜索区已保存。"; await load(ticket);
      } else if (action === "enabled") {
        await request("POST", {action, enabled:button.dataset.enabled === "true"});
        notice = "设置已保存。"; await load(ticket);
      } else if (action === "resume") {
        await request("POST", {action, platform});
        notice = "互动已恢复；平台仍有限制时会再次暂停。"; await load(ticket);
      } else if (action === "check") {
        await check(platform, ticket, true);
      } else if (action === "login") {
        clearTimeout(timer); timer = null; qr = "";
        notice = "正在请求二维码，请稍候…"; render();
        const result = await request("POST", {action, platform});
        if (!active(ticket)) return;
        notice = result.message || "请扫码登录";
        if (typeof result.qr === "string" && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(result.qr)) qr = result.qr;
        if (result.ok === false) return;
        let count = 0;
        const poll = async () => {
          if (!active(ticket)) { timer = null; return; }
          if (++count > 75) { timer = null; qr = ""; notice = "扫码等待已结束，请重新获取二维码。"; render(); return; }
          let result;
          try { result = await check(platform, ticket); }
          catch (_) { if (active(ticket)) { qr = ""; notice = "登录状态检查失败，请重新扫码。"; render(); } return; }
          if (!result?.logged_in && !result?.terminal && active(ticket)) timer = setTimeout(poll, 4000);
        };
        timer = setTimeout(poll, 4000);
      }
    } catch (_) { if (active(ticket)) notice = "操作未完成，请检查平台状态或稍后重试。"; }
    finally { if (active(ticket)) {busy = false; render();} }
  }
  function mount(node) {
    unmount(); root = node; scope = product.scopeKey();
    path = selectedPath();
    root.addEventListener("click", clicked); load(generation);
  }
  function unmount() { generation++; clearTimeout(timer); timer = null; root?.removeEventListener("click", clicked);
    root = null; status = null; path = ""; scope = ""; notice = ""; qr = ""; busy = false; }
  return {mount, unmount};
})();
