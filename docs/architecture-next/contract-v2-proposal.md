# 公共契约 v2 变更申请

**状态：BATCH 1 APPROVED。审批人：coordinator。已批准 V2-01/02/03/07/08；V2-04/05/06 明确 DEFERRED TO A7。本批准不授权合并、部署或生产配置切换。**

正式 batch 1 契约见 [03](03-shared-contracts.md) 与 `good_life/contracts/v2.py`，迁移/调用方/配对发布见 [迁移映射](contract-v2-migration.md)。以下 CONFIRMED/INFERRED 保留原申请基线证据；批准项的候选签名与措辞以正式冻结契约为准，不能把 04-06 的 INFERRED 视为已批准。

基线：`origin/main@0628be7877800ac6f69867448fed2d74e9596ad0`，申请分支 `w4/a7-audit`。这是原申请时的 v1 frozen 证据基线；本次批准后由 `w4/contract-v2-batch1` 从 `main@8975e4f` 按 [a0 契约流程](a0-contract-change-process.md) 串行更新 03、types/Protocol、迁移映射、受影响文档及测试，旧 wire/数据库/生产配置保持原样。

**CONFIRMED** = 本基线代码证据；**INFERRED** = 建议设计/尚待实现验证的判断。以下所有“拟新增”“应”“建议”均属于 INFERRED，不能被并行任务当成已批准接口。缩写 GL/HOST 与 [浏览审计](a7-browsing-audit.md) 相同，路径/行号固定在上述基线。

## 申请摘要与待决策项

| 编号 | 用户需决定（INFERRED，建议方案） | 影响 |
|---|---|---|
| V2-01 | 将 `life.command.commit` / `life.intent.start` 从 platform action 分类迁入独立的本地协调端口；不新增浏览专属宿主方法 | A3 调用方、宿主命令/intent、A7 完成提交 |
| V2-02 | 回执描述持久执行事实；结果当前是否可读另设 validity，并规定终态不可反转 | A3/宿主重验、A7 unknown 和结果消费 |
| V2-03 | 增加“只撤销自己未决准备命令”的最小端口，先检查宿主原决定；已 commit 只能恢复，绝不强制 abort | A3 无法退出的 prepared command、A7 同类提交故障 |
| V2-04 | 真实平台动作保持原 ID/旧摘要；新动作采用完整业务摘要和可查询、版本化的回执证据，无远端核实器时继续 unknown | 宿主平台/审计、生活编排、在途回退 |
| V2-05 | 明确外部材料获取/版本/图像覆盖、有限模型工具交换与受控资产能力；当前阅读窄能力不自动扩大授权 | 生活浏览选择、宿主来源/模型/资产 |
| V2-06 | 内核先串行接外部回执/资产和 chain/delay；包裹链由生活内部事件消费者实现 | 内核、存储、领域唯一拥有者 |
| V2-07 | 记录宿主受理时间/deadline 与显式 absent；预算用尽不等于任务不存在/动作可重发 | A3 查询恢复、A7 阶段等待 |
| V2-08 | 新协议与 v1 双版本并存，旧在途支持到排空并通过回退演练，不按日历强制淘汰 | 两插件配对发布、全部 v1 消费者 |

**INFERRED**：建议整体以架构契约整数 v2 审批；每项都有独立能力开关。具体请求/枚举变化必须同步更新 03、types/Protocol、codec 和测试，不以“只是新增字段”继续宣称 v1。若用户只批准其中一部分，契约维护分支需重新冻结该子集及 A7 依赖，不能推定其余已批准。

## V2-01 本地协调动作分类

**CONFIRMED**：`HOST/astrbot_host/life_async_capabilities.py:298` 的 execute_platform_action 仅接受 `life.command.commit` 与 `life.intent.start`，`:317` 明示分类延至 v2；`:412` 注册信息也只列这两个 local_actions。它们没有平台账号写调用。

**CONFIRMED**：commit 走 `ModelCompletionHandoff.recover`（`:334`），核验持久 model asset、manifest、来源、record revision/output bindings（`HOST/companion_v2/integration/model_completion_handoff.py:32`），再复用跨库 prepare/decide/resolve/confirm/ack。调用方是 `GL/reading_execution.py:471` _commit。intent 走 `HOST/companion_v2/integration/manual_reading_queue.py:69` start，查宿主已授权管理请求/开始回执，只授权/开始，不运行模型；调用方是 `GL/reading_scheduler.py:135` _manual。

**INFERRED，候选签名 diff**（名称待审批，精确类型在正式契约 PR 冻结）：

```text
旧 execute_platform_action(PlatformActionRequest[action=life.command.commit|life.intent.start])
  → OperationReceipt
新 reconcile_life_command(LifeCommandRequest) → LifeCommandReceipt
新 start_authorized_intent(AuthorizedIntentRequest) → IntentStartReceipt
新 inspect_authorized_intent(IntentInspection) → IntentStartReceipt
execute_platform_action / inspect_action 只用于已协商的真实平台动作

LifeCommandRequest = scope, command_id, request_hash, command_ref, proof_ref
LifeCommandReceipt = command_id, request_hash, decision_id?, decision,
                     settlement, result_ref?, reason_code?, message?
decision = undecided | commit | abort
settlement = pending | confirmed | unknown
AuthorizedIntentRequest = scope, operation_id, params_digest, intent_ref
IntentStartReceipt = operation_id, status, start_record_ref?, evidence_ref?, reason_code?, message?
IntentInspection = scope, operation_id, params_digest
```

**INFERRED**：本地 command 的 decision 与 settlement 分开：commit 已决定但生活尚未确认时不能回“活动完成”，abort 也不是平台动作 cancelled。引用必须能解析到原持久请求/证明；不能凭 command_id 任意构造效果。start 成功仅表示原意图已授权并开始/确认原开始，不表示消费内容完成；stale revision 返回原意图的持久拒绝结果，重试不新起 activity。新 inspection 必须从持久结果读取，不能要求仍存在已删除的 queue 行。

**CONFIRMED**：当前 intent start 成功删除 queue 行（`manual_reading_queue.py:120`）；拒绝会完成管理 Operation 并保存 rejected 结果（`:94`），generic inspect_action 却只看本地 identity 和 Operation completed/committed（`life_async_capabilities.py:353`），不表达 intent 的业务结果。且 identity 在 start/handoff 完成后才 remember（`:328`、`:348`），响应丢失时不能假定 generic inspect 总能找回。

**INFERRED，兼容/配对**：v1 调用方继续使用原两 action 与原参数/摘要，宿主保留其 adapter；不将 v2 request 发送到 v1 action 字典。新端口与 A3 _commit/_manual 的迁移同时配对验证，host 先支持、life 后选择 v2；A7 依赖同一通用协调能力，不新增 browse_complete 这种宿主业务端口。旧记录保留原命令/operation/证明，v2 引用适配只包住原身份，不重算。

## V2-02 执行事实、结果可用性及回执单调性

**CONFIRMED**：03 定义 failed 为已知失败，但未冻结 ReceiptStatus 完整转移表。`life_async_capabilities.py:31` wrapper 会在 inspect 重验发生部分永久错误时调用 inspect(result_error=...)；`:255` 只对 settled succeeded 使用该错误，`:259` 将返回视图改成 failed，不改 execution_tasks 审计。`:269`/`:274` 结果缺失/被抑制可能改成 unknown。因 source/authority 恢复可重新得到同一审计任务的 succeeded 视图；这里 authority 相关错误若映射 unauthorized 则仍抛 HostPortError，不会一概返回 failed。证据见 `tests/test_reading_host_recheck.py:72`、`:96`、`:117`、`:156` 和 14 中已记录的非单调现象。

**CONFIRMED**：A3 `GL/reading_execution.py:389` 及 `reading_selection.py:59` 将 failed/rejected/cancelled 视为停止消费；所以同一字段同时表示“任务实际失败”和“当前结果不可用”会影响重试/终结决策。原平台写结果未知尤其不能被材料失效改成可重发的 failed。

**INFERRED，候选 diff**：

```text
OperationReceiptV2 = 原身份/种类/status/evidence_ref/reason_code/message
                     + receipt_revision: positive int
                     + observed_at: aware UTC datetime
ModelTaskReceiptV2 = task_id, receipt: OperationReceiptV2, result_ref?, usage
                     + result_access: ResultAccess
ResultAccess = status, checked_at, reason_code?, message?
status = available | unavailable | unknown
```

**INFERRED**：receipt.status 专指原执行事实；settled succeeded 保持 succeeded，材料撤回时 result_access=unavailable、result_ref=null，缺失/临时查询故障则 unknown。底层确实失败仍 failed。只有 available 才允许消费结果，而且 read_material 每次继续核权限；result_ref 是引用，不是永久读取许可。unknown/unavailable 不填可见正文；result_access 不作为平台成功证据。所有嵌套 reason 使用统一 ReasonCode，available 时 reason=null；unknown 为 result_unknown，unavailable 必须提供拒绝原因。

**INFERRED，拟冻结转移表**：pending→running/succeeded/rejected/failed/cancelled/unknown；running→succeeded/failed/cancelled/unknown（rejected 仅执行前）；unknown→pending/running/succeeded/rejected/failed/cancelled 仅凭原操作持久核实，pending 只适用于证明原操作未开始但仍已受理；succeeded/rejected/failed/cancelled 不再转移。revision 只随持久回执事实变化递增，同 revision 异内容拒绝；当前访问检查只改变 ResultAccess 的 checked_at/status，不倒写执行 revision。人工更正另记证据/更正记录，不重开终态。failed/cancelled 仍不保证没有部分副作用。

**INFERRED，兼容/配对**：v1 仍返回其既有投影视图，不能承诺它已单调；v2 Consumer 才使用新两轴语义，旧 Consumer 不接收新增字段或枚举。host v2 adapter 与 life 结果处理器/持久 codec 必须配对测试。旧 failed 只有证实其为可重验投影且有原审计时才适配成 execution succeeded + unavailable；无证据保留旧状态/阻塞，禁止批量改旧事实。

## V2-03 最小“abort own prepared command”能力

**CONFIRMED**：v1 LifeAsyncPorts 没有显式 abort/reconcile-abort 方法，只暴露上述两种本地动作。`ModelCompletionHandoff` 继承 `NaturalWakeHandoff.abort`（`HOST/companion_v2/integration/natural_wake_handoff.py:196`），但未通过 async port 暴露。旧 `recover_reading` 只接受 `activity-reading-start-*` / `activity-reading-complete-*`，拒绝 A3 的 `activity-model-complete-*`（`HOST/astrbot_host/life_host_capabilities.py:550`）。

**CONFIRMED**：`GL/reading_execution.py:471` 在首次 commit 确认前反复准备并调用本地 commit；捕获 ValueError 时只有生活准备行已是 aborted 才能去 pausing（`:482`）。当前有界 `MAX_RESULT_CHECKS` 的 event 重试预算在 commit_decision 之后（`:509`），不能把它写成“首次提交已有完整有界退出”。`GL/prepared_commands.py:90` 唯一索引与 `:127` 检查禁止同 owner 在已有 prepared/committed 时准备另一命令；`GL/reading_scheduler.py:112` 保留 command/proof，直到确认 abort。

**INFERRED**：首次 commit 持续失败、或未来为首次提交增加重试上限后，life 不能通过公开端口明确终结自己那条未决准备命令。直接调用 pause_interrupted_work 仍要准备同 owner 的新转移，可能被原 prepared 行挡住；无确定原决定不能安全释放前台。因此仅“耗尽后暂停”缺少可调用的协议步骤。浏览采用同一 prepare/commit 模式也需要此能力；它只终结本地提交提案，绝不撤销/重发已经发生的真实平台操作。

**INFERRED，最小签名 diff**：

```text
+ abort_own_prepared_command(AbortPreparedCommandRequest) → LifeCommandReceipt
AbortPreparedCommandRequest = scope, command_id, request_hash, command_ref
```

**INFERRED，语义与幂等要求**：

1. 核验原 lease/owner/context、provider 里完整准备记录及 request_hash；只允许调用者的原持久命令和已协商命令种类/前缀（首批 activity-model-complete-*，A7 加入经批准的完成命令）。command_ref 解析原请求，不接收任意 SQL 或可信授权布尔值。此能力是受限安全恢复权限，不依赖再次读取已撤回的模型正文；新 scope 可由原身份重新授权，但不能沿用过期 lease。无法核验身份时继续阻塞，交显式人工恢复。
2. 同 command_id + request_hash 返回原决定/结算回执；不同摘要拒绝 invalid_config，跨 owner/context 拒绝 unauthorized。同 ID 重试不得插入另一条 command、operation 或更换 decision_id。
3. **宿主无决定**：在宿主决定事务里原子检查并写入 abort 决定，沿现有 verify_prepared/decide(outcome=abort)/provider.resolve 协议使生活行 aborted。只在两侧确认后回 decision=abort, settlement=confirmed；随后 life 才可清原 command/proof，并用独立受审计命令暂停/释放前台。超时不表示 abort 已完成，返回/抛 result_unknown，继续查同 command。
4. **宿主已决定 abort**：幂等完成原 abort 的生活 resolve，返回同一决定；响应丢失后的重试不再创建新事实。
5. **宿主已决定 commit，或与 commit 竞争时 commit 获胜**：绝不覆盖为 abort；返回原 decision=commit，沿原命令恢复落库/确认/ACK。无法完成时 settlement=pending/unknown，保持读取屏障；已确认 commit 不因来源/下游错误回滚。不能原样调用旧 abort 来假定其会替我们处理 commit 竞争，适配器必须识别原决定并分支。
6. **缺行/已终态/存储不可用**：已终态仅在原身份/摘要可证时返回原结果；缺行不能捏造 aborted，也不能凭超时推断无宿主决定。保留证据并显式 missing_dependency/result_unknown；不删除准备行来“解锁”。

**INFERRED，兼容/发布配对**：新增 v2 可选 `life.command.abort-own` 能力，v1 枚举/签名保持原样；旧 Consumer 继续工作，但仍有原恢复缺口，不能宣传已获该修复。最小修复必须配对发布 host abort/reconcile adapter + life 首次 commit 重试终结与原命令回执消费逻辑；缺能力时停止重试/诊断等待，不自动走 host_bridge、不清行。A7 启用同样要求该能力。测试需覆盖首次提交一直失败、abort 响应丢失、commit/abort 竞争、host 已 commit/life 未提交、跨角色/异摘要拒绝、abort 后新 pause 命令真正成功，并断言模型/平台调用数不增加。

## V2-04 真实平台动作及 unknown 证据

**CONFIRMED**：现行两条写路径在 `HOST/companion_v2/role_work/social.py:475` / `:724` 预留同一 `social-action-` ID，旧摘要仅 operation/target；`:480` / `:731` replayed 即跳过。ok=False→unknown，网络异常也可能留下 accepted。底层 `social_port.py:698` 不接收 operation_id，`:807` 仅 google_search 重试。`life_async_capabilities.py:353` 的 inspect_action 不是 social-action 的公开查询器。

**INFERRED，候选 diff/约束**：PlatformActionRequest 保留稳定 operation_id、scope、action/payload、source_refs、audience_bound；v2 明确 action registry 的参数 schema、授权用途及预算，每项能力分别协商。返回 OperationReceiptV2，evidence_ref 指宿主持久 ActionEvidence：原请求摘要、平台/规范目标、调用是否已开始、观察时间、服务端回执引用（没有则 null）、结果/部分副作用及关联材料版本；临时 token/密钥不成为可见证据正文。inspect 只查询原操作/可信平台核实，不调用动作；没有核实能力继续 unknown，原 accepted 也不能自动重发。

**INFERRED**：对新动作使用 03 的完整规范业务摘要，覆盖 reason 等可改变动作含义的字段；可轮换凭证应改成稳定的宿主许可引用，摘要不含秘密值。旧 digest([operation,target]) 与旧 ID 永久保留其命名空间/审计关联，新 adapter 必须先检查旧记录，不能用新摘要/新 ID 绕过旧 unknown。同 context 跨 activity 的同目标去重策略保持，不能借模板升级重新互动；跨角色/上下文隔离须核验 owner 与原 scope，不能只看字符串 ID。

**INFERRED，兼容/配对**：v1 平台 Consumer 没有这项窄宿主实现，不会因 v2 出现而突然获得平台写权限；继续原 host_bridge。host registry/平台核验/证据持久化先完成，life A7 checkpoint/inspect 后接；旧动作映射 reader 与回退工具同批验证。禁止把旧 accepted 映射 succeeded；completed 还要验证 committed 和可信成功证据，证据不足只供历史读取/阻塞查询。

## V2-05 材料获取、多轮模型及资产

**CONFIRMED**：`life_async_capabilities.py:21` ALLOWED_ACTIONS 只有 role.reading.record，`:22` 模型只 reading.consume/choose；read_material 接受已有 SourceRef，不负责通用外部推荐/搜索获取。当前 AuthorizedMaterial.content 是 str（`GL/contracts/ports.py:50`）。实际 social 工具返回文本/卡片/图像，业务循环位于 `social.py:517`。图像有来源 SHA256/受众（`:871`），正文/最终 checkpoint 没有逐条固定来源版本。publish_asset 在 Protocol 声明但未列入运行 CAPABILITIES（`life_async_capabilities.py:19`）。这是能力缺口，不能当作已有通用实现。

**INFERRED，候选 diff**：新增 async acquire_material(MaterialAcquisitionRequest) / inspect_material_acquisition(原 ID/摘要) → MaterialAcquisitionReceipt，请求包含 scope、稳定 operation_id/params_digest、registered_provider、规范查询/目标、purpose、audience_bound、limit/budget；返回原操作回执与受控 material_refs。已有 read_material 保留文本读取，另协商 read_asset 给媒体 descriptor/引用，避免把 base64/临时路径塞入 content。每份材料固定 SourceRef.version/content_digest、retrieved_at/collected_at、片段与权限引用；媒体范围 null，未知采集时间 null，不拿 fetched_at 当平台内容版本。

**INFERRED**：模型多轮沿通用 run_model_task/inspect_task，新增版本化 task input/result schema 描述受限工具目录、turn/checkpoint 和 tool call/result 引用；工具调用是待核验请求，不直接执行外部副作用。宿主保留网关、预算、注册工具约束及审计，生活负责浏览选择/顺序/解析。正式适配必须保留原 parent task ID，历史子任务 ID 读取原审计；新子任务首次分配后持久化。不能用多个全新 task ID 重置同一会话预算/未知结果。若现有任务监督器不能实现暂停/恢复多轮，先串行扩展通用任务 adapter，再启用 A7。

**INFERRED**：publish_asset 按声明做独立能力协商；成功返回非空资产引用并保存 source dependency/受众/撤回关系，生活持有当次 impression 的业务关系，宿主托管内容。产物/反思、实际工具结果、曝光只证明其覆盖的经历；图像转述不等于看完视频，想看不等于看过，share_candidate 不等于 send。兴趣上下文/反馈走宿主拥有者，禁止移植直接 SQL 兴趣更新。

**INFERRED，兼容/配对**：v1 文本消费者继续旧 AuthorizedMaterial，不发送媒体顶层字段/新 task schema。host 新获取/媒体/工具/资产能力逐项声明，不支持则暂停/桥接新实例，已归 life 实例不回旧执行器；life A7 输入/来源/产物引用与宿主依赖图同批集成。历史仅 URL/文本时不伪造 SourceRef，把旧经历作为旧记录保存；新执行要求真实材料证据。

## V2-06 内核外部结果、chain/delay 与事件 wire

**CONFIRMED**：`GL/kernel/registry.py:58` 限制 local_only=True，`:78` 只接受 intent trigger，`:92` 拒绝 inputs/model_policy；`GL/kernel/engine.py:394` 只接 simulated 且无 source_refs 的本地事件，`:484` 拒绝外部 receipts/artifacts/share_candidates。03 的完整 ActivityResult/EventEnvelope 是目标声明，不能视为运行支持。`GL/kernel/repository.py:134` 已有候选去重/原因合并，但 `engine.py:242` 新实例 causal_event_id 当前设 None。

**INFERRED，候选 diff**：不放宽同步 executor 回调去 await 网络。增加串行设计的宿主任务/动作恢复 adapter，将持久阶段、OperationReceiptV2、受控 artifact_refs 纳入内核提交验证；回执只验证所指操作，不自动证明整个实例完成。模板 capabilities 明示 external adapter 可用版本，未支持时 unavailable。原 local-only executor 保持原协议及行为。

**INFERRED**：chain 在生活内部消费已提交 EventEnvelope，新增持久消费键 `(event_id, consumer_id, trigger_id)` 和延迟预约字段 `causal_event_id, target_ref, due_at, revocation_key, status`；消费回执与唯一候选/延迟任务同库原子提交，重投不重新抽样。候选传递真实 causal_event_id，过期/撤销/目标 revision 变化不产生新副作用；仅保留接收端订阅。parcel.arrived 必须是到货事实，不能从购物完成直接捏造。

**CONFIRMED**：`HOST/companion_v2/integration/life_event_inbox.py:33` 目前只接受 activity.completed，strict legacy wire/消费者集合也有独立校验。`GL/storage.py:107` record_event 已要求在事实事务内写 outbox，按 event_id/content_hash 去重。

**INFERRED，兼容/配对**：内部包裹 chain 不借旧宿主 inbox 接受通用事件；v1 activity.completed wire 原样保留，目标 envelope 跨插件使用新 event 协议能力。不能给旧 strict wire 加 causation/provenance/字段或把 occurred_at 冒作 recorded_at。内核、触发消费/迁移、领域模板与因果验收配对；宿主新 inbox adapter 另按能力上线。新 Consumer 支持新 envelope，旧 Consumer 继续原 wire，无法证实的历史字段不转换。

## V2-07 deadline、absence 与受理后错误

**CONFIRMED**：`GL/contracts/ports.py` 请求/回执没有 deadline/timeout、查询无 absent 类型。A3 使用独立 480 秒 fallback（`reading_execution.py:27`）；宿主 HostPortError 通过 `task_absent` 附加属性表达明确不存在（`life_async_capabilities.py:25`、`:277`）。权限/provider 错误可能仍走异常而非已受理回执（`:46` 的特殊 inspect 回执分支排除部分原因），所以调用方不能把“抛异常”一概当未受理。

**INFERRED，候选 diff**：ModelTaskReceiptV2 增加 `accepted_at?, deadline_at?`，新 TaskInspectionResult 使用 `found | absent | unknown` 判别；found 带原回执，absent 仅在授权和审计均可检查且确认从未受理、无未决 model_calls 时返回。unknown 为查询证据不足，不是 absent；权限拒绝不泄漏结果，但保留调用方原任务身份。deadlines 有值时为 aware UTC 且不早于 accepted_at，旧无法证明时 null；v2 新受理必须持久化并返回实际 deadline，不能每次 inspect 刷新。

**INFERRED**：保持原 admission/执行预算和节奏；deadline 到达只触发停止/核实，不证明平台失败或原 task 不存在。v1 Consumer 继续 fallback，v2 Consumer 优先使用宿主 deadline，null 才用兼容策略；host receipt/codec 与 life scheduler 同批配对。新增 absent/字段必须升契约版本，不能仅在 Python exception 上再加未声明属性冒充规范。

## V2-08 发布、旧数据及在途恢复策略

本节全部为 **INFERRED** 的拟定兼容方案。

| 配对 | 允许的行为 | 必须拒绝/保留 |
|---|---|---|
| v1 life + v1 host | 保持当前桥接/A3 与原严格 wire | 不声称获得 abort-own、平台 action 或单调回执 |
| v1 life + 双版本 host | 显式协商仍得到完整 v1 adapter；本地两 action 原样支持 | 不向旧 response_types 注入新字段，不改变同步 companion.life.v1 |
| v2 life + 仅 v1 host | 可用独立 v1 兼容阅读路径；新 A7 实例保持 unavailable/host_bridge | 已归 life 的 v2 在途任务等待同版本恢复，不降级重发 |
| v2 life + 双版本 host | 新实例只使用双方声明的 v2 能力；实例/任务固定契约、执行器、模板和来源版本 | 不把类型可导入当运行能力已支持 |
| 回退 | 保留双版本 reader/旧执行器、prepare/decision/inbox/outbox/operation 去重、新增事实和未决屏障 | 不恢复旧快照抹掉新增事实，不重建 unknown 操作，不全库升级历史来源 |

支持期限建议为：v1 新实例支持到用户明确批准停止新建；v1 旧在途支持至少到所有旧任务、准备命令、unknown 操作和未确认投递清点完毕并通过联合回退演练。unknown 未核实就继续保留只读/查询与人工恢复路径，不用截止日期强行丢弃。v2 在途即使功能关闭也要保留同版本安全恢复能力。

发布序列：先审批 → 单一契约分支提交 03/types/Protocol/codec/兼容测试和调用方清单 → host 双版本能力与安全恢复/内核 adapter 串行验证 → life A3 v2 Consumer 与 abort 首次提交退出逻辑配对 → A7 业务编排和内部链式活动分开集成 → 联合故障/回退验收 → 用户另行授权启用。架构契约升到 2 不自动把原数据库 schema、companion.life.v1 或事件 wire 升号。

## 正式契约 PR 应包含的验证与评审

**INFERRED**：契约维护者须补精确 frozen request/response/Enum、完整转移表、JSON/UTC/null 规则、所有新摘要 namespace 与 schema。无 I/O 类型声明保持纯净；同 ID 异参、跨角色同名 ID、过期 lease、新旧 codec 互读拒绝行为都要测试。

**INFERRED**：宿主负责人评审权限/来源/平台动作/事件 wire/abort-own；内核与存储负责人评审外部结果、命令屏障与 chain 事务；兴趣和日记拥有者评审候选消费及撤回。建议验证 19 的 B04/B05/B06、T01、L03/L04、P01/P02、R01/R03/R04/R05。特别检查以下序列：

- execution succeeded → 来源撤回 → 来源恢复：执行回执不反转，访问状态可变化，模型/平台调用计数不增加。
- 原 command first commit 持续失败 → abort-own → 同 owner pause 成功；与 host commit 竞争时只恢复原 commit。
- social-action reserve 后中断、call 后断网、结果落库前崩溃、换 owner/lease/执行器、查询耗尽：同 ID 持续查询，未知写不再次发送。
- 重投 parcel.arrived、重启延迟队列、撤销目标 revision：一个有效取件候选，未到货不写取件成功。
- v1/v2 四种发布组合、关功能后的在途恢复和带新增数据回退：不扩大受众、不丢去重/审计、不重跑原活动。

原申请 PR 仅提交文档；当前 batch 1 契约 PR 已实现批准子集的声明、可选本地协调 adapter 和兼容/恢复测试，运行边界见 [03](03-shared-contracts.md)。V2-04/05/06 继续保留申请证据并延至 A7，不因本批批准或合入变成实现依赖。
