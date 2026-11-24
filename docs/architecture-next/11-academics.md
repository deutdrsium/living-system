# 学业系统

依赖：[公共契约](03-shared-contracts.md)、[调度](07-gating-planning.md)、[执行产物](14-execution-impressions.md)。

## 职责

academics 拥有校历、课程、课次、大纲、作业、学习进度、出勤和模拟评价。它向日程贡献承诺，向活动提供学习上下文，向压力模块提供负荷信号。它不直接修改情绪，不负责与真实教务系统自动交互。

首版一个学期、有限课程、少量稳定教师/同学。角色是否学生由配置决定，不把某个学校或专业写入内核。

## 最小数据对象

| 对象 | 必需信息 |
|---|---|
| AcademicTerm | term_id、学年、时区、起止、教学周映射、假期/补课例外、来源 |
| Course | course_id、名称、必修/选修、学分、教师引用、考核规则、大纲版本 |
| Enrollment | 角色、课程、学期、选课状态、确认来源 |
| ClassSession | session_id、course/term、时间、教室 place_id、主题引用、revision、取消状态 |
| SyllabusUnit | unit_id、教学周/课次、主题、概念、材料片段、讨论问题、作业关联 |
| Assignment | assignment_id、来源课次、题目、截止、估计剩余工时、提交要求、状态 |
| StudyProgress | 角色、材料/概念/作业、已读范围、草稿块、练习结果、下一步 |
| Participation / Grade | 关联课次或考核、参与区间/结果、规则版本、来源类型与回执 |

课表重复规则展开为具体课次，session_id 不随教室修改而改变；修改增加 revision。真实学校校历与通用法定假日可能不同，以选定校历为准。导入只能提供新版本，不重写已发生课次。

课表和校历的来源（2026-11-16，用户确认）：首版由用户在管理页手动录入，包括学期起止、教学周映射、假期与补课例外、课程（名称、必修或选修、学分、教师）、每周上课时间与单双周、上课地点（引用常住图的 place_id）、以及停课和调课。来源记为 `manual`。不抓取、不登录真实教务系统。CSV 或 ICS 文件导入可以以后再加，同样走"预览—确认—生成新版本"的流程。选课模拟（下文"选课和学年"一节）只在用户没有手动录入某门课时才使用。

## 课程包怎样具体

示例是虚构伦理学课程包：第 5 周主题“功利主义与反对意见”；指定版本《功利主义》第二章及片段；课前形成一个问题；课堂区分论点、理由与反例；课后作业分成选题、提纲、论证块。

材料未绑定时主题可以存在，但不能记录已读内容。真实大纲由资料导入；LLM 解析需预览确认。生成大纲标 authored/generated 模拟课程，不能假称真实教师布置。

## 课次流程

1. 根据课表、当前地点与路径报价反推出发时刻。
2. calendar 生成到课候选；执行 travel、wait、attend 阶段。
3. 实际到达晚于开课则记录迟到；缺席/请假是一份课次结果，不并行创建“已出勤”和“逃课成功”。
4. roll_call 是课次内事件，读取当时参与状态并按规则生成后果。
5. 已完成课堂生成整理笔记/复习意图；作业由 assignment.created 建一次，后续通过稳定 id 继续。

模拟出勤只证明角色按规则参加，不证明学习材料已被读取。课堂产物需要实际可用教材、预设讲义或明确的模拟内容，不凭时钟流逝生成对真实老师的引语。

## 作业与学习

任务以 25–40 分钟等可配置块执行。保存本次材料、实际工作区间、产物、剩余步骤；不能仅以“坐了 30 分钟”减少到零。首版可以用规则估算工作进展，但标记 simulated，与真实生成草稿/读取材料分别记账。

掌握度是带不确定性的估计，参考覆盖、练习和复习。不要直接把阅读页数映射成精确考试成绩。考试/课程评价按固定模拟规则或外部导入产生，保留评分版本；模型点评可作为反馈，不作为未经校验的唯一数值权威。

submitted 表示按当前模式有提交回执：本地模拟提交和真实平台提交必须不同 provenance。真实教学平台操作不由配置自动授权。

## 压力信号

提供 deadline_summary、remaining_work_s、feasible_work_s、overdue 状态，不直接写 stress。可行时间来自统一 agenda.free_windows，剔除睡眠、课程、交通与约定。

多个截止任务竞争同一空闲窗时先做简单分配，不为每个任务重复计算“完整空闲时间”；首版按截止优先分配，输出不足量。预测进度可修订但不能回改实际学习记录。

## 选课和学年

必修按课程包导入；选修从有限池按兴趣、时间冲突、容量与学分需求选择。有明确抽签规则才模拟抽签。当前学期初始化后不重启重选。

升年级与学年规则关联，不每个学期自动加一年级。教师/同学使用稳定档案与互动记录，不运行独立常驻 Agent。

## 接口与验收

查询 next_sessions、session_context、deadlines、workload；命令 revise_session、record_participation、record_study、submit_assignment、record_grade。日程承诺、状态负荷和活动结果通过 03 的契约交接。

验收：调课撤销旧任务；错过课不补写出勤；作业分段后重启可续；同事件不重复建作业；未读取材料不产出“已学会”事实；零可用时间不除零；同一空闲窗不重复分配；期末只产生一次评价。

日历格式可参考 [RFC 5545](https://www.rfc-editor.org/rfc/rfc5545.html) 的重复规则与例外，不必重造格式。

## 页面 API

A4 提供默认关闭的手动学期、课表 CRUD 与课次只读接口；A5 沿用同一开关与命令入口，增加大纲、作业 CRUD 和学习进度只读接口。功能未启用、运行时未装配 academics service 时返回 `missing_dependency`；不会因为读取页面自动启用。手动输入记录的来源固定为 `manual`，不连接真实教务平台。以下接口属于好好生活页面代理，不改动冻结公共契约或现有严格 v1 处理器。

路由前缀为 `/astrbot_plugin_good_life/targets/<target>/<role>/academics`。`target` 与 `role` 来自已授权的 `/astrbot_plugin_good_life/targets` 响应；`role` 是页面返回的 opaque token。请求必须来自登录的 AstrBot Dashboard，好好生活插件身份、有效配对注册、私聊目标和可管理角色均需通过宿主校验。API key 身份不能使用这些页面接口。

| 方法与后缀 | 配对 operation | 请求 | 成功 data |
|---|---|---|---|
| `GET /term` | `academics.term` | 无 query | `{schema_version: 1, revision, terms: [Term]}` |
| `GET /timetable` | `academics.timetable` | `term_id` | `{schema_version: 1, revision, term_id, entries: [Entry]}` |
| `GET /occurrences` | `academics.occurrences` | `term_id, start, end` | `{schema_version: 1, revision, term_id, timezone, start, end, items: [Occurrence]}` |
| `GET /syllabus` | `academics.syllabus` | `term_id, course_id` | `{schema_version: 1, revision, term_id, course_id, units: [SyllabusUnit]}` |
| `GET /assignments` | `academics.assignments` | `term_id, course_id` | `{schema_version: 1, revision, term_id, course_id, assignments: [Assignment]}` |
| `GET /study_progress` | `academics.study_progress` | `term_id, course_id` | `{schema_version: 1, revision, term_id, course_id, items: [StudyProgress]}` |
| `POST /command` | `academics.command` | 下述 JSON 命令，无 query | `{schema_version: 1, revision, kind, id, deleted}` |
| `GET /places` | `academics.places` | 无 query | `{academics_enabled, classes_enabled, graph: {graph_id, version} 或 null, places: [{place_id, name, type}]}` |

`/places` 供课表录入页使用：`academics_enabled` 为假时仍正常返回，页面据此说明「课表功能未开启」；`classes_enabled` 表示上课活动是否真正在运行。地点来自 `academics_graph_id` 的最新已确认图包，只列出有名称的地点，去掉路口和普通建筑；没有已确认图包时 `graph` 为 null、`places` 为空。它只读、不启用任何功能，同样要求 Dashboard 身份与目标角色授权。

成功使用现有宿主响应包装 `{status: "ok", message: "", data: ...}`；失败使用 `{status: "error", message: code, reason, data: {code}}`。所有 GET 拒绝重复、未知或缺失 query 字段。课次日期使用学期时区的规范 `YYYY-MM-DD`，区间为本地日期 `[start, end)`，长度为 1 至 31 个本地日期。客户端不传时区或 UTC 时间作为查询边界，返回的 `timezone` 来自学期。

学期和课表的输入记录字段必须完整且精确，嵌套对象也拒绝未知字段。

| 记录 | 字段与形状 |
|---|---|
| Term | `term_id, academic_year, timezone, start_date, end_date, week_starts: [YYYY-MM-DD], holidays: [YYYY-MM-DD], makeup_days: [{date, teaching_date}], source: "manual"` |
| Entry | `slot_id, term_id, course: {course_id, name, kind: "required" 或 "elective", credits: number, teacher_ref: string 或 null}, weekday: 1..7, start_time: "HH:MM", end_time: "HH:MM", weeks: [int], week_rule: "all" 或 "odd" 或 "even", place_id, exceptions: [{date, cancelled: bool, start_time: "HH:MM" 或 null, end_time: "HH:MM" 或 null, place_id: string 或 null}], source: "manual"` |
| Course 更新（只用于 `kind: "course"`，`action` 只能是 `update`，`id` 为 `course_id`） | `term_id, course_id, name, kind, credits, teacher_ref`：在一个事务里改写该学期所有用这个 `course_id` 的上课时间的课程信息，编号不变；只记一次 revision；找不到这门课时返回 `stale_version` |
| Occurrence（只读） | `session_id, slot_id, term_id, course_id, course_name, start_at, end_at, local_date, place_id, revision, cancelled, source` |

未另标类型的 ID、名称、学年、时区和日期/时刻字段均为字符串；`revision` 为正整数，顶层未初始化时为 0，`schema_version` 固定整数 1；`cancelled`、回执的 `deleted` 为布尔值。`academic_year` 是用户提供的非空学年标签，`timezone` 是可用的 IANA 时区；`weekday` 为 ISO 星期（周一 1、周日 7），`credits` 是 0..100 的有限数值，不能用布尔值代替数字。非空标识与名称最长 256 字符。Term 与 Entry 的读取对象额外包含 `schema_version`、`revision`；这两个字段不能放入输入 `record`。Occurrence 的 `start_at`、`end_at` 为 UTC RFC3339 字符串（`Z` 后缀），`local_date` 为该学期时区的日期。公开响应逐层保留上述已知字段，不转发 service 新增的内部字段。

`end_date` 为排他边界，学期长度为 1..366 个本地日期。`week_starts` 按时间递增、互不重叠，数组位置从 1 起定义教学周编号，最多 53 周；周起点必须位于学期内，最后一周可因学期结束而截短。Entry 的 `weeks` 必须是非空、无重复的有效教学周编号，`week_rule` 再按教学周编号筛选单双周。上课开始/结束均为同一本地日期的 `HH:MM`，必须开始早于结束；DST 不存在或重复的时刻会被明确拒绝。每个 workspace 最多 8 个学期、128 个课表时段，跨课程和学期的有效课次不能重叠；相邻下课/上课时间相同允许。

`holidays` 排除指定日期的常规课次；`makeup_days` 将 `teaching_date` 的课程移到 `date`，替换补课日自身的常规课表。两个日期都必须在学期内；不得重复目的/来源日期、形成补课链或将补课日设为假期。`exceptions` 按展开后的实际上课日期定位，所有字段都必填；时间/地点为 null 表示继承原时段，`cancelled: true` 表示停课。例外必须命中实际展开课次，假期没有课次时不能再对该日期添加例外。课程 ID 在同一学期的多个时段中须使用一致的课程元数据。`place_id` 是校园图的稳定地点引用；录入阶段可保存引用，开始到课活动还必须解析到已配置的可达校园地点。

课次 ID 由学期、时段和原教学日期决定，调整教室/时间或补课日不会换 ID；未来课次内容变化递增课次自己的 `revision`。删除时段会将未来课次标记取消；已结束或已认领课次保留原快照，未认领且仍在上课的课次允许修订/停课，不会报告成功后静默忽略。新增或替换时段只物化 `start_at > now` 的新课次，不凭手动录入补造过去课次。已有此类历史或认领课次后，不允许修改学期时区，避免历史查询的本地日期含义变化。学期删除前必须先删除该学期的课表时段；删除保留内部历史和 tombstone，不能复用同一记录 ID 创建新对象；已删除或不存在学期的课表、课次页面查询均返回 `missing_dependency`。

命令 JSON 的公共字段严格为 `command_id: 非空字符串, expected_revision: 非负整数, action: "create" | "update" | "delete", kind: "term" | "timetable" | "syllabus" | "assignment", id: 非空字符串`。创建、更新必须额外提供完整 `record`；删除省略 `record`。Term 的 `record.term_id`、Entry 的 `record.slot_id`、SyllabusUnit 的 `record.unit_id`、Assignment 的 `record.assignment_id` 必须等于命令 `id`。例如首次创建学期：

```json
{
  "command_id": "manual-term-2026-fall-1",
  "expected_revision": 0,
  "action": "create",
  "kind": "term",
  "id": "2026-fall",
  "record": {
    "term_id": "2026-fall",
    "academic_year": "2026-2027",
    "timezone": "Asia/Shanghai",
    "start_date": "2026-09-01",
    "end_date": "2027-01-31",
    "week_starts": ["2026-09-07"],
    "holidays": [],
    "makeup_days": [],
    "source": "manual"
  }
}
```

请求体最大 256 KiB（262144 字节），必须为 UTF-8 JSON，重复对象字段和非有限数值均被拒绝。客户端不能传 `owner_key`、`context_key`、`now` 或其他身份、权限、时钟字段。宿主在 controller 锁内取得 `LifeWebAPI.manager()`，执行 `manager.authorize(operator)`，从可信对象导出 `digest(manager.owner)`、`manager.context.key` 与命令所用 `manager.app.now()`；释放宿主锁后，通过 `asyncio.to_thread` 调用 academics service，避免反向获取存储锁及同步 SQLite 工作阻塞事件循环。领域写入只更新内容改变的课次行。

`revision` 是同一 owner 全部学期、课表、大纲和作业共享的写入版本，首次命令前为 0。客户端使用最近一次读取的顶层 `revision` 作为 `expected_revision`，不能把单条记录的版本当成独立写入栅栏。服务负责领域校验、原子版本栅栏和命令幂等：同一 `command_id` 重试相同命令返回最初回执，即使期间有其他写入，回执中的 `revision` 仍是首次成功时的版本；需要最新状态时重新 GET。不同内容不能复用已用过的 `command_id`。页面路由不会自行加版本或模拟命令回执。

主要领域错误为 `invalid_config`（JSON/字段/记录/查询或领域约束无效，包括重叠时段、无效教学周、同一命令号异参和历史学期时区变更）、`stale_version`（版本冲突、记录存在性不符）、`missing_dependency`（未启用、学期不存在或缺少依赖）、`unauthorized`（服务拒绝访问，包括跨私有 context 的 workspace 访问）。失败无部分写入，`reason` 提供可展示的校验说明。宿主鉴权与配对包装继续使用已有错误，包括 `dashboard_identity_required`、`life_provider_unavailable`、`life_dashboard_invalid`、`staging_host_terminated`、`staging_not_initialized`、`life_scope_invalid`、`life_target_changed`、`core_access_required`、`authorization_denied`。服务通过带 `.code` 的 `ValueError` 返回冻结错误码，宿主不直接导入好好生活插件实现。

### A5 大纲、作业与学习进度页面 API

`GET /syllabus`、`GET /assignments`、`GET /study_progress` 都要求且只接受单个非空 `term_id` 与 `course_id`，拒绝未知、重复或缺失字段。三者在既有 academics GET 旁注册，使用相同的 Dashboard 配对身份、目标角色授权、owner/context 隔离和 `academics_enabled` 默认关闭开关。GET 只读取 service 投影，不推进学习或生成评价；`study_progress` 不能作为写入命令的 `kind`。

| 对象 | 字段与形状 |
|---|---|
| SyllabusUnit | `unit_id, term_id, course_id, week: int, topic, concepts: [string], materials: [Material], questions: [string], assignment_ids: [string], source: "manual"` |
| Material | `source_ref: SourceRef, audience_bound: {permission_ref, context_keys: [string]}` |
| SourceRef | `source_id, version: string 或 null, content_digest: string 或 null, fragment_start: int 或 null, fragment_end: int 或 null, retrieved_at, collected_at: string 或 null, permission_ref` |
| Assignment | `assignment_id, term_id, course_id, unit_id, title, due_at, chunk_work_s: int, estimated_work_s: int, source: "manual"` |
| StudyProgress（只读） | `assignment_id, chunk_id, chunk_index: int, due_at, required_work_s: int, completed_work_s: int, remaining_work_s: int, status, revision: int, evaluation: string, provenance` |

未另标类型的字段为字符串；SourceRef 的时间和 `due_at` 为带时区的时间戳字符串，工时单位为秒。每个学习块的 `due_at` 继承所属作业的截止时间。作业 ID 最长 252 字符，为稳定分块 ID 的 `:0` 至 `:127` 后缀预留空间，使完整引用不超过内核的 256 字符限制。`materials` 可以为空，表示尚未绑定材料；有材料时必须完整保留来源片段与受众权限引用，不接受直接内嵌全文或额外权限字段。`evaluation` 是单个字符串，不能传回任意对象或数组。`provenance` 保留 service 给出的来源标记，不把模拟进度提升为真实阅读、真实作业提交或真实成绩。

大纲与作业读取记录各自附带 `schema_version`、`revision`；顶层响应始终包含 `schema_version`、共享 `revision`、`term_id`、`course_id`。输入 `record` 禁止这两个元数据字段，完整字段集和所有嵌套形状均严格校验；输出递归按上表白名单裁剪，不修改 service 原始结果。命令使用 `kind: "syllabus"` 或 `"assignment"`，沿用既有 `POST /command` 的 create/update/delete、可信宿主时钟、`asyncio.to_thread`、原子 revision 栅栏、命令幂等和冻结错误码；页面不自行生成进度回执或处理版本冲突。引用、日期与工时等领域有效性由 academics service 进一步校验。

### A5 学习装配与规则

`LifeRuntime.propose_study(scope, opportunity_key=..., now=..., session_s=1500)` 是显式异步学习意图入口，和 `propose_class` 一样由内核准备、开始及推进；不接管旧学习/阅读调度。底层 `activities.study.propose_study` 也可供显式 embedding 使用。每次按截止时间、作业 ID、分块序号选择首个未完成块；默认每次 1500 秒，允许 1..2400 秒的会话。作业按用户提供的 `chunk_work_s`（60..2400 秒）与 `estimated_work_s` 拆块，最多 128 块，尾块保留不足整块的工时。创建顺序为先建 `assignment_ids=[]` 的大纲、再建作业，需要时更新大纲反向关联；同一课程可有多个周主题。在途认领期间作业与大纲正文固定，大纲的 `assignment_ids` 仍可增删以添加后续作业。认领结束后可更新作业标题/截止时间、修订大纲或按依赖顺序删除记录；已产生进度的作业工时结构与课程/单元关联固定。删除只留 tombstone，不删除分块账本或活动中的版本快照，因此可正常退役课程与学期。

材料引用在每次新学习意图中经 A3 共用的 `read_activity_material` → 宿主 `read_material` 重新核验权限、版本和片段，外部 I/O 不在内核事务中执行。材料不存在、撤回、版本变更或端口不可用时拒绝该机会；正文随读取丢弃，不写模板、checkpoint 或事件。该执行器只估算作业工作量，不累计真实阅读覆盖、不产生引语、真实草稿或掌握/提交/成绩事实；教材阅读仍由 A3 执行。纯模拟推进后不再次访问源正文，因此在途执行与恢复不使用旧正文缓存。

A6 精力子模型关闭时固定效率 1、负荷 0.5，评价版本 `fixed_rule_v1`。开启时只读 A6 的 `energy` 快照，效率为 `max(0.25, energy)`，负荷为 `1 - energy/2`，评价版本 `shadow_energy_v1`；评价字符串追加 `:no_work`、`:partial_work` 或 `:chunk_complete`，只评价模拟工作完成程度。参数在本次活动中固定，下一次学习重新读取。A6 当前没有独立的公开负荷状态，因此这里是基于精力的显式规则估计，不写回或新造 A6 状态。缺失、不可用或超过内核快照时效的启用精力拒绝新学习，不悄悄回落为满精力。

`study_local@1` 使用 A1 原有 Effect/资源准备扩展原子认领当前分块及版本，按在线秒数与固定效率累计模拟工作量。会话完成不等于作业完成；下一次会话使用新的实例 ID 接续同一分块，完成后才选择下一块。任一分块仍被在途或暂停实例认领时，新学习意图跳过整个作业，仍可选择其他作业。每个机会号使用独立模板，门控资源绑定完整模板参数（含会话时长与精力规则），预先排队的其他学习候选不能借用认领实例的恢复资格；既有持久模板继续支持恢复。暂停/取消保留已提交工作量，恢复不补算离线区间；终态先持久提案再提交，崩溃重放不重复增加进度，放弃未提交提案只释放认领并保留已结算进度。进度的条目 `revision` 是分块自己的版本，和手动 CRUD 的 workspace revision 分开。

### W6 分步学习与产物

`study_enabled` 默认为 `false`，依赖 `academics_enabled`。启用时增建 `academics_study_work_schema_version=1` 的游标、产物及补学表。A5 的 `study_local@1` 继续使用原检查点和回执，`/study_progress` 读取同一个分块账本。W6 到地点执行与生活循环的接线另行交付。

可信协调器通过 `Academics.study_artifacts.plan(owner, context, window_s=..., travel_s=..., departure_s=...)` 计算一个空窗。先扣去程和离场，再按 UTC 截止时刻、作业 ID、分块序号分配。返回逐块分配额、工时缺口、交通缺口、零窗标记及首个待续游标。截止顺序变更后可用 `cursor(owner, context, assignment_id, chunk_index)` 读取原分块的下一步。`advance` 以作业、分块及游标 `expected_revision` 进行单步提交。`StudyWalk.arrived` 由导航提供到达证据。`work_s` 由协调器提供在线工时，`allocated_s` 为当次剩余分配额，返回 `used_s` 供协调器扣减。工作达到分块剩余量的三分之一、三分之二及全部时，分别生成草稿、笔记及复习题，向上取整。未达到下一阈值的时间保存在原步骤，产物与进度在同一事务提交。`checkpoint` 核验三份产物及其材料后结算，下一次计划才继续同作业的下一分块。

生成器版本 1 使用课程主题、首个概念、首个复习提示和预置虚构书屋案例。产物标为 `simulated`。生成前及提交前经原材料端口重新核验来源和权限，只保存链接引用。读取也重新检查原引用，撤回时保留条目并阻止打开正文，恢复授权后可继续原游标。`consume_attendance` 接收 `academics.class.catch_up` v1 事件，按 `(owner, session_id, kind)` 保留首个 `catch_up_notes`、`borrow_notes` 或 `extra_load` 待办，附带可选作业及建议工时。

| GET 后缀 | 配对 operation | 查询及 data |
|---|---|---|
| `/study_artifacts` | `academics.study_artifacts` | `assignment_id` 必填，`limit` 默认 20、范围 1–50。`before` 使用上页 `next_before`。返回 `{schema_version, items, next_before}`，按提交顺序倒排。 |
| `/study_artifacts/<artifact_id>` | `academics.study_artifact` | 无 query。返回条目及 `text`、`next_step` 两个纯文本字段。阻断时 `availability` 为 `blocked`，两字段均为 `null`。 |

条目包含 `schema_version, generator_version, artifact_id, assignment_id, chunk_id, chunk_index, kind, title, revision, updated_at, source, availability`。宿主先核验 Dashboard 登录、配对和目标角色，再以可信 owner/context 查询学业 workspace。详情对缺失与跨 workspace 返回相同的 `unauthorized` 响应。

### 模拟期末成绩

`grades_enabled` 默认 `false`，依赖 `academics_enabled`。取每门课最后一次课的结束时间与最晚作业截止时间中的较晚者，在此后 7–21 天的学期本地白天确定性发布。到课（含迟到）占 30%，最晚作业已完成的工作量占 50%，固定哈希抽样占 20%。无参与记录时到课按满分计，无作业时按比例重分其余权重。分数映射到 50–100，并按复旦等级换算。

启用时安装 `academics_term_grades_schema_version=1`。成绩以 `simulated` 来源、`term_grade_rule_v1` 规则写入 `life_term_grades`，与教务通知一起提交。同一角色、学期、课程只发布一次。陪伴侧通过生活事件收件箱将通知提供给角色的私聊上下文，相关对话可自然提及。`GET /grades?term_id=…` 在既有授权下只读已发布成绩。

### A4 装配与活动边界

插件配置 `academics_enabled` 默认 `false`。关闭时不安装学业/校园 schema，不创建候选、不注册活动钩子，现有生活处理器与调度行为不变。启用安装生活侧独立的 `academics_schema_version=1` 和 `academics_study_schema_version=1`；已有 schema 必须完整匹配，未知版本拒绝打开，不修改旧库版本、冻结契约、执行归属或生产开关。管理页读写通过 `LifeRuntime.academics` 的本地端口，读取不推进状态。一个 owner 的 workspace 固定首次写入的私有 context，另一个 context 不能读取或修改。

课次执行沿用 A1 的显式 embedding：需要启用内核、宿主验证的 `KernelAccess` 与 `Campus` 地点/有向步行路径图，不能从旧会话清单推造授权。`LifeRuntime(..., academics_config=AcademicsConfig(enabled=True), kernel_config=..., kernel_access=..., campus=...)` 完成装配；现有宿主没有该适配器时只能管理课表，课次不会偷偷接管旧调度。`Campus.locate` 用带版本的明确位置初始化/更正，未知起点或无路径时拒绝到课。首版只有本地步行，没有费用、外部地图或真实交通操作。

### 已确认图包的校园适配器

显式宿主可用 `Campus.from_settings(store, plugin_settings, places=<原手工地点>, paths=<原手工路径>)` 构造校园端口，再通过上述 `campus=` 注入；`academics_graph_id` 默认空，保留手工校园图。指定 graph_id 但尚无已确认包时也回落到原手工图。也可直接构造 `ConfirmedGraphCampus(store, graph_id, access_policy=...)`。确认图的归档仍走 `GraphPackageStore.preview/confirm`，不会读取未确认编辑。

有已确认包时，新课次报价读取该图的最新确认版本，并复用 A8 的 `OfflineNavigation`；同一套方向、门禁、开放时间和步行估算适用于课次与城市图内段。出发时刻由可信 `now` 传入。课次路线额外保存 `graph_id, graph_version, graph_digest, attribution, departure_at, assumptions, needs_review`，随模板、travel 参数和 checkpoint 持久化。新包只影响下一份报价，恢复和到达提交不读取最新包、不改变在途路程；归档支持 `version(graph_id, version, digest=...)` 精确读取旧包。图包路线的 prepare 和首次 start 必须落在固定出发时间窗内。报价包含 `departure_valid_until`，由同一路径、相同分段与等待的通行检查确定本秒内可接受的最晚出发时刻；恰好在关门边界完成的路线只接受原报价时刻。旧路线缺少此字段时仍使用精确 `departure_at`。模板仍在注册表时，窗口结束后的直接 prepare 沿用内核过期门控，将候选持久记为 expired，返回可重放的 `no_time_window`。若重启前尚未 prepare 且动态模板已离开注册表，直接 prepare 会先遇到模板缺失，之后由 tick 的候选隔离流程持久处理。协调器允许按持久记录重新报价，同 owner 的其他候选仍可继续。已准备但尚未开始的实例（包括空注册表重启后恢复）仍受原路线的资格和候选过期门控；新图包报价过期时转为 FAILED（`no_time_window`）并释放前台，不认领课次、不记录位置或出勤。另一个同课次报价不能覆盖原路线的门控结果。已开始的实例恢复继续固定路线，不重新查询开放时间或图包，也不受首次出发时刻限制。原四字段手工路线继续有效，冻结 contracts v1/v2 未变。

结构化门禁配置与词表见 [地理文档](13-geography.md#结构化门禁与开放时间默认关闭)。`graph_access_enabled` 默认 false，资格默认空；宿主必须显式传入对应设置，不从学籍、角色介绍、真实学校身份或课表推断资格。未知或未解析规则的可通行报价只在显式允许假设后生成，并固定假设及 `needs_review`；它们不会成为真实门禁规则。

`ClassActivities.tick` 在同一内核协调循环内生成有界候选。结构化开放时间启用时，提前到达须能在教室持续停留至上课。迟到路线只要求教室在到达时开放，并按 `travel_delay` 记录迟到。协调器选择剩余长度至少为 `early_departure_lead_s`（默认 300 秒）的最晚可行出发窗，从窗口截止前的预留时刻起在所选窗口内出发。没有足够宽的窗口且当前能立即走时，就按当前时刻出发。报价固定确认版本，提前到达后的等待占用活动前台。没有满足教室开放条件且能在课次结束前到达的路线时，tick 返回 `provider_unavailable`。手工路径和未启用结构化开放时间的图包仍按课次开始减路线耗时判断。`propose_class` 也支持显式到课/缺席决定，显式选择仍可包含起点等待。`class_session_local@1` 运行 `travel → wait → attend` 或 `skip`，通过组合快照门控、事务性课次认领及 `RecordRef` 围栏阻止旧调课候选与重复出勤决定。完成旅行后才提交位置，线上进度按单调时钟且只累计课堂时间内的实际参与区间；重启保留首次开始时间，离线不补记旅行/出勤，跨下课恢复终结已知进度。结果与效果提交使用内核原有持久提案/去重通道，崩溃后只重放提案；放弃提案保留已提交参与事实，记录 abandoned，不写入被放弃的出勤。

到课/缺席、迟到与早退均保留明确原因，首版原因集合为 `travel_delay, health, other_obligation, manual_decision, late_start, missed`。取消保留已参与区间；同一课次只有一份参与决定，缺席不会另建一份出勤。出勤来源为 `simulated`，手动课表来源仍为 `manual`。A4 不生成学过教材、掌握知识、教师引语、作业或考试成绩；大纲/作业/学习由 A5 接续，点名后果需要另有明确课程规则才能结算。

课次通过通用 `remaining_duration_s` 钩子按 `end_at - now` 门控，在线进度仍只按真实在线区间累计。`ClassActivities.tick` 主动处理已过下课时间的 running/paused 课次：可恢复的 running 课次正常结束；现有忙闲/权限条件阻止恢复时，用已有 cancel 边结束已知局部进度，释放前台，不留 active 参与记录。自动恢复中因时间窗暂停且仍未下课的课次会重新检查真实剩余窗口；当前门控仍拒绝时跳过 start，不在每 tick 追加拒绝 gate/command，满足门控后仍执行 start 的完整最终校验。显式人工暂停不自动恢复。学业扫尾与提案按 scope 隔离普通异常，仅返回统一错误码或 `unexpected_error:<type>`，继续其他 scope 并调用底层内核 tick，不因单个课次的宿主/存储故障阻断钱包或其他领域。准备后固定的路线报价不随新地图改写；同地点修正版本不阻塞到达，实际起点被更正到另一地点时明确取消，待提交到达提案与位置冲突时仅放弃未提交提案并保留已结算事实。上述行为不绕过 unknown 或授权。

重启后的在途模板从持久实例读取；没有持久实例且内存模板已丢失的旧候选由通用候选隔离逻辑明确 rejected/expired，不能堵住其他合法候选。学业 workspace 的私有 context 限制只约束学业投影，不阻止同 owner 其他 context 的无关领域活动。未知位置不反复生成失败候选，已拒绝课次不遮蔽后续到期课次。动态模板按 owner/context 索引后才投影，各 owner 使用相同课次 ID 也不会互相覆盖门控资源。因 `missing_dependency` 被拒绝的候选和出发时间过期的图包报价允许以关联原记录的新机会号重新提出；其余拒绝与过期仍阻止相同课次版本重提。每轮协调结束后回收没有 pending 候选或非终态实例引用的动态模板和 owner/context 索引；持久实例的模板快照及重放回执保留。

### A4 出勤与早课闹钟

启用 `attendance_enabled` 后，前一晚夜间睡眠的实际结束时间加 20 分钟准备时间，就是角色可出发的时间。到出发时还没醒，就等睡眠结束。确认醒来后，或即使立刻醒来也赶不上下课时，按课次持久化一次判定。出发时没有当晚睡眠记录，或关闭该开关，按计划出发时间就绪。

课程和出勤都启用且学业服务已接入时，计划夜间睡眠会检查本地次日最早的上午课。在午夜后才就寝时，检查当天上午。闹钟最晚设在开课时间减去预计路程和 20 分钟准备时间。优先沿用课程协调器的路程估算，缺少估算时按 30 分钟。没有早课时沿用常规睡眠时长。闹钟后仍可按既有睡眠生理规则延睡，缩短的睡眠照常计入睡眠债。

就绪时间晚于最迟准时出发时间而导致迟到，或加上预计路程后无法在下课前到达，归因为睡过头。身体不适和路上耽搁沿用原有判定。迟到和缺席的出勤结果与 `academics.class.catch_up` 待办同事务提交，分别生成补笔记或借笔记待办。课次改期保留已持久化的起床判定，跨日改期不沿用原日的身体不适。
