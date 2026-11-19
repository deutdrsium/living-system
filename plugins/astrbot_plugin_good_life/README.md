# 我会去好好生活

AstrBot 生活系统插件，负责角色的日程、阅读、浏览、作息和生活状态，依赖「我会永远陪着你」。

安装后在 AstrBot 的插件页面管理日程、阅读、浏览、作息和生活状态；原插件继续负责聊天、日记和梦境。

安装、日常使用和排查请看仓库中的[使用教程](../../docs/user-guide/使用教程.md)。

架构、接口及连接方式见 [技术说明](docs/architecture.md)。代码按 AGPL-3.0-only 授权，迁出来源为「我会永远陪着你」。

## A3 阅读执行器（默认关闭）

`reading_executor_enabled` 默认为 `false`。普通安装继续由 `execute_reading → RoleReader` 执行；不会改写生产执行归属或 cutover 标记。设为 `true` 后，配套宿主必须独立协商 `companion.life-host.async.v1`、契约版本 1 及所需能力，缺失时停止绑定，不把在途实例交给另一执行器。同步 `companion.life.v1` 和原混合能力字典的签名、参数与回执保持原样。

`reading_abort_own_enabled` 也默认为 `false`，只在启用后单独请求契约 v2 的 `life.command.abort-own` 与 `life.command.reconcile`，原阅读/模型/事件端口仍使用独立协商的 v1。宿主不提供两项能力时继续停驻，不释放原屏障。提交预算耗尽后保留原 command、proof 与 request hash；先请求中止原命令，宿主已有 commit 时只恢复原决定，本地已提交时直接 reconcile。仅 `abort + confirmed` 且本地中止证据匹配才执行独立暂停、释放前台。pending/unknown 按持久指数退避重查（30 秒起，最高 900 秒）。v2 在途标记在调用前保存；关闭开关或退回仅 v1 宿主后不降级重发，未决操作等待重新启用；已确认中止或已核验 ACK 可继续收尾。停驻日志只记录一次，provider_unavailable 也使用持久指数退避，最高 900 秒。

新路径由生活侧组织选书、原文与前次笔记、阅读提示、结果解析、当次感想、进度与下一片段。宿主提供受限人格上下文、授权材料及版本/受众、通用模型提交/查询、已授权命令的审计提交和持久事件收件。手动阅读也使用同一个生活执行器；原管理意图的授权、启动回执和去重通过兼容适配器保留。新端口暂定协商本地 `life.command.commit`、`life.intent.start` 动作，不授权外部平台操作；其在平台动作端口中的正式归类留待契约 v2 决定。

每个实例持久固定 `life/host_bridge`，关闭开关后仍按原路径恢复已开始的实例。原 `reading-task-*`、`reading-choice-*`、`reading-complete-*` 及完成事件 ID 保留。模型结果使用宿主的原监督器、模型档案、预算、调用审计与来源约束；`unknown`、已有旧桥任务及在途任务都只查询原任务。确证任务不存在才允许提交同一 ID；不会因执行器切换而重新发送。模型结果已持久保存时，重启只补原结果的跨库提交，并保留原完成时间。若只中断了监督器结算，必须同时持有原模型生成记录、已完成的结果操作回执和工作者已停止证明，才通过宿主既有 `reconcile` 补结算；缺少任一证据则继续保留未知状态。消费者失败不撤销已完成阅读，也不触发模型重读。

A3 选择复用现有活动生命周期和 `PreparedCommands` 的准备、宿主决定、生活提交、宿主确认与 ACK 屏障，未把生产记录转换成 A1 实例，也未修改 `contracts/` 或 `kernel/`。A1 的公开 codec 用于请求不可变快照和目标 wire 校验。目标参数摘要按 03 的 namespace/规范 JSON 规则计算，原 ID 与账本摘要继续使用旧算法。显式协商时传入契约返回值构造器，避免宿主强行导入可选生活插件；应用、routine、数据库及网络客户端均不跨端口暴露。

新增持久数据只使用 A3 自己的 `a3.reading.schema.version=1` 与 `a3.reading.execution.v1:*` 元数据。它们保存实例归属、阶段、模板/人格版本、原任务/材料/产物引用及在途提交命令；成功后清除检查点中的感想正文，唯一业务笔记仍是原生活活动的 `reading` 回执。宿主持有通用模型原始响应证据，读取时重新检查全部来源，不能借结果缓存绕过撤回。原生活表、版本项、进度单位、速度与队列标记不做转换。

本包保留原阅读节奏：片段消费后记录原进度，下一次启动继续受原速度和睡眠/日程门控约束；没有新增“模型返回即补造更长阅读时间”的逻辑。原来源与身份检查覆盖读取、模型提交/执行、落稿和事件收件。已有笔记对外展示继续走宿主原来源可见性规则。

验证入口为根目录 `tests/test_reading_extraction.py`，覆盖自动/手动阅读、原 ID 和进度、来源撤回、五段提交恢复、租约重绑、开关切换、unknown 不重发、事件去重和契约摘要。真实生产启用、线上页面和真实模型/外部平台不是这些本地替身测试的证明。未实现浏览抽取、A1 阅读实例转换及 `publish_asset`；端口只声明实际实现的能力。通用本地提交动作的正式契约归类仍需单独审议，保持默认关闭。

## A1 活动内核（默认关闭）

`good_life/kernel/` 实现冻结契约 v1 的本地规则闭环；`good_life/activities/local_order.py` 提供“整理书桌（规则模拟）”组件。它没有外部副作用、不调用模型，也不改变精力/情绪、钱包、位置或学业。新实例只写独立内核表，绝不把旧 `planned/active` 活动导入或接管。

当前交付是**显式嵌入入口**，不是生产切换或新增页面开关。普通 `LifeRuntime(storage, plugin)`、AstrBot 的现有入口和所有旧调度器保持原样；`KernelConfig.enabled` 默认 `False`，关闭时不迁移内核表、不生成实例、不调度。没有新增常驻计时器，也不向 `companion.life.v1` 注册新处理器。

受控嵌入方式（`access` 必须由可信宿主适配器提供，不可用模板或请求参数自报身份/权限）：

```python
from good_life.kernel import KernelConfig
from good_life.runtime import LifeRuntime

runtime = LifeRuntime(storage, plugin,
    kernel_config=KernelConfig(enabled=True), kernel_access=access)
# runtime.kernel.propose(scope, template_ref, opportunity_key=..., reason=...,
#                        now=aware_utc, expires_at=aware_utc_end)
runtime.advance_kernel(now=aware_utc)
```

`KernelAccess` 必须提供同步、本地、只读的 `scopes/authorize/snapshot/audience`；每次命令重新核验 owner/context、权限与注册代次，快照必须包含旧前台活动和硬承诺。**目前真实宿主尚没有冻结目标范围/受众适配器，故普通安装不会启用此入口；不能伪造该能力或直接接入生产旧循环。**未来接入还须统一新旧前台占用的可见性与互斥，不能仅凭一次 busy 快照声称已经互斥。

调用任何内核命令前，调用方必须释放宿主锁，禁止在持有宿主锁时进入生活库锁。协调器 lifetime 栅栏仅覆盖进程内共享的 `LifeStorage`，不提供跨进程领导权或互斥；当前生产嵌入前提是单进程、单共享存储实例。

实现内容：

- 模块唯一 `owns`、执行器/模板固定版本注册；模板可复用执行器。首版只接受显式稳定 `intent` 机会，按 owner/模板版本/目标版本/机会去重并合并触发原因。未实现的触发器和输入提供方明确拒绝。
- 候选规划廉价门控在 prepare 前执行；开始前按固定准备结果、最新快照/授权、完整时长、资源、机会过期时间再门控。拒绝原因和两次快照版本持久保存，未知资源不当作满足。
- 候选消费与 `preparing` 实例创建同事务；同 owner 的 preparing/running/unknown 有唯一前台约束，paused 释放槽位。转移使用冻结合法表，命令回放返回原回执，同 ID 异参和旧 revision 拒绝。
- 在线执行时长使用单调时钟；UTC 保存恢复锚点。重启重新门控并从已知 checkpoint 接续，不把离线时间补造为整理工作，不重新 prepare/抽样。暂停等待不计入实际时长。
- 运行阶段效果同 checkpoint 原子提交；终结先保存 `ActivityResult` 提案，再由唯一领域拥有者核验、同事务提交实例/效果/事件。中断后重放原提案落库，不重跑执行器。取消保留实际完成单位和耗时；重启后可直接按已知进度取消，不计离线时间。
- 重放 start 回执不会新建或更新单调时钟锚点；锚点必须匹配当前 revision，恢复仍重新门控。无进度、checkpoint 不变且无效果/事件/结果的 tick 不新增 revision 或命令回执。`ExecutorStep.completed_units` 显式提供领域单位，内核不解析领域 checkpoint 键。
- 待结算提案被确定性拒绝时，授权管理调用方可用 `abandon_result(scope, result_id, expected_revision=..., command_id=..., reason=..., now=...)` 将本地实例转为 failed/invalid_config，保留原提案、已知进度和既有领域事实，释放前台槽，不提交被放弃的效果。此入口使用独立 `kernel.abandon_result` 权限检查，支持关闭新活动后的安全处置；不能解除 unknown 或放弃含外部 operation/receipt 的实例。
- 事件复用 `LifeStorage.record_event`、全局唯一事件 ID、现有投递去重与 `LifeEventDelivery` 的回执核验 ACK；目标 `EventEnvelope` 另存扩展表，**不扩充严格旧 wire**。新事件只有 `kernel.audit.v1` 本地诊断用途，暂无真实宿主消费者，不自动 ACK 或发消息。
- unknown 保留原 operation ID 并阻塞；首版没有外部执行器或可核验的原操作查询适配器，故不自动离开 unknown，也不以 cancel/超时/重启解除它。已决定的本地终结提案可在关闭新活动后，由带可信适配器的 `ActivityKernel.recover_pending` 显式安全落库；不会因此创建/启动新实例。

迁移仅在显式启用时运行：新增 8 张 `life_kernel_*` 表、2 个索引、1 张独立领域表 `life_local_order_units`，以及 `kernel_schema_version=1`、`local_order_schema_version=1` 两个元数据项。基础 `good-life.storage.v1` 版本、旧表/字段/记录及执行归属标记均不改写。重复迁移校验 schema；未知版本、未认领或不完整表拒绝启动。生产启用仍需独立备份、切换授权和恢复演练，本交付不执行这些操作。

验证入口：根目录 `tests/test_kernel_*.py` 与 `tests/test_contracts_v1.py`。覆盖正常完成、取消、重启、结果提案/领域事务故障、事件重复、双上下文互斥、停用安全结算和旧库只加不改。真实宿主/平台集成、资源预留与完整触发调度仍由后续任务实现；不把本地替身通过当成生产验收。
