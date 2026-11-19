"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const names = { planned: "待开始", active: "进行中", paused: "已暂停", completed: "已完成", complete: "已完成", cancelled: "已取消", deleted: "已删除", absent: "尚未书写", written: "正文已保存", succeeded: "成功", failed: "失败", unknown: "结果未知", accepted: "已接纳", uncertain: "结果未知", running: "运行中", new: "尚未启动", stopped: "已停止", draining: "正在停止" };
  const coverageNames = {text:"正文文字", image:"图像", image_description:"视觉转述", metadata:"仅元数据", skipped:"已跳过"};
  const errors = { dashboard_identity_required: "请从已登录的 AstrBot 管理界面打开本页面。", staging_not_initialized: "角色数据尚未就绪，请在「账号与会话」保存角色映射；已保存可重新读取状态。",
    host_not_running: "请先启动当前 V2 配置。", life_not_bound: "请先启动当前 V2 配置，以载入原角色。", authorization_revoked: "执行权限已关闭或撤销，请确认生活日程设置。",
    authorization_missing: "请先启用生活日程与执行权限。", life_view_stale: "记录或权限已经变化，请刷新后重新确认。", life_reading_unresolved: "原阅读结果尚未确定，不能重复调用。",
    life_reading_active: "当前片段仍在阅读，请等待完成。", life_reading_paced: "当前阅读时间尚未结束，请稍后再开始下一段。", life_sleeping: "角色正在睡眠，请先醒来再阅读。", life_command_invalid: "生活记录或操作信息已变化，请刷新后重新尝试。", life_epub_invalid: "无法读取 EPUB，请检查文件是否损坏、加密或包含可阅读文字。", life_epub_too_large: "EPUB 文件不能超过 20 MB，正文不能超过 1000 万字符。", journal_trigger_unresolved: "原书写仍在进行、结果未知或已经成功，不能重复执行。",
    configuration_revision_conflict: "设置已变化，请刷新后重新保存。", core_access_required: "当前账号没有本体管理权限。", source_revoked: "原文已撤回，依赖它的内容已停止使用。", life_activity_not_removable: "只有已完成或已取消的阅读记录可以删除。", life_journal_snapshot_unavailable: "生活与日记数据暂未同步完成，请稍后再试。",
    bridge_timeout: "等待超时，结果未确认，可能已在处理；请先刷新核对，确认未完成再重试。" };
  let bridge, state, selected, dialogAction, dialogItem, busy = false, dirty = false, tab = "archive";
  let datePages = [null], datePage = 0, activityCursor = null, polling = null, archiveKind = "reading_note", loadSequence = 0;
  let readGeneration;
  /* 每个板块查询自己的 archive_kind（后端接受 diary / reading_note / social_note）。
     宿主的生活读取只放行 reading_note、social_note 两类 journals，diary 查到的日记会被过滤，
     所以内嵌时不再以 diary 起步。作息与设置不展示笔记，沿用 reading_note。 */
  const sectionArchiveKinds = {reading: "reading_note", notes: "reading_note", browsing: "social_note", settings: "reading_note"};
  const pending = new Map();
  const booleanFields = ["enabled", "diary_enabled", "sleep_enabled", "reading_enabled", "derive_enabled", "social_recommendation_enabled"];
  const numberFields = ["sleep_minutes", "sleep_variation_minutes", "sleep_window_minutes", "reading_chunk_chars",
    "reading_speed_chars_per_minute", "social_interval_minutes", "social_existing_percent", "social_related_percent", "social_new_percent", "social_random_read_percent"];
  const configFields = [...booleanFields, "timezone", "diary_time", "sleep_time", "sleep_timezone", ...numberFields];
  const date = value => value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
  const kind = item => item.kind === "dream" ? "梦境" : item.kind === "reading_note" ? "读书笔记" : item.kind === "social_note" ? "上网笔记" : "日记";
  const display = () => bridge?.getContext?.()?.display || {};
  function roleName() {
    if (display().role_name || state.role_name) return display().role_name || state.role_name;
    const name = state.role_id;
    return typeof name === "string" && !/^(?:role[-_:]|staging[-_:]|dashboard:)|^[a-f0-9]{24,}$/i.test(name)
      ? name : "当前角色";
  }
  function localizedError(value) {
    const code = value?.code || value?.data?.code || value?.response?.data?.code || value?.message;
    return Object.assign(new Error(errors[code] || "操作未完成，请刷新确认记录状态后重试。"), {code});
  }
  function operationKey() {
    const webCrypto = window.crypto;
    if (typeof webCrypto?.randomUUID === "function") return webCrypto.randomUUID();
    if (typeof webCrypto?.getRandomValues === "function") {
      return Array.from(webCrypto.getRandomValues(new Uint8Array(16)),
        value => value.toString(16).padStart(2, "0")).join("");
    }
    return "life-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }
  function unwrap(response) {
    const wrapped = ["ok", "error"].includes(response?.status) && response?.data !== undefined ? response : ["ok", "error"].includes(response?.data?.status) && response?.data?.data !== undefined ? response.data : null;
    if (wrapped?.status === "error") throw localizedError(wrapped);
    // Current AstrBot bridge returns the already-unwrapped data; older bridges return the envelope.
    const data = wrapped ? wrapped.data : response;
    if (!data || typeof data !== "object") throw new Error("未收到执行结果，请刷新确认后重试本次操作。");
    return data;
  }
  function notice(text, error = false) { $("notice").textContent = text; $("notice").className = error ? "error" : ""; $("notice").hidden = !text; }
  function controls(value) {
    busy = value;
    document.querySelectorAll("button, select").forEach(b => { b.disabled = value; });
    $("close-dialog").disabled = false;
    $("newer").disabled = value || datePage === 0; $("older").disabled = value || !state?.next_date;
    $("more-activities").disabled = value || !state?.next_activity;
    $("first-activities").disabled = value || !activityCursor;
    /* 只有能翻页时才显示翻页条；没有下一批也不在首批时整条收起。 */
    $("activity-pages").classList.toggle("is-idle", !state?.next_activity && !activityCursor);
    $("newer").parentElement.classList.toggle("is-idle", datePage === 0 && !state?.next_date);
    $("start-host").disabled = value || state?.host_state === "running";
    $("stop-host").disabled = value || !state || ["new", "stopped"].includes(state.host_state);
    ["write-diary", "start-sleep"].forEach(id => { $(id).disabled = value || !state?.config?.enabled || state?.host_state !== "running"; });
    /* 配置没读到（state 为 null）时别让用户对着能点的表单白点：整组禁用，
       具体原因由 load() 的提示给出，刷新成功后自动恢复。 */
    $("config-form").querySelectorAll("input, button").forEach(el => { el.disabled = value || !state; });
  }
  async function api(method, body) {
    try {
      const response = method === "GET" ? await bridge.apiGet("life", body) : await bridge.apiPost("life/command", body, body.action === "add_epub" ? {timeoutMs: 120000} : undefined);
      return unwrap(response);
    } catch (error) { throw localizedError(error); }
  }
  async function send(action, args = {}, item = null) {
    if (busy || !state) return;
    const body = { action, view_token: item?.view_token || state.view_token, ...args };
    if (item) {
      body.expected_revision = item.revision;
      if (item.activity_id) body.activity_id = item.activity_id;
      else { body.kind = item.kind; body.local_date = item.local_date; }
    }
    const signature = JSON.stringify(body);
    if (!pending.has(signature)) pending.set(signature, { ...body, operation_key: operationKey() });
    const generation = readGeneration, command = pending.get(signature);
    controls(true); $("dialog-error").hidden = true;
    notice(["diary", "dream", "derive"].includes(action) ? "正在书写…" : "正在保存…");
    try {
      const result = await api("POST", command);
      if (pending.get(signature) === command) pending.delete(signature);
      if (generation !== readGeneration) return;
      if (action === "configure") dirty = false;
      $("editor").close();
      const retryOf = action === "diary" ? String(args.retry_of || "") : "";
      if (retryOf && result.state === "unknown") blockedRetries.add(retryOf);
      notice(result.state === "failed" || result.state === "unknown" ? "本次书写未完成，详情已保留在执行记录。" : action === "add_epub" ? (result.replayed ? "此前的导入已处理，请在书架查看。" : `已导入《${result.title}》，共 ${result.activity_ids.length} 个待读片段。`) : "操作已完成。", ["failed", "unknown"].includes(result.state));
      await load(true);
    } catch (error) {
      if (generation !== readGeneration) return;
      const retryOf = action === "diary" ? String(args.retry_of || "") : "";
      if (retryOf && error.code === "journal_trigger_unresolved") {
        blockedRetries.add(retryOf);
        renderReceipts();
        notice(errors.journal_trigger_unresolved, true);
      } else {
        notice(error.message, true);
      }
      if ($("editor").open) { $("dialog-error").textContent = error.message; $("dialog-error").hidden = false; }
    } finally { if (generation === readGeneration) controls(false); }
  }
  function actionButton(label, action, item, direct = false) {
    const b = node("button", label, ["delete", "cancel", "withdraw"].includes(action) ? "danger" : "secondary");
    b.type = "button"; b.addEventListener("click", () => direct ? send(action, {}, item) : open(action, item)); return b;
  }
  function renderArchive() {
    const list = $("journal-list"); list.replaceChildren();
    const labels = {diary: "日记与梦境", reading_note: "读书笔记", social_note: "上网笔记"};
    const items = state.journals.filter(j => archiveKind === "diary" ? ["diary", "dream"].includes(j.kind) : j.kind === archiveKind);
    if (!items.length) list.append(node("p", `这里还没有${labels[archiveKind]}。`, "empty"));
    selected = items.find(j => j.kind === selected?.kind && j.local_date === selected?.local_date &&
      (!["reading_note", "social_note"].includes(j.kind) || j.activity_id === selected?.activity_id)) || items[0] || null;
    for (const item of items) {
      const b = node("button", `${item.local_date} · ${kind(item)}`, "journal-link"); b.type = "button";
      b.setAttribute("aria-current", String(item === selected));
      b.append(node("small", item.state === "deleted" ? "已删除" : item.unavailable_reason ? "来源不可用" :
        (item.kind === "reading_note" ? item.book_title + " · " + (item.body || "") : item.body || item.afterglow || "").slice(0, 45)));
      b.addEventListener("click", () => { selected = item; renderArchive(); }); list.append(b);
    }
    const detail = $("journal-detail"); detail.replaceChildren();
    if (!selected) { detail.append(node("p", `选择一篇${labels[archiveKind]}。`, "empty")); return; }
    const item = selected;
    detail.append(node("p", item.local_date, "eyebrow"), node("h2", kind(item)));
    if (item.state === "deleted") detail.append(node("p", "这篇记录已删除。可以主动填写正文后恢复。", "prose"));
    else if (item.unavailable_reason) detail.append(node("p", "依赖的原来源已不可用，正文与摘要已隐藏。", "prose"));
    else {
      if (item.kind === "reading_note") {
        detail.append(node("h3", item.book_title), node("p", `读到原文第 ${item.start_offset + 1}–${item.end_offset} 字`, "eyebrow"));
        if (item.excerpt) detail.append(node("h3", "原文摘录"), node("p", item.excerpt, "prose"));
        detail.append(node("h3", "读后感"));
      }
      detail.append(node("p", item.body, "prose"));
      if (item.afterglow) detail.append(node("p", item.afterglow, "afterglow"));
      if (item.summary) { const summary = node("div", undefined, "journal-summary"); summary.append(node("h3", "书写索引"), node("p", item.summary)); detail.append(summary); }
      if (item.dream_fragments?.length) { const ul = node("ul"); item.dream_fragments.forEach(s => ul.append(node("li", s))); detail.append(ul); }
    }
    const actions = node("div", undefined, "actions");
    if (item.kind === "reading_note") { detail.append(node("p", "读书笔记来自已完成的阅读记录。", "eyebrow")); return; }
    if (item.kind === "social_note") { detail.append(node("p", "上网笔记来自已完成的浏览感想。", "eyebrow")); return; }
    if (item.state === "deleted") actions.append(actionButton("填写正文并恢复", "restore", item));
    else { if (!item.unavailable_reason) actions.append(actionButton("更正正文", "edit", item), actionButton("提取索引", "derive", item, true)); actions.append(actionButton("删除", "delete", item)); }
    detail.append(actions);
  }
  function renderActivities() {
    const plan = state.reading_plan || {};
    const planText = {disabled: "自动阅读已关闭。", empty: "书架暂无待读书籍。",
      waiting: "等待合适的阅读安排或空档。", choosing: "正在根据兴趣和日程挑选书籍…",
      paced: "正在按阅读速度安排时间；结束后再开始新的阅读或上网。",
      incomplete: "这次选书未完成，等待生活状态变化后再考虑。",
      declined: "暂时没有选书。" + (plan.reason ? " " + plan.reason : ""),
      selected: `选择了《${plan.title || "已选书籍"}》。` +
        (plan.reason ? " " + plan.reason : "")};
    $("reading-plan").textContent = planText[plan.state] || "";
    for (const type of ["reading", "sleep", "social"]) {
      const list = $(type + "-list"); list.replaceChildren();
      const items = state.activities.filter(a => a.kind === type);
      if (!items.length) list.append(node("p", type === "reading" ? "本批暂无阅读片段。" : type === "sleep" ? "本批没有睡眠记录。" : "本批暂无浏览记录。", "empty"));
      for (const item of items) {
        const card = node("article", undefined, "activity");
        const badge = node("span", names[item.state] || "状态待核对", "badge"); badge.dataset.state = item.state || "";
        card.append(node("h3", item.title), badge);
        /* due_at 对睡眠是计划醒来，对阅读/浏览是按速度排定的结束时间。 */
        const dueLabel = type === "sleep" ? "计划醒来" : "预计结束";
        card.append(node("p", `${item.started_at ? date(item.started_at) : "尚未开始"}${item.due_at ? " · " + dueLabel + " " + date(item.due_at) : ""}`, "muted"));
        if (item.unavailable_reason) card.append(node("p", "原文已不可用，停止后续阅读并隐藏读后感。", "prose"));
        else if (item.reflection) card.append(node("p", item.reflection, "prose"));
        if (type === "social" && item.checkpoint) {
          try {
            const receipt = JSON.parse(item.checkpoint);
            if (receipt.subjective_reflection) card.append(node("p", receipt.subjective_reflection, "prose"));
            const labels = {bilibili:"B 站", xiaohongshu:"小红书", zhihu:"知乎", douban:"豆瓣", brightdata:"Google"};
            const operations = {bili_recommend:"浏览推荐",bili_timeline:"浏览关注动态",bili_search:"搜索",bili_watch:"阅读视频文字",bili_like:"点赞",bili_favorite:"收藏",bili_follow:"关注",xhs_recommend:"浏览推荐",xhs_search:"搜索",xhs_read:"阅读笔记",xhs_like:"点赞",xhs_favorite:"收藏",zhihu_home:"浏览首页",zhihu_search:"搜索",zhihu_read:"阅读正文",zhihu_like:"赞同",douban_books:"浏览图书榜",douban_movies:"浏览电影榜",douban_search_book:"搜索图书",douban_search_movie:"搜索电影",douban_read_book:"阅读图书详情",douban_read_movie:"阅读电影详情",douban_wish_book:"标记想读",douban_wish_movie:"标记想看",google_search:"搜索",google_read:"阅读网页"};
            const coverage = {results: "结果列表", text: "文字内容", image: "送达图片", image_description: "图片视觉转述", metadata: "仅元信息", interaction: "互动回执"};
            for (const action of receipt.actual_actions || []) card.append(node("p", `${labels[action.platform] || "平台"} · ${operations[action.operation] || "浏览"}${action.coverage ? " · " + (coverage[action.coverage] || "") : ""}${action.visual_coverage === "image_description" && action.coverage !== "image_description" ? " · 图片视觉转述" : ""}${action.query ? " · " + action.query : ""}${action.id ? " · " + action.id : ""}`, "muted"));
          } catch (_) { card.append(node("p", item.checkpoint, "muted")); }
        }
        if (item.task && item.task.state !== "succeeded") card.append(node("p", "阅读执行：" + (names[item.task.state] || "状态待核对"), "muted"));
        const actions = node("div", undefined, "actions");
        if (type === "reading" && ["planned", "paused"].includes(item.state) && !item.unavailable_reason && item.task?.state !== "unknown") actions.append(actionButton(item.state === "paused" ? "重新阅读" : "开始阅读", "read", item, true));
        if (type === "sleep" && item.state === "active") actions.append(actionButton("现在醒来", "wake", item));
        if (["planned", "active", "paused"].includes(item.state)) actions.append(actionButton("取消", "cancel", item));
        if (type === "reading" && !item.unavailable_reason) actions.append(actionButton("撤回原文", "withdraw", item));
        if (type === "reading" && ["completed", "cancelled"].includes(item.state)) actions.append(actionButton("删除", "remove", item));
        card.append(actions); list.append(card);
      }
    }
  }
  function renderInterests() {
    const profile = $("profile-hobbies"); profile.replaceChildren();
    if (state.profile_hobbies) profile.append(node("h3", "角色档案中的爱好"),
      node("p", state.profile_hobbies, "prose"), node("p", "这是已有设定；下方是从实际活动中逐渐形成的线索。", "muted"));
    const list = $("interest-list"); list.replaceChildren();
    const items = state.interests || [];
    if (!items.length) list.append(node("p", "还没有可展示的兴趣线索。", "empty"));
    const labels = {exposed: "接触中", curious: "正在探索", active: "正在探索", established: "稳定爱好", disliked: "试过不喜欢", dormant: "长期未接触", paused: "已暂停"};
    const groups = [["接触中", ["exposed"]], ["正在探索", ["curious", "active"]], ["稳定爱好", ["established"]],
      ["试过不喜欢", ["disliked"]], ["长期未接触或暂停", ["dormant", "paused"]]];
    const ordered = groups.flatMap(([label, statuses]) => {
      const entries = items.filter(item => statuses.includes(item.status));
      return entries.length ? [{heading: label}, ...entries] : [];
    });
    for (const item of ordered) {
      if (item.heading) { list.append(node("h3", item.heading)); continue; }
      const card = node("article", undefined, "activity");
      const badge = node("span", labels[item.status] || item.status, "badge"); badge.dataset.state = item.status || "";
      card.append(node("h3", item.name), badge);
      if (item.exposure_count) card.append(node("p", `有效接触 ${item.exposure_count} 次 · ${item.content_count} 条内容 · 熟悉度 ${item.familiarity}`, "muted"));
      if (item.summary) card.append(node("p", item.summary, "prose"));
      for (const contact of item.contacts || []) card.append(node("p", `${date(contact.occurred_at)} · ${contact.platform} · ${contact.title}${contact.coverage ? " · " + (coverageNames[contact.coverage] || contact.coverage) : ""}`, "muted"));
      if (item.next_question) card.append(node("p", "想继续了解：" + item.next_question, "muted"));
      for (const evidence of item.evidence || []) card.append(node("p", `${date(evidence.occurred_at)} · ${evidence.source || "活动"}${evidence.active === false ? " · 历史反馈" : ""} · ${evidence.reason}`, "muted"));
      const actions = node("div", undefined, "actions");
      const args = {interest_id: item.interest_id, expected_revision: item.revision, view_token: item.view_token};
      const pause = node("button", item.status === "paused" ? "恢复探索" : "暂停探索", "secondary");
      pause.type = "button"; pause.addEventListener("click", () => send(item.status === "paused" ? "interest_resume" : "interest_pause", args));
      const rename = node("button", "更正名称", "secondary"), field = document.createElement("input"), save = node("button", "保存名称", "secondary");
      rename.type = save.type = "button"; field.value = item.name; field.maxLength = 50; field.setAttribute("aria-label", "兴趣名称");
      field.hidden = save.hidden = true;
      rename.addEventListener("click", () => { field.hidden = save.hidden = false; field.focus(); });
      save.addEventListener("click", () => { if (field.value.trim()) send("interest_edit", {...args, name: field.value.trim()}); });
      actions.append(pause, rename, field, save);
      if (item.status === "disliked") {
        const clear = node("button", "解除不喜欢", "secondary"); clear.type = "button";
        clear.addEventListener("click", () => send("interest_clear_dislike", args)); actions.append(clear);
      }
      const targets = items.filter(other => other.interest_id !== item.interest_id);
      if (targets.length) {
        const select = document.createElement("select"); select.setAttribute("aria-label", "合并到的兴趣");
        select.append(new Option("选择合并目标", ""), ...targets.map(other => new Option(other.name, other.interest_id)));
        const merge = node("button", "合并到所选兴趣", "secondary"); merge.type = "button";
        merge.addEventListener("click", () => { const target = targets.find(other => other.interest_id === select.value);
          if (target) send("interest_merge", {...args, target_id: target.interest_id, target_revision: target.revision}); });
        actions.append(select, merge);
      }
      card.append(actions); list.append(card);
    }
    const disliked = $("disliked-item-list"); disliked.replaceChildren();
    if ((state.disliked_items || []).length) disliked.append(node("h3", "试过不喜欢的具体内容"));
    for (const item of state.disliked_items || []) {
      const card = node("article", undefined, "activity");
      card.append(node("h3", item.title), node("p", `${item.platform} · ${date(item.occurred_at)}${item.reason ? " · " + item.reason : ""}`, "muted"));
      const clear = node("button", "解除不喜欢", "secondary"); clear.type = "button";
      clear.addEventListener("click", () => send("interest_item_clear", {dislike_id: item.dislike_id,
        expected_revision: item.revision, view_token: item.view_token}));
      card.append(clear); disliked.append(card);
    }
    const recent = $("recent-exposure-list"); recent.replaceChildren();
    for (const item of state.recent_exposures || []) {
      const card = node("article", undefined, "activity");
      const stages = {"获取":"已获取", "接触":"已接触", "尝试":"已尝试", "反馈":"已反馈"};
      card.append(node("h3", item.title), node("p", `${item.platform} · ${date(item.exposed_at || item.fetched_at)} · ${stages[item.stage] || item.stage}${item.coverage ? " · " + (coverageNames[item.coverage] || item.coverage) : ""}${item.random_attempt ? " · 随机试读" : ""}`, "muted"));
      recent.append(card);
    }
    if (!(state.recent_exposures || []).length) recent.append(node("p", "暂无推荐接触记录。", "empty"));
  }
  const blockedRetries = new Set();
  function renderReceipts() {
    const list = $("receipt-list"); list.replaceChildren();
    const receipts = [...new Map(state.receipts.map(r => [r.operation_id || r.key, r])).values()].reverse();
    if (!receipts.length) list.append(node("p", "暂无书写执行记录。", "empty"));
    receipts.forEach((item, index) => {
      const card = node("article", undefined, "activity");
      card.append(node("strong", `书写记录 ${index + 1} · ${names[item.state] || "状态待核对"}`));
      if (item.code) card.append(node("p", errors[item.code] || "本次书写未完成，请核对模型与执行权限后重试。", "muted"));
      list.append(card);
    });
    // 只有“已结论的失败”才能重试；unknown/处理中/已受理的那条不能再次提交，
    // 否则同一天会多出第二条书写。retryable=false 由后端按操作状态给出。
    const failed = receipts.map((item, index) => ({item, index}))
      .filter(({item}) => item.state === "failed" && item.operation_id && item.retryable !== false);
    const unresolved = state.journal_unresolved === true;
    for (const id of ["retry-id", "dream-retry"]) {
      const select = $(id), previous = select.value;
      select.replaceChildren(new Option(id === "dream-retry" ? "首次记录" : unresolved ? "有一次书写结果未确定，暂不能重试" : failed.length ? "选择失败的书写" : "暂无已确定失败的书写", ""),
        ...failed.map(({item, index}) => new Option(`书写记录 ${index + 1} · 失败`, item.operation_id || item.key)));
      select.value = failed.some(({item}) => (item.operation_id || item.key) === previous) ? previous : "";
    }
    $("retry-form").hidden = unresolved || failed.length === 0;
  }
  function renderSleepPlan() {
    const plan = state.sleep_schedule;
    if (!plan) {
      $("sleep-scope").textContent = "暂不可用"; $("sleep-executor").textContent = "—";
      $("sleep-next").textContent = "—"; $("sleep-active-wake").textContent = "—";
      $("sleep-pressure").textContent = "—"; $("sleep-debt").textContent = "—";
      $("sleep-workload").textContent = "—"; $("sleep-recommended").textContent = "—"; return;
    }
    $("sleep-scope").textContent = plan.enabled ? "当前角色 · 各会话共享" : "未启用";
    const executorName = plan.executor_is_current ? "当前私聊窗口" : "同一角色的其他私聊窗口";
    $("sleep-executor").textContent = !plan.enabled ? "未安排"
      : plan.executor_state !== "available" ? "暂无有效授权窗口"
        : `${executorName}${plan.executor_is_current ? "（本页）" : ""}`;
    $("sleep-next").textContent = plan.next_sleep_at
      ? `${date(plan.next_sleep_at)} · ${date(plan.next_wake_at)}` : "未安排";
    $("sleep-active-wake").textContent = plan.active_wake_at ? date(plan.active_wake_at) : "当前没有进行中的睡眠";
    const profile = plan.physiology || {};
    $("sleep-pressure").textContent = profile.sleep_pressure == null ? "暂无记录" : `${profile.sleep_pressure} / 100`;
    $("sleep-debt").textContent = profile.has_sleep_history ? `${Math.round(profile.sleep_debt_minutes || 0)} 分钟` : "暂无记录";
    $("sleep-workload").textContent = `${Math.round(profile.workload_score || 0)} / 100`;
    $("sleep-recommended").textContent = plan.recommended_duration_minutes
      ? `${Math.round(plan.recommended_duration_minutes / 60 * 10) / 10} 小时` : "—";
  }
  function render() {
    $("role").textContent = roleName();
    $("participant").textContent = display().subject_name || "当前聊天对象";
    $("host-state").textContent = names[state.host_state] || "状态待核对";
    $("routine-state").textContent = state.config.enabled ? "已启用" : "未启用";
    $("next-routines").textContent = [state.next_diary_at ? "下次日记：" + date(state.next_diary_at) : "", state.next_sleep_at ? "下次入睡：" + date(state.next_sleep_at) : ""].filter(Boolean).join(" · ");
    renderSleepPlan();
    if (!dirty) configFields.forEach(k => { if (state.config[k] == null) return; if (booleanFields.includes(k)) $(k).checked = state.config[k] === true; else $(k).value = state.config[k]; });
    renderArchive(); renderActivities(); renderInterests(); renderReceipts(); controls(busy);
    if (state.failure_code) notice(errors[state.failure_code] || "生活日程已暂停，请检查执行权限与设置。", true);
  }
  async function load(preserveNotice = false) {
    if (!bridge) return;
    const sequence = ++loadSequence;
    try {
      const result = await api("GET", { archive_kind: archiveKind, ...(datePages[datePage] ? { before: datePages[datePage] } : {}), ...(activityCursor ? { after_activity: activityCursor } : {}) });
      if (sequence !== loadSequence) return;
      /* 宿主的生活读取按白名单裁剪：receipts / running 现在恒为空数组（日记与梦境的回执不再下发），
         其余已移除的字段（interests 等）可能整个缺失。列表字段统一补成数组、config 补成对象，
         各渲染函数不必逐处判空，也不会因缺字段抛错。 */
      const list = value => Array.isArray(value) ? value : [];
      state = {...result, journals: list(result.journals), activities: list(result.activities), receipts: list(result.receipts),
        running: list(result.running), config: result.config && typeof result.config === "object" ? result.config : {}};
      if (!preserveNotice) notice(""); render();
      clearTimeout(polling);
      if (state.running.length || state.activities.some(a => a.state === "active") || state.config.reading_enabled) polling = setTimeout(() => { if (!busy && !document.hidden) load(true); }, 3000);
    } catch (error) {
      if (sequence !== loadSequence) return;
      state = selected = null;
      ["journal-list", "journal-detail", "reading-list", "sleep-list", "receipt-list", "retry-id", "dream-retry"].forEach(id => $(id).replaceChildren());
      ["journal-list", "reading-list", "sleep-list"].forEach(id => $(id).append(node("p", "记录暂时无法读取，请刷新后再试。", "empty is-error")));
      $("social-list").replaceChildren(node("p", "记录暂时无法读取，请刷新后再试。", "empty is-error"));
      $("retry-form").hidden = true;
      for (const id of ["role", "participant", "sleep-scope", "sleep-executor", "sleep-next", "sleep-active-wake", "sleep-pressure", "sleep-debt", "sleep-workload", "sleep-recommended"]) $(id).textContent = "—";
      /* 配置/状态没读到时给出明确状态，而不是一排“—”：用户能分清是
         「没有配置」还是「这次没读到」，也知道下一步点哪里。 */
      $("routine-state").textContent = "读取失败";
      $("host-state").textContent = "暂不可用";
      $("settings").open = true;
      $("editor").close();
      notice(`${error.message} 配置暂时无法读取或保存，请点「刷新记录」重试。`, true);
      controls(false);
    }
  }
  function open(action, item = null) {
    dialogAction = action; dialogItem = item;
    const titles = { add_text: "添加阅读原文", sleep: "开始睡眠", edit: "更正正文", restore: "主动恢复正文", delete: "删除这篇书写", cancel: "取消活动", withdraw: "撤回阅读原文", remove: "删除阅读记录", wake: "结束这段睡眠", dream: "记录或重试梦境" };
    $("dialog-title").textContent = titles[action];
    $("dialog-description").textContent = { withdraw: "整份原文及其所有片段将停止使用，依赖它的私人书写也将隐藏。", remove: "这条阅读记录会从书架移除；原文与依赖它的私人书写也会停止使用，删除后无法恢复。", delete: "删除后自动书写不会覆盖此记录，恢复需要主动填写正文。", wake: "确认醒来后，会独立记录这段睡眠的梦境。", add_text: "保存完整原文，较长资料会拆为连续阅读片段。收存不会标记已读。" }[action] || "确认当前记录后保存。";
    $("title-field").hidden = action !== "add_text";
    $("body-field").hidden = !["add_text", "edit", "restore"].includes(action);
    $("glow-field").hidden = !["edit", "restore"].includes(action) || item?.kind !== "dream";
    $("minutes-field").hidden = action !== "sleep"; $("retry-field").hidden = action !== "dream";
    $("text-title").value = ""; $("text-body").value = item?.body || ""; $("text-glow").value = item?.afterglow || ""; $("dream-retry").value = "";
    $("text-title").required = action === "add_text"; $("text-body").required = action === "add_text" || (["edit", "restore"].includes(action) && item?.kind !== "dream");
    $("minutes").value = state.config.sleep_minutes ?? 480; $("dialog-error").hidden = true;
    $("editor").showModal();
  }
  function selectTab(value) {
    if (!document.querySelector('[data-tab="' + CSS.escape(value) + '"]')) return;
    tab = value; document.querySelectorAll("[data-tab]").forEach(x => x.setAttribute("aria-current", x.dataset.tab === tab ? "page" : "false"));
    document.querySelectorAll(".tab-panel").forEach(p => { p.hidden = p.id !== tab; }); $("activity-pages").hidden = !["reading", "sleep", "social"].includes(tab);
  }
  function selectArchiveKind(value, refresh = true) {
    if (archiveKind === value || !document.querySelector('[data-archive-kind="' + CSS.escape(value) + '"]')) return;
    archiveKind = value; selected = null; datePages = [null]; datePage = 0;
    document.querySelectorAll("[data-archive-kind]").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.archiveKind === value)));
    $("write-diary").hidden = archiveKind !== "diary";
    $("journal-list").replaceChildren(node("p", "正在读取记录…", "empty"));
    $("journal-detail").replaceChildren(node("p", "选择一篇记录。", "empty"));
    $("newer").disabled = $("older").disabled = true;
    if (refresh) void load();
  }
  function resetScope() {
    loadSequence++;
    clearTimeout(polling); polling = null;
    state = selected = dialogAction = dialogItem = null;
    datePages = [null]; datePage = 0; activityCursor = null;
    dirty = false;
    blockedRetries.clear();
    $("config-form").reset();
    $("editor").close();
    $("dialog-error").hidden = $("retry-form").hidden = true;
    for (const id of ["text-title", "text-body", "text-glow", "minutes", "retry-date", "epub-file", "epub-folder"]) $(id).value = "";
    for (const id of ["journal-list", "journal-detail", "reading-list", "sleep-list", "social-list", "receipt-list",
      "retry-id", "dream-retry", "profile-hobbies", "interest-list", "disliked-item-list", "recent-exposure-list"]) $(id).replaceChildren();
    for (const id of ["next-routines", "reading-plan", "dialog-title", "dialog-description", "dialog-error"]) $(id).textContent = "";
    for (const id of ["role", "participant", "routine-state", "sleep-scope", "sleep-executor", "sleep-next", "sleep-active-wake",
      "sleep-pressure", "sleep-debt", "sleep-workload", "sleep-recommended"]) $(id).textContent = "—";
    $("host-state").textContent = "正在读取…";
    notice("");
    controls(false);
  }
  document.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  document.querySelectorAll("[data-archive-kind]").forEach(b => b.addEventListener("click", () => selectArchiveKind(b.dataset.archiveKind)));
  /* 宿主切换板块：直接改 tab 与 archive_kind，不靠模拟点击（忙碌时按钮被禁用，点击会丢）。
     首次 context 在 ready() 之前处理，bridge 尚未就绪，load() 不发请求，首查即用该板块的 archive_kind。 */
  document.addEventListener("companion-section-change", event => {
    const {section, tab: wanted, readGeneration: generation} = event.detail || {};
    const scopeChanged = readGeneration !== generation;
    if (scopeChanged) {
      readGeneration = generation;
      resetScope();
    }
    if (wanted) selectTab(wanted);
    if (sectionArchiveKinds[section]) selectArchiveKind(sectionArchiveKinds[section], !scopeChanged);
    if (scopeChanged) void load();
  });
  $("config-form").addEventListener("input", () => { dirty = true; });
  $("config-form").addEventListener("submit", e => {
    e.preventDefault();
    /* 状态没读到时 state 仍是 null：这里以前会直接读 state.edit_token 抛错，
       表单看起来“点了没反应”。先把原因说清楚，再让用户刷新。 */
    if (!state) { $("settings").open = true; notice("配置尚未读取成功，请先点「刷新记录」重试后再保存。", true); return; }
    try {
      const changes = {};
      configFields.forEach(k => { changes[k] = booleanFields.includes(k) ? $(k).checked : numberFields.includes(k) ? Number($(k).value) : $(k).value; });
      if (changes.social_existing_percent + changes.social_related_percent + changes.social_new_percent !== 100) {
        notice("探索方向的三个比例相加必须为 100%。", true); return;
      }
      send("configure", { changes, edit_token: state.edit_token });
    } catch (error) { notice(error?.message || "保存失败，请刷新后重试。", true); }
  });
  $("editor-form").addEventListener("submit", e => {
    e.preventDefault();
    if (!dialogAction || !state) return;
    const args = {};
    if (dialogAction === "add_text") { args.title = $("text-title").value; args.text = $("text-body").value; }
    if (["edit", "restore"].includes(dialogAction)) { args.body = $("text-body").value; args.afterglow = dialogItem.kind === "dream" ? $("text-glow").value : ""; }
    if (dialogAction === "sleep") args.minutes = Number($("minutes").value);
    if (dialogAction === "dream" && $("dream-retry").value.trim()) args.retry_of = $("dream-retry").value.trim();
    send(dialogAction, args, dialogItem);
  });
  $("retry-form").addEventListener("submit", e => { e.preventDefault(); send("diary", { local_date: $("retry-date").value, retry_of: $("retry-id").value.trim() }); });
  $("close-dialog").addEventListener("click", () => $("editor").close());
  $("refresh").addEventListener("click", () => load());
  async function hostControl(action) {
    if (busy || !bridge) return;
    const generation = readGeneration;
    controls(true); notice(action === "start" ? "正在载入当前角色并启动…" : "正在停止运行…");
    const call = async name => unwrap(await bridge.apiPost("staging/" + name, {}));
    try {
      if (action === "start") {
        const status = unwrap(await bridge.apiGet("staging/status", {}));
        if (generation !== readGeneration) return;
        if (!status.runtime_open) {
          await call("probe");
          if (generation !== readGeneration) return;
          await call("initialize");
          if (generation !== readGeneration) return;
        }
      }
      await call(action);
      if (generation === readGeneration) await load();
    } catch (error) {
      if (generation === readGeneration) notice(localizedError(error).message, true);
    } finally { if (generation === readGeneration) controls(false); }
  }
  $("start-host").addEventListener("click", () => hostControl("start"));
  $("stop-host").addEventListener("click", () => hostControl("stop"));
  $("add-text").addEventListener("click", () => { if (state) open("add_text"); });
  $("import-epub").addEventListener("click", () => { if (state && !busy) $("epub-file").click(); });
  $("import-epub-folder").addEventListener("click", () => { if (state && !busy) $("epub-folder").click(); });
  async function importBooks(event) {
    const chosen = Array.from(event.target.files || []);
    if (!chosen.length || !state || busy) return;
    const files = chosen.filter(file => file.name.toLowerCase().endsWith(".epub"));
    if (!files.length) { notice("没有找到 EPUB 文件。", true); event.target.value = ""; return; }
    const failed = [], generation = readGeneration;
    let imported = 0;
    controls(true);
    try {
      for (const [index, file] of files.entries()) {
        if (generation !== readGeneration) return;
        notice(`正在导入 ${index + 1} / ${files.length}：${file.name}`);
        try {
          if (file.size > 20 * 1024 * 1024) throw new Error(errors.life_epub_too_large);
          const encoded = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
            reader.onerror = () => reject(new Error("无法读取文件。"));
            reader.readAsDataURL(file);
          });
          // 文件读取跨异步边界，不能用切换后角色的 view_token 发送旧批次。
          if (generation !== readGeneration) return;
          const result = await api("POST", {action: "add_epub", operation_key: operationKey(),
            view_token: state.view_token, filename: file.name, epub_base64: encoded});
          if (["failed", "unknown"].includes(result.state)) throw new Error("结果未确认，请先查看书架。");
          imported++;
        } catch (error) { failed.push(`${file.name}：${error.message || "结果未确认，请先查看书架。"}`); }
      }
      if (generation !== readGeneration) return;
      await load(true);
      if (generation !== readGeneration) return;
      const skipped = chosen.length - files.length;
      notice(`已导入 ${imported} / ${files.length} 本。` + (skipped ? `已跳过 ${skipped} 个非 EPUB 文件。` : "") +
        (failed.length ? ` 未完成：${failed.join("；")}` : "") +
        (!state ? " 书架刷新失败，请手动刷新确认。" : ""), failed.length > 0 || !state);
    } finally {
      if (generation === readGeneration) { event.target.value = ""; controls(false); }
    }
  }
  $("epub-file").addEventListener("change", importBooks);
  $("epub-folder").addEventListener("change", importBooks);
  $("start-sleep").addEventListener("click", () => open("sleep"));
  $("write-diary").addEventListener("click", () => send("diary"));
  $("older").addEventListener("click", () => { datePages[++datePage] = state.next_date; load(); });
  $("newer").addEventListener("click", () => { datePage--; load(); });
  $("more-activities").addEventListener("click", () => { activityCursor = state.next_activity; load(); });
  $("first-activities").addEventListener("click", () => { activityCursor = null; load(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !busy) load(true); });
  document.addEventListener("companion-mappings-change", () => { if (bridge && !busy) void load(true); });
  /* 先同步取下内嵌桥接再等待：bundle 里仍留着 defer 的外部 SDK 脚本，它在内联脚本之后执行，
     若重设了 window.AstrBotPluginPage，等待结束后再读全局就会拿到另一套桥接。 */
  (async () => { try { const page = window.AstrBotPluginPage; await page.ready(); bridge = page; await load(); } catch (_) { notice("无法连接管理界面，请从 AstrBot 插件页面重新打开。", true); } })();
})();
