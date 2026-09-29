# Dalton Research OS 巡检记录（2026-09-24 起，每 4 小时一次）

本文件按批次汇总巡检中发现的问题、修复和部署结果。每批的详细说明见 `pending-deploy.md` 的"已部署"部分，owner 执行过的线上写入见 `owner-actions-2026-09-24.md` 和 `docs/ops/*.sh`。

| 批次 | 部署时间（UTC） | release | 主要内容 |
|---|---|---|---|
| a | 09-24 10:42 | 41a772dc | 事件判断因结算超支卡住 lease；模型家族被重置为未分类；规划器 input_too_large；claim 核验读错 spool；日志轮转参数；出版 worker 的 stdout |
| b | 09-25 06:12 | 2f726c84 | muse 家族；venv 改用符号链接（不再每次部署都弹 TCC 授权）；dossier 校验；SEC 季度数据；晨报支持性核验；券商溯源；周报证据刷新；NO_CHANGE 日汇总；质量评分；别名；reopen 防护；规划器空转；lease 与数据库锁；误退役恢复；下游同步；cockpit 中文译文；出版链路日上限 |
| c | 09-25 14:36 | efe93904 | cockpit 加载慢；文档研究预算与重入；SEC 治理和 CIK；P13i 与 mission 版本迁移；debate map；规划器翻转；出版重试；dossier 预留；行业改挂；事件判断合批与记账 |
| d | 09-26 12:12 | 41fe150a | debate map 升版后判重复；网关输出超长；假扣费挡住核验；事件判断按文档合批；入账质量（SEO、时间不可能、主体）；hold 原因；EPAM dossier；数字误报；core principal 权限；核验路由挂错档（改到 verifier 档） |
| e | 09-27 09:08 | 1b5f6b04 | 核验 v2（整句加元数据）；SEC 治理前置条件；writer 对 owner 请求超时；MSFT debate map；英文数字；IBM 终态 |
| f | 09-28 10:50 | f9fe3f3e | 核验独立成 support-only 子进程；同义去重；核验 v3（双家族）；dossier 日期与每 3 小时节流；SEC 超时；model_spec；新建 workspace 开箱即用（治理基线、去掉 legacy 硬编码、parity 工具、canary） |
| g | 09-29 ~11:30 | ae70706a | Guidepoint 迁移；10-K 第四季配对与 FY−9M 推导；数字去重；GOOGL segment_sum；DXC 数字；发言人；核验 v4；恢复 independence predicates；ws-7d 路由对齐和 xai 槽位；讲别的行业的 claim 改挂行业；legacy 文档断供（索引）；discovery 协调器公平分时 |

## 运维经验

- **部署**：用 `docs/ops/deploy.sh`。它会自动重试 launchd bootstrap 偶发的错误 5，也会处理 pypi 超时，并在最后打印摘要。
- **owner 步骤**：全部写成 `docs/ops/*.sh` 脚本，避免在终端粘贴长命令时被折行。
- **writer 刚重启时**：owner 请求可能报 unavailable；脚本都带重试，而且会先查一下这次写入是否其实已经完成。
- **环境一致性**：`scripts/check_workspace_parity.py` 每轮巡检都跑，新建 workspace 的 canary 在全量测试里。
- **子代理约束**：线上只读（mode=ro），禁止任何外部请求，请求里不得带用户邮箱，停测试进程按 PID 停，不用 pkill。
