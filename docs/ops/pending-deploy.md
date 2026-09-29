# 待用户部署清单

巡检（每 4 小时）发现、已修复并合并进 main、但需要你执行部署的事项集中在这里。部署完成后，把对应批次移到文末「已部署」。

- 状态目录约定：
  - legacy：`L="$HOME/Library/Application Support/Dalton/state/dalton-core"`
  - ws-7d：`W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core`
- 验证命令都是只读的（sqlite 一律 `mode=ro`）。

---

## 批次 2026-09-28g（main `14e140c8` 及之后）

### 这批解决什么（批次 f 部署后验证中发现的问题，以及待你决定、已代为处理的事项）

| 主题 | 效果 |
|---|---|
| **Guidepoint 待处理 review 迁不过去**（`9d2ed711` `2d268987` `12c5d3c7` `229ee7c5`） | 迁移判定只把"已结算或已有 review"的后续行算作接管，重搜留下的 `already_in_authority` 行不再挡住迁移。Guidepoint 车道补上已持有文档的对账。parity 的判定口径与迁移保持一致；抽取 tick 汇总会报告被过滤的条数和原因。模拟结果：legacy 迁 164 条、ws-7d 迁 42 条，没有重复阅读，起草费用约 $0.10。canary 加了"两次升版中间夹一次重搜"的用例。 |
| **10-K 第四季配对**（`ab2d3112`） | "最近四个季度"把 10-K 所报的第四季也算进去，按各公司自己的财年处理，并与 bounded-planner 路径防重复派发。legacy 会派发 ACN 的 10-K（Q4 增速 +7.26%）。AMZN、GOOGL、META、MSFT、IBM、EPAM、CTSH、DXC 的 10-K 只报全年数，现有规则答不了，见下方"待办"。 |
| **数字入账去重与标签**（`ab994fdd` `0894dead`） | 同一文档按 (公司, 指标, 起止日期, 数值×量级) 只入账一次；标签取申报的 XBRL 行标签。 |
| **GOOGL segment_sum 卡住**（`940fb5b4`） | 申报了对账项的差额算作通过；分部成员不完整时判为 not_applicable。GOOGL 回放 38 组全部解除，META、AMZN、MSFT 结果不变。 |
| **DXC "970" 反复被拒**（`9e7056ff`） | 被引的 "$970 million" 可以支撑 "970 million 美元"；数字被拒时给一次修复机会，不再每轮都付费重放同一个被拒的回答。 |
| **纪要缺少发言人**（`b56ab1bb`） | 编号式的"发言人1/2/3"按未知处理；"Name :" 这种逐行标注能解析出发言人。核验升到 v4，prompt 写明"发言人未知"不等于"说话的是别人"。v3 拒绝过的约 570 条会各重新核验一次，费用只有几美元。 |
| **第四季 FY−9M 推导**（`84afb8ce`） | 10-K 只报全年数时，第四季 = 全年 − 同财年九个月。输入全部是已入库的申报行，可逐字节重放；按申报精度用区间计算，误差界超过 0.5pp 就拒绝；财年边界必须逐日对齐；出现概念变化或重述时拒绝。新 workspace 默认签入这条规则。模拟结果：IBM +12.1%、CTSH +4.9%、EPAM +12.75%、DXC −1%、AMZN +13.63%、META +23.78%、MSFT +17.75%。GOOGL 因为收入概念改名被拒。 |
| **"讲的是别的行业"的退役改挂到行业**（`8d09d78c`） | 核验判定原文支持、但说的不是本公司的 claim（supported + about_other），如果说的正是本 mission 的行业（例如 hyperscaler capex、IT services），就改挂为行业证据，不再直接丢掉。说的是别家公司、别的行业或别的地区，一律不改挂。等 v4 重核之后才生效，预计 ws-7d 9 条、legacy 2 条。 |
| **legacy 文档断供（web、SEC、AlphaEngine 采集）**（`1d0bcfbc`） | 三个 discovery 协调器共用 20 秒的 tick 预算。AlphaEngine 开头有一条"已持有对账"SQL，缺索引，又因为亲和性用不上表达式索引，在冷缓存下要跑 16 秒，把预算耗光；结果 web 从 09-25 起一次都没派发，AlphaEngine 有 775 份已发现文档一份都没采。现在新增两个索引并修正比较写法，实测从 16.2 秒降到 0.12 秒（web 对账从 10 分钟以上降到 0.57 秒）。部署后每天入账的文档预计从 4 份回到几十份。部署时会一次性建索引，约 7 秒。 |
| **恢复 independence predicates 的脚本**（`922e817b`） | 谓词内容取自 `policy.DEFAULT_POLICY`。rehearse 结果：legacy、ws-7d 受影响的路由都是 0 条。 |

全量测试：见文末"测试记录"。

### 部署与部署后（两条命令）

```zsh
zsh ~/Projects/dalton-research-agent-os/docs/ops/deploy.sh
zsh ~/Projects/dalton-research-agent-os/docs/ops/run-batch-g-post-deploy.sh
```

第二条会依次执行：
1. 恢复 legacy、ws-7d 的 independence predicates，两边都会升一次 mission 版本。
2. 对齐 ws-7d 的模型路由，共 19 项选择。
3. 补上 ws-7d 的 xai 凭证槽位。这一步会自动停掉 ws-7d 的 3 个服务，补完后再拉起来。
4. 撤回 10 条重复的数字 claim。
5. 签入 FY−9M 第四季推导规则。legacy、ws-7d 各升一次 mission 版本；签入后，季度车道的下一个 tick 会自动推导并入账 7 家公司的 Q4。

每一步都会先 dry-run 核对，任何一步失败都会停下来。

### 待办（需要新规则或新设计，没放进本批）

- 三个 discovery 协调器仍然顺序执行、共用 20 秒预算。AlphaEngine 真正在采集的那几轮，会挤掉同一轮里的 SEC 和 web。建议改为每个协调器有自己的预算，或者轮转执行。

- AMZN 和 MSFT 的产品轴同时申报了两套粒度（加起来是合并数的 2 倍），走到 forecast 门禁时会像 GOOGL 一样被卡住。
- model_spec 的 structure repair 只看到第一个错误就付费修复；应该一次把错误收集全，确认都能修再付费。
- legacy 的 brain 和 verifier 两个类别在路由对齐里被跳过了，因为 legacy 自身在这两项上前后不一致，需要定一条统一的链。
- legacy v26 有 46 行 Guidepoint 记录找不到已完成的采集；其中 `already_in_authority` 状态的行永远不会被重新采集。
- 核验 v3/v4 的"双家族才拒绝"需要你在 cockpit 模型页，给 `claim_support_verifier` 和 `claim_support_backfill` 各加一个非 Gemini 家族的核验模型。

### 测试记录

在 main `b188ba30` 上跑全量测试：10677 个，全部通过（skipped 4）。

---

## 已部署

- 2026-09-28 10:50 UTC：批次 2026-09-27f，release `f9fe3f3e…`。第一次构建因 pypi 超时失败，已清理半成品后重建。controller 和 writer 首次 bootstrap 都返回 5，重试后成功。部署后：ws-7d 签入 policy-6，恢复 backfill（v3），撤回 6 条同义改写的重复 claim。

<details><summary>批次 2026-09-27f 说明</summary>

## 批次 2026-09-27f（main `3fb7dfe9` 及之后）

### 这批解决什么（批次 e 部署后验证中发现，外加"新建工作环境开箱即用"）

| 主题 | 效果 |
|---|---|
| **ws-7d 核验停摆**（`70ef64ee` `cd1cae38` `09242179`） | 抽取队列为空时照样启动 support-only 子进程，跑核验、recheck、回补复核和 P13i 复评。mission 升版时，待处理 review 迁到新版本（ws-7d 有 47 条、legacy 有 168 条卡在旧版本，部署后会自动迁过去）。recheck 对同义改写去重（阈值 0.85，数字和否定词必须完全一致）。核验升到 v3："由原文直接推出的结论"算作支持；拒绝需要第二个模型家族也同意（目前线上只有 Gemini 一个家族，暂时回退为单家族判定）。 |
| **dossier 与 SEC**（`7ad98f15` `58e6bea8` `9d7c882c` `16e67cb8`） | IBM 的"23 Jun 2026"这种日在前的日期能识别了；每家公司的 dossier 最多每 3 小时重写一次；SEC 传输超时不计入次数，并改为退避重试；SEC 的完整性检查改用 quick_check，每天缓存一次。 |
| **model_spec**（`b6f8728b`） | 同一份申报里精度不同的重复事实，四舍五入后一致就取精确值（IBM 的 EPS 因此能算出来）；"营业利润"缺口不再误诊；DXC 的 EPS 枚举错误变成可修复；旧修复免费重放；META 地域分部不再重复计入国家成员。预计新调用 3 次，约 $1–3。 |
| **新建工作环境开箱即用**（`010154ad` `b6bc016a` `224019b4` `e0339154` `e2ab0a9b` `cc85ea3e`） | 首个 mission 一次签入整套运行基线（auto-commit 全部规则 + research_plan_auto_start + 封闭格式的预算）。修复了只认 legacy 公司的硬编码（CIK 解析、cockpit 公司名、`source:sec-filings` → `source:sec-edgar`，后者曾让所有环境都显示"0 份可研究的 10-K"）。预算编辑不再误删 independence predicates。新增 parity 检查工具 `scripts/check_workspace_parity.py`，以及新 workspace 端到端 canary 测试：用正式流程新建 workspace，跑完整条链，再做一次升版，任何破坏新环境的改动都会让测试失败。 |

全量测试：见文末"测试记录"。

### 部署与部署后（按顺序，每行一条命令）

```zsh
zsh ~/Projects/dalton-research-agent-os/docs/ops/deploy.sh
zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-workspace-parity-owner-steps.sh
```

第二条会给 ws-7d 补签缺的两条 auto-commit 规则（policy-6），并前后各跑一次 parity 检查。它会先核对当前 release 是否已包含本批代码，没有部署就会直接停止。

### 可选

- **撤回 6 条同义改写的重复 claim**（legacy）：`zsh ~/Projects/dalton-research-agent-os/docs/ops/withdraw-recheck-near-duplicates-2026-09-27.sh` 先 dry-run，确认后在末尾加 `apply`。
- **让交叉复核生效**：在 cockpit 模型页，给 `claim_support_verifier` / `claim_support_backfill` 加一个非 Gemini 家族、能做核验的模型。
- **补齐运行项**：ws-7d 缺 verifier 路由和 xai 凭证槽位，ws-e399 缺凭证槽位。用 `align_model_routing.py --apply` 和 `repair_workspace_lane_parity.py --apply` 修复，需要重启对应服务，parity 工具会给出具体命令。

### 需要你决定

- **independence predicates**（要求 producer 与 verifier 的模型家族不同）：legacy policy-14 和 ws-7d policy-2 做预算编辑时被顺带删掉了（bug 已修）。**我建议恢复**。这是一条安全约束，而且它本来就存在，只是被 bug 误删。恢复需要各发一版策略，会触发一次 mission 升版（费用最多约 $3）。可以等下次有其他理由升版时一起做。
- **IBM 按百万元四舍五入导致无法精确勾稽**：要放宽，需要先在入库时保存 XBRL decimals，按申报精度设容差。属于设计改动，留待以后。
- **ACN 的 EPS 分子**：需要补一条附注证据（归母净利 + 可赎回少数股东权益）。

### 已知未修

- SEC 车道在 mission 升版后，用同一个 run_key 重放已提交的 run 时会抛 `ResearchPlanClosurePending`（正常运行不会触发）。
- `research_task` 的 ADHOC_PROBE_TEMPLATES 写死了 legacy 公司。目前三个环境都没有发布这个模板，将来如果发布，需要按 workspace 生成。

### 测试记录

在 main `3fb7dfe9` 上跑全量：10612 个，全部通过（skipped 4），其中包括新 workspace 的 canary。

</details>


- 2026-09-27 09:08 UTC：批次 2026-09-26e，release `1b5f6b04…`（源 `3acdb54f`）。部署前 ws-7d 签入 policy-5，归还 SEC 次数 47 次；部署后恢复回补。controller 首次 bootstrap 返回 5，重试后成功。

<details><summary>批次 2026-09-26e 说明</summary>

## 批次 2026-09-26e（main `8eca60eb` 及之后）

### 这批解决什么（批次 d 部署后验证中发现）

| 主题 | 效果 |
|---|---|
| **核验误判带日期、发言人的陈述**（`44ab2f3b` `2ae6f513` `f6fcb060` `437c380f`） | 核验 v2：引文扩到完整句子（原来是从 1200 字切片半句处截断），同时传入文档日期、发言人、发布方。按日期锚定的时间、元数据能证明的发言人、同义改写都不再算新增事实。今天 93 条 not_supported 人工核对后，明确误判 29 条，v2 下都会改判。部署后，被撤回的 claim 按 v2 自动复核，判为支持的自动恢复。重核队列不再卡在同样的 24 条上，同一段文字只提交一个 claim。 |
| **SEC 治理前置条件烧掉重试次数**（`6abb0f79`） | 排队前先检查治理前置条件，不满足就 hold；治理失败、mission 升版导致的拒绝、companyfacts 尚未收录都不再计入重试次数，其中数据源滞后改为退避重试（1 天起，最长 7 天）。 |
| **writer 处理 owner 请求超时**（`55595420`） | owner 请求排到排队中的自动化任务前面；超时从 10 秒改为 120 秒；超时后先确认请求是否已完成，再决定是否重试。 |
| **debate map、dossier 数字、IBM model_spec、行业图**（`b95b59c8` `d5bfb6ef` `3c91a672` `267028c5`） | duplicate 终态绑定 novelty 规则版本，MSFT 等会各重问一次；支持英文数字单词（five thousand ↔ 5000）；因已取消的拒绝规则而进入终态的，给一次恢复机会；行业图不再把仍有效的行业改挂当成已退役。 |

测试记录见文末。

### 部署命令

```zsh
zsh ~/Projects/dalton-research-agent-os/docs/ops/deploy.sh
```

（见下文 E0：部署命令已写成脚本，不必再手动粘贴长行。）

### 部署后执行

1. **E1 恢复晨报核验回补**（部署后立刻执行；回补会先按 v2 规则复核被撤回的 claim，判为支持的自动恢复）：
   `zsh ~/Projects/dalton-research-agent-os/docs/ops/pause-support-backfill.sh resume`
2. **E2（可选）立即人工恢复 4 条已确认误撤回的 claim**，不想等自动复核时用：
   `zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh reinstate`（dry-run），确认后在命令末尾加 `apply`。

### 与本批无关、现在就可以做

- ws-7d SEC 治理策略：`zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-sec-governance-owner-steps.sh`（签入 policy-5 并归还被烧掉的次数）。

### 已知未修

- CTSH 2026Q2：SEC companyfacts 已有两个月没收录这份 10-Q。部署后改为退避重试，不再计入次数；如果一直不收录，需要改为直接读 filing 自身的 XBRL。
- 新建 workspace 的首份策略只签入了 auto-commit，没有签 research_plan_auto_start，以后新建的 workspace 会遇到和 ws-7d 同样的问题。
- 纯超时导致的 cockpit 终态不会开恢复 epoch（有意采取的保守做法）。

### 测试记录

在 main `8eca60eb` 上跑全量测试：10510 个，全部通过（skipped 4）。

</details>


- 2026-09-26 12:12 UTC：批次 2026-09-25d，release `41fe150a…`（源 `505b1451`）。controller 首次 bootstrap 报 5，release_switch 自动重试后成功。SEC 次数归还：ws-7d 47 次、legacy 2 次。部署后晨报核验恢复，12:37 时 verdict 数为 62/59。

<details><summary>批次 2026-09-25d 说明</summary>

## 批次 2026-09-25d（main `49b2d5c8` 及之后）

### 这批解决什么（批次 c 部署后验证中发现）

| 主题 | 效果 |
|---|---|
| **debate map 撤不掉已退役引用**（`b52dedcb` `4424ba31`） | mission 升版后，新版本按内容判断是否发布，不再一律当作 rebind 判为重复。输出上限提到 8000。 |
| **网关输出超长，付费后被拒**（`abf3999f`） | CLI 网关的输出超限只记遥测，不再拒绝（钱已经花了）。输入上限和费用上限照常执行。 |
| **晨报核验被假扣费卡住**（`43b9e617`） | 修复前"没发出却按全额结算"的调用不再计入日上限。回放结果：两个环境当天用量都回到 0。 |
| **事件判断按文档合批**（`c3d31071`） | 同一文档的 claim 合成一次调用，其中任何一条仍可单独改变判断。14 天回放：调用次数减少 48%，约省 $11/天；3 条历史 THESIS_WEAKENED 仍完整送审。积压约 4 天清完（原来要 14 天）。 |
| **入账质量**（`ccecfb0e` `70a4633b` `092d41dc` `0ed8795f` `4fc5bb87`） | SEO 统计汇编页、时间上不可能的统计（例如"尚未结束的季度的报告"）、相对年份暂挂不入账；公开网页不再凭标题认定属于某家公司；拒绝谈论系统自身流程的"元结论"；新增人工撤回 CLI；行业规则 v2 规定 capex 须与行业主体词同时出现，自动撤回 6 条 v1 改挂。 |
| **hold 原因写错**（`44bbe865`） | 6a2bcd、a9e588b0 会被正确判为 unproved，918307dc 判为 contract，CLI 从而接受对应的入口。 |
| **EPAM dossier 永久卡住**（`4264d59e`） | 修复 repair identity 核对漏掉 parse_error 的问题。 |
| **dossier 数字引用误报**（`dc96d9c6`） | "15.8 (percent)"、日期里的数字不再误报；"约 188.6 亿"这类换算按仓库规定仍算编造，但 prompt 里补了中文示例。注意：prompt 变了，各 dossier unit 下次重写会各重新付费一次。 |
| **quality_scoring 车道一直返回 forbidden**（`2487d28d`） | 批次 b 新增了这个车道，但 core principal 的权限只在 bootstrap 时写入，release_switch 不会重写，所以从部署起每个 tick 都 forbidden。修复后，core principal 自动拥有所有已注册车道的权限（等同于 bootstrap 会授予的内容），其他 principal 仍严格按各自的权限列表执行。 |
| **EPAM forecast、IBM earnings preview**（`c62e6b06`） | 方向一致性规则原来默认"成本全部占收入比例"，D&A 按自身增速预测时利润率本来就会变，却被判为矛盾；现在先按报表结构判断这个前提是否成立。preview 的 prompt 写明引用最多 16 条，契约计数时对重复 ref 去重。两个被 hold 的车道各放行重跑一次。 |
| **晨报核验找不到路由，claim 全部转人工**（`a72ce0b3`） | 批次 c 给这两个用途加了核验合同，WorkOrder 因此要求 provider-controlled-verify 能力，但这两个用途仍挂在 cheap 档，而 cheap 链上没有任何模型具备该能力。结果是 09-26 00:00 起每次都报 no route，失败 3 次后候选就转为 held。现已改挂 verifier 档；路由不可用视为系统性失败，只推迟、不计入次数。**部署后自动重新核验已 held 的候选**（legacy 105 条、ws-7d 94 条），通过的直接提交原候选；backfill 误标的 160 条重新打开。 |
| **SEC 归还脚本、锁冲突后留下的预留**（`4f057db4` `25875321`） | 归还脚本改用 run.log 取失败原因，dry-run 只读；锁冲突时没写进去的结算会记下来，之后重放。 |

全量测试结果见文末"测试记录"。

### 部署命令

```zsh
cd ~/Projects/dalton-research-agent-os
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260925d.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260925d.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit $(git rev-parse HEAD) --apply
```

release_switch 只改可执行文件的路径，control 的 Standard 优先级会保留，不用重做 C1。

### 部署后执行（与部署命令在同一个终端里执行，需要用到 `$NEW`）

**D1 授权 3 条之前原因写错的 hold**（部署后等 10 分钟，让车道先按新代码重新分类）：

```zsh
PY=~/.dalton/runtime/releases/$NEW/venv/bin/python
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
for r in 6a2bcd446237e1bd9732690b4b542b3a a9e588b02de9c7a713167cdafec2d9f1; do
  $PY -m dalton_core.document_recovery_cli authorize-unproved --state-dir "$L" --admission-ref mission-document-research-admission:$r --max-cost-usd 1.0 --actor human:owner --apply
done
$PY -m dalton_core.document_recovery_cli authorize-paid --state-dir "$L" --admission-ref mission-document-research-admission:918307dc4626f6a8d549501eb7e193a9 --max-cost-usd 1.0 --actor human:owner --apply
```

**D2 撤回 52 条 SEO 统计页上的 claim**（ws-7d）。先 dry-run，把输出里的 `selection_sha256` 填进第二条命令：

```zsh
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
export DALTON_WORKSPACE_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json
$PY -m dalton_core.claim_admission_cli retire --state-dir "$W" --from-replay statistics_compilation --from-replay temporal_impossibility --reason "SEO 统计汇编页：数字无原始出处，含未到期报告期的编造统计"
# 把上面输出的 selection_sha256 填到 <SHA>：
$PY -m dalton_core.claim_admission_cli retire --state-dir "$W" --from-replay statistics_compilation --from-replay temporal_impossibility --reason "SEO 统计汇编页：数字无原始出处，含未到期报告期的编造统计" --expect-selection <SHA> --apply --actor human:owner
unset DALTON_WORKSPACE_MANIFEST
```

**D3 补结算今天锁冲突后留下的 6 笔预留**（约 $1.8，只影响账面）：

```zsh
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
cd ~/Projects/dalton-research-agent-os
.venv/bin/python scripts/settle_orphan_cockpit_reservations.py --budget-db "$S/thesis-impact-budget.sqlite" --scheduler-db "$S/scheduler.sqlite" --broker-journal ~/.openclaw/dalton-model-broker.sock.journal.json --all-cockpit --apply
```

### 与本批无关、现在就可以做

- **SEC 失败次数归还**（修复已随 efe93904 上线，只是次数额度在修复前就用光了）：命令见巡检消息，或 `scripts/void_sec_dispatch_attempts.py`（ws-7d 用 `--match "unknown issuer ticker"`；legacy 用 `--config "$HOME/Library/Application Support/Dalton/config/service.json" --match Lease --accession 0001058290-26-000031`，二者都加 `--apply`）。
- **批次 c 的 C2**：5 条 authorize-unproved（legacy b2f1a00d、ddf3a04b；ws-7d bec19d08、c737cc83、4225dbd9）。

### 已知未修

- 09-25 16:30 起 legacy 研究停摆的主因：晨报核验 $0.15/天的日上限被修复前的假扣费占满，文档抽取因此全部推迟。本批的 `43b9e617` 会修复；即使不部署，UTC 零点也会自愈。event_judgement 在 $50 池用完后停止属于按设计。

- 行业规则里 "pricing power" 的 "power" 被当成行业词（2bb6a2ec），留待后续。
- figures/metric 窗口身份绑定 mission 版本（ADR-0006），升版时会整份重读。

### 测试记录

在 main `49b2d5c8` 上跑全量：10439 个，全部通过（skipped 4）。此前 `03d8f03e` 上跑过 10411 个，1 个失败，是 `test_installer_startup_wait` 的计时断言，因机器负载超时；之前出现过同样情况，单独重跑 3 次都通过，与本批无关。其余全部通过（skipped 4）。

</details>


- 2026-09-25 14:36 UTC：批次 2026-09-25c，release `efe93904…`。C1 已完成：用户在 bootout 后立即 bootstrap 报错，由我补做了 bootstrap；三个 control 均为 Standard，cockpit overview 响应 0.02s。C2（authorize-unproved 5 条）待执行。

<details><summary>批次 2026-09-25c 说明</summary>

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
  # bootout 是异步的，立刻 bootstrap 会报 "5: Input/output error"
  sleep 2
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

</details>


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
