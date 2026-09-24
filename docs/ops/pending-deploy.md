# 待用户部署清单

巡检（每 4 小时）发现、已修复并合并进 main、但需要你执行部署的事项集中在这里。部署完成后，把对应批次移到文末「已部署」。

- 状态目录约定：
  - legacy：`L="$HOME/Library/Application Support/Dalton/state/dalton-core"`
  - ws-7d：`W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core`
- 验证命令都是只读的（sqlite 一律 `mode=ro`）。

---

## 批次 2026-09-24b（main `ce0163bb` 及之后）

### 这批解决什么

| 主题 | 效果 |
|---|---|
| **muse-spark 家族**（`3a13e83d`） | 和其他模型一样进入人工目录，家族为 `meta-muse`。部署后三个环境都不再有未分类 profile，文档车道的独立核验预检通过（已在线上库副本上模拟验证）。 |
| **不再每次部署都弹外接盘授权**（`7adca1bb`） | release venv 的 python 改为指向 Homebrew python 的符号链接，这个 python 你 09/17 已授权过。完整性校验升到 0.3，把解释器的路径和哈希固定下来。**从这次切换起就不应再弹窗。** |
| **dossier 卡死**（`f6d43171`） | 校验规则自相矛盾：被撤回的 unit 要求 provenance 必须为空，同时又要求它等于上一版。IBM 因此自 09-17 起失败 14 次，一直停在 v9；ACN 停在 v20。 |
| **SEC 季度财报**（`a912a78e` `289d80cf` `8ed0d31b`） | statement lane 回补 8 个季度，并按到期日轮询新财报。company-facts lane 按最新 4 个季度判断是否需要派发，并能识别 submissions 形态的数据。AMZN 净利润和资本开支的概念映射也修了。周报会出现 CTSH 2026Q2；ws-7d 的估值快照能算出 TTM。 |
| **晨报 claim 核验**（`e41b4df8` `52393693` `db4701db`） | 入账前用廉价模型逐条核验是否有原文支持、说的是不是这家公司，不通过的转 held，每天上限 $0.15。存量回补默认慢速，每轮 1 批 × 20 条，每天上限 $0.50。 |
| **争议图券商溯源**（`ae25556d`） | 从 sales note 的发件域名识别券商（GS/BofA/Jefferies），争议图的独立性计数不再永远是 0/0。 |
| **周报证据包自动刷新**（`02b5990b`） | 每期出刊前从账本重建证据包。**需要部署后执行 P2 才生效。** |
| **NO_CHANGE 事件笔记**（`dd2b7105`） | 结论不变的研判不再每条都发一版，改为每家公司每天一版汇总。 |
| **质量评分阶段**（`8deec67c`） | 自动安装质量核验配置（复用 dossier-verifier 策略，单次上限 $1），新车道按限速调度评分。预计每天不到 $0.05。 |
| **公司别名**（`9ee415fe`） | 补齐 Google/谷歌/AWS/Azure/Facebook/埃森哲等别名，并新增正式的别名账本 CLI。 |
| **gate reopen 防护**（`17b83cb0`） | 提案针对的已经不是当前通过版本时，拒绝批准；过期的提案自动撤回。 |
| **UI 文本批次重试**（`fdceef45`） | 新增正式的重试入口。因路由或家族问题失败的批次，在路由修好后会自动重新起草。 |
| **OpenClaw 9.6 补丁**（`18b483e7`） | 仓库里的补丁支持 9.6，测试改用 fixture，不再依赖本机安装的版本。 |
| 其他 | 缺口文本末尾孤立汉字的校验；多类股股数；清理误提交的 sqlite 文件；测试时间炸弹；build 列出未提交文件时截字的 bug。 |

全量测试：见文末「测试记录」。

### ⚠️ 部署后会自动发生、会花钱的事

- **13 个 UI 文本批次各重新起草一次**：每批约 4 次调用，每轮最多 4 批。
- **晨报 claim 存量回补核验**：约 4,000 条、约 200 次调用，按实际计量约 $1.7，在每天 $0.50 的上限下分几天跑完。核验不通过的 claim 会经 challenge 自动退役。退役是追加记录，claim 仍可追溯，但不会自动恢复。我会在后续巡检中抽查被退役的 claim，确认没问题再提速。
- **抽取窗口会重算一次**：别名和提示词变了，在途窗口的 context hash 随之变化，可能各多一次调用（金额很小）。
- **ws-7d 的 SEC 回补**：4 家公司各补 8 份 10-Q，约 1 小时。SEC 数据免费，按 SEC 规定限速。
- **dossier 和质量评分**：IBM、ACN 的 dossier 会重新发布；legacy 5 份初筛会补做质量评分（一次性约 $0.1–0.2）。

### 部署命令

```zsh
cd ~/Projects/dalton-research-agent-os
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260924b.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260924b.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit $(git rev-parse HEAD) --apply
```

这次的 venv 是按新方式建的，**切换时应该不会再弹外接盘授权**。如果还是弹了，点允许，然后告诉我。

### 部署后执行（P1 → P2 → P3，都先 dry-run，确认后再加 `--apply`）

**P1 ws-7d 签入两条 SEC 自动入账规则。** 目前 ws-7d 有 409 条定量 claim 停在 staged，一条也没入账，初筛因此一直 idle。legacy 的 policy-17 早就包含这两条规则，这一步只是让 ws-7d 与 legacy 一致。dry-run 我已经跑过，结果是 `would-publish policy-4`。

```zsh
cd ~/Projects/dalton-research-agent-os
PY=~/.dalton/runtime/releases/$NEW/venv/bin/python
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
$PY scripts/sign_auto_commit_rules.py --state-dir "$W" --rule research-auto-commit:sec-statement-line:v1 --rule research-auto-commit:sec-public-company-facts-growth:v1
$PY scripts/sign_auto_commit_rules.py --state-dir "$W" --rule research-auto-commit:sec-statement-line:v1 --rule research-auto-commit:sec-public-company-facts-growth:v1 --apply --actor human:owner
```

**P2 周报切到 v4 排程**（启用证据包自动刷新，W40 之前做完即可）：命令在脚本 `scripts/switch_weekly_brief_plan.py` 写好后补到这里（正在写）。

**P3 恢复 document-research 的 hold**（legacy 15 条、ws-7d 5 条，muse 修好后才能成功）。先确认核验预检通过：

```zsh
.venv/bin/python scripts/check_model_family_independence.py   # 三个环境 document_verifier_preflight.status 都应为 ok，unclassified_profile_ids 都为 []
L="$HOME/Library/Application Support/Dalton/state/dalton-core"
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0 --apply --actor human:owner
export DALTON_WORKSPACE_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$W" --max-total-cost-usd 0
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$W" --max-total-cost-usd 0 --apply --actor human:owner
unset DALTON_WORKSPACE_MANIFEST
```

P1–P3 做完告诉我，部署后的验证由我在巡检中完成。

### 需要你决定的事项（都不影响部署）

- **E1 muse-spark-1.3-contributor**：这是"数据可被用于训练"的版本，目前在路由链里。券商晨报、专家访谈通常有许可限制，发给会用数据训练的模型可能有合规风险。**我建议从所有路由链移除 contributor**（在模型页操作）。要我改的话说一声。
- **E2 `brew pin python@3.14`**：锁住 Homebrew 的 Python 版本。否则 Homebrew 升级小版本后，外接盘授权要重新点一次，发布也要重建。建议执行。
- **E3（可选）核验走更便宜的模型**：两个 claim 核验用途目前落在 gemini-antigravity。在模型页把 zai-glm-5-3-flash 设为首选，费用约降到 1/3–1/5，总额本来也只有几美元。
- **E4（可选）重试 IBM、ACN 已用满次数的两个季度**（IBM `0000051143-25-000064`、ACN `0001467373-25-000169`）：需要作废旧的尝试记录。这两个是较早的季度，影响不大，可以不处理。

### 已由我处理的积压（记录见 `docs/ops/owner-actions-2026-09-24.md`）

- 47 条过期的 gate_reopen 提案已全部驳回：它们针对的都是已被取代的旧版本，批准反而会在没有新证据的情况下重开当前已通过的初筛。
- 在途草稿、DXC 和 CTSH 的 DIG、41 条 unreadable claim：查明会自动恢复，或者这批部署后会自动恢复。

### 测试记录

合并 SEC 分支后，在 main `ce0163bb` 上跑全量：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests`，共 10019 个，**全部通过**（skipped 4，0 error）。之前那个依赖本机 OpenClaw 版本的测试已改用 fixture。

---

## 已部署

- 2026-09-24 10:42 UTC：批次 2026-09-24a，`20a0f53c`（release `41a772dc…`）。你执行了部署、log-rotate 重装和第一次孤儿预留回收；第二次回收由我补跑，记录见 owner-actions。验证：服务正常，事件判断、dossier 和各核验恢复成功，日志轮转参数正确，claim 检测器 v2 正在运行。
- 2026-09-24 03:50：`b0292c13`（release `556268c6…`）
