# 待用户部署清单

巡检（每 4 小时）发现、已修复并合并进 main、但需要你执行部署的事项集中在这里。部署完成后把对应批次移到文末「已部署」。

- 状态目录约定：`S=~/Library/Application\ Support/Dalton/state/dalton-core`（legacy），`W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core`
- 所有验证命令只读（sqlite 一律 `mode=ro`）

---

## 批次 2026-09-24a（main 合并提交 `86fbfdec`）

### 背景：为什么要部署

09-24 03:50 部署 `b0292c13` 后研究几乎停摆：

1. **事件判断车道停摆约 19 小时**：CLI gateway 每次调用隐含约 24k token 的缓存写入，实际费用（约 $0.27）超出预留（约 $0.11），结算时抛异常，`scheduler.complete` 执行不到，工单 lease 挂住约 2 小时，之后同一请求一直报 "already running"。
2. **模型家族被重置为 unclassified**：catalog 对账把 6 个升级过型号的 profile 家族改成未分类，文档车道和出版 worker 的独立核验预检因此全部失败。
3. **ws-7d 研究规划器卡了 6 天**：prompt 82.6KB，超过 64KB 上限，一直报 `input_too_large`；legacy 也有同样问题。
4. **Claim 质量**：纠错检测器读错了 spool，在 ws-7d 上 100% unreadable，等于没在工作；约 30% 的定性 claim 挂错公司，时间写成 "current"，引用切片也会引错。
5. 次要问题：日志轮转自 09-17 起每天失败；出版 worker 每行 stdout 约 415KB，日志已 300MB；GOOGL/META 市值少算了多类股；催化剂日历会显示已经过去的日期。

另外：OpenClaw 在 03:52 自动升级到 9.6 后 gateway 停了，08:14Z 补丁移植到 9.6 并重启后已恢复。这件事不需要 Dalton 部署。

### 包含的提交

| 分支 | commit | 内容 |
|---|---|---|
| fix/budget-settle-lease | `1a742775` | 超支时按实际费用结算并记告警，lease 一定释放；ceiling 计入 CLI gateway 开销；新增孤儿预留回收脚本 |
| fix/family-and-logrotate | `174ee33d` `0062beb0` `b56279df` | 模型家族按谱系推导，不再重置为未分类；log-rotate 参数改回 `--apply` 并覆盖子目录；出版 worker stdout 只输出摘要 |
| fix/planner-shares-catalyst | `0b8c3488` `36f179f4` `57bcc82c` `c35b684e` | 规划器 prompt 新增压缩档，保证能收敛到上限内；多类股股数；催化剂日期改为实时计算 |
| fix/claim-review-extraction | `5740179f` `961317d3` | 纠错检测器读取所有 spool 和网页渲染后的原文，并检查引用片段；抽取时带文档日期、按句引用、引用片段必须出现主体公司 |

全量测试：见文末「测试记录」。

### ⚠️ 部署前先看：部署后会自动发生的事

- **大约 950 条错挂的 claim 会被自动退役**（ws-7d 约 393 条，legacy 约 555 条）。只读试跑抽查了 12 条，全部确实挂错了公司。退役是追加记录，账本不改，退役的 claim 仍可追溯。
- open review 的窗口会因为提示词契约变了而重抽一次：ws-7d 148 个、legacy 356 个，一次性约 $0.25。
- 6 个 profile 的家族会被自动修正（追加新版本）。两个 muse-spark profile 仍是 unclassified，所以**文档车道的独立核验预检仍会失败，要等你处理下面的决策 D1 才会通过**。

### 部署命令

```zsh
cd ~/Projects/dalton-research-agent-os
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260924a.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260924a.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit $(git rev-parse HEAD) --apply
```

**log-rotate plist 要单独重装一次**（`release_switch` 只改 `ProgramArguments[0]`，改不到参数）：

```zsh
repo=~/Projects/dalton-research-agent-os
venv=~/.dalton/runtime/releases/$NEW/venv
logs="$HOME/Library/Logs/Dalton"
dalton_home="$HOME/.dalton"
sed -e "s#@@RELEASE_VENV@@#$venv#g" -e "s#@@LOG_DIR@@#$logs#g" \
    -e "s#@@REPO@@#$repo#g" -e "s#@@DALTON_HOME@@#$dalton_home#g" \
    "$repo/deploy/macos/launchagents/com.dalton.log-rotate.plist.template" \
    > "$HOME/Library/LaunchAgents/com.dalton.log-rotate.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.dalton.log-rotate.plist"
launchctl bootout gui/$(id -u)/com.dalton.log-rotate 2>/dev/null
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.dalton.log-rotate.plist"
```

**孤儿预留回收**（可选，见决策 D2）：先 dry-run，确认后加 `--apply`；约 2 小时后再跑一次，处理当时还在 lease 期内的那些。

```zsh
STATE="$HOME/Library/Application Support/Dalton/state/dalton-core"
.venv/bin/python scripts/settle_orphan_cockpit_reservations.py --budget-db "$STATE/thesis-impact-budget.sqlite" --scheduler-db "$STATE/scheduler.sqlite" --broker-journal ~/.openclaw/dalton-model-broker.sock.journal.json
# 确认后在末尾加 --apply
```

### 部署后验证（全部只读）

详细命令见 `docs/reports/owner-runbook-2026-09-24.md`，要点：

1. **服务与版本**：`launchctl list | grep dalton`，11 个服务都有 PID；`plutil -p ~/Library/LaunchAgents/space.lumos.dalton.controller.plist | grep releases`，路径里的哈希应为 `$NEW`。
2. **事件判断**：新的 event-judgement-runs 下的 `summary.json` 不再出现 "already running" 或 "exceeds the admitted reservation"；`sqlite3 "file:$S/core.sqlite?mode=ro" "select max(created_at) from model_invocations"` 持续前进。
3. **模型家族**：`.venv/bin/python scripts/check_model_family_independence.py` 输出的 `family_drift` 为 `{}`，未分类的只剩两个 muse。
4. **规划器**：ws-7d `research-plans/latest.json` 不再是 `input_too_large`，`projection.rule_ref` 以 `:0.4` 结尾。
5. **Claim 纠错**：`sqlite3 "file:$W/core.sqlite?mode=ro" "SELECT detector_ref,outcome,count(*) FROM claim_review_examinations GROUP BY 1,2"`，v2 的行数逐步增加，ws-7d 的 unreadable 趋近于 0。
6. **新 claim 质量**：`sqlite3 "file:$W/core.sqlite?mode=ro" "SELECT json_extract(claim_json,'$.period') FROM claim_versions WHERE created_at>='<部署时间>' AND json_extract(claim_json,'$.claim_kind')='qualitative' LIMIT 30"`，不再出现单独的 "current"。
7. **日志**：`plutil -p ~/Library/LaunchAgents/com.dalton.log-rotate.plist | grep -A12 ProgramArguments`，最后一项是 `--apply`；`tail -1 ~/Library/Logs/Dalton/publication-worker.stdout.log | wc -c` 约 1KB。
8. **股数与催化剂**（下一个交易日之后）：GOOGL 股数约 12.2e9、META 约 2.55e9；META 的 `next_catalyst_date` 不早于 as_of。

### 需要你决定的事项（不影响部署本身）

- **D1 muse-spark 家族**：在模型页给两个 muse profile 声明一个独立家族，或者把 muse 移出文档草稿链。不处理的话，文档车道的独立核验会一直失败。
- **D2 孤儿预留**：是否运行回收脚本。按实测费用结算后，09-22 到 09-24 的账面消耗会从 $12.9 变成 $30.2，之后不再挤占每日额度。没有实测费用的，默认按预留金额结算，也可以加 `--unknown-cost zero` 按 0 结算。另有约 $23 属于其他 cockpit 用途，要不要加 `--all-cockpit` 一起处理。
- **D3 在途草稿**：ws-7d 有 55 条路由决策记录的家族是 unclassified，基于它们的草稿仍会报 verifier_not_independent。要不要重新起草或重新排队？
- **D4 晨报 claim 的支持性核验**：用 deepseek-flash，每天不到 $0.06，存量补核验一次约 $1.5–2。需要确定预算归属，以及核验失败时是 held 还是直接退役。
- **D5 周报证据包**：W36–W38 三期都引用同样的 5 条 claim，因为 `us-it-services:live-sec-lane-v2` 只在 08-27 登记过一次。要刷新这个包，还是做一个自动刷新的 lane？
- **D6 争议图的券商来源**：所有争议图的来源独立性都是 0/0，永远升不到 live。要不要从晨报里提取券商？
- **D7 NO_CHANGE 事件笔记**：ACN 在 4 小时内发了 9 个几乎一样的版本。要不要改成结论不变时不发新版，按日汇总？
- **D8 积压待决**：legacy 有 47 条 gate_reopen 提案、23 条 document-research hold（其中 14 条是 reentry_failed，建议 D1 解决后再授权）；DXC 的 DIG 已等新证据 7 天；CTSH 不在 DIG 候选里。
- **D9 别名**：在各 workspace 的 feed plan `companies[].names` 里补上 Google、AWS、Facebook、Azure、埃森哲等。缺别名会让主体检查更严，更多 claim 被 held。
- **D10 质量评分阶段**：仍未启用。要启用的话，需要选路由策略和单次费用上限。
- **D11 次要事项**：legacy 里有 1 份旧版 pypdf 渲染的 PDF，关联 41 条 claim，一直 unreadable；EPAM dossier 末尾多了一个"中"字，是模型原始输出带的；多类股股数要不要改用 SEC dei 作为来源；仓库根目录有两个误提交的 `<sqlite3.Connection object …>` 文件；OpenClaw 自带的 `test_openclaw_sqlite_integrity_timeout_patch` 在 9.6 上失败，需要更新到支持的版本。

### 测试记录

合并后在 main `86fbfdec` 上跑全量（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests`）：共 9846 个，skipped 4，errors 1。唯一的 error 是 `test_openclaw_sqlite_integrity_timeout_patch`，原因是本机 OpenClaw 为 9.6，补丁只支持 9.3；合并前在 main 上同样失败，与本批改动无关。

---

## 已部署

- 2026-09-24 03:50：`b0292c13`（release `556268c6…`）
