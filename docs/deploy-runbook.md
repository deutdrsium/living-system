# 生产配套升级操作手册

本手册适用于第一阶段数据迁移完成后的配套代码升级，保留数据、配置和执行归属。

**生产安装本身、任何功能开关变更、任何人工回退，都需要用户明确批准。** 开发测试和草稿 PR 不代表这些批准。安装命令包含启动失败后自动恢复上一配套代码的保护；批准安装时应明确包含这一保护。如果自动恢复被中断，由用户批准后进行人工处理。

## 一次性安装受限包装器与审批工具

由用户在自己的可信 root SSH 会话中执行以下步骤，验收现有 sudoers 和 SSH forced command。`companion-deploy` 的 sudo 权限只允许调用固定的 `/usr/local/bin/companion-deploy` 包装器。`/usr/local/sbin/companion-approve` 只供 root 在自己的会话中直接运行，sudo 和 SSH forced command 都不得开放此入口。受限身份不得获得 shell 或 PTY。包装器以 `/bin/bash -p` 启动以忽略启动环境，并在执行后续程序时使用干净环境。

统筹者先从已审阅的同一提交准备以下四个源码文件，放入服务器 root 管理的新空目录 `/tmp/companion-deploy-source`，保留相对路径。

- `tools/deploy/companion-deploy`
- `tools/deploy/deploy_host.py`
- `tools/life_migration_preflight.py`
- `tools/deploy/companion-approve`

在本地可信检出中生成校验单，连同文件交给用户。受限上传接口只接收插件包。

```bash
sha256sum tools/deploy/companion-deploy tools/deploy/deploy_host.py tools/life_migration_preflight.py tools/deploy/companion-approve > wrapper-SHA256SUMS.txt
```

在 root SSH 会话中确认校验单来自已审阅提交。安装前运行下列路径检查。逐级核对工具、审批目录、数据目录和日志的每一级现有父目录均为 root 所有的真实目录，组和其他用户均无写权限。末端文件和目录如已存在，也必须归 root 所有、无组或其他用户写权限，且不得为符号链接。尚未创建的末端路径在安装后再检查。遇到不符合要求的现有路径，先停止安装并核查。

```bash
namei -l /usr/local/bin/companion-deploy /usr/local/sbin/companion-approve
namei -l /usr/local/lib/companion-deploy/deploy_host.py /usr/local/lib/companion-deploy/life_migration_preflight.py
namei -l /etc/companion-deploy/approved-bundles
namei -l /var/lib/astrbot/good-life-deploy /var/log/companion-deploy.log
```

确认后执行以下命令，任一检查失败即停止安装。如果已有未决作业，先按原作业记录完成恢复，再更换工具。

```bash
(
set -e
cd /tmp/companion-deploy-source
sha256sum -c wrapper-SHA256SUMS.txt
command -v docker
/usr/bin/python3 --version
/bin/bash -n tools/deploy/companion-deploy
/usr/bin/python3 -I -c 'import ast, pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ("tools/deploy/deploy_host.py", "tools/life_migration_preflight.py", "tools/deploy/companion-approve")]'
test ! -L /etc/companion-deploy
test ! -L /etc/companion-deploy/approved-bundles
install -d -o root -g root -m 0700 /etc/companion-deploy /etc/companion-deploy/approved-bundles
install -d -o root -g root -m 0755 /usr/local/lib/companion-deploy
install -o root -g root -m 0644 tools/deploy/deploy_host.py /usr/local/lib/companion-deploy/deploy_host.py
install -o root -g root -m 0644 tools/life_migration_preflight.py /usr/local/lib/companion-deploy/life_migration_preflight.py
install -o root -g root -m 0755 tools/deploy/companion-deploy /usr/local/bin/companion-deploy
install -o root -g root -m 0700 tools/deploy/companion-approve /usr/local/sbin/companion-approve
install -d -o root -g root -m 0700 /var/lib/astrbot/good-life-deploy
# 使用 touch，不能用 install /dev/null 覆盖已有审计记录。
touch /var/log/companion-deploy.log
chown root:root /var/log/companion-deploy.log
chmod 0600 /var/log/companion-deploy.log
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin SSH_ORIGINAL_COMMAND=status /usr/local/bin/companion-deploy
)
```

安装后重复上述 `namei -l` 检查。`good-life-deploy/` 及其 `staging/`、`operations/`、`backups/`、`previous/`、`failed/` 必须为 root 所有的真实目录。已存在的 `pending.json`、`active.json`、`last-operation.json`、`operations/*.json`、`*.lock` 与 `/var/log/companion-deploy.log.lock` 必须为 root 所有的普通文件，锁文件只能有一个硬链接。目录和文件均不得为符号链接或允许组及其他用户写入；有不符项时停止并核查。部分状态文件和锁会在首次调用时自动创建。后台作业复用同一个 `deploy_host.py`。

生产必须具备可用的 systemd 管理器和 `systemd-run`。在可信 root 会话中先执行无副作用的本机临时单元检查，确认命令成功退出；失败则停止，不进行生产发布或回退。

```bash
test -x /usr/bin/systemd-run && test -x /usr/bin/systemctl
/usr/bin/systemd-run --wait --collect --pipe --property=RuntimeMaxSec=15 \
    --property=Type=exec --property=KillMode=control-group -- /usr/bin/true
```

在 root 会话中检查受限身份的实际 sudo 权限。

```bash
sudo -l -U companion-deploy
```

确认输出只允许固定包装器，没有 `companion-approve`、解释器、shell 或通配规则等其他授权。专用 sudoers 规则必须启用 `env_reset`，先清空 `env_keep` 列表，再仅保留 `SSH_ORIGINAL_COMMAND`，并对该授权使用 `NOSETENV`；用 `visudo -c` 和 `sudo -l -U companion-deploy` 核对实际生效规则及环境设置。

检查受限密钥的 `authorized_keys` 使用 `restrict`，或同时设置 `no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-user-rc`。forced command 只通过 `sudo -n` 调用固定包装器。用与实际连接匹配的用户、来源地址和主机参数检查 sshd 的有效配置，确认 `permituserenvironment no`。

```bash
sshd -T -C 'user=companion-deploy,host=<client_host>,addr=<client_address>' | grep -x 'permituserenvironment no'
```

替换为实际客户端主机名和地址；任何验收不符均停止。随后从本地使用现有 SSH 配置验证。前两条应成功，后两条必须由包装器拒绝，不能进入审批程序。

```bash
ssh -T -o BatchMode=yes companion-deploy status
ssh -T -o BatchMode=yes companion-deploy 'logs 100'
ssh -T -o BatchMode=yes companion-deploy 'companion-approve'
ssh -T -o BatchMode=yes companion-deploy '/usr/local/sbin/companion-approve'
```

要求 `/usr/bin/docker` 存在，Python 至少 3.10。清理正在进行的上传需要可用的 `/proc` 与 Linux pidfd，可用 `/usr/bin/python3 -I -c 'import os, signal; assert hasattr(signal, "pidfd_send_signal"); os.close(os.pidfd_open(os.getpid()))'` 在 root 会话中检查。包装器固定容器名 `astrbot`、数据目录 `/var/lib/astrbot`，标准插件数据布局为 `plugin_data/<plugin>/`，宿主运行库在 `installations/main/runtime/companion-v2.sqlite3`，配置库在其 `configuration/` 子目录，生活库为 `plugin_data/astrbot_plugin_good_life/good-life.sqlite3`。如果实际布局不同，应停止并审阅工具适配。备份会拒绝插件数据、代码和配置树中的符号链接或特殊文件。

包装器逐次记录原命令及结果到 `/var/log/companion-deploy.log`，审计日志按 1 MiB 轮换并保留 3 代。包装器共有 4 个并发槽位，其中 1 个为 `status` 保留。`logs` 和 `status` 的总响应时限为 30 秒。发布和回退互斥，上传使用独立的上传锁，`operation` 只查询作业状态。已完成的作业记录最多保留 128 份、合计不超过 1 MiB，较旧的记录会被清理。重复提交同一份尚未领取的审批时，只保留最近一次领取前失败的作业记录。未决作业和持久审批领取记录仍保留。包装器装到固定位置后忽略演练环境变量，拒绝独立的 `backup`、`install`，也拒绝 `set-flag`、任意命令、路径参数、数据库恢复和 shell。`logs [行数]` 仅取最后 1–500 行，单次最多返回 256 KiB，并遮盖常见 Authorization、Cookie、token、secret、password、API key、JWT 和 URL 口令。分享日志前仍需检查正文是否含私有内容。

## 发布顺序

1. 遵守 [v2 配套契约](architecture-next/contract-v2-migration.md) 和 [原迁移上线门槛](architecture-next/18-implementation-roadmap.md)：先合入契约与两端兼容实现，保持 v1 默认；逐组启用 v2 需要另行审批。新旧在途命令、unknown 操作、去重、投递与恢复接口继续保留，不能降级重发。
2. 选择一个已提交且 CI 完全通过的源码 HEAD。不要单独更新其中一个插件。发布脚本拒绝任何未提交／未跟踪文件，检查该准确提交最新 CI 工作流及 check-runs／status；缺失、失败或仍在运行均拒绝。
3. 确认 A1/A2/A3/A4/A5/A6/A7/A8/A9 新功能仍关闭。当前 AstrBot 入口实际读取 `reading_executor_enabled`、`reading_abort_own_enabled`、`academics_enabled`；内核、钱包、校园、城市和连续状态模型仅为显式嵌入接口，默认无实例。包装器不写这些设置，并拒绝启用值、已安装的新领域表及 A3 历史。不要关闭整个已迁移的生活服务；已有生活调度仍应工作。
4. 用本地虚拟环境运行兼容检查和只读计划。Windows 使用 `.venv/Scripts/python.exe`，不要用 `py -3` 运行 pip／pytest。Windows 演练通过 Python 入口运行，Shell 包装器、`flock` 和进程组超时测试在 Linux 或 WSL 运行。

```powershell
.venv/Scripts/python.exe tools/rehearse_upgrade.py
.venv/Scripts/python.exe -m pytest -q tests/test_deploy_wrapper.py tests/test_deploy_wrapper_extra.py tests/test_deploy_archive_limits.py tests/test_deploy_release.py tests/test_upgrade_compatibility.py tests/test_good_life_entry.py
.venv/Scripts/python.exe tools/deploy_release.py --dry-run
```

5. 在本地运行 `--execute` 构建并上传待审批的包。

```powershell
.venv/Scripts/python.exe tools/deploy_release.py --execute
```

脚本在本机临时目录调用原 `build_paired_release.py`。构建器从准确 HEAD 的 Git 归档导出插件源码，再用既有打包逻辑生成两个 ZIP、配套清单与 SHA256SUMS。工作目录中被忽略的本地文件不进入包。脚本封装 tar.gz，计算整体 SHA-256，通过受限身份执行 `upload <sha256>`。`--dry-run` 只读 Git／GitHub 并打印计划，不构建或发起 SSH。

6. 上传验证通过后，脚本打印带实际值的审批命令。用户用可信的本地构建结果核对摘要、提交和两个目标版本，在**自己的 root SSH 会话**中运行打印出的完整命令。这一次批准涵盖备份、安装、启动后检查，以及启动失败时通过兼容性检查后的代码恢复。

```text
/usr/local/sbin/companion-approve release <sha256> <source_commit> <host_version> <life_version>
```

如需使用预留上传槽位，先在本地生成并保留将实际上传的包。用户在自己的 root 会话中核对该包的摘要、提交和版本，再运行相同的 `release` 审批命令。随后用受限身份执行 `upload <sha256>`，标准输入必须是刚才核对的同一个 tar.gz。验证通过后提交 `release <sha256>`。若上传锁或磁盘空间锁被占用，查询 `status`，必要时由 root 清理上传后重试。

7. 审批成功后，回到本地脚本输入 `approved`。脚本用受限身份提交一次 `release <sha256>`。服务器在同一作业中完成备份、安装和启动后检查。提交返回 32 位小写十六进制作业 ID。脚本用 `operation <operation_id>` 轮询，发布成功以作业最终结果为准。下面是脚本使用的 SSH 命令。脚本已提交后，人工只查原作业。

```bash
ssh -T -o BatchMode=yes companion-deploy 'release <sha256>'
ssh -T -o BatchMode=yes companion-deploy 'operation <operation_id>'
```

本地轮询中断或到期时，继续用已知的作业 ID 查询。若提交结果不明且没有拿到 ID，先查 `status` 和日志，必要时由 root 核对作业记录。确认请求从未受理、已验证上传仍在且原审批未被领取后，才能提交原摘要。受限接口没有另起作业的远程恢复命令。

每份审批只用一次。已领取的摘要即使发布失败，重试也需要用户重新批准。待原作业结束、恢复问题已处理且包仍有经过验证的上传后，用户才可在 root 会话中重新批准同一摘要，再按发布流程提交。

```text
/usr/local/sbin/companion-approve release --reapprove <sha256> <source_commit> <host_version> <life_version>
```

### 上传与备份限制

上传仅接受最多 64 MiB 的 tar.gz。tar 原始数据在解析前限为 65 MiB，包必须采用普通 USTAR 布局且恰好包含四个普通文件（两个 ZIP、配套清单、SHA256SUMS）。PAX／GNU 扩展头的数量和体积预算均为零。工具严格校验整体摘要、配套清单、每个 ZIP 摘要及逐文件 `RELEASE_FILES.json`，拒绝重复路径、目录项、链接、绝对路径和路径穿越。ZIP 内容展开最多 256 MiB。每个 ZIP 的中央目录最多 8 MiB、20,000 个条目，打开前先校验目录边界和实际条目数。清单和元数据文件各限 1 MiB。归档和包内 JSON 在独立子进程中解析，最多使用 384 MiB 地址空间、30 秒 CPU 时间和 45 秒实际时间。两个插件必须来自同一配套发布清单。

上传区有 8 个普通槽位，另有 1 个只供已获 root 审批的摘要使用的槽位。全部 9 个槽位共用 256 MiB 的总容量上限。未经审批的上传必须留出 64 MiB 加 4 KiB 元数据额度，供预批准包使用。已有上传占用该额度时，由 root 清理未经审批的包。下一次上传时会清理已存放 24 小时、未经审批且没有作业引用的包。单次上传接收数据的空闲上限为 15 秒、总时长上限为 300 秒。超时会清理临时文件并释放上传锁。上传和备份都会检查数据文件系统的空间，除操作估算外，至少还要留出 1 GiB。

同一发布作业停止容器、确认停写后，只读检查任务恢复证据、配置未决操作、SQLite 完整性和既有 good_life 归属。备份复制两个插件的源码、插件数据与宿主 `config/`，额外用 SQLite backup API 创建三个独立快照。备份位于 `/var/lib/astrbot/good-life-deploy/backups/<backup_id>/`，`manifest.json` 记录时间、插件版本、文件清单及摘要。备份检查通过后，该作业继续安装。作业结果会记录备份 ID。备份属于私有数据，不进入 Git，也不通过 SSH 接口导出。

`backups/` 的容量上限为 32 GiB。磁盘密集操作串行执行，备份写入受容量限制，完成后再执行保留策略。当前生效记录、最近一次发布或回退作业、未决作业引用的备份及对应的 `previous/` 代码目录都受保护。容量允许时保留最新 5 份备份，并额外保留不在其中的受保护备份。若仅受保护的备份加上本次新备份就会超过上限，本次发布会被阻止，即使磁盘还有空闲空间。

发现受限密钥被滥用时，由 root 先撤销该密钥。`purge-uploads` 只删除未经审批、未被作业引用的上传包。清理时会使用 Linux pidfd 和 `/proc` 核验上传锁持有进程的身份，再终止已确认的上传持有者。`cleanup-backups` 清理未受保护的中断备份或不完整备份。

```bash
/usr/local/sbin/companion-approve purge-uploads
/usr/local/sbin/companion-approve cleanup-backups
```

### 安装、超时与恢复

发布重新验证上传包并检查审批中的摘要、源码提交和两个目标版本。两个目标版本都必须比当前版本高，上传时也会拒绝低于第一阶段基线的代码。发布和回退都在停止容器前，把领取审批的事实持久写入原作业记录（journal）。领过的审批不会因失败或 SSH 断开恢复可用。

安装用本次作业刚完成、清单完整且与当前代码配对一致的备份。在容器保持停止期间，在同一文件系统上准备新目录，再依次更名替换两端代码目录。原目录保存在 `good-life-deploy/previous/<backup_id>/`。随后启动容器。启动检查核对两个版本与文件摘要、两端插件加载记录、`AstrBot started.`、插件失败或禁用记录，以及生产数据标记。启动检查和稳定性检查共用同一次 Docker 启动的 `StartedAt` 和 `RestartCount`，并仅读取该次启动以来的日志。稳定性检查确认容器持续运行且已配置的健康检查通过，并查找 `[GoodLife] host binding ready`。这是生活运行时就绪且宿主已确认提供方绑定的信号。两阶段等待各最多 90 秒。仅回退或恢复到早于该日志信号的原始生活版本 0.1.0 时，免查此信号。日志检查匹配 AstrBot 4.29 的日志格式。若格式改变，应先审阅工具更新，再继续安装。

若新代码启动或检查失败，工具停止容器，复查与人工回退相同的兼容性条件。通过后才恢复原配套代码、重启并报告发布失败。自动恢复只换代码，保留当前 SQLite、配置、开关和归属。无法确认恢复安全时，保留现场并进入 `manual-recovery`。

生产用 `systemd-run` 和 `Type=exec` 启动临时作业。监护进程将接管记录持久化后，包装器才确认提交成功。SSH 断开后已启动的作业仍在服务器运行。作业时限为 15 分钟，恢复最多再用 2 分钟，临时单元另预留 30 秒写入结果。恢复先记录为 `recovering`，各步骤共享剩余时间。未能在时限内完成时，记录 `manual-recovery` 和最近一次确认的容器状态。

- 若在备份期间超时、尚未开始替换代码，尝试用原代码重启容器。无法确认重启成功时由人工接手。
- 若替换代码已经开始，除非已有完整证据表明原作业已完成，否则保持容器停止并标记 `manual-recovery`，交由人工核查。

若后台监护异常退出，先在可信 root 会话核对 `systemctl show companion-deploy-<operation_id>.service`。提交结果未知时也核对同一个固定单元及原作业记录。确认临时单元已停止或不存在、监护进程和作业进程均已退出后，用户可运行 `/usr/local/sbin/companion-approve recover <operation_id>`，按原作业记录恢复。发布、回退及恢复在清理未决记录前均检查容器持续运行 5 秒、期间没有重启且已配置的 Docker 健康检查通过。未通过则保留 `manual-recovery`。出现 `manual-recovery` 或状态无法确认时，保留原作业记录、备份和日志。未决恢复会阻止新的发布和回退。统筹者在用户批准后核对日志、当前代码与 `previous/` 目录、数据库标记及容器状态。确认原作业及监护进程已结束、配套代码状态已核实后，才可在 root 会话中归档未决记录。直接删除记录会丢失恢复依据。主机断电或恢复进程被强制终止后，也按此流程核查。

## 安装后验收

`status` 显示容器、两端版本、RELEASE_FILES 摘要、生活归属／ready 标记和新功能开关。它不启动任务或修改设置。再检查 `logs 200`，确认没有插件加载失败、恢复屏障报错或重复调度。

必须由用户／统筹者在 Pocket ID 的真实浏览器登录流程中完成页面验收，这不由包装器的日志检查代替：

- 未登录访问两个插件入口、生活只读 API 和写入接口，仍应跳到登录或被拒绝，不能匿名读写。
- 登录后打开「陪伴小屋」和「我会去好好生活」；检查角色选择、日程、阅读、浏览、作息、状态的原记录可读，静态 CSS／JS 正常加载，生活服务显示已连接。
- 对照安装前内容确认无重置、丢记录或归属变化；仅在用户批准后做一次保持原值的配置保存，确认权限与回执。
- 新 A 线功能继续关闭；不因页面新增钱包／课业相关资源或路由就宣称功能已启用。iframe 跨插件链接仍遵守原 AstrBot 沙箱，可从左侧扩展功能切换或按 Ctrl／⌘ 打开。
- 观察原生活调度、去重与事件消费。模拟测试不能证明自然睡眠、梦境、换装或真实平台浏览已在线上发生。

## 回退

人工回退只适用于最近一次成功发布后的配套代码问题。确认新功能一直关闭、没有新领域历史、归属仍为 good_life。从发布作业结果取得 `backup_id`，用 `status` 核对预期的当前两端版本。审批命令填写当前正在使用的版本。用户在自己的 root SSH 会话中明确批准这一次回退。

```text
/usr/local/sbin/companion-approve rollback <backup_id> <expected_current_host_version> <expected_current_life_version>
```

审批成功后，在本地用受限身份提交一次回退，并查询返回的作业 ID，直到作业结束。提交或轮询中断时按发布的办法查询同一作业。若回退失败，处理完未决恢复后仍须重新取得 root 审批才能重试。

```bash
ssh -T -o BatchMode=yes companion-deploy 'rollback <backup_id>'
ssh -T -o BatchMode=yes companion-deploy 'operation <operation_id>'
```

回退只能选择最近一次成功发布前的配套代码。包装器核对审批中的当前版本、备份清单与前后代码摘要，停写后复查保护条件，只换回代码并执行同样的启动检查。备份中的数据库和配置留在原处，当前生活事件、消费去重和宿主记录全部保留。错误 ID、备份缺失或被改动、代码漂移、目标低于 7.1.17／0.1.0、新功能已开启、存在 A3 历史或新领域表、归属变更或有未决作业时，工具会拒绝回退。若换回代码后启动或检查失败，通过兼容性核查后才尝试恢复到回退前的代码。

如果需要退出第一阶段、切回旧执行归属，或存在新领域／v2 在途事实，**不能使用这个子命令，更不能恢复切换前数据库**。按 [存储恢复规则](architecture-next/16-storage-recovery.md) 先停写并保存当时三个库的一致性备份，运行 `tools/prepare_life_rollback.py` 对这些最新快照构建候选，保留切换后新增状态、生活记录、去重、事件与任务关联，核对 `rehearse_life_rollback.py` 和配置回迁结果，再审阅两端代码与执行归属的配套切换方案。新领域或 v2 事实未被该回迁工具覆盖时继续停止，先实现并演练保留这些事实的转换；不能把首次迁移前的快照当作回退候选。任何生产安装、数据／归属变更及回退都另行批准并记录。
