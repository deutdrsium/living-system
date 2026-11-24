"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const frame = $("life-frame");
  const allowedSettings = new Set(["enabled", "timezone", "reading_chunk_chars", "reading_daily_segments", "reading_enabled",
    "reading_speed_chars_per_minute", "sleep_enabled", "sleep_minutes", "sleep_time", "sleep_timezone", "sleep_variation_minutes",
    "sleep_window_minutes", "social_existing_percent", "social_interval_minutes", "social_new_percent", "social_random_read_percent",
    "social_recommendation_enabled", "social_related_percent"]);
  let bridge, catalog, editorState, monthState, snapshot, selectedTarget, selectedRole,
    page = "overview", readingPage = "reading", frameReady = false, stateFeature = "maintenance", fetching = true,
    calendarView = null, calendarViewTouched = false;
  const overviewDisclosures = {pastOpen:false, openReasons:new Set()};
  const calendarDisclosures = {pastOpen:false, openReasons:new Set()};
  const titles = {overview:["今天","生活总览"], calendar:["生活","日程"], wallet:["生活","钱包"], timetable:["生活","课表"], reading:["记录","阅读"], browsing:["记录","浏览"], settings:["设置","作息与设置"]};
  window.GoodLifeIcons.hydrate();
  const node = (tag, text, cls) => { const item = document.createElement(tag); item.textContent = text; if(cls)item.className=cls; return item; };
  const endpoint = () => `targets/${encodeURIComponent(selectedTarget.id)}/${encodeURIComponent(selectedRole.token)}`;
  const unwrap = response => {
    const envelope = ["ok", "error"].includes(response?.status) ? response :
      ["ok", "error"].includes(response?.data?.status) ? response.data : null;
    if(envelope?.status === "error") throw Object.assign(new Error(envelope.reason || envelope.message),
      {code:envelope.data?.code || envelope.message, confirmed:true});
    return envelope ? envelope.data : response;
  };
  async function api(method, path, body, options) {
    return unwrap(await (method === "GET" ? bridge.apiGet(path, body, options) : bridge.apiPost(path, body, options)));
  }
  /* 子页（EPUB 导入等长操作）请求的时限原样交给外层 SDK；只接受与子页桥接相同的范围。 */
  const forwardedTimeout = value => Number.isInteger(value) && value >= 1000 && value <= 300000 ? {timeoutMs:value} : undefined;
  let saveNotice, connectionNotice;
  const readErrors = new Map();
  function renderNotice() {
    const message = connectionNotice || saveNotice || [...readErrors.values()].join("；");
    $("notice").textContent=message; $("notice").hidden=!message; $("notice").dataset.error="true";
  }
  function connectionError(message) { connectionNotice=message; renderNotice(); }
  function saveError(message) { saveNotice=message; renderNotice(); }
  function readNotice(source, message) {
    if(message)readErrors.set(source, message);
    else readErrors.delete(source);
    renderNotice();
  }
  function tellFrame(refreshMappings=true) {
    if(!frameReady || (!selectedRole && !scopeGeneration))return;
    const section = page === "reading" ? readingPage : page === "browsing" ? "browsing" : "settings";
    const tab = section === "notes" ? "archive" : section === "browsing" ? "social" : section === "settings" ? "sleep" : "reading";
    frame.contentWindow.postMessage({channel:"companion-workspace",kind:"context",context:{section,tab,
      display:{role_name:selectedRole?.name,subject_name:selectedTarget?.name},confirm:false,readGeneration:scopeGeneration,
      active:["reading","browsing","settings"].includes(page)}}, "*");
    if(refreshMappings && selectedRole)frame.contentWindow.postMessage({channel:"companion-workspace",kind:"host-mappings-refreshed"}, "*");
  }
  const snapshotInput = () => ({...snapshot, loading:fetching, now:Date.now()});
  function disclosureFocus(container) {
    const active = document.activeElement;
    const disclosure = active?.matches?.("summary") && container.contains(active) ? active.parentElement : null;
    const key = disclosure?.dataset.overviewReason;
    if(key)return `[data-overview-reason="${CSS.escape(key)}"] > summary`;
    return disclosure?.matches(".gl-agenda-past") ? ".gl-agenda-past > summary" : null;
  }
  function trackDisclosures(container, saved) {
    container.addEventListener("toggle", ({target})=>{
      if(target.matches?.(".gl-agenda-past"))saved.pastOpen=target.open;
      const key=target.dataset?.overviewReason;
      if(key) {
        if(target.open)saved.openReasons.add(key);
        else saved.openReasons.delete(key);
      }
    }, true);
  }
  /* 总览四块全部由 overview.js 生成；这里只负责把已读到的数据交给它。 */
  function renderOverview() {
    const view = window.GoodLifeOverview;
    const input = {...snapshotInput(), ...overviewDisclosures};
    const focus = disclosureFocus($("overview"));
    $("current").innerHTML = view.now(input);
    $("sleep-summary").innerHTML = view.rest(input);
    $("body-state").innerHTML = view.stateGrid(input);
    $("upcoming").innerHTML = view.schedule(input);
    if(focus)$("overview").querySelector(focus)?.focus();
    renderClock();
  }
  /* 顶部时间按角色的规划时区显示；时区取自已读到的数据，读到之前用浏览器本地时间。
     天气不在本插件已有接口里，顶部条不显示天气，也不占位。 */
  function renderClock() {
    const zone = snapshot ? window.GoodLifeOverview.zoneOf(snapshot) : undefined, at = new Date();
    const options = zone ? {timeZone:zone} : {};
    $("topbar-time").textContent = new Intl.DateTimeFormat("zh-CN", {...options, hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).format(at);
    $("topbar-date").textContent = new Intl.DateTimeFormat("zh-CN", {...options, month:"long", day:"numeric", weekday:"short"}).format(at)
      + (zone ? " · " + zone : "");
    $("topbar-scope").hidden = !selectedRole;
    $("topbar-scope").textContent = selectedRole ? `${selectedRole.name} · ${selectedTarget?.name || "私聊窗口"}` : "";
  }
  function editorReads() {
    let savedRead;
    return {
      async api(method, path, body, options) {
        const scope = selectedRole ? captureScope() : null;
        const result = await api(method, path, body, options);
        if(method === "POST" && typeof result?.operation_id === "string" && scope && isCurrentScope(scope)) {
          saveError("");
          months = new Map();
          savedRead = {scope, state:requestSnapshot()};
        }
        return result;
      },
      refresh() {
        const saved = savedRead;
        savedRead = null;
        return saved ? (isCurrentScope(saved.scope) ? saved.state : Promise.resolve(null)) : loadState();
      },
    };
  }
  const calendar = window.CompanionCalendarEditor.create({
    ...editorReads(), changed:renderCalendar,
    failed:error=>saveError(error.message || "无法保存日程，请刷新后再试。")
  });
  const stateEditor = window.CompanionStateEditor.create({
    ...editorReads(), changed:renderStateSettings,
    failed:error=>saveError(error.message || "状态设置暂时无法保存，请刷新后再试。")
  });
  function renderStateSettings() {
    $("state-settings-editor").innerHTML = stateEditor.markup(stateFeature === "maintenance"
      ? {configPanel:"body"} : {feature:stateFeature}) || "正在读取状态设置…";
    /* 日程页的「编辑文案」由 stateEditor 打开草稿，表单在它的规划面板里渲染；
       这里在日程页放一份规划面板，文案表单就出现在按钮所在的页面。 */
    $("calendar-planning").innerHTML = stateEditor.markup({configPanel:"planning"}) || "";
    $("calendar-planning").hidden = !$("calendar-planning").innerHTML.trim();
  }
  /* 日程页：月历 + 所选日期的时间线由 calendar-view.js 生成；下方日程记录与
     全部编辑仍由 calendar-editor.js 负责，这里只换呈现，不碰编辑流程。 */
  function renderCalendar() {
    const items=(editorState?.calendar?.items || []).map((item,index)=>({item,index}));
    $("calendar-editor").innerHTML = calendar.markup({items}) || (fetching ? "正在读取日程…" : "<p class=\"gl-empty\">日程暂未读取。</p>");
  }
  /* 日期读取：月历按月请求 calendar/occurrences，所选的非今天日期请求 life/activities。
     结果按当前角色缓存；换角色或日程保存后整体作废。scopeGeneration 用来丢弃
     切换角色之前发出、之后才返回的响应。 */
  let months = new Map(), days = new Map(), wallet, scopeGeneration = 0;
  function resetDateReads() { months = new Map(); days = new Map(); wallet = undefined; }
  const readFailure = error => ({status:"error", code:error?.code || "read_failed"});
  async function fetchMonth(range) {
    /* 已有结果（含失败）就不再自动请求，否则失败后重绘会立即重试、形成请求循环；
       失败的月份只由「重新读取」按钮删除条目后重读。 */
    if(!selectedRole || months.has(range.key))return;
    const generation = scopeGeneration, store = months;
    store.set(range.key, {status:"loading"});
    let entry;
    try {
      const data = await api("GET", `${endpoint()}/calendar/occurrences`, {start:range.start, end:range.end});
      entry = data && Array.isArray(data.items) ? {status:"ok", data} : readFailure({code:"invalid_response"});
    } catch(error) { entry = readFailure(error); }
    if(generation !== scopeGeneration || store !== months)return;
    store.set(range.key, entry); renderCalendarView();
  }
  async function fetchDay(key, more=false) {
    const current = days.get(key);
    if(!selectedRole || current?.status === "loading" || current?.more || (!more && current) || (more && !current?.next_cursor))return;
    const generation = scopeGeneration, store = days;
    store.set(key, more ? {...current, more:true, moreError:false} : {status:"loading"});
    if(more)renderCalendarView();
    let entry;
    try {
      const data = await api("GET", `${endpoint()}/life/activities`, {date:key, limit:50, ...(more ? {before:current.next_cursor} : {})});
      if(!data || !Array.isArray(data.items))throw Object.assign(new Error("invalid_response"), {code:"invalid_response"});
      const cursor = typeof data.next_cursor === "string" && data.next_cursor ? data.next_cursor : null;
      entry = {status:"ok", items:[...(more ? current.items : []), ...data.items], next_cursor:cursor,
        timezone:typeof data.timezone === "string" ? data.timezone : more ? current.timezone : ""};
    } catch(error) { entry = more ? {...current, more:false, moreError:true} : readFailure(error); }
    if(generation !== scopeGeneration || store !== days)return;
    store.set(key, entry); renderCalendarView();
  }
  /* 只在日程页可见时发请求；总览不需要这两份数据。 */
  function ensureDateReads(input) {
    if(page !== "calendar" || !selectedRole || !calendarView)return;
    void fetchMonth(window.GoodLifeCalendarView.monthRange(calendarView));
    const selected = calendarView.selected;
    if(selected && selected !== window.GoodLifeCalendarView.today(input))void fetchDay(selected);
  }
  function renderCalendarView() {
    const view = window.GoodLifeCalendarView, input = {...snapshotInput(), ...calendarDisclosures, showSnapshotTime:true};
    if(!calendarView || !calendarViewTouched)calendarView = view.initial(input);
    const full = {...input, view:calendarView, months:key=>months.get(key), history:days.get(calendarView.selected)};
    // 月历接口失败时，用最近验证的规则推算，即使编辑器正在保留旧草稿。
    const monthInput = monthState ? {...full, state:monthState} : full;
    /* 异步结果到达时会重绘月历；保住键盘焦点所在的格子或按钮。 */
    const active = document.activeElement, focus = $("calendar-month").contains(active) || $("calendar-day").contains(active)
      ? ["data-cal-day","data-cal-month","data-cal-today","data-cal-retry","data-cal-history-more"].find(name=>active.hasAttribute(name)) : null;
    const focusValue = focus ? active.getAttribute(focus) : null;
    const summary = disclosureFocus($("calendar-day"));
    $("calendar-month").innerHTML = view.month(monthInput);
    $("calendar-day").innerHTML = view.day(full);
    if(focus)document.querySelector(`#calendar [${focus}="${CSS.escape(focusValue)}"]`)?.focus();
    if(summary)$("calendar-day").querySelector(summary)?.focus();
    ensureDateReads(input);
  }
  /* 钱包页（只读）：进入钱包页才读取 life/wallet，结果按当前角色缓存，换角色时随日期读取一起作废。
     「加载更多」用上一批的 next_before 往后读，接在已显示的记录后面；失败时保留已读到的部分。 */
  async function fetchWallet(more=false) {
    if(!selectedRole || wallet?.status === "loading" || wallet?.more || (!more && wallet) || (more && !wallet?.next_before))return;
    const view = window.GoodLifeWallet, generation = scopeGeneration, current = wallet;
    const pending = more ? {...current, more:true, moreError:false} : {status:"loading"};
    wallet = pending;
    renderWallet();
    let entry;
    try {
      const data = await api("GET", `${endpoint()}/life/wallet`, view.query(current, more));
      entry = more ? view.merge(current, data) : view.normalize(data);
    } catch(error) { entry = more ? {...current, more:false, moreError:true} : readFailure(error); }
    if(generation !== scopeGeneration || wallet !== pending)return;
    wallet = entry; renderWallet();
  }
  function renderWallet() {
    /* 读取结果到达时整块重绘；保住「加载更多 / 重新读取」上的键盘焦点。 */
    const active = document.activeElement, focus = $("wallet").contains(active)
      ? ["data-wallet-more","data-wallet-retry"].find(name=>active.hasAttribute(name)) : null;
    const entry = wallet || (!selectedRole && !fetching ? {status:"error", code:""} : undefined);
    $("wallet").innerHTML = window.GoodLifeWallet.render({entry, zone:stateTimezone || lifeTimezone || "Asia/Shanghai", now:Date.now()});
    if(focus)$("wallet").querySelector(`[${focus}]`)?.focus();
    if(page === "wallet")void fetchWallet();
  }
  /* 课表页：状态与请求都在 timetable-page.js；这里只给它当前角色的接口前缀，
     换角色时 reset()，进入页面时 show()。默认时区用角色的规划时区。 */
  /* 保存时控制器先用 scope() 记下当时的前缀，之后的每条命令都带着它发，换了角色也不会发到新角色上。 */
  const timetable = window.GoodLifeTimetablePage.create({root:$("timetable"),
    scope:() => selectedRole ? endpoint() : null,
    api:(method, path, body, pinned) => pinned || selectedRole ? api(method, `${pinned || endpoint()}/${path}`, body)
      : Promise.reject(Object.assign(new Error("请先选择角色。"), {code:"no_role"})),
    zone:() => stateTimezone || lifeTimezone});
  const captureScope = () => ({path:endpoint(), generation:scopeGeneration});
  const isCurrentScope = scope => scope.generation === scopeGeneration && !!selectedRole && scope.path === endpoint();
  const editorScopeFor = scope => ({installation_id:catalog.installation_id,operator_id:catalog.operator_id,path:`${scope.path}/state`});
  const pollInterval = 60000, maxPollDelay = 480000;
  let pollDelay = pollInterval, pollDueAt, pollTimer, pendingSnapshot, queuedSnapshot, stateReads = 0;
  let stateReadSequence = 0, verifiedStateSequence = 0;
  let stateTimezone, lifeTimezone;
  function rememberTimezone(source, zone) {
    if(!zone)return;
    try { new Intl.DateTimeFormat("zh-CN", {timeZone:zone}); } catch { return; }
    if(source === "state")stateTimezone=zone;
    else lifeTimezone=zone;
    if(page === "wallet")renderWallet();
  }
  function schedulePoll() {
    if(!["overview","calendar","settings"].includes(page))return;
    if(pollTimer != null || pendingSnapshot || fetching || stateReads || !selectedRole)return;
    if(document.visibilityState !== "visible")return;
    pollTimer = setTimeout(()=>{
      pollTimer=null;
      if(!stateReads && document.visibilityState === "visible")void readSnapshot();
    }, Math.max(0, pollDueAt - Date.now()));
  }
  function requestSnapshot() {
    if(!selectedRole)return Promise.resolve(null);
    clearTimeout(pollTimer);
    pollTimer=null;
    if(!pendingSnapshot) {
      void readSnapshot();
      return pendingSnapshot.state;
    }
    if(!queuedSnapshot) {
      let resolve;
      const state = new Promise(done => { resolve = done; });
      queuedSnapshot = {state, resolve};
    }
    return queuedSnapshot.state;
  }
  async function readPart(scope, source, path, query, options) {
    try {
      const result = await api("GET", path, query, options);
      if(!isCurrentScope(scope))return null;
      if(!result)throw new Error();
      readNotice(source, "");
      return result;
    } catch(error) {
      if(isCurrentScope(scope))readNotice(source, error.message || (source === "life" ? "生活记录暂时无法读取。" : "日程暂时无法读取。"));
      return null;
    }
  }
  /* 当天课次：和课表页一样读 academics/term 取当前学期，再按日期读 academics/occurrences（end 不含）。
     未启用、没有学期或读取出错时返回 null，总览据此不把空白说成空闲；今天不在任何学期里就是没有课。
     地点名称只在有课时顺带读 academics/places，读不到就不显示地点。 */
  async function readClasses(scope, range, options) {
    try {
      const terms = await api("GET", `${scope.path}/academics/term`, {}, options);
      if(!isCurrentScope(scope) || !Array.isArray(terms?.terms) || !terms.terms.length)return null;
      const term = terms.terms.find(item => item.start_date <= range.start && range.start < item.end_date);
      if(!term)return {...range, items:[]};
      const data = await api("GET", `${scope.path}/academics/occurrences`, {term_id:term.term_id, ...range}, options);
      if(!isCurrentScope(scope) || data?.start !== range.start || data.end !== range.end || !Array.isArray(data.items))return null;
      let places = new Map();
      if(data.items.some(item => item.place_id && !item.cancelled)) {
        try {
          const setup = await api("GET", `${scope.path}/academics/places`, {}, options);
          places = new Map((Array.isArray(setup?.places) ? setup.places : []).map(place => [place.place_id, place.name]));
        } catch { /* 地点只是附加说明。 */ }
      }
      return {...range, items:data.items.map(item => ({id:item.session_id, title:item.course_name, start:item.start_at,
        end:item.end_at, place:places.get(item.place_id) || "", cancelled:item.cancelled === true}))};
    } catch { return null; }
  }
  async function readSnapshot(initial=false) {
    if(!selectedRole || pendingSnapshot)return;
    const scope = captureScope(), editorScope = editorScopeFor(scope), sequence = ++stateReadSequence;
    const options = initial ? undefined : {timeoutMs:20000};
    const lifeRead = readPart(scope, "life", `${scope.path}/life`, {overview:"1"}, options).then(life=>{
      if(life && isCurrentScope(scope))rememberTimezone("life", life.config?.timezone);
      return life;
    });
    const rawStateRead = readPart(scope, "state", editorScope.path, {}, options).then(state=>{
      if(!state || !isCurrentScope(scope))return null;
      acceptState(editorScope, state, sequence);
      return state;
    });
    let classesRead = Promise.resolve(null);
    const stateRead = rawStateRead.then(async state=>{
      if(!state || !isCurrentScope(scope))return null;
      const zone = state.settings?.planning_timezone;
      if(!zone)return state;
      const range = window.GoodLifeOverview.calendarRequest({state}, Date.now());
      const calendarRead = api("GET", `${scope.path}/calendar/occurrences`, {...range, include_cancelled:"1"}, options);
      classesRead = readClasses(scope, range, options);
      try {
        const result = await calendarRead;
        if(result?.start === range.start && result.end === range.end && result.timezone === zone && Array.isArray(result.items)) {
          return {...state, occurrences:result};
        }
      } catch { /* /state 的期次仍可展示，但没有覆盖范围，不能判断空闲。 */ }
      return state;
    });
    const classes = stateRead.then(() => classesRead);
    const request = Promise.allSettled([lifeRead, stateRead, classes]);
    pendingSnapshot={done:request, state:rawStateRead};
    try {
      const [nextLife, nextState, nextClasses] = await request;
      if(!isCurrentScope(scope))return;
      fetching=false;
      if(nextLife.value && nextState.value) {
        snapshot={state:nextState.value, life:nextLife.value, classes:nextClasses.value ?? null, snapshotUpdatedAt:Date.now()};
        pollDelay=pollInterval;
      } else pollDelay=Math.min(pollDelay * 2, maxPollDelay);
      if(initial && !editorState)renderCalendar();
      renderOverview(); renderCalendarView();
    } finally {
      if(isCurrentScope(scope)) {
        pendingSnapshot=null;
        pollDueAt=Date.now() + pollDelay;
        const queued = queuedSnapshot;
        queuedSnapshot=null;
        if(queued)queued.resolve(requestSnapshot());
        else schedulePoll();
      }
    }
  }
  function acceptState(scope, fresh, sequence, explicit=false) {
    if(sequence < verifiedStateSequence)return;
    verifiedStateSequence=sequence;
    monthState=fresh;
    rememberTimezone("state", fresh.settings?.planning_timezone);
    // A background refresh must keep the editor's draft and cursor together.
    if(explicit || (!calendar.dirty && !stateEditor.dirty)) {
      editorState=fresh;
      calendar.setScope(scope); stateEditor.setScope(scope);
      calendar.setStatus(fresh); stateEditor.setStatus(fresh);
      renderCalendar(); renderStateSettings();
    }
    renderCalendarView();
  }
  async function loadState(afterId="") {
    if(!selectedRole)return null;
    const scope = captureScope(), editorScope = editorScopeFor(scope);
    stateReads++;
    try {
      /* Explicit editor rebase and pagination still use their own state request. */
      while(isCurrentScope(scope) && pendingSnapshot)await pendingSnapshot.done;
      if(!isCurrentScope(scope))return null;
      const sequence = ++stateReadSequence;
      const fresh = await readPart(scope, afterId ? "state-page" : "state", editorScope.path, afterId ? {after_id:afterId} : {});
      if(!fresh || !isCurrentScope(scope))return null;
      acceptState(editorScope, fresh, sequence, true);
      return fresh;
    } finally {
      if(isCurrentScope(scope)) { stateReads--; schedulePoll(); }
    }
  }
  let mappedIdentity;
  function chooseRole() {
    selectedRole=selectedTarget?.roles.find(item=>item.token===$("role").value) || selectedTarget?.roles[0];
    const identity = selectedRole ? JSON.stringify([catalog.installation_id, catalog.operator_id, endpoint()]) : null;
    if(identity === mappedIdentity) {
      tellFrame(false);
      return;
    }
    mappedIdentity=identity;
    snapshot=editorState=monthState=stateTimezone=lifeTimezone=null;
    fetching=!!selectedRole; calendarViewTouched=false;
    clearTimeout(pollTimer); pollTimer=null; pollDelay=pollInterval; pendingSnapshot=null; stateReads=0;
    queuedSnapshot?.resolve(null); queuedSnapshot=null;
    for(const saved of [overviewDisclosures, calendarDisclosures]) { saved.pastOpen=false; saved.openReasons.clear(); }
    scopeGeneration++; resetDateReads(); readErrors.clear(); renderNotice(); tellFrame(false);
    calendar.invalidate(); stateEditor.invalidate();
    timetable.reset(); if(page==="timetable" && selectedRole)timetable.show();
    renderOverview(); renderCalendar(); renderStateSettings(); renderCalendarView(); renderWallet(); void readSnapshot(true);
  }
  function chooseTarget() {
    const previousTarget = selectedTarget?.id, previousRole = selectedRole?.token;
    selectedTarget=catalog?.targets.find(item=>item.id===$("target").value) || catalog?.targets[0];
    $("role").replaceChildren();
    for(const role of selectedTarget?.roles || []) { const option=node("option",role.name); option.value=role.token; $("role").append(option); }
    if(selectedTarget?.id === previousTarget && selectedTarget?.roles.some(role=>role.token === previousRole)) $("role").value=previousRole;
    $("role").disabled=!selectedTarget; chooseRole();
  }
  async function connect() {
    try {
      const status=await api("GET","status",{});
      $("connection").textContent=status.runtime_ready ? "生活系统已连接" : "等待陪伴插件连接";
      $("connection").parentElement.toggleAttribute("data-offline",!status.runtime_ready);
      catalog=await api("GET","targets",{}); $("target").replaceChildren();
      for(const target of catalog.targets) { const option=node("option",target.name); option.value=target.id; $("target").append(option); }
      $("target").disabled=!catalog.targets.length;
      if(catalog.targets.some(target=>target.id === selectedTarget?.id))$("target").value=selectedTarget.id;
      const previousGeneration = scopeGeneration;
      chooseTarget();
      if(selectedRole && scopeGeneration === previousGeneration) {
        resetDateReads();
        requestSnapshot();
        renderCalendarView(); renderWallet();
        timetable.reset(); if(page==="timetable")timetable.show();
      }
      connectionError(catalog.targets.length ? "" : "请先在陪伴小屋配置私聊窗口与角色。");
    } catch(error) { $("connection").textContent="连接暂不可用"; $("connection").parentElement.setAttribute("data-offline","");
      if(!selectedRole) { fetching=false; renderOverview(); renderCalendar(); }
      connectionError(error.message || "请确认陪伴插件已启动，然后刷新。"); }
  }
  function selectPage(value) {
    page=value; document.querySelectorAll("[data-page]").forEach(button=>button.setAttribute("aria-current",button.dataset.page===page?"page":"false"));
    $("overview").hidden=page!=="overview"; $("calendar").hidden=page!=="calendar"; $("wallet").hidden=page!=="wallet";
    $("timetable").hidden=page!=="timetable";
    $("workspace").hidden=!["reading","browsing","settings"].includes(page);
    $("reading-nav").hidden=page!=="reading"; $("state-settings").hidden=page!=="settings";
    [$("page-eyebrow").textContent, $("page-title").textContent] = titles[page] || titles.overview;
    setRail(false); tellFrame();
    if(["overview","calendar","settings"].includes(page))schedulePoll();
    else { clearTimeout(pollTimer); pollTimer=null; }
    if(page==="calendar")renderCalendarView();
    if(page==="wallet")renderWallet();
    if(page==="timetable" && selectedRole)timetable.show();
  }
  /* 窄屏时左栏是抽屉：默认收起，由顶部按钮打开，选中一项或点遮罩后收起。 */
  function setRail(open) {
    $("shell").dataset.rail = open ? "open" : "closed"; $("rail-scrim").hidden = !open;
    $("rail-open").setAttribute("aria-expanded", String(open));
  }
  $("rail-open").addEventListener("click", ()=>setRail(true));
  $("rail-close").addEventListener("click", ()=>setRail(false));
  $("rail-scrim").addEventListener("click", ()=>setRail(false));
  document.addEventListener("keydown", event=>{ if(event.key==="Escape" && $("shell").dataset.rail==="open")setRail(false); });
  $("navigation").addEventListener("click", event=>{ const button=event.target.closest("[data-page]"); if(button)selectPage(button.dataset.page); });
  $("overview").addEventListener("click", event=>{ const button=event.target.closest("[data-go]"); if(button)selectPage(button.dataset.go); });
  trackDisclosures($("overview"), overviewDisclosures);
  trackDisclosures($("calendar-day"), calendarDisclosures);
  document.addEventListener("visibilitychange", ()=>{
    if(document.visibilityState === "visible")schedulePoll();
    else { clearTimeout(pollTimer); pollTimer=null; }
  });
  setInterval(()=>{ if(document.hidden){ renderClock(); return; }
    if(page==="overview")renderOverview(); else { renderClock(); if(page==="calendar")renderCalendarView(); } }, pollInterval);
  /* 月历：切月、回到今天、选日期先改本地视图；该月或该日尚未读取时，renderCalendarView 再按需请求。 */
  $("calendar-month").addEventListener("click", event=>{
    const view=window.GoodLifeCalendarView, input=snapshotInput();
    if(event.target.closest("[data-cal-retry]") && calendarView) { months.delete(view.monthRange(calendarView).key); renderCalendarView(); return; }
    const day=event.target.closest("[data-cal-day]"), month=event.target.closest("[data-cal-month]");
    if(day)calendarView=view.select(calendarView, day.dataset.calDay);
    else if(month)calendarView=view.shift(calendarView, Number(month.dataset.calMonth));
    else if(event.target.closest("[data-cal-today]"))calendarView=view.initial(input);
    else return;
    calendarViewTouched=true; renderCalendarView();
    const focus=day ? `[data-cal-day="${day.dataset.calDay}"]` : month ? `[data-cal-month="${month.dataset.calMonth}"]` : "[data-cal-today]";
    $("calendar-month").querySelector(focus)?.focus();
  });
  /* 方向键在月历里按天 / 周移动，跨月时自动翻页。 */
  $("calendar-month").addEventListener("keydown", event=>{
    const day=event.target.closest("[data-cal-day]"), step={ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7}[event.key];
    if(!day || !step)return;
    event.preventDefault();
    const at=new Date(Date.parse(day.dataset.calDay+"T00:00:00Z")+step*86400000).toISOString().slice(0,10);
    calendarView=window.GoodLifeCalendarView.select(calendarView, at); calendarViewTouched=true; renderCalendarView();
    $("calendar-month").querySelector(`[data-cal-day="${at}"]`)?.focus();
  });
  /* 所选日期卡片：重新读取失败的月份 / 当天记录，或读取当天更多活动记录。 */
  $("calendar-day").addEventListener("click", event=>{
    if(!calendarView)return;
    if(event.target.closest("[data-cal-history-more]")) { void fetchDay(calendarView.selected, true); return; }
    const retry=event.target.closest("[data-cal-retry]")?.dataset.calRetry;
    if(retry==="month")months.delete(window.GoodLifeCalendarView.monthRange(calendarView).key);
    else if(retry==="history")days.delete(calendarView.selected);
    else return;
    renderCalendarView();
  });
  /* 钱包页只有两种按钮：读取更多收支记录、整页重新读取。没有任何写操作。 */
  $("wallet").addEventListener("click", event=>{
    if(event.target.closest("[data-wallet-more]")) { void fetchWallet(true); return; }
    if(!event.target.closest("[data-wallet-retry]"))return;
    if(!selectedRole) { void connect(); return; }
    wallet=undefined; renderWallet();
  });
  $("reading-nav").addEventListener("click", event=>{const button=event.target.closest("[data-reading]"); if(!button)return;
    readingPage=button.dataset.reading; document.querySelectorAll("[data-reading]").forEach(item=>item.setAttribute("aria-current",item===button?"page":"false")); tellFrame();});
  $("state-features").addEventListener("click",event=>{const button=event.target.closest("[data-state-feature]");
    if(!button || !stateEditor.discardIfPristine())return; stateFeature=button.dataset.stateFeature;
    document.querySelectorAll("[data-state-feature]").forEach(item=>item.setAttribute("aria-current",item===button?"page":"false"));
    renderStateSettings();});
  $("state-settings-editor").addEventListener("click",event=>stateEditor.click(event));
  $("state-settings-editor").addEventListener("input",event=>stateEditor.input(event));
  $("state-settings-editor").addEventListener("change",event=>stateEditor.input(event));
  $("state-settings-editor").addEventListener("submit",event=>stateEditor.submit(event));
  $("companion-link").addEventListener("click",event=>{if(!event.ctrlKey && !event.metaKey && !event.shiftKey)event.preventDefault();});
  $("target").addEventListener("change",chooseTarget); $("role").addEventListener("change",chooseRole); $("refresh").addEventListener("click",connect);
  $("calendar-editor").addEventListener("click",event=>{
    if(calendar.click(event))return;
    /* 「编辑文案」按钮由 calendar-editor 渲染，但草稿归 stateEditor 管。 */
    if(event.target.closest("[data-state-edit]")) { stateEditor.click(event); return; }
    const button=event.target.closest("[data-state-page]"); if(button)void loadState(button.dataset.statePage);
  });
  $("calendar-planning").addEventListener("click",event=>stateEditor.click(event));
  $("calendar-planning").addEventListener("input",event=>stateEditor.input(event));
  $("calendar-planning").addEventListener("change",event=>stateEditor.input(event));
  $("calendar-planning").addEventListener("submit",event=>stateEditor.submit(event));
  $("calendar-editor").addEventListener("input",event=>calendar.input(event));
  $("calendar-editor").addEventListener("change",event=>calendar.change(event));
  $("calendar-editor").addEventListener("submit",event=>calendar.submit(event));
  window.addEventListener("message",async event=>{
    if(event.source!==frame.contentWindow || event.data?.channel!=="companion-workspace")return;
    const message=event.data;
    if(message.kind==="ready"){frameReady=true;
      frame.contentWindow.postMessage({channel:"companion-workspace",kind:"theme",theme:document.documentElement.dataset.theme}, "*");
      tellFrame(false);return;}
    if(message.kind==="resize" && Number.isFinite(message.height)) { frame.style.height=Math.max(600,Math.min(message.height+24,10000))+"px"; return; }
    if(message.kind!=="request")return;
    const isRead = message.method === "GET";
    if(isRead ? message.readGeneration !== scopeGeneration : !selectedRole)return;
    const scope = selectedRole ? captureScope() : null;
    const reply={channel:"companion-workspace",kind:"response",requestId:message.requestId};
    const respond = payload => {
      if(!isRead || (scope ? isCurrentScope(scope) : message.readGeneration === scopeGeneration)) {
        frame.contentWindow.postMessage({...reply, ...payload},"*");
      }
    };
    try {
      if(!scope || !["life","life/command"].includes(message.path) || !["GET","POST"].includes(message.method))throw new Error("此操作请在陪伴小屋完成。");
      let body=message.body;
      if(message.path==="life/command" && body?.action==="configure")body={...body,changes:Object.fromEntries(
        Object.entries(body.changes || {}).filter(([key])=>allowedSettings.has(key)))};
      const data=await api(message.method,`${scope.path}/${message.path}`,body,forwardedTimeout(message.timeoutMs));
      respond({ok:true,data});
    } catch(error) { respond({ok:false,error:error.message,code:error.code,
      reason:error.message,confirmed:error.confirmed===true}); }
  });
  frame.srcdoc = window.GoodLifeWorkspace;
  renderOverview(); renderCalendar();
  (async()=>{try{bridge=window.AstrBotPluginPage;await bridge.ready();await connect();}catch(_){fetching=false; renderOverview(); renderCalendar(); connectionError("请从已登录的 AstrBot 插件页面打开。");}})();
})();
