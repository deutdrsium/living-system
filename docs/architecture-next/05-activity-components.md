# 活动组件规范

依赖：[契约](03-shared-contracts.md)、[内核](04-kernel-lifecycle.md)、[触发器](06-triggers-events.md)。

## 组件由什么组成

普通组件 = 一份模板 + 一个已注册执行器 + 输入绑定。新行为流程才需要新执行器代码。领域动态与活动可由同一功能包交付，但状态的 owns 必须唯一，不能每个活动都定义一份 energy。

区分 activity（有进度的行动）、event（时点事实）、condition（持续状况）、intent（待满足目标）。生病属于 condition，休息/治疗属于 activity；收到成绩是 event，复查成绩可以是 activity。

## 推荐最小模板

~~~yaml
schema: life.activity/v1
id: snack_run
version: 1
executor: purchase_local@1
requires:
  - economy.wallet
  - geo.nearby_shop
triggers:
  - {id: random_snack, type: random, rate: 2, per: week}
  - id: stress_snack
    type: state
    mode: enter
    when: {key: stress.level, op: gt, value: 0.5}
    p: 0.2
frequency: {cooldown_h: 12}
gating:
  free_min: 10
  available_money_minor: 1000
inputs:
  shop:
    required: true
    contract: place_offer/v1
    providers: [local_places, poi_search]
  trend:
    required: false
    contract: tagged_material/v1
    providers: [shopping_trends]
params:
  duration_min: [10, 30]
  spend_limit_minor: 3000
  currency: CNY
~~~

10 分钟/10 元只用于廉价初筛，不承诺一定足够。选定后按店铺、往返和实际报价再次门控。所有随机时长与价格抽样按实例保存，不能门控一次、执行时重新抽一次。

## 输入槽

每个槽声明类型、必需性、提供方优先级与时效要求。只有实现同一 contract 的提供方才允许互为回退。路线与天气为两个槽；街景与地点照片可作为视觉材料槽的备选，但分别保留来源类型。

required 缺失：不能开始依赖该输入的阶段。optional 缺失：允许降级并记录原因。模型只能消费实际得到的材料；不能凭“供应商失败”补写成已观察场景。

输入声明是能力请求，不是联网或对外动作授权。适配器检查宿主授权与调用预算，密钥留在安全配置服务中。

## 执行器接口

- prepare：返回阶段、预计耗时、资源报价、所需回执和输入引用。
- start：以已预留资源开始，不重复报价抽样。
- advance：按截至时刻推进到下一个可持久化阶段，返回 checkpoint 和待结算结果。
- cancel：说明可停止位置、已发生部分及可释放资源。
- resume：仅对支持暂停的执行器提供；从 checkpoint 继续。

同一个执行器可服务多个模板；购买零食、购买日用品只需要不同参数/输入，不必复制业务类。通勤是父目标的 travel 阶段，不能订阅所有活动再启动自己。

## 配置校验

检查 schema、枚举、范围顺序、概率、时间/金额单位、已注册执行器、状态键与依赖、事件类型、输入契约、条件操作符、冷却及窗口。类型化条件仅支持有限 all/any/not 与比较；复杂判断由声明输入的纯函数 predicate 提供。禁止任意 eval。

模板加载成功不等于可启用：缺少地图、厨房或学生身份时标为 unavailable，显示原因。人格标签主要调制效用，不在触发概率、门控和结果里重复乘同一偏好。

## 结果表达

执行器返回 03 的 ActivityResult，不能直接改 mood、钱包或记忆。presentation 仅控制姿态、注意力与文案，不决定生命周期事实。模型策略包括能力、调用/Token 上限、失败降级；“预计 calls”不能当实际用量。

## 新增一个活动的交付清单

模板、所依赖能力与输入槽、执行器复用/新增理由、结果归属、正常样例、缺依赖样例及取消样例。若不涉及新机制，不需要新增框架或一整套镜像实现测试。

