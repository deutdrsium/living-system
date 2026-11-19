# OSM 常住区域图生成器

独立、默认离线的图生成与本地微调工具，对应 `docs/architecture-next/13-geography.md` 的“常住图的生成与维护”。插件运行时不导入本工具，也不请求 OSM、Overpass 或瓦片。现有调度、位置、课次、行程、执行权配置和管理页没有接入本功能。

所有输出及编辑页面保留 **© OpenStreetMap contributors**，许可标为 **ODbL-1.0**。地图只表示地理结构；坐标与步行秒数不代表实测精度，也不证明到访。

## 从本地文件生成

在仓库根目录运行，XML 路径只使用 Python 标准库。以下坐标是示例，请显式换成自己的范围；边界框顺序为 **南、西、北、东**。

```bash
py -3 -m tools.osm_graph_builder \
  --source /path/to/region.osm \
  --bbox 30.999 120.999 31.003 121.003 \
  --graph-id my-home --output /path/to/home-v1.json
```

也可以给中心点（纬度、经度）和米制半径。范围取外接矩形，首版不做圆形裁剪或多边形输入。

```bash
py -3 -m tools.osm_graph_builder \
  --source /path/to/region.osm.pbf \
  --center 31.001 121.001 --radius-m 300 \
  --graph-id my-home --output /path/to/home-v1.json
```

PBF 解析器为可选的**工具专用依赖**，没有添加到插件依赖中。需要时安装到单独虚拟环境。在 Windows 上，`py -3` 即使激活 venv 也可能仍指向全局 Python，安装和执行应使用 venv 的解释器。

```bash
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -r tools/osm_graph_builder/requirements-pbf.txt
.venv/Scripts/python.exe -m tools.osm_graph_builder --help
```

结构化开放窗口使用 `Asia/Shanghai` 等地区时区时，Python 需要 IANA 时区库。Windows 等缺少系统时区库的环境可按需运行 `.venv/Scripts/python.exe -m pip install tzdata`。`UTC` 和原文字段的 `24/7`、`always` 不需要额外依赖。

道路默认保留 `footway,pedestrian,path,steps,residential,living_street,service,unclassified,tertiary,secondary,primary,track`；建筑、设施默认全部保留。可使用：

```text
--roads footway,path,steps
--buildings yes,dormitory,university
--facilities cafe,bench,shop:convenience
--walking-speed 1.4
```

设施类别检查 `amenity/shop/leisure`，可以写值或 `key:value`。`*` 表示全部，空字符串表示不保留这一组。道路类别用显式列表，没有 `*`。`foot=no` 和道路面 `area=yes` 不生成可走边；步行方向优先读取 `oneway:foot`（包括显式 `no` 覆盖）；没有该标签时，`footway/path/pedestrian/steps` 遵守 `oneway`（含 `yes/-1`），其他车行道路的 `oneway` 不限制步行。已知 `access/foot=no/private` 的道路或端点使边默认关闭。门禁规则仍需用户复核，工具不声称已知真实通行权。

## 缓存与一次在线获取

默认缓存目录是用户主目录下 `.cache/osm-graph-builder/`，也可 `--cache /path/to/cache`。缓存按 WGS84 bbox 定址，包含范围内的提取矢量和标签；分类筛选在缓存之后执行。本地大区文件只缓存提取结果，不复制整份 Geofabrik 文件。XML 采用流式分遍扫描，PBF 使用 pyosmium 分遍扫描。

**已有相同范围缓存时，即使提供了 `--source` 或 `--overpass`，也优先使用缓存。** 本地源文件改变、换了文件，或需要重新在线获取时，必须加 `--refresh`。缓存可在原始本地文件删除后使用。

```bash
# 不提供数据来源，重用已有缓存（不会联网）
py -3 -m tools.osm_graph_builder --bbox 30.999 120.999 31.003 121.003 \
  --graph-id my-home --output /path/to/cached-v1.json

# 显式允许首次单次 Overpass 获取
py -3 -m tools.osm_graph_builder --overpass \
  --bbox 30.999 120.999 31.003 121.003 \
  --graph-id my-home --output /path/to/online-v1.json

# 只有显式刷新才再次请求网络
py -3 -m tools.osm_graph_builder --overpass --refresh \
  --bbox 30.999 120.999 31.003 121.003 \
  --graph-id my-home --output /path/to/refreshed-v1.json
```

在线 bbox 上限 **25 km²**，超出拒绝并提示使用离线区域文件。离线文件没有这一面积上限，但仍只提取显式范围。纬度限制为 -85 到 85 度，不跨日期变更线。实现只向固定的 `https://overpass-api.de/api/interpreter` 发一次 POST：无并发、分块、自动重试或重定向，User-Agent 固定为 `osm-graph-builder/0.1`，请求只含类别和 bbox。生成器绝不调用 OSM 主 API 或任何瓦片服务器（编辑器的在线底图是另一个默认关闭的显式开关，见下文）。

这一次查询同时取彩色底图用的 `landuse`、`natural`（水面、树林、灌丛、草地等）、`waterway`、`water` 和带名称的 `place` 节点。它们只进入 `vector_data` 供显示，**永远不会成为图节点或边**，也不改变已有地点/边的 ID 或内容。穿过 bbox 边缘的底图面和水道会读取范围外顶点（每次提取最多 50,000 个），再裁剪到 bbox；建图仍只用范围内顶点。

**旧缓存没有这些图层。** 加入本功能前生成的缓存仍可直接使用，但 Overpass 缓存里没有绿地/水面，本地文件缓存缺少跨边缘区域的外部顶点。想要完整彩色区域，请对同一范围加 `--refresh` 重新读取本地文件（`--source ... --refresh`）；当初用 Overpass 获取、手头没有数据文件时，用 `--overpass --refresh`，这会重新发一次网络请求，并受上述冷却期限制。`build_report.basemap` 记录底图要素数和因缺顶点而跳过的数目。

成功在线响应原文保留在对应缓存目录的 `overpass.osm`，解析后的提取结果为 `extract.json`。所有在线获取或解析失败都在缓存根目录 `overpass-backoff.json` 记录最少 60 秒的全局持久冷却期，并尊重更长的 HTTP `Retry-After`；更改 bbox 或改用中心点/半径不能绕过，错误消息显示实际剩余秒数。失败不自动重试，也不替换上一次可用提取缓存。响应上限 64 MiB。网络函数可注入 mock，测试不会真实访问网络。XML 禁止 DTD、实体声明及 NUL/UTF-16/UTF-32 数据。

## 本地编辑

可以直接在现代浏览器中打开此目录的 `editor.html`；若浏览器不支持文件页面上的 Web Crypto，使用仅绑定 loopback 的小服务器：

```bash
py -3 -m tools.osm_graph_builder --serve-editor --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

服务器仅返回编辑页面，没有文件读取 API、写入 API 或管理路由。用页面的文件选择框打开图包，再导出新文件。页面无第三方框架、外部脚本或字体，默认不发任何网络请求。CSP 为 `connect-src 'none'`，`img-src` 只额外放行 `https://tile.openstreetmap.org`，仅供下述可选在线底图使用。

**彩色底图（默认，离线）。** 完全由图包的 `vector_data` 绘制：道路按 `highway` 类别（主干/次干/支路/服务路/人行步道/台阶等）区分颜色、宽度和描边，大致仿照 OSM 标准样式，宽度随缩放按真实米数变化；建筑填充；绿地（`landuse=grass/park/forest/meadow`、`leisure=park/garden/pitch`、`natural=wood/scrub` 等）、水面和水道着色；设施用按类别着色的小菱形。名称优先 `name:zh`，其次 `name`，地名、区域、建筑、设施、道路依缩放显示，并做屏幕空间的贪心避让和同名去重。可编辑的图（蓝色圆点、带白描边的蓝线、黄底黑框表示已选）始终画在底图之上。工具栏可隐藏底图、调整底图不透明度。旧图包没有彩色区域时页面会提示用 `--refresh` 重新生成。

**在线 OSM 底图（可选，默认关闭）。** 开关“在线 OSM 底图”从不持久化，每次打开页面都为关闭。打开时先弹出说明：当前视野（大致位置与缩放级别）和网络地址会发送到 OpenStreetMap 官方瓦片服务器，官方服务器只允许轻量使用；确认后才加载。开启期间地图左上角显示指示条，右下角显示“© OpenStreetMap contributors”。遵守 [OSM 瓦片使用政策](https://operations.osmfoundation.org/policies/tiles/)：只用标准地址 `https://tile.openstreetmap.org/{z}/{x}/{y}.png`；只加载当前视野内的瓦片外加 10% 视野边距；缩放级别向下取整（瓦片按 1–2 倍显示），张数上限按地图窗口大小计算，超出时暂停在线加载；缩放、平移或改变窗口后约 200 毫秒才加载，不请求中间级别；不预取、不批量下载、不离线保存；最大缩放 19，更近时放大 19 级瓦片；由浏览器按 HTTP 头缓存。瓦片要求有效 Referer，所以此开关只在 `--serve-editor` 的 localhost 页面可用；直接以 `file://` 打开时开关禁用，并提示如何启动本机服务器。为与瓦片精确对齐，编辑器始终使用 Web Mercator 投影显示（只影响显示，图包坐标仍为 WGS84）。

- 支持缩放、平移、可键盘选择的数据表和选中项说明。地图上的地点和道路可用 Tab 聚焦，Enter 选择，空格加入/移出多选。
- 选择或拖动地点，编辑名称、类型、区域、WGS84 坐标、能力、楼层/房间、门禁和开放规则。
- 点击添加地点，用“连接两个节点”新增边；可编辑秒数、方向、通行状态和负荷说明。
- 可删除地点/边、合并两个地点的拓扑、拆分边并插入地点。删除地点会同时移除关联边和 gateway；合并保留目标地点 ID，并将被移除地点及连接变化记入历史。
- **框选与多选**：“框选”模式拖出矩形，选中框内地点；勾选“框选包含道路”时也选中两端都在框内的边。Shift 拖动为追加，Ctrl/Cmd+点击单独加入或移出一项，Esc 清除。数据表每行有多选框，另有“勾选全部筛选结果”（例如筛选“宿舍”后一次勾选）。地图右上角和侧栏显示“已选 N 个地点、M 条路”。
- **批量删除**：删除全部已选项及其相连的边和 gateway，作为一次修改：一条历史记录包含全部 before/after，一次撤销整体恢复。影响超过 3 项时先确认并显示数量。
- **批量合并**：把已选地点合并为一个。目标可以是已选地点之一（保留其 ID、OSM 身份和原内容），或在选区质心新建人工地点（填写名称与类型，`merged_from` 记录来源）。连到被合并地点的边改接到目标；两端都在合并集合内的边和自环删除；改接后同一对端点、同方向含义、同启用状态的平行边只保留预计秒数最小的一条，标记 `estimate.model: manual-merged/v1`、`needs_review: true` 和 `collapsed_edge_ids`，并列入报告 `edges_needing_review`。只有实际改接的边（及平行组保留的那条）变为 `source: manual`，其余边保持原来源。被合并地点作为删除记录（`after: null`）留在历史中，因此重新生成不会把它们带回来。历史条目另附 `note` 摘要。单个合并模式复用同一逻辑。
- 合并遵守 `graph_packages.py` 的身份规则：OSM 身份从不转移到另一个 place_id（否则违反“同一 OSM 地点不换 place_id / place_id 不换绑身份”），所以被合并地点连同其 OSM id 一起作为删除处理，新目标地点 `osm_ids` 为空。
- **出入口（gateway）**：选中地点后点“设为出入口”，在地点当前坐标新建 `source: manual`、`osm_ids: []`、ID 为 `manual:gateway/<uuid>` 的 gateway，默认 `modes: ["walk"]`；勾选框切换 walk/bus/metro（按此顺序保存），最后一种已知方式不能取消（勾选框禁用，核心也拒绝并给出具体提示）；同一地点有多条 gateway 记录（例如合并两个门之后）时，编辑器按模式并集把它们当作一个出入口，第一次改方式时合并为一条记录，优先保留 OSM 门的 `gateway_id` 与 `osm_ids`，其余记录成为删除记录；出入口栏位于属性表单之外，改方式不会冲掉表单里尚未保存的字段；“取消出入口”删除该地点的全部 gateway 记录。三者各为一次修改、一条历史、一次撤销。勾选 OSM gateway 只把这一条记录变为 `source: manual`，保留其 `gateway_id` 和 `osm_ids`，地点本身来源不变。地图上出入口为橙框白底标记；侧栏“出入口”列表、数据表和报告区列出各出入口及其方式。移动或合并地点时 gateway 随之更新。
- 移动地点会重算相邻边长度，保留原秒数并标记 `manual-retained/v1`，需要复核；工具不会静默把人工秒数换回默认速度。
- 每次变更都带 `source: manual`、对应版本，以及带 before/after 的历史。来源于 OSM 的对象继续保留原 OSM id。纯人工对象的 `osm_ids` 为空。
- 导出版本从载入版本 + 1 开始，连续导出递增。历史随 JSON 导出，不写浏览器存储；导出前关闭页面会丢失尚未保存的工作。提供一步撤销（批量操作也是一步），撤销本身也记录历史。几千个地点、几百个已选项时，框选、批量合并、删除和撤销均在约 1 秒内完成。

## 图包格式与自行确定的约定

文档 13 定义了语义而未冻结具体 JSON 字段。本工具使用独立的 `schema: home-area.graph/v1`，**没有修改 `good_life/contracts/`**。

| 顶层字段 | 含义 |
| --- | --- |
| `schema` | 固定 `home-area.graph/v1` |
| `graph_id` | 用户指定的稳定常住区域身份；不同图独立版本化 |
| `version` | 正整数包版本；重新导入必须严格增加 |
| `source`, `coordinate_system` | 固定 `osm`, `WGS84`；混合人工编辑仍保留 OSM 派生图来源 |
| `attribution`, `license` | 固定 `© OpenStreetMap contributors`, `ODbL-1.0` |
| `bounds` | `[south, west, north, east]` |
| `nodes` | 文档中的 Place；每个地点也是图节点 |
| `edges` | 文档中的 Edge；一条记录可表示双向边 |
| `gateways` | 出入口：城市出行离开/进入常住图的节点、WGS84 坐标及可接的交通方式（`walk/bus/metro`） |
| `vector_data` | 从本地缓存提取的矢量。`road/building/facility` 对应建图类别；`landcover/water/waterway/place` 仅供显示（绿地、水面、水道、地名），只保留样式和名称标签，排在前者之后。不用于证明路由连通 |
| `build_report` | 数量、丢弃原因、弱连通分量、孤立点、无名地点、缺少门禁/开放信息，以及再生成差异提案 |
| `edit_history` | 操作、UTC 时刻、版本、逐对象的 before/after 记录；删除记录用 `after: null` |
| `digest` | 下述规范内容摘要 |

`nodes` 必需业务字段为 `place_id,name,type,region,lat,lon,coordinate_system,opening_rules,capabilities,access,source,osm_ids,version`，另保留 OSM `tags`。未知区域、门禁、开放规则为 `null`；空能力数组表示尚未标注能力。OSM 生成的地点 ID 为 `osm:node/<id>` 或 `osm:way/<id>`，不依赖名称或坐标，因此重命名不会改 ID。建筑/设施 way 的地点是其不同顶点的算术均值，不声称为测量入口或精确面内点。

`edges` 字段为 `edge_id,from,to,name,direction,length_m,estimated_duration_s,estimated,estimate,load,access,availability,enabled,source,osm_ids,version`。`direction` 为 `both/forward`；`estimated_duration_s` 是正整数秒；`estimated` 为 `true`；`estimate.model` 标明估算方法。道路边 ID 含 OSM way id、起终点 OSM node id 和段索引，能区分重复段，但 OSM 拓扑重排可能使边 ID 改变；稳定性保证针对 Place ID。步行速度默认 1.4 m/s，`path` 1.1、`track` 1.0、`steps` 0.65 m/s，其余保留道路按 1.4 m/s；`--walking-speed` 对这些速度等比例缩放。秒数按球面距离除以速度向上取整，最少 1 秒。它不是道路实测时间或导航报价。

`gateways` 字段为 `gateway_id,place_id,lat,lon,coordinate_system,modes,source,osm_ids,version`。生成器只从 `entrance` 或 `barrier=gate/lift_gate/entrance` 的节点生成，`modes` 只有 `walk`，不凭空把所有区域边缘当出入口，也不调用城市导航；公交、地铁出入口由用户在编辑器中标注。A8 城市导航（`good_life/city/navigation.py`）只允许外部 walk/bus/metro 服务在 `modes` 含该方式的 gateway 节点接入常住图；同一地点有多条 gateway 记录时取模式并集。包校验要求 `modes` 为非空字符串数组，不设白名单，因此 `bus`、`metro` 及未来的其他方式都可通过；编辑器只提供 walk/bus/metro 三个勾选框，并原样保留其他方式。

未知 JSON 扩展字段会被保留并参与摘要；必需字段、字段类型、坐标、来源、引用、重复 ID、版本及摘要会校验。每个条目的版本不能超过包版本。所有 JSON 数值有限且绝对值不超过 `2^53-1`。校验、摘要和存储读取前进行非递归资源检查：紧凑 UTF-8 JSON 最多 16 MiB、最多 100,000 个 JSON 对象（含历史快照和嵌套对象）、最多 64 层 JSON 值；存储原文还在解析前检查字节上限，递归溢出统一返回 `ValueError`。

摘要使用 **typed canonical v1**，以避免 Python 与浏览器对 `1/1.0`、指数格式的差异：移除顶层 `digest`，对象键按 Unicode 码点排序；数组保持顺序；字符串使用非 ASCII 转义的 JSON 字符串形式；null/boolean 使用 JSON 字面量；所有数值转为 `#` 后接 IEEE754 binary64 大端的 16 位十六进制字节，负零归一为正零；结构仍以 `{}`, `[]`, `,`, `:` 连接且无空格。对此 UTF-8 字节串计算 SHA256。数字 token 不带引号，因此与字符串不同。该摘要用于完整性和确认绑定，**不是签名或授权**。Python/浏览器摘要一致性有集成测试。

关系、多重多边形、洞、楼层推断、路径裁剪、自动连接设施入口未实现（底图同样不画多重多边形关系，例如用关系表示的大湖）。只保留范围内顶点；缺失顶点/跨边界的路段会丢弃，线不跨缺失顶点补连。不完整建筑面丢弃。范围内参与建图但被丢弃的 way/路段在 `dropped_items` 中逐项记录 OSM id、原因及路段端点；大区文件中范围外对象和不支持关系只计数，避免把整份区域文件的 ID 复制进小图报告。建筑/设施不会自动吸附道路，因此常出现孤立组件；报告要求用户审阅并手工补连接。报告中的连通分量按启用边的无向弱连通计算，不能证明单向可达性或合法通行。

## 可选结构化门禁与时间

`home-area.graph/v1` 兼容增加地点和边的三个可选字段：`access_requirement`、`access_area`、`opening_schedule`。旧包仍有效；新字段参与原摘要并由 Python/编辑器校验。完整词表、确定性 OSM 映射、时区/DST、未知信息及假设策略见 [文档 13](../../docs/architecture-next/13-geography.md#结构化门禁与开放时间默认关闭)。

资格词表为 `public/campus_member/student/staff/resident_of:<area>/closed/unknown/unparsed`；student 和 staff 满足 campus_member。`yes/permissive/public/designated` 映射公众，`no` 关闭，`private` 要求显式 graph_id/area 资格映射，`destination` 保留 unparsed。精确中文短句映射仅限文档列出的四句，长篇叙述不做 NLP。

```json
{"access_requirement":"student","access_area":"campus","opening_schedule":{
  "status":"known","timezone":"Asia/Shanghai",
  "windows":[{"weekdays":[0,1,2,3,4],"start":"08:00","end":"18:00"}]
}}
```

星期一=0，星期日=6，窗口半开；结束不晚于开始表示跨夜，相等表示 24 小时；仅结束可写 24:00。明确空 windows 为关闭。unknown/unparsed 状态只保存 `{ "status": "unknown" }` / `{ "status": "unparsed" }`，不能夹带猜测的时间。旧文字字段保留、可读，显式结构化字段优先。没有结构化字段时只把 `24/7/always` 识别为已知全天，其他时间表达式仍 unparsed，空值 unknown。

编辑器的“结构化门禁与时间”可对地点、道路设置资格和多条窗口，同一次保存只有一条 history；`source=manual` 让 `--previous` 原样保留。每项显示当前解析状态；报告另列 `unparsed_access/unparsed_opening`。默认 CSP/网络约束没有增加端点。

生成时默认继续关闭 `access/foot=private` 道路；显式 `--structured-access` 才保留 private 道路供凭据判断，`no` 仍然关闭。旧包的 `enabled=false` 必须在编辑器明确启用，不能凭资格绕过。

宿主可用 `GraphAccessPolicy.from_settings(plugin_settings)` 或 `OfflineNavigation(..., settings=plugin_settings)`；`graph_access_enabled` 默认 false，未启用保留旧路由行为和报价形状。启用时角色资格默认空，`treat_unparsed_access_as/treat_unknown_opening_as` 默认 blocked；allowed 必须在新报价中带假设及 needs_review。A4 可使用 `Campus.from_settings(store, plugin_settings, places=..., paths=...)`；`academics_graph_id` 默认空，指定 ID 且已确认图包才切换至与 A8 共用的离线路由。没有确认包继续使用传入的手工路径；课次和旅行固定图版本及摘要，后续确认包不改变在途记录。

## 再生成与人工修改

已有输出文件永不覆盖。使用上一次人工导出的版本作为 `--previous`，写到新文件：

```bash
py -3 -m tools.osm_graph_builder \
  --bbox 30.999 120.999 31.003 121.003 \
  --graph-id my-home --previous /path/to/edited-v2.json \
  --output /path/to/proposed-v3.json
```

默认版本取 previous + 1，也可显式指定更大 `--version`。重新获取原数据另加 `--source ... --refresh` 或 `--overpass --refresh`。

重新生成按 OSM 身份复用现有 Place ID，保留所有 `source: manual` 对象以及历史中的删除记录。保留整条人工对象是保守选择：即使只改了名称，也不会自动覆盖该对象的其他字段。人工边依赖的旧地点仍保留。出入口沿用同一规则：编辑器新建或改过方式的 gateway 是 `source: manual`，整条保留；取消的 gateway（含 OSM 门）在历史中是删除记录，即使 OSM 仍标着入口也不会回来。保留的 gateway 坐标会同步到其地点的最终坐标，因此 OSM 移动了地点时，出入口跟着移动而方式不变。原始 OSM 候选差异位于 `build_report.regeneration_proposal`，最终保留人工修改后的差异也输出到 CLI；**不会自动导入**。用户在编辑器检查各个 added/removed/changed ID 后再决定导入哪个包。首版没有三方合并冲突解决器或逐项勾选应用 OSM 更新的向导，可通过编辑器逐项调整提案。

## 插件侧预览和确认存档

新增两个独立模块：`good_life.graph_packages`（无 I/O 的校验/差异）和 `good_life.graph_package_storage`（默认关闭的存档入口）。没有运行时注册、执行权变更、管理页写路由或管理页面改动。调用方仍需由宿主完成授权，本模块不把文件摘要当作角色权限。

```python
from good_life.graph_packages import validate_package, diff_packages
from good_life.graph_package_storage import GraphPackageStore

candidate = validate_package(decoded_json)
store = GraphPackageStore(life_storage)  # 默认关闭；预览不建表
review = store.preview(candidate)
# 将 review 呈现给用户，其中含版本关系和节点/边/出入口的增删改 ID。
# 只有外部调用方获得明确确认后，显式启用并使用原预览的两个摘要：
store = GraphPackageStore(life_storage, enabled=True)
receipt = store.confirm(
    candidate,
    expected_current_digest=review["current_digest"],
    expected_proposed_digest=review["proposed_digest"],
    confirmed=True,
)
```

`diff_packages` 返回 `initial/newer/same/older`，只有 initial/newer 可导入。同一个 OSM 地点不许更换 place_id，同一个 place_id 不许换绑或清除 OSM 身份；存储确认时会检查该 graph_id 的全部历史，防止删除后重新分配身份。初次确认才在现有 `LifeStorage` 事务中创建独立的 `geo_graph_versions` 表。每次保存完整包、摘要、差异和导入时刻。确认时再次核对当前图摘要和候选摘要，拒绝过期预览、同版覆盖及降版。不同 graph_id 的版本相互独立；现有库 schema 标识和事件表不变。

`latest(graph_id)` 和 `history(graph_id)` 是供未来只读展示使用的存储读入口；当前功能只做版本存档，**不会激活图或改写已发生的行程/课次**。

## 验证

```bash
py -3 -m unittest discover -s tests -p 'test_osm_graph_builder.py' -v
py -3 -m unittest discover -s tests -p 'test_graph_package_import.py' -v
```

`tests/fixtures/osm/tiny.osm` 是手写测试数据。用例覆盖解析、建图、速度、方向、分类、缺失引用、缓存、显式刷新、冷却期、无网络、校验、差异、稳定 ID、确认绑定、版本存档和重开恢复。测试封锁默认 HTTP 入口，所有在线用例使用 mock。安装了 Node 时，Python 测试还执行页面中同一份 JS 核心，验证增删移、连边、合并、拆分、框选命中、批量删除/合并（改接、内部边、平行边、删除记录、撤销、历史）、出入口的新建/勾选方式/取消（每步一次历史与撤销、最后一种方式不可取消、只冻结实际改动的记录）、瓦片坐标与标签避让，以及批量操作后的跨语言摘要、“重新生成不复活已合并地点”，以及出入口方式在两次 `--previous` 重新生成后保留、已取消的 OSM 门不复活；没有 Node 时仅该项跳过。底图测试确认新图层不改变图内容、Overpass 仍为单次请求（mock）、旧缓存可用。
