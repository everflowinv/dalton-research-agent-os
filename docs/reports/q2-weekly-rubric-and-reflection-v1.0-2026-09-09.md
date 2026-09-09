# Q2：周报评分表与研究周期 Reflection v1.0

日期：2026-09-09
状态：分支 `q2-reflection`；第一轮 code review 的一个 blocker 与五项已修（第 10 节），已并入 main `8198f0f`（3,437 项测试，第 11 节），待合并
基线：main `61f4255`（2,627 项测试）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节「vision 回顾后的补充」行 **D4 + C4 / Q2**；[全部 vision 讨论的复盘](vision-review-against-plan-v1.0-2026-09-09.md) **C4**、**D4**；[Q 线：研究质量回路 v1.0](q1-research-quality-loop-v1.0-2026-09-09.md)（本片扩展的评分表 / 打分器 / journal 框架）
数据：live Core 只读副本（`/private/tmp/dalton-ro/core.sqlite`，复制到 `/tmp` 后打开）。**没有写过任何 live 状态，没有部署，没有发过 mission 版本，没有发起过任何真实模型调用。**

---

## 0. 一句话

Q1 的报告里写着「**没有周报评分表**。owner 把周报投递搁置到最后，给一个没人在建的交付物写评分表，等于给没有东西打分」。D4 说这句话读错了一个词：搁置的是**投递**，不是**评估**，而 live 上已经有两期周报发布并投到了 Discord。这一片做两件事——给已经存在的周报一份评分表（十条标准、冻结哈希、两条带能力闸），以及给「我们把时间花在哪」一个每周一条、只读不写的答案（`ResearchCycleReflection`）。

**最值得先看的两行数字**：

- 两期已发布周报跑过确定性层，**0 处无源数字、0 处引用残迹、0 处并列重复引用、各缺 1 节**（都缺 `预测对账`——它是 P9c 之后才进模板的，两期都比它早）。十条标准里有 **2 条今天不可评**，按 `not_applicable_yet` 加原因发布，不记 0 分。
- 上一个完整周（2026-W36）系统一共花了 **$0.026 / 19 次计价调用**，占 mission 周预算上限的**不到 0.1%**；新登记 1 条问题、回答 0 条，周末未决 67 条；退役 0 条 Claim；planner 派发 0 条。**预算不是瓶颈**——这句话此前没有任何地方能说出来。

---

## 1. `rubric:weekly-brief`（含哈希）

`src/dalton_core/research_quality_rubrics.py`。第四份评分表，与前三份同样是冻结记录 + `content_hash`。

| rubric_ref | 版本 | 适用对象 | 标准条数 | content_hash |
| --- | --- | --- | --- | --- |
| `rubric:weekly-brief`（短名 `weekly_brief`） | 1 | `weekly_brief_issue_version` | 10 | `ca2c06585715903e456bf4b14d309f52f46380e52643741d71f0bc003046d740` |

**前三份的哈希一个字节都没动**（`334c9aee…` / `7fe91c60…` / `0b1718d1…`），`tests/test_research_quality_rubrics.py::test_the_hashes_are_what_the_report_published` 四条一起钉住。做到这一点的办法写在代码注释里：`Criterion` 新增的 `capability` 字段**只在被设置时才进 `body()`**，所以前三份评分表的 body 与它出现之前逐字节相同；一个永远存在的 `"capability": null` 会为了什么都不说而重写三份冻结标准。

### 1.1 十条标准

前六条是 owner 周会上真正要的六件事（蓝图 P15c 原话：上周价格表现与归因、观点变化、debate 转向、预测变动、缺口与下周计划），后四条是对每一份产出都成立的纪律。

| criterion | 问的是 | 确定性检查 | capability |
| --- | --- | --- | --- |
| `price_performance_attribution` | 每家的涨跌是否被复盘并归因到 driver / 事件 / Claim | `weekly_brief_capability_gate` | **`market_price`** |
| `view_changes` | 哪几家 thesis 动了、往哪动、为什么，没动的为什么没动 | — | |
| `debate_shifts` | 辩论焦点相对上期移动没有，被哪条新证据推动 | `weekly_brief_capability_gate` | **`debate_map`** |
| `forecast_changes` | 哪些预测变了、变多少、是 actual 取代还是 driver 修订 | — | |
| `gaps_named` | 缺什么、缺了它这周哪个问题答不了 | — | |
| `next_week_plan` | 下周计划是不是可执行的研究动作 | — | |
| `number_provenance` | 每个数字是否由被引 Claim 逐字承载 | `numbers_without_refs`、`claim_refs_resolve` | |
| `citation_hygiene` | 剥离引用脚手架后正文是否还成句 | `residual_citation_artefacts` | |
| `citation_dedupe` | 同一事实是否只被引用一次 | `duplicate_parallel_citations` | |
| `structure_complete` | 周报 authority 自己声明的八节是否齐备 | `required_sections_present` | |

`WEEKLY_BRIEF_SECTIONS` 是 `weekly_brief.ISSUE_SECTIONS` 的冻结副本（八节），`test_the_template_is_the_authoritys_own_section_list` 断言两者相等——评分表不去评一个对象没有的结构。**价格一节没有进模板**：它今天是一条标准而不是一个槽位，等市场层落地、P15c v2 真的写出这一节时，随 rubric 版本 2 一起进模板。

### 1.2 能力闸：`not_applicable_yet`，不是 0

六件事里有两件今天做不了：价格归因要等市场层被 mission 授予（Wave 1A 建好了 `MarketPriceSeriesVersion`，live mission 的 `may_write` 里没有 `market_price`），debate **转向**要等 Wave 2 的 `DebateMap`（live 周报的「关键争议」是 evidence pack 的确定性争议清单，是一张快照而不是一次移动）。

关键的设计判断：**能力有没有接入，是关于系统的事实，不是对文档的阅读**。所以它是一条**确定性检查**（`weekly_brief_capability_gate`），不是一个判官问题——一个被要求判断自己装了什么的模型只是在猜。

- 判据是两半都成立：authority 表里**有记录**，且 live mission **授予了**对应的 `may_write` scope。只有一半是半装好的一层：没人可以写的行，或者背后没有东西的权限。
- **没有 Core 连接就按未接入处理**（golden 集、离线重打分）。默认「已接入」会让系统悄悄开始按没人能写的章节给文档扣分。
- 命中时 `status=skipped`（不是 `fail`——文档没有任何地方失败），`count` = 被搁置的标准数，findings 每条带 `criterion_id` / `capability` / `reason`。

今天 live 上的 reason 是逐字这两句：

```
price_performance_attribution（market_price_series_versions 尚无记录：这一层还没有产出任何版本）
debate_shifts（debate_map_versions 尚无记录：这一层还没有产出任何版本）
```

grading notes 里写死四条不扣分的事：能力未接入的标准以 `not_applicable_yet` 加原因发布、永远不记 0 分也不计入均值；首期 baseline 没有 delta 是事实不是缺陷；「尚无正式 ThesisVersion，因此不能断言本期证据改变了投资观点」是满分行为；宁可留空写明缺口也不要写一个没有 authority 承载的价格数字。

**被闸住的标准不被打分，一路到底。** 这是第一轮评审的 blocker，原来的做法（让判官照打、只在发布时改标签）在三个地方漏成了 0：`summarise_scores` 把每条标准都算进均值、把低分列进 `below_passing`，而 golden 集给被闸标准的下界是 0，判读 golden 测试又把那个 0 原样喂给判官。**一个 not_applicable 被平均进去就是一个换了名字的 0。**

现在三处读同一个函数 `withheld_criteria(deterministic)`（把能力闸的 findings 拆开一次）：

- **prompt**：被闸标准标成 `NOT APPLICABLE YET — do not score this one` 并附原因，**不给锚点**，并写明「a number for one of them is refused」。
- **`validate_judge_output(..., withheld=…)`**：被闸标准可以**缺席**，也可以带 `"score": null`（那句 evidence 会被留下来，它正是 grading note 要的「诚实说明」）；**返回一个数字 → 拒绝**。applicable 的标准返回 `null` 或缺席同样拒绝。
- **`summarise_scores(scores, withheld)`**：被闸标准不进 `mean`、不进 `below_passing`，单独以 `{criterion_id, status: "not_applicable_yet", reason}` 列在 `withheld` 里。

复核 prompt 也只看被打过分的那些标准——把一条没有分数的标准连同锚点递给复核者，等于交给它一个「缺答案」去找。

golden 集里被闸标准的下界抬到 2（`test_a_withheld_criterion_never_carries_a_floor_of_zero` 钉住「永远不是 0」），判读 golden 测试改为只回答它真正被问到的那些标准。区间对这两条今天不是判断，而是「市场层与 DebateMap 落地之后一个评分者应当落在哪」。

`test_both_halves_present_makes_the_whole_rubric_gradeable` 用一个装了两层的假 Core 断言：**评分表一个字不改，闸自己打开**（`count` 从 2 变 0）。

---

## 2. 周报变成可打分产物

`artefact_from_weekly_brief`（加进 `research_quality_score.py` 的第四个适配器，与另外三个并排）。

渲染出来的周报是一份**里面装着账本的文档**：每条 claim ref、每个 evidence version、每个 content hash、每个 CIK 都印在正文里。直接对这段原文跑数字检查，会把 CIK `0001467373` 报成一个无源数字、把 `retrieved=…T06:31:56.471063` 里的 `31` 报成一个数字。所以适配器把机器地址提到 `claim_refs`（引用该在的地方），在正文里**留下一个可见的 `〔ref〕` 标记**。

标记不是装饰。悄悄删掉 ref 会藏起 `citation_hygiene` 在周报上真正要评的东西：机器的地址是由引用字段承载，还是抹在人要读的句子里。判官数每句话里的 `〔ref〕` 就是在读那个缺陷。

每条被引 Claim 贡献**两条**来源字符串：`normalized_statement`（数字的出处，filing 说 `up 5.59%`）和 `value unit`（周报实际印出来的 `5.59 percent`）。只知道前一条的数字检查会把每一期周报的每一个数字都报成无源。

---

## 3. Golden 集与结果

`tests/golden/weekly-brief/*.json`，5 例，格式与 Q1 完全一致（`source` / `note` / 完整 artefact / `expected.deterministic` / `expected.score_ranges` / `score_ranges_are_calibration_only: true` / `rationale`），新增一个 `expected.not_applicable_yet`。

**live 只有两期，不是三期。** 任务书要「≥3 期最近的 live 周报」，`weekly_brief_issue_versions` 里一共两行（`weekly_brief_issue_pointer` 一行、`weekly_brief_deliveries` 两行），所以第三期不存在。如实记在这里，并用三个人为破坏例把 golden 集补到 5（golden 测试要求每份评分表 5–10 例）。

| 案例 | 来源 | 能力闸 | 无源数字 | 引用残迹 | 并列重复 | 章节 |
| --- | --- | --- | --- | --- | --- | --- |
| `live-w35-baseline` | live v1，human:lumos 2026-08-27 发布并投递 | skipped(2) | 0 | 0 | 0 | **fail(1)**：缺 `预测对账` |
| `live-w36-second-issue` | live v2，core 2026-09-03 按 cycle admission 自动发布并投递 | skipped(2) | 0 | 0 | 0 | **fail(1)**：缺 `预测对账` |
| `broken-unsourced-figure` | v2 副本 + 一句无源的行业 margin | skipped(2) | **fail(2)** | 0 | 0 | fail(1) |
| `broken-residual-markers` | v2 副本 + live ACN v2 S7 的逐字残迹 | skipped(2) | 0 | **fail(5)** | 0 | fail(1) |
| `broken-duplicate-parallel-citations` | v2 副本 + DXC 的 −5.06% 由三条 ref 并列承载 | skipped(2) | 0 | 0 | **fail(2)** | fail(1) |

三个破坏例全部从**同一期 live 周报**派生，`test_a_broken_variant_breaks_only_what_it_was_built_to_break` 逐检查断言它们只坏掉自己那一项——一个漏到隔壁检查的缺陷是侦测器的问题，不是文档的问题。

**读法**：

1. **数字纪律又一次是有效的那一层**。两期 live 周报 0 处无源数字、0 处残迹、0 处并列重复——和 Q1 在五份 Initial Screen 上读到的结论一致：有硬闸的地方干净，没有闸的地方带伤。周报的正文由确定性渲染器生成而不是模型起草，所以它今天比 Initial Screen 干净；**P15c v2 一旦让 LLM 起草叙事，Initial Screen 身上那 30 处残迹会原样搬过来**，`broken-residual-markers` 就是先把那个未来钉住。
2. **两期都缺 `预测对账`**，而且两期的 issue 记录里 `sections` 都只有七项。`ISSUE_SECTIONS` 是八项，渲染器按「发布时冻结的那份列表」决定要不要输出这一节（`weekly_brief.py:1064-1068`，为的是重放逐字节一致，这是对的）。结果是：**当前模板要求的一节，live 上一期都没有过**。`forecast_changes` 这条标准因此在两期上都低。
3. `broken-duplicate-parallel-citations` 的 count 是 2 而不是 1，因为适配器给每条 Claim 放两条来源字符串（statement 形与 value 形），两组各命中一次。三条 ref 的那一组是对的，计数是适配器形状的产物——记在这里，见开放问题 5。

**分数区间仍然只是校准**（`score_ranges_are_calibration_only: true`，逐例断言）。**没有跑过任何一次真实判读调用**，理由和 Q1 一样：这个 worktree 上不动 live 预算。

---

## 4. `ResearchCycleReflection`

`src/dalton_core/research_cycle_reflection.py` + `research_cycle_reflection_schema.sql`。每周一条，每 mission 一条链，append-only、`content_hash`、三触发器、写完读回校验。

### 4.1 v0.4 冻结，按字面执行

- **不写 Ledger**：模块只打开自己的两张表。四条测试从两侧钉它：`test_the_module_imports_nothing_that_could_write_the_ledger` 与 `test_the_lane_child_imports_nothing_that_could_write_the_ledger_either` 逐行扫 import（评审第 2 项：原来只扫了 authority 模块，没扫**真正无人值守跑起来的那个**）；`test_the_only_writes_are_its_own_two_tables` 与 **`test_the_lane_child_writes_only_its_own_two_tables`** 装一个 `sqlite3` authorizer，把两张表以外的任何 INSERT/UPDATE/DELETE 判 `SQLITE_DENY`——后者把 `DaltonStore` 换成一个在构造时就装上 authorizer 的子类，然后跑真正的 `main(["run", …])`；`test_computing_a_reflection_writes_nothing_at_all` 把连接设成拒绝一切写，再算一遍。唯一能写的 import 是 mission authority（它是唯一能校验一份 live mission 的东西），它被延迟到函数内、不被保存、只被调用一次读——`test_the_child_reads_the_mission_and_never_holds_the_authority` 钉住这三点，而真正兜底的是上面那条 authorizer 测试。
- **不改 policy**：产出里有 `policy_suggestions`，它们是句子。
- **不登记问题**：产出里有 `backlog_candidates`（`question` + `because` + `refs`，正是 `ResearchQuestionBacklog.record_question` 要的三样），由 planner 或人经它自己的准入路径登记。记录里有一条 `authority_note` 把这三句话写进每一行，读一行的人不必去找模块才知道它不许做什么。
- **没有模型调用**。整条 reflection 是对已经在库的行做算术，所以这条 lane 不花钱，也所以两次跑同一周会得到同一个 `inputs_hash`。

### 4.2 八项指标

| 指标 | 取自 | live（2026-W36）读数 |
| --- | --- | --- |
| 各池花费 vs 池上限 | `observability_cost_entries` ⋈ `observability_usage_entries` ⋈ `model_invocations`，按 **work order family** 分池 | $0.026 / 19 次；agenda $0.016（63%）、llm-research-planner $0.010（37%）；占周上限 $700 的不到 0.1% |
| 新登记问题 vs 被回答 | `backlog_question_events.state` | 新 1 / 答 0 / **周末未决 3** |
| 退役 Claim | `claim_retirement_decisions` | 退役 0、保留 0、新挑战 0、未裁决 0 |
| planner inquiry 派发 / 总数 | `coverage_mission_research_plans.inquiries_json`；派发 = 本周 `admission.source == "inquiry"` 的新 `BoundedPlannerLoop` | 0 / 0；两道闸里只差 `research_task` 授予（ProbeTemplate 已有 2 份） |
| 闲置 tick 比例 | 调用方交上来的 tick 摘要 | **不可算**（见下） |
| 未关闭人类检查点的年龄 | `coverage_mission_stage_records` 里最新状态仍是 `entered` 的 (company, stage) | 1 个（CTSH `initial_screen`），0.65 天 |
| 本周发布的质量分 | `research_quality_score_versions` | **不可读**：Q1 的表还没部署到 live |
| 反馈按词计数 | `analyst_journal_entries` + `weekly_brief_feedback`（同一套五词） | 0 条，且标 `partial`：两处只有一处已部署 |

**「池」是 work order family，不是 `driver_key`。** 任务书说「C2 落地前 `pool` = lane 的 `driver_key`」，但 Core 里没有任何一行成本能回指到 lane：成本挂在 `work_order_ref` 上，而 work order 的 id 是各生产者自己拼的前缀（`work:document-extraction-<digest>`、`work:llm-research-planner-<digest>`、`work:cockpit-<purpose>-<digest>`）。所以池 = 前缀，lane 归属只在**代码能证明**的四处标注（`document-extraction` / `document-numeric` / `metric-discovery` → `document_extraction` lane，`llm-research-planner` → `research_plan` lane），其余标 `lane: null`。**一个没被核验的「谁花了这笔钱」比一个诚实的空格更坏。** 这直接变成第一条 policy suggestion：C2 的 `budget_pool` 应当被写进 work order 的 id，否则每一次归集都是一次对 ref 前缀的解析。

**闲置率今天不可算，而且这是本片最有价值的一个发现。** `bounded_planner_driver.run_once` 的摘要是一个返回值；`service` 把它放进 `run/heartbeat.json`；下一次 tick 覆盖它。**Core 里没有 tick 账本。** 所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。reflection 因此接受调用方交上来的 tick 摘要（CLI 的 `--tick-summary-dir`），没有就报 `available: false` 加原因——**0 和「没有」在表格里长得一样，意思相反**。第二条 policy suggestion 就是「tick 摘要应当落到 Core 的一张 append-only 表」。

### 4.3 「我们把时间花在哪」

散文 + 表格，两者都只由指标派生，没有模型参与。live 上这一周逐字如下（`narrative.prose`）：

```
这一周（2026-W36）一共花了 $0.03、19 次计价调用，占 mission 这一周预算上限（$700.00）的不到 0.1%。
最大的一池是 agenda（没有已注册 lane 认领它），$0.02，占本周花费的 63%。
问题账本上，本周新登记 1 条、被回答 0 条，周末仍未关闭的有 3 条。新登记多于被回答，未决问题集合在这一周变大了。
Claim 层退役 0 条、保留 0 条，本周新提出 0 条挑战，周末还有 0 条挑战没有裁决。
规划器本周记录 0 份 research plan、提出 0 条 inquiry，其中 0 条被派发。本周没有从 inquiry 开出任何
BoundedPlannerLoop。P14e 的两道闸里，mission 的 may_write 没有授予 research_task；两者都是 owner 的
版本化动作。
闲置率不可算：Core 里没有 tick 账本——run_once 的摘要只写进 run/heartbeat.json，下一次 tick 就覆盖它；
除非有人归档过，没有东西可数
有 1 个 stage 停在 entered 等人，最老的一个已经等了 0.68 天；本周关掉了 0 个。
质量分不可读：research_quality_score_versions 不在这个 Core 里：Q1 的质量分 authority 还没有部署
这一周没有人给过任何一条反馈；另一处（analyst_journal_entries）不在这个 Core 里，没有数进来。
五个词的反馈词表还没有 UI，所以「没有反馈」暂时不能读成「读过而没有意见」。
```

表格（`narrative.table`）每池一行：`pool` / `lane` / `cost_usd` / `calls` / `share_of_spend` / `share_of_mission_cap`。

live 这一周产出 **1 条 backlog candidate**（「这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？」，because：五个词的反馈词表已经存在两处，两处都还没有 UI）与 **3 条 policy suggestion**（C2 的 budget_pool 应进 work order id；tick 摘要应落到 Core 的一张表；本周实际花费不到预算上限的 0.1%，所以「做得少」的原因要到别处找，提高上限不会让系统做更多事）。

候选的生成规则都盯**形状**而不是某个人喜欢的阈值：花了钱而未决问题集合没动、inquiry 提了没派发、检查点等待超过清它的那条 cadence、闲置率过半、这一周没打过任何质量分、这一周没有任何人类反馈。上限 6 条。

### 4.4 duplicate 规则

`reflection_ref = research-cycle-reflection:<mission>:<ISO 周>`；`inputs_hash` 覆盖 reflection 读到的每一个数字加窗口加 counter 版本。同一周、输入没动 → `duplicate`；输入动了（周一之后到账的成本行、周二回答的问题）→ **同一周的新版本**，不是第二条记录。数据库上由 `UNIQUE(reflection_ref, inputs_hash)` 承担。

**未关闭检查点的年龄被刻意排除在身份之外**：它按定义每秒都在变，一个永远不会命中的 duplicate 规则不是规则。`test_ageing_alone_does_not_make_a_new_version` 钉住这一条。

**tick 每次都算一遍这个哈希。** 评审第 3 项：原来的 lane 只问「这一周有没有一行」，那让上面这条「输入动了就出新版本」永远走不到——周二到账的一条成本行不会被任何人注意到。现在 tick 在进程内算出本周的 reflection（只读、无模型、八条查询）并比哈希；child 再算一次并写入，两次之间又落了一行的话，authority 看到不同的哈希、写下一个版本，这也是对的。ticket 与失败 hold 都按「周 + 读数」联合命名，所以一个在某个读数上失败的周，会在它的数字变了之后被重试。

---

## 5. Lane

`mission_reflection_lane.py`：`LaneSpec(operation="dispatch_mission_reflection", order=120, driver_key="mission_reflection", init_kwarg="reflection_launcher")`，`lane_registry.LANE_MODULES` 加一行。排在最后，因为它读这一周别的 lane 干了什么。

**cadence 用两套机制表达两次，是刻意的**：tick 持有边界（`closed_week` 认的是上一个**本地**周一 00:00 收口的那一周，owner 的周一而不是 UTC 的），authority 持有规则（输入没动的第二次写入是 duplicate）。**「这周写过没有」是读自己的 authority，不是内存里的定时器**，所以周三重启不会写出第二条。六天里的每一个 tick 都答 `duplicate` 并给出 ref——不是 `idle`：`idle` 说的是「没有事情要做」，而事情做过了，两者对读日志的人不是一回事。

- 写入范围 **`deliverable`**：reflection 是一份带日期、按 cadence 产出、给人读的 mission 文档，和 Initial Screen 同类，而且不对任何公司断言任何事。live mission 已经授予了 `deliverable`，所以**不需要 owner 发新 mission 版本**。没有授予时 lane 报 `ungranted` 并说明理由。
- 一个 child，`LaneChildLauncher` 的标准形状，命令是 `python -m dalton_core.research_cycle_reflection_cli run`，**没有 `--model-config`、没有 `--scheduler-db`、没有 `--allow-network`**（测试逐个断言）。
- `argv_fragment` 只在 state 目录里有 `core.sqlite` 时返回参数。这是唯一诚实的门：这条 lane 不需要模型配置也不需要 connector 批准，它需要的是一个可以反思的 Core。**mission 授予不在这里检查**——plist 只在安装时渲染一次，一条因为授予变化而从 plist 里消失的 lane 要重装才能回来。
- 失败的一周被 hold 在进程内，不会每五分钟重试一次。

**CLI**：`dalton-research-reflection run|show`（`pyproject.toml` 已加 console script）。`--tick-summary-dir` 今天没有任何东西写，参数存在是为了「哪天有人开始归档，读它不需要改代码」。

**打分钩子**：`dalton-research-quality score --rubric weekly-brief --target <issue ref>` 在 live issue ref 上跑通了（第 6 节）。`--rubric` 的取值来自 `RUBRIC_ALIASES`，所以加一个别名就有了这个选项；`run_score` 里按 rubric 分派 resolver，周报走**重新渲染**而不是读一个存下来的 blob——authority 本来就不存 body，`render_markdown` 从 evidence pack 重放并在 pack 漂移时拒绝，所以一条分数永远是关于一份**还以发布时的形态存在**的文档。

---

## 6. 只读 smoke（live 副本）

`/tmp` 上的只读副本，全程没有写过 live：

**两期周报的确定性层**（真 Core，引用真解析）：

| issue | 正文字符 | 章节 | claim refs | 能力闸 | 无源数字 | 引用可解析 | 残迹 | 并列重复 | 章节 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `…:us-it-services:2026-w35` | 4,612 | 7 | 4 | skipped(2) | pass(0) | pass(0) | pass(0) | pass(0) | **fail(1)** |
| `…:9eafec9e9796671cd2ed9fcbae0ef596` | 5,209 | 7 | 5 | skipped(2) | pass(0) | pass(0) | pass(0) | pass(0) | **fail(1)** |

CLI 端到端（`--dry-run`，退出码 0）跑的是同一条路径，能力闸的 reason 在真 Core 上是 `market_price_series_versions 尚无记录` / `debate_map_versions 尚无记录`——两张表在 live 上都不存在。

**本周 reflection**：窗口 `2026-08-31T00:00:00-04:00 → 2026-09-07T00:00:00-04:00`，`iso_week=2026-W36`，`inputs_hash=866a3ecb…`；八项指标里 6 项可读（其中反馈一项标 `partial`）、2 项按原因报缺（闲置率、质量分）；1 条 backlog candidate、3 条 policy suggestion。数字见 4.2 与 4.3。

**评审后最大的一处数字变化：周末未决问题从 67 变成 3。** 67 是「现在有多少条问题是开着的」，3 是「上周日晚上有多少条是开着的」——原来的读法没有把窗口的右端算进去（评审第 6 项），于是把窗口结束之后才登记的问题也算了进来。一份关于上一周的报告里，那是一个字面上错误的数字。

---

## 7. P15c 周报 v2 应该从这里读什么

周报 v2 要「叙事由 LLM 起草，结构与数字由权威强制」。这一片给它三样东西：

1. **一份可以在起草前贴进 prompt 的评分表**。`WEEKLY_BRIEF.grading_notes` 与十条 anchor 就是「一份好周报长什么样」的合同；能力闸告诉起草器**这一期哪两节不该写**——`not_applicable_yet` 的两条标准，正是它必须留空并写明原因的两节。一个不知道自己对市场是盲的的起草器会编一段涨跌归因，而无源数字比空白更坏。
2. **一份可以在发布前跑的确定性检查**（零成本、零模型）：`numbers_without_refs`、`residual_citation_artefacts`、`duplicate_parallel_citations`、`required_sections_present`、`claim_refs_resolve`。Q1 对 Initial Screen 的建议在这里同样成立，而且更急——周报 v2 是第一份由模型起草叙事的定期外发文档。**`预测对账` 一节必须真的写进 `sections`**，否则 v2 会继承 v1 的那一处结构缺失。
3. **一条 reflection 记录**，它正好回答周会开头那句话。`narrative.prose` 与 `narrative.table` 可以原样成为周报的一节（建议命名就叫「我们把时间花在哪」）；`backlog_candidates` 可以原样成为「下周研究计划」的候选——它们已经是 `question` + `because` + `refs` 的形状，也就是 `next_week_plan` 这条标准要的 4 分形状；`policy_suggestions` 是给 owner 的，不是给周报读者的。

**周报 v2 不应该从 reflection 里读的**：任何判断。reflection 只报告别的 authority 已经记下的事，它不产生投资断言，也不产生「我们做得好不好」的结论。

---

## 8. 集成待办与开放问题

### 8.1 集成待办（都不在这条分支上）

1. **`research_quality_schema.sql` 的 `artefact_kind` CHECK 加了 `'weekly_brief'`**。这张表**从未部署过**（live Core 里没有 `research_quality_score_versions`），所以不存在迁移问题；`CREATE TABLE IF NOT EXISTS` 不会回头改约束，如果它已经部署过，这一处就需要一次迁移。合并时确认一下部署顺序。
2. **cockpit / install.sh 不需要改**。lane 的 argv 片段由 registry 拼出，条件只是 state 目录里有 `core.sqlite`；lane 不用模型配置，所以 `install.sh` 的模型配置块不动。
3. **`tick-summaries` 归档**要有人写才有闲置率。最小实现是在 `service` 写 heartbeat 的同一处多写一个按日期命名的文件，或者（更好）一张 Core 表——那是 policy suggestion 里的第二条，属于集成决策。
4. **`research_quality_cli.py` 被改了一处**（新增 `_resolve_weekly_brief` 与 rubric 分派）。它不在我的所有权清单也不在禁改清单，而交付项 5 要求 CLI 在 live issue ref 上可用，所以改了，改动是纯新增。
5. **CLI 的短名是 `weekly_brief`**（与另外三份一致），不是 `weekly-brief`；golden 目录同名。`rubric("weekly-brief")` 仍然解析得到——ref 是带连字符的，所以那是每个人都会犯一次的拼法——但它不在 `RUBRIC_ALIASES` 里，因为 golden 目录与 CLI 的 `choices` 都是从那张表派生的。
6. **`research_task` 授予**：P14e 已经在 main 上落地，live 也已经有 2 份 ProbeTemplate，唯一缺的是 mission 的 `may_write` 里没有 `research_task`。reflection 现在会把这句话逐字写进每周的 `dispatch_reason`，直到 owner 发一版授予它。

### 8.2 开放问题

1. **live 只有两期周报，不是三期。** 交付要求的「≥3 期」在今天的账本上做不到。第三期会在下一个 cycle admission 到期时出现；届时把它补进 golden 集是十分钟的事（生成脚本的形状见 §3）。
2. ~~**被闸住的标准仍然要判官返回一个数字。**~~ **已修**（评审 blocker，第 1.2 节）：判官不再为它们打分，缺席或 `null` 都可以，返回数字则整条判读被拒。剩下的问题小一号但是真的：**没有跑过真实判读，所以没有证据说明模型会照办**。第一次真实调用之后要看的第一件事，就是它有没有为被闸标准编一个分数出来——那正是这条契约存在的理由。
3. **能力闸的「两半都成立」可能太严。** 一个刚接好但还没跑出第一版的市场层会被判成未接入，而这时其实已经该要求周报写价格一节了。反过来放松成「或」会让一个空表加一句授予就打开闸。我选了严的一侧，因为误开的代价（开始按没人能写的章节扣分）比误关的代价（晚一周开始要求）大。
4. **候选生成规则是我写的，没有第二个人看过（评审看了代码，没有评过判断）。** 和 Q1 的第四条开放问题同一个形状，而且更尖锐：这一片的产出是「系统怎么评价自己的一周」。建议 owner 拿 2026-W36 这条 reflection 的散文与候选各读一遍，说哪一句是废话、哪一条候选不该被提出来。
5. **`duplicate_parallel_citations` 在周报上的计数是适配器形状的产物。** 每条 Claim 放两条来源字符串（statement 形与 value 形），所以一处真实的三 ref 重复会记成 2。findings 本身是对的（period、三条 ref 都在），只有 count 被放大了一倍。改它要么让适配器合并两条来源、要么让检查按 ref 集合而不是按 token 组去重——两者都会动 golden 计数，留给下一次 rubric 升版。
6. **周报把公司称作 CIK。** live 两期的「对现有观点的影响」一节逐字是 `company:sec-cik:0000051143：insufficient——…`。适配器在打分时用 evidence pack 的 `coverage_universe` 把它换成 ticker，但**发布出去的那份文档里它还是 CIK**。这是 P15c v2 应该顺手修的，一行渲染改动。
7. **`work_order_family` 是一次前缀解析。** 它今天正确（八个前缀全部在代码里拼出，逐一核对过），但它是约定而不是契约。C2 落地时应当作废它。合并 main 之后又多了 S1/S2/S3 的几条连接器 lane（sales-notes、company-wiki、guidepoint、雪球、xreach、员工评价），它们的 work order family 没有被映射到 lane——按「不核验就不猜」的规则报成 `lane: null`。

8. **tick 每次都算一遍 reflection。** 这是让「输入动了就出新版本」为真的代价（第 4.4 节）：八条只读查询，其中 `spend_by_pool` 是一次跨三张 observability 表的全表连接，今天 3,568 行、会长。它不写、不花钱、不到一秒，但它是 O(账本) 而不是 O(本周)。真正的修法是把窗口下推进 SQL；那是一次独立的改动，等这张表大到能被量出来再做。

---

## 9. 验收结果（原文）

全量测试，`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，**合并 main `8198f0f` 之后**：

```
Ran 3557 tests in 327.998s

OK (skipped=1)
```

main 是 3,437 项，本片新增 **120 项**。分项：

```
tests.test_research_quality_weekly_brief: Ran 41 tests in 0.066s
tests.test_research_cycle_reflection:     Ran 55 tests in 0.835s
tests.test_mission_reflection_lane:       Ran 22 tests in 0.196s
tests.test_research_quality_rubrics:      Ran 16 tests in 0.002s   （Q1 的 15 项，净 +1）
tests.test_research_quality_golden:       Ran 15 tests in 0.156s   （Q1 的 14 项，净 +1）
tests.test_research_quality_score:        Ran 77 tests in 0.108s   （Q1 原样，一项未改）
```

没有失败、没有静默跳过。`tests/test_service.py` 与 `tests/test_lane_registry.py` 的字面量一个字没改。
改了两处别人的测试断言，都是「加一条 lane 之后那句话不再为真」，两处都是把断言改成它本来的意思而不是把它放宽：
`test_crowd_source_lane::test_the_lane_runs_after_every_evidence_lane`（它手写了一个「不是证据 lane」的例外，
现在有两个：P14e 的 research-task 与本片的 reflection），以及 Q1 的判读 golden 测试（见第 10 节第 1 条）。
`dalton-research-quality golden run --rubric weekly_brief` 五例全部与 golden 一致（退出码 0）。

**没做的事**，如实列出：

- 没有跑过任何一次真实模型判读，所以周报的分数区间没有被验证过，判官会不会遵守「不给被闸标准打分」也没有证据。
- 没有第三期 live 周报（不存在）。
- 没有 tick 摘要归档，所以闲置率在 live 上仍然不可算——只有在测试里用合成摘要验证过。
- 没有把确定性层接进周报的发布前检查（那会碰 `weekly_brief.py` 的发布路径）。
- 没有 cockpit 接线：reflection 今天只能用 CLI 读。

---

## 10. 第一轮 code review：一个 blocker 与五项

评审复算了本报告的每一个数字、确认了三份 Q1 哈希逐字节未动、确认能力闸读的是 live 授予、确认冻结在进程内成立。以下在同一条分支上修完，全量重跑。

1. **（blocker）`not_applicable_yet` 一路上仍然是 0。** 见第 1.2 节：闸报了，然后下游三处都不同意它——`summarise_scores` 把每条标准都平均、把低分列进 `below_passing`，golden 的下界是 0，判读 golden 测试又把那个 0 喂给判官。现在三处读同一个 `withheld_criteria`；prompt 收走锚点并写明「返回数字会被拒」；`validate_judge_output` 接受缺席或 `null`、拒绝数字；`summarise_scores` 把它们移出均值与 `below_passing`，单列 `withheld`；golden 下界抬到 2 并由测试钉住「永远不是 0」。
2. **冻结的证明没有覆盖 lane child。** 现在 `test_the_lane_child_writes_only_its_own_two_tables` 用一个在构造时装 authorizer 的 `DaltonStore` 子类跑真正的 `main(["run", …])`；import 扫描扩到 CLI 模块；唯一能写的 import（mission authority）被单独钉成「延迟导入、不保存、只调一次读」。
3. **lane 永远不会为变化了的一周再出一版**：它问的是「有没有行」而不是「哈希是不是这个」。现在 tick 在进程内算出本周并比哈希，ticket 与失败 hold 都按「周 + 读数」命名。
4. **混合时区的时间戳被当文本排序。** Ledger 里同时有 `+00:00` 与本地偏移（第一期周报就是 `-04:00`），按文本排会把一个西偏移的时间戳排到更晚的 UTC 之后——于是一个已经关闭的检查点被读成还开着。stage record 与 backlog event 现在都按 `_as_utc` 解析出的瞬间排序。
5. **两处反馈表都不在时，反馈数悄悄报 0。** 那句话的意思是「人读了、没有意见」，而实际发生的是「没有地方写意见」。现在两处都缺 → `available: false`；缺一处 → `partial` 加 `missing_sources`，散文里也说出来。
6. **`open_at_end` 依赖行序且没有右端边界**：它数的是「现在开着的」而不是「上周日晚上开着的」。现在按瞬间取窗口结束前的最后一个状态。**live 上这个数从 67 变成 3。**

外加评审列的五个 nit，全做了：能力闸的授予只读被反思的那个 mission（不再跨 mission 取并集）；`closed_week` 在日历上做算术，DST 边界不再让窗口多一小时少一小时；两个 planner 的 work order family 补进映射；`_brief_prose` 里没人读的计数器删掉；短名统一成 `weekly_brief`。

---

## 11. 并入 main（`8198f0f`）

评审期间 main 走了五步（S1 人工投喂、S2 Guidepoint、S3 众源、P14-M 模型路由、P14e 专项研究、C1 事件日历、P14a daily tracking），所以这里合了四次。除了下面三件事，四次都是「两条分支在同一个位置各加一行」。

**lane order 从 120 挪到 160。** S1 的 feed lane 占了 120 与 130，S3 的众源 lane 占 140，P14e 的 research-task lane 占 150，而 `register_lane` 对重复 order 直接抛错——这正是 Wave 0 把顺序做成显式的理由。现在 registry 上有 20 条 lane，160 仍然是最后一个，理由没变：它读这一周别的 lane 干了什么。

**S3 的众源 lane 有一条断言要跟着改。** `test_the_lane_runs_after_every_evidence_lane` 断言众源是最后一条**证据** lane，并手写了唯一的例外（P14e 的 research-task lane，它不取证据）。reflection 是第二个例外，所以那句断言改成说它本来的意思：排除「不取证据的那些」，而不是排除「上次看到的那一条」。

**P14e 落地改变了一项指标的读法。** `planner_inquiries` 原来写的是「P14e 尚未落地，`adhoc_research_enabled` 仍为 False」——那句话现在是错的。派发数现在数的是 `admission.source == "inquiry"` 的新 loop（人开的 loop 单列 `human_opened_loops`：它也回答问题，但那不是 planner 的 inquiry 被执行），而 0 的理由改成去读 P14e 真正的两道闸。**live 上的答案是：ProbeTemplate 已经有 2 份，只差 mission 的 `may_write` 授予 `research_task`** ——这条现在会逐字出现在每周的 reflection 里，直到 owner 发一版授予它。

**名字没有撞车，但离得很近。** P14a 带来了 `thesis_reflections` 与模型用途 `thesis_reflection`：那是「一个事件对某条 thesis 意味着什么」的判读，一次一个事件、要花模型钱。本片的 `research_cycle_reflection` 是「这一周我们把时间花在哪」，一周一条、不花钱、不产生任何投资断言。两者共用一个中文词而不是一个对象，集成时值得在 PROJECT_STATUS 上把这句话写清楚。

合并后 main 上还多了六条连接器 lane（sales-notes、company-wiki、guidepoint、雪球、xreach、员工评价）与两条判断 lane。它们的 work order family 都没有被映射到 lane，按「不核验就不猜」的规则报成 `lane: null`；下一次有人动 `WORK_ORDER_FAMILY_LANES` 时应当把它们逐一核验补上，而真正的修法仍然是 C2 把 `budget_pool` 写进 work order id。
