# W6-0 · 第二阶段运行时接线

代码基线 `c68befa`。基线场景关闭新增功能。第二阶段串通一个授权私聊角色的课表、校园到课、到地点学习、城市出行及管理页。设计承接 [04](04-kernel-lifecycle.md)、[07](07-gating-planning.md)、[11](11-academics.md)、[12](12-economy.md)、[13](13-geography.md)、[14](14-execution-impressions.md)、[16](16-storage-recovery.md)。

## 基线调用图

`GL` = `plugins/astrbot_plugin_good_life/`，`L` = `GL/good_life/`，`H` = `plugins/astrbot_plugin_private_companion/`。行号按基线。领域归属见 [A0 边界图](a0-boundary-map.md)。

| 路径 | 已有调用与断点 |
|---|---|
| 启动 → 绑定 → 推进 | `H/main.py:214,518` 发布 facade、启动应用 → `GL/main.py:37–85` 创建 `LifeRuntime`、检查 cutover、监听重绑，只传两个阅读开关和学业开关 → `L/runtime.py:99–144` 注册同步 provider、取宿主能力、启动 `DueSleepScheduler`。`L/runtime.py:39–70,181–185` 的 A1、钱包、A6 与显式 tick 没接到启动或调度。`H/astrbot_host/life_host_capabilities.py:28` 另查应用 running 和执行归属。 |
| A3 协商 | `L/runtime.py:110–144` 可选 A3 端口失败时会撤销整个 provider。`L/reading_scheduler.py:79` 已按执行归属分流，`L/sleep_scheduler.py:166` 起保留旧恢复链。 |
| 课表 → 忙闲 → 到课 | `L/web_api.py:87–106` 已代理学业 API。`L/calendar_records.py:18–43` 读旧日历及确认屏障，`H/companion_v2/agenda/calendar.py:169–172` 的角色计划未合并课程。`L/academics/service.py:247–260` 只取尚未结束的课次，`L/academics/coordinator.py:115–172` 只在有位置和路径时提案。缺未开跑课次的到期结算。现有参与唯一键和围栏在 `L/academics/service.py:262–294`。 |
| 学习 → 进度 | `L/runtime.py:171–179` 有显式 `propose_study`，启动和调度都未调用。`L/academics/study.py:103–148` 按截止和分块找任务、核验材料，缺共享窗口分配。`L/activities/study.py:20–43,93–111,135–163` 仅计模拟工时，缺地点、工作步骤和产物。 |
| 导航 → 位置 → 钱包 | `L/academics/campus.py:93,124` 与 `L/city/domain.py:35,72` 各自持有位置。`L/runtime.py:44–61` 缺 `register_city` 和城市协调器。`L/activities/city_trip.py:189,286,357` 已处理固定报价、停靠和恢复。`L/city/amap.py:330,370,491` 基线入口依赖预置点与网关。`L/activities/buy_drink.py:228` 同时装钱包和购买模板，W6 仅需 A2 钱包。 |
| 总览 → 诊断 | `H/astrbot_host/life_dashboard.py:24,50` 核验配对和目标。`H/astrbot_host/life_page_reads.py:20–66,93–108` 过滤至阅读、睡眠、浏览。`GL/pages/我会去好好生活/app.js:269` 从 `/life`、`/state` 及日历取数。 |

`w6/class-attendance` 提供迟到/缺勤模型和补学输出。`w6/amap-poi-destinations` 提供 POI、外部直达和 Amap 修补。`w6/overview-statuses` 提供总览状态、可扩展原因码表和渲染。下文定义调用点及验收，接线复用各分支合入后的接口。

## 装配与调度

| 接线点 | 决定 |
|---|---|
| 宿主能力 | 在 `H/astrbot_host/integration_registry.py:279` 的能力入口旁新增独立协商，返回当前租约绑定的 `KernelAccess`。使用宿主 authority、应用运行状态、`good_life` 执行归属及私聊绑定校验 scope、动作和 audience。owner 使用与学业管理同源的 envelope 摘要。旧 `contexts()` 仅作查找线索。每次调用复查 generation。 |
| 生活插件 | `initialize → bind` 先就绪核心 v1 provider 和原调度，再按需装可选组。一个 `LifeStorage`、显式注册表、一个 A1 coordinator。按依赖装共享位置及确认图 `Campus.from_settings`、`register_academics`、A2 钱包、`register_city(enabled=True)` 及 A8 wrapper，合成领域快照。钱包页持有明确的 wallet 引用。按可信位置证据初始化或经带 revision 的命令确认起点。 |
| A3 隔离 | reading execute、已有阅读 recovery、abort-own、study material 和 W6 各自绑定、诊断和有界重试。可选失败保留核心。未确定新执行归属的阅读走原宿主兼容路径。已有 A3 实例保留原归属、ID、未决回执与屏障，端口恢复后继续。浏览继续推进，同 owner 的未决屏障和占用只暂缓冲突项。 |
| 单一循环 | `LifeRuntime.bind` 每租约起一个可选 W6 worker，通过 `advance_kernel(now=…)` 编排。按结算及到期 → 承诺与课次 → 学习/出行提案 → 一次 A1 tick → 投递推进。原调度负责旧功能。锁外异步预检设并发、截止和退避上限，落地前重验租约、目标及 revision。解绑先失效新工作，再取消并等 worker/预检结束，关闭组件和注销。 |
| 时间与并发 | 统一注入 UTC 墙钟和单调进度钟。遵守 `L/kernel/gating.py:36–55` 的 `life → host` 锁顺序。回调只同步本地读，避免递归借用 provider 或重开 life 写事务。旧 staged 准入与 A1 start 在同一 life 写事务互查 owner 的 active/prepared 和确认屏障，覆盖手动/调度入口。快照版本来自事实版本，未知忙闲拒绝新活动。 |
| A6 | 独立 `state_model_enabled`。从可信 legacy body key 及旧观测初始化，按已结算睡眠和活动区间以稳定 ID 输入。A6 只作影子快照，旧状态仍由原链写入。sleep/energy 各有开关。energy 关闭用 A5 固定规则，启用却缺有效精力时拒绝新学习。基线 `L/state_model/config.py:93–108` 无 health 子项，身体不适仅取可信旧健康输入或将来实际提供的 A6 health。 |

## 一条场景的执行规则

| 能力 | 计划、执行及恢复 |
|---|---|
| 课次占窗 | 在生活侧日历投影合并有效 occurrence、旧日程、睡眠、已认领活动与预留，计算区间并集与补集，给 `free_windows` 和 A1 `free_until`。课程最高优先，始终预留课堂，另反推合法出发和缓冲。经验证的到课候选仅可使用自身标识且当前 revision 的课次预留，其余冲突进两次门控。学习与出行算去程、本体、去下一承诺或返程中适用的一条，加缓冲。未知硬边界保守缩窗。停调课淘汰旧提醒、预留和报价。 |
| 到课、唯一缺勤 | `ClassActivities` 调 attendance 的醒来检查、到达与下课接口，校园段走确认图。结束边界先扫尾已认领实例，再扫描课堂接线已启用期间、已过下课但未认领的课次。收口在 academics 同一事务检查当前 revision、非停课和无现有参与，再以 `(owner, session_id)` 和稳定业务键记唯一缺勤。start 认领与收口共围栏。无图、无位置、忙碌或离线也能收口，证据不足用通用 `missed`。 |
| 迟到与缺勤模型 | 预留阶段保持最高优先。组件开关启用后，在早课出发前调用持久化决定接口。每课次固定种子，配置开课时段、昨晚入睡及连续早课权重。默认正常早八约每 2–3 周一次迟到或缺课，晚睡更高，下午接近零。读取既有睡眠/日程，A6 sleep 开启时取有效观测，避免与 `L/sleep_physiology.py:101` 的实际延长重复累计。轻微晚起或路上耽搁记部分参与，过下课记缺勤。原因随结果投影，重启/改道复用决定。组件开关关闭仍执行确定性收口。迟到或缺勤按 session + follow-up kind 交一项虚构补学笔记或复习待办给 A5。 |
| 共享位置 | 采用 A8 `at_node/in_transit` 作为单一写入模型，保存 owner、revision、最后确认节点以及 route/segment/progress，覆盖校园、网关、外部地点。A4 `geo.place_id` 是同一位置的校园投影。用可审计来源一次性初始化，冲突或无映射由人工确认。保留旧表、旧 executor 和效果，以 adapter 续结算。走路共享导航及分段进度，阶段提交才移动。 |
| 延误、取消和重排 | 已走阶段和费用按原回执结算。到可确认的安全停靠点后，从该点和位置 revision 重报未走段并重验承诺。高德粗粒度段没有可证实中间站时用确认的段端点，停靠未知时保留 unknown 并阻断依赖项。明确取消后重排下一承诺或等待新意图。关联旧意图的稳定新行程及独立预留保留原账，释放旧预留未用部分。 |
| 到地点学习 | 在 W6 协调器锁外调用版本化 `LifeRuntime.propose_study`，配置该角色可达学习点。复用校内导航 `walk → work → checkpoint`。按 `(due_at,assignment_id,chunk_index)` 从同一空闲窗先扣交通及离场，再逐块分配工作，剩余不足及零窗显式报告。以 assignment/chunk/步骤为持久游标。首版按课程主题、提示和预置虚构材料生成可读草稿段、笔记和复习题，版本化规则和产物、进度同库提交。时间给工作量上限，产物证据推进步骤。关联材料在生成前、提交前和读取时重验来源及权限。撤回阻断受影响产物，恢复从原步骤接着写。 |
| 城市意图 | 可选网页搜索 → `await asearch_pois(public_place_name, city_code=…)` → 模型从返回列表选择从 0 开始的索引 → 服务端 `select_poi(list_token, index=…)` → 把所选稳定 `place_id` 交给行程规划与 `aquote`。查询仅发送公共地点名及城市码。`ok` 才展示裁剪候选，`empty/error` 带原因且不生成行程。调度侧仅在可信进程内暂存 `list_token`，按租约 generation、owner/context、角色和意图绑定到选择会话，验证 `expires_at` 与当前权限。模型只见候选及索引。一次选择后清除 token，解绑或重启需重新查询，持久化的是所选地点、高德 POI id 和坐标来源。外部→外部直接公交报价，跨校园边界检查网关及接驳时刻门禁。未知坐标系、票价不明或过期报价拒绝执行。复用 A2 预留、阶段扣费及最终门控。Key 只取服务端私有配置或秘密注入。 |

## 管理 API 与待审契约

路由共用 `/astrbot_plugin_good_life/targets/<target>/<role>`，宿主检查登录的 Dashboard 身份、当前配对、私聊目标、可管理角色，导出可信 owner/context。GET 仅做白名单投影。

| 用途 | API |
|---|---|
| 课程录入页，UI 后续交付 | 复用 `GET /academics/term`、`GET /academics/timetable?term_id=…`、`GET /academics/occurrences?term_id=…&start=…&end=…`、`POST /academics/command`。维护学期、教学周、假期/补课、课程、时段单双周及按日停调课。命令 `{command_id,expected_revision,action,kind,id,record?}` 使用 workspace 顶层 revision。成功刷新课次，`stale_version` 保留草稿并重读，校验、权限和依赖错误分别提示。字段按 [11 页面 API](11-academics.md#页面-api)。 |
| 教室选项 | 新增 `GET /academics/places` 返回 `{schema_version,graph_version,items:[{place_id,label,kind}]}`，仅列该角色可选的已确认可进入地点，换图刷新选项。旧引用可标失效，执行时重验通行。 |
| 学习产物 | 沿用 `/academics/study_progress?term_id=…&course_id=…`。新增 `GET /academics/study_artifacts?assignment_id=…&limit=…&before=…` 与 `GET /academics/study_artifacts/<artifact_id>`。列表 `{schema_version,items,next_before}` 含 ID、assignment/chunk、kind、标题、revision、更新时间、来源和可用性。详情附纯文本及下一步。按 workspace 鉴权，拒绝时不暴露条目存在性。学习页复用此读取入口打开产物。 |
| 总览 | 给 overview 组件的 `/life` 扩展提供课次、学习、出行的阶段、状态、原因码、时间和模拟来源。包括 paused/failed/cancelled/unknown、课次迟到与缺勤、候选拒绝及能力未就绪。取消请求尚未安全停靠时展示在途。白名单过滤原始异常、凭据、材料正文和内部操作号。 |

| 待统筹确认 | 变化与兼容 |
|---|---|
| C1 宿主装配 | 新增独立可选的版本化 in-process 协商，暂名 `life_kernel_access(plugin, registration_id, version=1)`，返回 `KernelAccess` 的 `scopes/authorize/snapshot/audience`。旧宿主缺入口仍可运行基础能力。 |
| C2 页面及领域记录 | 核准上述新 Dashboard 操作/响应、总览桥接、共享位置及学习本地域 schema 和 executor 版本。原 checkpoint 按原版本恢复或显式转换，保留旧参与唯一键与回执。新原因按组件码表映射现有 `ReasonCode`。 |

继续使用冻结 [03](03-shared-contracts.md) 的 `ExecutionScope`、`Effect`、`RecordRef` 及既有材料端口。C1/C2 提案保持 strict `companion.life.v1` 同步 handler 和 async v1/v2 旧 wire。若实际需要修改冻结字段、枚举、端口或语义，统筹审批后先合独立契约 PR，再让依赖项 pin 新版本。恢复使用原 ID、摘要、可见性和 unknown 语义。见 [变更流程](a0-contract-change-process.md) 和 [迁移](contract-v2-migration.md)。

## 实施 PR 顺序

所有新增开关默认 false。并行组件可独立开发，依赖指接线合入顺序。验收编号见 [19](19-acceptance.md)，分包原则见 [18](18-implementation-roadmap.md)。

| PR 与前置 | 范围及文件 | 开关 | 测试与验收 |
|---|---|---|---|
| W6-1 宿主注册及 A3 隔离（C1） | `GL/main.py,_conf_schema.json`，`L/runtime.py,kernel/gating.py,staged_activity_commands.py`，`H/astrbot_host/integration_registry.py,life_host_capabilities.py` 和 adapter。单循环、租约、分组绑定和跨账本前台。 | `kernel_enabled`，既有 reading/abort-own 独立 | `test_life_provider_binding.py`、`test_life_runtime.py`、`test_contracts_v2_coordination.py`。可选组失败、热重载、跨账本并发、原回执 ID（B02/B05/B06、L04、R01/R05）。 |
| W6-2 共享位置及校园导航（W6-1） | `L/academics/campus.py,access.py`、`L/city/domain.py,navigation.py`、`L/graph_packages.py,runtime.py` 及共享 travel。确认图、位置迁移、分段停靠。 | `campus_enabled`，原图及门禁配置 | `test_graph_access.py`、`test_city_trip.py`。走满 7 分钟、位置冲突、旧 checkpoint、当前停靠点重算（G01/G02/G04、L01、R03/R04）。 |
| W6-3 日历与到课（W6-2 + attendance） | `L/calendar_records.py,calendar_scheduler.py`、`L/academics/coordinator.py,service.py`、`L/activities/class_session.py`、`L/runtime.py,state_model/`。最高课程优先，占窗、接 attendance 唤醒/下课、出勤及补学投影。 | `academics_enabled` + `class_enabled`，组件的缺勤机制开关，`state_model_enabled`/sleep 可选 | `test_academics_domain.py`、`test_academics_lifecycle.py`、`test_state_model_runtime.py`。调课版本失效、准时、晚醒、延误、无重抽及唯一缺勤（T04/T05、A02、G01/G02、R02、S01/S08）。 |
| W6-4 到地点学习及产物（W6-3，C2） | `L/activities/study.py,academics/study.py,runtime.py,web_api.py`、`H/astrbot_host/life_dashboard.py` 和学业读代理。步行、空窗、工作游标和可读产物。 | `study_enabled`，A6 energy 可选 | `test_study.py`、`test_academics_routes.py`。双截止、步行、续写、零窗、撤回及横向鉴权（A01/A03/A04、G01、S04/S06、P01/P02、R03、U01/U02）。 |
| W6-5 钱包与城市运行（W6-2/3 + POI） | `L/runtime.py,economy/,city/`、`L/activities/city_trip.py`。注册钱包及城市，调用 POI/报价组件，从停靠点改道，按段结算。 | `wallet_enabled` → `city_enabled` → `amap_navigation_enabled` / `amap_poi_search_enabled` | `test_city_trip.py`、`test_amap_navigation.py`、`test_wallet_purchase.py`。外部直达、无效目的地、门禁、取消保留支出及预算守恒（T05、G02/G04、L01/L02、E01/E02/E04、R03）。 |
| W6-6 管理接入、联测与回退（W6-3/4/5 + overview，C2） | `L/web_api.py,runtime.py`、`H/astrbot_host/life_page_reads.py,life_dashboard.py`、地点选项及诊断桥，`tests/test_w6_scenario.py`、使用教程 §3。接学习产物列表/详情到管理页。 | 复用上述开关及 Dashboard 鉴权 | 新联测、`test_life_dashboard.py`、`test_academics_routes.py`。可见状态原因、权限拒绝、全关及带数据回退（B05、R01/R04/R05、U01/U02）。U03/U04 留给另行授权的真实页面验收。 |

## 端到端联测

从两插件入口与 Dashboard API 驱动，注入 UTC/单调模拟钟、固定种子、合成校园图、假 Amap HTTP 的 POI/公交响应与错误。保留真实注册、SQLite、授权、门控和 executor。重启复用原库，只调用本地替身。

| 场景 | 断言 |
|---|---|
| 全关、重绑及手录 | 日历、睡眠、阅读、浏览可用，启动和 GET 无新增 schema。注入 A3 协商失败，核心仍就绪。切测试开关，手录学期及课次后调课，旧 revision 与提醒失效，空窗仅计新承诺。两会话只起一个前台。 |
| 到课 | 走满 7 分钟。固定种子覆盖严重晚醒缺勤、轻微晚醒迟到、途中延误迟到和准时。决定持久后中断，重启及重放仍同一决定/结果。另测未启动且跨下课、缺位置、同刻 start 竞争和停课。前两例各一条缺勤，停课无缺勤。 |
| 学习 | 双截止分配总工时不超同一可行窗。先走再写，保存草稿段后中断，从同 chunk 和步骤继续。打开草稿、笔记、复习详情。撤回和跨 context 拒绝相应读取，补学仅一项。 |
| 城市 | 校园→外部从假 POI 列表选择。外部→外部时 HTTP 直接用两个外部点。列表外 ID、过期选择、未知坐标系、空集、超时及缺票价都不移动不扣款。付一段后延误、取消再重启，下一报价从确认 stop 起步，原扣费一次，未用预留释放。总览有取消原因、非阅读失败和注入的 unknown。 |
| 恢复与回退 | 在原提案提交、宿主决定/确认及事件 ACK 处中断，核对原 ID、结果、游标与屏障。关开关后仍可提交已知原提案并保留 unknown。分别用始终全关及已产生事实的库演练配对回退。固定种子跑 7 天查位置、钱包、课程和截止，多种子跑 30 天查早晚课分布。 |

## 启用、回退及产品决定

| 阶段 | 放行条件 |
|---|---|
| 配对部署，全关 | 配对代码回退演练及旧功能回归通过。新增键纳入发布门禁。全关启动无新增 schema、Key 读取或导航 HTTP。 |
| 课表、校园、到课 | `academics_enabled` → `kernel_enabled` → `campus_enabled` → `class_enabled` → 缺勤机制开关。逐项核对课表、租约、确认图及起点、空窗、出勤链。 |
| 学习 | `study_enabled`。验证步行、窗口和可读产物。新版阅读和 abort-own 可保持关闭。A6 可后启用，分别验证初值、sleep/energy。参数和开关变更按 A6 迁移。 |
| 城市 | `wallet_enabled` 核对余额 → `city_enabled` 验证离线路线 → `amap_navigation_enabled` 验证高德公交 → `amap_poi_search_enabled` 验证选点、外部直达、费用及停靠。两项由 `AmapConfig.from_plugin` 分别读取。参照 [使用教程 §3](../user-guide/使用教程.md#3-功能开关怎么用) 一次变更一项。 |
| 回退 | 先停新意图及高德请求，保留原 executor、诊断和只结算路径 `L/kernel/engine.py:600–608`。全关且从未装可选 schema 时走现有守卫。`tools/deploy/deploy_host.py:1056–1064` 在已有可选表或 A3 历史时会拒绝直接换旧代码。先审阅并演练保留新表、回执、游标和未决项的配对回退。保全路径未就绪时保持现有配套代码、停新功能。禁止用旧快照抹掉新增记录。 |

**产品待定**。启用日期、试用角色和允许向高德发送的地点范围由用户确定。本地实现先用一个授权私聊角色和合成地点。技术决定已落在各节，包括首版规则生成可读虚构产物的实现及安全停靠所需证据。以上为设计和验收计划，运行结果由各实施 PR 填报。
