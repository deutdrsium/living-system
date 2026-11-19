# W0 测试基线

记录日期：2026-11-16。目标工作树 HEAD：`c83ad623389a32234422e689618a0b874915a1f0`。这是当前机器上的局部测试记录，不是完整回归或真实平台验收。

## 环境和隔离

- Windows，Python 3.13.7，Node.js v26.7.0。Python 可导入 `yaml`，不可导入 `pytest`、`aiohttp`、`astrbot` 或 `regex`；贡献指南示例中的 AstrBot Python 路径在本机不存在。
- 设置 `PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1` 并使用 `-B`；未安装依赖、未启动真实机器人或主动测试，未进行真实外部服务验收。测试中的发布切换日志来自临时目录和模拟 subprocess，不能证明生产切换。
- 首次运行分别在约 60 秒和 30 秒中止，不作为最终结论；随后三套测试分别设置 600 秒上限重新运行。并发运行和本机 SQLite 慢事务可能影响耗时。`unittest` 将无法导入的测试模块作为错误计数项，而非已执行用例。

## 实际结果

| 范围 | 结果 | 数字和限制 |
|---|---|---|
| 根 `tests`，unittest discovery | 完成，有失败和错误 | `Ran 198 tests in 96.034s`；**1 断言失败、56 错误**，其中 40 项是测试模块收集导入错误、16 项是用例执行错误，计数中没有报告跳过。剩余 141 个计数项通过；收集错误不是实际执行的用例。 |
| 生活插件独立 `tests` | 无此目录 | discovery 报 `ImportError: Start directory is not importable`；这是测试入口缺失，不是测试用例失败。生活相关测试存在于根 `tests`。 |
| 主插件 `tests`，unittest discovery | 600 秒超时，未完成 | Python `subprocess.run(timeout=600)` 终止运行；没有最终汇总，无法准确统计已通过、失败、跳过或收集错误。日志显示大量 SQLite 慢事务。该环境缺少宿主 SDK 和多项依赖，也未安装 pytest；unittest discovery 不能替代所有 pytest 风格测试。 |
| 根目录生活迁移定向测试 `test_activity_migration.py` | 完成 | **16 项通过，0 失败，0 错误，1.326 秒**。 |
| 主插件定向 `test_binding_store.py` | 导入错误 | `ModuleNotFoundError: No module named 'regex'`，测试文件未进入用例；不能记为用例失败。 |
| 主插件 `tests/*.cjs`，Node test runner | 完成，有失败 | **1,227 项：1,219 通过、6 失败、2 跳过、0 取消**；79.223 秒。 |

## 已完成套件的失败详情

- 根测试唯一断言失败：`test_life_runtime.LifeRuntimeTests.test_rebinding_stops_previous_scheduler_and_revokes_registration` 期望的处理器集合少了实际返回的 `configuration_command` 和 `configuration_read`。不能仅凭测试判断是测试期望过时还是实现有误。
- 根测试 40 个收集错误和 16 个用例执行错误涉及缺少 `regex`、`pytest`、`tzdata`（使 `Asia/Shanghai` 时区无法加载）及宿主 SDK 等；具体原因不能一概归于单一依赖。
- CJS 六项失败：`test_companion_nested_confirm.cjs` 等待本地浏览器 fixture 页面就绪超时；`test_life_workspace.cjs` 阅读速度预期为 1200、实际为 undefined；`test_maintenance_page.cjs` 未匹配“V2 联动摘要”；`test_native_perception_page.cjs` 未匹配带凭据说明的天气预警文案；`test_page_scale.cjs` 两项尺度审计未通过，涉及多个 CSS 固定尺度及名单。
- 仓库另有 pytest 函数、参数化和 fixture 测试；当前解释器没有 pytest，离线 pip wheel 缓存中也没有，故这部分未按 pytest 执行，不能计入 unittest 覆盖率。

### 根测试：断言失败（1 项）

- `test_life_runtime.LifeRuntimeTests.test_rebinding_stops_previous_scheduler_and_revokes_registration`：处理器集合多出 `configuration_command`、`configuration_read`。

### 根测试：用例执行错误（16 项）

- `test_calendar_changes_compatibility.CalendarChangesCompatibilityTests.test_create_revise_and_each_existing_action_match_original`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_calendar_changes_compatibility.CalendarChangesCompatibilityTests.test_rejections_match_original_codes`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_good_life_entry.GoodLifeEntryTests.test_dependency_discovery_handles_load_order_and_conflicts_without_running_life`：导入 `astrbot.api.web` 失败，当前 `astrbot.api` 不是包。
- `test_life_ledger_proxy.LifeLedgerProxyTests.test_rest_gate_reads_sleep_from_provider_and_never_falls_back`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_memory_page_migration.MemoryPageMigrationTests.test_current_life_records_are_used_and_unconfirmed_changes_are_hidden`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_sleep_planner.SleepPlannerTests.test_nap_retains_original_pressure_window_and_daily_limit`：缺少 `tzdata`，无法加载 UTC 时区。
- `test_sleep_planner.SleepPlannerTests.test_nightly_window_advances_only_after_confirmed_sleep`：缺少 `tzdata`，无法加载 UTC 时区。
- `test_sleep_planner.SleepPlannerTests.test_pressure_and_busy_hold_until_the_window_closes`：缺少 `tzdata`，无法加载 UTC 时区。
- `test_sleep_planner.SleepPlannerTests.test_restart_after_bedtime_and_expired_window_skip_old_sleep`：缺少 `tzdata`，无法加载 UTC 时区。
- `test_staged_calendar_batch.StagedCalendarBatchTests.test_batch_is_atomic_fenced_and_recoverable`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_staged_calendar_transition.StagedCalendarTransitionTests.test_change_and_reminder_recover_without_exposing_unconfirmed_state`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_staged_calendar_transition.StagedCalendarTransitionTests.test_changed_calendar_blocks_host_finalization`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_staged_calendar_transition.StagedCalendarTransitionTests.test_create_checks_all_original_owner_keys_and_replays_once`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_state_algorithms_migration.StateAlgorithmMigrationTests.test_mood_and_cycle_computations_preserve_the_original_values`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `test_state_patrol.StatePatrolTests.test_imported_daily_history_is_preserved_and_updated_once`：缺少 `tzdata` 导致 Asia/Shanghai 时区不可用；清理临时数据库时另报 WinError 32。
- `test_state_patrol.StatePatrolTests.test_life_scheduler_runs_the_patrol_once_for_a_bound_role`：断言调用次数期望 1、实际 0；清理临时数据库时另报 WinError 32，最终按错误计数。

### 根测试：模块收集错误（40 项，非已执行用例）

以下保留 unittest 输出的完整 `_FailedTest` 标识；各项均未能成功导入对应模块。

- `unittest.loader._FailedTest.test_activity_creation_authority`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `unittest.loader._FailedTest.test_activity_deletion_authority`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_activity_transition_authority`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_adoption`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_authority`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_batch_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_command_bridge`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_executor_ownership`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_handoff_reminder_integration`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_life_planning`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_lifecycle_binding`：导入时缺少 `pytest`。
- `unittest.loader._FailedTest.test_calendar_ownership_audit`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_records_proxy`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_reminder_hold`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_calendar_rollback_rehearsal`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_configuration_life_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_exposure_life_projection`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_interrupted_work_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_journal_joint_snapshot`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_journal_life_snapshot_gate`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_dashboard`：导入时缺少 `pytest`。
- `unittest.loader._FailedTest.test_life_executor_ownership`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_interest_event`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_joint_snapshot`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_ledger_router`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_provider_binding`：导入时缺少 `pytest`。
- `unittest.loader._FailedTest.test_life_proxy_host_integration`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_proxy_proactive_integration`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_proxy_timeline_integration`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_life_wardrobe_event`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_manual_sleep_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_native_sleep_interruption`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `unittest.loader._FailedTest.test_natural_wake_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_prepare_life_cutover`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_reading_import_life`：缺少 `tzdata`，无法加载 Asia/Shanghai 时区。
- `unittest.loader._FailedTest.test_reading_life_execution`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_scheduled_sleep_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_sleep_interruption_handoff`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_social_life_execution`：导入时缺少 `regex`。
- `unittest.loader._FailedTest.test_state_text_migration`：导入时缺少 `regex`。

### CJS 失败（6 项）

- `test_companion_nested_confirm.cjs:140` — `嵌套工作区的确认层在插件视口内可见可操作，取消 0 写、确认 1 写`：等待本地浏览器 fixture 页面就绪超时，约 31.7 秒。
- `test_life_workspace.cjs:121` — `reading pace settings are submitted as numbers`：预期 1200，实际为 `undefined`。
- `test_maintenance_page.cjs:1656` — `maintenance shows connected external token ledgers without mixing their budgets`：输出未匹配 `V2 联动摘要`。
- `test_native_perception_page.cjs:67` — `environment summary points to the role weather settings below`：输出未匹配 `官方预警源尚未就绪 · 需和风凭据`。
- `test_page_scale.cjs:59` — `尺度审计通过：已迁移页面零写死、令牌单源、档位间距足够`：多个 CSS 文件的固定尺度及迁移名单未通过审计。
- `test_page_scale.cjs:73` — `所有已迁移页面都在判定名单内且零写死`：`models-page.css` 有 3 处固定间距和 1 处固定圆角。

## 运行方式

在仓库根目录运行以下命令。下方列出实际子进程命令及其环境变量；完整套件运行时外层调用 Python `subprocess.run(..., timeout=600)`，设置 600 秒上限。Node 的 `*.cjs` 作为单个参数传给 Node v26.7.0，由 Node 测试运行器展开；本次输出包含不同文件的测试和 1,227 项最终汇总，确认实际匹配并运行了文件。不要改用没有 glob 支持的旧版 Node。

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'plugins\astrbot_plugin_good_life;plugins\astrbot_plugin_private_companion;tools'
& '.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s 'tests' -q
& '.venv\Scripts\python.exe' -B -X utf8 'tests\test_activity_migration.py' -q
$env:PYTHONPATH = 'plugins\astrbot_plugin_good_life'
& '.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s 'plugins\astrbot_plugin_good_life\tests' -q
$env:PYTHONPATH = 'plugins\astrbot_plugin_private_companion'
& '.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s 'plugins\astrbot_plugin_private_companion\tests' -q
& '.venv\Scripts\python.exe' -B -X utf8 'plugins\astrbot_plugin_private_companion\tests\test_binding_store.py' -q
node --test 'plugins\astrbot_plugin_private_companion\tests\*.cjs'
```

后续完整基线需要隔离的 AstrBot SDK 和项目依赖，按实际测试框架分开运行并设置可观察的超时；不能把模拟结果当作真实外部服务或生产验收。

## CI 环境结果

2026-11-16，GitHub Actions Ubuntu、Python 3.12、Node 24；安装插件 requirements、pytest、regex、tzdata 和 PyPI `astrbot==4.29.4`。CI 已成功导入 `astrbot.api.web`；`test_good_life_entry` 自造的 `astrbot.api` 非包桩仍产生基线已记录的 SDK 错误，列为已知排除。

首次根测试运行（UTC）为 286 通过、19 失败、1 排除，其中 18 项此前因依赖缺失未执行；这些项目单列于 `tests/ci-needs-triage.txt`，继续执行为非严格 xfail，并在 job summary 逐项显示结果，不等于已确认产品缺陷。其余已知基线排除见 `tests/ci-known-failures.txt`。Node 首次运行发现两项时区差异，将 CI 时区固定为 Asia/Shanghai 后通过，第二轮 job 耗时 5 分 35 秒。contracts 缺少文件时正常通过，第二轮耗时 1 分 13 秒。主插件 Python 套件按文件分为八片，CI 首轮分片仍有多项失败，部分分片在 30 分钟时终止；失败包括缺失 CSV、SDK 接口差异及行为断言，详见 Actions 日志，不能把这些尚未排查的问题记为原基线已知失败。此套件改为每周日北京时间 02:00 和手动触发，仅报告、不阻塞；逐项记录失败并按文件汇总到 job summary 与合并 artifact，测试步骤 27 分钟超时以留出时间保存报告。耗时统计仅包含已完成的测试调用，超时测试可能不在其中。

PR 仅修改无关路径时跳过主插件 Node job；主插件、生活插件、契约测试或 CI 文件变更则运行，main push 和手动触发始终运行。根测试和契约测试始终执行；主插件 Python 不在 PR 或 push 时运行。
