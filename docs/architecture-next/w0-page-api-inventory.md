# W0 页面接口清单

盘点日期：2026-11-16。代码基线：`c83ad623389a32234422e689618a0b874915a1f0`。本文件记录当前源码可证明的页面契约，不代表接口已经在线验收或目标架构已经实现。

## 1. 范围、证据与计数

仅盘点 `plugins/astrbot_plugin_good_life/pages/我会去好好生活/` 下全部 JS、生活插件注册路由，以及这些页面实际转发到的主插件处理链。主插件自己的日记页、工作区和其他页面不在范围内。

下文路径缩写均相对仓库根目录：

- `G` = `plugins/astrbot_plugin_good_life`。
- `P` = `plugins/astrbot_plugin_good_life/pages/我会去好好生活`。
- `H` = `plugins/astrbot_plugin_private_companion/astrbot_host`。
- `C` = `plugins/astrbot_plugin_private_companion/companion_v2`。
- `T` = `targets/{encodeURIComponent(target.id)}/{encodeURIComponent(role.token)}`。
- `B` = 注册前缀 `/astrbot_plugin_good_life`。这是传给 AstrBot `register_web_api` 的路径；SDK 的最终网络 URL 包装不在本仓库中，不能据此断言浏览器直接请求的 URL 就是该字符串。

完整阅读范围和用途：

| 文件 | 完整阅读范围 | 接口职责 |
|---|---|---|
| `P/app.js` | 1–161 行 | 外层 SDK、连接和角色选择、生活总览、日程和状态编辑器、iframe 请求转发 |
| `P/calendar-editor.js` | 1–228 行 | 日程 5 类命令、版本令牌、原请求恢复 |
| `P/state-editor.js` | 1–492 行 | 状态配置、周期校准、公开文案；含未接入的联系配置分支 |
| `P/state-maintenance.js` | 1–740 行 | 配置字段定义、校验、表单、周期校准命令构造；不直接发送 HTTP |
| `P/life/app.js` | 1–451 行 | 阅读、笔记、浏览、睡眠和生活设置；含保留的日记、兴趣、宿主管理代码 |
| `P/life/embedded-bridge.js` | 1–99 行 | `postMessage` 传输、请求超时、上下文和尺寸通知 |
| `P/life-bundle.js` | 全文件，2 行、73,472 字节 | `window.GoodLifeWorkspace` 字符串；完整解码后核对，等于 `life/index.html` 内联 `life/style.css` 和 `embedded-bridge.js + app.js` 的生成结果，不另计重复接口 |
| `G/main.py` | 1–117 行 | 初始化、注销 API、连接状态生成；本文件没有额外静态页或 API 路由声明 |
| `G/good_life/web_api.py` | 1–75 行 | 6 个路由、认证检查和宿主转发 |
| `H/life_dashboard.py` | 1–80 行 | 目录投影、目标选择、命令白名单、日记过滤 |
| `H/life_api.py` | 1–101 行 | 生活读写参数和请求体解析 |
| `H/state_api.py` | 1–215 行 | 状态/日程读写参数解析；其中 weather 路由不是本页请求 |
| `C/role_work/life/management.py` | 1–850 行 | 生活响应投影、配置和活动命令分流 |
| `C/role_work/state/management.py` | 1–408 行 | 状态响应投影、配置与日程命令分流 |

返回字段还沿实际调用核对了 `C/agenda/calendar.py`、`C/storage/versioned_records.py`、`C/role_work/life/coordinator.py`、`C/role_work/life/body_clock.py`、`C/role_work/state/service.py` 和相关 handoff/queue。这里只描述页面使用的契约，不展开未被页面请求的宿主功能。

计数口径：

- 已注册、页面使用的业务接口：**6 个方法 + 路径组合**，其中 GET 4 个、POST 2 个。
- JS 保留、被父页面拒绝的宿主管理请求：**5 个方法 + 路径组合**；连同前者共 **11 个请求组合**。iframe 的 `life` / `life/command` 是前述目标路由的别名，不重复计数。
- `life/command` 白名单：**9 种 action**；JS 另保留 **12 种**日记/兴趣 action，均被生活页后端白名单拒绝。
- 状态/日程 JS 的命令构造分支：**10 种 action**，其中 9 种在后端白名单中、`contact.configure` 被拒绝；部分分支在当前页面没有入口，见第 7 节。后端另外接受的 `configure_window`、`state.write`、`maintain`、`calendar.accept` 没有本范围 JS 调用，不计页面接口。

## 2. 入口、传输与认证

`G/main.py:40` 的 `initialize()` 调用 `LifePageAPI.register()`，`main.py:78` 的 `terminate()` 注销。6 个路由均在 `G/good_life/web_api.py:22` 注册。`main.py` 没有调用静态页面注册函数；静态页目录如何映射由 AstrBot 宿主负责，仓库内不能确认其服务 URL 和缓存策略。

页面 `P/index.html:11` 引用 `/api/plugin/page/bridge-sdk.js`；外层 `P/app.js:160` 等待 `AstrBotPluginPage.ready()`，使用 `apiGet(path, query)` / `apiPost(path, body)`。所有目标路径都由目录中选择的 target id 和 role token 编码构成，不能把角色显示名作为 token。

共同认证检查位于 `G/good_life/web_api.py:40`、`H/life_dashboard.py:24`：`request.plugin_name` 必须为 `astrbot_plugin_good_life`，`username` 必须为非空字符串且不能以 `api_key:` 开头。目标接口还要求 runtime ready、存在 `life_dashboard` 处理器、注册身份仍属于当前插件、目标为 private 且角色同时提供 life/state 能力。管理器继续校验 Dashboard 操作者权限、当前角色/身份绑定和来源权限；目录可见性不等于写入授权。

调用链：`LifePageAPI._dispatch()`（`G/good_life/web_api.py:51`）→ `H/integration_registry.py:274 life_dashboard()` → `H/life_dashboard.py:22 dispatch()` → `TargetControllerView` → `LifeWebAPI` 或 `StateWebAPI`。该过程是进程内调用，页面没有直接请求 `/astrbot_plugin_private_companion/life` 或 `/state`。

共同响应封装（`H/web_api.py:70 outcome_response()`）：

- 成功：`{status: "ok", message: "", data: <业务结果>}`。
- 失败：`{status: "error", message: <机器错误码>, reason: <原因>, data: {code: <机器错误码>}}`。
- 生活插件本地 `_failure()`（`G/good_life/web_api.py:9`）采用相同失败结构。
- 外层 `P/app.js:14` 和内层 `P/life/app.js:41` 同时兼容业务对象、封装、嵌套封装；失败码进入本地化错误处理。不能把封装内的 `data` 与实际业务结果字段混为一层。
- 没有在这些处理函数中显式设置 HTTP 状态码，不能只凭这里的成功/失败封装推断实际 HTTP 状态码。

## 3. HTTP/SDK 接口总表

| 编号 | 方法、SDK 路径 → 注册路径 | 参数 | 后端处理位置 | 主要成功字段、页面使用 |
|---|---|---|---|---|
| A01 | GET `status` → `B/status` | 页面传 `{}`；本地 handler 未校验 query | `G/good_life/web_api.py:46 status()` → `G/main.py:114 companion_v2_status()` | `schema`, `plugin_id`, `enabled`, `runtime_ready`, `dependency_state`；外层 `P/app.js:106` 只用 `runtime_ready` 显示连接文案和离线样式 |
| A02 | GET `targets` → `B/targets` | 无 query；下游拒绝任何 query，不允许覆盖身份 | `G/good_life/web_api.py:62 targets()` → `H/life_dashboard.py:30` → `H/page_api.py:260 targets()` | `installation_id`, `operator_id`, `targets[]:{id,name,roles[]:{token,name}}`；`P/app.js:109` 填充窗口/角色选择器，两个身份 id 参与编辑器恢复缓存隔离 |
| A03 | GET `T/life` → `B/targets/<target>/<role>/life`；iframe 别名 GET `life` | path：target 非空，role 为 64 字符的 `digest(role_id)`；query：可选 `before`, `after_activity`, `archive_kind`，详见下文 | `G/good_life/web_api.py:65 life()` → `H/life_dashboard.py:70` → `H/life_api.py:38 status()` → `C/role_work/life/management.py:208 status()` | 完整结构见第 4 节；外层 `P/app.js:87` 取活动和睡眠总览，内层 `P/life/app.js:315` 取书架、笔记、浏览、睡眠和设置 |
| A04 | POST `T/life/command` → `B/targets/<target>/<role>/life/command`；iframe 别名 POST `life/command` | JSON：`action`, `operation_key`，以及 action 对应令牌/参数；不接受 query；9 种 action 见第 5 节 | `G/good_life/web_api.py:68 life_command()` → `H/life_dashboard.py:57` → `H/life_api.py:58 command()` → `C/role_work/life/management.py:525 command()` | 随 action 返回 `operation_id`, `replayed?`, `state?`, `activity_id?`, `activity_ids?`, `total_segments?`, `title?`, `removed?`；内层 `send()`/`importBooks()` 保存后重读 |
| A05 | GET `T/state` → `B/targets/<target>/<role>/state` | path 同 A03；query 可选 `after_id`（默认空）、`limit`（默认 30，整数 1–100）；重复或额外字段被拒绝；页面仅发送 `{}` 或 `{after_id}` | `G/good_life/web_api.py:71 state()` → `H/life_dashboard.py:70` → `H/state_api.py:81 status()` / `:60 _run()` → `C/role_work/state/management.py:43 status()` / `:48 _status()` | 完整结构见第 6 节；`P/app.js:75 loadState()` 为日历和状态编辑器设置 scope、状态和令牌，并更新总览 |
| A06 | POST `T/state/command` → `B/targets/<target>/<role>/state/command` | JSON；不接受 query；请求体上限 100,000 字节，拒绝重复 JSON key 和 NaN/Infinity；页面命令见第 7 节 | `G/good_life/web_api.py:74 state_command()` → `H/life_dashboard.py:57` → `H/state_api.py:160 command()` → `C/role_work/state/management.py:354 command()` | 通常为 `operation_id`, `replayed`；日程/周期回执另有 `id`, `revision`, `state`；编辑器要求 `operation_id` 是字符串，然后刷新 A05 |

A02 在宿主目录结果中只保留 private 目标、有 life/state 两个能力的角色。`role.token` 来自 `digest(role["role_id"])`，不是认证凭据；仍须通过 Dashboard 权限检查。写请求会先按目录重新定位角色，再用 `role_source_target_id()` 选择其源目标视图（`H/life_dashboard.py:49`），不能根据返回显示名判断底层会话归属。

A03 query 规则（`H/life_api.py:41`、`C/role_work/life/management.py:208`）：

| 字段 | 类型、含义与约束 | 页面调用 |
|---|---|---|
| `before` | 日期字符串，由 `date.fromisoformat()` 校验；按本地日期排他读取更早记录；空值转 None | `P/life/app.js:315` 使用日期页历史；`:444` 从 `next_date` 进入旧页，`:445` 回较新页 |
| `after_activity` | 最多 512 字符的活动游标；空值转 None | `P/life/app.js:315`、`:446` 用 `next_activity` 读下一批，`:447` 清空回首批 |
| `archive_kind` | None / `diary` / `reading_note` / `social_note` | `P/life/app.js:13` 初始为 diary；notes 上下文切 reading_note（`P/life/embedded-bridge.js:35`）；切页、选角色会重新读 |

query 不允许未知字段，同名字段必须恰好出现一次。每次生活状态读取的档案按最多 50 个日期分页，活动按原始记录游标扫描后收集至少一批匹配项，并非保证恰好 30 个可见项目（`C/role_work/life/management.py:253`、`:265`）。

## 4. A03 生活状态返回与页面消费

`H/life_api.py:49` 在管理器返回上附加 `profile_id`，存在时附加 `target_id`。随后 `H/life_dashboard.py:72` 把 `journals` 过滤为 reading_note / social_note；其他顶层字段没有在此处裁掉。字段按当前静态源码列出，非所有分支均必有值。

| 业务结果字段 | 来源位置 | 当前页面使用 |
|---|---|---|
| `role_id`, `participant`, `profile_id`, `target_id` | `C/role_work/life/management.py:308`；`H/life_api.py:49` | 内层角色名优先取父上下文 `display.role_name`，再取 `role_name`（后端未声明该字段），最后取 `role_id`；私聊显示优先用 `display.subject_name`。`participant/profile_id/target_id` 不直接渲染 |
| `host_state`, `failure_code`, `config`, `edit_token`, `view_token` | `management.py:311` | `P/life/app.js:50` 控件启用、`:300 render()`、`:373` 配置表单、`:72` 命令令牌；`failure_code` 触发提示 |
| `next_diary_at`, `next_sleep_at` | `management.py:313` | 内层 `render():305` 写隐藏的例行任务提示；日记管理不在生活页入口范围 |
| `sleep_schedule`, `body_sleep` | `management.py:303`、`:315` → `C/role_work/life/coordinator.py:131` / `body_clock.py:108` | 作息页 `renderSleepPlan():277` 展示共享、执行窗口、下次入睡/醒来、压力/睡眠债/负荷；外层 `renderOverview():39` 也尝试显示，但字段存在不匹配，见第 10 节 |
| `reading_plan:{state,title?,reason?,resume_at?}` | `management.py:178 _reading_plan()` | `P/life/app.js:146` 展示 disabled / empty / waiting / choosing / paced / incomplete / declined / selected；`resume_at` 未显示 |
| `activities[]` | `management.py:75 _project_activity()`、`:317` | 外层取 active/paused 活动；内层按 kind=reading/sleep/social 分组展示并决定可操作按钮 |
| `next_activity` | `management.py:318` | 活动下一批游标 |
| `journals[]`, `next_date` | `management.py:149`、`:237`、`:318`，再经 dashboard 过滤 | 笔记目录、摘录、感想、日期分页。日记/梦境正文被 dashboard 过滤掉 |
| `running[]`, `receipts[]`, `journal_unresolved` | `management.py:273`、`:320` | 内层即使面板隐藏仍执行回执渲染；`running.length`、存在 active 活动或启用自动阅读时 3 秒轮询；`journal_unresolved` 控制保留的失败书写重试控件 |
| `interests[]`, `disliked_items[]`, `recent_exposures[]`, `profile_hobbies` | `management.py:304`、`:460`、`:490` | 内层总会调用 `renderInterests():186`，但当前父上下文和隐藏 tabs 不提供兴趣页入口；这些字段仍经 API 返回，不能把隐藏面板当作后端裁剪 |

`activities[]` 的主要字段（`management.py:103`）：`activity_id`, `revision`, `kind`, `current_context`, `state`, `title`, `checkpoint`, `started_at`, `updated_at`, `due_at`, `start_offset`, `end_offset`, `sleep`, `reflection`, `unavailable_reason`, `task`, `view_token`。

- `task` 可为 null，否则有 `task_id`, `state`, `reason`。页面只展示未 succeeded 的任务状态；unknown 禁止重新阅读。
- `sleep` 可为 null，否则含 `kind`, `target_minutes`, `slept_seconds`, `interruptions`, `early_interruptions`, `quality_range`, `wake_until`, `oversleep_minutes`。当前活动卡片主要使用标题、状态、起止时间。
- social 的 `checkpoint` 是 JSON 字符串。页面尝试读取 `subjective_reflection`, `actual_actions[]`，并显示 action 的 `platform`, `operation`, `coverage`, `visual_coverage`, `query`, `id`（`P/life/app.js:165`）。它不是独立 HTTP 接口；不能从显示代码保证所有历史 checkpoint 都有这些字段，解析失败会显示原 checkpoint。
- 原来源不可用时 `unavailable_reason` 有错误码，阅读感想隐藏；读按钮还要求 planned/paused、来源可用、task 非 unknown（`P/life/app.js:175`）。

`journals[]` 在此页面实际能收到的结构：

| kind | 返回主要字段 | 页面使用 |
|---|---|---|
| reading_note | `kind`, `activity_id`, `local_date`, `completed_at`, `state`, `book_title`, `start_offset`, `end_offset`, `body`, `excerpt`, `unavailable_reason`（`management.py:149`） | `renderArchive():107` 显示书名、原文范围、最多 300 字摘录、读后感；来源失效时隐藏内容；不提供编辑/删除笔记操作 |
| social_note | `kind`, `activity_id`, `local_date`, `completed_at`, `state`, `body`（`management.py:237`） | 通用渲染支持上网感想，但当前浏览入口切 social 活动页，notes 入口隐藏 social_note 筛选；没有正常导航直达该档案分类 |

`receipts[]` 的主要字段为 `key`, `state`, `code?`, `operation_id?`, `retryable?`（`management.py:273`）。这里只列返回契约，不把保留的书写管理控件计为有效生活功能。

`sleep_schedule`（`coordinator.py:147`）包含：`shared`, `owner:{installation_id,bot_id,role_id}`, `enabled`, `time`, `timezone`, `duration_minutes`, `variation_minutes`, `window_minutes`, `recommended_duration_minutes`, `physiology`, `executor_is_current`, `executor_state`, `executor_window`, `next_sleep_at`, `next_wake_at`, `sleep_window_closes_at`, `rhythm`, `sleep_state`, `active_wake_at`。作息页使用 `physiology.sleep_pressure`, `has_sleep_history`, `sleep_debt_minutes`, `workload_score`；未枚举 physiology 的其他内部字段。

`body_sleep` 可为 null，否则 `body_clock.py:132` 返回：`basis`, `state`（sleeping/interrupted/awake）, `kind`, `started_at`, `planned_wake_at`, `ended_at`, `slept_minutes`, `target_minutes`, `interruptions`, `early_interruptions`, `wake_until`, `oversleep_minutes`, `sleep_quality_range`，以及 profile 的扩展字段。这里没有 `resting`。

## 5. A04 生活命令与配置参数

共同构造位置：`P/life/app.js:70 send()`；EPUB 批量导入另见 `:408 importBooks()`。`operation_key` 为稳定、非空、最多 128 字符的字符串；同次不确定请求复用原 key，不应生成另一条请求代表原操作。`view_token` 来自对应状态或活动。普通活动命令附带原活动 `expected_revision`；新增内容和 sleep 默认使用 life 根令牌、revision 0（页面省略该字段，管理器补默认）。

解析层 `H/life_api.py:74` 把共同字段限定为 `action`, `operation_key`, `view_token`, `expected_revision`，再按 action 扩展；拒绝未知 JSON 字段、重复 JSON key、query。路由体积限制为 `MAX_EPUB_BASE64 + 16384`，非 EPUB 还限制 1,200,000 字节；dashboard 分发另有 28 MiB 前置限制（`H/life_dashboard.py:58`）。EPUB 是 JSON 内 Base64，不是 multipart。

| action | 页面发送的 action 专用参数 | 后端实际处理位置 | 成功业务字段与页面使用 |
|---|---|---|---|
| configure | `changes` 对象、`edit_token`；`view_token` 随通用 send 带上，但配置分支用 edit_token 校验 | `C/role_work/life/management.py:360 configure()`；分发 `:531` | `operation_id`, `replayed`；成功清除 dirty 并刷新 |
| add_text | `title`（非空、≤160 字符）, `text`（非空、≤262144 字符） | `management.py:554` → `C/integration/reading_import_handoff.py:178 execute()`；legacy 分支 `management.py:433 _add_text()` / `:734` | `operation_id`, `activity_ids[]`, `total_segments`；handoff 重放可另有 `state:"completed"`, `replayed:true`。新增为待读，不等于已读 |
| add_epub | `filename`, `epub_base64`；页面本地只选 .epub、文件≤20 MiB，解析后的正文≤1000万字符 | `management.py:548` → `C/role_work/life/epub_import.py:parse_epub_upload()` → 同上 ReadingImportHandoff；legacy `management.py:736` | 同 add_text，另有 `title`；`P/life/app.js:427` 每文件一请求、顺序批量导入并重新读取。通用 `send():89` 用 `activity_ids.length` 展示片段数，实际总量字段是 `total_segments` |
| sleep | `minutes`，表单整数范围 1–1440 | `management.py:605` → `C/integration/manual_sleep_handoff.py:ManualSleepHandoff`；legacy `:739` | `operation_id`, `activity_id`；handoff 已确认重放可有 `state`, `replayed`。成功重新读取睡眠记录 |
| read | `activity_id`, `expected_revision`, 活动 `view_token` | `management.py:602` → `C/integration/manual_reading_queue.py:18 enqueue()`；legacy `:743` | `operation_id`, `activity_id`, `state`（scheduled/completed，或已保存结果中的 state/code）, `replayed`；只是排队/回执，不表示已完成阅读。页面刷新活动 |
| wake | `activity_id`, `expected_revision`, 活动 `view_token` | `management.py:631` → `C/integration/manual_wake_handoff.py:ManualWakeHandoff`；legacy `:746` | `operation_id`, `activity_id`，重放可能加 `state`, `replayed`；请求前有醒来确认框 |
| cancel | `activity_id`, `expected_revision`, 活动 `view_token` | `management.py:626` 按 reading/sleep 分别使用 `ManualCancelReadingHandoff` / `ManualCancelSleepHandoff`；其他/legacy `:749` | handoff 通常 `operation_id`, `activity_id`, `state?`, `replayed?`；legacy 新请求可能仅 `operation_id`，重放 `operation_id/state/replayed`。页面 active/planned/paused 显示取消按钮 |
| withdraw | `activity_id`, `expected_revision`, 活动 `view_token` | `management.py:558` → `C/integration/activity_deletion_handoff.py:ActivityDeletionHandoff`；legacy `:749` | `operation_id`, `activity_id`（handoff），可能 `state`, `replayed`；legacy 可能仅 operation_id。撤回整份原文并停止关联未完成片段，页面显示确认说明 |
| remove | `activity_id`, `expected_revision`, 活动 `view_token` | 同 ActivityDeletionHandoff；legacy `management.py:791` | `operation_id`, `removed`，重放可能 `state/replayed`；页面仅 completed/cancelled 阅读记录显示删除按钮，刷新后移除 |

以上回执不是固定统一 schema；特别是 legacy 重放可能只返回操作状态而不重放新请求的全部专用字段。`P/life/app.js:89` 对 state=failed/unknown 特别提示，其他状态多统一显示“操作已完成”，应结合重新读取判断实际进度。

生活配置父页白名单位于 `P/app.js:5`，iframe configure 会先过滤 changes；宿主 `H/life_dashboard.py:66` 还会校验 `C/configuration/life_provider.py:7 LIFE_FIELDS`。当前两者为同一组 18 个字段：

| 字段 | 类型、后端/表单范围 | 当前表单行为 |
|---|---|---|
| enabled, sleep_enabled, reading_enabled, social_recommendation_enabled | boolean | 设置页保存；enabled 影响执行授权同步 |
| timezone, sleep_timezone | IANA 时区字符串 | 设置页保存 |
| sleep_time | `HH:MM` 本地时间字符串，无秒、无时区后缀 | 设置页保存 |
| sleep_minutes | integer 1–1440 | 设置页保存 |
| sleep_variation_minutes | integer 0–120 | 设置页保存 |
| sleep_window_minutes | integer 0–360 | 设置页保存 |
| reading_chunk_chars | integer 200–6000 | 设置页保存，下一段生效 |
| reading_daily_segments | integer 1–6 | 白名单存在，但当前 `configFields` 和 HTML 没有该输入，不由正常表单提交 |
| reading_speed_chars_per_minute | integer 1–100000 | 设置页保存 |
| social_interval_minutes | integer 0–10080 | 设置页保存，0 表示不设间隔 |
| social_existing_percent, social_related_percent, social_new_percent | integer 0–100，三项合计 100 | 页面和后端均校验合计 |
| social_random_read_percent | integer 0–100 | 设置页保存 |

范围依据 `C/role_work/life/configuration.py:42 configuration_fields()` / `:65 values()`、`P/life/app.js:15` / `:366` 和 `P/life/index.html:38`。内部配置存储前缀是 `role.life.`，页面 changes 使用无前缀字段。

`diary_enabled`, `diary_time`, `derive_enabled` 虽仍在 iframe `configFields` 中被收集，作息页 CSS 隐藏它们，父页过滤后不会发送给生活路由。不能把“收集了隐藏字段”误记为生活插件拥有这些配置写接口。

## 6. A05 状态/日程返回与页面消费

返回位置：`C/role_work/state/management.py:99 _status()`。读状态还会尝试补齐展示/维护授权（`:51`），不能假设该 GET 是绝对无本地副作用。读取通过生活账本一致快照和日程边界准备，权限或来源不可用时可能返回局部不可用投影。

| 字段 | 主要结构 | 页面用途 |
|---|---|---|
| settings | role.state 全部已声明字段的有效配置，额外 `mood_cross_window:true` | `stateEditor` 初始化和对比 changes；组及范围见第 8 节。返回不局限于当前页面显示字段 |
| edit_token, role_edit_token, cycle_settings_token | 窗口配置、角色配置、周期配置快照令牌 | 配置保存及周期校准；不能互换 |
| effective | 各权限项 `{allowed,reason}` | 当前外层不单独展示；共用编辑器及后续消费者可读取 |
| body_scope | `{installation_id,bot_id,role_id,settings[]}`；settings 列出 hunger/health 和 cycle 字段 | 当前页面不直接显示 |
| state | `state`, `revision`, `view_token`, `values?`, `cycle_timeline?` 等状态投影 | 总览读取 `values.energy/hunger/health/sleep/mood/busy/location/cycle`；mood 对象使用 description；周期校准读取 revision/view_token/timeline |
| calendar | `{items[],next_cursor,skipped,skipped_reasons}` | 日程卡片、编辑器、来源不可读提示、下一批游标 |
| create_token | 新日程 revision 0 对应的 view token | 创建日程、判断日程管理入口可用 |
| occurrences | `{items[],partial,skipped,skipped_reasons,scan_truncated,occurrences_truncated}`（管理器启用 diagnostics） | 总览优先取 items，显示最多 8 项；只覆盖配置时区的当天窗口，不是任意未来多日安排 |
| planning | `app.calendar_planning.status(context)` 的投影 | 共用组件支持读取 enabled/running/next_check/last_result，但本页未开放规划配置专题，不能据此认定页面有自动规划设置入口 |
| sleep_schedule, body_sleep | 与 A03 的睡眠投影相同 | 当前外层用 A03 的 life 对象显示睡眠；A05 冗余字段不单独渲染 |
| maintenance | null 或 `{next_at,enabled,result}` | 当前状态编辑器主要呈现配置，不单独显示完整维护回执 |
| presentation, presentation_diagnostics, host_state | 展示执行结果、诊断和宿主状态 | 当前页面没有 QQ 展示专题；不展开宿主诊断内部 schema |

`state` 的 values 字段名来自 `C/role_work/state/service.py:22`：`energy`, `hunger`, `health`, `sleep`, `busy`, `mood`, `location`, `cycle`, `presence`, `online_mode`, `conditions`。未知状态为 `{state:"unknown",revision:0,values:...}`；读取拒绝时管理器返回 `{state:"unavailable",revision,code,view_token}`，不保证有 values（`management.py:80`）。正常记录投影还可能有 `id`, `updated_at`, `reason`, `changed_by`, `mood_dynamics`, `cycle_feeling`, `cycle_timeline`, `sleep_execution`；权限和共享情况决定是否保留，页面不能要求全部出现（`service.py:315`、`:377`）。

`cycle_timeline` 页面实际引用 `phase`, `cycle_day`, `cycle_days`, `applicability`, `pending_plan.effective_date`（`P/state-maintenance.js:498`、`:605`、`:648`）；本清单没有把整个周期内部记录当作稳定页面 schema。

`calendar.items[]` 来自 `C/agenda/calendar.py:280 inventory()` 和状态管理器附加 token，主要包含：`id`, `revision`, `updated_at`, `kind`, `state`, `title`, `detail`, `schedule`, `duration_minutes`, `presence`, `exceptions`, `sources`, `accepted_by`, `changed_by`, `reminder_id`, `reminder_context_key`；可能含 `planning`, `details`。页面使用 `schedule.anchor/timezone_name/frequency/interval/count/fold/gap_policy`、exceptions 数量和 reminder_id。读清单可含多种 kind，编辑控件仅允许非 cancelled 的 role_plan。

`occurrences.items[]` 来自 `C/agenda/calendar.py:327`：`id`, `revision`, `sequence`, `kind`, `state`, `title`, `detail`, `start`, `end`, `presence`, `sources`。响应最多保留 64 个 occurrence，并有 `partial` 标识（`:343`）；外层总览没有呈现 partial/skipped 警告。

## 7. A06 状态/日程命令

所有下表 action 都发送到同一个 `T/state/command`，不是另外的 REST 路由。`operation_key` 非空、最多 128 字符，由编辑器生成并按 scope 缓存。成功须有字符串 operation_id；结果未确认时保留原请求，可用原 key 重新核对。scope 缓存键含 installation_id、operator_id 和目标 path（`P/state-editor.js:102`、`P/calendar-editor.js:101`）。

| action | 精确请求专用字段（另含 action、operation_key） | 处理位置与成功字段 | 页面可达性、使用 |
|---|---|---|---|
| configure | `edit_token`, `changes`；混合窗口/角色变化时另有 `role_edit_token` | `H/state_api.py:182` → `C/role_work/state/management.py:118 _configure()`；`{operation_id,replayed}` | `P/state-editor.js:237` 构造。当前已开放状态专题的字段都属角色，因此正常新建表单走 configure_role；窗口/混合分支属于共用组件保留能力 |
| configure_role | `role_edit_token`, `changes` | `H/state_api.py:185` → 同 _configure；`{operation_id,replayed}` | `P/state-editor.js:239`；维护、功能与展示、精力、健康、饥饿、周期、心情、巡检表单保存 |
| cycle.align | `expected_revision`, 当前 state 的 `view_token`, `settings_token`, `data:{applicability,day}`；applicability=applicable/not_applicable/unknown/auto，applicable 时 day 为当前周期长度内整数，否则 null | `H/state_api.py:191` → `StateManagement.command():358` / `_life_state_write():286` / legacy `_local_command():387` → `C/role_work/state/service.py:269 align_cycle()`；回执 `{operation_id,id,revision,state,replayed}` | `P/state-maintenance.js:506 alignment()`；周期校准，要求已启用周期、有可用状态及 cycle_settings_token |
| calendar.create | `id:"plan-"+UUID`, `expected_revision:0`, `view_token:create_token`, `data:EventData` | `H/state_api.py:195` → `StateManagement._life_calendar_command():256` / legacy `_local_command():397` → `C/agenda/calendar.py:187 command()`；回执 `{operation_id,id,revision,state,replayed}` | `P/calendar-editor.js:140`；创建角色计划 |
| calendar.revise | `id`, 原 `expected_revision`, 原 item `view_token`, `data:EventData`, `sources`（原数组或 []，最多 8 项） | 同上；回执同上 | `P/calendar-editor.js:141`；修改整组，重置例外并撤销原提醒 |
| calendar.cancel | `id`, 原 `expected_revision`, 原 item `view_token` | `H/state_api.py:199` → 同日程命令处理；回执同上，state 为 cancelled | `P/calendar-editor.js:152`；取消整组 |
| calendar.exception | `id`, 原 `expected_revision`, 原 item `view_token`, `data:{sequence,start}`；sequence 为从 0 开始的整数，start 为 ISO 时间字符串或 null（跳过） | `H/state_api.py:201` → 同日程命令处理；回执同上 | `P/calendar-editor.js:143`；UI 从第 1 次计数，提交时减 1，仅重复日程显示入口；改期/跳过撤销原提醒 |
| calendar.remind | `id`, 原 `expected_revision`, 原 item `view_token`, `sequence`（从 0 开始整数） | `H/state_api.py:203` → `StateManagement._life_calendar_command():268` → `C/agenda/calendar.py:375 remind()`；回执同上 | `P/calendar-editor.js:148`；安排/替换指定 occurrence 提醒，需授权、已接受且未来有效 |
| calendar.presence | `id`, 原 `expected_revision`, 原 item `view_token`, `data:{presence}`；字符串≤8 个字符，可空 | `H/state_api.py:197` → 同日程 command 的 presence 分支（`C/agenda/calendar.py:242`）；回执同上 | `P/state-editor.js:245` 构造，但当前日程按钮未转发给 stateEditor.click，正常点击不可达，见第 10 节 |
| contact.configure | `edit_token`, `changes`，字段名由可选 `CompanionStateContact` 组件提供 | `H/life_dashboard.py:18/63` 不含该 action，返回 life_dashboard_invalid；不继续推断联系后端 | `P/state-editor.js:218` 残留分支；本页未加载该组件、未提供联系入口，changes 字段不能从本范围静态确定 |

日程/EventData 的精确形状（`P/calendar-editor.js:22` / `:122`；后端 `C/agenda/calendar.py:30 event_data()`）：

```text
{
  kind: "role_plan",
  title: string,              // 非空，最多 256 字符
  detail: string,             // 最多 4096 字符
  presence: string,           // 最多 8 字符，可空
  duration_minutes: integer, // 1–1440
  schedule: {
    anchor: ISO datetime,
    timezone_name: IANA timezone,
    frequency: "once" | "daily" | "weekly" | "monthly" | "yearly",
    interval: positive integer,
    count: positive integer | null,
    fold: 0 | 1,
    gap_policy: "reject" | "shift_forward"
  }
}
```

页面把 datetime-local 按所选 IANA 时区转换为实际 ISO 时刻，显式处理夏令时重叠 fold 和缺口 gap_policy；修改时未变的时间保留原 anchor（`P/calendar-editor.js:57`、`:132`）。`sources` 由 `H/state_api.py:209` 转换成 SourceRef，不接受超过 8 项。当前创建表单不传 sources，修改表单回传原 sources。

回执依据 `C/storage/versioned_records.py:99`；生活日程转发 `C/integration/calendar_command_bridge.py:111` 返回提供方 proposal.receipt 并重算 replayed。配置成功不返回新的 edit token，日程命令也不返回完整新列表，必须重读 A05。accepted 表示接受计划，不能当作活动完成。

## 8. 状态配置 changes 字段字典

定义及分组：`P/state-maintenance.js:22 definitions`、`:100 groups`；前端校验 `:347 validField()`、`:363 normalized()`；保存分流 `P/state-editor.js:222`。内部配置前缀是 `role.state.`，请求 changes 使用无前缀字段。后端 `C/role_work/state/management.py:118` 要求非空 changes、字段在 DEFAULTS 中、类型与声明严格一致；`settings_scope.py:9` 仅把 typing_enabled/typing_refresh_seconds 作为窗口字段，其余都是角色偏好。

| 当前专题/组 | 字段 | 前端类型与范围 |
|---|---|---|
| 维护 maintenance | hunger_enabled, health_enabled | boolean；当前维护总面板另提供 cycle/align/mood/patrol 入口 |
| 功能与展示 home | enabled, maintenance_grant_scope, maintenance_minutes | boolean；scope=role/window；整数 1–1440 分钟 |
| home 八项功能 | `{energy,health,hunger,sleep,cycle,mood,busy,location}_enabled`；`home_show_{同八项}` | boolean；开关决定状态维护/首页展示，不表示按时入睡 |
| home 特殊状态展示 | `home_only_special_{health,hunger,sleep,cycle,busy}` | boolean；首页特殊状态显示条件 |
| 精力 intensity | humanized_state_intensity, energy_expression_prompt | 整数 0–100；字符串≤512 字符 |
| health | health_enabled, health_expression_prompt | boolean；字符串≤512 字符 |
| hunger | hunger_enabled, hunger_expression_prompt | boolean；字符串≤512 字符 |
| cycle 基础 | cycle_enabled, cycle_mode, cycle_timezone, cycle_initialization, cycle_variation_enabled, cycle_discomfort_enabled | boolean；mode=basic/six_phase；有效 IANA 时区；initialization=natural/unknown/first_day；后两项 boolean |
| cycle 阶段时长 | `cycle_{menstrual,follicular,pre_ovulation,ovulation,luteal,pms}_days` | 各整数 1–30；校准 day 上限取当前六项之和，周期整体验证仍由后端负责 |
| cycle 体感/表达 | cycle_expression_prompt, cycle_link_intensity, cycle_discomfort_chance, cycle_discomfort_types | 字符串≤2048；boolean；整数 0–100；逗号分隔且不重复，值为痛经/头痛/腰酸/乏力/情绪低落/恶心 |
| cycle 阶段表达 | 每个上述阶段的 `cycle_{phase}_energy`, `cycle_{phase}_prompt`, `cycle_{phase}_mood` | 整数 −50–30；字符串≤512；字符串≤80 |
| mood | mood_enabled, mood_update_probability, mood_update_max_delta, mood_decay_half_life_minutes | boolean；有限数字 0–1；整数 1–4；整数 5–1440 分钟 |
| patrol | daily_state_patrol_enabled, daily_state_patrol_time, daily_state_patrol_retention_days | boolean；严格 HH:MM；整数 3–180 天 |

以下组在同一 JS 中完整定义，但 `P/index.html:53` 的当前导航没有相应 feature，不能记成已开放页面：

| 共用组件组 | 字段与范围 | 接口状态 |
|---|---|---|
| settings / presentation | typing_enabled:boolean, typing_refresh_seconds:整数1–10, presence_enabled:boolean, online_status_enabled:boolean | configure/configure_role 构造分支存在；无当前专题入口 |
| datetime | planning_timezone：有效 IANA 时区 | configure_role 支持；无当前专题入口 |
| weather | planning_weather_enabled:boolean, planning_weather_location:string（≤512 UTF-8字节的 JSON）, planning_weather_max_age_minutes:整数5–120 | configure_role 支持；无当前专题入口；没有调用 `/state/weather` |
| planning | calendar_enabled:boolean, schedule_simulation_mode=continuous/orchestrated, planning_enabled:boolean, planning_timezone:IANA, planning_horizon_days:整数1–366, planning_refine_minutes:整数1–1440, planning_max_items:整数1–32, planning_prompt:string≤4096, planning_annual_calendars:string≤16384 UTF-8字节 | configure_role 支持；日程页目前展示记录编辑器，不展示本规划专题 |

`planning_weather_location` 的 JSON 字段为 `name`（必填≤64字）、可选成对数字 `latitude`（−90–90）/`longitude`（−180–180）、可选成对字符串 `alert_city`/`alert_area`（各≤64字）；不允许控制字符和额外字段，空字符串表示未配置（`P/state-maintenance.js:125`）。启用天气感知时要求地点非空。这里配置地理信息不是发起天气读取接口。

`planning_annual_calendars` 是 JSON 数组字符串，每项 `{schema:"companion.annual-calendar.v1",year,holidays:[{name,start,end}],makeup_workdays:[{date,name}],solar_terms:[{date,name}]}`。最多 3 个年份、每年最多标记 96 个不同日期、名称≤64字，日期需完整 YYYY-MM-DD 且属于该年份；拒绝冲突节假日、节假日与调休重叠、重复调休日和重复节气（`P/state-maintenance.js:177`、`:280`）。这也是 changes 中的一个字符串字段，不是额外导入路由。

以上范围是页面声明和已核对的后端字段约束；后端配置 owner 还会做整体周期、规划、心情和提示词校验（`C/role_work/state/configuration.py:223`），前端通过不保证后端接受。

## 9. iframe 协议、残留请求及未开放动作

### 9.1 当前使用的消息协议

这不是另外一组 HTTP API。`P/life/embedded-bridge.js:69 request()` 把 SDK 调用封装为 `{channel:"companion-workspace",kind:"request",requestId,method,path,body,timeoutMs}`。`P/app.js:142` 仅接受来自当前 iframe.contentWindow 的该 channel 消息：

- path 只允许 `life` / `life/command`，method 只允许 GET / POST；当前页面实际使用 GET life、POST life/command。父层路径和方法分别校验，没有强制这两个标准配对；不标准配对最终不会命中所注册的方法。
- 父层拼接当前选择的 `T`，用外层 bridge 请求对应 API；不是使用 iframe 自带 target 参数。
- configure 的 changes 经父页白名单过滤。
- 成功返回 `{channel,kind:"response",requestId,ok:true,data}`；失败有 `ok:false,error,code,reason,confirmed`。
- ready/context、host-mappings-refreshed、resize 用于启动、切 section/tab、刷新和高度；子页支持 theme/appearance/confirm 等协议，但当前父页 `confirm:false` 且未实现确认代理。
- 内层默认超时 18 秒，请求超时加 1 秒触发 bridge_timeout；可请求 1–300 秒，EPUB 请求传 120 秒（`P/life/app.js:66`）。父层收到 timeoutMs 后没有传给外层 SDK（`P/app.js:154`），所以 120 秒不能证明底层网络超时也同步增加。

### 9.2 JS 保留的宿主管理请求（不计可用接口）

以下调用都在 `P/life/app.js:391 hostControl()`；启动/停止控件被 `embedded-bridge.js:91` 隐藏，`.host-controls` 也被生活 section 样式隐藏。即使代码触发，父层仍以 path 不在允许集为由拒绝，不会到达主插件路由。因此只登记静态调用参数，不展开被拒绝后的宿主后端。

| 编号 | 方法、相对路径 | 请求参数 | JS 期望字段/用途 | 实际处理 |
|---|---|---|---|---|
| R01 | GET staging/status | `{}` | `runtime_open`；若未打开则 probe/initialize | 父页 `P/app.js:150` 拒绝，后端未调用 |
| R02 | POST staging/probe | `{}` | 只检查成功，不读取专用字段 | 同上 |
| R03 | POST staging/initialize | `{}` | 只检查成功，不读取专用字段 | 同上 |
| R04 | POST staging/start | `{}` | 成功后重读 life | 同上 |
| R05 | POST staging/stop | `{}` | 成功后重读 life | 同上 |

### 9.3 JS 保留、生活后端明确拒绝的 action

`P/life/app.js` 仍有以下 12 种 `life/command` 构造，父页路径过滤本身不会拦截，最终由 `H/life_dashboard.py:17/63` 的 LIFE_COMMANDS 拒绝为 `life_dashboard_invalid`。不能把 `H/life_api.py` 或管理器支持当作生活页授权。

| action | JS 发送的额外参数（都有 action/operation_key/view_token） | 调用位置、页面状态 |
|---|---|---|
| diary | 可选 `local_date`, `retry_of` | `P/life/app.js:388`、`:443`；日记入口隐藏；默认 archive_kind=diary 仍可能发 GET，但正文被过滤 |
| dream | 以 sleep item 带 `activity_id`, `expected_revision`，可选 retry_of | `:385` 保留弹窗分支；当前睡眠卡片没有创建 dream 按钮的代码 |
| edit | `kind`, `local_date`, `expected_revision`, `body`, `afterglow` | `:141`、`:383`；只有 diary/dream 条目可走此分支，生活页结果被过滤 |
| restore | 同 edit | `:141`、`:383`；同上 |
| delete | `kind`, `local_date`, `expected_revision` | `:142`；同上 |
| derive | `kind`, `local_date`, `expected_revision` | `:142`；同上 |
| interest_pause | `interest_id`, `expected_revision`，条目 view_token 覆盖根令牌 | `:210`、`:212`；兴趣 tabs 隐藏，无正常导航入口 |
| interest_resume | 同 interest_pause | 同上 |
| interest_edit | 同上另加 name（trim 后非空，输入最多50字） | `:217`；兴趣页隐藏 |
| interest_merge | 同上另加 `target_id`, `target_revision` | `:229`；兴趣页隐藏 |
| interest_clear_dislike / interest_item_clear | 前者 `interest_id`, `expected_revision`；后者 `dislike_id`, `expected_revision`；均带条目 view_token | `:221` / `:240`；兴趣页隐藏；这两类即使绕过 dashboard 白名单，当前 `H/life_api.py:75` fields 表也没有声明 |

本表最后一行合并列出两种 action。日记/梦境相关 6 种、兴趣相关 6 种，合计 12 种，不按表格行数计数。

## 10. 差异、静态无法确认项与验收限制

### 10.1 已由源码确认的差异（D01–D07）

| 编号 | 差异、影响 | 证据 |
|---|---|---|
| D01 | 总览读 `body_sleep.resting`，后端返回 `state` 而无 resting；按这条处理链即使正在睡也会落到“当前没有睡眠活动”的文案 | `P/app.js:39`；`C/role_work/life/body_clock.py:132` |
| D02 | 总览检查 `sleep_schedule.next_at`，实际字段是 next_sleep_at；下次入睡行不能据当前源码正常出现 | `P/app.js:41`；`C/role_work/life/coordinator.py:157` |
| D03 | 日程卡片渲染 `data-state-edit="wording"`，外层日程点击仅调用 calendar.click 和分页；没有 stateEditor.click 转发，正常点击“编辑文案”不能构造 calendar.presence 请求 | `P/calendar-editor.js:218`；`P/app.js:136`；`P/state-editor.js:333` |
| D04 | 初始 iframe archive_kind=diary，切 reading/browsing/settings 只切 tab 不改 archiveKind；notes 才改 reading_note。该保留查询能返回其他生活字段，但 journals 被 dashboard 过滤，不能视为日记入口 | `P/life/app.js:13/315`；`P/life/embedded-bridge.js:35`；`H/life_dashboard.py:72` |
| D05 | 父页没有传递子页 timeoutMs；EPUB 的较长超时只在子页等待层成立，外层 SDK 时限未知 | `P/life/embedded-bridge.js:69`；`P/life/app.js:66`；`P/app.js:154` |
| D06 | A05 响应未加 request_cursor，calendar.markup 却以 status.request_cursor 决定“返回首批”按钮；已有下一批按钮不代表分页回首批 UI 完整 | `C/role_work/state/management.py:99`；`P/calendar-editor.js:221`；`P/app.js:75` |
| D07 | 生活读取仅过滤 journals，仍返回日记回执/日记配置/兴趣等顶层字段；隐藏 UI 与字段归属或访问裁剪不能画等号 | `H/life_dashboard.py:72`；`C/role_work/life/management.py:308` |

### 10.2 未能静态确定或本次未验证（U01–U07）

| 编号 | 不确定项 | 当前能证明的边界/需要的补证 |
|---|---|---|
| U01 | AstrBot SDK 最终 HTTP URL、HTTP 状态码、认证 cookie/Pocket ID 行为、SDK 默认超时、错误封装透传 | SDK 从外部 `/api/plugin/page/bridge-sdk.js` 载入，仓库没有其实现；需真实 Dashboard 浏览器网络记录 |
| U02 | 静态页目录服务路径、缓存/CSP、sandbox srcdoc 内保留的外部 SDK 加载与自定义 bridge 初始化次序 | main.py 只注册业务 API；bundle 内仍含 SDK script。已确认 bundle 与源一致，未在浏览器验证两层 bridge 的执行次序 |
| U03 | 当前实际部署是否采用此 SHA、迁移归属、运行时绑定和角色目录 | 只有本地代码基线；runtime.ready/归属标记动态决定可用性及 legacy/handoff 分支；没有读取生产状态 |
| U04 | 动态业务值及来源失效后的具体字段缺省 | 当前记录、权限和来源状态未知；checkpoint/profile/planning/presentation 等内部扩展不是统一稳定 schema。已列页面读取的字段，不用样本推断完整返回 |
| U05 | 状态配置混合写、回执恢复、跨角色切换时的实际交互和网络竞争 | 本次只做代码盘点；未调用任何写 API，也未运行浏览器交互、未以真实身份验证隔离 |
| U06 | 联系组件 changes 的具体字段和可用后端 | 本页未加载 CompanionStateContact，后端白名单直接拒绝 contact.configure；不能从可选组件引用猜测字段 |
| U07 | 超时后导入是否已成功、批量导入失败后用新 operation_key 重试的结果 | 页面每文件创建 key，失败只保留文本列表；未知请求是否已完成需重读和原回执核对，不能把网络错误认定为未导入或安全重试 |

未将未验证项写成通过，也没有为盘点打开迁移标记、请求生产接口、导入书籍或执行消息/平台操作。

## 11. 本次检查记录

- 完整阅读范围内 7 个 JS、main.py、生活路由、实际到达的 life/state 路由及返回投影；生成 bundle 完整解码并与源 HTML/CSS/bridge/app 的生成结果严格比较，相等。bundle SHA-256：`b5a14d94969b33e7f3a5772951028bdc6b8e9151d77c06b0e7671067b4207fb7`。
- 方法/路径、action、参数与返回字段均以代码位置交叉核对；接口计数不重复包含 bundle 和 iframe 别名。
- 本次仅新增此文档。未修改代码、未运行业务写入、未提交；没有把文档检查当作功能回归或线上验收。
