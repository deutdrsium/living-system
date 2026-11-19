/* 钱包页：余额 / 预留 / 可用、预留中的额度、收支记录（只读）。
 *
 * 只投影 app.js 读到的 life/wallet 结果，不发请求、不持有状态：
 *   - 金额一律是整数最小单位（minor），按 minor_unit_digits 用字符串拆出小数位，
 *     不经过浮点运算；不是安全整数的金额显示为「—」，不猜；
 *   - 收入 / 退款记为流入，支出记为流出；不认识的 kind / status 原样显示，不着色、不报错；
 *   - enabled 为 false 时只有这一个字段，页面显示「钱包未启用」。
 * 收支记录按 next_before 向后翻页，merge() 把下一批接到已读到的记录后面。 */
"use strict";
window.GoodLifeWallet = (() => {
  const icon = name => window.GoodLifeIcons.icon(name);
  const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const text = value => typeof value === "string" ? value.trim() : "";
  const PAGE_SIZE = 20;
  const symbols = {CNY:"¥"};
  const kinds = {income:{label:"收入", flow:"in"}, refund:{label:"退款", flow:"in"}, spend:{label:"支出", flow:"out"}};
  const statuses = {active:{label:"预留中", tone:"active"}, released:{label:"已释放", tone:"done"}};

  /* === 金额 === */
  const digitsOf = value => Number.isSafeInteger(value) && value >= 0 && value <= 8 ? value : 2;
  /* 整数最小单位 → 展示文本。sign 为 "auto" 时负数带「−」；为 "none" 时只显示绝对值。 */
  function formatMinor(value, digits = 2, currency = "CNY", sign = "auto") {
    if (!Number.isSafeInteger(value)) return "—";
    const places = digitsOf(digits), raw = String(Math.abs(value)).padStart(places + 1, "0");
    const whole = raw.slice(0, raw.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const unit = typeof currency === "string" && currency ? symbols[currency] ?? currency + " " : "";
    return (sign === "auto" && value < 0 ? "−" : "") + unit + whole + (places ? "." + raw.slice(-places) : "");
  }
  /* 一笔流水的方向与带符号金额；不认识的 kind 不定方向，金额按原值显示。 */
  function flow(item, digits, currency) {
    const known = kinds[item?.kind], cur = text(item?.currency) || currency;
    if (!known) return {flow:"neutral", label:text(item?.kind) || "未分类", amount:formatMinor(item?.amount_minor, digits, cur)};
    const amount = formatMinor(item.amount_minor, digits, cur, "none");
    return {flow:known.flow, label:known.label, amount:amount === "—" ? amount : (known.flow === "in" ? "+" : "−") + amount};
  }

  /* === 时间 === */
  function validZone(zone) {
    if (typeof zone !== "string" || !zone) return "Asia/Shanghai";
    try { new Intl.DateTimeFormat("en-US", {timeZone:zone}); return zone; } catch { return "Asia/Shanghai"; }
  }
  function when(value, zone, now = Date.now()) {
    const at = typeof value === "string" ? Date.parse(value) : NaN;
    if (!Number.isFinite(at)) return "";
    const year = at => new Intl.DateTimeFormat("en-US", {timeZone:zone, year:"numeric"}).format(new Date(at));
    return new Intl.DateTimeFormat("zh-CN", {timeZone:zone, ...(year(at) !== year(now) ? {year:"numeric"} : {}),
      month:"long", day:"numeric", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).format(new Date(at));
  }

  /* === 读取结果 === */
  /* life/wallet 的一页 → 页面条目；不像钱包响应的数据抛 invalid_response。 */
  function normalize(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) throw Object.assign(new Error("invalid_response"), {code:"invalid_response"});
    if (data.enabled === false) return {status:"disabled"};
    if (data.enabled !== true) throw Object.assign(new Error("invalid_response"), {code:"invalid_response"});
    const list = value => Array.isArray(value) ? value.filter(item => item && typeof item === "object") : [];
    return {status:"ok", currency:text(data.currency) || "CNY", digits:digitsOf(data.minor_unit_digits),
      balance:data.balance_minor, reserved:data.reserved_minor, available:data.available_minor,
      reservations:list(data.reservations), transactions:list(data.transactions),
      next_before:typeof data.next_before === "string" && data.next_before ? data.next_before : null};
  }
  /* 接上下一批：汇总与预留以首批为准（与已显示的记录同一时刻），流水按 transaction_id 去重。 */
  function merge(entry, data) {
    const next = normalize(data);
    if (next.status !== "ok" || entry?.status !== "ok") return next;
    const seen = new Set(entry.transactions.map(item => item.transaction_id).filter(Boolean));
    const added = next.transactions.filter(item => !item.transaction_id || !seen.has(item.transaction_id));
    return {...entry, transactions:[...entry.transactions, ...added], next_before:next.next_before, more:false, moreError:false};
  }
  /* 请求参数：首批只带 limit，翻页再带 before。 */
  const query = (entry, more = false) => more && entry?.next_before ? {limit:PAGE_SIZE, before:entry.next_before} : {limit:PAGE_SIZE};

  /* === 标记 === */
  const head = (title, note) => `<header class="gl-card-head"><h2>${e(title)}</h2>${note ? `<span class="gl-card-note">${e(note)}</span>` : ""}</header>`;
  const state = (glyph, title, body, action = "") => `<div class="gl-empty-state">${icon(glyph)}<div><strong>${e(title)}</strong>${body ? `<p>${e(body)}</p>` : ""}${action}</div></div>`;

  function summary(entry) {
    const money = value => formatMinor(value, entry.digits, entry.currency);
    const stat = (kind, label, value, note) => `<div class="gl-wallet-stat" data-kind="${kind}"><dt>${label}</dt>
      <dd><strong>${e(money(value))}</strong>${note ? `<small>${e(note)}</small>` : ""}</dd></div>`;
    return `<article class="gl-card gl-wallet-summary">${head("账户概况", `只读 · ${entry.currency}`)}
      <dl class="gl-wallet-stats">${stat("balance", "余额", entry.balance, "账上的全部金额")}${stat("reserved", "预留中", entry.reserved, "已为进行中的活动留出")}
        ${stat("available", "可用", entry.available, "余额减去预留")}</dl></article>`;
  }

  function reservations(entry, zone, now) {
    const money = value => formatMinor(value, entry.digits, entry.currency);
    const rows = entry.reservations.map(item => {
      const status = statuses[item.status] || {label:text(item.status) || "状态未知", tone:"neutral"};
      const limit = item.limit_minor, remaining = item.amount_remaining_minor;
      const valid = Number.isSafeInteger(limit) && Number.isSafeInteger(remaining) && limit > 0 && remaining >= 0 && remaining <= limit;
      const used = valid ? limit - remaining : null, review = when(item.review_at, zone, now);
      return `<li class="gl-wallet-reserve" data-tone="${status.tone}">
        <div class="gl-wallet-reserve-head"><strong>${e(text(item.activity_label) || "未命名活动")}</strong><span class="gl-chip${status.tone === "active" ? " is-ok" : " is-muted"}">${e(status.label)}</span></div>
        <p class="gl-wallet-reserve-amount"><span>已用 <b>${e(valid ? money(used) : "—")}</b> / 额度 <b>${e(money(limit))}</b></span>
          <span>剩余 ${e(money(remaining))}</span></p>
        ${valid ? `<span class="gl-wallet-meter" role="img" aria-label="${e(`已用 ${money(used)}，额度 ${money(limit)}`)}"><i style="width:${Math.round(used * 100 / limit)}%"></i></span>` : ""}
        <small>${icon("clock")}${e(review ? `复核 ${review}` : "复核时间未定")}</small></li>`;
    });
    return `<article class="gl-card gl-wallet-reserves">${head("预留中的额度", `${rows.length} 项`)}
      ${rows.length ? `<ul class="gl-wallet-reserve-list">${rows.join("")}</ul>`
        : state("wallet", "眼下没有预留的额度", "开始需要花钱的活动时，会先在这里留出一笔额度，结束后释放。")}</article>`;
  }

  function ledger(entry, zone, now) {
    const rows = entry.transactions.map(item => {
      const info = flow(item, entry.digits, entry.currency), at = when(item.occurred_at, zone, now), label = text(item.activity_label);
      /* 没有活动名时标题就是收支类型，副标题只写时间，不重复类型。 */
      const time = at ? `<time datetime="${e(item.occurred_at)}">${e(at)}</time>` : "时间未记录";
      return `<li class="gl-ledger-item" data-flow="${info.flow}"><span class="gl-ledger-glyph" aria-hidden="true">${icon(info.flow === "in" ? "arrow-in" : info.flow === "out" ? "arrow-out" : "wallet")}</span>
        <div class="gl-ledger-body"><strong>${e(label || info.label)}</strong>
          <small>${label ? `${e(info.label)} · ` : ""}${time}</small></div>
        <span class="gl-ledger-amount">${e(info.amount)}</span></li>`;
    });
    const more = entry.next_before ? `<p class="gl-ledger-more"><button type="button" class="gl-chip-button" data-wallet-more${entry.more ? ' aria-disabled="true"' : ""}>${entry.more ? "正在读取…" : "加载更多"}</button></p>`
      : rows.length ? `<p class="gl-caption">已显示全部记录。</p>` : "";
    const moreError = entry.moreError ? `<p class="gl-caption gl-warn" role="status">更多记录读取失败，可再试一次。</p>` : "";
    return `<article class="gl-card gl-wallet-ledger" aria-live="polite">${head("收支记录", `按 ${zone} 显示`)}
      ${rows.length ? `<div class="gl-agenda-meta"><span>已显示 ${rows.length} 笔${entry.next_before ? "，还有更多" : ""}</span><span>最近的在前</span></div>
        <ul class="gl-ledger" aria-label="收支记录">${rows.join("")}</ul>`
        : state("wallet", "还没有收支记录", "有收入或花销时，会按时间记在这里。")}${moreError}${more}</article>`;
  }

  /* 整个钱包页。entry: undefined / {status:"loading"} / {status:"error", code} / {status:"disabled"} / normalize() 的结果。 */
  function render(input = {}) {
    const entry = input.entry, zone = validZone(input.zone), now = input.now ?? Date.now();
    if (!entry || entry.status === "loading")
      return `<article class="gl-card gl-wallet-only" aria-busy="true">${head("钱包")}<p class="gl-empty">正在读取钱包…</p></article>`;
    if (entry.status === "disabled")
      return `<article class="gl-card gl-wallet-only">${head("钱包")}${state("wallet", "钱包未启用",
        "这个角色的钱包功能尚未开启。开启后，这里会显示余额、预留中的额度与收支记录。")}</article>`;
    if (entry.status !== "ok")
      return `<article class="gl-card gl-wallet-only" role="status">${head("钱包")}${state("alert", "钱包暂时无法读取",
        entry.code ? `错误代码：${entry.code}。可以稍后再试。` : "可以稍后再试。", `<button type="button" class="gl-chip-button gl-wallet-retry" data-wallet-retry>重新读取</button>`)}</article>`;
    return summary(entry) + ledger(entry, zone, now) + reservations(entry, zone, now);
  }

  return {PAGE_SIZE, formatMinor, flow, when, normalize, merge, query, render};
})();
