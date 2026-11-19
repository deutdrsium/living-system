# 活动执行与当次感想

依赖：[边界](02-plugin-boundaries.md)、[生命周期](04-kernel-lifecycle.md)、[材料与记忆](15-sources-memory.md)。这是从“世界触发、主插件执行”过渡到目标架构的实施重点。

## 执行归属的判断标准

谁决定活动步骤、读取范围、阶段成功条件、暂停恢复、结果组合，谁拥有业务执行。调用模型的函数位于宿主，不意味着业务必须归宿主；只移动文件而仍传完整 app 也不算解耦。

目标：执行器在生活侧，使用受限 HostCapabilities；模型网关、权限、来源、平台连接和任务审计留在主插件。

## 当次感想对象

Impression 包含 artifact_id、instance_id、phase、material_refs、text/structured_observations、questions、persona_context_version、model_task_ref、created_at、provenance、audience_bound。

感想回答本次材料或经历，允许“没理解”“没有特别感受”。不得每次强制生成宏大价值观变化。它不直接改人格或长期兴趣，也不自动变成对用户的一条消息。

活动记录可以引用宿主托管的资产；生活拥有感想的业务关系，宿主资产服务继续约束访问与撤回。

## 阅读的目标执行链

1. 按队列选择未读片段，固定书籍来源版本、范围和活动实例。
2. 从宿主取得受限人格/兴趣上下文，以及授权原文。
3. 生活侧构造阅读任务的结构化输入，调用宿主持久模型任务端口；使用稳定 task_id。
4. 持久化返回结果引用，验证结果结构、来源仍有效、实例 revision 未失效。
5. 达到所需活动时间且必要消费任务完成后，提交已读范围、进度、当次产物与下一片段。
6. 发出 reading.completed/segment.completed；宿主独立处理兴趣和记忆候选。

模型几秒返回不等于角色已完成 30 分钟阅读；材料实际消费完成与生活时间达到均需满足。没有消费证据不得只按墙钟推进书籍偏移。模型超时不能换 task_id 假装第一次调用。

v1 兼容投影的 FAILED 可能来自 inspect 时的结果重验；来源/授权恢复后同一任务可再次显示 SUCCEEDED。v2 batch 1 的独立 OperationReceiptV2 固定执行事实与终态，访问变化通过 ResultAccess 表达；模型 v2 adapter 尚未声明运行支持，不能对 v1 投影声称单调。

Contract v1 的模型回执与能力声明没有 deadline/timeout 字段，因此生活侧保留独立安装所需的 480 秒回退值；宿主提交与注册共用一个超时常量，回归测试约束两侧一致。仅确认原任务不存在、以同一 task_id 重新提交时刷新 deadline；已有 PENDING/RUNNING 任务在重启后沿用原 deadline。提交 command 后先恢复原决议：只有确认 aborted 时才走暂停路径；确认 commit 后，journal/interest 投递中的来源或版本错误以 completed 结束，标记 events_withheld_source_unavailable、记录未确认接收的消费者并清除 command/proof。暂时投递失败使用 MAX_RESULT_CHECKS 次退避重试，重启保留次数与期限；耗尽后以 events_withheld_retry_exhausted 完成，不再占用阅读调度。已确认的 commit 不因投递重试而重复 prepare/commit，已接收的消费者也不重复投递。

必需与可选产物区别：阅读消费/理解任务是必要步骤时，缺结果保留暂停/未知；单独的润色或分享文案通常可选，失败不抹掉已确认阅读范围。具体要求由执行器契约声明，不由 UI 临时决定。

## 浏览与真实互动

浏览执行器组织取得内容、筛选、阅读和记录，保留 item/source 版本与实际工具回执。点赞、评论、发帖等动作继续通过宿主平台能力及原有授权，不能因为“自主生活”就获得任意发送权。

读取成功不等于互动成功；发送结果 unknown 时冻结该操作、查询原回执。模拟互动必须标为模拟，不能写入真实平台成功回执。

## 创作、上课与旅行

创作完成需有实际保存的作品资产；读影评不能记为看完整电影；引用照片不记为亲自拍摄。上课有参与记录和材料记录两条轴；旅行有路线记录和场景材料两条轴，不互相代替。

活动执行器可调用通用 summarize/reflect 等模型任务，但不能使用一个“大万能 prompt”决定所有时间、钱与位置。阶段成功条件由代码和回执控制。

## 抽取步骤

A. 冻结既有行为：记录输入、任务 id、来源核验、进度、结果和失败路径，建立少量代表性回归。
B. 引入明确 executor 接口，让旧宿主实现暂充 adapter，保持同一实例只能一个 execution_owner。
C. 把阅读专属提示模板、输入组织、阶段编排、结果解析迁入生活侧；保留宿主通用任务端口。
D. 把进度与当次感想写入生活拥有的记录，通过现有跨库协议处理资产/审计关系。
E. 移除宿主已无调用的活动专属分支，继续保留兼容入口转发，随后对浏览做同样处理。

不能复制 RoleReader 到新插件后同时启动两份。不要一次抽离人格、模型网关、来源和数据库基础设施。

## 已有代码定位

- 新调度：[reading_scheduler.py](../../plugins/astrbot_plugin_good_life/good_life/reading_scheduler.py)
- 宿主能力：[life_host_capabilities.py](../../plugins/astrbot_plugin_private_companion/astrbot_host/life_host_capabilities.py)
- 原阅读执行：[reader.py](../../plugins/astrbot_plugin_private_companion/companion_v2/role_work/reader.py)
- 当前注册约束：[生活服务接口](../life-provider-contract.md)

路径用于定位，不表示这些文件无需进一步拆分或当前所有功能已启用。

## 验收

旧/新路径的同一授权片段得到等价进度和回执；中途撤权不落稿；模型完成但本地提交中断只补落库；可选感想失败不重复消费；主插件记忆失败不重读；全局人格上下文依然一致。

