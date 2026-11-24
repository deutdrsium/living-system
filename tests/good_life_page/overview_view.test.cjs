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
const input = activities => ({now:at, state:{occurrences:{items:[], start:"2026-11-18", end:"2026-11-19", timezone:"Asia/Shanghai"}}, life:{activities},
  classes:{start:"2026-11-18", end:"2026-11-19", items:[]}});

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

/* 课次：2026-11-23 10:30（Asia/Shanghai），马克思主义哲学导论 09:55–11:35 正在上。 */
const classAt = Date.parse("2026-11-23T02:30:00Z");
const lecture = {id:"s-marx", title:"马克思主义哲学导论", start:"2026-11-23T01:55:00Z", end:"2026-11-23T03:35:00Z",
  place:"示例教学楼", cancelled:false};
const lunch = {id:"lunch", sequence:0, title:"午饭", state:"confirmed", kind:"role_plan",
  start:"2026-11-23T04:00:00Z", end:"2026-11-23T05:00:00Z"};
const classDay = (items, activities=[]) => ({now:classAt, life:{activities},
  state:{occurrences:{items:[lunch], start:"2026-11-23", end:"2026-11-24", timezone:"Asia/Shanghai"}},
  classes:items === null ? null : {start:"2026-11-23", end:"2026-11-24", items}});

test("a class in progress fills its slot instead of an unscheduled gap", () => {
  const data = classDay([lecture]);
  const html = schedule(data);
  assert.match(html, /data-tone="current" aria-current="time"[\s\S]*09:55 – 11:35[\s\S]*马克思主义哲学导论<\/strong><small>课程 · 上课中<\/small><small class="gl-agenda-detail">示例教学楼/);
  assert.doesNotMatch(html, /此刻未安排|data-current="true"|不完整|课表暂未读取/);
  assert.match(html, /13:00 – 24:00[\s\S]*未安排时段/, "gaps are still computed after the merged class");
  assert.match(now(data), /马克思主义哲学导论<\/strong><small>课程 · 上课中 · 09:55 – 11:35 · 示例教学楼/);
  assert.doesNotMatch(now(data), /此刻没有进行中的活动/);
  const later = schedule({...data, now:Date.parse("2026-11-23T01:00:00Z")});
  assert.match(later, /09:00 – 09:55[\s\S]*此刻未安排[\s\S]*下一项 09:55 马克思主义哲学导论/);
  assert.match(later, /马克思主义哲学导论<\/strong><small>课程 · 课表安排/);
  const after = schedule({...data, now:Date.parse("2026-11-23T03:50:00Z")});
  assert.match(after.split('<details class="gl-agenda-past"')[1], /马克思主义哲学导论<\/strong><small>课程 · 时段已过/);
});

test("a cancelled class does not count as scheduled", () => {
  const html = schedule(classDay([{...lecture, cancelled:true}]));
  assert.match(html, /10:30 – 12:00[\s\S]*此刻未安排[\s\S]*下一项 12:00 午饭/);
  assert.doesNotMatch(html, /马克思主义哲学导论/);
});

test("without a class read for today blank time is not called free", () => {
  for (const data of [classDay(null), {...classDay([]), classes:undefined},
    {...classDay([lecture]), classes:{start:"2026-11-22", end:"2026-11-23", items:[lecture]}}]) {
    const html = schedule(data);
    assert.match(html, /日程数据不完整，空白时段暂不判断为空闲/);
    assert.match(html, /课表暂未读取/);
    assert.doesNotMatch(html, /gl-agenda-gap|此刻未安排/);
  }
  assert.match(schedule(classDay([])), /此刻未安排/, "an empty class day is a complete read");
});

test("class sessions and their activity projections are shown once", () => {
  const planned = {activity_id:"class:s-marx", kind:"class", title:"马克思主义哲学导论", state:"planned",
    started_at:lecture.start, due_at:lecture.start, ends_at:lecture.end};
  const current = schedule(classDay([lecture], [planned]));
  assert.equal(current.match(/马克思主义哲学导论<\/strong>/g).length, 1);
  assert.match(current, /课程 · 上课中/);
  const running = {activity_id:"kernel:k1", kind:"class", title:"马克思主义哲学导论", state:"active",
    started_at:"2026-11-23T01:40:00Z", outcome:{state:"in_progress"}};
  const html = schedule(classDay([lecture], [running]));
  assert.equal(html.match(/马克思主义哲学导论<\/strong>/g).length, 1);
  assert.match(html, /data-tone="active"/);
  const ended = {...planned, state:"completed", updated_at:lecture.end, outcome:{state:"completed", source:"simulated"}};
  const past = schedule({...classDay([lecture], [ended]), now:Date.parse("2026-11-23T04:30:00Z")});
  assert.equal(past.match(/马克思主义哲学导论<\/strong>/g).length, 1);
  assert.doesNotMatch(past, /课程 · 时段已过/);
});
