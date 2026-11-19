"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const window = {GoodLifeIcons:{icon:() => ""}};
const file = path.resolve(__dirname, "../../plugins/astrbot_plugin_good_life/pages/我会去好好生活/overview.js");
vm.runInNewContext(fs.readFileSync(file, "utf8"), {window}, {filename:file});
const {now, schedule, agenda, calendarRequest} = window.GoodLifeOverview;
const at = Date.parse("2026-11-18T03:00:00Z");
const cancelled = {activity_id:"cancel", title:"取消的活动", kind:"social", state:"cancelled",
  started_at:"2026-11-18T01:00:00Z", updated_at:"2026-11-18T01:20:00Z"};
const failed = {activity_id:"fail", title:"失败的阅读", kind:"reading", state:"paused",
  started_at:"2026-11-18T01:30:00Z", updated_at:"2026-11-18T02:00:00Z",
  task:{state:"failed", reason:"读取 <原文> 失败"}};
const unknown = {activity_id:"unknown", title:"结果待核对的阅读", kind:"reading", state:"active",
  started_at:"2026-11-18T02:05:00Z", updated_at:"2026-11-18T02:30:00Z",
  task:{state:"unknown", reason:"等待确认"}};
const input = activities => ({now:at, state:{occurrences:{items:[], start:"2026-11-18", end:"2026-11-19", timezone:"Asia/Shanghai"}}, life:{activities}});

test("cancelled and failed activities are elapsed while ongoing readings disclose uncertain execution", () => {
  const old = {...cancelled, activity_id:"old", title:"昨天的活动", updated_at:"2026-11-17T03:00:00Z"};
  const data = {...input([old, cancelled, failed, unknown]), openReasons:new Set(["agenda:activity:fail"])};
  assert.deepEqual(Array.from(agenda(data), item => [item.label, item.tone]),
    [["已取消", "elapsed"], ["失败", "elapsed"], ["进行中 · 待确认", "active"]]);
  const html = schedule(data);
  assert.match(html, /已落幕 2 项/);
  assert.match(html, /取消的活动<\/strong><small>已取消<\/small>\s*<\/div>/);
  assert.match(html, /<details data-overview-reason="agenda:activity:fail" open>\s*<summary><small>查看原因<\/small><\/summary>.*读取 &lt;原文&gt; 失败/s);
  const [timeline, history] = html.split('<details class="gl-agenda-past"');
  assert.match(timeline, /结果待核对的阅读<\/strong><small>进行中 · 待确认<\/small>/);
  assert.doesNotMatch(timeline, /取消的活动|失败的阅读|gl-agenda-gap/);
  assert.doesNotMatch(history, /昨天的活动|结果待核对的阅读|aria-current="time"/);
});

test("current snapshot keeps unresolved readings visible across days and failed readings distinct from live activities", () => {
  const active = {...failed, activity_id:"running", title:"正在进行的活动", state:"active", task:null};
  const oldUnknown = {...unknown, started_at:"2026-11-17T01:00:00Z", updated_at:"2026-11-17T02:00:00Z"};
  const html = now(input([active, cancelled, failed, oldUnknown]));
  assert.match(html, /data-state="active".*正在进行的活动/s);
  assert.match(html, /最近活动 · 失败的阅读 · 失败/);
  assert.match(html, /结果待核对的阅读<\/strong>\s*<small>阅读 · 进行中 · 待确认/);
  assert.match(html, /<details data-overview-reason="now:activity:unknown">.*等待确认/s);
  const timeline = schedule(input([oldUnknown]));
  assert.match(timeline, /data-tone="active" aria-current="time"[\s\S]*进行中 · 待确认/);
  assert.doesNotMatch(timeline, /已落幕|gl-agenda-gap/);
  const cancelledHtml = now(input([{...cancelled, checkpoint:"没有成功读取外部内容"}]));
  assert.match(cancelledHtml, /最近活动 · 取消的活动 · 已取消/);
  assert.match(cancelledHtml, /<summary><small>查看记录<\/small><\/summary>.*没有成功读取外部内容/s);
});

test("paused reading awaiting confirmation remains visible after midnight without occupying the current slot", () => {
  const paused = {...unknown, state:"paused", started_at:"2026-11-17T15:30:00Z", updated_at:"2026-11-17T15:59:00Z"};
  const data = input([paused]);
  assert.match(now(data), /data-state="paused"[\s\S]*已暂停 · 待确认/);
  assert.match(now(data), /查看原因[\s\S]*等待确认/);
  assert.match(schedule(data), /接下来没有安排/);
  assert.doesNotMatch(schedule(data), /data-tone="active"/);
});

test("projected non-reading outcomes show reasons, unknown execution and simulation", () => {
  const data = input([]);
  data.life.overview_activities = {items:[
    {...cancelled, activity_id:"city", title:"回程", kind:"city_trip",
      outcome:{state:"cancelled", source:"simulated", reason_code:"delay_abandoned", reason_label:"延误后放弃"}},
    {activity_id:"purchase", title:"买饮料", kind:"purchase", state:"unknown",
      started_at:"2026-11-17T02:15:00Z", updated_at:"2026-11-18T02:30:00Z",
      outcome:{state:"unknown", source:"simulated", reason_label:"付款 <待核对>"}},
    {activity_id:"study", title:"整理笔记", kind:"study", state:"failed", updated_at:"2026-11-18T02:40:00Z",
      outcome:{state:"failed", reason_code:"provider_unavailable", reason_label:"服务不可用"}},
    {activity_id:"campus", title:"去教室", kind:"campus_trip", state:"active", started_at:"2026-11-18T02:45:00Z",
      outcome:{state:"in_progress", source:"simulated"}},
  ]};
  const html = schedule(data);
  assert.match(html, /整理笔记<\/strong><small>失败 · 服务不可用/);
  assert.match(html, /回程<\/strong><small>已取消 · 延误后放弃 · 模拟/);
  assert.match(now(data), /去教室<\/strong>\s*<small>校内出行 · 进行中 · 模拟/);
  assert.match(now(data), /买饮料<\/strong>\s*<small>购买 · 结果待确认 · 付款 &lt;待核对&gt; · 模拟/);
  assert.doesNotMatch(now(data), /购买 · 进行中|付款 <待核对>/);
});

test("cancelled calendar time does not occupy a slot and absent domain data stays empty", () => {
  const data = input([]);
  data.state.occurrences.items.push({id:"off", sequence:0, title:"取消的约定", state:"cancelled",
    start:"2026-11-18T04:00:00Z", end:"2026-11-18T05:00:00Z",
    outcome:{state:"cancelled", reason_code:"cancelled", reason_label:"被取消"}});
  const [timeline, history] = schedule(data).split('<details class="gl-agenda-past"');
  assert.match(timeline, /接下来没有安排/);
  assert.doesNotMatch(timeline, /取消的约定/);
  assert.match(history, /取消的约定<\/strong><small>被取消/);
  assert.equal(schedule(input([])), schedule({...input([]), life:{activities:[], overview_activities:{items:[], partial:false}}}));
});

test("recent activity uses the outcome time instead of a cancelled appointment's scheduled end", () => {
  const data = input([failed]);
  const appointment = {id:"evening", sequence:0, title:"晚间约定", state:"cancelled",
    start:"2026-11-18T10:00:00Z", end:"2026-11-18T11:00:00Z", outcome_at:"2026-11-18T01:10:00Z",
    outcome:{state:"cancelled", reason_code:"cancelled", reason_label:"被取消"}};
  data.state.occurrences.items.push(appointment);
  assert.match(schedule(data), /晚间约定<\/strong><small>被取消/);
  assert.match(now(data), /最近活动 · 失败的阅读 · 失败[\s\S]*读取 &lt;原文&gt; 失败/);
  appointment.outcome_at = "2026-11-18T02:30:00Z";
  assert.match(now(data), /最近活动 · 晚间约定 · 被取消/);
  delete appointment.outcome_at;
  assert.match(now(data), /最近活动 · 失败的阅读 · 失败/);
});

test("calendar coverage rejects an expired or ambiguous range in the role timezone", () => {
  const before = Date.parse("2026-11-18T15:44:00Z"), after = Date.parse("2026-11-18T16:16:00Z");
  const data = {...input([]), now:before};
  assert.equal(calendarRequest(data, before).start, "2026-11-18");
  assert.equal(calendarRequest(data, after).start, "2026-11-19");
  assert.match(schedule(data), /接下来没有安排/);
  const nextDay = {...data, now:after};
  assert.match(schedule(nextDay), /日历快照尚未覆盖今天/);
  assert.doesNotMatch(schedule(nextDay), /gl-agenda-gap/);
  assert.match(now(nextDay), /今天后续的安排暂不确定/);
  const ambiguous = {...nextDay, state:{occurrences:{items:[]}}};
  assert.doesNotMatch(schedule(ambiguous), /gl-agenda-gap/);
  const changedZone = {...data, state:{...data.state, settings:{planning_timezone:"UTC"}}};
  assert.doesNotMatch(schedule(changedZone), /gl-agenda-gap/);
});

test("a partial activity page cannot be interpreted as an idle period", () => {
  const data = input([]);
  data.life.next_activity = "more";
  assert.match(now(data), /当前活动尚未完整读取/);
  assert.match(schedule(data), /部分生活活动未读取/);
  assert.doesNotMatch(schedule(data), /接下来没有安排|今日暂无安排/);
});
