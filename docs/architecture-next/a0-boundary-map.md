# A0 边界与现有实现映射

核查基线：`c83ad623389a32234422e689618a0b874915a1f0`，分支 `w0/a0-contracts`。本文使用仓库相对路径，GL 代表 `plugins/astrbot_plugin_good_life/good_life/`，HOST 代表 `plugins/astrbot_plugin_private_companion/`。映射以代码为准；`docs/implementation-status.md` 已记录第一阶段切换，早期文档/注释中的“尚未接入”不能推翻当前代码。本轮不连接线上验证该记录。

本次新增 `GL/contracts` 仅纯声明，**下列“已有”均指基线运行实现**，不将新类型视为已接通功能。分类：“已有”表示语义已有证据；“需改名映射”也包括明确的结构/单位/协议适配，绝不只是机械 rename；“缺失”须后续实现，不凭默认值补造事实。

## 现有调用图及同步/异步边界

| 调用方向 | 当前调用链 | 调用方式与说明 |
|---|---|---|
| 生活 → 主插件：发现与注册 | `GoodLifePlugin.initialize/_watch_dependency/_dependency_state` → `LifeRuntime.bind` → `UnifiedCompanionExtensionAPI.register_life_provider` → `LifeProviderRegistry.register` | 插件初始化/重绑为 async；注册和 token 返回为 sync。发现核验产品契约、唯一激活实例和 readiness，受生活库 cutover 门禁控制 |
| 主插件 → 生活：事实查询及命令 | 宿主 calendar/activity/exposure/state/configuration 代理 → 提供方 lease → `dispatch/_invoke` → `LifeRuntime.handlers` | **sync**。注册拒绝 coroutine function，调用再拒绝 awaitable；旧租约撤销、inflight 排空、重绑代次必须保留 |
| 生活 → 主插件：现有基础能力 | 各 `*_scheduler` → `life_host_capabilities()` 返回的 `companion.life-host.v1` 字典 | 混合接口：contexts/快照等 sync，阅读/浏览/状态维护/唤醒/恢复等 async；不能以整个字典概括为全异步 |
| 生活 → 主插件 → 生活：阅读开始 | `ReadingScheduler._read` → `start_reading` → `ReadingStartHandoff` → 同步 activity 提供方 prepare/resolve/ack | 外围 async，跨库准备与提交 sync；宿主核验来源、节奏、身份、revision |
| 生活 → 主插件 → 生活：阅读执行 | `ReadingScheduler._read` → async `execute_reading` → `RoleReader.consume/_generate` → 监督器/模型 → `complete_supervised_reading` → `ReadingCompletionHandoff` → 生活活动提交 | **兼容桥**。生活拥有调度和最终事实；宿主仍组织阅读材料、提示词、感想生成，不等于目标通用模型端口已完成抽取 |
| 生活 → 主插件 → 生活：浏览/状态 | social scheduler → async `execute_social`；state scheduler → 宿主监督维护任务 → 状态 handoff/同步提供方 | **兼容桥**。平台/模型审计在宿主；生活写浏览/状态，宿主仍有业务验证和组织部分 |
| 生活事件 → 主插件 | `LifeStorage.record_event` → 主插件 `LifeEventRunner.advance` → `receive_pending/accept/consume_once` → sync `activity_history` acknowledge | outbox/inbox 核验、持久去重与 ACK 为 sync；dream/interest/wardrobe 后续模型或动作另起 async；ACK 不是下游完成 |
| 管理页 → 主插件 → 生活 | `UnifiedCompanionExtensionAPI.life_dashboard` → 身份与权限检查 → 生活服务 | Dashboard 是独立 async 入口，不能混入同步 provider handler 集合 |

提供方完整十二个 sync handler：`calendar_read/calendar_command/configuration_read/configuration_command/activity_read/activity_control/activity_history/browsing_read/browsing_command/state_read/state_command/state_schedule`。其中 `activity_history` 还承担事件读取/确认。

主要证据：`GL/runtime.py:LifeRuntime.bind/handlers`；`HOST/astrbot_host/life_provider_registry.py:register/_invoke`；`HOST/astrbot_host/integration_registry.py:UnifiedCompanionExtensionAPI`；`HOST/astrbot_host/life_host_capabilities.py:life_host_capabilities`；`GL/reading_scheduler.py:_read`；`HOST/companion_v2/role_work/reader.py:RoleReader`；`HOST/companion_v2/integration/reading_handoff.py:ReadingStartHandoff/ReadingCompletionHandoff`。

### 现有交接、读取屏障和审计

跨库路径为：生活 prepare → 宿主 decide → 生活 resolve（提交事实）→ 宿主 complete → 生活 acknowledge（核验宿主完成、解除屏障，活动完成事件在此确认事务发布）。不能将生活提交完成等同已对所有读取公开，也不能拆掉最后 ACK。

`GL/prepared_commands.py:PreparedCommands` 的 `life_prepared_commands` 保存 `command_id,owner_key,context_key,kind,expected_revision,payload_json,request_hash,prepared_at,state,decision_id,decision_hash,result_json`，状态为 `prepared/committed/acknowledged/aborted`。唯一索引约束每 owner 至多一个 prepared/committed 未决命令；同命令异摘要拒绝。这是协调状态，不能当活动状态。宿主决定/完成见 `HOST/companion_v2/integration/life_commands.py:decide/complete`。

联合快照已有 `LifeLedgerRouter.snapshot_factory/consistent_snapshot` 固定提供方代次，`LifeStorage.read_snapshot` 固定生活读，宿主 `storage/runtime.py:coordinated_snapshot` 协调同步事务。网络或模型等待不放进这些事务。

执行归属全局标记 `life.activity.execution_owner.v1` 仅 `legacy/good_life`，不是目标实例 `execution_owner=life/host_bridge`。迁到 good_life 后断连不恢复旧写库/旧执行器；`RoleLifeCoordinator.advance` 只保留宿主任务与事件消费推进。A0 不改变任何标记。

## 领域事实：当前写入者 → 目标唯一拥有者

| 领域事实 | 当前实际写入/表字段 | 现存桥接及目标唯一拥有者 |
|---|---|---|
| 活动实例、历史、操作回执 | GL `StagedActivityCreations.apply/StagedActivityTransitions.apply` → `staged_role_activities/staged_role_activity_revisions/staged_role_life_operations` | 宿主仍授权与决定；目标生活内核/活动账本唯一写入 |
| 阅读进度、片段、感想 | 生活活动 `record_json.reading` 的 `source/content_hash/text_length/start_offset/end_offset/completed_at/reflection`；完成时写下一 planned 片段 | `RoleReader` 仍生成感想；目标生活阅读执行器与产物账本，宿主只提供通用能力 |
| 原文登记及业务产物 | GL `staged_activity_creations.py` → `staged_role_artifacts(owner_key,artifact_id,activity_id,scope_key,record_json)` | 生活拥有业务引用与生命周期；原文字节/通用 SourceRecord 与撤回归宿主 |
| 浏览批次、曝光、尝试 | GL `StagedExposureCommands` → `staged_role_exposure_sessions/staged_role_exposures`，含 fetched_at/exposed_at/attempted_at/coverage/random_attempt | 宿主 execute_social、平台动作和兴趣反馈仍存在；目标生活拥有浏览过程，宿主拥有账号与基础动作审计 |
| 睡眠事实/中断 | 生活活动 work/sleep：slept_seconds/interruptions/wake_until/quality_min/quality_max，经活动变更提交 | 宿主入睡/唤醒/中断 handoff；目标 sleep 模块唯一写睡眠，梦境/换装仍宿主 |
| 精力、饥饿、健康、情绪、周期等旧状态 | GL `StagedStateCommands._apply` → `staged_role_state_records/history/receipts` 与对话游标 | 宿主 `RoleStateService._write` 和 `_HostStateEffect` 仍验证/存决定；情绪周期计算经生活提供方；目标各生活状态域唯一拥有，A0 不换公式 |
| 日程、历史、日程命令回执 | GL `StagedCalendarCommands.apply` → `staged_calendar_records/history/receipts` | 宿主身份/来源/提醒任务发送；目标生活 calendar 拥有事实，宿主发送通道 |
| 生活配置和状态维护日程 | GL configuration/state configuration 命令 → `staged_configuration_*`、`staged_state_configuration_*`、`staged_role_state_schedules` | 管理权限及跨库审计宿主保留；目标生活拥有业务配置 |
| 已提交事件及投递游标 | GL `LifeStorage` → `life_events/life_event_deliveries` | 生活唯一拥有事件；宿主 `life_event_inbox` 在 runtime_metadata 持久收件及消费者去重 |
| 人格、关系、长期记忆、兴趣、自我反思 | 宿主相关服务及事件消费者 | 目标仍主插件，生活不回写其表 |
| 日记、梦境、换装、聊天、主动联系 | 宿主 journal/dream/wardrobe/聊天/主动任务 | 目标仍主插件 |
| 身份、来源资产、模型任务、平台操作、用量审计 | 宿主 authority/sources/execution/storage | 目标仍主插件，生活只能通过受限端口引用 |
| 钱包、课程结构、学业事实、地理和模拟世界事实 | 基线无对应生活领域实现 | 缺失；目标 economy/academics/geo/environment 各模块唯一拥有，不由通用 state.set 修改 |

## 03 契约对象逐项映射

### 身份、版本、引用与单位

| 契约对象/字段 | 当前表/字段/函数 | 分类及适配决定 |
|---|---|---|
| OwnerKey | `foundation/refs.py:RoleOwnerRef` + `execution/models.py:digest`；活动 envelope owner_key | 已有角色身份摘要，不自行重算 |
| ContextKey | `execution/models.py:TaskContext.key`：principal/subject/binding ID+revision/role/audience 摘要 | 已有，不能用窗口 scope 冒充；不含 actor.policy_revision，调用仍重新授权 |
| 状态/旧记录 owner key | `role_work/state/ownership.py` 的 role-body.v1/role-state-maintenance.v1；`storage/versioned_records.py:owner_key` 的旧窗口摘要 | 需改名映射；旧表同名 owner_key 可能不是角色 envelope owner；`integration/life_state_records.py` 双读保留 |
| ExecutionScope | 宿主 contexts、授权检查、注册 lease 分散实现 | 需改名映射，缺统一 authority_ref 返回对象；不能传 app/DB 给生活 |
| SchemaVersion/TemplateVersion | 旧库 schema 版本；work.kind/read/sleep/social 专用配置 | 缺目标数据协议/模板版本，旧 schema 不可直接转换 |
| Revision | RoleActivity.revision 和 staged 历史/命令 expected_revision | 已有乐观版本；快照整体 revision 仍缺失 |
| RegistrationGeneration | 注册 UUID token、LifeLedgerRouter._generation、TaskRecord.generation | 缺统一绑定代次；三个现存值不可互换，后续适配器显式分配 |
| TemplateRef/RecordRef | activity/source ID、revision/work.kind 分散存在 | 需改名映射；模板版本缺失时不能捏造 ref |
| Money | 无钱包流水 | 缺失；未来最小币种单位整数，现有模型成本审计不是模拟钱包 |

`digest` 既有规范是排序紧凑、ASCII 转义 JSON 的 SHA256；目标新请求 params_digest 不得回写或替换旧 request_hash。

### 模板、候选、实例

| 03 对象 | 当前对应 | 分类 |
|---|---|---|
| ActivityTemplate | `role_work/work_models.py:RoleWorkSpec.kind` 和专用 scheduler/settings/prompts | 缺失统一模板；kind 仅迁移线索，不具备 requires/triggers/gating/inputs/params/version 注册 |
| Candidate | GL `reading_choices.candidates/choice_slot` 选 planned 活动，持久选书记录去重 | 缺失独立候选 ID/状态/过期/快照版本/消费事务；名称相同不代表实体已有 |
| ActivityInstance | `role_work/models.py:RoleActivity` + `RoleWorkSpec`；GL 活动 staged 表 | 需改名映射（部分已有），逐字段见下表；没有 `life_activity_instances` 表 |

活动主表：`staged_role_activities(owner_key,activity_id,owner_json,revision,state,updated_at,record_json)`，PK `(owner_key,activity_id)`；历史 `staged_role_activity_revisions` UNIQUE `(owner_key,activity_id,revision)`，DDL 见 `GL/activity_import.py`。

| ActivityInstance 字段 | 当前字段/函数 | 分类与差异 |
|---|---|---|
| instance_id/owner_key | activity_id + 索引 owner_key | 需改名映射 / 已有；同名活动跨角色须带 owner |
| context_key | work.context_key | 需改名映射；无 work 的旧记录缺失，不能从 scope 推测授权 |
| template_id/template_version/executor_version | work.kind | 缺失统一版本；kind 不足以确定版本 |
| execution_owner | 全局 legacy/good_life 标记 | 缺实例字段；阅读执行兼容桥需显式 host_bridge，不能从全局 good_life 猜 life |
| target_ref | reading.source + content_hash + 范围 | 需改名映射，其他活动缺通用目标 |
| candidate_id/causal_event_id | 无 | 缺失 |
| status | planned/active/paused/completed/cancelled | active→running；后三项可映射；planned 不等于 preparing；缺活动级 failed/unknown |
| revision | revision、expected_revision | 已有，写入递增并保存历史 |
| phase | 无 | 缺失 |
| started_at | 已有同名，但 planned 创建时也填 | 需语义映射，不能作为真实进入 running 时间 |
| updated_at | updated_at | 已有 UTC 时间，按目标 wire 规范转换 |
| planned_duration_s/elapsed_s | sleep.target_minutes/slept_seconds/due_at；阅读字符范围和速度 | 需单位/结构映射，非睡眠缺通用字段；不凭速度推断已执行时间 |
| checkpoint | 当前为有长度上限的字符串 | 需结构映射为 JSON 对象，保留原文而非解析成事实 |
| reservation_refs/source_refs/operation_refs | 工作、reading receipt、历史、宿主任务/操作表 | 部分已有引用证据，缺统一列表及预留机制 |
| outcome/reason_code | 转移回执/TaskResult/自由文本 | 缺统一实例字段；不能把 checkpoint 文本当 reason_code |

### 结果、回执、效果与产物

| 03 对象 | 当前对应 | 分类与约束 |
|---|---|---|
| ActivityResult | TaskResult + RoleReadingReceipt + 各 handoff/活动转移 payload | 缺统一领域核验后的结果提案；监督器 TaskResult 不是 ActivityResult |
| Outcome | 活动 state、任务 status、专用失败原因 | 需改名映射，缺实例 outcome.partial/summary |
| Progress | 阅读 start/end_offset、text_length；sleep.slept_seconds | 部分已有，缺通用 actual_duration_s/completed_units/partial |
| OperationReceipt | 宿主 storage.models.OperationReceipt、execution.models.TaskResult、生活 staged_role_life_operations | 需按双轴证据映射，不是改一个状态字符串；见下表 |
| Effect | 日程 cancel_reminder 等专用 effects、状态 handoff | 缺通用 effect_id/instance_id/target_owner/kind/payload/effective_at/source_refs 和逐效果领域核验 |
| EventProposal | 完成后单个 activity.completed 事件 | 缺执行器事件提案数组和 schema/source/受众；不得提前当已提交事件 |
| Artifact | RoleArtifact + staged_role_artifacts | 已有业务产物引用；需映射 type/content_ref/source_refs，独立 impression 和任务/人格版本关联缺失 |
| ShareCandidate | 无统一对象 | 缺失；分享决定仍宿主 |
| Provenance/FieldProvenance | 旧历史 provenance=recorded；阅读/来源证据分散 | 缺四分类与字段级 JSON Pointer；recorded 不是 source_backed，不能强转 |
| ContractError/ReasonCode | TaskError.code/reason、Outcome.failure、各领域错误类、ValueError | 缺统一十类映射；旧错误不得直接假定是新枚举 |

| 当前审计层 | 当前状态 | 映射要求 |
|---|---|---|
| TaskResult | status=succeeded/failed/uncertain；effect=none/not_committed/committed/unknown；result_ref/receipt_ref/operation_id/retryable | uncertain→unknown 且不能自动重试；检查 effect 与回执；已提交不等于可重跑 |
| execution_tasks | created/admitted/running/retry_scheduled/cancelling/succeeded/failed/uncertain/cancelled/expired/dead_letter；代码也兼容检查 rejected | 排队/执行/终态分别适配，未知值拒绝推断成功；generation 是 supervisor 代次 |
| OperationReceipt | state=accepted/completed/unknown；commit_state=committed/not_committed/unknown | accepted+committed 只可能是预留/决定落库，不足以证明业务成功 |
| model_calls | reserved/dispatched/generated/not_started/unknown/discarded；token/cost/result/receipt 字段 | generated 只证明模型返回，仍需来源/领域提交验证；未知用量不能当 0 |
| 跨库命令 | 生活 prepared/committed/acknowledged/aborted；宿主 decided/completed | 协调层状态，不是活动或模型任务状态 |

证据：`HOST/companion_v2/execution/models.py:TaskResult/TaskRecord`；`storage/models.py:OperationReceipt`；`storage/schema.py:execution_tasks/model_calls`；`GL/staged_activity_commands.py:apply/acknowledge`。

### 事件、状态和来源

| 对象/字段 | 当前对应 | 分类与缺口 |
|---|---|---|
| EventEnvelope.event_id/owner_key/context_key/occurred_at/payload | `GL/storage.py:record_event` → life_events | 已有，持久重复同内容返回原 sequence，异内容拒绝 |
| type | 旧 wire kind | 需改名映射，不能直接扩 strict v1 |
| aggregate_id/aggregate_revision | 完成 payload.activity_id/revision | 需改名映射，核对对应活动历史版本 |
| recorded_at/schema_version/causation_id | 无对应持久字段 | 缺失；sequence 不是时间，operation_id 不等于因果 event_id |
| source_refs/provenance/audience_bound | 宿主经活动版本、reading receipt 与权限服务核验 | 需证据聚合且有缺口，不能根据事件名称补造 |
| LifeEventReceipt | 宿主 runtime_metadata 的 life.event.inbox.v1:* + consumer/event/state | 已有持久收件语义，缺目标记录形状；ACK 不代表下游成功 |
| StateVar/StateRange/StateBucket | 固定 FIELDS、情绪/周期配置与公式 | 缺通用 key/owner_module/value_type/unit/range/initial/baseline/timescale_s/buckets/schema_version；保留原算法 |
| StateValue/StateQuality/EstimateAssumptions | 旧状态 values + 整条时间/revision/sources | 需改名映射并补逐变量 updated_at/revision/quality；预测模型/假设缺失，不默认为 valid |
| StateSnapshot | RoleStateService 投影 + coordinated snapshot | 一致读取基础已有，缺统一 snapshot revision 与 key→StateValue；现有部分读会推进，读写分离留 A6 |
| SourceRef | `foundation/refs.py:SourceRef` 的 source_id/owner/scope/version/issued_at/expires_at | 已有权限身份引用，需合并 SourceRecord 摘要/受众、work/receipt 范围、浏览 fetched_at；issued_at 不是 retrieved_at/collected_at |
| AudienceBound | TaskContext.audience、SourceRecord、来源授权与撤回依赖图 | 已有权限治理；缺目标 permission_ref/context_keys 形状，空集合不表示公开 |

当前事件 wire **精确字段集合**：`sequence,event_id,owner_key,context_key,kind,payload,content_hash,occurred_at,consumers`；宿主 `integration/life_events.py:_validate_event` 要求集合相等。完成事件 payload 精确为 `activity_id,revision,operation_id`；`event_id="life-activity-"+SHA256(operation_id UTF8)`，见 `GL/staged_activity_commands.py`。不能为契约 v1 往旧 wire 添加顶层或 payload 字段。

outbox：`life_events(sequence PK,event_id UNIQUE,owner_key,context_key,kind,payload_json,content_hash,occurred_at)`；投递：`life_event_deliveries(sequence,consumer,acknowledged_at)`，PK `(sequence,consumer)`。宿主 `integration/life_event_inbox.py` 以 runtime_metadata 的 `life.event.inbox.v1:` 前缀存 schema/state/consumer/event；journal/dream/interest/wardrobe 后续由 `LifeEventRunner` 推进。

## 02 主插件目标端口对应现状

九个目标 async 签名见 `contracts/ports.py:CompanionHostPorts`，当前没有以此对象注册，不得替换旧提供方协议。

| 目标能力 | 当前可复用基础 | 分类与后续工作 |
|---|---|---|
| resolve_authority | 注册 lease、TaskContext、authority/来源检查、life_host_capabilities 的运行检查 | 需适配，缺受限 ExecutionScope 对象 |
| get_persona_context | RoleReader/宿主任务访问人格兴趣记忆的既有编排 | 缺受限、有版本上下文端口；不得暴露整个 app |
| read_material | RoleReader 来源解析、SourceService/SourceRecord/来源依赖与撤回 | 需适配；范围、获取时间、权限与受众必须保留 |
| run_model_task / inspect_task | 宿主 supervisor、execution_tasks/model_calls/任务结果审计 | 基础已有，缺目标通用异步请求/回执协议；不以 execute_reading 重命名代替抽取 |
| execute_platform_action / inspect_action | 宿主平台路由、操作回执、execute_social 业务路径 | 基础已有，需通用授权动作端口，unknown 查原操作 |
| publish_asset | 宿主通用来源/资产与依赖图 | 基础已有，缺当次产物发布目标回执；保留撤回关系 |
| accept_life_event | LifeEventInbox/consume_once 与旧同步事件桥 | 已有持久接收，需单独协商目标 envelope/ACK；不改变原字段集合 |

## 验证入口与尚未完成的运行工作

现有根 `tests/` 使用 unittest，主插件也有 `tests/` Python 与 `.cjs` 页面测试，部分 Python 测试需要 pytest 或 AstrBot SDK。主插件 CONTRIBUTING 要求 SDK 测试隔离数据目录；测试结果数量必须以本次实际命令为准，不能引用历史 `docs/test-results.md` 当验证。本轮实际结果写入 PR。

A0 不建新运行表、不执行迁移、不改读时推进、不打开 marker。A1 需落地生命周期/候选/领域结算，A3 抽离阅读编排并给旧记录显式适配，A6 建状态元数据与快照；宿主通用端口、事件新协议须由生活和宿主任务共同实现并协商能力。未知旧记录的 provenance、真实开始时间、来源获取时间和受众不得默认补齐。
