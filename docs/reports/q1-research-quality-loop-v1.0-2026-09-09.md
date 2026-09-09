# Q 线：研究质量回路 v1.0

日期：2026-09-09
状态：分支 `wave1d-quality-loop` 已完成，待主 agent code review 与合并
基线：main `08c66d0`；[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 Wave 1「D 质量回路」；[能力差距分析与开发蓝图 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) 5.3 Q 线
数据：live Core 只读副本（`/private/tmp/dalton-ro/core.sqlite`，2026-09-09 12:23），复制到 `/tmp` 后只读打开。**没有写过任何 live 状态，没有部署，没有发过 mission 版本，没有对 live 预算发起过模型调用。**

---

## 0. 一句话

蓝图横切面那一条写的是「**没有研究质量评估**：1,995 项测试全是管道正确性；Initial Screen、Claim 相关性、问答、周报没有 rubric、golden set 或 PM 评分回路」。这一片做的是那句话的反面：三份冻结哈希的评分表、一个两层的打分器（确定性检查 + 一次有界模型判读）、两条 append-only authority（质量分与 analyst journal）、20 例 golden 集（其中 5 例是已发布且永远不能重写的 live 文档），外加 P10c 三处修复。

**最值得先看的一行数字**：确定性层跑过五份已发布的 Initial Screen，找出 **30 处引用剥离残迹**、**1 处同季三重并列引用**、**0 处无源数字**，并且五份里只有 DXC 一份是全干净的。四份 `gate_passed` 是终态，这些文档永远不会重写。

---

## 1. 三份评分表（含哈希）

`src/dalton_core/research_quality_rubrics.py`。每份是一个冻结记录：criteria + 0–4 分锚点 + 「打这个分需要指认什么证据」+ grading notes，`content_hash` 由 `store.content_hash` 对整个 body 计算。每条质量分记录都绑定这个哈希，所以改一个字就是改了标准，`tests/test_research_quality_rubrics.py` 把三个哈希钉死，改动必须是显式的。

| rubric_ref | 版本 | 适用对象 | 标准条数 | content_hash |
| --- | --- | --- | --- | --- |
| `rubric:initial-screen` | 1 | `mission_deliverable:initial_screen` | 9 | `334c9aee0ffebfc3816ee8f367d9304c225f1a05d29cfb3dbf405247260e2d64` |
| `rubric:ask-answer` | 1 | `cockpit:ask` | 7 | `7fe91c6057b145c7d7f23e5697a6e097c487a9905dfce0c5fa1ae4e6e9146e37` |
| `rubric:company-dossier` | 1 | `company_dossier_version`（Wave 2） | 7 | `0b1718d165bad133ce66fc5ca2cfb5ac3d5fb5d67dc61ff30d8bc54f8519ee51` |

**分档**：0 不可接受 / 1 严重不足 / 2 勉强合格 / 3 良好 / 4 优秀。锚点写在 0、2、4 三档，1 和 3 是它们之间——五档全写锚点会把评分表变成一篇散文，而三档加插值规则正是人类评分表的通行做法。2 是「满足了字面要求但没有做出判断」，这条线的位置是刻意的：它把「可以更好」和「不该被发布」分开。

**没有周报评分表**。owner 把周报投递搁置到最后（计划第 1 节），给一个没人在建的交付物写评分表，等于给没有东西打分。

### 1.1 `initial_screen`（9 条）

前四条由确定性检查支撑，后五条是判读。

| criterion | 问的是 | 确定性检查 |
| --- | --- | --- |
| `number_provenance` | 每个时效性数字是否由本节引用的定量 Claim 逐字承载 | `numbers_without_refs`、`claim_refs_resolve` |
| `citation_hygiene` | 引用脚手架剥离后正文是否还是完整句子 | `residual_citation_artefacts` |
| `citation_dedupe` | 同一个事实是否只被引用一次 | `duplicate_parallel_citations` |
| `required_sections` | 模板八节是否都在且都不是空壳 | `required_sections_present` |
| `key_driver_thesis` | 是否识别关键 driver、论证其足以驱动显著变化、指出 street 盲点 | — |
| `anti_thesis` | 是否是完整反向观点并逐条说明成立条件 | — |
| `falsifiers_and_tracking` | 跟踪指标是否真能验证或证伪上面的判断 | — |
| `gaps_honest` | 缺口是否诚实、正文是否止步于证据 | — |
| `source_discipline` | 是否符合 `evidence_discipline` 的来源层级、关键数字是否有两个独立来源 | — |

前四条直接来自 Playbook 的 `stages[initial_screen].exit_gate` 四问与 `pass_rule`；`key_driver_thesis` 与 `anti_thesis` 用的是 Playbook `required_outputs` 的原话（「论证 thesis 足以驱动业绩/股价显著变化；说明 street 为什么没有 price in」）；`source_discipline` 用的是 `evidence_discipline.source_hierarchy` 与 `minimum_independent_sources_for_key_numbers: 2`。

**Grading notes 里写死了三件不扣分的事**：估值一节按数字纪律留空是正确行为；「证据不足以支撑一个 thesis，直说不足在哪里」是满分行为；不要因为文档没回答你想问的问题而扣分。没有这三条，判官会因为文档遵守自己的合同而扣它的分。

### 1.2 `ask_answer`（7 条）

ADR-0006 那句话逐子句拆开：`cites_only_shown_claims`（只引这次展示的 Claim）、`no_invented_numbers`、`confidence_stated`、`admits_unknown`（证据不够时直说不知道）、`answers_the_question`、`gaps_named`、`stays_a_cockpit_artifact`（答案是驾驶舱产物，不得声称自己入了账本或改了 thesis）。

### 1.3 `company_dossier`（7 条，为 Wave 2 预写）

`every_section_cites`、`new_version_new_evidence`、`no_restatement_drift`、`section_coverage`、`numbers_traced`、`contradictions_surfaced`、`gaps_honest`。中间两条是蓝图第 6 节止损规则的直接编码（「dossier 版本必须绑定新证据才能发布；不满足就 duplicate」）与 Constitution `method.output_rubric` 最后一句（「好的研究产出缩小未决问题集合或锐化一个证伪条件，坏的复述已知事实」）。

**十节结构** `DOSSIER_SECTIONS` 是我按 owner 原文（计划第 1 节引的那段）归纳的：业务与收入构成 / 行业特性与周期位置 / 长期与短期驱动因素 / 竞争位置与壁垒 / 管理层与资本配置 / 财务画像与质量 / 历史股价与估值驱动 / 多空辩论焦点 / 叙事演绎与催化剂 / 跟踪指标与证伪条件。**Wave 1 Agent B 的 `aspect` 封闭词表要与 dossier 十节一致**（计划第 3 节原话），而 B 与我并行，两边没有对过。集成时必须把两份词表对齐，以哪一份为准由主 agent 定；改这里只是改一个常量加一次 rubric 版本升版。

---

## 2. 打分器设计

`src/dalton_core/research_quality_score.py`。

### 2.1 一个产物形状，三种产物

三种产物被归一到同一个 dict（`artefact()`）：`sections[{title, body, claim_refs, numbers[{text, claim_version_ref, period}], gaps}]`，加上 ask 专用的 `shown_claims` / `cited_tags` / `confidence` 与 dossier 专用的 `prior`。三个适配器：`artefact_from_deliverable`、`artefact_from_ask_answer`、`artefact_from_dossier`。

问答被表示成一节文档，它的 `numbers` 就是它引用的那些 Claim。这不是偷懒：这样一来「答案编了个数字」和「screen 编了个数字」是同一个问题，同一个检查回答。

### 2.2 第一层：不需要模型的检查

| check | 判定 |
| --- | --- |
| `numbers_without_refs` | 用 `mission_deliverable.unsourced_numbers` 逐节比对：正文中的数字 token 必须在本节 `numbers` 的某条来源文本里逐字出现 |
| `residual_citation_artefacts` | 对已知标记语法的正则：残留 C/N 标记、连续分隔符、句末后紧跟分隔符、括号边上的分隔符、空引用括号、行首分隔符，以及丢了主语的「和指出 / ：显示 / ；（重复）显示」 |
| `duplicate_parallel_citations` | 一节的 `numbers` 按（period × 数字 token 集合）分组，一组有多于一条 claim ref 即命中；另加「同一数字在一句话里出现两次」 |
| `required_sections_present` | 章节标题与模板逐一比对；有正文也没有 gap 的章节记为空壳 |
| `claim_refs_resolve` | 引用在 `claim_versions` 中存在且不在 `claim_retirement_decisions` 里；没有 Core 连接时明确报 `skipped` 而不是 `pass` |
| `cites_only_shown_claims` | 引用标签 ⊆ 这次展示的标签 |
| `confidence_stated` | confidence ∈ {high, medium, low} |
| `every_section_cites` | 写了正文的章节必须有引用或数字 |
| `new_version_cites_new_refs` | 本版引用集合减上一版非空；第一版 `skipped` |
| `restatement_drift` | 逐节 `difflib` 相似度 ≥ 0.85 记为复述；**只有当没有任何一节有实质改动时才判 fail** |

`restatement_drift` 的这条规则是写第三版 golden 案例时改的：逐节判 fail 会让「只改一节但改得对」的诚实修订成为被罚最重的一类。漂移是「一版里什么都没动」，不是「一版里有九节没动」。

每个检查返回 `{check, status: pass|fail|skipped, count, findings[], detail}`，findings 带上出错的章节与原文摘录——「12 处残迹」对修它的人没有用，`「718144000，同比增5.59%；、、（同一季度数据重复）显示2025-0」` 有用。

**为什么正则可以承担这件事**：`residual_citation_artefacts` 的每一个模式都是从已发布文档里读出来的，不是想出来的。「，表示」这种普通中文被刻意排除在外（`tests/test_research_quality_score.py::test_ordinary_chinese_prose_is_not_flagged` 钉住了这一点），只有「和指出」「；显示」这类连词或句末后直接接报道动词的形状才算残句——它们在中文里本身就不成句。

### 2.3 第二层：一次有界模型判读

一次 `CockpitModel` 调用，purpose `quality`（`cockpit_model.PURPOSES` 加了这一个词，Wave 0 把 PURPOSES 变成注册表后改成一行登记）。

- prompt = 评分表（含分档、锚点、grading notes）+ 已经跑完的确定性检查结果（作为事实给它，不让它重新推）+ 产物正文 + 被引 Claim 的表格（`| ref | period | statement |`）。
- 界限沿用 `company_model_cli` 的量级：`MAX_INPUT_TOKENS=120_000`、`MAX_OUTPUT_TOKENS=2_000`、`MAX_COST_USD=0.60`、`TIMEOUT_SECONDS=180`、prompt 里产物正文上限 40,000 字符、Claim 表 80 行。live 最长的一份 screen 建出的 prompt 是 ~28 KB，远在界限内（golden 测试逐例断言这一点）。
- 输出 = `{"scores": [{"criterion_id", "score", "evidence"}]}`，验证：criterion id 恰好是这份评分表的全集（不多不少不重）、score 是 0–4 的整数（`3.5`、`"3"`、`True` 都拒）、evidence 是一句话（≤300 字符、无换行、中间无句号）、顶层与每行都不许有多余的键。任何一项不满足 → `refused`，**不修补**。

不修补的理由和 `thesis_impact` 的 verifier 只返回 verdict 是同一个：需要先被整理才能读的判断不是判断。

### 2.4 独立复核（stretch goal，**做了**）

`build_verifier_prompt` / `validate_verifier_output` / `verify()`：第二次独立调用，看到产物与第一次的分数，只回答一个问题——每个分数是否被产物和它给出的证据句支撑。verdict ∈ {pass, reject}，finding code 是四个封闭值，pass 必须无 finding、reject 必须有 finding（`thesis_impact` 的同一条规则）。verdict 绑定 `judged_scores_hash`，不指名它读了哪些分数的 verdict 是关于虚无的 verdict。复核是可选的：`score_artefact(..., verifier_model=...)` 才跑，成本翻倍。

**没有做的**：像 thesis-impact 那样的 30 例校准集与 provider-output JSON Schema。复核目前只有形状约束，没有「复核本身准不准」的证据。

### 2.5 `QualityScoreVersion` authority

`research_quality_schema.sql` + `QualityScoreAuthority`。两张表（版本链 + pointer）、六个触发器（写入需 `dalton_research_quality_authorized()`、update/delete 一律 ABORT，pointer 同）、`content_hash`、写完读回校验，照 `statement_snapshot.py` 与 `mission_deliverable.py` 的样子。

一条记录绑定：产物 ref **与 content_hash**、rubric ref 与 hash、`scorer_version`、`model_config_fingerprint`。两层分开存（`deterministic` / `judge` / `verifier` 三个字段），因为一层是关于文档的事实、一层是模型对它的阅读，读者要能在不信任第二层的情况下信任第一层。

**duplicate 规则**：`scoring_identity_hash = H(target_ref, target_hash, rubric_ref, rubric_hash, scorer_version, model_config_fingerprint)`，同一身份第二次打分返回 `duplicate` 且不写新版本（DB 上还有 `UNIQUE(score_ref, scoring_identity_hash)` 兜底）。**分数商店（score shopping）是所有「按需打分」质量闸的失败模式**：分数不好就再问一次。`scorer_version` 进身份，是为了让修好的检查还能重新打一份被坏检查打过的文档。

`model_config_fingerprint` 只取 routing policy / budget policy / credential slots / agent id / client id 加上这次的界限，**不含机器本地路径**：同一条路由策略在两台机器上是同一个配置；而输出预算翻倍的判官是另一个判官。

---

## 3. `AnalystJournalEntry`

`analyst_journal_schema.sql` + `analyst_journal.py`。Playbook 对 Basic Level 1 分析师的要求最后一句是「建立 analyst journal（内化反馈、避免重复错误）」——Dalton 从 Phase 9 起就在这一级，而反馈一直没有地方放。

- 封闭词表 `read` / `useful` / `needs_more_evidence` / `disagree` / `revise`，可选自由文本（≤4000 字符），可选 score override（必须指名 rubric_ref，分数 0–4）。
- 绑定 target ref **与 content_hash**：对一份此后被重写的文档说过的「好」，是关于旧文档的话。
- append-only、三触发器、`content_hash`、写回校验。**没有版本链，这是刻意的**：一条 entry 是一个事件，改主意就再写一条，`entry_number`（按 target 单调）就是链。
- 只接受 `human:` 主体。自动化给自己打分是质量分，它有自己的记录；让它写进这里，这里就不再是唯一承载人类判断的地方。
- 读法三个：`for_target`、`for_company`、`outstanding`（只要 needs_more_evidence / disagree / revise 三种，即「上一次交的不够」的那些）。`journal_context()` / `render_journal_for_prompt()` 把它渲染成起草 prompt 能直接贴进去的一段，最新在最后，上限 20 条。

**今天没有任何起草器读它**——写入口（writer op）与驾驶舱按钮是集成工作，见第 6 节。

---

## 4. Golden 集与结果

`tests/golden/<rubric>/*.json`，20 例。每例带 `source`（从哪来）、`note`（为什么在这里）、完整 artefact、`expected.deterministic`（逐检查的 status 与 count，测试里逐例断言）、`expected.score_ranges`（每条标准一个区间）与 `expected.rationale`（为什么是这个区间）。

### 4.1 `initial_screen`（8 例：5 份 live + 3 份人为破坏）

五份 live screen 是从 `/tmp` 上的只读副本里逐字复制的，带它们真实的 `mission-deliverable-version:` 与 `claim-version:` ref。

| 案例 | 无源数字 | 引用残迹 | 重复并列引用 | 章节 |
| --- | --- | --- | --- | --- |
| `live-acn-v2`（ACN v2，已过闸） | 0 | **12** | **1**（2025-09-01..2025-11-30，三条 ref 一个数字） | pass |
| `live-ctsh-v1`（未过闸，缺电话会） | 0 | **1** | 0 | pass |
| `live-epam-v1`（已过闸） | 0 | **11** | 0 | pass |
| `live-ibm-v1`（已过闸） | 0 | **6** | 0 | pass |
| `live-dxc-v1`（已过闸） | 0 | 0 | 0 | pass |
| `broken-numbers-without-refs` | **2** | 12 | 1 | pass |
| `broken-residual-markers` | 0 | **19** | 1 | pass |
| `broken-missing-anti-thesis` | 0 | 12 | 1 | **fail**（缺 S4） |

30 处 live 残迹的分布：`orphan_sentence_start_verb` 12、`separator_before_closing_bracket` 7、`orphan_conjunction_verb` 5、`separator_run` 3、`separator_after_sentence_end` 2、`empty_parenthetical` 1。

**读法**：数字纪律是硬闸（发布路径会拒），所以五份 live 全是 0——那一层是有效的。引用卫生没有任何闸，所以五份里四份带伤，而且四份 `gate_passed` 是终态，这些伤永远在。DXC 那一份说明同一条流水线在同一天可以写出干净文档，问题不是「模型不行」而是「没有人检查」。

### 4.2 `ask_answer`（7 例，由真实 ACN / EPAM Claim 构造）

| 案例 | 只引展示过的 | 无编造数字 | 有信心 |
| --- | --- | --- | --- |
| `good-revenue-trend` | pass | pass | pass |
| `honest-unknown`（「我不知道，缺 bookings」） | pass | pass | pass |
| `hallucinated-number`（真收入夹着编的利润率） | pass | **fail (2)** | pass |
| `unit-rewrite`（USD 1353443000 → 13.53 亿） | pass | **fail (1)** | pass |
| `cites-unshown-claim`（C17 / C23 没展示过） | **fail (2)** | pass | pass |
| `claims-recorded-overreach`（「我已记入账本并更新 thesis」） | pass | pass | pass |
| `no-confidence-stated` | pass | pass | **fail (1)** |

后两例是确定性层抓不到的两类：越权表述和「事实全对但纪律错了」，它们靠判读层。`unit-rewrite` 是最容易被放过的一个——换算是对的，但读者拿着 13.53 亿去核对 filing 会核不上。

### 4.3 `company_dossier`（5 例，手写，论断全部指向真实 ACN Claim）

| 案例 | 每节有引用 | 新版本引新证据 | 复述漂移 | 章节齐备 |
| --- | --- | --- | --- | --- |
| `good-first-version`（十节） | pass | skipped（第一版） | skipped | pass |
| `advanced-with-new-evidence`（只改一节但引了新证据） | pass | pass | pass（8 节未动，1 节推进） | pass |
| `restated-without-new-refs` | pass | **fail** | **fail (9)** | pass |
| `uncited-moat-section`（最像研究的那一节零引用） | **fail (1)** | skipped | skipped | pass |
| `missing-half-the-structure`（只有前四节） | pass | skipped | skipped | **fail (6)** |

`golden run` 的输出（CLI 直接跑，20 例全部与 golden 一致，退出码 0）见 `research_quality_cli.py golden run`；`tests/test_research_quality_golden.py` 逐例断言同样的结果，并用假模型跑判读层（断言 prompt 能装下、回复能验证、拿另一份评分表的回复会被拒）。

### 4.4 分数区间是校准，不是测量

**没有跑过任何一次真实判读调用**。这个 worktree 上没有可达的 broker 配置，而且指令是不要动 live 预算。所以 `expected.score_ranges` 是我作为 golden 集作者写下的校准判断，每例附了理由；它们目前只在假模型下被断言「结构上能对上」，**没有任何证据说明真实模型会落在区间内**。第一次真实判读跑完之后，应该做的是把落在区间外的每一条拿出来，判断是模型错了还是区间错了——这是第 8 节的第一个开放问题。

---

## 5. P10c 三处修复

### 5.1 引用标记残句：修的不是剥离器，是缺一个回路

**现状先说清楚**：P13ap（`490e63f`，main 上我这条分支的父提交）已经修了剥离器本身——标记连成一串一起删、括号边的分隔符跟着走、被清空的引用括号整条删掉——并且明确写了它不打算把句子的主语补回来。这是对的。

我做的是另一半：**剥离器修不了的那部分，此前没有任何东西看着它**。发布路径对无源数字有一个纠正回路（一次带具体数字的重试，仍不合格就把这一节降级成 gap），对引用残句什么都没有，所以残句进了已发布文档。现在是同一个回路，一次调用同时告诉它两类缺陷：

**Before**（live ACN v2，S7 正文，已发布、已过闸、永不重写）：

```
首先，收入增长轨迹是最直接的验证点：显示2026-03-01..2026-05-31季度收入为USD
18718144000，同比增5.59%；、、（同一季度数据重复）显示2025-09-01..2025-11-30
季度收入为USD 18742125000，同比增5.95%；……
第五，联邦业务风险是特定尾部变量：和指出美国联邦支出削减导致合同终止、价格和范围缩减……
```

**After**（同样的模型输出走今天的路径）：剥离器把 `、、` 连同标记一起去掉；`residual_citation_artefacts` 仍然报出 `orphan_sentence_start_verb`（`：显示2026-…`）与 `orphan_conjunction_verb`（`和指出`）；这一节被送去一次纠正重试，prompt 里带上原文摘录与「no C or N tag anywhere in the body … name the source in words（管理层、该季报、卖方研报）」；重试仍不合格则整节变成：

```
"body": "",
"gaps": ["这一节已丢弃：引用标记剥离后留下了残句（orphan_sentence_start_verb）：：显示2026-03-01.."]
```

回归测试用的是**逐字的 live 片段**（`tests/test_initial_screen_citations.py::LiveResidualFragmentTests`，常量 `LIVE_ACN_S7_FRAGMENT`）：断言 `、、` 与 `；、` 被去掉、两个数字与 `同比增5.95%` 原样保留、剥离后剩下的恰好是 `orphan_sentence_start_verb`，以及一句干净改写不会被误报。

侦测器只有一份定义：`research_quality_score.residual_citation_artefacts`，起草路径与打分器共用。同一个缺陷的定义，既用来防它，也用来给它打分。

### 5.2 同季重复 Claim 并列引用

**Before**（live ACN v2 的 S7 `numbers` 列表，三条 ref 一个数字）：

```
claim-version:ba477771…  2025-09-01..2025-11-30  Revenues of USD 18742125000 … up 5.95%
claim-version:5848e037…  2025-09-01..2025-11-30  Revenues of USD 18742125000 … up 5.95%
claim-version:ef266e8d…  2025-09-01..2025-11-30  Revenues of USD 18742125000 … up 5.95%
```

三条都是 `official-filing-xbrl`、同一 subject × metric × period × unit × value，由三次不同的 research-plan 执行从同一批 XBRL 事实写出来。

P13ap 已经按「逐字相同的 statement」把它们收成一条了。我改了两件事：

1. **去重键从「措辞」变成「断言」**。一条 Claim 与另一条是同一断言，当它们（a）对同一 period 说了同样的话，**或**（b）测量了同一 subject × metric_or_aspect × period × unit 且值相同。P13ap 只有 (a)，所以 filing 的数字与卖方研报对同一个数字的转述（不同措辞）仍会被并列offer 出去。新增的 `test_a_paraphrase_of_the_same_measurement_is_also_one_citation` 钉住这一条。
   顺带：statement 键现在按 period 限定。同样的一句话说的是两个不同季度时，它们是两条不同的断言（P13ap 的 `test_the_same_figure_in_two_periods_is_two_figures` 本来就要求这一点）。live 影响：定性 Claim 的去重数从 P13ap 报的 3 条降到 1 条（CTSH 1 条），因为另外两条是「同样的话、不同期间」。
2. **保留哪一份：filing 级优先，其次最近记录，最后按 ref 定序**。P13ap 保留最早的一份，理由是重画时引用的 ref 稳定。那个理由是真的，但它输给这一条：一份 screen 引用卖方对 filing 已经报出的数字的转述，就是在两个来源里引了弱的那个，而 Playbook 的来源层级不是 tie-break。代价（新的更好来源到达时重画会换 ref）是暂时的——Agent B 的 claim 索引会给每个去重组指定一个 canonical 成员，这条偏好届时退休。
   **这改了 P13ap 的一个刻意决定**，我把原因写进了那条测试的注释里，指回本报告。

live 效果（新逻辑跑过只读副本的全部 2,098 条 live Claim）：

| | live Claim | C 标签 | N 标签 | 去重掉的语句 | 去重掉的数字 | 数字系列 |
| --- | --- | --- | --- | --- | --- | --- |
| ACN | 482 | 120 | 4 | 0 | **2** | `quarterly_revenue_yoy_growth` |
| CTSH | 453 | 120 | 4 | 1 | 0 | 同上 |
| DXC | 267 | 120 | 4 | 0 | 0 | 同上 |
| EPAM | 713 | 120 | 4 | 0 | 0 | 同上 |
| IBM | 183 | 120 | 4 | 0 | 0 | 同上 |

ACN 的 N2 现在是 `…249b41c5e9`（三条里最近记录的那条），一个数字一条 ref。

**顺手修掉的一个会炸的东西**：live 有一条 Accenture 新签订单 Claim 的 `period` 是结构化对象而不是字符串（`{"kind": "fiscal_quarter", "label": "FY2026Q3", …}`）。2,098 条里的 1 条，而它会让去重键进不了 set，用 `TypeError` 把这家公司的起草 lane 整条打下来。所有键分量现在先规范化成可哈希形式（提交 `d587c11`）。

### 5.3 只有收入一个数字系列

**结论先行：数字上下文构建器本身不限于收入，账本才是。** 全部 2,098 条 live Claim 里只有 22 条定量 Claim，五家公司各 4–6 条，**全部是 `quarterly_revenue_yoy_growth`**；带 `value=None` 但句子里含数字的定性 Claim 是 **0 条**。所以五份已发布 screen 只有一条数字系列，是账本的事实，不是构建器的取舍。

其余的核验过数字确实在库里（`coverage_mission_document_figures`、`coverage_mission_statement_lines` 的 10,023 行报表行），但 `mission_deliverable.validate_section` 要求每个数字绑定一条 live `claim_version_ref`，那两张表里的行不是 Claim。**放开这一条是 ADR 级的改动**（PROJECT_STATUS 待办第 6 条「把已核验的数字接进 Ledger」，明写要走 ADR + 重签策略），我没有碰它。

我在不动那条纪律的前提下做了两件事，两件都是「等第二条系列出现时它才真的起作用」：

1. **预算按系列轮转分配**。旧代码取「最近记录的 40 条」。live 只有一条系列时这没有区别；一旦有第二条系列而它写得比第一条稀疏（这正是一条 lane 的行为），最近的 40 条就全是第一条系列的。现在按 `metric_or_aspect` 分组、组内按 period 倒序、跨组轮转到填满 40 条，**单条系列仍然能吃满整个预算**。
2. **句子里带数字的 Claim 现在按数字（N 标签）offer，而不是按语句（C 标签）**。起草合同只允许数字经由 N 标签进入正文，所以一条「管理层称本季利用率为 91.2%」的定性 Claim，它的数字此前根本无法被写下来。今天 live 有 0 条这样的 Claim，但抽取侧一旦开始写这类 Claim（Agent B / C 都会），这就是第二条系列进入 screen 的最短路径。

另外 `build_claim_context` 现在返回 `series`（这次能看见几条系列），起草 summary 也带上它与 `duplicates_dropped`：**一条系列意味着这份 screen 无论写得多好都只有一条数字系列，这是账本的事实，应该在 lane 的 ticket 上看得见。**

**与 Agent B 的关系**：B 在建带 aspect / importance / 去重组的 claim 索引投影。我这份去重是**起草组装的局部行为**，不改 `claim_versions`、不新建 authority，集成时应当被 B 的索引取代（用它的 canonical 成员替掉我的 `_by_preference`）。我没有依赖 B 的任何东西。

---

## 6. 集成待办（都不在这条分支上）

1. **writer op：`record_analyst_journal_entry`**。驾驶舱进程没有 Core 写句柄（ADR-0006），所以 PM 反馈必须走 writer。参数 `{target_ref, target_hash, target_kind, verdict, company_ref?, note?, score_override?, idempotency_key?}`，`actor_ref` 由 writer 从 Tailscale 主体推导并强制 `human:` 前缀（authority 本身也拒非 `human:`）。`writer_server.py` 是我的禁改文件，所以这里只写规格。
2. **驾驶舱按钮**。每份交付物与每条问答答案下面五个按钮（读过 / 有用 / 证据不够 / 不同意 / 要重写）加一个可选备注框；`idempotency_key = f"cockpit:{login}:{target_ref}:{verdict}"`，重复点击返回 duplicate。答案的 target_ref 用 `artefact_from_ask_answer` 算出的 `cockpit-ask:<job_id>`，target_hash 用它算出的内容哈希——答案没有 Core 记录，这个哈希就是它的身份。
3. **`quality` 模型用途与配置名**。`cockpit_model.PURPOSES` 我加了一个词（一行，与 Wave 0 的注册表改造会有一行冲突，按计划这是允许的）。还需要：`scripts/raise_day_budget_cap.py` 的 `MODEL_CONFIG_NAMES` 加一项（如果质量判读要独立的模型配置），以及 `deploy/macos/install.sh` 的模型配置块。**建议判读复用 extraction 配置**（和 ask / draft 一样），先不建独立配置：判读一次 ~$0.05–0.60，独立配置只在需要单独限流时才值得。
4. **打包**。`research_quality_schema.sql` 与 `analyst_journal_schema.sql` 需要进 `pyproject.toml` 的 package-data；Wave 0 改成 `*_schema.sql` 通配之后自动覆盖，在那之前 `PYTHONPATH=src` 能找到。`tests/test_packaging.py` 只断言一份固定清单，所以不会因为新增文件而失败。
5. **console script**（我不能改 `pyproject.toml`）：`dalton-research-quality = "dalton_core.research_quality_cli:main"`。
6. **要不要建一条 tick lane 重新给每份新交付物打分**。我的建议是**分两步**：
   - **第一步（建议做，成本为零）**：把确定性层直接接进 `initial_screen_cli` 的发布前检查——它已经在那里跑残句检测了，把 `duplicate_parallel_citations` 与 `required_sections_present` 一并跑一遍写进 summary 即可。不需要 lane、不需要模型、不花钱。
   - **第二步（建议等第一次真实校准之后再定）**：判读层的 lane。如果建，coordinator + launcher + CLI 三件我已经有了 CLI，另两件照 `initial_screen_launcher` 抄；registry 那一行大致是：

     ```python
     LaneSpec(
         operation="score_deliverable_quality",
         core_only=False,
         param_fields=frozenset({"target_ref", "rubric_ref"}),
         build_coordinator=build_quality_coordinator,
         argv_fragment=("--quality-summary-dir", "quality-scores"),
         launcher_factory=QualityScoreLauncher,
         driver_key="quality_score",
     )
     ```

     选择规则应当是「有新发布的交付物，且它的 (ref, hash, rubric_hash, config fingerprint) 还没有质量分」——duplicate 规则天然让它幂等。每份交付物一次判读调用，按 live 现在的节奏（5 份 screen / 两天）是可以忽略的开销。
7. **`build_claim_context` 的 `series` 与 `duplicates_dropped` 已经进了 lane summary**，驾驶舱如果要显示「这家公司只有一条数字系列」，读的是这两个字段。
8. **dossier 十节与 Agent B 的 aspect 词表对齐**（见 1.3）。

---

## 7. 验收结果（原文）

全量测试，`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`：

```
Ran 2176 tests in 248.006s

OK (skipped=1)
```

基线是 2,034 通过 / 1 跳过，本片新增 **142 项**，没有失败、没有静默跳过。

`golden run` 20 例全部与 golden 一致（退出码 0）。

各模块分项：`test_research_quality_rubrics` 15、`test_research_quality_score` 65、`test_research_quality_golden` 13、`test_analyst_journal` 21、`test_research_quality_cli` 13、`test_initial_screen_citations` 32（P13ap 的 17 项加我新增的 15 项）。

**没做的事**，如实列出：

- 没有跑过任何一次真实模型判读，所以分数区间没有被验证过（第 4.4 节）。
- 独立复核只有形状约束，没有校准集。
- 没有把确定性层接进发布前检查（那会碰 `initial_screen_cli` 的发布路径，属于集成决策，见第 6 节第 6 条第一步）。
- 没有周报评分表（owner 已搁置）。
- 没有 lane、没有 writer op、没有驾驶舱接线。

---

## 8. 开放问题

1. **分数区间没被验证过。** 第一次真实判读跑完之后，逐条看落在区间外的标准，判断是模型错了还是我写的区间错了。在那之前，`expected.score_ranges` 只是文档，不是断言（测试里只用它的下界喂假模型）。
2. **「阈值数字」与「测量数字」在数字纪律下无法区分。** 写 dossier golden 时踩到：证伪条件里的「收入增速连续两个季度低于 5%」会被 `numbers_without_refs` 判为无源数字。`value_tokens` 已经排除了不带单位的小整数（「book-to-bill 跌破 1」），但带百分号的阈值排不掉。我绕开了（把 golden 文本改成不带数字的表述），没有改规则——改它要动 `mission_deliverable.value_tokens`，那会影响发布闸。**这是一个真实的表达力损失：分析师写证伪条件时本来就要写阈值。**
3. **P13ap 的「保留最早一份」被我改成了「filing 级优先、其次最近」。** 理由见 5.2，但代价是重画可能换 ref、从而产生一个内容相同但 body_hash 不同的新版本。如果主 agent 认为 ref 稳定性更重要，改回去是一行；我认为在 Agent B 的索引落地之前，来源层级应该赢。
4. **同一 agent 写代码、写测试、写报告**，仓库的长期风险项在这一片同样成立——而且更尖锐：**这一片的产出就是「评价质量的标准」，而它的质量没有被第二个人评价过。** 蓝图第 6 节建议每个 Phase 结束由 PM 或第二位分析师做一次盲评；这一片是最该做的那一个。具体建议：owner 拿 `live-acn-v2` 与 `live-dxc-v1` 两份 golden 案例按 `initial_screen` 评分表各打一次分，与我写的区间比对。
5. **`gate_passed` 是终态，所以四份带伤的文档永远不会被重写。** owner 已裁决「证据变厚可重出 Initial Screen，但必须版本化」（计划第 1 节第二批裁决），实现这条之后，**质量分应当是重出的触发条件之一**：一份 `citation_hygiene` 为 0 的已过闸文档，比一份证据变厚的文档更该被重画。这条我没有实现，因为 gate 重开是 Wave 3 的事。
6. **评分表升版之后旧分数怎么读。** 现在的答案是「照旧」：一条分数绑定的是它当时那份评分表的哈希，它对那个标准说的话仍然为真。但驾驶舱要展示「这家公司的质量趋势」时，跨 rubric 版本的分数不能直接连成一条线。需要一个显式的规则，我建议是「趋势线按 rubric 版本分段，换版本时画一条竖线」。
