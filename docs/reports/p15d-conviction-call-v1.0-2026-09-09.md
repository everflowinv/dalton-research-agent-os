# P15d 高 conviction call：自动化提案，人裁决 v1.0

日期：2026-09-09
分支：`w3-conviction-call`（worktree `~/Projects/dalton-w3-conviction-call-worktree`），基线 main `ebd2ea8`
作者：Wave 3 agent「conviction-call」（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) §1 owner 自我反思规则与 §5.2 P15d、[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) §3 ②「high conviction 超预期机会」与 §5.2 P15d、ADR-0006 / ADR-0007 / ADR-0008、[P12c DebateMap](p12c-debate-map-v1.0-2026-09-09.md)、[P12a 公司档案](p12a-company-dossier-v1.0-2026-09-09.md)、[P14a 每日跟踪](p14a-daily-tracking-v1.0-2026-09-09.md)、[P13-M2 预测行](p13-m2-forecast-lines-v1.0-2026-09-09.md)、[P11a 市场层](p11a-market-layer-v1.0-2026-09-09.md)、[Q1 质量回路](q1-research-quality-loop-v1.0-2026-09-09.md)、[INT1 Cockpit](int1-cockpit-install-v1.0-2026-09-09.md)

全量测试：

```
Ran 4061 tests in 566.589s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`；基线 main `ebd2ea8` 是 3,932，本片 +129。）

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
| `tests/test_conviction_call.py` / `_draft.py` / `_lane.py` / `_cockpit.py` | 新增 | 55 + 38 + 33 + 3 |
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

**决定与提案是两张表。** 自动化能写第一张、永远写不了第二张。决定 append-only 带 `decision_number`，
绑 `proposal_hash`；`accept` / `reject` 结案，再裁一次是 conflict 而不是更正——「先接受了后来又悄悄没做」
正是这条记录要让它无法发生的历史。`defer` 不结案：它离开审批队列（deferred 的 call 每 tick 再弹一次
只会教会 owner 忽略队列），但留在 `deferred_calls()` 里，可以在财报之后再裁一次，链上看得见
「9 号推迟、26 号接受」。

---

## 3. 确定性前置闸：这一片最重要的部分是**不做事**

`precheck` 完全机械，问三个问题，任一为否就不起草、不叫模型：

| 检查 | 通过条件 | 不通过的词 |
| --- | --- | --- |
| `active_thesis` | 该公司至少有一条 current thesis（含覆盖它的行业 thesis） | `no_active_thesis` |
| `divergence` | 有一条 live debate 满足 `market_position.available` 且 `our_position.state=held` 且 `our_position.side != market_position.lean`；**或**某条 metric 的 forecast-vs-consensus 差 ≥ 策略阈值（10%） | `we_agree_with_the_market` / `no_debate_map_and_no_consensus` |
| `variant_material` | 市场看法有来源：档案的 `variant_view`（`market_view_available`）、任一 debate 的 `market_position`、或 consensus | `no_variant_material` |

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
            "reason": "一句话，会进正式记录"}
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

## 10. 开放问题（留给 owner / 主 agent）

1. **`avoid` 方向要不要占周配额？** 现在占。理由是「不值得碰」也是一条要人读的结论；
   但如果实际用起来 `avoid` 挤掉了真正的多空 call，配额应该分开。
2. **风险回报标准表的阅读对不对？** 三处是我的判断而不是手册的原文：3–5x 取下界 200%；
   「回报必须补偿波动」读成 reward/risk ≥ 2；「周期反转 + 催化型」同时覆盖 1–3 年与 3–6 个月的做多。
   每一行都带了 `playbook_text`，改这三个数只要改 `deploy/phase9/p15d-conviction-policy-v1.json`
   与模块里的常量（有漂移测试盯着两者一致）。
3. **预期差阈值 10% 是拍的。** 一条 revenue 线上 10% 是真分歧、2% 是两个人的模型差异——这个判断没有数据支撑，
   等 P11b 真的有 consensus 之后应该按实际分布重标。
4. **`defer` 要不要带一个「什么时候再问我」？** 现在 defer 之后 call 就静默地待在 `deferred_calls()` 里，
   除非有人主动去看。一个 `defer_until` 日期（多半就是下一个 catalyst 的日期）会让它自己回到队列，
   但那是一条新的调度规则，需要 owner 点头。
5. **同一家公司同时存在 long 与 short 的 call 怎么办？** 契约不禁止（周配额 1 条实际上挡住了同周的第二条），
   但跨周是可能的，而且没有任何东西会把旧的 accepted call 标记为被取代。
   这属于 ADR-0008 的「没有终态」：也许 call 也该有版本链而不是一次性对象。本片没有做这个决定。
6. **`market_view.sources` 里的 `crowd_narrative` 该不该单独成立一条 call？** 现在可以：
   只要 sources 非空即可。按证据阶梯，只靠雪球和推特确立的「市场看法」大概不该支撑一条 call，
   但把它写死成规则之前想听 owner 的意见。
