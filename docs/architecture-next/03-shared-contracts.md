---
contract_version: 2
status: frozen
---

# 公共数据契约

v2 batch 1 冻结说明：审批人 coordinator 已批准 V2-01/02/03/07/08，V2-04/05/06 延至 A7。`CONTRACT_VERSION=2`；`good_life/contracts/{models,ports}.py` 保留 v1 字段、枚举和签名，新增声明集中在 `good_life/contracts/v2.py`。下文原有各节继续规定共用规则与 v1 记录；末尾 v2 专节是新增类型及语义的权威定义。冻结声明不代表所有提供方已经实现。现有 `companion.life.v1`、`companion.life-host.v1`、async.v1、严格 event wire、数据库 schema 和任务审计协议独立版本化，保持不变。现状见 [A0 边界映射](a0-boundary-map.md)，变更遵守 [申请流程](a0-contract-change-process.md)，迁移、发布配对与调用方见 [v2 迁移映射](contract-v2-migration.md)。其他规划有歧义时以本篇为准。

## 身份、时间、版本与单位

- `owner_key: OwnerKey` 是宿主核验的 `RoleOwnerRef(installation_id, bot_id, persona_id)` 的既有摘要，沿用 `execution.models.digest` 的规范，不由生活插件或 YAML 重算身份。`context_key: ContextKey` 是原始 `TaskContext.key`，保留会话主体、角色绑定版本和受众；不能代替 owner。现有状态表使用 `role-body.v1` 命名空间的存储键，维护调度使用 `role-state-maintenance.v1` 键，均需适配，不能直接当 envelope owner。
- `ExecutionScope(owner_key, context_key, authority_ref, registration_generation)` 仅由宿主 `resolve_authority` 返回；它是授权引用而非永久授权，调用时仍检查权限、来源、租约有效性。不能拿一个角色的 scope 操作另一角色，也不能扩大原 context 受众。
- 类型内时间点为 aware `datetime`；JSON 为 UTC RFC3339 字符串，规范输出 `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`，拒绝无时区时间。日历规则另保留 IANA timezone。持续时间统一非负整数秒，`timescale_s` 为正整数或 null。运行间隔使用单调时钟，恢复依 UTC 锚点。旧浮点 epoch 时间由适配器转换，不原地改库。
- `Money(amount_minor: int, currency: str)`：整数最小货币单位，币种为大写 ISO 4217；CNY 1000 表示 10 元。符号由领域命令解释，不由通用状态积分器扣钱包。不做汇率换算。
- `CONTRACT_VERSION=2` 是架构契约号；`schema_version: SchemaVersion` 为各数据对象协议号（v1 默认 1）；`template_version: TemplateVersion` 是模板定义号；`revision: Revision` 是并发记录号；`registration_generation: RegistrationGeneration` 是绑定代次，四者均为正整数且不能互换。`executor_version` 是非空、不透明的执行器版本字符串。旧库迁移版本不转成以上任何一项。
- ID 是非空字符串。`command_id` 为业务命令号，`operation_id` 为外部操作/效果提交号，`task_id` 为持久模型任务号，`event_id` 为已提交事实号。command/operation/task ID 的作用域为已核验 owner 与对应命名空间。`event_id` 则必须在同一安装的全部角色/事件类型间全局唯一，匹配既有 life_events UNIQUE(event_id)；消费者去重键为 `(event_id, consumer_id)`，因果引用也使用该全局事件身份。新 ID 分配须包含 owner/命名空间或采用全局唯一方案，旧确定性 ID 保持不变，发生碰撞拒绝而非覆盖。`effect_id` 在 owner 范围去重。同 ID 不同参数摘要拒绝为 `invalid_config`，不得重建 ID 绕过未知结果。既有幂等摘要算法保持原样。目标 `params_digest` 对请求的业务内容进行 SHA256：先按本篇 wire 规则转换，将 scope 替换为仅 owner_key/context_key，去掉顶层 params_digest 及该请求自身的 task_id/operation_id；其他字段和嵌套资源 ID 一律保留。任务/动作请求类型作为固定 namespace 一并参与（`model_task/platform_action/publish_asset/accept_life_event`），规范输入为 `{namespace, request}`，对象键递归排序、分隔符逗号/冒号、不加空格、ensure_ascii=false、UTF-8 编码，拒绝 NaN/Infinity。inspect 不另算摘要，提交原摘要；更新授权引用/绑定代次不改变原业务输入。event_id 作为事件身份保留在 event 内。同一 namespace 内摘要比较仍必须核验 owner 和上下文。宿主适配器须明确翻译，不能改旧摘要。
- `RecordRef(kind, id, revision)` 引用可版本化记录，null revision 表示该资源不支持版本，不能表示“任意最新版本”；`TemplateRef(id, version)` 始终固定版本。新实例固定 template/executor 版本，不能升级时默换。

## 传输与不可变性

声明包只有 frozen dataclass、Enum、类型别名、静态转移表和 Protocol，无 I/O、注册或序列化实现，现有运行代码不导入。Python `frozen=True` 仅限制字段赋值，`Mapping` 的只读性由构造入口负责：传入自有不可变映射，禁止保留可变别名；序列字段用 tuple。JSON 将 tuple 表示为数组，Enum 表示为 value，NewType 表示为底层 str/int；反序列化按目标类型恢复 tuple/Enum/dataclass/UTC datetime，不往 JSON 注入 Python 类型名。JSON 对象键只允许字符串，数值必须有限。边界校验、codec、持久化适配属于后续实现，本包不假装已执行这些校验。

显式 null 表示无值/未知，空集合表示已知为空；不得相互替换。扩展业务内容仅放在声明的 `JSONObject` 字段，接收方按具体 kind/type 与版本校验，不能依靠任意顶层字段扩展。

## ActivityTemplate 与 Candidate

`ActivityTemplate` 必需字段：`id, version, executor, requires, triggers, gating, inputs, params`；可选字段（默认 null）：`frequency, model_policy, presentation`；另有 `schema_version=1`。`executor` 使用 `name@version`，适配层核验与实例 executor_version 一致。05 的 YAML `schema: life.activity/v1` 映射到 schema_version，不把它当模板版本。触发器、输入槽和门控的 JSON 语义遵守 05/06/07，不包含余额、当前进度或实例 ID。

`Candidate` 必需字段：`candidate_id, owner_key, template_ref, target_ref, reasons, created_at, expires_at, dedupe_key, snapshot_revision, status`。target_ref 可为 null（无特定目标）；expires_at 必须晚于 created_at。reasons 是稳定触发原因引用数组；同 owner、模板版本、目标、机会实例合并，dedupe_key 不只是模板 ID。

候选状态完整集合：`pending, consumed, rejected, expired`。只允许 `pending → consumed/rejected/expired`；后三者不可再转移。消费和创建唯一实例须本地原子提交；消费不证明执行成功。旧 `planned` 活动既不自动变成 Candidate，也不自动变成 preparing。

## ActivityInstance 与生命周期

必需字段与精确类型见 `models.ActivityInstance`：

~~~text
instance_id, owner_key, context_key
 template_id, template_version, executor_version, execution_owner
 target_ref, candidate_id, causal_event_id
 status, revision, phase, started_at, updated_at
 planned_duration_s, elapsed_s, checkpoint
 reservation_refs, source_refs, operation_refs
 outcome, reason_code
~~~

`execution_owner` 仅 `life/host_bridge`，一个实例固定一条执行路径；host_bridge 表示现有宿主专属执行兼容桥，不是宿主拥有生活事实。target_ref/candidate_id/causal_event_id 可为 null。phase 为执行器声明的非空阶段。started_at 是第一次进入 running 的时刻，准备期间 null，恢复不重置；updated_at 是最近记录更新。elapsed_s 只累计实际执行，不含停止的暂停等待；planned_duration_s 不保证完成时间。checkpoint 是版本固定执行器的 JSON 对象，reservation_refs 引用领域预留，operation_refs 引用稳定操作号。

状态完整集合和唯一合法转移表（`ACTIVITY_TRANSITIONS`）：

| 当前状态 | 允许下一状态 |
|---|---|
| preparing | running, failed, cancelled, unknown |
| running | paused, completed, failed, cancelled, unknown |
| paused | running, failed, cancelled, unknown |
| unknown | preparing, running, paused, completed, failed, cancelled |
| completed / failed / cancelled | 无 |

实例首次持久化为 preparing，revision 从 1 开始；每次事实更新递增，命令 expected_revision 不匹配返回 stale_version。重复提交相同命令返回原回执，不执行“自转移”；同状态 checkpoint 更新是 revision 更新而非状态转移。暂停只在执行器允许阶段发生；恢复再次门控。

unknown 是原操作结果待核实且前台不可用的阻塞状态。离开 unknown 的每条边均要求原 operation/task 的持久回执或明确人工核实证据；恢复到 preparing/running/paused 还须证明未重复副作用并重新检查授权/资源。恢复到 preparing 仅限从未实际开始、started_at=null 的实例；running/paused 要有真实开始证据并保留首次 started_at；终态须核实该实例的实际结果，不能只依据某个准备调用成功。无证据继续 unknown，不能以超时、插件重绑、cancel 命令或新执行器直接解除。终态不重新打开；更正通过独立带版本命令/新实例保留历史。

`Outcome(status, partial, summary, reason_code)` 的 status 仅 `succeeded/failed/cancelled`；completed 对应 succeeded，failed 对应 failed，cancelled 对应 cancelled。非终态 outcome=null。部分完成用 outcome.partial 与 checkpoint 表达，无 partial 状态；失败/取消也保留实际进度。succeeded 的 reason_code=null，failed 必填统一错误码，cancelled 必填 cancelled；summary 提供可展示说明，不解析为机器原因。终态提交将 outcome.reason_code 原样写入 ActivityInstance.reason_code；非终态如 unknown 可单独记录 result_unknown。即使本地失败没有外部操作回执，ActivityResult.outcome 也必须携带原因；单个操作回执原因仅解释该操作，不自动代替实例终结原因。

## ActivityResult、回执、效果及产物

~~~text
instance_id, expected_revision, outcome, occurred_at
progress: actual_duration_s, completed_units, partial
receipts[]: operation_id, kind, status, evidence_ref, reason_code?, message?
effects[]: effect_id, instance_id, target_owner, kind, payload, effective_at, source_refs
events[]: type, schema_version, payload, source_refs, provenance, audience_bound
artifacts[]: artifact_id, type, content_ref, source_refs, provenance, audience_bound, field_provenance
share_candidates[]: artifact_refs, relevance_reason, audience_bound
~~~

结果是执行器的终结提案，不是已提交事实；运行中阶段 checkpoint/效果由内核领域提交协议处理，不能将此类型当作持久事务实现。progress.actual_duration_s 是本实例实际累计值，非每次重试的增量；completed_units 是领域定义单位和值的 JSON 对象；progress.partial 必须等于 outcome.partial。领域拥有者核验效果、费用、权限、revision 后才能提交事实与 outbox。只允许当前 owner/instance 的效果和产物。

`OperationReceipt.status` 完整集合：`pending/running/succeeded/rejected/failed/cancelled/unknown`。pending 表示已持久受理尚未运行，running 表示执行中；succeeded 必须有可核验证据，evidence_ref 非空。rejected 表示执行前业务拒绝，failed 表示已知失败，cancelled 表示已确认停止；后三者不得据状态推断“没有部分副作用”，需查 evidence。unknown 必须 reason_code=result_unknown，保留原操作号。回执是归一化目标视图：旧 TaskResult 的 `uncertain` 只可映射 unknown，旧 OperationReceipt 的 `accepted` 不能映射 succeeded，`completed` 还需检查 commit_state。不修改旧枚举。

效果的 target_owner 是唯一注册领域模块名（例如 affect/economy），不是角色 owner_key；实例 owner 仍由提交范围核验。effect_id 在提案首次持久化时确定，重试稳定；不能靠随机新 ID 二次结算。EventProposal 的原草案 `version` 统一为 `schema_version`。事件只在事实提交后成为 EventEnvelope，提交事务中分配并保存 event_id，重放不重抽。现有由 operation_id 确定的事件 ID 保留。

“当次感想”用 `Artifact.type=impression`；情绪变化用 affect Effect；值得记住用宿主消费候选。Artifact.content_ref 指受权限管理的资产引用，不是正文副本。ShareCandidate 不授权公开或发送，artifact_refs 必须指已获权限的产物；宿主长期记忆/分享失败不回滚生活完成事实。

## EventEnvelope 与宿主 ACK

精确字段：`event_id, type, schema_version, owner_key, context_key, aggregate_id, aggregate_revision, occurred_at, recorded_at, causation_id, payload, source_refs, provenance, audience_bound`。

occurred_at 是业务有效时间，recorded_at 是本地落库时间；causation_id 是原因事件 ID 或 null，不是 operation_id 的混合字段（操作号写 payload/回执引用）。aggregate_id/revision 指已提交聚合版本。事件源先持久化 outbox 再投递；宿主先持久化 inbox/去重后 ACK，至少一次投递加本地幂等，不承诺外部副作用 exactly-once。

现有严格 v1 wire 为 `sequence,event_id,owner_key,context_key,kind,payload,content_hash,occurred_at,consumers`。目标 envelope 不直接替换或扩充该字典。新协议须单独能力/版本协商；旧桥读取原活动版本/授权来源补足可核验字段，无法证明的字段拒绝转换，不捏造 provenance 或受众。

`LifeEventReceipt(event_id, consumer_id, owner_key, status, inbox_ref, recorded_at, reason_code, message)`：consumer_id 是接收宿主的稳定 inbox 消费命名空间，不是本次异步记忆任务号。只有持久 inbox 已建立时 status=succeeded 且 inbox_ref/recorded_at 非空；表示接收成功，不代表记忆、日记或分享完成。pending/running/succeeded 的 reason_code=null；rejected/failed 必填统一原因和可展示 message；unknown 必填 result_unknown，cancelled 必填 cancelled。拒绝/失败/unknown 保留原 event_id 查询，按原因决定重投或人工修复；不能重新执行原活动。

## StateVar、StateValue 与 StateSnapshot

`StateVar`：`key, owner_module, value_type, unit, range, initial, baseline, timescale_s, expose_to_llm, buckets, schema_version`。原草案 timescale 统一为 timescale_s。value_type 完整集合 `boolean/integer/number/string/object/array`。unit 非空（无量纲用 `1`）。range 为 StateRange(minimum, maximum) 或 null，数值范围上下界包含；null 单侧表示无该侧界。StateBucket(label, lower, upper) 使用左闭右开区间，null 为无穷边界；不允许重叠，分档变化须升级描述 schema。baseline 为无此含义时 null，initial 可以为 null，但不能因此确认事实。钱包/位置/课次只作投影，不接受通用 state.set。

`StateValue`：`value, updated_at, revision, source_refs, quality, assumptions`。quality 完整集合 `valid/estimated/stale/unknown/unavailable`；unknown/unavailable 的 value 必须 null；stale 可保留最后已知值但门控不得当新鲜值。estimated 必须提供 `EstimateAssumptions(model_version, assumptions)` 且假设数组非空；其他质量 assumptions=null。缺钱、位置或天气不能用 0/false 代替。

`StateSnapshot`：`owner_key, observed_at, revision, values`，values 是 key→StateValue 的只读映射。revision 是该 owner 本地快照版本，不是各域 revision 之和；后续内核在同库协调事务形成。固定快照用于一次决策，执行前重新检查关键资源。现有 joint snapshot 使用既有跨库协调，不将两个查询宣称为原子快照。旧读路径推进算法在 A0 不变，新模型读写分离属于 A6。

## 来源、受众与来源属性

`Provenance` 仅 `source_backed/simulated/authored/generated`。现有历史 `provenance=recorded` 表示记录证据，不可强制转成这四项。source_backed 只证明访问材料，不证明事件发生于角色。

`SourceRef` 字段：`source_id, version, content_digest, fragment_start, fragment_end, retrieved_at, collected_at, permission_ref`。version/content_digest 至少一个非空；范围为规范化来源版本文本的 Unicode 码点、零起点左闭右开，二者同时 null 表示整体引用而非授权整体读取；不适用文本的资产必须范围为空，不能猜字节偏移。retrieved_at 为获取时间；collected_at 为外部场景采集时间，未知 null。permission_ref 是宿主权限引用，读取时重新核验；旧来源 offset 需按其原单位显式转换。

`AudienceBound(permission_ref, context_keys)` 表示宿主许可范围的引用和允许 context 列表，空列表不表示公开。宿主权限服务最终核验，可见范围取所有来源与当前权限交集；不能自行用 public 标签扩大。生成产物仍保留输入 source_refs、受众和撤回关系。`FieldProvenance(field_path, provenance)` 以 JSON Pointer 指定字段覆盖；未覆盖字段继承记录级 provenance，不重复同一路径。

## 主插件能力端口（02 的精确签名）

`ports.CompanionHostPorts` 的九个方法全部 async，参数均为 frozen request，返回 frozen record：

| 方法 | 请求 → 返回 |
|---|---|
| resolve_authority | AuthorityRequest → ExecutionScope |
| get_persona_context | PersonaRequest → PersonaContext |
| read_material | MaterialRequest → AuthorizedMaterial |
| run_model_task / inspect_task | ModelTaskRequest / TaskInspection → ModelTaskReceipt |
| execute_platform_action / inspect_action | PlatformActionRequest / ActionInspection → OperationReceipt |
| publish_asset | PublishAssetRequest → AssetReceipt |
| accept_life_event | AcceptLifeEventRequest → LifeEventReceipt |

请求及返回的精确字段由 ports.py 声明。本版只定义签名，不新增运行注册。除 resolve_authority 外每次请求携带 scope，inspect 使用原 ID 和原参数摘要，不启动新任务。模型提交须给任务类型、模板版本引用、输入、source_refs、受众及 max_calls/max_input_tokens/max_output_tokens 非负预算；ModelTaskReceipt 保留 task_id、操作回执、结果引用和实际用量（未知用量 null，不是 0）。成功任务 result_ref 非空；asset 成功 asset_ref 非空。提交方法可以返回 pending/running/unknown，不能把入队当已完成。authority_ref 过期须重新授权，不能改参数重用原 ID。

所有能力在持久受理前的权限/前置校验失败，统一由后续适配器异常携带 `ContractError(reason_code,message,instance_id?,operation_id?)`；无持久任务的上下文/材料读取失败也走该通道。已经持久受理的任务、操作或事件，其后续失败通过原 ID 的类型化回执返回 reason_code/message，不另抛一个丢失原任务身份的业务异常。基础设施故障若连回执都无法查询，仍可抛携带 result_unknown 的适配器异常，调用方只能 inspect/查询原 ID。此包不实现异常或重试器。宿主未支持目标能力时调用方按 missing_dependency/provider_unavailable 停止或暂停，不能绕回旧执行器重复工作。旧 companion.life.v1 同步回调绝不返回 awaitable。

## 统一错误分类

完整枚举：`invalid_config, missing_dependency, unauthorized, stale_version, insufficient_resource, no_time_window, source_unavailable, provider_unavailable, result_unknown, cancelled`。

机器字段 reason_code 加可展示 message，日志关联 instance/operation ID，UI 不回显私密正文。业务拒绝不是异常成功，真实异常不能静默替换成空结果。无新细分码时使用已有分类与 message，不能私自扩枚举。

## A0 验收与后续责任

`tests/test_contracts_v1.py` 验证声明、完整枚举、JSON 往返、完整合法转移图、无运行导入及 Protocol 同步性隔离；不证明已有权限执行、并发控制或数据库迁移完成。后续仍需验证跨角色同名 ID 隔离、同 ID 异参拒绝、revision 冲突、单位和来源校验、unknown 不重发、唯一领域拥有者、inbox/outbox 恢复。不得将类型声明或本轮测试通过写成这些运行场景已经落地。

## v2 batch 1：本地协调与安全撤销（V2-01/03）

`v2.LifeCoordinationPortsV2` 的四个方法全部 async，每次核验原 provider lease、owner、context 与 action。它们是本地跨库协调，不是平台写入；v1 的两个 local action 保留原字典、摘要与行为。精确字段以 `v2.py` 为准：

| 方法 | frozen 请求 → frozen 返回 | 可选能力组 |
|---|---|---|
| reconcile_life_command | LifeCommandRequest(scope, command_id, request_hash, command_ref, proof_ref) → LifeCommandReceipt | life.command.reconcile |
| abort_own_prepared_command | AbortPreparedCommandRequest(scope, command_id, request_hash, command_ref) → LifeCommandReceipt | life.command.abort-own |
| start_authorized_intent | AuthorizedIntentRequest(scope, operation_id, params_digest, intent_ref) → IntentStartReceipt | life.intent.authorized |
| inspect_authorized_intent | IntentInspection(scope, operation_id, params_digest) → IntentStartReceipt | life.intent.authorized（与 start 共用） |

`LifeCommandReceipt(command_id, request_hash, decision_id?, decision, settlement, result_ref?=null, reason_code?=null, message?=null)`：decision 完整集合为 `undecided/commit/abort`，settlement 完整集合为 `pending/confirmed/unknown`。undecided 的 decision_id 必须 null；commit/abort 必须指原持久决定。confirmed 必须已有两侧核实；commit 还要原生活 resolve、宿主 confirm、生活 ACK 全部完成。undecided 不允许 confirmed。pending 表示已知尚未结算，unknown 表示结算证据不足，两者都保留屏障；unknown 的 reason_code 必须 result_unknown。commit 的 confirmed result_ref 指原生活命令结果，abort 不产生活动结果引用。决定与活动完成不能混为一个 status。

首批 `command_ref` 精确为 `life.prepared-command.v1:<command_id>`；通过现有 transition provider 的 request_for_replay 读取包括终态在内的完整原准备记录，验证原 owner/context/kind 和 request_hash。只支持 `activity.transition` 且 command_id 前缀 `activity-model-complete-`。request_hash 沿用原准备记录 SHA256，绝不换用新 params_digest。proof_ref 是宿主持久 `life.model.result.v1:<task_id>:manifest`，只有尚无决定的 commit 才读取并校验原 manifest/模型结果。它不是调用方提供的授权布尔值、SQL 或模型正文。

abort-own 无需重新读取模型正文。先查宿主原决定：无决定时在原宿主决定事务内再次检查并决定 abort；已有 abort 则恢复原 resolve；已有 commit（包括竞争获胜）只恢复原 commit/confirm/ACK，绝不调用旧 abort 去覆盖。响应丢失重复原 ID/hash，保持 decision_id；缺行/无法核验不得捏造 aborted、清行或释放屏障，返回 missing_dependency/result_unknown 或携带 ContractError 的异常。只有 abort + confirmed 后调用方才可清 command/proof，再用独立审计命令暂停。已 commit 即使来源撤回也只恢复其已授权决定，不能回滚已确认事实。

intent_ref 精确为既有 `life.reading.manual.v1:<operation_id>`；新 params_digest 使用 `authorized_intent` namespace，按共用规范去掉顶层 operation_id/params_digest，只保留 scope 的 owner/context；其余业务字段全部保留。不改变 v1 platform_action 摘要。`IntentStartReceipt(operation_id, status, start_record_ref?, evidence_ref?, reason_code?=null, message?=null)` 复用 ReceiptStatus；succeeded 仅表示原已授权意图确实开始，必须有持久 start_record_ref/evidence_ref，不表示阅读完成。stale revision 的原拒绝回 rejected/stale_version。宿主在 dispatch 前保存 owner/context/摘要绑定与开始时间锚点；只有该锚点确有准备命令时才固定它，prepare/decide/apply/complete/ACK 中断后用 request_for_replay 恢复原 start command。准备前的 pacing/校验失败未产生命令时，重试以当前 app.now() 事务性重置锚点。v2 自己决定 abort 前先持久记录原 command_id/hash 与原因；只有明确的永久授权拒绝或 revision 冲突才能在确认 abort 后持久化 rejected/unauthorized 或 rejected/stale_version。来源/提供方暂不可用、无永久原因标记、以及 v1 recover_reading sweep 的 abort，均保留队列，在确认原 abort 后以新时间锚点重试。原时钟尚未前进则保持 pending，不能重用已 abort 的命令 ID。恢复已准备但无决定的命令时，若 decide 拒绝且再次确认仍无决定，按同样规则记录原因并 abort；commit 竞争获胜时只恢复原 commit。inspect 不运行模型/不启动活动，通过原管理操作、持久拒绝结果和 life start receipt 查询，queue 删除或宿主适配器重建后仍可查询；尚有生活读取屏障时返回 unknown/result_unknown。

## v2 batch 1：执行回执与结果访问（V2-02）

`OperationReceiptV2(operation_id, kind, status, evidence_ref, receipt_revision: Revision, observed_at: datetime, reason_code?=null, message?=null)` 保留 ReceiptStatus 全集。receipt_revision 从 1 开始，只随持久执行事实/证据改变而递增；同 revision 必须同执行内容，禁止同 revision 覆盖不同事实。observed_at 为该持久版本的 aware UTC 观察时间，重复查询不能刷新它。重复同一事实返回原版本，非状态事实更新可递增版本，但终态整体执行记录不可改写；人工更正另记证据/更正记录，不重开终态。

| 当前执行状态 | 允许下一执行状态 |
|---|---|
| pending | running, succeeded, rejected, failed, cancelled, unknown |
| running | succeeded, failed, cancelled, unknown |
| unknown | pending, running, succeeded, rejected, failed, cancelled |
| succeeded / rejected / failed / cancelled | 无 |

`v2.RECEIPT_TRANSITIONS` 是完整表，无 self edge；重放不是转移。unknown 离开必须有原操作持久核实，返回 pending 额外证明原操作从未开始但已受理。rejected 仅执行前；failed/cancelled 不保证没有部分副作用。succeeded 必须有原可核验证据，unknown 必须 reason_code=result_unknown。不能将 v1 的可重验失败投影直接当作 v2 执行事实。

`ResultAccess(status, checked_at, reason_code?=null, message?=null)` 的 status 全集为 `available/unavailable/unknown`。available 的 reason_code=null；unavailable 必须给统一拒绝原因；unknown 必须 result_unknown。checked_at 为当前访问检查时间，可以随检查变化。`ModelTaskReceiptV2(task_id, receipt: OperationReceiptV2, result_ref?, usage: TaskUsage, result_access: ResultAccess, accepted_at?=null, deadline_at?=null)` 仅在执行 succeeded 且 access=available 时允许非空 result_ref/消费结果；其余情况 result_ref=null。来源撤回/权限变化只使 access=unavailable，临时查询或结果丢失为 unknown，执行 succeeded 及其 revision/observed_at 保持原样。read_material 仍每次核权。未知用量仍 null；不把访问拒绝变成可重发执行的 failed。

## v2 batch 1：受理时刻、deadline 与存在性（V2-07）

`v2.ModelTaskPortsV2.run_model_task(ModelTaskRequest) → ModelTaskReceiptV2`；`inspect_task(TaskInspection) → TaskInspectionResult`。复用 v1 请求的完整字段和业务摘要，不复用 v1 返回类型。`TaskInspectionResult(task_id, status, receipt?, reason_code?=null, message?=null)` 的 status 全集为 found/absent/unknown：found 必须带同 task_id 的原回执，reason_code=null；absent 的 receipt=null、reason_code=missing_dependency，只能在原授权和任务/调用审计均可核验，证明从未受理且没有任何未决 model_calls 时返回；unknown 的 receipt=null、reason_code=result_unknown，不能按 absent 处理。权限拒绝不泄漏他人任务；异常始终保留原 task_id。已受理的失败通过 found + 原失败回执表达。

accepted_at/deadline_at 为 aware UTC，deadline 不早于 accepted_at。v2 新受理必须同原任务持久化实际 accepted_at/deadline_at；inspect 不重设期限。旧数据无证据时可 null，不根据当前时间推测。到期只触发停止/核实，不证明任务不存在、动作失败或允许重发。v2 consumer 优先原 deadline，null 才使用旧兼容等待策略。此批冻结模型 v2 声明和参考序列化/转移测试；当前宿主**不声明 life.model-task.v2**，v2 模型持久版本/时间/两轴 adapter 与调用方切换留给配对实现，不能把声明当运行支持。

## v2 batch 1：协商与验证边界（V2-08）

架构支持版本为 `(1, 2)`；v1 使用原 async.v1、v2 使用 `companion.life-host.async.v2`。`life_async_capabilities(..., contract_version=2, requested_capabilities=(), response_types=...)` 默认无 v2 业务能力；只有显式请求且宿主已实现的组才返回方法清单，不支持的组直接拒绝。v2 facade 不暴露 v1 model/action submit。首批实现 life.command.reconcile、life.command.abort-own、life.intent.authorized；不触碰配置/生产标记。v1 调用不带新参数时行为及严格返回原样不变。`CompanionHostPortsV2` 组合本地协调与模型目标 Protocol，实际可用性仍以逐组协商为准。

所有 v2 records 仍为 frozen dataclass；JSON 延用 UTC/Enum.value/null/tuple 共用规则。v1 decoder 不接受 v2 额外字段，v2 decoder 不能为缺失的 receipt_revision/observed_at 编造值。生产 codec/持久 adapter 负责语义校验，声明包无 I/O/运行校验。模型 v2 adapter 启用前必须实现运行时 guard：按 RECEIPT_TRANSITIONS 原子校验每条转移、终态不可改写、receipt_revision 单调递增及同 revision 异内容拒绝，并完成持久化/并发验收；本批静态表与 frozen 声明不执行这些 guard。deadline 持久化、absence 证明仍需后续实现验收；本批运行测试只证明已协商的本地协调、安全 abort 与 v1 兼容恢复。四种配对、支持期、发布顺序、回退与完整 caller list 见 [迁移映射](contract-v2-migration.md)。
