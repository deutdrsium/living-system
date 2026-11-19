"use strict";
/* 钱包页渲染模块（pages/我会去好好生活/wallet-view.js）的金额格式化、翻页与各状态。
 * 由 tests/test_good_life_wallet_page.py 用 `node --test` 运行。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..", "..");
const page = path.join(root, "plugins", "astrbot_plugin_good_life", "pages", "我会去好好生活");
globalThis.window = globalThis;
for (const file of [path.join(page, "icons.js"), path.join(page, "wallet-view.js"), path.join(__dirname, "..", "fixtures", "good_life_page", "wallet-fixtures.js")])
  vm.runInThisContext(fs.readFileSync(file, "utf8"), {filename:file});
const W = globalThis.GoodLifeWallet, fixtures = globalThis.GoodLifeWalletFixtures;
const clone = value => structuredClone(value);
const NOW = Date.parse("2026-11-17T12:00:00+08:00");

test("formatMinor splits minor units by string, never by float", () => {
  assert.equal(W.formatMinor(0, 2, "CNY"), "¥0.00");
  assert.equal(W.formatMinor(5, 2, "CNY"), "¥0.05");
  assert.equal(W.formatMinor(10, 2, "CNY"), "¥0.10");
  assert.equal(W.formatMinor(123456789, 2, "CNY"), "¥1,234,567.89");
  assert.equal(W.formatMinor(-120, 2, "CNY"), "−¥1.20");
  assert.equal(W.formatMinor(-120, 2, "CNY", "none"), "¥1.20");
  /* 0.1 + 0.2 一类的浮点误差：29 分、最大安全整数都必须逐位保留。 */
  assert.equal(W.formatMinor(29, 2, "CNY"), "¥0.29");
  assert.equal(W.formatMinor(Number.MAX_SAFE_INTEGER, 2, "CNY"), "¥90,071,992,547,409.91");
  assert.equal(W.formatMinor(1234, 0, "CNY"), "¥1,234");
  assert.equal(W.formatMinor(1234, 3, "CNY"), "¥1.234");
  assert.equal(W.formatMinor(7, 3, "CNY"), "¥0.007");
  assert.equal(W.formatMinor(100, 2, "USD"), "USD 1.00");
  assert.equal(W.formatMinor(100, undefined, "CNY"), "¥1.00", "缺 minor_unit_digits 时按 2 位");
});

test("formatMinor refuses amounts that are not safe integers", () => {
  for (const bad of [1.5, "100", null, undefined, NaN, Infinity, 2 ** 53, -(2 ** 53)])
    assert.equal(W.formatMinor(bad, 2, "CNY"), "—", String(bad));
});

test("flow marks income/refund as inflow, spend as outflow, unknown kinds neutral", () => {
  assert.deepEqual(W.flow({kind:"income", amount_minor:20000}, 2, "CNY"), {flow:"in", label:"收入", amount:"+¥200.00"});
  assert.deepEqual(W.flow({kind:"refund", amount_minor:350}, 2, "CNY"), {flow:"in", label:"退款", amount:"+¥3.50"});
  assert.deepEqual(W.flow({kind:"spend", amount_minor:387}, 2, "CNY"), {flow:"out", label:"支出", amount:"−¥3.87"});
  assert.deepEqual(W.flow({kind:"adjustment", amount_minor:-120}, 2, "CNY"), {flow:"neutral", label:"adjustment", amount:"−¥1.20"});
  assert.deepEqual(W.flow({kind:"spend", amount_minor:"x"}, 2, "CNY"), {flow:"out", label:"支出", amount:"—"});
  assert.equal(W.flow({kind:"spend", amount_minor:100, currency:"USD"}, 2, "CNY").amount, "−USD 1.00");
});

test("normalize recognises the disabled envelope and rejects non-wallet data", () => {
  assert.deepEqual(W.normalize({enabled:false}), {status:"disabled"});
  for (const bad of [null, [], "x", {}, {enabled:"yes"}])
    assert.throws(() => W.normalize(bad), error => error.code === "invalid_response");
  const entry = W.normalize({enabled:true, balance_minor:1, transactions:[null, {transaction_id:"a"}], next_before:""});
  assert.equal(entry.currency, "CNY");
  assert.equal(entry.digits, 2);
  assert.deepEqual(entry.reservations, []);
  assert.deepEqual(entry.transactions, [{transaction_id:"a"}]);
  assert.equal(entry.next_before, null);
});

test("query only sends before when loading more with a cursor", () => {
  assert.deepEqual(W.query(undefined), {limit:20});
  assert.deepEqual(W.query({next_before:"c"}), {limit:20});
  assert.deepEqual(W.query({next_before:"c"}, true), {limit:20, before:"c"});
  assert.deepEqual(W.query({next_before:null}, true), {limit:20});
});

test("merge appends the next page, dedupes and keeps the first page summary", () => {
  const first = W.normalize(clone(fixtures.ok.first));
  const second = clone(fixtures.ok.pages["cursor-page-2"]);
  second.balance_minor = 1;
  second.transactions.unshift(clone(fixtures.ok.first.transactions[19])); /* 两批之间出现重复的一笔 */
  const merged = W.merge({...first, more:true}, second);
  assert.equal(merged.transactions.length, 27);
  assert.equal(new Set(merged.transactions.map(item => item.transaction_id)).size, 27);
  assert.deepEqual(merged.transactions.slice(0, 20), first.transactions);
  assert.equal(merged.balance, 123456789);
  assert.equal(merged.next_before, null);
  assert.equal(merged.more, false);
  assert.equal(merged.moreError, false);
  assert.deepEqual(W.merge(first, {enabled:false}), {status:"disabled"});
});

test("paging through the fixture follows next_before until it runs out", () => {
  const pages = fixtures.ok.pages, requests = [];
  const get = query => { requests.push(query); return clone(query.before ? pages[query.before] : fixtures.ok.first); };
  let entry = W.normalize(get(W.query(undefined)));
  while (entry.next_before) entry = W.merge(entry, get(W.query(entry, true)));
  assert.deepEqual(requests, [{limit:20}, {limit:20, before:"cursor-page-2"}]);
  assert.equal(entry.transactions.length, 27);
  assert.doesNotMatch(W.render({entry, zone:"Asia/Shanghai", now:NOW}), /data-wallet-more/);
});

test("render shows summary, reservations and ledger for wallet data", () => {
  const html = W.render({entry:W.normalize(clone(fixtures.ok.first)), zone:"Asia/Shanghai", now:NOW});
  assert.match(html, /¥1,234,567\.89/);
  assert.match(html, /¥11\.50/);
  assert.match(html, /¥1,234,556\.39/);
  assert.match(html, /已用 <b>¥3\.50<\/b> \/ 额度 <b>¥8\.00<\/b>/);
  assert.match(html, /复核 11月17日 22:30/);
  assert.match(html, /复核时间未定/);
  assert.match(html, /pending_review/, "不认识的 status 原样显示");
  assert.match(html, /data-flow="neutral"[\s\S]*?adjustment/, "不认识的 kind 原样显示且不着色");
  assert.match(html, /\+¥200\.00/);
  assert.match(html, /−¥3\.87/);
  assert.match(html, /data-wallet-more/);
  assert.match(html, /已显示 20 笔，还有更多/);
  /* 只读：除了「加载更多」，页面上没有任何按钮或表单。 */
  assert.doesNotMatch(html, /<form|<input|<select|<textarea/);
  assert.equal((html.match(/<button/g) || []).length, 1);
});

test("render states: loading, disabled, empty, error and load-more feedback", () => {
  const zone = "Asia/Shanghai";
  assert.match(W.render({entry:undefined, zone, now:NOW}), /正在读取钱包/);
  assert.match(W.render({entry:{status:"loading"}, zone, now:NOW}), /aria-busy="true"/);
  const disabled = W.render({entry:{status:"disabled"}, zone, now:NOW});
  assert.match(disabled, /钱包未启用/);
  assert.doesNotMatch(disabled, /<button/);
  const error = W.render({entry:{status:"error", code:"bridge_timeout"}, zone, now:NOW});
  assert.match(error, /钱包暂时无法读取/);
  assert.match(error, /bridge_timeout/);
  assert.match(error, /data-wallet-retry/);
  const empty = W.render({entry:W.normalize(clone(fixtures.empty.first)), zone, now:NOW});
  assert.match(empty, /还没有收支记录/);
  assert.match(empty, /眼下没有预留的额度/);
  assert.match(empty, /¥0\.00/);
  assert.doesNotMatch(empty, /data-wallet-more|已显示全部记录/);
  const entry = W.normalize(clone(fixtures.ok.first));
  assert.match(W.render({entry:{...entry, more:true}, zone, now:NOW}), /data-wallet-more aria-disabled="true">正在读取…/);
  assert.match(W.render({entry:{...entry, moreError:true}, zone, now:NOW}), /更多记录读取失败/);
});

test("render escapes labels and tolerates malformed rows", () => {
  const html = W.render({zone:"Asia/Shanghai", now:NOW, entry:W.normalize({enabled:true, balance_minor:"bad",
    reservations:[{activity_label:"<b>x</b>", limit_minor:100, amount_remaining_minor:500, status:7, review_at:"nope"}],
    transactions:[{kind:null, amount_minor:1, occurred_at:"nope", activity_label:"<img src=x>"}]})});
  assert.doesNotMatch(html, /<b>x<\/b>|<img/);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.match(html, /状态未知/);
  assert.match(html, /时间未记录/);
  assert.match(html, /未分类/);
  assert.doesNotMatch(html, /undefined|null|NaN/);
});
