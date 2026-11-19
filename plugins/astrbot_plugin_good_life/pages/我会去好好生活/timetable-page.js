/* 课表页的状态与请求。标记全部由 timetable-view.js 生成。
 *
 * 读取：academics/places（开关与地点）、academics/term、academics/timetable、
 *       academics/occurrences（只读当前看的那一周）。
 * 写入：只有 academics/command。每条命令带上最近一次读到的 revision；
 *       命令成功后用回执里的 revision 接着发下一条，全部完成后整页重读。
 * 换角色时 app.js 调 reset()，世代号让切换前发出的响应作废。 */
"use strict";
window.GoodLifeTimetablePage = (() => {
  const fresh = () => ({status:"idle", code:"", setup:null, revision:0, termRevision:0, terms:[], termId:null, entries:null, week:0,
    occurrences:null, panel:null, draft:null, placeFilter:"", confirm:null, busy:false, message:null});
  const hex = () => {
    const bytes = new Uint8Array(8);
    globalThis.crypto.getRandomValues(bytes);
    return [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
  };
  /* 表单字段 → 普通对象（不依赖 FormData，测试里可用替身）。 */
  const values = form => Object.fromEntries([...(form?.elements || [])]
    .filter(item => item.name && !item.disabled).map(item => [item.name, item.value]));

  /* scope() 返回当前角色的接口前缀；api(method, path, body, scope) 收到 scope 时必须用它而不是当前角色。 */
  function create({root, api, scope = () => "current", zone = () => undefined, now = () => Date.now(), random = hex}) {
    const V = window.GoodLifeTimetable;
    let state = fresh(), generation = 0;
    const ids = {term:() => "term-" + random(), course:() => "course-" + random(), slot:() => "slot-" + random()};
    const term = () => state.terms.find(item => item.term_id === state.termId) || null;
    const slot = id => (state.entries || []).find(item => item.slot_id === id) || null;
    const group = id => V.courses(state.entries).find(item => item.course.course_id === id) || null;
    const today = () => V.localParts(now(), term()?.timezone || zone()).date;

    function render(focus) {
      root.innerHTML = V.render(state, {now:now(), zone:zone()});
      if (!focus) return;
      const target = root.querySelector(focus);
      target?.focus?.();
      if (focus === "[data-tt-place-filter]" && target?.setSelectionRange) target.setSelectionRange(target.value.length, target.value.length);
      if (focus.startsWith(".gl-tt-panel")) target?.scrollIntoView?.({block:"nearest"});
    }
    /* 只接收本世代的响应；切换角色后到达的旧响应丢弃。 */
    const current = run => run === generation;

    async function load({keepMessage = false} = {}) {
      const run = generation;
      state = {...state, status:state.status === "ready" ? "ready" : "loading", message:keepMessage ? state.message : null};
      if (state.status === "loading") render();
      const [places, terms] = await Promise.allSettled([api("GET", "academics/places", {}), api("GET", "academics/term", {})]);
      if (!current(run)) return;
      const setup = places.status === "fulfilled" && places.value && Array.isArray(places.value.places) ? places.value : null;
      if (setup?.academics_enabled === false || terms.reason?.code === "missing_dependency") {
        state = {...fresh(), status:"disabled", setup};
        return render();
      }
      if (terms.status !== "fulfilled" || !Array.isArray(terms.value?.terms)) {
        state = {...state, status:"error", code:terms.reason?.code || "invalid_response"};
        return render();
      }
      const list = terms.value.terms;
      const termId = list.some(item => item.term_id === state.termId) ? state.termId : V.defaultTerm(list, today())?.term_id || null;
      const changedTerm = termId !== state.termId;
      state = {...state, status:"ready", setup, revision:terms.value.revision, termRevision:terms.value.revision, terms:list, termId,
        week:changedTerm ? 0 : state.week, entries:changedTerm ? null : state.entries, occurrences:changedTerm ? null : state.occurrences};
      render();
      await loadTerm(run);
    }

    async function loadTerm(run = generation) {
      const selected = term();
      if (!selected) return;
      let data;
      try { data = await api("GET", "academics/timetable", {term_id:selected.term_id}); }
      catch (error) { if (current(run)) { state = {...state, entries:[], message:{tone:"warn", text:"课程暂时读不到：" + V.reason(error)}}; render(); } return; }
      if (!current(run) || state.termId !== selected.term_id) return;
      const count = V.windows(selected).length;
      const week = state.week >= 1 && state.week <= count ? state.week : V.defaultWeek(selected, today());
      state = {...state, revision:data.revision, entries:Array.isArray(data.entries) ? data.entries : [], week};
      render();
      await loadWeek(run);
    }

    async function loadWeek(run = generation) {
      const selected = term();
      if (!selected || !state.week) return;
      const range = V.weekRange(selected, state.week), key = `${selected.term_id}:${state.week}`;
      state = {...state, occurrences:{key, status:"loading", items:state.occurrences?.key === key ? state.occurrences.items : []}};
      render();
      let entry;
      try {
        const data = await api("GET", "academics/occurrences", {term_id:selected.term_id, start:range.start, end:range.end});
        entry = Array.isArray(data?.items) ? {key, status:"ok", items:data.items} : {key, status:"error", code:"invalid_response"};
      } catch (error) { entry = {key, status:"error", code:error?.code || "read_failed"}; }
      if (!current(run) || state.occurrences?.key !== key) return;
      state = {...state, occurrences:entry};
      render();
    }

    /* 依次发命令。开始保存时就定下接口前缀（角色）和 revision；step(send) 里每次 send 都用上一条的回执 revision。
       每条命令发出前都核对世代号：中途换了角色就停下，绝不把 A 角色的数据用 B 角色的前缀发出去。 */
    async function save(step, done, {revision:pinned} = {}) {
      if (state.busy) return;
      const run = generation, endpoint = scope();
      let revision = pinned ?? state.revision;
      state = {...state, busy:true, message:null};
      render();
      const send = async (action, kind, id, record) => {
        if (!current(run) || !endpoint) throw Object.assign(new Error("scope changed"), {code:"scope_changed"});
        const body = V.command(action, kind, id, record, revision, "page-" + random());
        const receipt = await api("POST", "academics/command", body, endpoint);
        if (Number.isInteger(receipt?.revision)) revision = receipt.revision;
        if (current(run)) state = {...state, revision};
        return receipt;
      };
      try {
        const result = await step(send);
        if (!current(run)) return;
        state = {...state, busy:false, panel:null, draft:null, placeFilter:"", confirm:null,
          message:{tone:"ok", text:done}, ...(result?.select ? {termId:result.select, week:0, entries:null, occurrences:null} : {})};
        render();
        await load({keepMessage:true});
      } catch (error) {
        if (!current(run)) return;
        const stale = error?.code === "stale_version";
        state = {...state, busy:false, message:{tone:"warn", text:V.reason(error)}};
        render();
        /* 多步保存可能只完成了一部分；任何失败后都重读，页面只显示后端的真实结果。 */
        if (stale || error?.code !== "invalid_input") await load({keepMessage:true});
      }
    }
    /* 表单校验错误不发请求，只提示。 */
    function invalid(error) {
      state = {...state, message:{tone:"warn", text:V.reason(error)}};
      render();
    }

    function open(panel) {
      state = {...state, panel, draft:null, placeFilter:"", confirm:null, message:null};
      render(".gl-tt-panel input:not([type=search]), .gl-tt-panel button");
    }

    /* 学期写入必须用学期读取时的 revision：表单里的放假、调休来自那次读取。
       之后课程读取拿到的 revision 不同，说明中间有别处改过，学期和课程一起重读再让用户确认。 */
    function termSave(step, done) {
      if (state.termRevision !== state.revision) {
        state = {...state, message:{tone:"warn", text:V.reason({code:"stale_version", message:"academic revision changed"})}};
        render();
        return load({keepMessage:true});
      }
      return save(step, done, {revision:state.termRevision});
    }

    /* === 保存动作 === */
    function saveTerm(form) {
      const existing = state.panel.termId ? state.terms.find(item => item.term_id === state.panel.termId) : null;
      let record;
      try { record = V.termRecord(form, existing, ids); } catch (error) { return invalid(error); }
      return termSave(async send => {
        await send(existing ? "update" : "create", "term", record.term_id, record);
        return existing ? null : {select:record.term_id};
      }, existing ? "学期已保存。" : "学期已创建，接着添加课程吧。");
    }

    function saveCourse(form) {
      const selected = term(), existing = state.panel.courseId ? group(state.panel.courseId) : null;
      let course, record;
      try {
        course = V.courseRecord(form, existing?.course, ids);
        if (!existing) record = V.slotRecord(form, {term:selected, course, ids});
      } catch (error) { return invalid(error); }
      if (!existing) return save(send => send("create", "timetable", record.slot_id, record), "课程已添加。");
      const same = ["name", "kind", "credits", "teacher_ref"].every(key => course[key] === existing.course[key]);
      if (same) { state = {...state, panel:null, draft:null}; return render(); }
      /* 一条 course 命令在一个事务里改写这门课所有上课时间的课程信息，编号不变。 */
      return save(send => send("update", "course", existing.course.course_id, {term_id:selected.term_id, ...course}), "课程已保存。");
    }

    function saveSlot(form) {
      const existing = state.panel.slotId ? slot(state.panel.slotId) : null;
      const owner = group(existing?.course.course_id || state.panel.courseId);
      let record;
      try { record = V.slotRecord(form, {term:term(), course:owner.course, existing, ids}); } catch (error) { return invalid(error); }
      const dropped = existing ? existing.exceptions.length - record.exceptions.length : 0;
      return save(send => send(existing ? "update" : "create", "timetable", record.slot_id, record),
        !existing ? "上课时间已添加。" : dropped ? `上课时间已保存。有 ${dropped} 次单独调整已经不在新的上课日子里，一并清除了。` : "上课时间已保存。");
    }

    function saveSession(change, done) {
      const session = state.occurrences?.items?.find(item => item.session_id === state.panel?.sessionId);
      const owner = session && slot(session.slot_id);
      if (!owner) return;
      let record;
      try { record = V.withException(owner, session.local_date, change); } catch (error) { return invalid(error); }
      return save(send => send("update", "timetable", owner.slot_id, record), done);
    }

    function deleteTerm(id) {
      if ((state.entries || []).some(item => item.term_id === id))
        return invalid(Object.assign(new Error("这个学期里还有课程，请先删掉课程。"), {code:"invalid_input"}));
      return termSave(send => send("delete", "term", id), "学期已删除。");
    }
    const deleteCourse = id => save(async send => {
      for (const item of group(id)?.slots || []) await send("delete", "timetable", item.slot_id);
    }, "课程已删除。");
    const deleteSlot = id => save(send => send("delete", "timetable", id), "上课时间已删除。");

    /* === 事件 === */
    function click(event) {
      const hit = name => event.target.closest?.(`[${name}]`);
      const data = (name, el = hit(name)) => el?.getAttribute(name);
      if (hit("data-tt-reload")) return void load();
      if (hit("data-tt-retry")) return void loadWeek();
      if (hit("data-tt-week")) {
        const week = Number(data("data-tt-week"));
        if (!Number.isInteger(week) || week < 1 || week > (term() ? V.windows(term()).length : 0)) return;
        state = {...state, week, panel:state.panel?.kind === "session" ? null : state.panel};
        return void loadWeek();
      }
      if (hit("data-tt-close")) { state = {...state, panel:null, draft:null, confirm:null}; return render(); }
      if (hit("data-tt-unconfirm")) { state = {...state, confirm:null}; return render(); }
      if (hit("data-tt-confirm")) { state = {...state, confirm:data("data-tt-confirm")}; return render(); }
      if (state.busy) return;
      if (hit("data-tt-new-term")) return open({kind:"term"});
      if (hit("data-tt-edit-term")) return open({kind:"term", termId:data("data-tt-edit-term")});
      if (hit("data-tt-new-course")) return open({kind:"course"});
      if (hit("data-tt-edit-course")) return open({kind:"course", courseId:data("data-tt-edit-course")});
      if (hit("data-tt-new-slot")) return open({kind:"slot", courseId:data("data-tt-new-slot")});
      if (hit("data-tt-edit-slot")) return open({kind:"slot", slotId:data("data-tt-edit-slot")});
      if (hit("data-tt-session")) return open({kind:"session", sessionId:data("data-tt-session")});
      if (hit("data-tt-delete-term")) return void deleteTerm(data("data-tt-delete-term"));
      if (hit("data-tt-delete-course")) return void deleteCourse(data("data-tt-delete-course"));
      if (hit("data-tt-delete-slot")) return void deleteSlot(data("data-tt-delete-slot"));
      if (hit("data-tt-session-cancel")) return void saveSession({cancelled:true}, "这节课已停课。");
      if (hit("data-tt-session-restore")) return void saveSession(null, "这节课已恢复原样。");
    }
    function input(event) {
      const form = event.target.closest?.("[data-tt-form]");
      if (!form) return;
      state = {...state, draft:values(form)};
      if (event.target.matches?.("[data-tt-place-filter]")) {
        state = {...state, placeFilter:event.target.value};
        return render("[data-tt-place-filter]");
      }
      const preview = root.querySelector("[data-tt-preview] span");
      const session = state.occurrences?.items?.find(item => item.session_id === state.panel?.sessionId);
      if (preview && session) preview.textContent = V.sessionPreview(session, slot(session.slot_id), state.draft, state.setup, term()?.timezone);
    }
    function change(event) {
      if (event.target.matches?.("[data-tt-term]")) {
        state = {...state, termId:event.target.value, week:0, entries:null, occurrences:null, panel:null, draft:null, message:null};
        render();
        return void loadTerm();
      }
      /* 搜索框失焦时也会触发 change；输入时已经处理过，这里不再重绘，免得把焦点拉回去。 */
      if (event.target.matches?.("[data-tt-place-filter]")) return;
      input(event);
    }
    function submit(event) {
      const form = event.target.closest?.("[data-tt-form]");
      if (!form) return;
      event.preventDefault();
      if (state.busy) return;
      const fields = values(form);
      ({term:saveTerm, course:saveCourse, slot:saveSlot,
        session:value => saveSession(value, "这节课已调整。")}[form.dataset.ttForm])?.(fields);
    }

    root.addEventListener("click", click);
    root.addEventListener("input", input);
    root.addEventListener("change", change);
    root.addEventListener("submit", submit);

    return {
      show() { if (state.status === "idle") void load(); else render(); },
      reset() { generation++; state = fresh(); render(); },
      render,
      get state() { return state; },
    };
  }
  return {create, values};
})();
