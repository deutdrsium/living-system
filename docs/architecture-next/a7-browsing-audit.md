# A7 浏览抽取审计

状态：文档审计；不是 A7 实现或上线验收。基线 `0628be7877800ac6f69867448fed2d74e9596ad0`，分支 `w4/a7-audit`，架构契约 `CONTRACT_VERSION=1` frozen。本任务只新增本文及 [v2 草案](contract-v2-proposal.md)，不改代码、`contracts/` 或 03。

证据标记：**CONFIRMED** 表示在本基线代码中读到的事实；**INFERRED** 表示据此提出的设计、风险判断或尚待验证的行为。测试名称表示测试覆盖意图，实际运行结果另见 PR/CI，不能据名称宣称真实平台已通过。以下 `GL/` = `plugins/astrbot_plugin_good_life/good_life/`，`HOST/` = `plugins/astrbot_plugin_private_companion/`；`文件:行` 均相对本基线。

## 结论与前置决策

**CONFIRMED**：浏览的调度、活动和曝光事实已经在生活侧，但推荐选择、工具循环、当次反思及完成组织仍在宿主 `RoleSocial`。当前 async v1 只协商阅读权限/任务和两个临时本地动作；不能用它直接接管真实平台浏览。A1 内核只接受 local-only 执行器，结果拒收外部回执/资产，模板只支持显式 intent。因此“先抽阅读，再抽浏览”目前仍是必要的串行边界。

**INFERRED**：建议先审批 v2，再串行实现宿主通用动作/材料端口与内核外部结果适配，随后按 A3 抽业务编排。不能只复制 `RoleSocial`，也不能将两个本地协调动作当作已经存在的真实平台动作能力。需用户决定的方案集中在 [v2 草案](contract-v2-proposal.md)：本地协调端口分类、执行事实与结果可用性分离、动作证据/摘要、链式事件适配和发布配对。

## 当前调用图

下表整表为 **CONFIRMED**。行号是函数入口或关键语句位置；箭头包含同步提供方与异步宿主边界，不表示整条链都 async。

| 阶段 | 调用链与代码证据 | 事实归属/核验 |
|---|---|---|
| 调度优先级 | `GL/sleep_scheduler.py:245` collect → `:261` social_first → `:264` 阅读/浏览顺序 → `:268` advance | 有 worker 时不启动另一前台活动；手动阅读优先。`GL/social_schedule.py:41` 按 owner/day 与上次完成类型选择顺序 |
| 浏览机会 | `GL/social_scheduler.py:39` advance → `:45` social_snapshot → `:55` social_window → `:59` worker | busy、平台可用、阅读 pace_until、浏览间隔、睡眠窗及日历检查；`social_schedule.py:15`/`:38` 保留旧窗口与间隔语义 |
| 创建 | `GL/social_scheduler.py:34` _browse → `HOST/astrbot_host/life_host_capabilities.py:593` start_social → `HOST/companion_v2/integration/social_handoff.py:81` execute → `:44` prepare / `:62` decide → inherited finish | 宿主 preflight；prepare 和 decide 都复核窗口/权限，生活提供方写活动。activity ID = `social-activity-` + digest(context.key, request_id) |
| 执行入口 | `life_host_capabilities.py:601` execute_social → `:615` role_social.consume → `HOST/companion_v2/role_work/social.py:218` consume | 再核 revision、active、work.kind、平台绑定；固定前 3 条已完成浏览上下文。协调读取屏障保留 |
| 监督任务 | `social.py:222` 原 task ID → `:230` tasks.submit → ExactTaskRunner → `:517` _generate | parent ID = `social-task-` + digest([context.key, activity_id])；注册 `:172` 外部网络、3600 秒、max_attempts=1、concurrency=1；`:204` validate 检查原 job/活动/上下文/平台 |
| 推荐与选择 | `social.py:544` 推荐分支 → `:374` _recommendations → SocialPort.call(feed) → ExposureStore.save_batch / topics / selected | 轮换平台、固定 activity 种子、最多 8 张卡、加权最多 3 项、随机试读；分类任务 `:314` _classify；暂停/负反馈先过滤 |
| 模型/工具循环 | `social.py:603` open_session → `:630` generate → `:683` tool_calls → `:738` port.call | 原生工具名 social_browse；白名单、先读后写、临时 token、风控阻断和覆盖程度在业务层检查 |
| 正反馈互动 | `social.py:669` feedback → `:674` _feedback_engagement → `:475` reserve → `:483` port.call | 只对已试读、已曝光、有已保存正反馈且明确 engagement 的条目；与工具循环共用 action ID 命名空间 |
| 完成 | `social.py:832` completed 判定 → `:842` completion operation → `:845` complete_supervised_social → `social_handoff.py:167` → `:129` execute_completion | actual_actions + subjective_reflection 写 checkpoint；生活活动 completed/cancelled，宿主保留监督/权限和命令决定 |
| 跨库与事件 | `social_handoff.py:144` prepare → decide → apply → complete → acknowledge → `GL/staged_activity_commands.py:672` _publish_completion | 完成事件 ID = `life-activity-` + SHA256(operation_id)，完成消费者 journal/interest；未确认期间仍受原读取屏障保护 |
| 恢复 | `GL/sleep_scheduler.py:139` 识别旧 social command → `HOST/astrbot_host/life_host_capabilities.py:569` recover_social；`GL/sleep_scheduler.py:173` 扫描孤立 active work → pause_interrupted | 已决定命令恢复原决定/落库/确认；不是重跑平台工具。没有待提交结果的孤立浏览先暂停 |
| 宿主旧归属 | `HOST/companion_v2/role_work/life/smart_social.py:42` advance → `:72` _browse → RoleSocial.consume | 兼容路径直接在宿主创建/转移；`social.py:844` 按 activity_owner 选择唯一完成路径，不能并开两份 |

## 真实平台副作用与授权边界

下表整表为 **CONFIRMED**。代码证明“调用了平台适配器”，不证明外部服务最终效果。

| 动作 | 实际调用位置 | 约束与证据限制 |
|---|---|---|
| B站点赞/收藏/关注 | `HOST/astrbot_host/social_port.py:631` / `:637` → run_bili video like/fav 或 user follow → `:743` session.call_tool | BV/用户 ID 校验；`social.py:111` write_allowed 要求对应观看内容或从真实 owner.mid 得到的关注目标 |
| 小红书点赞/收藏 | `social_port.py:641` → like_feed/favorite_feed → `:779` HTTP MCP → `:743` | 笔记 ID 与 token；推荐 token 由当前卡片注入，不能从历史正文恢复凭证 |
| 知乎赞同/收藏 | `social_port.py:652` → vote_up/favorite_add → `:712` 本地服务 HTTP tool | readToken + 具体 reason；`:730` pending 强制 ok=False，不能记成互动成功 |
| 豆瓣想读/想看 | `social_port.py:657` → mark_book/mark_movie(status=wish, shareToFeed=False) → `:743` | 想看不是看过；账号写能力只由宿主配置/持有 |
| 推荐、搜索、正文、图片/抽帧 | `social.py:389`、`:556`、`:738`；`social_port.py:698`、`:704`、`:743`、`:843` | 是实际网络、账号或外部计费调用；B站 watch 传 record=False。只读也不宣称绝无服务端记录；仅 google_search 有适配器内 5/30 秒重试，`:807`，国内操作/写动作一次调用 |

**CONFIRMED**：业务白名单写动作仅为 `social.py:55` WRITES 中的 9 项，没有评论/发帖。`RoleSocial.validate:204`、每轮 `run.validate()`、`write_allowed:111`、账号 enabled/available、blocked_for 均属于现有防线。`SocialPort.call` 是底层连接适配器，不能因方法存在就认为它独立实现了完整角色授权。凭证、浏览器会话和平台连接继续归宿主。

**INFERRED**：A7 必须把这些防线变成宿主端口的独立核验，而非相信生活侧传来的 viewed=True。新增评论、发帖不属于等价抽取，应另提权限/契约申请；不能扩大现有动作集。

## 幂等、unknown 与证据缺口

**CONFIRMED**：两条写路径在网络前调用 `OperationRepository.reserve`：工具循环 `social.py:724`，反馈路径 `:475`。ID 都是 `social-action-` + digest([原 context.key, operation, target])，parameter_hash 是 digest([operation, target])。工具循环的知乎 target 通过 readToken→真实 URL 解析；反馈路径用卡片 ID。范围跨 activity，意味着同一 context 的同类同目标互动会被历史记录挡住；不是每活动一次。

**CONFIRMED**：`HOST/companion_v2/storage/repositories.py:87` reserve 按 owner + 原 scope + idempotency_key 返回 replayed，异摘要拒绝，operation_id 全局碰撞也拒绝。`social.py:731` 和 `:480` 对任何 replayed 都跳过，包括 accepted、completed、unknown。返回 ok 后从 accepted→completed，否则→unknown（`:779`、`:490`）；unknown 不进入成功 action 列表。普通工具异常在 `:821`/`:823` 转成失败工具回复，可能让已预留操作停在 accepted；重复仍被拦截。反馈路径捕获 call 异常后通常写 unknown，但进程退出/协程取消也可能留下 accepted。

**CONFIRMED**：`reserve` 的旧摘要不含 reason/token；不能把它说成已验证完整业务参数摘要。`SocialPort.call` 不接收 operation_id，只看到 route 参数；本地幂等不能证明平台有相同幂等键。`OperationRepository.transition:196` 只做 expected_state 的 CAS，没有 03 风格的完整回执转移图。当前 async `inspect_action`（`life_async_capabilities.py:353`）只查询临时本地 life action identity，不是 social-action 的通用查询接口。

**INFERRED**：现有机制满足“遇到已有记录不自动重发”的保守策略，但 accepted 无法区分“预留后未发”和“发出后未落回执”，unknown 也没有通用自动平台核实器。不能推断 unknown 最终会自动成功，也不能在迁移时将 accepted 视为安全可重发。A7 应保留旧 ID/摘要/审计行，把未知动作作为阻塞证据；仅允许查询原操作或附原 ID 的明确人工核实，不能因冷却、重绑、换执行器或生成新 ID 解除。

**CONFIRMED**：parent SocialJob、工具历史、viewed、read_tokens、推荐临时 token 保存在内存（`social.py:167`、`:226`、`:618`）；子任务分类/图片使用分配后保存于内存的随机 ID（`:322`；图片见 `:259`）。完成命令可按原决定恢复，但未到完成命令的工具循环没有 A3 那样持久阶段存储。启动恢复在 `GL/sleep_scheduler.py:173` 将孤立 active social 暂停。

**INFERRED**：A7 要新增 durable checkpoint 和只含引用的工具交换清单；不能把“恢复完成命令”写成“完整浏览会话可续跑”。旧在途浏览留给 host_bridge/暂停核实，新实例才使用新编排；旧随机子任务 ID 读取审计或原存储，不能按新公式重建。

**CONFIRMED**：`social.py:832` 的 completed 条件是 events 中存在任意非 WRITES 动作；`:619` 会把成功取得推荐批次记为 feed event。因此当前“完成浏览”可仅证明取得推荐/列表，不一定证明已读正文；checkpoint 的 coverage 才区分 results/metadata/text/image。此次本地 decline 测试失败也显示模型最终拒绝前可能已发生自动推荐调用。

**INFERRED**：等价抽取必须记录并保留这个完成判定，不能暗中改成“读完正文”或把列表曝光宣传成读完文章。若要提高完成证据门槛，应在正式契约/行为变更中另行审批并补测试。

## 来源、日记、兴趣与社交耦合

| 关系 | 证据及当前事实 | A7 边界判断 |
|---|---|---|
| 来源版本 | **CONFIRMED**：`social.py:871` _images 为 parent/child 发布 v1 来源，保留 SHA256、audience、1 小时有效期；`:611` 每次 media refresh 检查 parent/child。正文工具回复在内存 results/history；最终 checkpoint 只留 platform/operation/id/query/coverage 与反思，没有逐项文本 SourceRef/version/digest | **INFERRED**：新取得材料固定来源版本/摘要、范围、受众和实际送模覆盖，正文/图片都在提交前重验。历史只有 URL 时保留缺证据状态，不补造来源版本 |
| 曝光与兴趣 | **CONFIRMED**：`HOST/companion_v2/role_work/exposure.py:132` save_batch 在迁移模式将内容写生活库、宿主只留空正文 FK 占位；`:231` topics / `:330` feedback / `:378` assess 仍改宿主兴趣、主题关系、反馈、系数；`social.py:367` 直接 SQL 读这些表 | **INFERRED**：不能把 ExposureStore 连数据库一起搬。生活拥有曝光/实际尝试/选择，宿主提供受限兴趣上下文并消费候选，抽取首阶段需兼容 adapter 保留原反馈顺序与去重 |
| 完成后兴趣 | **CONFIRMED**：`HOST/companion_v2/integration/life_event_runner.py:169` _interest 查 processed；`:181` 有曝光 session 的 social 返回 not_eligible，避免再跑普通兴趣分析 | **INFERRED**：新路径不能同时保留即时 topics/feedback 又对完成事件再增加一次兴趣；宿主消费者失败不重新浏览 |
| 日记 | **CONFIRMED**：`HOST/companion_v2/integration/life_event_inbox.py:102` journal inbox 标 completed 只是材料可读取；`HOST/companion_v2/role_work/journal/material_producer.py:60` 按活动 scope/revision 取 description/checkpoint 形成材料，故包含浏览反思 | **INFERRED**：这不是已生成日记/长期记忆 ACK；新反思应有受控资产引用和撤回依赖，journal 失败不回滚生活完成 |
| 事件与社交 | **CONFIRMED**：`social_handoff.py:112` completed 才 journal/interest；`life_event_inbox.py:31` 校验 activity.completed、activity revision、双库 committed 回执，`:67` accept → `life_events.py:53` consume_once，原 event/consumer 持久去重后 ACK | **INFERRED**：完成事件只描述已确认经历，不是发送/分享授权；对外表达仍走宿主原规则 |

## 测试与缺口

下表“已有”均为 **CONFIRMED** 的测试代码；“补齐”均为 **INFERRED** 的 A7 验收建议。

| 已有入口 | 已覆盖的断言 | 必须补齐/不能据此证明 |
|---|---|---|
| `tests/test_social_life_execution.py:52` | 真实调度/双库提供方配替身平台；unknown like 仅调用一次，完成落库后中断恢复，journal/interest delivery，曝光与 unknown 回迁保留 | A7 新旧执行器互斥、完整工具会话重启、accepted crash windows、平台查询回执、跨角色相同 ID |
| `tests/test_social_schedule_compatibility.py` | 新旧窗口/顺序行为兼容 | 持久阶段恢复不能重抽卡片、改变 interval 或轮换顺序；新 host 缺能力应 fail closed |
| `HOST/tests/test_social_integration.py:337`、`:389`、`:421` | 真监督器/模型替身执行先读后写，unknown 不重试/不报成功，拒绝浏览不编造经历 | 反馈 engagement 与直接 tool 同目标去重；变更 token/reason 对旧/新摘要的兼容；退出发生在 reserve 前/后、call 后/transition 前 |
| 同文件 `:125`、`:435`、`:467` | 先读约束、视觉图像/单独转述送达 | 来源在工具返回后、送模前、完成前撤回；正文版本变化；图片过期；私有输入→产物→日记不扩大受众 |
| 同文件 `:153`、`:171`、`:184`、`:199` | Google 暂时故障重试、认证失败不重试、国内操作不重试 | 不是实际平台账号/最终远端副作用验证，不代表所有平台 pending 都有同样语义 |
| `tests/test_reading_extraction.py:101`、`:123`、`:174`、`:189`、`:223`、`:391` | A3 等价进度/任务 ID、提交中断、撤权、unknown 只 inspect、命令各断点恢复、事件重投不重读 | 是抽取模板；不自动覆盖 social-action 网络和多轮工具循环 |
| `tests/test_reading_host_recheck.py:79`、`:103`、`:123`、`:155` | settled succeeded 才受结果重验影响；未决 model call 保留 running/unknown；并发 reconcile 原结果恢复、审计不改写 | v2 需验证执行事实与结果可用性独立，终态单调，权限恢复仅恢复可读性，不重发 |
| `tests/test_kernel_lifecycle.py:163` | 本地模拟执行器 unknown 不重启/重发/取消原操作 | 不是真实平台适配器测试；当前没有 parcel.arrived→取件 chain/delay 闭环 |

### 本轮实际本地核查

**CONFIRMED（运行记录）**：Windows Python 3.13，用 `py -m pytest -q tests/test_social_life_execution.py tests/test_social_schedule_compatibility.py tests/test_reading_host_recheck.py tests/test_reading_extraction.py tests/test_contracts_v1.py plugins/astrbot_plugin_private_companion/tests/test_social_integration.py`，PYTHONPATH 指向两插件、tools、tests：**38 passed，26 failed，104 subtests passed**。24 项在宿主 registry fixture 导入时因 `ModuleNotFoundError: No module named 'astrbot'` 失败；另 2 项输出为知乎导入 fixture 缺 `accounts/zhihu/admin-token`，以及 decline 测试期望 `[]` 但实际已有 `[('bili_recommend', {})]`。本轮没有修改代码/测试或安装缺失 SDK，不将这些失败归因于文档，也不宣称其已修复。表中测试名称不能替代本次真实结果；完整 CI 状态在 PR 上记录。

**CONFIRMED（运行记录）**：两份文档的相对链接、显式 GL/HOST 源路径与行号范围、代码围栏配对检查通过。纯文档 diff 另执行 git diff --check；只允许这两份新增文件。

## 按 A3 的抽取步骤与串并行边界

本节全部为 **INFERRED** 的提案，审批前不作为其他实现分支的已冻结依赖。

1. **串行，固定行为基线。** 记录原 parent task ID、activity/request/completion/action ID、旧参数摘要、source/version/coverage、权限和审计；把直接工具互动与反馈互动都纳入样例。保留 `social_interval_minutes`、pace_until、social_first、日历/睡眠窗口、30 秒到 900 秒退避、3600 秒任务上限、推荐采样及 Google 独立重试。恢复不能重抽机会或把模型几秒返回解释成新的活动时长规则。
2. **串行，先落 v2 契约及宿主/内核共用接口。** 按 a0 流程由单一维护分支实现明确协商、材料获取、有限模型工具交换、真实动作提交/查询、本地命令/intent、回执语义、外部结果及链式事件消费适配。必须先通过旧 v1 同步性、在途恢复、拒权/来源和 unknown 兼容测试；不得多代理同时修改这些共享面。
3. **步骤 2 合入 pin 后，可并行做互不写共享面的工作。** 一条线实现生活侧浏览选择/输入组织/多轮状态/反思解析；一条线实现生活内部 chain/delay 和包裹模拟样例；一条线补各自隔离的故障测试夹具。共享存储迁移、host 注册、kernel 接口修改仍归步骤 2 的维护者串行集成；有新字段需要返回契约申请。
4. **串行接通唯一执行归属。** 仿照 `reading_scheduler.py:79` _owner 和 `reading_execution.py:254` claim：先 inspect 原 task/operation，再决定新实例归属，成功开始后持久 claim；一个实例固定 life/host_bridge。旧任务不被新路径抢走；回退/禁用时有 life checkpoint 仍恢复原 owner，不能透传旧 execute_social 重跑。
5. **串行接通 durable 编排。** 仿照 `reading_execution.py:296` execute 和 `reading_selection.py:20` choose：网络前持久化 ID、摘要、版本和阶段，只存受控内容引用；原 ID inspect。明确不存在且从未受理才可用同 ID 提交；unknown/accepted 不重发。每个模型 turn、工具获取、选中卡片和凭证引用都可恢复；子任务首次分配 ID 后持久化，不二次生成。当前进程内 token 不直接复制到生活库；宿主按原来源重验/恢复权限。
6. **串行接通提交/产物/下游。** 仿照 `reading_execution.py:420` 的 command/proof 先保存、`:471` 原决定恢复、`:509` 已 commit 后只投递事件。保留原命令审计/读取屏障和 completion/event ID；已确认活动不因 journal/interest 故障重做浏览。来源撤回前不落稿；提交后撤回走可见性失效及消费者审计，不能改成“未发生”。结果查询耗尽可停止轮询，但不能把未知平台动作标为安全失败/可重发。首次 commit 无法确认时还需要 v2 的 abort-own 能力（见草案 V2-03）：只在宿主确认原命令 abort 后才能准备暂停命令；若已 commit 必须恢复原决定，不能清除 prepared 行强行释放前台。
7. **串行联合验收后，再分批活动包。** 在平台替身上注入 prepare/decision/life commit/host confirm/ACK、reserve/call/receipt 各断点；重启、重绑、撤权、回退仍保留操作/事件身份。只在明确授权的独立集成验收中验证真实平台；此文档任务没有平台写入或上线。移除无调用的宿主浏览专属编排前保留旧在途执行器和兼容转发。

**CONFIRMED**：A3 当前嵌入旧活动账本，不直接转换为 A1 ActivityInstance（`reading_execution.py:1`）；因此本方案也不能以“A3 已完成”为由宣布浏览已接入统一内核。`GL/kernel/registry.py:58` 和 `GL/kernel/engine.py:484` 的限制需先显式解决。

## “包裹链去重、平台 unknown 不重发”的代码落点

| 完成证据 | 当前落点（CONFIRMED） | A7 应交付（INFERRED） |
|---|---|---|
| 包裹链去重，19/T01 | `GL/kernel/engine.py:169` propose 的 opportunity key；`kernel/repository.py:134` 同 dedupe_key 合并原因；`GL/storage.py:107` outbox event identity 去重。`kernel/registry.py:78` 只支持 intent；没有 chain/delay runner，现有宿主 inbox 只收 activity.completed（`life_event_inbox.py:33`）。这些是基础件，不是包裹链已落地 | 生活侧新增内部事件消费者/延迟队列，在同库事务记录 `(event_id, consumer_id, trigger_id)` 消费与唯一候选/预约，保留 causal_event_id、目标 revision、due_at/撤销键；只保留接收端 chain。重投/重启/ACK 丢失只得一个有效取件候选，过期/版本变化不复活旧候选；并测试购买/到货/取件各自事实，不能购买成功就写到货 |
| 平台 unknown 不重发，19/L04 | `HOST/companion_v2/role_work/social.py:475`/`:724` reserve，`:480`/`:731` replayed 跳过，`:490`/`:780` unknown；`HOST/companion_v2/storage/repositories.py:87` 持久身份；`social_port.py:807` 写操作不内部重试；`tests/test_social_life_execution.py:52` 和 `HOST/tests/test_social_integration.py:389` 替身回归 | 宿主真实 platform action port/inspect 原操作，生活侧 checkpoint 引用相同 ID/摘要及查询证据；unknown/accepted/查询失败、旧 owner 切换、撤权、取消、重启都断言外部写调用数不增加。无核实能力继续 unknown/人工处置，不生成新 ID |

**INFERRED**：包裹模拟链与真实平台 unknown 是两组独立验收证据；前者不得借宿主旧 activity.completed inbox 冒充通用 chain，后者不能借 A1 本地模拟 unknown 测试冒充真实平台确认。
