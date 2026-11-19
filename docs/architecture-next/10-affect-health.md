# 情绪压力与健康模型

依赖：[状态系统](08-state-system.md)、[学业](11-academics.md)。本篇用于角色生活模拟，不建立人的临床预测模型。

## 情绪

先用一个情绪潜变量 z 表达向基线回归与随机波动；展示 mood ∈ [0,1] 可采用 sigmoid(z)。需要更丰富表达时再加入唤醒度；首版不同时引入大量同义状态。

潜变量遵循 OU：

~~~text
dz = k*(mu-z)*dt + sigma*dW
z_next = mu + (z-mu)*exp(-k*dt)
         + sigma*sqrt((1-exp(-2*k*dt))/(2*k))*N(0,1)
~~~

要求 k>0；dt 和 k 使用一致单位。上式是输入/参数恒定区间的更新。显示值经过 sigmoid 后不是严格 OU；不要用显示均值反向误解释潜变量 mu。若实现选择截断 OU，需记录该选择并检查边界堆积，不能混用两种参数含义。

已完成事件产生 impulse，例如得到成绩或一次愉快交谈；长期压力或睡眠状态可改变目标 mu，但只在一个明确入口作用。LLM 不直接每轮写 mood。

## 压力

stress 模块拥有短期 stress.level 和慢变量 stress.chronic。学业提供工作负荷，经济可提供财务紧张信号，活动提供刺激/恢复；压力模块统一合成目标并平滑更新。

剩余作业工时除以截止前可行工时形成紧迫信号。分母不含睡眠、课程与不可用时间；为零时使用明确的 infeasible 档，不能无穷值溢出。逾期单独记录 overdue，不将负时间塞进比值。

chronic 是 stress 的低通/滚动派生指标，窗口与单位配置化。不要同时把任务数量、deadline、工作比值和逾期全部满权重相加，重复表达同一压力来源。

## 健康与病程

健康模块拥有基线 health.level、病程 episode 和暴露记录。事件生成只在模型定义的易感状态运行；病程有 onset、阶段、严重度、恢复条件和结束时间。

状态相关风险率示意：

~~~text
lambda(t) = lambda0 * exp(beta_sleep*sleep_debt
                        + beta_stress*chronic_stress
                        + beta_season*season_feature
                        + beta_exposure*exposure)
p(event in dt) = 1 - exp(-integral(lambda(t), dt))
~~~

lambda 单位必须为 1/时间。跨状态变化分段积分；常率段化简为 1-exp(-lambda*dt)。不能每 tick 用同一个固定 p。参数必须有限，输入做规定的归一化；极端值校验/限制作为参数策略明确记录。

雨淋、季节等这里只是可配置的模拟暴露特征，不宣称“淋雨必然感冒”。暴露有来源、时间和衰减，天气晴雨不能永久滞留为高风险。

病程不会占住前台活动几天。病程改变精力恢复、行动约束和休息需求；休息、就医等是实际活动。首版同一病种未恢复时不重复抽 onset，不模拟多病原传播网络。

## 行为与下游

高压力可提高舒缓活动效用；低情绪不自动禁止学习或社交。严重病程触发休息/改期候选，但需要走日程和取消规则。

事件引发的情绪数值变化由 affect 结算；即时感想由活动执行器生成。二者可以相互提供上下文，不要求每次 mood_delta 都调用 LLM。长期自我反思仍在主插件。

## DA NE 5-HT 的取舍

首版不采用。这些变量缺少可观察校准数据，且会与已有情绪/精力重复。以后只有当具体目标需要、参数与观测映射明确且能通过对照验证时，再独立实验；不要将生化名称作为真实性保证。

## 验收

恒定输入下情绪向基线回归；读取不重新抽噪声；随机流可恢复；压力零可用时间不溢出；病程不独占活动槽；同一病程不会重复 onset；改变 tick 时疾病发生分布近似一致；感想失败不撤销已发生的数值效果。

参考：[Oravecz 等的情绪随机过程模型](https://sites.socsci.uci.edu/~zoravecz/bayes/data/Articles/Oravecz2011AHLSD.pdf)、[Stan 的 hazard 与生存函数](https://mc-stan.org/docs/stan-users-guide/survival.html)。本文简化与耦合系数是工程提案。

