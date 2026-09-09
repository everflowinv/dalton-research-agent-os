# P12a / P12f：公司档案十节，结构由 Constitution 定，没有新证据的一版不是一版

*2026-09-09* · 分支 `w2-dossier`，基于 main `7708d43`（2,843 项）
· 依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 1 节（版本化是机制不是触发器）、
第 3 节 Wave 2、vision 回顾补充行 **C3**（Constitution `method` 接消费者）与 **D2**（认知层产出复用
independence predicate）、[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md)
5.2 Phase 12（P12a / P12f / 验收）、[ADR-0008](../adr/0008-research-outputs-are-never-terminal.md)、
[P12b Claim 索引](p12b-claim-index-v1.0-2026-09-09.md)、[Q1 研究质量回路](q1-research-quality-loop-v1.0-2026-09-09.md)

---

## 0. 一句话

Ledger 里 2,170 条 Claim 的出处都无可挑剔，但没有人能从中读出一家公司。这一片把「按 aspect 分组」变成
一份可以读的档案：十节名字与 P12b 的封闭词表逐字一致，`demand_drivers` 与 `supply_and_cost` 两节的**结构
由 Constitution 的 `causal_chain` 派生**（模型只填槽，永远不发明结构），每一句话自带 refs 而正文里一个标签都
没有，发布前过 Q1 的 `company-dossier` 评分表与 Constitution 自己的 `output_rubric`，由**不同 model_family**
的复核给出 verdict，最后由 ADR-0008 决定这到底算不算一个新版本。

## 1. `CompanyDossierVersion` 的形状

`company_dossier.py` + `company_dossier_schema.sql`。一条链一家公司，append-only、`content_hash`、
三个 `dalton_company_dossier_authorized()` 触发器（insert 需权威、update/delete 一律 ABORT）、写后读回校验，
照 `statement_snapshot.py` 与 Wave 1C 的 `ForecastModelAuthority` 的样子。**没有 pointer 表**：「当前那一版」
是链上版本号最大的那条，派生而不是存储——存一份 pointer 就是给一个已有答案的问题造第二个答案。

| 字段 | 说明 |
| --- | --- |
| `sections[10]` | 顺序与名字**逐字**等于 `claim_aspect_vocabulary.ASPECTS[:10]`。每节：`status ∈ {drafted, unavailable}`、`reason`（封闭词表）、`structure`（槽 id 列表）、`slots`、`sources`、`gaps`、`profile`（只有 `guidance_style` 有） |
| `industry_classification` | Deep Insight Gate 第 1 问：`commodity_cycle` / `capital_cycle` / `contract_compounder` / `structural_growth` / `turnaround` / `insufficient_evidence`，加两个槽 `basis` 与 `counterexample`（Playbook 原文要的「分类依据和反例」） |
| `variant_view` | `our_view` / `market_view` / `where_market_is_wrong` / `convergence_pathway` / `observable_signals`。没有共识、评级、sales note、大众叙事材料时，`market_view` **根本不是一个槽**，块上记 `market_view_available: false` 与理由 |
| `change_reason` + `evidence_refs` + `decision` | ADR-0008 的五词词表，**直接 import 自 `model_forecast_driver.CHANGE_REASONS`**——同一份合同不该有两份封闭列表 |
| `bindings` | constitution（ref+hash）、playbook、mission 版本、policy ref+hash、`causal_chain_hash`、rubric ref+hash |
| `body_hash` / `content_hash` | body 排除 id / 时间 / 版本号 / change_reason / evidence_refs / decision，所以「这一版说了什么」与「为什么写它」是两件可以分别比对的事 |

### 句子是行，不是散文里的标签

**每一句话自带 refs，而正文里一个 C / N 标签都没有。** 模型返回的是
`{"slot_id": ..., "sentences": [{"text": "...", "refs": ["C3","N1"]}]}`，`body` 由代码拼出来。
P13ap 从已发布的 Initial Screen 上读到的教训是：把标签当词写进正文的句子活不过剥离——live 的 S7 里
`：显示2026-03-01…` 与 `和指出美国联邦支出削减…` 都是主语跟着标签走了。行式句子让「逐句可回指」与
「正文干净」不再冲突：标签从来没进过正文，所以没有东西需要被剥离。

三种 ref：`claim`（P12b 的 canonical Claim）、`figure`（已入库报表行 `statement-line:<line_id>`）、
`forecast_cell`（Wave 1C 驱动模型的格 ref）。**不用 ModelInputTable 的格**：那张投影按需派生、不存储，
指向它的 ref 命名的是一个不存在的东西；它背后那条 filing 行存在，而且那才是数字真正的证据。

### 发布规则（ADR-0008）

`publish(body)` 有两种 `duplicate`，而且它们是不同的发现：

- `identical_body`——链上那一版逐字节就是它；
- `no_new_evidence`——正文变了，但这一版依赖的 ref 集合是当前版的子集。**这是 ADR-0008 真正要挡的那一种**：
  说不出自己学到了什么的改写，是套着版本号的噪音。

另外：`change_reason` 必须是五词之一、`evidence_refs` 不能为空、`computed_from_version_ref` 与链头不符时
拒绝（丢失更新——否则新版会带着调用方的 `change_reason` 撤销别人刚做的改动）。
`revise()` 是 ADR-0008 要求的入口点，**权威自己永远不调用它**。

### 填不了的节是 `unavailable`，不是被填满

封闭理由：`no_canonical_claims` / `no_market_data` / `no_catalyst_calendar_authority` /
`causal_chain_unmapped` / `refused_by_verification` / `not_drafted_this_run`。
`catalyst_calendar`（C1 未落地）与 `history_of_price_drivers`（P11a 的价格序列不在这个 Core 上）
就是蓝图已经知道的两个洞，它们如实说出缺的是**哪个权威**，而不是「暂无数据」。

一节只要每个槽都是 `unknown`，权威直接拒绝：那是 `unavailable`，不是 `drafted`。

## 2. 结构从 Constitution 来（计划 C3）

`method.causal_chain` 是一个有序的散文列表。`demand_drivers` 与 `supply_and_cost` 的槽 =
**映射到这一节的那些链环**，槽的 prompt 就是链环原文。映射写在
`deploy/phase9/p12a-dossier-policy-v1.json` 里，按 **整条链的哈希** 绑定：

```
constitution:us-it-services  chain hash b1781893…  6 links
  [demand_drivers, demand_drivers, demand_drivers, demand_drivers,
   supply_and_cost, supply_and_cost]
```

链多一环、少一环、换个顺序，哈希就对不上，那两节立刻变成 `causal_chain_unmapped` 的 `unavailable`，
直到有人重新映射。**这是有意的**：一条新链环属于哪一节是方法论问题，靠关键词猜的规则会把 Constitution
的形状悄悄改写成关键词的形状。live 的 `constitution-version:us-it-services:9` 的链哈希与随片交付的 policy
里的那一条**相符**（冒烟已验证），所以集成后这两节直接可用。

`output_rubric` 同样接了消费者：policy 给**每一条** criterion 绑一个确定性检查，或显式声明
`check: null` 加理由。没有被提到的 criterion 本身就是一条 finding（`unmapped_output_rubric_criterion`）——
只读自己看得懂的那几条，读的就不是这份标准。当前绑定：

| criterion | 处置 |
| --- | --- |
| 周报应当写什么 | `null`：档案不是周报，没有可读的结构 |
| 每个数字都回指 EvidenceVersion | `numbers_trace_to_refs`（正文数字必须逐字出现在它引用的行里） |
| 产出永不自动生成投资结论 | `no_investment_conclusion`（买入 / 卖出 / 目标价 / 低估…出现即 finding） |
| 好的产出缩小未决问题集合，坏的复述已知事实 | `not_a_restatement`（新版必须引新证据） |

## 3. 起草与核验（`company_dossier_draft.py`）

每个 unit 一次有界 `CockpitModel` 调用，purpose **`dossier`**（模块 import 时
`register_purpose("dossier")`，没碰 `cockpit_model.py`）。界限沿用 `company_model_cli` 的量级：
`MAX_INPUT_TOKENS=120_000`、`MAX_OUTPUT_TOKENS=3_000`、`MAX_COST_USD=0.60`、`TIMEOUT_SECONDS=180`，
一次 tick 全部调用（含复核）上限 `MAX_RUN_COST_USD=2.50`、`MAX_UNITS_PER_RUN=3`。

**prompt 是表，不是对象图**（`company_model_spec` 与 `claim_index_tagging` 的同一条教训：同样内容用对象
编码要多几倍字节，而路由按 prompt 字节预留预算）：

```
Part: demand_drivers -- what makes customers spend more or less …
Structure. Fill every slot below, in this order, and invent none:
  causal_chain:0	Enterprise IT budgets react to macro confidence with a one-to-two-quarter lag.
  causal_chain:1	New bookings lead revenue by two to four quarters.
Hard rules: …（禁止正文写标签 / 每句必须引用 / 数字逐字抄 / 槽答不出写 unknown / 不要复述上一版）
Statements available (n) -- tag, period, source grade, text:
C1	2026Q2	management_statement	…
Figures available (n) -- tag, period, kind, text:
N1	2025-09-01..2026-05-31	figure	…
```

Claim 来自 `query_company_research(index_aspect=…, canonical_only=True)`，按 P12b 的 importance 排序
（一手 filing → 管理层原话 → 卖方 → 新闻）并截断。上一版该节的正文也在 prompt 里，正是为了让新版推进
而不是复述。

**越界整批拒绝，永不修补**：没展示过的标签、结构里没有的槽、少一个槽、超过句数上限、一句话零引用、
合同里没有的键、JSON 外面夹散文——任何一条都拒绝整个 unit。理由与 P12b 相同：凭空造出标签的回复说明
它不是在读那张表，那么它碰巧答对的部分也是同一个回复的产物。

### 独立复核（计划 D2）

一次 verdict-only 的第二调用，看到每一句话与它引用的行，只回答「每句话是否留在它引用的材料里」。
verdict ∈ {pass, reject}，finding code 四个封闭值（`unsupported_sentence`、`number_not_in_source`、
`structure_deviation`、`conclusion_beyond_evidence`），pass 不许有 finding、reject 必须有——
与 `thesis_impact` 的 verifier 同一条规则。verdict 绑 `verified_draft_hash`：不指名读了哪一份草稿的
verdict 是关于虚无的 verdict。

独立性**事后从 route decision 读**（`ModelRouter.get_decision → get_profile.family`），因为「谁答的」
是路由的事实而不是我们请求的事实；复核的 family 只要等于任何一次起草调用的 family，或者任何一个 family
解析不出来，就**失败关闭**（`dossier_status: not_independent`，不发布）。独立性未知的核验，价值等于没有核验。

### 发布前五道闸

草稿要成为一版，要过：(1) 复核 `pass`；(2) 独立性成立；(3) 每条 ref 在拥有它的权威里解析得开
（Claim 在 `claim_versions` 且未撤回、报表行在 `coverage_mission_statement_lines`、forecast 格在当前
模型版本里）；(4) Q1 `company-dossier` 评分表的确定性层，硬检查
`numbers_without_refs` 与 `new_version_cites_new_refs` 任一失败即拒；(5) Constitution 的 `output_rubric`
零 finding。然后才轮到 ADR-0008 判断这是不是一版。

## 4. P12f 管理层与 guidance 档案（`guidance_profile.py`）

**确定性优先，模型只写表格周围的散文。**

- `guidance_events(guides, actuals)`：guides 是索引打了 `guidance_style` 的 Claim，actuals 是同期同指标的
  已结算数字。区间由**具名规则**读出（`between_x_and_y` / `x_to_y` / `x_dash_y` / `point`），每个事件带
  `guide_basis` 说明是哪条规则读的——没有规则读得出来的句子进 `unparsed`，不猜。
- **口径不同不比较**：百分比的指引与美元的实际值不是同一种测量，`deviation.verdict` 记 `unknown` 并写明理由。
- `classify(events)`：规则原文存在每一份 profile 上（`rule` + `rule_hash`）：

  > 结算事件 = 有指引区间且有同口径实际值。少于 4 条 → `insufficient_data`。否则：beats ≥ 75% 且连续两次
  > 指引中至少一半被上调 → `beat_and_raise`；beats ≥ 75% 而没有上调模式 → `conservative`；
  > misses ≥ 50% → `aggressive`；其余 → `insufficient_data`（没有任何模式达到阈值）。

  三个季度的连胜是一段运气，不是一种风格；`insufficient_data` 是真答案，而且今天是最常见的正确答案。
- profile 存在 `guidance_style` 那一节的 `profile` 字段里，`render_profile_table()` 把它渲染成
  tab 表贴进 prompt，并明说「这张表已经算好，不是你的活；描述它，不要重算」。

## 5. Lane（`mission_dossier_lane.py` + `company_dossier_cli.py` + `company_dossier_launcher.py`）

- `LaneSpec(operation="dispatch_company_dossier", order=135, driver_key="company_dossier")`，
  `LANE_MODULES` 加一行。没有碰 `writer_server` / `coverage_mission` / `bounded_planner_driver` /
  `macos_launchagent` / `install.sh` / cockpit。
- **无队列**：要做什么每 tick 从 Ledger 派生。协调器不做规划（规划全在子进程里），它只保存一个
  ledger signature（Claim 条数 + 最新一条 + 各档案链头 + 索引条数），上一轮报「无事可做」而 signature
  没变就不再起子进程。model-spec lane 的教训是：会重新推导子进程选择的协调器早晚会和它意见不一致，
  然后永远把同一家公司递回去；signature 不会有意见。
- **只对已过 Initial Screen 闸的公司起草**（`stage_records` 里 `initial_screen` = `gate_passed`）。
- **陈旧度按 Claim 的 `created_at` 判，不按「有材料没被引用」判**：一节有十条 Claim 而草稿引了三条，
  并不是留下七件没做的事；那样判会让这一节每 tick 都被重画，然后每次都发布一个 duplicate。
  从没写过的 unit 永远算陈旧。
- `--revise <unit>` 是另一扇门：人可以要求重写某一节，**但规则不因此暂停**——没有新证据的重写照样被
  `new_version_cites_new_refs` 拦下（测试里就是这样）。
- 授权词是 `dossier`（Wave 0 已加进 `AUTOMATION_WRITE_SCOPES`），**没有 fallback**：借 `deliverable`
  来写一个 owner 从没授权过的权威，是拿一个意思去用另一个意思。

## 6. 验收结果（原文）

```
Ran 2950 tests in 292.233s
OK (skipped=1)
```

命令：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`
（`PYTHONPATH` 必须是**绝对路径**：子进程 cwd 是 state dir，相对的 `src` 会解析不到。）
基线 main `7708d43` 是 2,843，本片新增 **107** 项，分布：
`test_company_dossier.py` 28、`test_company_dossier_draft.py` 27、`test_guidance_profile.py` 18、
`test_dossier_lane.py` 34。全部用假模型，没有对 live 预算发起过任何模型调用。

### 冒烟：live 只读副本（`/tmp` 拷贝，规则跑，不调模型）

副本早于 Claim 索引，所以先在副本上跑 P12b 的规则半边（`pending 2170 → rule-settled 22`，2.6s），
然后问：ACN 的每一节会拿到多少条 canonical Claim，哪些节是 `unavailable`。

```
mission coverage-mission-version:us-it-services:13  grants dossier: False
constitution constitution-version:us-it-services:9  causal chain links: 6  mapped by the shipped policy: yes
authorities on this Core: statement_lines yes | catalyst_calendar no | market_price no | forecast_model no
figure rows offered: 30   market-view rows: 0

unit                                 claims  figures  slots  status
business_model                            0        0      1  no_canonical_claims
segments_and_mix                          4       30      1  ready
demand_drivers                            0        0      4  no_canonical_claims
supply_and_cost                           0        0      2  no_canonical_claims
competitive_position                      0        0      1  no_canonical_claims
management_and_capital_allocation         0        0      1  no_canonical_claims
guidance_style                            0        0      1  no_canonical_claims
kpi_dictionary                            0        0      1  no_canonical_claims
catalyst_calendar                         0        0      1  no_catalyst_calendar_authority
history_of_price_drivers                  0        0      1  no_market_data
industry_classification                   4        0      2  ready
variant_view                              0        0      4  no_canonical_claims

guidance profile: insufficient_data | 0 settled events; the rule needs 4 before it will call a style
ACN claim versions still unindexed (awaiting P12b's model tagger): 483
```

**这张表最重要的一行是最后一行。** 今天十节里只有一节有材料，不是因为 Ledger 空，而是因为
P12b 的 aspect 标注只有规则那一半跑过（规则只settle定量 Claim），ACN 的 483 条 Claim 还在等模型标注器。
档案的可用性**完全取决于索引先被填满**——这是集成顺序上的硬依赖，不是这一片能自己解决的事。

另外两件冒烟发现并当场修掉的：报表行没有 level 0（根行在 1–3 级），第一版的
`level=0` 过滤让 filing 数字一条都取不到；以及同一个 `period_end` 下 YTD 与季度用同一个 label
并排存在，所以 figure 的文本与去重键都带上了区间起点——「本季 18.7bn」与「年初至今 37.7bn」
不是同一件事，读者分不开就会把第二个读成对第一个的重述。

## 7. 集成要接的线

1. **mission 授权**：live `coverage-mission-version:us-it-services:13` 的 `may_write` 里**没有** `dossier`。
   词已经在词表里（Wave 0 加的），owner 发一版新 mission 授予即可，代码不用改。在那之前 lane 每 tick
   报 `held / not_authorized` 且**一分钱不花**（授权检查在任何模型调用之前）。
2. **P12b 模型标注器要先跑完**（见第 6 节）。补完 2,148 条存量是 P12b 报告第七节的开放问题；
   在它跑完之前，档案 lane 会诚实地只写有材料的那一两节。
3. **policy 文件要随部署落盘**：`deploy/phase9/p12a-dossier-policy-v1.json` 的默认路径只在源码 checkout 里
   成立；安装环境要用 `--company-dossier-policy` 指到它，否则 lane 报 `held / no_policy`（不会崩，也不会花钱）。
4. **install.sh / launchagent**：lane 的 argv 片段是 `--company-dossier-model-config <state>/initial-screen-model-config.json`，
   与 Initial Screen 复用同一份起草模型配置（同一条路由、同一个 broker、同一本日账），
   所以 `scripts/raise_day_budget_cap.MODEL_CONFIG_NAMES` 不用加新名字。
   argv 片段已经按 registry 的形状写好，`macos_launchagent` 无需改动。
5. **cockpit**：需要一个公司档案页——十节、每节可点回 Claim / filing 行 / forecast 格、
   版本链可回放（`CompanyDossierAuthority.replay_section(company, aspect)` 已经就是这个读法：
   每一版说了什么、`change_reason` 是什么、引了哪些 ref）。
   `writer_server.OPERATION_FIELDS` 不需要新参数：lane 的 tick 不带参数。
6. **Q1 的 `DOSSIER_SECTIONS` 与 aspect 词表对齐**（Q1 报告 1.3 已经点名的那件事）。
   本片没有改 `research_quality_rubrics.py`（不是我的文件），而是在
   `company_dossier.dossier_artefact()` 里直接传 aspect 名作 `expected_sections`。
   集成时把 `DOSSIER_SECTIONS` 换成 `claim_aspect_vocabulary.ASPECTS[:10]` 并升一版 rubric，
   本片这条注释就可以删掉。
7. **Q1 的 `claim_refs_resolve` 只解析 Claim。** 档案还引用报表行与 forecast 格，本片自己有
   `unresolved_refs()` 逐种检查；如果 Q1 那条检查将来认得另外两种 ref，这一层可以退休。

## 8. 没做的

- **没有对 live 调过任何模型**，所以没有一份真实的档案存在；测试与冒烟全是假模型 / 只读副本。
- **没有 cockpit 页面**（见上）。
- **`ResearchTask` / 事件流的接线没做**：`revise()` 与 `--revise` 是入口，Wave 3 的 P14a 判断层
  接上去时，把五词决定写进 `decision` 字段即可（字段已经在契约里，且 `driver_event` 之外不强制）。
- **`market_view` 今天几乎永远缺席**：P11b 的 consensus 与 S1 的 sales note 落地后，
  `market_view_material()` 的两扇门（sell-side 级 Claim、`history_of_price_drivers` aspect）会自动有料，
  不用改代码。
- **guidance 的区间解析没有在真实电话会 Claim 上验证过**：live 副本上 ACN 的 `guidance_style`
  aspect 一条都还没标（同第 6 节的原因），所以四条规则只在构造的例子上跑过。

## 9. 开放问题（需要 owner 或主 agent 定）

1. **FTS 漏检率还测不出来，但可以先看一个数。** 计划要的是「aspect 索引供给了多少条被章节标题关键词检索
   漏掉的 Claim」，用来决定要不要上 embedding。索引今天只有 22 条规则标注，分子分母都不成立。
   能先给的是分母那一半——对 ACN 的 489 条 Claim 做关键词检索会捞回多少：

   | 章节 | 标题词命中 | 定义词命中 |
   | --- | --- | --- |
   | business_model | 98 | 175 |
   | segments_and_mix | 23 | 232 |
   | demand_drivers | 178 | 271 |
   | supply_and_cost | 32 | 85 |
   | competitive_position | 93 | 219 |
   | management_and_capital_allocation | 110 | 208 |
   | guidance_style | 30 | 280 |
   | kpi_dictionary | **0** | 141 |
   | catalyst_calendar | 3 | 147 |
   | history_of_price_drivers | 26 | 133 |

   读法：定义词检索会把最多 57% 的全部 Claim 判给单独一节——那不是过滤器；标题词检索在
   `kpi_dictionary` 上是 0，在 `demand_drivers` 上是 178（36%）。两头都不能用。
   **真正的漏检率要等模型标注器跑完再测**，方法是现成的：对每一节比较索引供给集与关键词集的差集。
   在那之前不要动 embedding 这个冻结项。
2. **`guidance_style` 的第五个词。** 封闭词表是 owner 给的四个词，于是「结算够多但没有模式」只能落进
   `insufficient_data`，与「样本太少」共用一个词（`basis` 里写清了是哪一种）。建议加一个 `mixed`；
   这是词表变更，需要 owner 点头。
3. **`unavailable` 的节要不要计入 cockpit 的完成度？** 建议要，而且要显示理由——
   「十节里有两节在等 C1 与 P11a」本身就是给 owner 看的排期信息。
4. **档案版本要不要成为 `mission_deliverable` 的一种 kind？** 现在不是（它有自己的权威）。
   周报与 memo 将来要引用档案时，引的是 `company-dossier-version:<company>:<n>`，
   这一点最好在 P15 之前定下来。
