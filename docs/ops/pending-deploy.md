# 待用户部署清单

巡检（每 4 小时）发现、已修复并合并进 main、但需要你执行部署的事项集中在这里。部署完成后，把对应批次移到文末「已部署」。

- 状态目录约定：
  - legacy：`L="$HOME/Library/Application Support/Dalton/state/dalton-core"`
  - ws-7d：`W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core`
- 验证命令都是只读的（sqlite 一律 `mode=ro`）。

---

## 批次 2026-09-25c（main `e60954bd` 及之后）

### 这批解决什么（均为 09-25 部署后验证中发现的问题）

| 主题 | 效果 |
|---|---|
| **cockpit 加载慢**（`53625232` `d8f4af06` `279c6144` `1d18154f` `fe15dbe2` `39bbcffb`） | 线上 overview 一次要 59–428 秒，页面 30 秒就超时。原因有四个：译文索引每次发布都全量重建（7.5 秒）；缓存上限小于实际 run 数，每 5 秒要重新解析约 3800 个 run；缓存键全表扫描；control 以 Background 优先级运行，CPU 和 IO 被限流。修复后发布后再刷新首屏只要 0.5 秒（原来 12.8 秒以上，甚至超时），log 接口从约 10 秒降到 1 秒以内，译文请求合并成少量批次。**需要额外执行下面的 C1。** |
| **文档研究先付费后被拒、P3 重入全部失败**（`4dc4ac63` `6d866f9e` `65660a62` `edbeca0c` `8e19713f` `35656887`） | token 预算把 CLI 网关自带的前缀算进去了，装不下时在发送前就拒绝（不付费），并换到链上下一个模型；PROVIDER_BUDGET_EXCEEDED 不再在同一路由上自动重试。已完成的阶段改为按当时的绑定核验，所以 mission 升版后也能重入；因系统性校验失败的重入会退还一次 grant；已完成的 admission 会移出 holds。按回放，**15 条被浪费的重入会自动恢复，不需要重新签发 P3**。 |
| **SEC**（`3c8fa7c0` `fc47930c` `485c9ad6`） | ws-7d 的 company-facts 车道按 mission 的 CIK 运行（AMZN/GOOGL/META/MSFT）。ClaimIndex 挪到领取 lease 之前构建。审批只绑定自己操作对应的 fixture，修复了 09-12 以来 75 次 discovery 失败，CTSH 2026Q2 可以入账。 |
| **P13i 与 mission 版本**（`2ca24c65` `55f360c4` `af17d30a` `d0adbc30` `c05a1d92`） | mission 升版后，旧版本已决定的文档带着决定迁入新版本，不再重读（ws-7d 模拟 1,084 份，约省 2,900 个付费窗口）。P13i 重评、人工 reopen、figures 和 prose 二次读取、搜索节奏都改为跨版本。 |
| **晨报 claim 核验**（`4fa1e3c4`） | 补上核验合同，入账前核验和回补才能跑起来；发送前就被拒的调用按 0 结算。 |
| **debate map**（`c8d61613` `340f64b3`） | 同一主题最短 6 小时重算一次，失败后退避；去掉已退役引用算作新版本。 |
| **规划器、出版、dossier**（`ae3cb1d3` `62240231` `b84353b9` `cee24324` `dbb4215e`） | dossier 状态来回翻转不再触发付费规划；"四家"等同于"4 家"；check_only 失败时有确定性回退；出版产品有了重试入口，部署后 19 个卡住的产品会自动重试一次；checker 引文里的空格差异会被重新锚定（114 个卡住的 stage 中 113 个能通过）；dossier 按路由的实际价格预留；因引用退役被清空的节会优先重新起草。 |
| **行业改挂、撤销收紧**（`ea243e88` `bc9dfe12` `390e0d3d`） | 约 97 条（legacy）和 72 条（ws-7d）行业层面的退役结论作为行业证据保留，只在行业口径下读取。撤销规则收紧到 v4，6 条过宽的撤销会被自动撤回。 |
| **事件判断花费**（`697d9c8b` `58d32c8a`） | 车道原本用自己的账本卡每天 $50 的上限，但调用失败时记 0（钱其实已经花了），9-24 实际花了 $55.3，账本只记了 $38.2。现在改按预算总账卡上限。低层级输入（卖方 note、专家片段、新闻等）同一公司同一天合并成一次调用，每条内容仍完整送给 Opus；同一份文档以前全是 NO_CHANGE 的，不再重复研判。public_web Claim、文件、电话会纪要仍然逐条单独研判。按 14 天历史回放：**漏判 0**，调用次数减少 24.5%，每天省 $7–29。 |
| **lease 与锁**（`0adf919e` `e4e37643`） | release 切换前领取的孤儿 lease 可以回收；预算库 admit 遇到锁冲突会重试。 |

全量测试：见文末"测试记录"。

### ⚠️ 部署后会自动发生、会花钱的事

- 15 条文档研究会自动重入：其中约 9 条直接完成，不再调用模型；约 4 条会走一次自动重试的模型调用（草稿阶段每次上限 $1）。
- 19 个出版产品各自动重试一次（每个用途都受日上限约束）。
- 行业改挂、撤回撤销、P13i 重评（约 213 份，每天最多重开 30 份）：判定部分不花钱，重读走正常队列。
- ws-7d 的 company-facts 车道开始为 4 家公司产出增长 claim（SEC 数据免费）。
- dossier 每次运行能完成更多 unit，预计每天 $8–15。

### 部署命令

```zsh
cd ~/Projects/dalton-research-agent-os
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260925c.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260925c.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit $(git rev-parse HEAD) --apply
```

### 部署后执行

**C1 把三个 control 服务改为 Standard 优先级**（cockpit 提速的一部分；release_switch 不会改 ProcessType）。只改 control，controller/writer 保持 Background：

```zsh
for p in space.lumos.dalton.control space.lumos.dalton.workspace.ws-7d894366d1132e2930475a60.control space.lumos.dalton.workspace.ws-e399ececd5aa7a3762b1a0a4.control; do
  f=~/Library/LaunchAgents/$p.plist
  plutil -replace ProcessType -string Standard "$f" && plutil -lint "$f"
  launchctl bootout gui/$(id -u)/$p
  launchctl bootstrap gui/$(id -u) "$f"
done
# 应输出 3 行 "Standard"
for p in space.lumos.dalton.control space.lumos.dalton.workspace.ws-7d894366d1132e2930475a60.control space.lumos.dalton.workspace.ws-e399ececd5aa7a3762b1a0a4.control; do plutil -extract ProcessType raw ~/Library/LaunchAgents/$p.plist; done
```

然后强制刷新 cockpit 页面。

**C2 授权因 PROVIDER_BUDGET 被挂起的文档研究**（每条只放一次，每次最多 $1）。部署后等 10–20 分钟，让车道先把能自动恢复的恢复掉，再执行：

```zsh
PY=~/.dalton/runtime/releases/$NEW/venv/bin/python
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
# 先看还剩哪些 hold，把结果贴给我也行，我来挑出需要授权的
$PY -m dalton_core.document_recovery_cli holds --state-dir "$L"
$PY -m dalton_core.document_recovery_cli holds --state-dir "$W"
```

目前已知的有 legacy b2f1a00d、ddf3a04b 和 ws-7d bec19d08、c737cc83，可能还有新增的。先 dry-run，确认后再加 `--apply`：

```zsh
$PY -m dalton_core.document_recovery_cli authorize-unproved --state-dir "$L" --admission-ref mission-document-research-admission:<ref> --max-cost-usd 1.0 --actor human:owner
```

ws-7d 执行前先 `export DALTON_WORKSPACE_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json`，执行完再 `unset`。

**不要再执行 P3 的 `authorize-all-escalated`**，这批部署后它们会自动恢复。

### 已替你决定的事项

- SEC 审批的语义调整：审批只绑定自己操作对应的 fixture。接受。安全性不变，修复了 75 次 discovery 失败。
- 出版自动重试也覆盖 check_only 的内容失败，否则已经卡住的 NO_CHANGE 研判不会受益于这次修复。

### 已知未修（设计决定，后续评估）

- figures/metric 窗口的身份绑定了 mission 版本（ADR-0006 的有意设计），每次升版会把同一份文档整份重读一遍。只在签策略、切周报这类升版时触发。
- 初筛、备忘录、stage readiness、年报研究等几处仍只认当前 mission 版本，升版后会重新起草。
- `mission_annual_research_lane.py:729` 用的是 `source:sec-filings`，其他地方都是 `source:sec-edgar`，待核实。

### 测试记录

所有分支合并后在 main `e60954bd` 上跑全量测试：共 10333 个，**全部通过**（skipped 4，0 error）。

---

## 已部署

- 2026-09-25 06:12 UTC：批次 2026-09-24b，`d3c1f687`（release `2f726c84…`）。用户执行了 P1、P2、P3。**本次切换没有弹出外接盘授权**，venv 符号链接修复已生效。部署后验证见巡检记录。下面保留该批次的完整说明，供查阅。

<details><summary>批次 2026-09-24b 说明</summary>

## 批次 2026-09-24b（main `72b86d0f` 及之后；含第 2、3 轮巡检的修复）

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
| **第 2 轮巡检：文档研究车道崩溃**（`e17fbc22`） | 某个 admission 名下有多张终态票据，每个 tick 都在同一处崩溃，整条车道停摆。修复后，单条 admission 出错只把这一条挂起，恢复任务和新 admission 轮流使用唯一的子进程槽位。 |
| **第 2 轮：规划器空转**（`b4ca6eb9`） | 状态哈希里包含今日花费，导致每 5 分钟就调用一次 Opus（约 $100/天）。修复后，没有实质变化时至少间隔 2 小时才重新规划；按回放，ws-7d 的调用从 19 次降到 2 次。 |
| **第 2 轮：lease 挂住与锁**（`264056cd` `120a1c9d` `9b5cfb8b`） | complete 遇到锁冲突时有界重试，仍失败则留下持久记录，下一次请求会回收孤儿 lease；busy_timeout 统一为 30 秒；route 查询补了缺失的索引；子进程日志不再被覆盖。 |
| **第 2 轮：误退役恢复**（`344a945a`） | 新增追加式的"撤销退役"表和人工 CLI；自动复核按新规则，误杀会被撤销（模拟中 14 条确认误杀全部撤销，另有少量边缘条目）。退役判定改为整词匹配，并考虑上下文指代、本公司文件（XBRL/10-K 封面、名称密度）。 |
| **第 2 轮：下游同步**（`249f0178` `11b0b9f2` `5c9ca488` `1a96d500` `c636469a`） | 引用已退役 claim 的 dossier unit 会重新起草，debate map 会重算，cockpit 上标注"引用已退役"；本地化中截断的重复章节被去除；UI 文本改为按 batch 分文件存储（原来单文件超过 16MB 上限，导致 28 个产品一直 pending）；百科、简介类页面不再算作新闻事件；维度值为 0 的空格子不再提升为 claim。 |
| **第 2 轮：抽取主体名称**（`0fc37747` `5294c4ee` `1f3c0b97` `ae400773` `fca3bbe2` `0342a981`） | 抽取子进程现在能拿到完整名称表，ws-7d 不会再全部误拦；按旧规则驳回的 P13i 文档每天最多重开 30 份（约 160 份，起草费约 $0.15）；补充 GOOG、Gemini、YouTube、Red Hat 等别名，名称按整词匹配；claim 的 created_at 改为实际写入时间；抽取提示词禁止推断。 |
| **第 2 轮：earnings_preview**（`e4ac1c31`） | 输出上限从 2000 调到 4000。原先模型已经答完并计费，却因超出上限被拒。 |
| **cockpit 中文译文**（`c2f8bfe9`） | 原来译文映射超过 2MB 就被整体丢弃（线上有 4.8MB），页面上一条译文都不显示。现在改为按页面、按需下发，单次响应约 12KB；实测显示的已审核译文从 0 条增加到 205 条。**部署后请强制刷新页面**。另外，要等 ui-texts.json 在第一次发布时自动迁移到 0.2 格式后，译文才会显示出来。 |
| **第 3 轮：出版链路花费失控**（`69f93775` `e4b03b89`） | 9-23 模型目录修好后，语言修订按设计回到 Opus（"brain" 档）。出版 worker 在补低价值积压，4 小时花了 $52，照此一天约 $300。修复内容：修订、检查、草稿、核验四个用途分别设日上限（$25、$5、$3、$3），到上限就推迟、不算失败；高价值产品优先处理；NO_CHANGE 研判和 cycle reflection 只做检查，再加确定性修订，每个 chunk 约 $0.02，原来约 $0.5–0.7。预计部署后全任务每天约 $90–105。 |
| 其他 | 缺口文本末尾孤立汉字的校验；多类股股数；清理误提交的 sqlite 文件；测试时间炸弹；build 列出未提交文件时截字的 bug。 |

全量测试：见文末「测试记录」。

### ⚠️ 部署后会自动发生、会花钱的事

- **13 个 UI 文本批次各重新起草一次**：每批约 4 次调用，每轮最多 4 批。
- **晨报 claim 存量回补核验**：约 4,000 条、约 200 次调用，按实际计量约 $1.7，在每天 $0.50 的上限下分几天跑完。核验不通过的 claim 会经 challenge 自动退役。退役是追加记录，claim 仍可追溯，但不会自动恢复。我会在后续巡检中抽查被退役的 claim，确认没问题再提速。
- **抽取窗口会重算一次**：别名和提示词变了，在途窗口的 context hash 随之变化，可能各多一次调用（金额很小）。
- **ws-7d 的 SEC 回补**：4 家公司各补 8 份 10-Q，约 1 小时。SEC 数据免费，按 SEC 规定限速。
- **dossier 和质量评分**：IBM、ACN 的 dossier 会重新发布；legacy 5 份初筛会补做质量评分（一次性约 $0.1–0.2）。
- **引用已退役 claim 的 dossier unit 重新起草**：共 16 个 unit（IBM 8、ACN 4、CTSH 2、EPAM 2），每轮最多 3 个，约 7 轮完成，约 $1（硬上限 $17.5）；debate map 最多重算 6 个主题；约 30 个 surface 产品重新本地化一次。
- **误退役自动撤销**：ws-7d 约 20 条、legacy 约 2 条，不花钱。撤销后相关 dossier 会重算。
- **P13i 文档重新评估**：每天最多重开 30 份，重新阅读走正常队列和预算。
- **部署后第一次打开 model-router 库会建索引**：需要一两秒，其他写入方最多等 30 秒。

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

**P2 周报切到 v4 排程**（启用证据包自动刷新，10-01 11:00Z 的 W40 之前做完即可）。

- 做什么：发布 policy-18，在 `allowed_plan_bindings` 里加入 v4，v3 保留；级联更新 constitution 16 和 mission 26；把 service.json 里的 plan 换成 v4。service.json 会先备份，再原子替换。
- 为什么必须等部署之后：当前线上 release 不认 0.2 格式的 plan，在旧 release 上执行 `--apply` 会被脚本拒绝（`runtime_ready: false`）。
- 重复执行是幂等的。如果恰好撞上已经出刊的时段，只会返回 already_issued，不会重复出刊。

```zsh
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
C="$HOME/Library/Application Support/Dalton/config/service.json"
# dry-run：应为 runtime_ready=true、would-publish
PYTHONPATH=$PWD/src $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C"
PYTHONPATH=$PWD/src $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C" --apply --actor human:owner
# 只重启 controller，writer 不用重启
launchctl kickstart -k gui/$(id -u)/space.lumos.dalton.controller
# 应为 switched，heartbeat 显示 v4 且状态为 waiting
PYTHONPATH=$PWD/src $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C" --verify
```

注意：刷新机制本身不会产生新内容。W40 能不能出现新 claim，取决于 SEC 季度修复部署后 CTSH 2026Q2 等数据能否入账。

**P3 恢复 document-research 的 hold**（legacy 15 条、ws-7d 5 条，muse 修好后才能成功）。先确认核验预检通过：

```zsh
# 三个环境 document_verifier_preflight.status 都应为 ok，unclassified_profile_ids 都为 []
.venv/bin/python scripts/check_model_family_independence.py
L="$HOME/Library/Application Support/Dalton/state/dalton-core"
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0 --apply --actor human:owner
export DALTON_WORKSPACE_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$W" --max-total-cost-usd 0
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$W" --max-total-cost-usd 0 --apply --actor human:owner
unset DALTON_WORKSPACE_MANIFEST
```

P1–P3 做完告诉我，部署后的验证由我在巡检中完成。

**P4（可选）ws-7d 那 5 条被误拦的 AMZN 候选**：新规则下它们能通过，但已经进入人工评审队列，不会自动入账。可以在 cockpit 的候选评审里接受；不处理也没关系，同一篇文章后续重新抽取时会按新规则入账。

### 需要你决定的事项（都不影响部署）

- **E1 muse-spark-1.3-contributor**：这是"数据可被用于训练"的版本，目前在路由链里。券商晨报、专家访谈通常有许可限制，发给会用数据训练的模型可能有合规风险。**我建议从所有路由链移除 contributor**（在模型页操作）。要我改的话说一声。
- **E2 `brew pin python@3.14`**：锁住 Homebrew 的 Python 版本。否则 Homebrew 升级小版本后，外接盘授权要重新点一次，发布也要重建。建议执行。
- **E3（可选）核验走更便宜的模型**：两个 claim 核验用途目前落在 gemini-antigravity。在模型页把 zai-glm-5-3-flash 设为首选，费用约降到 1/3–1/5，总额本来也只有几美元。
- **E5 行业层面结论**：约 54 条 CIO 调查类结论（CTSH 44、EPAM 10）和约 101 条 hyperscaler 资本开支结论，按公司口径被退役了。账本契约不允许直接改挂主体。可以另做一张"行业改挂"表，供行业框架和周报读取。要不要做？
- **E7 16 个卡住的 pending 产品**：按现有逻辑，同一 hash 的 pending 不会再被尝试。其中有 3 个 dossier、4 个初筛、thesis、周报、2 个 model notes 的本地化，当初卡住的原因是预算拒绝、database locked、检查员引用对不上等。部署后我会查清这些原因是否已经消除，再决定要不要加一个正式的重试入口。
- **E4（可选）重试 IBM、ACN 已用满次数的两个季度**（IBM `0000051143-25-000064`、ACN `0001467373-25-000169`）：需要作废旧的尝试记录。这两个是较早的季度，影响不大，可以不处理。

### 已由我处理的积压（记录见 `docs/ops/owner-actions-2026-09-24.md`）

- 47 条过期的 gate_reopen 提案已全部驳回：它们针对的都是已被取代的旧版本，批准反而会在没有新证据的情况下重开当前已通过的初筛。
- 在途草稿、DXC 和 CTSH 的 DIG、41 条 unreadable claim：查明会自动恢复，或者这批部署后会自动恢复。

### 测试记录

所有分支（包括第 2 轮巡检的修复）合并后，在 main `72b86d0f` 上跑全量测试：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests`，共 10174 个，**全部通过**（skipped 4，0 error）。

---

</details>


- 2026-09-24 10:42 UTC：批次 2026-09-24a，`20a0f53c`（release `41a772dc…`）。你执行了部署、log-rotate 重装和第一次孤儿预留回收；第二次回收由我补跑，记录见 owner-actions。验证：服务正常，事件判断、dossier 和各核验恢复成功，日志轮转参数正确，claim 检测器 v2 正在运行。
- 2026-09-24 03:50：`b0292c13`（release `556268c6…`）
