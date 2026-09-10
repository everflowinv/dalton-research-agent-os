# P12d：Deep Insight Gate 十二问草稿与人裁决 v1.0

日期：2026-09-09（v1.1：按 review 修完三个 blocker 与四项意见）
分支：`w2-deep-insight-gate`（基线 main `ebd2ea8`；已 `git merge main` 到 `eb8e5fb`）
蓝图：[能力差距分析与开发蓝图 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) §3 ③ / §5.2 P12d；
[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 1 节（版本化四条硬规则）、C3、D2
依赖：P12a 档案（`company_dossier`）、P12c DebateMap、P13-M2 预测行、P11c 估值快照、Q1 rubric/scorer、ADR-0006 / ADR-0008

---

## 0. 一句话

Playbook 从 Phase 9 起就对每家公司问了十二个问题，从来没有东西回答过它们（差距分析：「有合同无 lane」）。
这条线把回答变成一个**版本化的产出**（十二问逐条 answer + refs + 信心，允许 `unknown`），
把裁决变成一个**只有人能写的追加记录**（approve / return_for_more_work / reject，绑定草稿哈希），
并且通过仓库里已有的 mission stage ladder 让「通过」真正等于「这家公司可以进入下一阶段」。

自动化永远不通过这道闸。

## 1. 做了什么

### 1.1 新增模块（本 agent 独有）

| 文件 | 内容 |
| --- | --- |
| `src/dalton_core/deep_insight_gate.py` | 合同与 authority：十二问的记录形状、ADR-0008 版本链、人裁决表、门自己的 rubric、`output_rubric` 消费者、Q1 可打分投影 |
| `src/dalton_core/deep_insight_gate_schema.sql` | 两张表：`deep_insight_gate_versions`（append-only、三触发器、`body_hash` + `evidence_scope_hash`）、`deep_insight_gate_decisions`（append-only、每份草稿至多一条、CHECK 三词） |
| `src/dalton_core/deep_insight_gate_draft.py` | 材料装配（档案分节 / 争议 / 数字）、四组提示词、逐组严格解析、独立 verifier |
| `src/dalton_core/deep_insight_gate_cli.py` | 子进程：选公司 → 判空转 → 四次有界调用 → 第一问一致性 → 独立复核 → 结构检查 → 发布 → 生成阅读副本 |
| `src/dalton_core/deep_insight_gate_launcher.py` | `LaneChildLauncher` 子类，票据按（公司 × 证据签名）命名 |
| `src/dalton_core/mission_deep_insight_lane.py` | `LaneSpec(order=138)` + 协调器（签名去抖，待裁决即静默） |
| `tests/test_deep_insight_gate.py`（37 项） | authority、答案形状、裁决、结构标准 |
| `tests/test_deep_insight_gate_lane.py`（55 项） | 子进程、拒绝路径、沿用与降级、mission 版本滚动、崩溃重试、审批页、lane 注册、deliverable 迁移 |

### 1.2 共享文件的增量改动（只加，不改既有行为）

| 文件 | 改动 |
| --- | --- |
| `lane_registry.LANE_MODULES` | 一行：`dalton_core.mission_deep_insight_lane` |
| `mission_deliverable.py` / `_schema.sql` | 交付物类型加 `deep_insight_gate`；`_widen_kind_check` 的哨兵改为最新的那个词，老 Core 一次性重建 CHECK（照 P14a 的做法，含 `PRAGMA foreign_key_check`） |
| `writer_server.py` | 三个人类治理 op：`decide_deep_insight_gate`、`deep_insight_gate_draft`、`deep_insight_gate_submissions`；`OPERATION_FIELDS` 与 actor 绑定各一行 |
| `cockpit_plane.py` | 审批页列出未裁决的门（十二问与引用随条目一起给），`decide` 分支，registry lane 标签一行 |
| `cockpit_model.register_purpose("deep_insight_gate")` | 在 `deep_insight_gate_draft` 模块内登记（Wave 0 的接口，未改 `cockpit_model.py`） |
| `scripts/rehearse_deploy.py` | 一行 `MigrationSpec`，让新 schema 进部署预演（review B3） |
| `bootstrap.py` `SCHEMA_DATABASES` | 一行 `("deep_insight_gate_schema.sql", None)`，INT3 之后安装时就建表，而不是等某条 lane 第一次构造 authority |

没有碰：`coverage_mission`（含 schema）、`bounded_planner_driver`、`macos_launchagent`、`install.sh`、
`PROJECT_STATUS`、`tests/test_service`、`tests/test_lane_registry`、其他 agent 的模块。

## 2. 设计决定与理由

**十二问的文字不写在代码里。** `gate_questions(playbook)` 从 mission 绑定的 playbook 版本的
`stages[deep_insight_gate].exit_gate.questions` 读回，数量必须是 12，否则这条 lane 停下来
（换了方法论是人的决定，不该被悄悄吸收）。草稿绑定 `questions_hash`，记录校验时会核对
「十二条 answer 携带的问题」与它绑定的哈希一致——在一套方法论下回答的门，永远不会被当成另一套下的。

**一问要么有句子有引用，要么是 `unknown` 并说明什么证据能定它。** 中间态没有位置：
已答必须带 `high/medium/low` 信心，未答不许带信心，未答必须写 `missing` 与 `evidence_that_would_answer`。
薄档案上大部分问题就该是 unknown——十一个诚实的 unknown 加一个真回答，比十二段读起来完整、
指不回任何证据的散文更有价值，而且只有前者告诉 owner 下一步该买什么。

**第一问是核对的，不是相信的。** 档案已经在五类闭合词表里选过 `industry_classification`，
且经过独立复核。门的第一问答案与档案不一致时**整份拒绝**（`classification_conflict`），
而且在付 verifier 那一次调用之前就拒绝——不一致不是值得读的分歧，是两个模型在猜。

**门从档案读，不从 Ledger 读。** 材料来自 P12a 档案分节（分节的 sources 原样带过来，加上分节正文
本身作为一条可引用行）、P12c 的 live debates、报表行 / 预测格 / 估值指标。
如果门自己再去 Claim 层聚合一次，同一家公司在同一周会有两份互相打架的聚合。

**引用有六种，六道门。** `claim` / `figure` / `forecast_cell` / `valuation_metric` /
`dossier_section` / `debate`，发布前逐条回各自的 authority 解析，解析不了整份拒绝。

**四次调用而不是十二次。** 四个行业问读同一张表，一问一调用等于同一张表付四次钱。
分组：`industry`(q1–q4) / `company`(q5–q6) / `market`(q7–q8) / `thesis`(q9–q12)。
单次上限沿用 `company_model_cli` 的量级（`MAX_COST_USD = 0.60`），整轮含 verifier 上限
`MAX_RUN_COST_USD = 3.00`；付不起复核就什么都不发布，不发布未经复核的版本。

**独立 verifier（D2）复用同一条谓词。** 直接 import `company_dossier_draft.independence` /
`independence_precheck` / `router_family_resolver`——三份「不同 model family」的拷贝就是一条共享规则
不再是一条规则的方式。失败闭合：解析不出 family 就不算独立，而且在付第二次钱之前就判掉。

**发布 = 开检查点。** 没有第二套「提交」机制：一份已发布、`deep_insight_gate_decisions` 里没有对应行的草稿，
就是一个待裁决的检查点。cockpit 审批页、`deep_insight_gate_submissions` op、
`DeepInsightGateAuthority.undecided()` 读的是同一个 LEFT JOIN。

**裁决走已有的 ladder。** `decide_deep_insight_gate` 先调 `CoverageMission.record_stage`
（`entered` 若缺则补，再 `gate_passed` / `gate_failed`，两次都带以草稿 ref 为键的幂等键），
再写裁决记录并回填 `stage_record_ref`。顺序是故意的：中途崩溃会留下「过了闸但没有裁决记录」，
下一次调用自愈；反过来会留下「裁决了但公司没进下一阶段」，而且没有任何东西会发现。
`return_for_more_work` 完全不写 stage 记录——退回补充不是没过闸，是 owner 换了个更好的问题。

**退回之后只在有新证据时重出（ADR-0008）。** lane 的资格判定：过了初筛 + 有档案 +
没有待裁决的草稿 + 头部草稿的决定是 `return_for_more_work` + 证据指纹变了。
指纹 = （档案版本 ref、档案版本 hash、争议图版本 ref）的哈希，就是任务书说的
「按（公司，证据指纹）幂等」；指纹没变时在花任何一次模型调用之前就说 `nothing_new`。
approve / reject 之后本 lane 不再重出——重开一个已裁决的门是 `gate_reopen`，它自己是一个人类检查点（Wave 3）。

**门的 rubric 是冻结的，但没有登进 Q1 的 `RUBRICS`。** `research_quality_rubrics.rubric_hashes()`
被逐条钉住，且 `tests/test_research_quality_golden.py` 要求每个 rubric 配 5–10 条 golden case。
把它登进去而不带 golden set，会为了没有收益的事同时打破那个 pin 和 golden 形状检查。
所以 `DEEP_INSIGHT_GATE_RUBRIC` 定义在 `deep_insight_gate.py` 里、用 Q1 的 `Criterion` / `Rubric` /
`run_deterministic`、哈希由本线的测试钉住（`d78bb551…`），推广进 `RUBRICS` 是 Q 线带 golden set 的活。
硬闸只有两条：`numbers_without_refs`、`residual_citation_artefacts`。

**`unknown` 写进 gaps。** 结构检查的 `required_sections_present` 会把「正文空且无 gaps」判为空壳；
不做这一步，「说不知道而不是猜」就成了这套标准唯一惩罚的行为。

## 3. 验收

### 3.1 全量测试（原文）

```
Ran 4305 tests in 735.234s

OK (skipped=1)
```

（对齐当前 main `88a9325` 跑的。本线新增 92 项；另加 cockpit lane 标签一行、
`rehearse_deploy` 一行 `MigrationSpec`，使既有用例继续通过。）

本线两个文件单独跑：

```
Ran 37 tests in 0.263s

OK
```

```
Ran 55 tests in 8.171s

OK
```

### 3.2 蓝图 §5.2 的验收条款

> ACN 12 问草稿进入审批并被 owner 裁决一次

机制齐了、在测试里端到端走通了（`DecisionOperationTests` 用真的 `WriterServer` + 真的
`CoverageMission.record_stage` 走完 approve / return / reject 三条路），
**但在 live Core 上还跑不出来**：live 没有 P12a 档案、没有 P12c 争议图、没有 Claim 索引（见 §4）。
owner 的那一次裁决要等 Wave 2 部署之后，步骤写在 §5。

### 3.3 只读 smoke（/tmp 副本，无写入）

`cp /private/tmp/dalton-ro/core.sqlite /tmp/p12d-smoke.sqlite`，只读打开：

| 事实 | 值 |
| --- | --- |
| `company_dossier_versions` | **不在** live Core 上 |
| `debate_map_versions` | 不在 |
| `claim_index_entry_versions` | 不在 |
| `valuation_snapshot_versions` / `forecast_model_versions` | 不在 |
| ACN `initial_screen` `gate_passed` | 1 条 |
| ACN 的 Claim 版本 | 489 条（483 定性 / 6 定量） |

**十二问今天有多少材料：一问都没有。** 在这份副本上跑这条 lane 的结果是
`held: no_dossier_authority`——连草稿都不会起，一次模型调用都不花。
即使档案表存在但 ACN 没有档案，也只会是 `no_eligible_company` / `blocked[ACN] = no_dossier`。
档案一旦跑起来、某几问仍然没有材料时，它们会以 `unknown` 出现、`reason` 是
`source_unavailable`、`evidence_that_would_answer` 指向「先把这一问依赖的档案分节写出来」。
这不是本线的缺陷，是依赖链：门从档案读，档案从 Claim 索引读，两者都还没部署到 live。

ACN 的 489 条 Claim 里最密的几个 `metric_or_aspect`（说明档案跑起来之后哪几问会先有内容）：

| metric_or_aspect | 条数 | 大致落到哪一问 |
| --- | --- | --- |
| `quarterly_revenue_yoy_growth` | 6 | q2 / q10（需求与下一阶段盈利） |
| `new bookings` / `large client bookings` | 7 | q2 / q9（需求领先指标、证伪指标） |
| `competitive positioning` / `positioning and strengths` | 7 | q5（竞争优势） |
| `revenue guidance` / `revenue growth outlook` | 5 | q6（guidance 风格）/ q10 |
| `generative AI bookings/demand` | 4 | q4（行业长期趋势） |
| `operating margin` | 2 | q3（供给与成本） |

第七问（过去五到十年股价驱动）与第八问（共识与多空）在 live 上**结构性没有材料**：
没有价格序列、没有 consensus、没有 debate map。这与蓝图第 236 行的判断一致。

## 3.4 review 之后修了什么

| 编号 | 问题 | 修法 |
| --- | --- | --- |
| **B1** | 退回补充之后重出的那一版会**自己拒绝自己**——而且是在付完四次调用之后。`unresolved_refs` 把 `dossier_section` / `debate` / `valuation_metric` 只对着「本轮展示过的行」解析，而沿用的旧答案引用的是**它当时那一版**档案的分节，按定义不是本轮读的那一版。 | 每一种引用都回它自己的 **authority** 解析，不再对着「今天桌上有什么」。版本域从 ref 里拆出来（`dossier-section:<版本 id>:<分节>`，从右边切一刀，因为版本 id 自己带冒号），再查 `company_dossier_versions` / `debate_map_versions` / `valuation_snapshot_versions` 里那一版有没有这个成员。`forecast_cell` 查整条链而不只是链头：一格在被引用的当时是真的，模型后来出了新版并不能让旧引用变假。**沿用的答案解析不到时降级成 `unknown/source_unavailable`，永不整份拒绝**；只有**本轮新起草的**答案引用解析不到才整份拒绝。新增三个用例：一组被拒的重出、沿用答案的证据被退役后降级、新起草的引用被退役后整份拒绝。 |
| **B2** | `decide_deep_insight_gate` 在 mission 版本滚动之后会崩。阶段账本按版本记，live 的 mission 一直在滚；新版本只把 `entered` 带过来，不带 `gate_passed`，所以草稿绑定的那一版之外写不进去。 | **在 owner 点之前就判出来。** 新增 `deep_insight_gate.decidability(core, record)`（纯 SQL，cockpit 与 writer 共用一个谓词，两边不可能对「这个按钮开不开」有两种说法）：版本滚了 / 当前版本下初筛没过 / 已经过了闸 / 没绑 mission 版本，四个理由都有 owner 读得懂的中文。审批页与 `deep_insight_gate_submissions` 都带 `decidable` 与理由；不可裁决时**条目仍然在页面上**（它仍然是 owner 要处理的事），只是没有按钮、带一句 `note`——照 INT2/P14b 的 `_revision_checkpoints` 的做法。写入端 op 先问同一个谓词，不通过就抛可读的 `WriterServerError`。`return_for_more_work` 不受影响：它根本不写阶段记录。**耐久解法（阶段账本跨版本带 `gate_passed`）不在本线**，见 §5 集成项。 |
| **B3** | 新 schema 没有进部署预演。 | `scripts/rehearse_deploy.py` 加 `MigrationSpec("deep_insight_gate_schema.sql", "dalton_core.deep_insight_gate", "DeepInsightGateAuthority", "core")`。 |
| **4** | `QUIET_STATUSES` 是白名单，漏掉的拒绝状态（`classification_conflict`、`verification_failed`、`rubric_refused`…）会在签名不变时每 tick 重跑、重付四次调用。 | 反过来写：`RELAUNCH_STATUSES = {"submitted", "duplicate"}`，其余一律安静。长名单每加一条拒绝路径就要记得加一个词，忘记的那天就是这条 lane 把当天预算烧在循环里的那天。用例对四个状态逐个断言只启动一次。 |
| **5** | 审批页把十二个答案当**对象数组**塞进 `details`；页面对数组是 `v.join("、")`、对对象是 `JSON.stringify`，实际会渲染成十二个 `[object Object]` 连在一起。 | 每一问预先拼成**一行字符串**，并按 `q1`…`q12` 做成 details 的键，于是每一问是自己的一行。cockpit 测试改成**真的调 `approvals()`**（起一个 CockpitPlane 打真 Core），断言标题、按钮、每个 detail 值都是字符串、裁决后条目离场——不再 grep 源码。 |
| **6** | 换了 Playbook（十二问改了措辞）之后，链头看起来永远是最新的。 | `evidence_fingerprint` 加 `questions_hash`：问题是输入不是标签，换了问法链头就该被判为陈旧，要不要出新版本再交给 ADR-0008。 |
| **7** | 阶段记录写完、裁决记录还没写就崩，重试时阶段记录已在，`stage_record_ref` 会留成 `None`——自愈路径愈了个寂寞。 | 重试时把已存在的那一行找回来填进裁决记录。用例直接模拟「只做了前一半」再调 op。 |
| 小 | 回复里的 `unknown.reason` 一律写成 `no_material_shown`，即使材料明明给了。 | 词表改成 `material_insufficient`（看过了，定不了）与 `source_unavailable`（根本没有可看的），两条路把 owner 送到不同的地方。 |
| 小 | `argv_fragment` 不看 verifier 配置，缺它时 lane 会被打开、然后每 tick 报同一个 `no_verifier`。 | 三个文件齐了才吐参数。 |

## 4. 之前报告的 P12a bug：已在 main 上修好

v1.0 报告里记了一条：`company_dossier.validate_variant_view` 的 drafted 分支返回值漏了 `gaps`，
导致任何**起草过** variant view 的档案都发布不出去（`body_hash is not its body`）。
合并 main `88a9325` 时带进来了 `f76618d Merge w2-dossier-fix`，那一行已经补上。

本线相应地把测试 fixture 从「unavailable 的 variant view」改回**drafted**，并新增一个用例断言
variant view 真的到达了第八、九、十一问所在的两个组——那三问是关于市场而不是关于公司的，
计划第 1 节把 variant view 定为一等字段，正是为了它们。

## 5. 集成时要接的线 / owner 步骤

1. **授权**：live mission 的 `autonomy.may_write` 需要含 `deliverable`（现在只有 evidence / claim /
   forecast_line / model_run / research_question / observation / stage_record）。
   门用 `deliverable` 而不是新造第十三个词：它就是 Playbook 里某个阶段的出口文档。
   `human_checkpoints` 已经含 `deep_insight_gate`（Playbook 强制），不用动。
2. **部署文件**：state 目录需要 `initial-screen-model-config.json`、`p12a-dossier-policy-v1.json`、
   `dossier-verifier-model-config.json` 三个（与 P12a 完全同一组）。三个齐了 `argv_fragment` 才吐参数；
   缺 verifier 配置时子进程 `held: no_verifier`，一次模型调用都不花。
   `install.sh` 与 `macos_launchagent` 的接线由集成统一做（本线未碰）。
3. **前置依赖**：P12a 档案 lane 与 P12b Claim 索引必须先在 live 跑出东西，否则门永远 `no_eligible_company`。
4. **owner 的那一次裁决**：cockpit「待你裁决」页会出现一条
   「深度认知门十二问：是否让这家公司进入完整覆盖」，条目里直接带十二问的回答、信心、引用与未答项的下一步；
   三个按钮 通过 / 退回补充 / 否决，必须写一句理由。
   命令行等价物：
   ```
   dalton-governance --operation decide_deep_insight_gate \
     --param gate_version_ref=<版本 ref> --param gate_version_hash=<content_hash> \
     --param decision=approve --param reason='<一句理由>'
   ```
   通过会在同一次调用里补 `deep_insight_gate entered` 并写 `gate_passed`，都记在 owner 名下。
5. **B2 的耐久解法（另派）**：阶段账本按 mission 版本记，carry-forward 只把 `entered` 带到新版本。
   只要 live 的 mission 还在滚，任何跨版本的人类闸都会撞上这一条——不只是深度认知门。
   正确的修法是让 `gate_passed` 跟着 carry-forward 走（或者让 `record_stage` 按 `mission_ref` 而不是
   `mission_version_ref` 判前置阶段）。本线只做了「点之前就说清楚」，没有改 `coverage_mission`。
6. **`p12a-dossier-policy-v1.json` 被两条 lane 共用**：门读的是它的 `output_rubric_bindings`，
   因为那些 criterion 属于 Constitution 而不属于任何一份文档。若将来门要自己的绑定，
   加一个 `p12d-*` 文件并把 `--gate-policy` 指过去即可，代码不用改。

## 6. 没有做

- **没有 judge 层**。门只跑确定性检查与 `output_rubric`；模型打分（Q1 的 judge）留给 Q 线，
  连同把 `rubric:deep-insight-gate` 登进 `RUBRICS` + 5–10 条 golden case 一起做。
- **没有 `gate_reopen`**。approve / reject 之后本 lane 不再重出；重开是 Wave 3 P14d 的人类检查点。
- **没有改 `ARTEFACT_KINDS`**（`research_quality_score.py` 属于 Q 线）。
  本线自己拼 artefact 字典，`artefact_kind` 老实写 `deep_insight_gate`；
  所有 check 都不读这个字段，只有 `artefact()` 构造器校验它。
- **没有把 Q1 的 `claim_refs_resolve` 当硬闸**。门的引用有六种，只有一种是 Claim；
  一份全部引用档案分节与 debate 的草稿会被那条检查报 `no_citations`，那是它看不见的东西，不是缺陷。
  它进摘要，不进闸。
- **没有 `every_section_cites` 这条检查**。unknown 的一问本来就不引用任何东西。

## 7. 未决问题（留给 owner / 主 agent）

1. 四个问题分组的边界是本线定的（`QUESTION_SOURCE_MAP_REF = deep-insight-gate-source-map:p12d:v1`，
   带哈希写进每一版）。owner 若认为某一问该看别的分节，改这张表会让新版本的 `source_map_hash` 变化——
   这是有意的，旧版本仍能按当时的映射回放。
2. 一轮四次调用 × 0.60 美元上限 = 每家公司一版最多 3.00 美元（含复核）。
   `MAX_RUN_COST_USD` 是否要按 C2 的池子再收一道，等 owner 看到第一版真实开销之后再定。
3. 「退回补充」之后 owner 常常想指定「重答第七问」。今天没有这个入口（档案有 `--revise`，门没有）。
   加起来不难，但要先确认 owner 真的想要按问重答，而不是整份重出。
