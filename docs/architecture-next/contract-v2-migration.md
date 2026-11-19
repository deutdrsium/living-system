# 契约 v2 batch 1 迁移与发布配对

审批人：coordinator。批准 V2-01/02/03/07/08；V2-04/05/06 延至 A7，未经本批实现。基线 `8975e4f`，单一维护分支 `w4/contract-v2-batch1`。允许改动为 03/受影响架构文档、`good_life/contracts/`、宿主 async 协商/本地协调 adapter 及其摘要 codec、相关测试；不改阅读调用方、不改生产配置/数据库 schema、不操作服务器。规范以 [03](03-shared-contracts.md) 和 `contracts/v2.py` 为准，遵守 [变更流程](a0-contract-change-process.md)。

## 新旧映射

| v1/旧记录 | v2 batch 1 | 迁移限制 |
|---|---|---|
| execute_platform_action(life.command.commit) | reconcile_life_command(LifeCommandRequest) | 原 command_id/request_hash；command_ref 指原完整 provider 记录，proof_ref 指原 manifest；v1 adapter 原样保留 |
| execute_platform_action(life.intent.start) | start_authorized_intent + inspect_authorized_intent | 新 authorized_intent namespace；新协商记录绑定原管理 operation；不重算原管理 parameter_hash |
| 无 abort port | abort_own_prepared_command | 单独 life.command.abort-own 能力；仅 activity-model-complete-*；先查原决定，commit 只恢复 |
| OperationReceipt 六字段 | 独立 OperationReceiptV2 增 receipt_revision/observed_at | 不给 v1 wire 加字段；无原持久版本证据不能捏造 revision 或时间 |
| ModelTaskReceipt(task_id, receipt, result_ref, usage) | 独立 ModelTaskReceiptV2 + ResultAccess + accepted_at?/deadline_at? | 原执行审计与当前访问分开；本批只有声明，不广告运行模型 v2 能力 |
| inspect_task 返回 ModelTaskReceipt/异常 task_absent 属性 | TaskInspectionResult(found/absent/unknown) | v2 才用判别结果；v1 异常/投影行为不变；absence 需原授权及任务/调用审计证明 |
| strict companion.life.v1 / 旧 event wire | 继续原同步接口/字段集 | v2 不改变数据库 schema、原 consumer/inbox/outbox/ACK |

所有原 ID、幂等摘要、来源和受众保持可查。v1 的 accepted 不能视为 succeeded；completed 仍需 committed 与证据。v1 failed 若只是可重验投影，仅在原任务审计证明执行 succeeded 时，未来 adapter 才可映射为 execution succeeded + access unavailable；缺证据保留阻塞，不批量修改历史。

## 后续模型 v2 adapter 必需 guard

本批的 RECEIPT_TRANSITIONS、OperationReceiptV2/receipt_revision 是声明，尚无运行时强制。未来宿主声明 life.model-task.v2 前必须在持久事务中实施：完整转移表校验、终态执行事实不可改写、receipt_revision 正整数且随事实单调递增、同 revision 异内容拒绝；访问重验不得倒写执行 revision。还必须持久化 accepted_at/deadline_at，核验 found/absent/unknown 的审计证据，并通过并发、重放与旧回执恢复验收。

## 本地协调恢复补充

intent 时间锚点只随确实存在的 prepared command 固定；准备前 pacing 等失败没有命令时，重试事务性重置为当前时间。异步 preflight 后 prepare 前复核当前尝试，拒绝使用已被另一请求替换的锚点。v2 自己 abort 前按原 command_id/hash 持久记录原因，只有明确永久授权/版本原因才结算为同原因的 durable rejection。来源暂不可用和 v1 recovery sweep 的未标记 abort 不删除管理队列，确认原 abort 后使用新时间重试；同一时间不得重用 aborted command。恢复已准备且无决定的命令遇到拒绝也须先记原因再安全 abort，commit 竞争结果只能恢复。reconcile 对尚无决定的证据/授权/版本拒绝返回保留原命令身份的具体 ContractError，不把确定拒绝伪装成 result_unknown。

## 调用方清单

| 调用方 | 当前接口/路径 | 本批动作 |
|---|---|---|
| `plugins/astrbot_plugin_good_life/good_life/reading_execution.py` | v1 `execute_platform_action`，首次 commit/恢复 | 不改；后续 reading follow-up 使用 abort-own 时，应请求 v2、复用 stored command/request_hash，commit 竞争只恢复，不 abort |
| `plugins/astrbot_plugin_good_life/good_life/reading_scheduler.py` | v1 `life.intent.start` | 不改；后续改为 start/inspect authorized intent 配对，启动仅表示原意图开始 |
| `plugins/astrbot_plugin_good_life/good_life/reading_selection.py` | v1 model task | 不改；后续 v2 task adapter 才消费 ResultAccess/deadline/absence |
| `plugins/astrbot_plugin_private_companion/astrbot_host/life_async_capabilities.py` | v1 capability dispatch | 保留 v1；contract_version=2 分派独立 facade，默认 requested_capabilities 空 |
| `plugins/astrbot_plugin_private_companion/astrbot_host/integration_registry.py` | async capability negotiation | 只增加可选 requested_capabilities，旧调用默认空 |
| `plugins/astrbot_plugin_private_companion/astrbot_host/life_async_coordination_v2.py` | 新 local coordination adapter | 本批实现 reconcile/abort-own/intent start+inspect，依赖原 prepare/decision/resolve/confirm/ack |
| `plugins/astrbot_plugin_private_companion/companion_v2/integration/life_commands.py` | 原子 host decision/lookup | 作为 v2 adapter 的 decision authority；不改 v1 wire |
| `plugins/astrbot_plugin_private_companion/companion_v2/integration/manual_reading_queue.py` | 原已授权 intent 的启动 | 内部可选 handoff 参数供 v2 固定原 command 时间锚点；默认 v1 行为不变，原 provider 同步接口不扩展 |
| `plugins/astrbot_plugin_private_companion/companion_v2/integration/life_async_codec.py` | frozen records wire/业务摘要 | 只增加 authorized_intent namespace 的当前操作号排除规则；原 namespace 摘要不变 |
| `plugins/astrbot_plugin_good_life/good_life/prepared_commands.py` | 原准备命令/恢复记录 | 只读取原完整记录；commit 不走 abort；无 row 不捏造事实 |
| `tests/test_contracts_v1.py` | v1 declaration/codec checks | 仅断言 architecture version=2，v1 records/enums/protocol shapes保持 |
| `tests/test_contracts_v2.py` | 新 enum/record/transition/compatibility tests | 本批新增真实 JSON/UTC/null/strict v1 rejection 检查 |
| `tests/test_contracts_v2_coordination.py` | host/life integration | 本批新增 default-off、abort/reconcile race、lost response、identity/lease、intent recovery checks |

## 发布、支持期与回退

1. 先合入此契约 PR；host 双版本协商保持 v1 默认，逐组验证 v2 adapter。
2. v1 life + v1 host、v1 life + 双版本 host 均沿用原同步接口与旧 wire；不将 v2 请求发送到 v1 action 字典。
3. v2 life + 仅 v1 host 不启用本批能力；已归属 v2 的在途操作继续使用同版本恢复，不能降级重发。
4. v2 life + 双版本 host 只在双方明确声明且调用方显式请求能力组时启用；类型可导入不等于能力支持。
5. v1 新实例支持到统筹者批准停止新建；旧在途、prepared/committed 命令、unknown 操作、未确认投递清点完并通过联合回退演练前继续保留。关闭能力不丢弃 v2 在途恢复路径。
6. 回退保留 v1 readers、prepare/decision/inbox/outbox/operation 去重及 v2 新事实；不恢复旧快照抹掉决定，不重建 unknown，不批量改旧来源/状态。

V2-04/05/06（真实平台动作证据、材料/媒体/工具/资产、内核外部回执和 chain/delay）明确 deferred to A7；本批不声明相关能力、不扩大来源或受众。
