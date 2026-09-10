# P15d 高 conviction call：自动化提案，人裁决 v1.0

日期：2026-09-09
分支：`w3-conviction-call`（worktree `~/Projects/dalton-w3-conviction-call-worktree`），基线 main `ebd2ea8`，过 review 后已 `git merge main` 到 `56cf508`（P14b/P14d 修订回路、INT2、ADR-0009 都已并入）
作者：Wave 3 agent「conviction-call」（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) §1 owner 自我反思规则与 §5.2 P15d、[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) §3 ②「high conviction 超预期机会」与 §5.2 P15d、ADR-0006 / ADR-0007 / ADR-0008、[P12c DebateMap](p12c-debate-map-v1.0-2026-09-09.md)、[P12a 公司档案](p12a-company-dossier-v1.0-2026-09-09.md)、[P14a 每日跟踪](p14a-daily-tracking-v1.0-2026-09-09.md)、[P13-M2 预测行](p13-m2-forecast-lines-v1.0-2026-09-09.md)、[P11a 市场层](p11a-market-layer-v1.0-2026-09-09.md)、[Q1 质量回路](q1-research-quality-loop-v1.0-2026-09-09.md)、[INT1 Cockpit](int1-cockpit-install-v1.0-2026-09-09.md)

全量测试：

```
Ran 4357 tests in 728.094s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，`git merge main` 到 `56cf508` 之后。
本片自己 156 项：`test_conviction_call` 73、`_draft` 42、`_lane` 38、`_cockpit` 3。
merge 之前在基线 main `ebd2ea8`（3,932）上是 4,061。）

---

## 0. 一句话

Dalton 现在**会主动 call**，但只在它能说清「市场错在哪」的时候：一个公司要有活着的 thesis、
要在某条 live debate 上站在市场的对面（或者预测与街上的差超过阈值）、而且市场的看法要有来源可指——
三条全是确定性的，任何一条不满足就连模型都不叫；过了这三条才由一次有界模型调用写下方向、
variant view、预期差、可观察的靠拢路径、对照手册风险回报标准的上下行；独立核验通过之后，
它只能变成一条**提案**，进 `conviction_call` 人类检查点，由人 accept / reject / defer，
每一条裁决 append-only 并绑定提案的 content hash。被接受的 call 由 `accepted_calls(window)`
读出来，等 P15c 的周报来取。

---

## 1. 文件清单

| 文件 | 性质 | 是什么 |
| --- | --- | --- |
| `conviction_call.py` + `conviction_call_schema.sql` | 新增 | `ConvictionCallProposal` 权威：append-only、`content_hash`、`dalton_authorized()` 三触发器、按 (company, evidence fingerprint) 幂等、按 (company, ISO 周) 限一条；确定性前置闸 `precheck`；手册风险回报标准的确定性判定 `check_risk_reward`；决定表（accept / reject / defer，绑 proposal hash，`human:` 才写得进去）；读取面 `open_calls` / `deferred_calls` / `accepted_calls(window)` / `calls_this_week`；Q1 制品渲染 `conviction_call_artefact`；rubric 机械半边 `rubric_findings` |
| `conviction_call_draft.py` | 新增 | 输入表、起草 prompt、封闭输出契约与整体拒绝、独立 verifier、`draft_conviction_call` 编排。所有日期来自日历行，所有 consensus 数字来自 bridge，模型只写论证 |
| `conviction_call_cli.py` | 新增 | 子进程：先查授权与检查点，再跑闸，再（且仅在需要时）叫模型；把 gate 结果与失败原因写进 summary |
| `conviction_call_launcher.py` | 新增 | `LaneChildLauncher` 子类，run 按 (company, fingerprint) 命名 |
| `mission_conviction_lane.py` | 新增 | tick 协调器 + `LaneSpec`（order 141，driver_key `conviction_call`） |
| `deploy/phase9/p15d-conviction-policy-v1.json` | 新增 | 冻结策略（阈值、周配额、手册风险回报标准表），与代码里的 `CONVICTION_POLICY` 有漂移测试 |
| `tests/test_conviction_call.py` / `_draft.py` / `_lane.py` / `_cockpit.py` | 新增 | 73 + 42 + 38 + 3 |
| `tests/golden/conviction_call/*.json` | 新增 | 5 例 golden（1 正例、3 反例、1 边界） |
| `lane_registry.LANE_MODULES` | 加一行 | 注册 lane 模块 |
| `research_quality_rubrics.py` | additive | `CONVICTION_CALL` rubric（7 条标准）+ alias + `__all__`；既有四份 rubric 的 body 与哈希不变 |
| `writer_server.py` | additive | 唯一一个新 op `decide_conviction_call`（human-governance only），照 INT1 的 `record_analyst_journal_entry` 接线：操作集合、`OPERATION_FIELDS`、`OPERATION_ACTOR_FIELDS`、开/关权威、`_op_` 方法 |
| `cockpit_plane.py` | additive | approvals 里列出未裁决的 call（含三个中文标签表），不加按钮 |

---

## 2. `ConvictionCallProposal` 的形状与它为什么长这样

```
{company_ref, week_key, direction ∈ {long, short, avoid}, decision ∈ 手册五词,
 confidence ∈ {low, medium, high}, time_horizon ∈ {3_6_months, 6_12_months, 1_3_years, 3_5_years},
 variant_view {our_view, market_view, where_market_is_wrong, convergence_pathway},
 consensus_gap {status, reason, metrics[]},
 event_pathway[] {signal, window, catalyst_ref, refs},
 risk_reward {upside, downside, standard},
 falsifiers[], thesis_refs[], debate_refs[],
 evidence_fingerprint, precheck, rubric, drafted_by, verified_by,
 policy_ref/hash, mission_version_ref/hash, checkpoint_kind, actor_ref, content_hash}
```

四条设计上值得写下来的选择：

**`variant_view` 的四格全是必填，而且 `market_view` 不可缺。** 蓝图给的是 `available:false` 的形状，
DebateMap 也是这么做的。但一条 call 与一张 debate map 不一样：map 记录「谁在争什么」，缺一边是事实；
call 断言「市场错了」，而没有市场看法的时候，「市场错在哪」这句话没有指涉对象。所以契约层的规则是：
`market_view.available:false` 是一个合法的**回答**（起草层遇到它就报 `no_variant_view` 收工，一次调用，
不叫 verifier），但不是一份合法的**提案**。`market_view` 还必须带 `sources`，取自封闭词表
（consensus / sell_side_rating / debate_market_position / sales_note / crowd_narrative / management_framing）——
「街上这么想」出自券商研报和出自雪球帖子是两个不同的断言。

**`consensus_gap` 与日期都不由模型写。** 预期差逐项来自 forecast-vs-consensus bridge，原样带进记录；
每一步 pathway 的日期来自模型**指名的那一条日历行**（`catalyst_row_id`），不是模型敲的字符串。
会敲 `2026-09-25` 的模型也会敲错，而一份日期错了的 call 比一份没有日期的 call 更坏。日历上没有的信号
写 `window.kind = unknown`，这是诚实的，不扣分（rubric 的 2 分锚点就是它）。

**`risk_reward.standard` 是权威算的，不是起草者说的。** `CONVICTION_POLICY.risk_reward_standards`
是对 Playbook `risk_reward_standards` 那五句自由文本的一次**阅读**，每一行都带 `playbook_text`
说明它读的是哪一句，所以不同意这个阅读的人能看见被读的是什么。四种判定，含义不同：
`met` / `not_met` 是对已给数字的判决；`unavailable` 是「有标准但这份 call 没给可测的数字」，
是 call 的缺口而不是通过；`avoid` 得 `not_applicable`——手册对「站在一边不动」没有回报标准。
做空落在 3–6 个月之外直接 `not_met`，因为手册明写「做空：3–6 个月 30% downside；交易时间跨度必须写明」。
表里每一行的 `playbook_text` 都有漂移测试盯着它**逐字**出现在 `deploy/phase9/p9a-research-playbook-v1.json`
的 `risk_reward_standards` 里；手册那五句里唯一没有被读进表的是「Dalton 只提出研究观点和仓位建议，
人类团队决定交易」——它不是阈值，它是这个模块以人类裁决收尾的原因，测试也把这一点钉住了。

**百分比必须是有限的，而且有上界。** review 抓到的 blocker：`inf`、`1e999`、`nan` 都能原样穿过 `float()`，
而无穷大的上行满足手册表里的每一条阈值——标准检查会对一份什么都没说的回复答 `met`；`nan` 则对每条阈值
都比较为假，并在比例规则里抛出 `InvalidOperation`，而那时起草调用的钱已经花掉了。现在起草解析层与权威层
各自独立拒绝非有限值，并共用同一个 `MAX_PERCENT = 10_000` 上界——只查有限性不够，`Decimal("1e999")`
是有限的，而且照样满足每一条阈值。

**决定与提案是两张表。** 自动化能写第一张、永远写不了第二张。决定 append-only 带 `decision_number`，
绑 `proposal_hash`；`accept` / `reject` 结案，再裁一次是 conflict 而不是更正——「先接受了后来又悄悄没做」
正是这条记录要让它无法发生的历史。`defer` 不结案：它离开审批队列（deferred 的 call 每 tick 再弹一次
只会教会 owner 忽略队列），但留在 `deferred_calls()` 里，可以在财报之后再裁一次，链上看得见
「9 号推迟、26 号接受」。`defer` 也是唯一一个调用方能重试的决定（另外两个撞 conflict），
所以它带 `idempotency_key`：回包丢了再按一次不应该变成两条推迟。

**没有终态的 call（ADR-0008）。** 一家公司的 call 是一条版本链：`call_ref` 命名它、`version_number`
排序它、`prior_version_ref` 串起它，每一版带 `change_reason`（取自 `model_forecast_driver.CHANGE_REASONS`，
导入而不是重写）与**这一版比上一版多引了哪些 refs**（`change_evidence_refs`，必须真的出现在这份 call 里）。
起草 lane 只能诚实地说 `evidence_thicker`——其余几个词是关于世界的断言（filing 落地、driver 动了、人改了主意），
一条读 thesis / map / 模型再写 call 的 lane 并不知道那些。

「被取代」在这里比在预测格里窄，而且**故意是一件人做的事**：owner 接受一家公司的新 call，就取代了他之前
接受的那一条。没有任何一行被改写来记录这件事——**做出取代的那条 accept 决定带 `supersedes_ref`**，
所以标记与造成它的那个行为是同一行，旧的那条是「往前读」读出来被取代的，而不是被编辑过。
`accepted_calls(window)` 只返回还站着的那些（一份把两条都列出来的周报，等于告诉 owner 他对一家公司持两种看法），
被取代的那条留在链上，`replay(company)` 把「哪一版说了什么、人怎么裁的、被谁取代」一行一行读回来。

---

## 3. 确定性前置闸：这一片最重要的部分是**不做事**

`precheck` 完全机械，问三个问题，任一为否就不起草、不叫模型：

| 检查 | 通过条件 | 不通过的词 |
| --- | --- | --- |
| `active_thesis` | 该公司至少有一条 current thesis（含覆盖它的行业 thesis） | `no_active_thesis` |
| `divergence` | 有一条 live debate 满足 `market_position.available` 且 `our_position.state=held` 且 `our_position.side != market_position.lean`；**或**某条 metric 的 forecast-vs-consensus 差 ≥ 策略阈值（10%） | `we_agree_with_the_market` / `no_debate_map_and_no_consensus` |
| `variant_material` | 市场看法有来源：档案的 `variant_view`（`market_view_available`）、任一 debate 的 `market_position`、或 consensus | `no_variant_material` |

`divergence` 那一格有两条容易读错的细则。**`our_position.side == "neither"` 不算分歧**：P12c 用这个词表示
「我们有看法，而且看法是两边都不对」，那是一个立场但不是一个**方向**，而 call 是方向；少了这条判断，
字符串比较只要不相等就会把这家公司放进来。**`market_position.lean == "split"` 算分歧**：街上真的分裂时
我们站 bull 是一条早期 call 的常见形状，这一条是特意留下的。

**consensus 的形状在闸之前就查。** P11b 在一次按名字的查找之后，不是这一片的代码。所以 `consensus_gap()`
拿到东西之后立刻过 `validate_consensus_gap`（多余的键、空 refs、超过 12 行、不是数字的百分比都算问题），
任何一条不过就退化成诚实的 `unavailable` 带原因，而不是抛出来。理由有两个：形状不对的行进了闸，
要么开出一条谁也验证不了的 call，要么——更糟——在两次模型调用花完之后才被权威发现。
同理，只有**真正被展示给起草者的那些行**会进入记录：输入表是有界的、会丢掉没有引用的行，
一份带着起草者没见过的 metric 的 call，那个数字是没人掂量过的，而在记录里它和被争论过的那些长得一模一样。

第二格特意分两个词。「有 map 也有 consensus，但我们跟街上站在一边」是关于这家公司的**发现**；
「根本没有 map、也没有 consensus」是管线上的缺口。周会应该能不打开 Core 就分清这两种沉默。

「不通过」也从不写进任何权威——它只出现在 lane 的 tick 摘要和子进程 summary 里。原因是 ADR-0008：
没有终态、也没有「今天不值得 call」这种版本。

---

## 4. 起草与核验

一次有界调用（purpose `conviction_call`，`MAX_COST_USD` 0.60、prompt ≤ 40KB），表格 prompt，六张表：
我们的 thesis、live debates（**DIVERGENT 标在第一列且排在最前**）、我们的预测 vs 街上、可挂的日历行、
公司档案自己的 variant view、手册的风险回报标准。只能引用表里的 row id（`T…` `D…` `G…` `K…` `V…` `P…`），
引到没展示过的行 → 整份拒绝（`refused`），不做局部修补。

拒绝路径按发生顺序：`refused`（出表）→ `no_variant_view`（模型自己说市场看法无从确立，一次调用收工）
→ `rubric_failed`（机械 rubric 不过，例如做空写了 6–12 个月，第二次调用不花）→ `not_independent`
（producer 与 verifier 同 family，或任一 family 读不出来——fail closed）→ `verifier_rejected`。
verifier 只出判决，八个封闭 finding code，其中 `this_is_not_a_disagreement` 是它存在的主要理由：
方向不同但理由是市场已经在讨论的同一套论据，机械检查抓不到，只能靠读。

独立性谓词 `independent` 与路由族读取 `route_family` 直接从 `debate_map_draft` 导入而不是复制：
两份「verifier 跑在别处」的实现，是其中一份悄悄不再成立的方式。

---

## 5. Lane

`dispatch_conviction_call`，order 141（crowd-source feed 140 之后、research task 150 之前——
在这周的 sales note 与推特被读完之前写的 call，是关于上周市场看法的 call），driver key `conviction_call`，
预算池取默认 `coverage`（lane 与 purpose 两端都是），无队列。

每 tick：过闸的公司里，取第一个 `precheck` 通过、且没有同 fingerprint 的既有提案、且本周还没有 call、
且不在进程内 hold 里的；一次一个子进程，下一 tick 结算。

两条不同的规则：**幂等**按 (company, evidence fingerprint)——同样的材料不产生第二个意见；
**周配额**按 (company, ISO 周) 且**在权威里按已存的 `week_key` 列判**，不在协调器内存里——
协调器的内存活不过重启，而重启正是会在周一早上产生第二条 call 的那件事。

---

## 6. Q1 `conviction_call` rubric

七条标准：`variant_view_is_variant`、`pathway_is_observable`、`consensus_gap_named`、
`risk_reward_against_the_standard`、`falsifiers_bound_to_a_thesis`、`horizon_matches_the_direction`、
`not_a_paraphrase_of_the_price`（纯判官层）。既有四份 rubric 的 body 与哈希一个字节没动。

**它的确定性一半故意不在 `research_quality_score.CHECKS` 里。** 那个注册表读的是渲染出来的通用制品
（章节、数字、claim refs），而关于一条 call 值得机械问的每个问题都是关于**记录**的：市场看法有没有来源、
路径每一步有没有引用、证伪挂没挂在这份 call 引用的 thesis 上、做空的跨度对不对。所以机械半边是
`conviction_call.rubric_findings`，在提案写下之前跑，失败的 call 根本不会被放到人面前；
rubric 的 grading notes 里写明了它在哪。`research_quality_score.py` 是共享文件，本片不碰它。

5 例 golden 全部经 `conviction_call_artefact` 从手写的 ConvictionCallProposal 记录渲染，
确定性期望为空（这份 rubric 不命名共享 check），分数区间按现有惯例标为 calibration。
一例正例、一例「与市场同向同理由」（这份标准存在的理由）、一例「真分歧配不可观测路径」、
一例「诚实但不达标的做空」（证明 `not_met` 不是低分）、一例「证伪条件挂对了但不可观测」
（机械半边的能力边界）。

---

## 7. 只读实测（live Core 只读副本 → `/tmp`，2026-09-09）

`cp /private/tmp/dalton-ro/core.sqlite /tmp/p15d-state/core.sqlite`，跑 `precheck`：

```
mission coverage-mission-version:us-it-services:13
may_write has conviction_call: False
checkpoints has conviction_call: False
screen-passed: ACN, EPAM, IBM, DXC

company                            passed theses debates dossier consensus     eligible  reasons
company:sec-cik:0001467373 (ACN)   True   2      0       n       unavailable   False     no_debate_map_and_no_consensus, no_variant_material
company:sec-cik:0001058290 (CTSH)  False  1      0       n       unavailable   False     no_debate_map_and_no_consensus, no_variant_material
company:sec-cik:0001352010 (EPAM)  True   1      0       n       unavailable   False     no_debate_map_and_no_consensus, no_variant_material
company:sec-cik:0000051143 (IBM)   True   1      0       n       unavailable   False     no_debate_map_and_no_consensus, no_variant_material
company:sec-cik:001688568  (DXC)   True   1      0       n       unavailable   False     no_debate_map_and_no_consensus, no_variant_material
```

**今天没有任何一家能过闸，如预期。** 缺的是三样，按重要性排：

1. **DebateMap 一张都没有。** live Core 上 `debate_map_versions` 表根本不存在——P12c 的权威已并进 main，
   但这台 Core 上 lane 从没跑过、也没发布过任何 map。这是最关键的一块：没有 `market_position`，
   就既没有「分歧」也没有 variant view 的素材，两条 gate 同时不过。
2. **consensus 权威不存在。** P11b 是 Wave 2，`w2-consensus` 分支上目前**一行代码都没有**
   （落后 main 108 个提交、领先 0；`git ls-tree -r w2-consensus` 里没有任何 consensus 模块）。本片按名字延迟解析 `dalton_core.consensus_estimate.latest_consensus`，
   解析不到就如实报 `unavailable` 并把理由写进 prompt 与记录——一个悄悄消失的预期差小节读起来会像
   「我们与街上一致」，那是一条 conviction call 最不能不小心说出口的话。
3. **公司档案一份都没有。** `company_dossier_versions` 也不存在，所以 P12a 的 `variant_view` 这条素材也是空的。
   `valuation_snapshot_versions` 与 `catalyst_calendar_versions` 同样不存在（路径都有表存在性守卫，
   读一台没有这些表的 Core 不会留下任何 schema——有测试）。

（并入 main `56cf508` 之后重跑，结论不变。）

thesis 是有的：ACN 两条（一条公司级、一条覆盖它的行业级），其余四家各一条。所以三条闸里
`active_thesis` 已经满足，卡住的是后两条，而后两条要等 DebateMap 与 consensus 在 live Core 上真的有东西。

另外：live mission v13 **既没有授予 `conviction_call` 写入范围，也没有 `conviction_call` 检查点**，
所以就算过了闸，子进程也会在花钱之前 `held`（两个 gate 分别报 `not_authorized` / `no_checkpoint`）。

---

## 8. owner 需要做的两步

**第一步：发一个 mission 版本，同时给写入范围和检查点。** 两个都要——只给范围会让自动化提出一条
没人答应裁决的提案，子进程会拒绝起草并报 `no_checkpoint`。

```
autonomy.may_write          += "conviction_call"
autonomy.human_checkpoints  += "conviction_call"
```

（词表在 Wave 0 就加齐了，`coverage_mission.AUTOMATION_WRITE_SCOPES` 与 `CHECKPOINT_KINDS` 都已含这个词，
契约 JSON 也已含，不需要改代码。）

lane 要真的跑起来还需要 `initial-screen-model-config.json` 在 state 目录里（`argv_fragment` 的门槛），
以及 DebateMap 在 live Core 上先有东西。

**第二步：裁决一条 call。** 提案会出现在 Cockpit 的「待办」里（标题「是否采纳这条投资 call：做多」，
下面是我们的看法 / 市场的看法 / 市场错在哪 / 时间跨度 / 风险回报是否达标 / 可观察信号）。
卡片上**没有按钮**——写者操作已经在了，Cockpit 自己的 decide 分支与三个按钮是集成活（见第 9 节）。
今天裁决走写者：

```
operation: decide_conviction_call
params:    {"proposal_ref": "conviction-call-proposal:…",
            "proposal_hash": "<卡片上的 hash>",
            "decision": "accept" | "reject" | "defer",
            "reason": "一句话，会进正式记录",
            "idempotency_key": "<可选；重试同一个决定时带同一个键>"}
```

`actor_ref` 由写者按已认证的 principal 绑定，不由调用方给；非 `human:` 的 actor 被拒三次
（操作集合、权威、schema CHECK）。

---

## 9. 集成待办（本片不碰的共享文件）

1. **`cockpit_plane.decide` 的 `conviction_call` 分支 + `cockpit_control.html` 的三个按钮**（INT2）。
   按 INT1 的规矩，今天卡片没有按钮而不是有一按就报错的按钮。三个决定是 accept / reject / defer，
   都要求写理由（权威层强制）。
2. **`budget_pools.LANE_POOLS` / `PURPOSE_POOLS` 的显式行**：`dispatch_conviction_call` → `coverage`、
   purpose `conviction_call` → `coverage`。两端现在都靠默认值落在 coverage，行为已经正确；显式行属于 C2 的表。
3. **`research_quality_cli.py` 的 `--rubric conviction_call`**：alias 加进去之后它成了合法选项，
   但 `run_score` 会走 `artefact_from_deliverable`——一条 call 不是 deliverable。需要一个
   `conviction_call` 分支去调 `conviction_call.conviction_call_artefact`。今天不影响任何测试，
   只影响手敲这个选项的人。
4. **`macos_launchagent` / `install.sh`**：lane 的 argv 片段已经在 `LaneSpec.argv_fragment` 里，
   registry 会自动拼；不需要额外改动，但装机时 state 目录要有 `initial-screen-model-config.json`。
5. **P15c 周报**：`accepted_calls((since, until))` 已经可读，窗口按**裁决时间**而不是起草时间——
   周报问的是「这周我们决定了什么」。

---

## 9.5 review 之后改了什么（2026-09-09 晚）

| 项 | 改动 |
| --- | --- |
| **BLOCKER：非有限百分比** | `conviction_call_draft._percent` 与 `conviction_call._decimal` 各自独立拒绝 `inf` / `-inf` / `nan`，并共用 `MAX_PERCENT = 10_000` 上界（只查有限性不够：`Decimal("1e999")` 是有限的）。`inf` / `nan` / `1e999` 三个值在起草层、权威层、consensus 层各有测试 |
| **`neither` 不是分歧** | `divergent_debates` 要求 `our_position.side ∈ {bull, bear}`；`split` 的市场仍然算分歧，两条都写进 docstring 并各有测试 |
| **consensus 形状** | `consensus_gap()` 在闸之前、任何花费之前过 `validate_consensus_gap`，多余键 / 空 refs / 超 12 行 / 非数字百分比都退化成 `unavailable` 带原因；输入表只把**展示过的**行带进记录，一份 available 但每行都没引用的 gap 变成诚实的 unavailable |
| **手册漂移测试** | 每条标准的 `playbook_text` 必须逐字出现在 `deploy/phase9/p9a-research-playbook-v1.json`；另有一条测试钉住「唯一没被读进表的那句是没有阈值的那句」 |
| **ADR-0008** | 提案按公司版本化（`version` / `prior_version_ref` / `change_reason` / `change_evidence_refs`）；accept 带 `supersedes_ref` 取代上一条被接受的 call；`accepted_calls(window)` 只返回未被取代的；新增 `current` / `versions` / `superseded_by` / `replay` 读取面 |
| 部署 | 新 schema 在 `scripts/rehearse_deploy.CORE_MIGRATIONS` 里登记（main 合进来的检查要求每个 `*_schema.sql` 有具名 owner，否则演练会把一次从没跑过这条迁移的部署报成干净的） |
| nits | 模型配置没有 `model_router_db` 时 fail closed（family 不可知 → 独立性判定拒绝）；周配额同时是 `UNIQUE(company_ref, week_key)` 约束；`decide` 接受 `idempotency_key`（写者 op 也开了这个字段） |

合并 main `56cf508` 时两处冲突，都是「两边各加了一条相邻的条目」，取双方：`writer_server` 里
`decide_conviction_call` 与 P14b/P14d 的 `decide_thesis_revision_candidate` / `decide_gate_reopen` 并存；
`cockpit_plane` 里本片的 call 卡片与 INT2 的 `_revision_checkpoints()` 并存。两边都选择了「先列出来、
不给按钮」，理由也一样。

---

## 10. 开放问题（留给 owner / 主 agent）

1. **`avoid` 方向要不要占周配额？** 现在占。理由是「不值得碰」也是一条要人读的结论；
   但如果实际用起来 `avoid` 挤掉了真正的多空 call，配额应该分开。
2. **风险回报标准表的阅读对不对？** 三处是我的判断而不是手册的原文：3–5x 取下界 200%；
   「回报必须补偿波动」读成 reward/risk ≥ 2；「周期反转 + 催化型」同时覆盖 1–3 年与 3–6 个月的做多。
   每一行都带了 `playbook_text`，改这三个数只要改 `deploy/phase9/p15d-conviction-policy-v1.json`
   与模块里的常量（有漂移测试盯着两者一致）。
3. **预期差阈值 10% 是拍的。** 一条 revenue 线上 10% 是真分歧、2% 是两个人的模型差异——这个判断没有数据支撑，
   等 P11b 真的有 consensus 之后应该按实际分布重标。
4. **`defer` 要不要带一个「什么时候再问我」？** 重试已经安全了（`idempotency_key`），但 defer 之后
   call 仍然静默地待在 `deferred_calls()` 里，除非有人主动去看。一个 `defer_until` 日期
   （多半就是下一个 catalyst 的日期）会让它自己回到队列，但那是一条新的调度规则，需要 owner 点头。
5. **~~同一家公司同时存在 long 与 short 的 call 怎么办？~~** review 之后做掉了：版本链 + 接受即取代，
   `accepted_calls` 只返回还站着的那条。留下的余数是**这一条**——取代只在人接受新 call 时发生，
   所以一条被接受、然后公司基本面明显变了、但没有人再提新 call 的旧 call，会一直站着。
   要不要给 accepted call 一个「过期」判据（比如它的 `time_horizon` 走完、或者它的证伪条件被触发），
   是一条 owner 该定的规则，本片没有定。
6. **`market_view.sources` 里的 `crowd_narrative` 该不该单独成立一条 call？** 现在可以：
   只要 sources 非空即可。按证据阶梯，只靠雪球和推特确立的「市场看法」大概不该支撑一条 call，
   但把它写死成规则之前想听 owner 的意见。
