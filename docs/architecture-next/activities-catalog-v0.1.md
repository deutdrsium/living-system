# 活动目录 v0.1

基于 [原始草案](activities-draft-v0.1.yaml)、[W0 盘点](w0-activity-draft-inventory.md)、[组件规范](05-activity-components.md)、[管理规范](17-library-admin.md)和[路线图](18-implementation-roadmap.md)。原始 id/name 保持原样；本表为启用规划，不是可直接执行的配置。

类型区分 activity、event、condition、intent；condition 的可能应对活动列在备注，不能视为状况已经发生。执行器采用本次明确给定的词表；`(拟)` 表示尚未实现。`none` 仅用于 event/condition；待定表示没有合适的词表项。课次、购物、整理与阅读的部分覆盖均在备注中说明。`activities/` 是 `plugins/astrbot_plugin_good_life/good_life/activities/`，`good_life/` 是 `plugins/astrbot_plugin_good_life/good_life/`。

包按涉及的领域和活动包统计，同一条可计入多个包；A3 阅读已实现但不在本表指定的归属包名单中，其活动包接入列 A7。requires 仅列本次给定的能力键；原始清单中的库存、配角、技能、兴趣、生成材料等无对应键时不伪造映射，启用前仍须核查。

批次计算：1 为现有执行器或 event/condition 的 `none`，且仅需允许的已实现依赖；2 为恰好一个进行中/拟议执行器或一个 syllabus、route、weather、memory 依赖；3 为其他情况，包括 browse、feed、平台副作用、nearby_place 或至少两个未完成项。outing 同时需要 trip@1，因此计两个执行器；待定执行器也列入批次 3。一个依赖键只计一次；route 与 trip@1 分别计作能力和执行器。批次是相对规划，仍须检查输入、实际能力、权限和回执。

| 原始 id / name | 类型 | 归属包 | 执行器 | requires | 状态 | 建议批次 | 备注 |
|---|---|---|---|---|---|---|---|
| `oversleep` / 赖床 | activity | A4、A6、A7 | `sleep_scheduler` | `geo.home_graph`、`source.calendar`、`source.weather`、`state.energy`、`state.sleep` | 部分可用 | 2 | 已有睡眠调度：good_life/sleep_scheduler.py；赖床及迟到语义待接入 |
| `nap` / 午睡 | activity | A4、A6、A7 | `sleep_scheduler` | `geo.home_graph`、`source.calendar`、`state.energy`、`state.sleep` | 部分可用 | 1 | 已有睡眠调度：good_life/sleep_scheduler.py；午睡语义待接入 |
| `shower` / 洗澡 | activity | A4、A7 | `timed_local@1`(拟) | `geo.home_graph`、`source.calendar` | 未实现 | 2 | — |
| `laundry` / 洗衣服 | activity | A2、A4、A6、A7 | `timed_local@1`(拟) | `economy.wallet`、`geo.home_graph`、`source.weather`、`state.energy` | 未实现 | 3 | — |
| `clothes_wont_dry` / 衣服晾不干 | condition | A6 | `none` | `source.weather` | 未实现 | 2 | 对应应对活动：处理未干衣物（重晾或烘干） |
| `tidy_room` / 收拾房间 | activity | A4、A6、A7 | `local_order@1` | `geo.home_graph`、`source.calendar`、`source.memory`、`state.energy` | 部分可用 | 2 | 已有模拟整理书桌：activities/local_order.py；房间与记忆待接入 |
| `seasonal_wardrobe` / 换季整理衣柜 | activity | A4、A7 | `local_order@1` | `geo.home_graph`、`source.weather` | 部分可用 | 2 | 已有模拟整理书桌：activities/local_order.py；衣柜与库存待接入 |
| `haircut` / 剪头发 | activity | A2、A6、A7、A8 | `outing@1`(拟)、`trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route`、`state.energy` | 未实现 | 3 | 前往与返回由 trip@1 承担 |
| `minor_illness` / 小病（感冒等） | condition | A6 | `none` | `source.weather`、`state.energy`、`state.sleep` | 未实现 | 2 | 对应应对活动：休息或就医 |
| `insomnia` / 失眠 | condition | A4、A6 | `none` | `geo.home_graph`、`source.memory`、`state.energy`、`state.sleep`、`state.stress` | 未实现 | 3 | 对应应对活动：休息或寻求助眠帮助 |
| `meal` / 吃饭 | activity | A2、A6、A7、A8 | `meal@1`(拟) | `economy.wallet`、`geo.nearby_place`、`source.calendar`、`state.energy` | 未实现 | 3 | — |
| `try_new_restaurant` / 尝试新餐厅 | activity | A2、A6、A7、A8 | `meal@1`(拟) | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.feed`、`state.energy` | 未实现 | 3 | — |
| `cook` / 做饭 | activity | A2、A4、A6、A7 | `meal@1`(拟) | `economy.wallet`、`geo.home_graph`、`source.feed`、`state.energy` | 未实现 | 3 | 包含产出 share_candidate |
| `snack_run` / 买零食饮料 | activity | A2、A6、A7、A8 | `purchase_local@2` | `economy.wallet`、`geo.nearby_place`、`source.feed`、`state.stress` | 部分可用 | 3 | 已有模拟饮料购买：activities/buy_drink.py；零食和来源待接入 |
| `drink_caffeine` / 喝咖啡或茶 | activity | A2、A6、A7 | `meal@1`(拟) | `economy.wallet`、`source.calendar`、`state.energy`、`state.sleep` | 未实现 | 2 | — |
| `online_shopping` / 网购 | activity | A2、A6、A7 | `browse@1`(拟) | `economy.wallet`、`platform.side_effect`、`source.calendar`、`source.feed`、`state.stress` | 未实现 | 3 | — |
| `pick_up_parcel` / 取快递 | activity | A6、A7、A8 | `outing@1`(拟)、`trip@1` | `geo.route`、`state.energy` | 未实现 | 3 | 前往与返回由 trip@1 承担 |
| `buy_daily_goods` / 买日用品 | activity | A2、A7、A8 | `purchase_local@2` | `economy.wallet`、`geo.nearby_place` | 部分可用 | 3 | 已有模拟购买：activities/buy_drink.py；日用品与物品库待接入 |
| `secondhand_browse` / 逛二手平台 | activity | A7 | `browse@1`(拟) | `source.feed` | 未实现 | 3 | — |
| `watch_movie` / 看电影 | activity | A2、A6、A7 | `browse@1`(拟) | `economy.wallet`、`source.calendar`、`source.feed`、`state.energy` | 未实现 | 3 | — |
| `follow_series` / 追剧/追番 | activity | A7 | `browse@1`(拟) | `source.calendar`、`source.feed` | 未实现 | 3 | — |
| `browse_discussion` / 看剧集/作品讨论区 | activity | A7 | `browse@1`(拟) | `source.feed` | 未实现 | 3 | — |
| `watch_videos` / 看视频 | activity | A7 | `browse@1`(拟) | `source.calendar`、`source.feed` | 未实现 | 3 | 包含产出 share_candidate |
| `scroll_feed` / 刷社交媒体 | activity | A7 | `browse@1`(拟) | `source.calendar`、`source.feed` | 未实现 | 3 | 包含产出 share_candidate |
| `listen_music` / 听音乐 | activity | A7 | `browse@1`(拟) | `source.feed` | 未实现 | 3 | — |
| `listen_podcast` / 听播客 | activity | A7 | `browse@1`(拟) | `source.feed` | 未实现 | 3 | 包含产出 share_candidate |
| `read_book` / 读书 | activity | A7 | `reading` | `source.calendar` | 已实现 | 1 | 阅读归 A3；A7 为活动包接入；已有阅读执行：good_life/reading_execution.py；草案其他触发与产物须核对；包含产出 share_candidate |
| `daily_puzzle` / 做每日谜题 | activity | A7 | `practice@1`(拟) | `source.calendar`、`source.feed` | 未实现 | 3 | — |
| `doodle` / 涂鸦 | activity | A7 | `compose@1`(拟) | `source.memory` | 未实现 | 3 | 原始材料来源：memory_store；包含产出 share_candidate |
| `serious_artwork` / 认真画一幅画 | activity | A6、A7 | `compose@1`(拟) | `source.memory`、`state.energy` | 未实现 | 3 | 原始材料来源：memory_store、place_photos；包含产出 share_candidate |
| `take_photo` / 拍照 | activity | A7 | `compose@1`(拟) | `source.memory`、`source.weather` | 未实现 | 3 | 原始材料来源：place_photos、street_view、weather；包含产出 share_candidate |
| `write_poem_or_prose` / 写诗或短文 | activity | A7 | `compose@1`(拟) | `source.memory` | 未实现 | 3 | 原始材料来源：memory_store |
| `write_diary` / 写日记 | activity | A7 | `compose@1`(拟) | `source.calendar`、`source.memory` | 未实现 | 3 | 原始材料来源：memory_store |
| `write_review` / 写短评 | activity | A7 | `compose@1`(拟) | `source.feed`、`source.memory` | 未实现 | 3 | 原始材料来源：bookshelf、movie_db |
| `curate_playlist` / 整理歌单 | activity | A7 | `compose@1`(拟) | `source.feed`、`source.memory` | 未实现 | 3 | 原始材料来源：music_charts；包含产出 share_candidate |
| `write_postcard` / 写明信片/信 | activity | A2、A7 | `compose@1`(拟) | `economy.wallet`、`source.calendar`、`source.memory` | 未实现 | 3 | 原始材料来源：memory_store、npc_roster；包含产出 share_candidate；包含产出 npc_update |
| `language_practice` / 练外语 | activity | A7 | `practice@1`(拟) | `source.feed` | 未实现 | 3 | — |
| `online_course` / 看公开课 | activity | A6、A7 | `browse@1`(拟) | `source.feed`、`state.energy` | 未实现 | 3 | — |
| `attend_talk` / 去讲座 | activity | A7、A8 | `outing@1`(拟)、`trip@1` | `geo.nearby_place`、`geo.route`、`source.feed` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `practice_skill` / 练习一项技能 | activity | A7 | `timed_local@1`(拟) | 无 | 未实现 | 2 | — |
| `revive_old_hobby` / 重拾旧爱好 | activity | A7 | `practice@1`(拟) | `source.feed` | 未实现 | 3 | — |
| `attend_class` / 上课 | activity | A4、A8 | `class_session@1` | `academics.timetable`、`geo.route`、`source.calendar` | 部分可用 | 2 | 已有模拟课次：activities/class_session.py；课堂笔记和变体待接入 |
| `skip_class` / 逃课 | activity | A4 | `class_session@1` | `academics.timetable`、`source.calendar`、`source.weather` | 部分可用 | 2 | 已有明确缺席：activities/class_session.py；动态决策待接入 |
| `caught_absent` / 逃课被点名 | event | A4、A6 | `none` | `academics.timetable`、`state.stress` | 未实现 | 3 | 点名产生的缺席事实 |
| `study_session` / 自习/写作业 | activity | A4、A5、A6 | `study@1` | `academics.syllabus`、`academics.timetable`、`geo.home_graph`、`source.calendar`、`state.energy`、`state.stress` | 未实现 | 3 | — |
| `exam` / 考试 | activity | A4、A5、A6 | `study@1` | `academics.syllabus`、`academics.timetable`、`source.calendar`、`state.energy`、`state.stress` | 未实现 | 3 | — |
| `get_grade` / 出成绩 | event | A4、A5 | `none` | `academics.syllabus`、`academics.timetable` | 未实现 | 2 | 成绩发布事实 |
| `course_registration` / 选课 | activity | A4、A7 | 待定 | `academics.timetable`、`platform.side_effect`、`source.calendar` | 未实现 | 3 | 执行器待定 |
| `group_project_meeting` / 小组作业讨论 | activity | A4、A5、A7 | `social@1`(拟) | `academics.syllabus`、`academics.timetable` | 未实现 | 3 | 包含产出 npc_update |
| `part_time_shift` / 兼职/上班 | activity | A2、A6、A7 | 待定 | `economy.wallet`、`source.calendar`、`state.energy` | 未实现 | 3 | 执行器待定 |
| `chat_with_roommate` / 和室友/同住者聊天 | activity | A4、A7 | `social@1`(拟) | `geo.home_graph`、`source.calendar`、`source.memory` | 未实现 | 3 | 包含产出 npc_update |
| `meal_with_friend` / 和朋友吃饭 | activity | A2、A7、A8 | `meal@1`(拟) | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.calendar` | 未实现 | 3 | 包含产出 npc_update |
| `call_family` / 给家里打电话 | activity | A7 | `social@1`(拟) | `platform.side_effect`、`source.calendar` | 未实现 | 3 | 包含产出 npc_update |
| `npc_birthday` / 朋友过生日 | activity | A2、A7 | `social@1`(拟) | `economy.wallet`、`source.calendar` | 未实现 | 2 | 包含产出 npc_update |
| `club_activity` / 社团/兴趣小组活动 | activity | A7、A8 | `social@1`(拟) | `geo.nearby_place`、`source.calendar`、`source.feed` | 未实现 | 3 | 包含产出 npc_update |
| `lurk_group_chat` / 潜水群聊 | activity | A7 | `browse@1`(拟) | `platform.side_effect`、`source.feed` | 未实现 | 3 | — |
| `walk` / 散步 | activity | A6、A7、A8 | `outing@1`(拟)、`trip@1` | `geo.nearby_place`、`geo.route`、`source.calendar`、`source.weather`、`state.energy`、`state.stress` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `caught_in_rain` / 淋雨/被天气困住 | condition | A6 | `none` | `source.weather` | 未实现 | 2 | 对应应对活动：避雨或等待天气好转 |
| `sunset_watch` / 看日落 | activity | A7、A8 | `outing@1`(拟)、`trip@1` | `geo.route`、`source.weather` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `sky_watch` / 看月亮/星星 | activity | A7、A8 | `outing@1`(拟)、`trip@1` | `geo.route`、`source.calendar`、`source.weather` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `seasonal_scenery` / 赏季节景色 | activity | A7、A8 | `outing@1`(拟)、`trip@1` | `geo.nearby_place`、`geo.route`、`source.calendar`、`source.feed`、`source.weather` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `city_walk` / 城市漫步 | activity | A2、A6、A7、A8 | `outing@1`(拟)、`trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.calendar`、`source.weather`、`state.energy` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `commute` / 通勤/出行 | activity | A2、A4、A8 | `trip@1` | `economy.wallet`、`geo.route`、`source.weather` | 部分可用 | 3 | 课次含校园 travel 阶段：activities/class_session.py；通用往返尚在 W4 |
| `trip` / 短途/长途旅行 | activity | A2、A6、A8 | `trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.calendar`、`source.weather`、`state.energy` | 未实现 | 3 | 包含产出 share_candidate |
| `go_home` / 回家/返校 | activity | A2、A4、A6、A8 | `trip@1` | `academics.timetable`、`economy.wallet`、`geo.route`、`source.calendar`、`state.energy` | 未实现 | 3 | — |
| `visit_exhibition` / 看展 | activity | A2、A6、A7、A8 | `outing@1`(拟)、`trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.feed`、`state.energy` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `live_performance` / 看演出 | activity | A2、A6、A7、A8 | `outing@1`(拟)、`trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.feed`、`state.energy` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `bookstore_visit` / 逛书店/图书馆 | activity | A2、A7、A8 | `outing@1`(拟)、`trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route` | 未实现 | 3 | 前往与返回由 trip@1 承担 |
| `market_or_fair` / 逛市集/展会 | activity | A2、A6、A7、A8 | `outing@1`(拟)、`trip@1` | `economy.wallet`、`geo.nearby_place`、`geo.route`、`source.feed`、`state.energy` | 未实现 | 3 | 前往与返回由 trip@1 承担；包含产出 share_candidate |
| `exercise` / 运动 | activity | A6、A7、A8 | `practice@1`(拟)、`outing@1`(拟)、`trip@1` | `geo.nearby_place`、`geo.route`、`source.calendar`、`source.weather`、`state.energy`、`state.stress` | 未实现 | 3 | 外出运动场景使用 outing@1 与 trip@1 |
| `fitness_test` / 体测 | activity | A4、A6、A7 | `timed_local@1`(拟) | `academics.timetable`、`source.calendar`、`state.energy` | 未实现 | 2 | — |
| `check_trending` / 看热搜 | activity | A7 | `browse@1`(拟) | `source.calendar`、`source.feed` | 未实现 | 3 | — |
| `grab_tickets` / 抢票/抢名额 | activity | A2、A7 | `browse@1`(拟) | `economy.wallet`、`platform.side_effect` | 未实现 | 3 | — |
| `phone_cleanup` / 整理手机/备忘录 | activity | A7 | `compose@1`(拟) | `source.memory` | 未实现 | 3 | 原始材料来源：interest_engine、memory_store |
| `revisit_old_memory` / 翻看旧照片/旧物 | activity | A7 | `compose@1`(拟) | `source.calendar`、`source.memory` | 未实现 | 3 | 原始材料来源：memory_store；包含产出 share_candidate |
| `reread_diary` / 重读旧日记 | activity | A7 | `reading` | `source.calendar`、`source.memory` | 部分可用 | 2 | 已有阅读流程：good_life/reading_execution.py；日记来源和权限待接入 |
| `daydream` / 发呆/想事情 | activity | A4、A7 | `compose@1`(拟) | `academics.timetable`、`source.memory` | 未实现 | 3 | 原始材料来源：bookshelf、course_system、memory_store；包含产出 share_candidate |
| `homesick` / 想家 | condition | A6 | `none` | `source.calendar`、`source.memory` | 未实现 | 2 | 对应应对活动：联系家人 |
| `plan_and_procrastinate` / 列计划然后拖着 | activity | A4、A5、A6、A7 | `compose@1`(拟) | `academics.syllabus`、`academics.timetable`、`source.calendar`、`source.memory`、`state.stress` | 未实现 | 3 | 原始材料来源：course_system |
| `solar_term_custom` / 节气习俗 | activity | A2、A7 | `compose@1`(拟)、`purchase_local@2` | `economy.wallet`、`source.calendar`、`source.memory`、`source.weather` | 未实现 | 3 | 原始材料来源：solar_terms、weather；涉及购物时由 purchase_local@2 处理购买；包含产出 share_candidate |
| `festival` / 过节 | activity | A2、A7 | `compose@1`(拟)、`purchase_local@2` | `economy.wallet`、`source.calendar`、`source.memory` | 未实现 | 3 | 原始材料来源：calendar_holidays、npc_roster、solar_terms；涉及购物时由 purchase_local@2 处理购买；包含产出 share_candidate |
| `shopping_festival` / 购物节 | activity | A2、A7 | `browse@1`(拟) | `economy.wallet`、`source.calendar`、`source.feed` | 未实现 | 3 | — |
| `anniversary_of_figure` / 名人纪念日 | activity | A7 | `compose@1`(拟) | `source.calendar`、`source.feed`、`source.memory` | 未实现 | 3 | 原始材料来源：anniversaries、bookshelf、public_domain_text；包含产出 share_candidate |
| `term_milestone` / 学期节点 | event | A4 | `none` | `academics.timetable`、`source.calendar` | 未实现 | 1 | 学期节点事实；仪式活动另行建模；包含产出 share_candidate |

## 存疑项

- `watch_movie`：草案仅使用简介和短评，不能将结果记为真实观影经历。
- `course_registration`：行为需要处理输入（course_system）及产出（memory、mood_delta）；词表没有明确覆盖该行为的执行器。
- `part_time_shift`：行为需要处理输入（job_system）及产出（mood_delta、purchase）；词表没有明确覆盖该行为的执行器；草案用负 purchase 表示收入；须转换为收入事实。
- `npc_birthday`：草案以生日后的负 delay 表示提前购买；须改为未来日历实例的提前量。
- `caught_in_rain`：淋雨事实与受困状态、避雨行动须拆分。
- `commute`：草案通配链触发与 05、17 冲突；应由父活动创建 travel 阶段。
- `shopping_festival`：草案把购物节事件与浏览/购物意图混作活动；行为边界待拆分。

## 启用时需要处理的通用事项

将原始 state 字符串条件转换为类型化条件或声明输入的纯 predicate；将全局 inputs 回退拆为具名输入槽，并核对契约、必需性、来源与时效。将 followups/chain 双写去重为一条事实事件订阅；将动态 `p: 0.0` 转换为已注册 policy，不能当作固定概率；拆分阶段负荷、效果、产物与事件，保留来源和权限。确认没有 requires 对应键的原始依赖，以及执行器的授权、取消、中断和真实平台回执。

## 汇总

包和执行器按多选计数；类型和批次每行各计一次。

| 包 | 条目数 |
|---|---:|
| A2 | 26 |
| A4 | 23 |
| A5 | 5 |
| A6 | 34 |
| A7 | 69 |
| A8 | 23 |

| 类型 | 条目数 |
|---|---:|
| activity | 76 |
| event | 3 |
| condition | 5 |
| intent | 0 |

| 执行器 | 条目数 |
|---|---:|
| purchase_local@2 | 4 |
| local_order@1 | 2 |
| class_session@1 | 2 |
| reading | 2 |
| sleep_scheduler | 2 |
| study@1 | 2 |
| trip@1 | 16 |
| timed_local@1 | 4 |
| meal@1 | 5 |
| outing@1 | 13 |
| browse@1 | 14 |
| social@1 | 5 |
| compose@1 | 15 |
| practice@1 | 4 |
| none | 8 |
| 待定 | 2 |

| 批次 | 条目数 |
|---|---:|
| 1 | 3 |
| 2 | 16 |
| 3 | 65 |

总计 84 条；存疑条目 7 条。
