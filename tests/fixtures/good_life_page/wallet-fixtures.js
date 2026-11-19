/* 钱包页的开发 / 测试样例数据（life/wallet 响应），浏览器预览与 Node 测试共用。
 * 不随插件发布：只在 tests/ 下，由 serve_preview.py 注入预览页。 */
"use strict";
(() => {
  const at = (day, hour, minute) => `2026-11-${String(day).padStart(2, "0")}T${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:00+08:00`;
  const labels = ["便利店买饮料", "食堂午饭", "书店买书", null, "地铁出行"];
  /* 第 index 笔流水（越大越早）。 */
  function transaction(index) {
    const kind = index % 9 === 4 ? "refund" : index % 4 === 0 ? "income" : "spend";
    return {transaction_id:`wallet-tx-${String(index).padStart(3, "0")}`, kind,
      amount_minor:kind === "income" ? 20000 + index * 100 : kind === "refund" ? 350 : 350 + index * 37,
      currency:"CNY", occurred_at:at(17 - Math.floor(index / 3), 21 - (index % 3) * 4, (index * 7) % 60),
      activity_label:kind === "income" ? "零花钱" : labels[index % labels.length]};
  }
  const base = {enabled:true, currency:"CNY", minor_unit_digits:2, balance_minor:123456789, reserved_minor:1150, available_minor:123455639};
  const reservations = [
    {reservation_id:"wallet-rsv-1", activity_label:"便利店买饮料", limit_minor:800, amount_remaining_minor:450, status:"active", review_at:at(17, 22, 30)},
    {reservation_id:"wallet-rsv-2", activity_label:null, limit_minor:350, amount_remaining_minor:350, status:"active", review_at:null},
    {reservation_id:"wallet-rsv-3", activity_label:"书店买书", limit_minor:6000, amount_remaining_minor:0, status:"pending_review", review_at:at(18, 9, 0)},
  ];
  const first = Array.from({length:20}, (_, index) => transaction(index));
  /* 第一批里放一笔不认识的 kind，页面应原样显示、不着色。 */
  first[5] = {transaction_id:"wallet-tx-odd", kind:"adjustment", amount_minor:-120, currency:"CNY", occurred_at:at(16, 12, 0), activity_label:null};
  const second = Array.from({length:7}, (_, index) => transaction(20 + index));
  globalThis.GoodLifeWalletFixtures = {
    /* 有数据：首批 20 笔，next_before 指向第二批；第二批 7 笔后到底。 */
    ok:{first:{...base, reservations, transactions:first, next_before:"cursor-page-2"},
      pages:{"cursor-page-2":{...base, reservations, transactions:second, next_before:null}}},
    empty:{first:{...base, balance_minor:0, reserved_minor:0, available_minor:0, reservations:[], transactions:[], next_before:null}, pages:{}},
    disabled:{first:{enabled:false}, pages:{}},
  };
})();
