# 经济系统

依赖：[契约](03-shared-contracts.md)、[生命周期](04-kernel-lifecycle.md)、[持久化](16-storage-recovery.md)。

## 范围与归属

economy 拥有钱包、预留、流水和预算规则；黑板投影余额和可用金额。首版单币种、无透支、配置生活费、一两种模拟兼职和常见消费。经济不是只能加减的裸状态条。

所有金额为 amount_minor 整数。配置的钱、实际支出、收入、退款与预留分别表达，不能用正负 cost.energy 或 purchase 同时代表所有资源。

## 最小对象

Wallet：owner_key、currency、balance_minor、revision。
Reservation：reservation_id、instance_id、amount_remaining_minor、status、expires/review_at。
Transaction：transaction_id、operation_id、kind、amount_minor、currency、effective_at、instance_id、source_ref、provenance、related_transaction_id。
BudgetPolicy：生活保留金、可选分类/月度预算及规则版本。

available = balance - 活跃预留。生活保留金在政策门控中另判断，不伪装成已花金额；不允许负余额时 balance/available 必须满足规定约束。

## 预留与阶段结算

1. 初筛只查 available，不扣钱。
2. prepare 得到实际报价或标明估计的上限；最终门控通过后预留。
3. 阶段付款原子写流水、减少余额并减少该预留剩余。
4. 完成/取消释放未用预留；退款是独立关联流水，不能删除原消费。

示例：余额 100，预留 36，可用 64。去程扣 6 后余额 94、预留 30，可用仍 64；门票扣 20 后余额 74、预留 10；返程扣 6 后余额 68、预留 4；释放 4 后可用 68。全过程不重复扣预算。

父活动统一持有预算，travel、ticket 等子步骤使用同一预留。金额超出剩余预留时须先增额并再门控；不能支付后才发现透支。活动准备已使用的真实 API/LLM 费用归宿主用量，不混入角色虚构钱包。

## 收入与周期

生活费由 profile 配置付款规则、金额和来源，按稳定周期实例去重；恢复时可补记规则明确应得的周期收入，但来源标 simulated，不能声称真实到账。

兼职是活动：地点、时间窗、完成要求、体力负荷、收入规则。工资在规定结算时点产生；缺勤、取消或部分工作按规则支付，不总发满额。家境不由通用模板预设。

## 支出、物品与情绪

购买成功事件可供简单 belongings/库存模块接收；首版只在确实需要“持有雨伞/收到包裹”时维护少量物品事实，不扩展成商城系统。已付订单不等于已收到物品。

消费对情绪的影响由活动提交 effect、affect 解释；经济只产出财务事实。financial_strain 可作为压力输入，避免多模块重复惩罚低余额。

## 接口

quote_available、reserve、increase_reservation、settle_step、release、record_income、refund、transactions。写入都带 operation_id、expected_revision 和执行证据；同 ID 不同金额必须拒绝。

本地模拟交易和真实外部付款不混用。真实操作走宿主授权与审计，结果未知保留预留并查询，不能立即释放然后发起第二次付款。未知预留定期显示待处理，不能自动当未消费。

## 页面读取 API

`GET /astrbot_plugin_good_life/targets/<target>/<role>/life/wallet?limit=20&before=<opaque cursor>`
沿用 life 页面的登录、提供方注册、私聊角色和 core 管理权限检查，以及 `status/message/data` 响应封装。`limit` 默认为 20，范围为 1–50；`before` 必须原样使用上页的 `next_before`。未知字段、重复字段、越界页大小或无效游标返回 `life_query_invalid`。

钱包未启用、未安装或协调器已停止时，`data` 只含 `{"enabled":false}`。读取不会安装钱包、生成收入、恢复活动或释放预留，也不开启新的数据库事务；只短暂持有已有存储读锁。

启用时 `data` 字段固定如下：

- `enabled`、`currency`、`minor_unit_digits`、`balance_minor`、`reserved_minor`、`available_minor`；CNY 的 `minor_unit_digits` 为 2（JPY/KRW 为 0，其余当前支持币种为 2）。空钱包返回零金额，不创建账户。
- `reservations`：仅当前 `active` 预留，每项含 `reservation_id`、`activity_label`、`limit_minor`、`amount_remaining_minor`、`status`、`review_at`（ISO8601，可空）。
- `transactions`：每项含 `transaction_id`、`kind`、`amount_minor`、`currency`、`occurred_at`（ISO8601）、`activity_label`。`occurred_at` 对应钱包存储的 `effective_at`；按该时间和 `transaction_id` 同时倒序，使用严格小于上一页末项的游标；对静态数据，时间相同也不会漏项或重复。跨页请求不共享快照；并发新增的流水若与游标的 `effective_at` 相同且 `transaction_id` 排在游标之前，可能不出现在本轮后续分页中，需刷新后重新从首页读取。`amount_minor` 始终为正，收支方向由 `kind` 判断。
- `next_before`：有下一页时为不透明字符串，否则为 `null`。

`wallet.py` 实际财务流水 `kind` 为 **`income`、`spend`、`refund`**；预留 `status` 为 **`active`、`released`**，此接口只返回 `active`。预留增额和释放不产生财务流水；内部 operation/audit 的 kind 不属于页面流水枚举。

所有查询使用宿主鉴权后推导出的 owner key，不接受客户端指定 owner。`activity_label` 只从相同 owner、相同 context 的已保存内核活动确定，目前 `buy_drink` 显示为“买饮料”；无活动、其他 context 或未知模板均为 `null`。钱包余额和流水属于 owner；跨 context 的活动名称不披露。页面对白名单以外的字段一律裁剪，禁止暴露 `source_ref`、`operation_id`、`instance_id`、`evidence`、`owner_key` 或原始 proposal，游标也只含时间和允许公开的流水 ID。

## 验收

同一消费回执重复只扣一次；两并发活动不能占用同一余额；阶段取消保留已付项；已退交易再次退款不多加钱；兼职缺勤不收入；跨月周期只记一次；不同币种配置拒绝或禁用；人工余额修订有审计，不改历史流水。

