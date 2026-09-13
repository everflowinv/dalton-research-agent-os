# Dalton 开发与发布文件清理

Owner 要求清理磁盘，同时明确开发继续进行。清理工作与 Cockpit 修复、产物处理并行。

本次移除了以下过期文件：

- R17a、R18b、R20、R21、R22b 发布包中的 `databases`、`restore`、`venv` 回滚载荷，共 15,877,947,392 字节；各包的发布清单、验收记录、日志、wheel、配置快照保留，并写入回滚载荷已退役说明。
- `/private/tmp` 中可重建的 Dalton 开发、探针和演练副本，共 3,792,261,120 字节。
- Dalton 备份目录中三份无 manifest、不完整、无引用、无打开句柄的隐藏临时数据库备份，共 1,465,196,544 字节。
- 72 个干净、已合入、非活动路径的历史临时 worktree，约 3.92 GiB。使用不带 `--force` 的 `git worktree remove`，分支和提交保留。

当前 R24 和 R22c、R23、R24 三份完整发布回滚包保留；三份最新完整数据库快照保留。用户研究资料、所有 live/OpenClaw 数据库、已付费的语言审校缓存、当前发布准备文件、未合并或有改动的工作树均保留。清理结束时磁盘可用空间约 27 GiB。

精确路径、删除前核验及空间读数分别见：

- `/Users/everflow/Projects/dalton-owner-activation-20260910/storage-cleanup-2026-09-13.json`
- `/private/tmp/dalton-dev-scratch-cleanup-0913.json` 及 `-v2.json`、`-v3.json`
- `/private/tmp/dalton-live-backup-cleanup-20260913.json`
- `/private/tmp/dalton-worktree-cleanup-result-0913.json`

后续每次成功发布后检查历史回滚载荷，保留当前发布所引用的回滚包及最近三份完整回滚包；过期载荷清理后保留发布证据并注明已退役。演练完成后及时移除其大数据库副本。此规则尚未实现为自动定时删除任务。
