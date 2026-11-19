# 内核与活动生命周期

依赖：[公共契约](03-shared-contracts.md)、[恢复协议](16-storage-recovery.md)。本篇定义生命周期，具体阅读流程见 14。

## 内核的最小职责

提供统一时钟、模块注册、角色执行互斥、定时队列、快照、候选调度、活动实例管理及结算协调。首版同一角色一条前台活动线；长期等快递和衣服晾干是后台条件/事件，不占前台槽。

模块注册声明 owns、reads、emits、consumes、版本和必需能力。重复 owns 必须拒绝。注册完成后才能启用依赖该模块的模板。

## 一次推进

~~~text
recover_known_pending_operations()
advance_domains_to(now, ordered_known_boundaries)
snapshot = read_consistent_snapshot()
consume_due_events_once()
advance_running_activity()
propose_or_expire_candidates()
if foreground_available:
    choice = select_after_cheap_gate(snapshot)
    prepare_choice_then_recheck_and_start(choice)
deliver_committed_events()
~~~

顺序表达业务边界，不要求每次循环扫描所有活动。定时任务按最近边界唤醒，事件按索引消费；使用一个协调循环，避免每个模板一个常驻计时器。网络与模型等待不持有数据库事务或角色数据库锁。

同一时刻的边界采用稳定顺序：先结算刚结束区间，再应用到期事实变化与明确命令，随后产生新候选。调课/取消先使旧候选失效。未来若改变顺序，必须修改场景测试。

A4 增加两项通用的默认兼容扩展。纯本地执行器可选实现 `remaining_duration_s(instance, now) -> int`，给出当前时刻仍需的门控时间；必须非负且不超过固定计划的剩余秒数。未实现时完全保留 `planned_duration_s - elapsed_s`。start、恢复与资源准备后的最终门控均使用同一入口；它不修改已累计在线进度、首次开始时间或固定计划。适用于有绝对截止的活动，避免将离线时间再算成未来所需时间。

候选循环只将确定的组件缺陷隔离到单个候选：注册表缺模板/执行器、prepare 明确抛出的 `invalid_config`、或准备结果结构/数值校验失败，仍为 pending 时持久化为 rejected（过期则 expired），再检查下一候选。宿主 snapshot/audience/授权、SQLite、提供方故障，以及 prepare 中未明确分类的普通异常不证明候选本身无效；沿用原有 scope 异常边界，保留 pending/已消费状态，本轮跳过该 scope 的剩余候选，其他 scope 继续，下一 tick 可以重试。已消费的候选和已有在途实例不丢弃；出现占用前台的实例时停止尝试其他候选，继续原有恢复协议。异常信息仅保留统一错误码或异常类型，不输出私密正文。两项扩展均不修改冻结契约或关闭状态的行为。

## preparing

先完成廉价门控再进入准备：取得必要材料、路线与报价，校验权限和版本，创建持久实例。准备可花真实 API/模型预算，但不得先扣虚构活动支出、瞬移位置或写完成经历。

最后一次门控按实际准备结果检查时间、金钱、位置与能力。通过后在生活库内原子锁定前台槽与本地预留，再开始执行。涉及宿主授权/资产的交接沿用既有协议。准备失败则释放预留、记录原因，本轮可选其他候选；不得无限重试同一个失败源。

本地执行器的规则是：**prepare 只能依赖 template 与 now；外部依赖失败须抛非 INVALID_CONFIG 码**。材料授权、版本核验和其他外部预检由世界侧编排在事务外完成；不能从 prepare 读取宿主、数据库或进程状态，再将临时不可用误报为永久配置错误。

## A1 本地事务准备扩展

此扩展实现契约 v1 已有的 Effect、RecordRef、reservation_refs 与 StateSnapshot 语义，不修改 contracts/、CONTRACT_VERSION、生命周期边或宿主端口。`LocalExecutor.start/resume` 仍同步返回 checkpoint；未声明扩展的本地执行器继续使用原回调。内核默认关闭时不安装 schema、不调用资源钩子。仅在显式启用且同进程共享 LifeStorage 的本地执行器路径使用，不能用来提交网络、模型、真实付款或跨库动作。

可选纯计算钩子 `prepare_resources(instance, now, snapshot)` 返回内核内部 `ResourcePreparation(phase, checkpoint, effects=(), reservation_refs=())`。参数 instance 是待进入 running 的视图（原始实例还未提交）；snapshot 是本次最终门控实际使用的自有不可变组合快照。引用为本次新增引用，内核合并既有引用并拒绝同 kind/id 的冲突版本。效果由已有唯一领域拥有者的 `apply_effect(db, instance, effect)` 校验和写入，执行器不接触数据库。钩子不隐式新增宿主能力或 wire。

start、paused 恢复和重启恢复 running 共用如下边界：在 BEGIN IMMEDIATE 中读取当前实例及快照，最终门控，通过后计算 checkpoint 与领域准备；校验准备期间快照版本未变，暂存实例及引用，提交领域效果，再重新读取组合快照检查完整剩余时间窗、资源、前台和能力，并在提交前再次验证该快照未变及授权仍有效。所有暂存只在同一事务内可见。领域拒绝、版本冲突或准备后的门控拒绝将实例、引用、钱包、效果去重及命令回执一起回滚，实例不会进入 running；准备前门控拒绝沿用原有 preparing→failed / paused 原样返回策略。新命令可在依赖恢复后重新尝试，已提交命令按原摘要返回原回执。

事务内的宿主 snapshot/authorize 在 life 锁和 SQLite 写锁内同步调用；锁顺序仍为 life→host，但临界区比原先更大，真实宿主适配器接入时必须专门核验，不能在回调内再次开启 life 事务。同一 now、同一底层事实版本必须产生确定的快照；observed_at 由传入 now 导出，不能每次读墙钟或递增计数。事实经资源准备实际变化时仍须产生新视图。

内核为资源准备使用 SAVEPOINT。可暂停阶段恢复时若准备之后的门控拒绝，先回滚该准备的实例、引用、领域效果和快照变更，再保存原 checkpoint 的 paused 状态；主动恢复 paused 实例则保持原 paused 实例。这样准备前与准备后的拒绝都不会让恢复长期占用前台。对不确定快照等导致的 STALE_VERSION，在同一 coordinator 生命周期和同一实例 revision 上最多连续尝试三次；前两次完整回滚并返回 stale_version，第三次 preparing→failed 释放前台，可暂停阶段的重启恢复 running→paused 保留已知进度和原有预留。所有恢复暂停仍核验执行器 pausable_phases；未声明可暂停的阶段继续返回明确错误并保留原状态、原资源，可用已有授权 cancel 出口，不能因重试耗尽伪造安全停靠。成功或其他错误清空该连续次数，关闭 coordinator 清空内存计数。快照恢复确定后可显式恢复 paused 实例，不补算离线进度。

内核 schema 2 只增加 `life_kernel_snapshots(owner_key, revision, snapshot_hash)`。在同库协调事务中，以宿主快照版本、各领域值/版本及时间、能力和 agenda 的完整指纹分配 owner 本地递增 revision，既不是各领域 revision 之和，也不是直接沿用宿主 revision。相同视图复用 revision；任一输入变化会得到新 revision，回滚的版本不成为事实。当前表只按 owner 保存最新视图，不按 context 或实例分别保存版本；本轮仅记录该限制，不扩充 schema。首次分配以当前宿主版本及旧 Candidate/GateResult 历史中的最大 snapshot_revision 为下界，并越过已有历史编号，避免升级后版本倒退或别名；已有 Candidate、GateResult 与实例历史不重编号。领域 StateValue.revision 保留其原版本，资源准备须将所依据的版本交回领域写入者进行 expected_revision 校验。

`abandon_result` 在原始本地提案尚未结算且无外部操作的前提下，将实例暂存为 failed，并调用可选纯计算 `abandon_resources(instance, result, now)`，以同一领域效果通道提交清理。清理只释放领域实际持有的未用资源，不结算被放弃提案里的支出，不修改提案 checkpoint。清理拒绝时 failed、释放、审计、结果 final 和命令回执全部回滚；重放原命令不重复调用钩子。关闭内核后重启并新建 Wallet 时不执行 install，清理钩子仍只通过传入 db 读提案和领域事实，不依赖 Wallet.storage 的绑定。unknown/外部 receipts 的限制继续保留。无清理钩子的旧组件保留既有管理员退出行为。

购买新实例固定 `buy_drink@2 / purchase_local@2`，start 即预留并持久化 `economy.reservation` 引用，首个实际在线区间从 start 开始累计；预留命令的 expected_revision 来自本次组合快照内钱包 StateValue.revision。`buy_drink@1 / purchase_local@1` 只读兼容：既有模板元信息可读，已持久化实例继续 start/推进/恢复/结算，但其执行器声明内部 `accepts_new_instances=False`，propose 及 prepare 创建入口均拒绝新建 @1 实例；升级前残留的 pending 候选在 prepare 中持久化为 rejected，已到期则为 expired，因此不会反复阻塞后续新版候选。旧 awaiting_reservation 实例保留 checkpoint、首次 advance 才预留及从预留时刻计时的事实，不批量转换或补记经过时间。两个版本都支持放弃提案时的自动清理；A2 受校验的手动 release_abandoned 仍可清理旧版本历史终态，自动清理已完成时返回原释放回执。

升级只在启用内核时核对旧 schema 1 对象后增加快照表并将内核自己的版本项置 2，不变更 good-life.storage.v1、经济 schema 2、旧表和生产归属标记。关闭后的必要结算兼容 schema 1/2，不触发安装迁移。旧代码不能打开 schema 2 的已启用内核；回退须先保留一致性备份和全部新增事实，不能删除新版本实例或重发操作。

## running 与 paused

执行器按阶段写 checkpoint；效果在实际发生时结算。例如去程车费已发生，后续取消不能退掉没有退款依据的车费。长活动的疲劳按实际区间推进，不全放在结束时一次扣。

暂停需执行器声明可停靠阶段。暂停后不能继续累计已停止的前台工作量；等待物流等计时由独立事件队列继续。恢复前校验权限、来源、目标版本和资源。paused 实例与新候选争用同一个前台槽。

## completed、failed、cancelled

终态提交包括实例、领域效果、资源释放、产物引用和待投递事件。生活库本地变更尽可能在同一事务完成；各领域校验后才写入。下游日记或记忆失败不撤销既定生活事实。

取消保留已走路线、已花金额、已读片段；未发生部分释放。failed 记录失败点与实际进度。完成一个取消命令，不等于把活动标成 completed。

## unknown

外部调用超时且不能确定是否执行时，实例或该步骤进入 unknown。记录稳定 operation_id，优先查宿主审计/提供方回执。没有可核验依据时暂停相关流程，不能新建操作 id 重做。纯观察性失败可按提供方策略重试，但不能把“请求是读取”推导为所有派生模型任务都免费可重放。

## 人工操作和并发

用户聊天、管理页、触发器均走相同命令入口，带原身份和 expected_revision。新安排可取消尚未开始的自由计划；不得直接覆盖在途事实。多会话操作按 owner_key 串行，私有材料继续保留原 context_key。

回消息的注意力策略与前台暂停策略分别配置。收到消息不自动把睡觉/行走/上课标为完成。

## 关闭与热重载

停止接收新实例，撤销旧租约，保存或保留在途任务状态，再结束工作。重绑不复活旧回调，不启动第二个循环。生活插件不可用时主插件不得自动恢复旧执行器。

## 验收

同角色双会话同时开始活动仅一项成功；取消后只结算实际阶段；重启不重复效果；未知结果不重发；热重载只有一个循环；下游消费者失败不反向改变完成状态。

