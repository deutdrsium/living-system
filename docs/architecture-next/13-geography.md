# 地理环境与场景系统

依赖：[调度](07-gating-planning.md)、[经济](12-economy.md)、[来源](15-sources-memory.md)。

## 两层空间

常住校园/住宅使用本地图网络（由离线 OSM 生成器一次性生成，用户在本地工具里微调后导入插件，见下文“常住图的生成与维护”），城市范围使用导航适配器。geo 拥有当前位置、地点、图与行程。environment 拥有外部天气观测和可复用环境快照；模拟场景事实单独标来源。

地图提供方不负责决定活动已经到达；全景/照片只提供视觉材料。位置由 travel 执行阶段推进。

## 最小数据结构

Place：稳定 place_id、名称/类型、区域、图节点或外部坐标、坐标系、开放规则、活动能力、来源及版本。
Edge：起终点、方向、预计秒数/距离、负荷、门禁与可用条件。
Gateway：本地图节点与外部坐标/交通模式的映射。
RouteQuote：起终点、出发时刻、分段、预计时长、金额范围、有效期、提供方与依据。
LocationState：at_node 或 in_transit；后者保存 route_id、segment_index、progress、最后确认位置。
SceneMaterial：材料引用、地点/朝向、获取时间、采集时间（可未知）、provenance、授权。

室内楼层和房间可作为节点；精度由源数据和人工修订的质量决定，不能因为来自 OSM 或人工修订就声称测量准确。

## 常住图的生成与维护

决定（2026-11-16，用户确认）：常住区域的图不手工绘制，由独立的离线生成器从 OpenStreetMap 数据程序化生成一次，之后由用户在同一套本地工具里查看和微调，再把图配置包导入插件。

生成器：

- 是仓库里的独立命令行工具（例如 `tools/osm_graph_builder/`），不属于插件运行时。插件运行时不访问 OSM 或 Overpass，只读取生成后的图配置包。
- 输入由用户显式给出：一个小范围的边界框或多边形（一个校园或一个住宅区），以及要保留的道路、建筑和设施类别。范围设上限，超出就拒绝，提示改用离线区域文件。
- 数据获取遵守 OSM 的使用政策，避免滥用：
  - 优先读用户自备的离线区域提取文件（如 Geofabrik 的 `.osm.pbf`）。
  - 在线时只对给定范围发一次 Overpass 查询。请求使用不含个人信息的通用 User-Agent，失败时退避，不自动重试轰炸，不并发，不分块批量拉取，不调用 OSM 主 API 或瓦片服务。
  - 原始响应缓存在本地；再次生成默认读缓存，需要用户显式 `--refresh` 才重新拉取。
- 输出图配置包：Place、Edge、Gateway 带 `source: osm` 和 OSM 元素 id；坐标统一为 WGS84 并标注坐标系；边的预计秒数按步行速度和道路类型估算，并标明是估算。另附一份生成报告，列出孤立节点、无名地点、缺失的门禁或开放信息。
- 许可：OSM 数据按 ODbL 授权，图配置包、本地编辑界面及插件管理页要标注“© OpenStreetMap contributors”。私有使用不需要公开数据，但派生的图不能去掉来源标注。

微调与再生成：

- 微调放在本地工具里做（2026-11-16 用户确认），不要求插件管理页提供图编辑。本地工具提供一个只在本机打开的编辑界面（例如本地网页），用已缓存的 OSM 矢量数据自己绘制底图，不加载 OSM 公共瓦片服务。可以在里面重命名、合并或拆分节点，增加室内楼层和房间，标注地点类型和活动能力，关闭或新增边，修改时间、门禁和开放规则；人工修改的来源记为 `manual`。
  - 补充（2026-11-17，用户选择“默认离线彩色底图 + 可选在线 OSM 瓦片”）：默认仍只用缓存矢量自绘彩色底图；另有默认关闭、不持久化的“在线 OSM 底图”开关，开启前说明会向官方瓦片服务器发送当前视野，仅在本机 `--serve-editor` 页面可用并遵守其瓦片使用政策。插件运行时仍不访问瓦片。
- 编辑结果导出为图配置包，带包版本和校验摘要；插件只负责导入。导入时先校验坐标系、引用完整性和 place_id 稳定性，显示与当前图的差异，确认后生成新版本，不改写已发生的行程和课次。插件管理页只读展示当前图版本、来源标注和导入记录。
- 生成器再次运行时，按 OSM 元素 id 和稳定的 place_id 与现有图对照，输出差异供用户逐项确认，不覆盖人工修改，也不改动已有的 place_id。
- 生成的图只代表地理结构，不代表已经去过；位置仍由 travel 执行阶段推进。

## 路径选择

局部路径先过滤门禁、闭馆、不可通行边，再按时间权重寻路。出校园时比较可用 gateway 的完整路线，不永远只用最近直线距离出口。

组合路线 = 起点局部段 + 城市段 + 终点局部段。报价包含接驳/步行/等待与交通费用；不同提供方坐标在适配器边界明确转换，禁止直接拼接不同坐标系。

门控按下一承诺地点决定离场路线，算完整行程与缓冲。未选中的大量候选不逐个调用导航；缓存可使用起终点、出发时间段、模式与版本键，并保存有效期。

## 移动执行

位置不能在选择目的地时改变。travel 阶段开始后按段记录进度、负荷和已发生车费；到达段终点才更新节点，到达活动入口才进入本体阶段。

中途取消在可停靠点结束；不能瞬移回宿舍。路线失效时从当前节点/路段可停靠点重算。重启按已知路线与经过时间恢复，是否能继续推进取决于实际/模拟模式，不能将未知外部位置当确定抵达。

通勤是父活动步骤。去上课、散步、看展复用 travel 执行能力，禁止 commute.after="*" 的全局链式触发。

## 天气与场景

天气保存地点范围、观测时刻、有效期、提供方、可用性。旧缓存过期返回 stale；没有天气不默认为晴天。雨影响路线可通行性、遮雨偏好和 exposure，但如何影响健康由健康模块决定。

全景时间、天气时间和角色到访时间分别记录。浏览旧全景不证明今天有某只猫或某个人。实时不可得时可使用 authored 场景或明确 simulated 偶遇；缺全景不妨碍合法路线执行。

常驻猫/店员/同学使用少量稳定 entity_id、相关地点和互动历史。世界对象持有位置与出现规则；主插件持有人际长期关系/记忆，双方通过引用关联，不复制完整关系引擎。

## 提供方失败

路线必需且不可得：延期、取消或选用显式手工估计，估计来源和不确定性可见。不能返回 0 秒成功。全景通常 optional，失败则不生成“亲眼看见”的描述。

真实 API 的覆盖、许可、费用与额度由配置者按当前文档核实。路线与视觉材料可来自不同提供方；首版只实现一个路线适配器，避免同时维护多供应商差异。

## 验收

7 分钟校园路线确实经过 7 分钟才到达；关闭一条边后改走合法路径；公交延迟使返程门控重新判断；取消停在合理位置；未知坐标系拒绝拼接；旧全景不生成当前现场事实；路线失败不扣未发生门票。

参考：[高德路径规划](https://developer.amap.com/api/webservice/guide/api/newroute)、[百度全景服务](https://lbsyun.baidu.com/products/panoramic)。不依赖其特定免费配额或校园覆盖承诺。

## A8 首个离线闭环（显式嵌入，默认关闭）

实现入口为 `good_life/city/` 和 `activities/city_trip.py`。`register_city(registry)` 默认不注册、不建表；只有显式传入 `enabled=True`，并在启用的 A1 内核中安装，才创建 `life_city_locations` 和 `city_schema_version=1`。没有新增 AstrBot 配置、宿主 HTTP 路由、页面、定时器、旧调度器接管或线上切换。钱包继续使用 A2 的同一个 `Wallet`；普通安装行为保持原样。

嵌入顺序：注册已有 A2 钱包（可复用 `register_buy_drink`），调用 `register_city(registry, enabled=True)`，以 `CityAccess(trusted_host_access, wallet, city)` 构造现有 `ActivityKernel`。宿主仍负责核验 scope、受众、前台占用与下一硬承诺的 `free_until`；调用前释放宿主锁，沿用单进程、同一个 `LifeStorage` 的前提。可信调用方通过 `city.locate(owner_key, place_id, expected_revision=...)` 显式初始化位置，禁止在活动期间手动改位。此处的 `geo.city_location` 只属于 A8 的独立嵌入实验，不投影或改写 A4 的 `geo.place_id`，也不自动从校园位置推断城市位置。未来将 A4 通勤接入同一位置拥有者需要另行协调，不能同时把两套位置当作同一次行程的权威。

`OfflineNavigation(GraphPackageStore(storage), graph_id, currency=..., external_places=(...), services=(...))` 只从已确认档案读取一次完整图包，固定 graph_id/version/digest/attribution。`preview` 或未确认编辑不能成为路线。常住图内部只走图边，按方向、enabled、节点和边的 access/opening/availability 过滤，再按预计时间寻路。首版可解释的开放值为 `None`、`24/7`、`always`，access 为 `None/yes/public/permissive/designated`；其他自由文本限制保守视为不可用，尚无开放时间表达式解释器。图边步行时长和不确定性是估计，不声称测量精度。

离开常住图时，把图中所有模式兼容的 gateway 和外部手工交通连接一起比较完整时间，包含步行、等待与换乘，不固定使用最近出口。外部地点仅为用户配置的稳定 ID，不执行地址搜索或地理编码。`TransitService` 指定 service_id、起终点、walk/bus/metro、duration_s、cost_minor、uncertainty_s；`departures` 为有时区、严格递增的固定出发时刻 tuple，空 tuple 明确表示用户配置的手工时长估计。常住节点必须是支持该模式的 gateway 才能接外部段；同在常住图内的起终点不能借外部服务绕过图边。图包坐标校验只接受 WGS84，首版没有坐标转换或外部坐标拼接。

Python 端口 `NavigationAdapter.quote(origin, destination, departure_at) -> RouteQuote` 的返回形状为：

- `route_id`、`origin`、`destination`、有时区 `departure_at/valid_until`、`currency`、`provider`、`graph_id/graph_version/graph_digest/attribution`。
- 不可变 `segments` tuple；每段包含 `origin/destination/mode/duration_s/cost_minor/uncertainty_s/source`，以及显式手工事件 `delay_s/cancel_after_s/safe_stop/refund_minor`。固定时刻表产生单独的 wait 段；城市报价总 `duration_s/cost_minor` 为分段求和属性。图边来源含图摘要和 edge_id，交通来源为 `manual-transit:<service_id>`。
- `valid_until` 是报价材料的有效期上限，不授权延后搭乘。首版固定时刻表按报价出发时刻计算，prepare 和首次 start 必须匹配该出发时刻；错过时刻返回 `no_time_window` 并要求重新报价。执行器只接收此离线提供方。可选 `PanoramaPort.material(place_id, heading)` 仅声明接口，没有实现或获取视觉材料，更不生成当前现场事实。

往返调用方先 quote 去程，以去程总时长加显式 stay_s 计算返程出发时间，再 quote 返程；`city_trip_template(outbound, inbound, stay_s=..., reserve_minor=...)` 固定整个计划并注册到现有 registry，通过 `trip_requested` intent 提案。往返必须连通且固定同一图版本；总计划最长七天、单方向至多 256 段，预算至少覆盖所有已报分段车费。prepare、start 和恢复门控均计算完整剩余行程，手工延误已计入时间，不把路由失败伪装为零秒成功。

父实例 start 在内核事务中一次预留整个预算；每个付费子段在实际进入后从相同 `<instance_id>:budget` 扣费，段结束时才确认到达。`refund_minor` 仅由配置事件在该段结束或取消安全停靠时产生独立关联退款，退款不会偷偷扩充预留。完成或取消释放未用预算，保留已发生流水。免费路线不创建零金额预留。位置持久保存 at_node 或 in_transit、分段索引、进度秒数、最后确认节点，在途另存 route_id；选择、报价和 prepare 均不改变位置。

手工事件 `delay_s` 延长该段时长并在进入时保存 `reason_code=provider_unavailable` 与可读秒数/来源说明。`cancel_after_s` 与明确配置的 `safe_stop` 表示交通中止事件，到达事件边界后停在该位置，记录 `reason_code=cancelled`、取消解释、已发生费用及关联退款，后续路段不再执行。用户中途 cancel 则持久保存请求，在当前段下一终点才提交 cancelled；请求返回仍可能为 running，调用方须继续推进，不能把“已请求”显示成“已停靠”。每个事件都带 `simulated` 和原私聊受众，不代表真实车辆观测。

重启沿用固定模板/图/分段/checkpoint、钱包业务键和效果 ID；A1 只恢复已知进度并重新门控，后续在线时间才推进，不将离线间隔补造为现实到达或搭乘。这里的手工时刻表是模拟计划，暂停/重启不会假称实际仍赶上现实车辆。`CityActivities.recover/tick` 是世界侧恢复包装：已消费但错过固定出发时刻的 preparing 行程会生成可解释的取消终态、释放前台槽位且不扣费；普通 `recover` 不会无限重试该实例。若在途行程已有取消请求，执行器的剩余时长只计算到下一安全停靠点，恢复门控不要求原完整往返仍有空窗。终态提案先持久化，钱包、位置、终态及 outbox 随后同事务提交；中断重放原提案。关闭新活动后仍可通过现有 `recover_pending` 完成已保存终态提案。该首版没有运行时交通事件订阅、在途图更新自动改道或实时车辆位置核实；新确认图只影响新 adapter/报价，不改写旧实例。

以上首版离线路径不发送位置信息，没有地图/导航/全景网络请求。后续真实适配器必须定义提供方、授权、覆盖、许可/费用/额度、坐标转换、quote 有效期、缺失与超时处理、事件和取消安全停靠的证据、缓存及恢复策略。外部调用应在纯执行器及数据库事务之外进行，真实位置未知时保留未知。位置、目的地、出发时间和路线偏好会暴露居住/学校/行为规律；启用前须让用户明确选择是否及向谁发送，最小化坐标精度和字段，限制日志/缓存保留、凭据权限及跨角色可见性。用户现已选择高德公交导航，默认关闭的实现与授权边界见下一节。

验证入口 `tests/test_city_trip.py` 覆盖 G01/G04、离线图边关闭后的新报价、出口完整路线比较、固定时刻等待、延误后的往返时间门控（G02 的已知手工事件子集）、L01/L02、E01/E04、预算不足拒绝、取消可停靠点、付款/退款/终态故障恢复及默认关闭回归。全景只有占位接口，因此 G03 不产生视觉现场事实；真实提供方、天气、动态在途重规划、真实宿主及页面集成均未据此验收。

## 结构化门禁与开放时间（默认关闭）

`good_life/graph_access.py` 是 A4/A8 共用的纯本地解释器；显式构造 `GraphAccessPolicy` 或在 `OfflineNavigation(..., settings=plugin_settings)` / `Campus.from_settings` 中读取设置。`graph_access_enabled=false`、空资格和空 private 映射为默认值；没有传入启用配置时保留旧过滤与原报价 wire 形状（含旧版把空规则当可用的行为）。启用后未知开放时间明确为 unknown，默认阻断。

`home-area.graph/v1` 在 Place 和 Edge 上可选添加 `access_requirement`、`access_area`、`opening_schedule`；旧包无需迁移，来源、摘要、确认和历史机制不变，新增字段参与摘要并经过校验。结构化字段是人工确认的优先规则，原 `access/opening_rules/availability` 保留供阅读；选“沿用原文字”会移除结构化字段。

| access_requirement | 规则 |
| --- | --- |
| public | 公众可用，无资格要求 |
| campus_member | 需要 campus_member、student 或 staff |
| student / staff | 需要对应的显式资格，不相互替代 |
| resident_of:<区域或宿舍ID> | 需要完整相同的资格 token |
| closed | 明确关闭；假设配置也不放行 |
| unknown / unparsed | 缺信息 / 不能解析；默认阻断 |

`graph_access_credentials` 是资格列表，默认 `[]`；`graph_private_credentials` 是 JSON 对象字符串，默认 `{}`，例如 `{"my-campus":"student","my-campus/east-dorm":"resident_of:east-dorm"}`。private 必须配置资格映射，按 `graph_id/access_area`、`graph_id/region`（地点未指定 access_area 时）再到 graph_id 查找；边没有 region 时应在编辑器明确填写 access_area 或使用整图映射。没有映射的 private 始终阻断，不能用假设开关绕过。映射值只能是资格 token，不允许 public。凭据集合只保存在本地，不向地图服务发送。

没有结构化覆盖时，原文精确映射：`yes/permissive/public/designated → public`，`no → closed`，`private → 显式映射的资格`；`destination` 以及其他未识别文本均为 unparsed，不能猜测访问目的或身份。仅支持精确的简短中文词句：“仅校内有资格人员可用”→ campus_member、“仅学生可用”→ student、“仅教职工可用”→ staff、“禁止通行”→ closed。带“需授权”“访客联系”“估算、未核实”等附加条件的长段文字不会被提取其中部分当作完整规则。

开放时间示例：

```json
{"opening_schedule":{"status":"known","timezone":"Asia/Shanghai","windows":[
  {"weekdays":[0,1,2,3,4],"start":"08:00","end":"18:00"},
  {"weekdays":[5,6],"start":"09:00","end":"17:00"}
]}}
```

星期一=0，星期日=6；使用 IANA 地区时区（例如 `Asia/Shanghai`，也接受 `UTC`），不接受 `+08:00` 等数值偏移。窗口为本地半开区间，`end <= start` 表示跨夜（相等表示 24 小时），仅结束可为 `24:00`。空 windows 是明确关闭；`{"status":"unknown"}` / `{"status":"unparsed"}` 不得附带猜测的窗口。节点在实际预计抵达/离开时刻检查，边须在整段穿越期间有连续窗口覆盖；等待期间也检查图内起点。DST 不存在或重复的窗口边界只使该窗口可能覆盖的时段成为 unknown，不猜 fold；不影响无关日期或另一个明确窗口已覆盖的时段。没有显式结构化窗口时只识别精确 `24/7/always`；其他 OSM opening_hours、节假日表达式和叙述文本保留 unparsed，空值为 unknown，不从坐标推断时区。

`treat_unparsed_access_as` 和 `treat_unknown_opening_as` 只接受 `blocked/allowed`，默认 blocked。前者只控制 unknown/unparsed 门禁；后者控制 unknown 和 unparsed 开放时间（包括无法解析的开放文字）。allowed 只允许产生带具体条目 ID、状态、所用设置的 `assumptions` 和 `needs_review=true` 的 `GraphRouteQuote` / A4 路线，并随活动持久化。已知关闭、禁用边、缺资格以及缺 private 映射仍阻断；原包不改写。这个实现评估明确出发时刻下的可用通路，不从复杂文字推导节假日、授权或预约规则。

寻路按「地点、连续开放区间」保留最早到达。同一开放区间内可等待，因此无需按秒扩展同一地点，也不靠往返步行消耗时间。如果下一地点会先关闭再开放，可在上游保持开放的地点等候，再于较晚的区间抵达。边须在整个步行期间开放。启用策略后，手工班次会跳过无法按整秒表示的发车时刻，并检查连续等候、登车和抵达；没有固定时刻表的手工服务也可等到一个可用窗口再出发。若配置了中止停靠点，报价还检查预计停靠时刻的图内门禁与开放窗口，假设一并写入报价。等待保存为 `wait` 段并计入 A4/A8 总时长。配置策略的寻路受七天、50,000 个已处理区间状态及 100,000 次最早到达改进的上限约束，超出返回 `provider_unavailable`。A8 往返活动入口仍保留每方向 256 段的执行校验。

编辑器在每项显示解析状态，生成报告列出 `unparsed_access/unparsed_opening`。修改结构化门禁和时间窗口与其他属性一起保存为一次 history transaction；`source=manual` 保证 `--previous` 再生成保留整条记录，撤销一次恢复。CSP、网络端点及默认离线行为不变。生成器默认继续关闭 OSM private 边；只有显式 `--structured-access` 才保留为启用边供资格判断。生成步行图时，出入口和道路的 `foot` 优先于通用 `access`，`foot=private` 仍须匹配映射资格，`foot=no` 仍禁止通行。结构化构建将影响步行的条件标签原样保存在 `access_conditionals`。不会解析条件表达式；只有满足基础门禁且明确允许未解析门禁假设时，含这些标签的记录才可用于报价。编辑器合并地点时，仅合并通行条件相同的平行边。旧包中手工禁用的边需要用户复核并明确启用，资格不会绕过 enabled=false。

验证使用纯合成数据：`tests/test_graph_access.py`、A4 生命周期中的固定版本恢复测试、生成器再生成与页面核心跨语言测试。

## Amap 公交适配器（用户选择，默认关闭）

`good_life/city/amap.py` 的 `navigation_from_config` 是显式 A8 嵌入工厂。`AmapConfig.enabled=False` 或未提供 Key 时返回原 `OfflineNavigation`，不会请求网络、写额度记录或改变手工服务行为。当前这个工厂还不能接收 `GraphAccessPolicy` 或结构化门禁设置，因此经它构造的图内路由和离线回退仍使用旧过滤规则；仅填写门禁配置不会让该工厂启用新策略。需要结构化门禁时应显式构造前述 `OfflineNavigation(..., access_policy=...)` / `settings=...` 或 A4 的 `Campus.from_settings`。工厂的策略透传留待后续改动，本次不修改 `amap.py`。没有变更 contracts v1/v2、内核协议、生产调度器或线上开关。插件的导航与地点搜索开关均默认关闭。显式嵌入调用方负责读取配置、鉴权、请求和提案。

使用高德 Web Service 的 `GET https://restapi.amap.com/v5/direction/transit/integrated`。2026-11-17 核对官方[路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute)（页面标注更新于 2026-06-17）、其链接的 [v3 分段结构](https://lbs.amap.com/api/webservice/guide/api/direction)及[错误码](https://lbs.amap.com/api/webservice/guide/tools/info)。请求必填 `key/origin/destination/city1/city2`，显式发送中国时区 `date=YYYY-MM-DD`、`time=HH-MM`，并请求 `show_fields=cost`；固定控制参数为 `AlternativeRoute=5/strategy=0/output=json`。citycode 如上海 `021`、北京 `010`，不是六位 adcode。稳定外部地点可单独给出目标城市编码；反向请求交换两城市编码。出入常住图仍通过出口，两个已保存的外部地点直接查询公交路线，不经过出口。双向均复用同一报价、预算和恢复流程。

解析 `route.transits[].cost.duration`（秒，含等车），`cost.transit_fee`（CNY 元，按继承的公交价格语义转为分，零头向上取整）。官方 v5 费用表的层级描述有歧义（注明 `transit_fee` 位于 `segments` 下），因此同时支持每个方案的 `cost.transit_fee` 和各有交通的 `segments[].cost.transit_fee` 全部已知时求和；不把多个层级的费用重复相加，不用出租车价或 v3 的字符串 `cost` 冒充公交价。有公交或地铁腿时，缺少任一必需费用且无方案总价，票价为 `None/fare_known=False`，可展示但 `city_trip_template` 拒绝提交。纯步行方案缺少票价时按已知 0 元处理。`walking.duration` / `walking.cost.duration`、`bus.buslines[0].duration` / `.cost.duration` 取步行和车辆腿时间；`buslines` 是同一段的替代线路，不能累加。其 `type` 仅允许已识别的公交/地铁/轻轨，出租车、铁路和未知模式保守拒绝。总时间不能小于已知腿时间之和；总时间与各腿之差保留为等候/接驳位置未知的秒数，不凭空补造班次。详情全部缺失、无方案或解析失败，均返回错误而非 0 秒成功。

常住图只读已确认版本，图内仍使用原寻路和限制过滤。图外比较模式兼容且可达的 gateway，按图内步行时间与 gateway_id 稳定排序，每方向默认查询至多 3 个（可配置 1–5），所以默认完整往返至多 6 个出口查询、含重试至多 12 次 HTTP 尝试；比较「图内步行 + 高德总时间 + 不确定性余量」。这是额度约束下的候选最优，不声称搜索了全部出口。去程在图内步行后请求公交出发时间，返程先请求外部地点到出口再接图内步行；去程首个车辆、返程末个车辆必须与出口模式兼容，城市内部可正常公交转地铁。`aquote_options` 返回排序后的候选；`aquote` 优先选可执行的已知票价方案。失败时只有 `manual_fallback=True` 且有合法配置的 `TransitService` 才显式退回原手工来源；没有任何自动手填时刻表。

### 坐标与位置证据

`StablePlace(place_id, lon, lat, coordinate_system="WGS84", city_code="")` 是用户配置的稳定目的地。图节点、出口、外部地点必须声明 WGS84；未知、未声明或混用 GCJ-02/BD09 均拒绝（G04）。适配器边界将 WGS84 转 GCJ-02，请求坐标舍入至 5 位小数（约米级）；本地转换为大陆常用近似，不声称测量精度或全球服务覆盖。另提供数值迭代 GCJ-02 → WGS84 逆转换并测试参考点。此实现不请求、不保留任何返回几何，所以没有带 GCJ-02 坐标进入本地图或活动记录；未来若保留几何，必须先逆转换并显式标记 WGS84。

高德中间站不是本地图的确认节点，也没有恢复后的可路由位置保证。因此外部部分只执行一个粗粒度阶段，连接出口和稳定外部地点，或直接连接两个稳定外部地点。它保留路线总费用、时间和保守余量；报价 `evidence` 则完整保留已知步行/公交/地铁腿、原始总秒数、票价及未知位置的等候秒数。取消只在这一段的已配置终点停靠，不创建不可恢复的虚构中间站。费用在进入外部阶段时从 A2 同一笔父预算扣除，不能声称精确分车次票价、实际上车时刻或实时站点位置。行程仍是明确 `simulated` 的在线执行，不由高德响应宣称已实际抵达；动态延误订阅、真实车辆位置和中途重新导航仍不在范围内。

### 有效期、故障与恢复

默认每请求总超时 5 秒、至多 1 次重试（可配置 0–2），重试退避 0.25/0.5 秒，取消直接传播。HTTP 408/429/5xx、超时、网络失败及官方繁忙/QPS 错误可短暂重试；坏 Key/平台不符、日额度/余额/授权不足、无效参数、未知模式或无方案不重试。`AmapError` 具有兼容的 `code=provider_unavailable`、具体 `reason_code`（如 `amap_bad_key/amap_timeout/amap_quota/amap_no_route/amap_daily_quota`）、`transient` 和本地中文解释；不回显提供方 `info`、URL 或原始异常。异步 HTTP 复用现有宿主的 aiohttp 模式，禁用重定向和环境代理，响应上限 1 MiB。事件循环调用方必须 `await aquote` / `await aquote_options`，且在内核和数据库事务外执行；同步 `quote` 仅供非事件循环调用方，循环中调用会明确拒绝。

路线缓存仅进程内、最多 128 个规范化结果，TTL 默认 120 秒（上限 300 秒），键为有顺序的路线端点、出口（如有）和出发时间桶（默认 60 秒）。图版本、城市/坐标和模式固定在 adapter 实例内；重启缓存为空，过期时淘汰。不缓存原始响应、几何、线路名、凭据或错误详情。默认每秒 2 个请求槽，等待槽位超过请求超时即拒绝；所有失败、重试和被取消的已授权尝试都保守计入额度。按 Asia/Shanghai 自然日，默认日上限 100；仅「日期、尝试次数」写入 `life_metadata.city_amap_usage`，同一 LifeStorage 的所有 adapter 共用，重建 adapter/重启不能重置；次日才重置，时间回退拒绝。沿用单进程、同一 LifeStorage 的 A1 前提，不宣称跨主机或多进程 Key 级分布式限流；实际购买额度/QPS 仍需在高德控制台核实。

每个新报价固定请求时刻、提供方、原始总时长、票价、腿、图摘要/版本、假设和余量。外部总时间加 `max(120 秒, uncertainty_s, ceil(provider_duration_s * uncertainty_ratio))`，默认比例 20%；余量覆盖分钟分辨率及以下启动窗口，仍是估计，不是保证。为容纳真实 HTTP 耗时，高德去程允许在报价出发时刻之后 60 秒内 prepare/start，且两个方向的材料都必须仍在请求时间起算的 TTL 内；过窗或过期需新报价，preparing 恢复也使用同一规则。旧离线行程维持精确出发时刻要求。返程的 API 时刻仍按原去程预算时长 + stay_s 固定，短暂启动偏差视为估计不确定性，不重新查询、更换班次或假称赶上车辆。

`city_trip_template` 将选定的往返完整报价与分段写入原参数/checkpoint 持久化路径。A2 的预留、分段扣费、释放和重放业务键不变。开始后即使 TTL 已过、开关关闭、没有 Key 或进程重启，恢复只读固定报价与在线进度，不访问高德，不改变已承诺的时间、费用或图版本；新路线调用失败不会产生新扣款。新报价不能覆盖既有实例。存量记录保留受原 owner/context 权限和存储保留策略约束，不进入跨角色共享缓存；本 PR 不新增集中轨迹库或自动日志。

### 公共地点搜索与选择（单独默认关闭）

可信 A8 宿主明确启用 `amap_poi_search_enabled` 后可 `await navigation.asearch_pois(public_place_name, city_code=...)`。范围用三或四位 `citycode`，默认使用配置城市。调用前须确认输入只是公共地点名称。插件自己的网页搜索可帮助辨认名称，网页结果不能用于创建可路由地点。[高德地点搜索 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch) 每次只请求第一页，最多展示十项。结果为 `ok`（有列表）、`empty`（无匹配）或 `error`（带具体 `reason_code`，包括 Key、额度、超时或响应异常）。失败时不会编造候选。

`navigation.select_poi(list_token, index=...)`（从零计数）或 `navigation.select_poi(list_token, poi_id=...)` 只接受该未过期列表中的一项。列表 token 仅供当前授权调用方使用，宿主应限定在匹配的 owner/context 内，不能把它当长期地点 ID。token 在进程内有效 120 秒，重启或被有界缓存淘汰后须重搜。规范化查询结果最多缓存 128 项查询，默认 120 秒，和公交共用同一 Key、限速、日额度及重试策略。只有有效选择才写入本地数据库。保存公共地点名称、原始 GCJ-02 与转换后的 WGS84 坐标、Amap POI id、城市编码和获取时间。已保存的 POI id 在新列表中显示本地保存的名称和坐标，选择时复用该地点和 `place_id`。如果另一份列表在当前列表发出后先保存了不同的位置，当前选择会要求重新搜索确认。原始响应、搜索词、列表和 token 不写数据库。选地点不改变位置或扣费。

### 隐私和配置步骤

接收方是高德 `restapi.amap.com`。搜索只发送经宿主确认的公共地点名称 `keywords`、`region`（高德 `citycode`）、服务器 Web服务 Key 和固定查询控制 `city_limit=true/page_size=10/page_num=1/output=json`。搜索不发送坐标、时间、用户或角色标识，也不发送原始聊天。只有两个开关都开启才发起搜索。公交规划发送「两个端点的 GCJ-02 坐标、起终 citycode、Web服务 Key、计划出发的中国时区日期/分钟」。出入常住图时只发送出口坐标，返程交换坐标和城市。除此以外只有上面列出的固定接口控制字段及普通 HTTP/TLS 元数据（提供方可见出口 IP）。不发送 role_id、user_id、owner/context、姓名、图包/项目/地点 ID、活动内容、钱包余额、偏好、房间或起始宿舍坐标。坐标只使用出口，不发送图内实际起点。提供方自身的保留/许可/收费由配置者核实；本地报价日志不含请求 URL 或 Key。Key 仅取私有插件配置或可信宿主秘密注入，不写入 RouteQuote、activity、checkpoint、outbox、额度或缓存记录；不要把完整配置或 HTTP params 自行记录到宿主调试日志。

1. 打开[高德开放平台控制台](https://console.amap.com/)，注册/登录开发者账号，在「应用管理」新建应用、添加 Key，**服务平台选择「Web服务」**（不是 JS API 或 Android/iOS）。按控制台当前规则开通/确认公交路径规划的权限、许可、价格与配额；参见官方[获取 Key](https://lbs.amap.com/api/webservice/guide/create-project/get-key)。没有作出固定免费额度承诺。
2. 在生活插件的私有配置填 `amap_navigation_key`、`amap_navigation_city_code`；阅读发送字段后才显式将 `amap_navigation_enabled` 设为 true。凭据配置的读取权限须限制在管理员；也可保留 Key 字段空值，由可信嵌入宿主构造 `AmapConfig(api_key=<secret>, enabled=True, city_code=...)`。仅有开关不会安装 A8 执行入口。
3. 显式 A8 嵌入调用 `AmapConfig.from_plugin(plugin_config)` 和 `navigation_from_config(GraphPackageStore(storage), graph_id, currency="CNY", places=(StablePlace(...),), services=<可选手工回退>, config=config)`。以现有 `CityAccess` 核验 owner/context、初始化位置，在事务外 await 去程、按去程总时长加停留时间 await 返程，再用原 `city_trip_template`、registry 和内核提案/prepare/start。所有出发时间必须有时区；往返尽快提交且满足 60 秒启动窗。默认不开启手工回退，可在构造 `AmapConfig` 时明确选择 `manual_fallback=True`。预算必须覆盖已知报价费用。
4. 可先在本地工作树虚拟环境安装 `aiohttp`（AstrBot 宿主本身已提供；独立工具需要 `.venv/Scripts/python.exe -m pip install aiohttp tzdata`），然后运行下面的**手动单请求** smoke。它不在 CI，不导入图、不写活动/钱包，不重试；重复运行仍消耗高德真实额度。Key 通过隐藏输入提示提供，或来自本机秘密环境变量 `AMAP_WEB_SERVICE_KEY`，不要放到命令行、仓库或 PR。

```powershell
.venv/Scripts/python.exe tools/amap_smoke.py --help
# 将下面四个数字替换为你自己的 WGS84 出口/目的地经纬度，先阅读隐私说明。
.venv/Scripts/python.exe tools/amap_smoke.py --origin-wgs84 121.00000 31.00000 --destination-wgs84 121.40000 31.20000 --city 021 --send-to-amap
```

测试入口 `tests/test_amap_navigation.py` 全部使用合成官方形状响应或 mock aiohttp，禁止真实高德请求，覆盖坐标参考点与逆转换、G04、费用缺失、不同费用层级、公交转地铁出口模式、多出口完整比较、候选上限/关边、缓存 TTL/时间桶/方向、日额度跨重启、QPS/时钟回退、坏 Key/超时/重试、网络期间调度器可运行、延迟提交窗口、固定证据恢复、A2 费用、取消后位置仍可路由、off/no-key 回归及日志/数据库不含 Key。未使用真实 Key，因此没有据此宣称真实账号的覆盖、费用层级或服务授权已通过 smoke 验收。

