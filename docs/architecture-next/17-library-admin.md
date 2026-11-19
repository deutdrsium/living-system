# 活动库配置与管理页

依赖：[组件规范](05-activity-components.md)、[触发器](06-triggers-events.md)、[来源](15-sources-memory.md)。

## 活动库是内容目录

附件 activities.yaml v0.1（仓库内副本：[activities-draft-v0.1.yaml](activities-draft-v0.1.yaml)）有 84 个唯一活动、14 类、30 个输入源名和 18 个输出类型；scope 为 73 条 universal 与 11 条 student。保留它作为参考清单，不直接当成可执行规范。

通用包不内置某个人物的家境、同学和私有账号。core/student/worker 等是装配配置；厨房、室友、校园、交通工具属于能力/场景条件，universal 不表示所有角色都必须能执行。

配置分四层：引擎默认 → 活动包默认 → 角色/世界配置 → 显式管理员覆盖。有效配置页显示来源和版本；命令/实例固定生效时的版本。高层不能覆盖授权规则或篡改来源限制。

## 附件转换规则

| 原写法 | 处理 |
|---|---|
| random.rate 无单位 | 按附件注释每周转换，显式 per: week |
| followups 与子活动 chain 双写 | 转成一条事件订阅，报告重复，不叠加概率 |
| inputs 全局顺序回退 | 拆具名槽；只有同契约提供方在槽内回退 |
| p: 0.0 表示“运行时决定” | 固定零只表示关闭；动态概率使用已注册 policy |
| commute.after="*" | 移除，移动由父活动显式创建 travel 阶段 |
| 生日后的负 delay | 改为未来日历实例的 lead time |
| 病况、成绩、衣物未干当活动 | 分成 condition/event；实际应对才是 activity |
| cost.energy 同时表示恢复与消耗 | 改成阶段负荷/效果，由状态拥有者计算 |
| outputs 包含 memory、mood_delta、photo | 拆产物、效果、事件与下游候选，补来源 |
| presentation.interruptible | 分开注意力策略和执行暂停/取消能力 |
| 未声明 string cond | 转类型化条件或有声明的纯 predicate |
| modifiers 多处重复强化 | 偏好主要进入效用，机会与长期兴趣更新分开 |

转换先输出差异和不可映射项，不静默猜测。缺执行器/依赖的模板保持 disabled，并解释原因。

核对到 18 组双写关系：laundry→clothes_wont_dry；tidy_room→revisit_old_memory/revive_old_hobby；insomnia→scroll_feed；drink_caffeine→insomnia；online_shopping→pick_up_parcel；follow_series→browse_discussion；listen_music→curate_playlist；skip_class→caught_absent；exam→get_grade；walk→take_photo/caught_in_rain；caught_in_rain→minor_illness；sunset_watch→take_photo；commute→caught_in_rain；trip→write_postcard；visit_exhibition→write_review；exercise→shower。

## 首批活动

建议逐步启用 12 个模板：吃饭、睡觉、洗衣、买零食、网购、取件、兼职班次、上课、分段作业、阅读、浏览资讯、散步。它们不是一个迭代全部实现：先买饮料闭环，再按 18 的依赖扩充。看展和创作随后加入。

活动包应携带模板、依赖版本、输入绑定说明、正常/失败样例和素材许可信息。只验证新增行为或契约，不为每个参数组合复制大量测试。

## 管理页信息结构

第一阶段页面范围沿用原计划；新功能实际启用后再增加对应页签。

| 页面 | 最少内容 |
|---|---|
| 生活总览 | 当前活动/阶段、下一承诺、状态分档、异常与依赖可用性 |
| 日程与学业 | 课表、课次变更、大纲、任务截止、学习进度与来源 |
| 阅读与浏览 | 实际材料、消费范围、当次产物、暂停/续接和回执 |
| 地点与出行 | 当前节点/在途、路径、时间/费用估计和来源时间 |
| 钱包 | 可用/预留/余额、流水、收入规则、未决支出 |
| 组件与设置 | 模板启用状态、缺依赖、参数版本、校验结果 |
| 诊断 | 为什么没发生、候选效用分项、事件链、实例与操作回执 |

业务页面用“余额不足”“资料已失效”等可理解原因；内部 id/摘要放详情，不铺满正常生活界面。展示“计划中”“执行中”“模拟”“结果待确认”，不能统一显示成功。

## 写入与认证

管理页只调用统一命令入口，带角色、权限与 expected_revision；不能直改 SQLite 或跳过领域校验。浏览器认证沿用 Pocket ID 与 AstrBot 访问控制。

未来在目标服务器 安装/更新页面时，必须在现有单点登录环境检查真正加载：iframe 内脚本、样式、后台读取与保存；同时验证匿名与未授权写入仍拒绝。调试用浏览器标签控制/浏览器调试，不使用整个桌面的 Computer Use。本轮文档不执行该部署验收。

## 验收

配置错误能定位到模板字段；缺能力不悄悄当成功；启停不破坏在途版本；管理页不扩大来源可见范围；提交过期 revision 不覆盖新状态；“为什么没发生”能定位触发、门控或执行阶段。

