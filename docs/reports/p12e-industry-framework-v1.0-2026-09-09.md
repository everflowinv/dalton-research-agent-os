# P12e 行业框架交付物 v1.0

日期：2026-09-09（实现完成 2026-09-10）
分支：`w2-industry-framework`，基线 main `ebd2ea8`，交付前合入 main `b1f1345`
复核：code-review 2026-09-10，四条 findings 与七条 nits 全部处理（§5.7）
蓝图条目：[能力差距分析与开发蓝图 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) §5.2 P12e、§3 ②「行业特性、长短期驱动」
计划条目：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) C3、D2

---

## 0. 一句话

`industry_framework` 从「只在 `DELIVERABLE_KINDS` 枚举里的一个词」变成一个版本化产出类
authority：分节由 Constitution 的因果链决定、长短期驱动绑定 IndustryDriverPack、五家横向对比表
**由代码算出**（收入、同比增速、毛利率、经营利润率，逐格带 filing accession），缺口清单由
`SourceCapabilityMap` 派生而不是由模型写。今天在只读副本上跑出来的结果是：**72 个格子算得出来，
88 个算不出来且每个都说明了为什么；九条缺口全部 open**——这份清单就是 S 线的输入。

---

## 1. 交付内容

| 文件 | 说明 |
| --- | --- |
| `src/dalton_core/industry_framework.py` | 记录合同、对比表计算、缺口派生、debate 投影、authority |
| `src/dalton_core/industry_framework_schema.sql` | 一条 append-only 链，比档案多一列 `comparison_hash` |
| `src/dalton_core/industry_framework_draft.py` | 三张材料表的 prompt、整体拒绝的解析、独立 verifier |
| `src/dalton_core/industry_framework_cli.py` | 子进程：先算表，再起草，再核验，最后发布 |
| `src/dalton_core/industry_framework_launcher.py` | 一次一个子进程，ticket 由证据签名命名 |
| `src/dalton_core/mission_industry_framework_lane.py` | LaneSpec，order 139，周频 |
| `deploy/phase9/p12e-industry-framework-policy-v1.json` | 因果链标题、driver 时间跨度、九条缺口清单、output_rubric 绑定 |
| `tests/test_industry_framework{,_draft,_lane}.py` | 139 项 |
| `tests/golden/industry_framework/*.json` | 六个 golden case |
| 共享文件（加法） | `lane_registry.LANE_MODULES`、`research_quality_rubrics.INDUSTRY_FRAMEWORK`、`bootstrap.SCHEMA_DATABASES`、`scripts/rehearse_deploy.py`、`cockpit_plane.REGISTRY_LANE_LABELS` 一行、`deploy/macos/install.sh` 一个 seed 块 |

---

## 2. 四个设计决定

### 2.1 分节就是因果链，一环一节（计划 C3）

`method.causal_chain` 是人发布、哈希绑定的方法论陈述，而行业框架是**整份文档的主题就是这条链**的那个
交付物。所以 section 不是模型选的，也不是本模块选的：policy 给每一环一个标题，绑定在**整条链的哈希**
上。链增加、减少或重排一环，policy 条目就不再匹配，所有 section 变成 `causal_chain_unmapped` 而不是
让模型自由发挥。这是 P12a 对 `demand_drivers` / `supply_and_cost` 两节的同一条拒绝，推广到六节。

每节两个 slot：`state`（材料对这一环说了什么）与 `divergence`（五家在这一环上哪里分歧）。一个 slot 会
让 section 去答它更容易答的那个问题。

live 链的哈希是 `b1781893…c307f454`，与 P12a policy 里的同一个值——两份 policy 读的是同一条链。

### 2.2 对比表由代码算，模型只在表的周围写字

`build_comparison` 是 ModelInputTable 的纯函数：角色概念冻结在代码里（`REVENUE_CONCEPTS` 等，按优先
顺序），公式冻结（同比 = 本季/去年同季 − 1，毛利率 = (收入 − 营业成本)/收入），每格带算它用到的
`source_accessions`，格子的名字由 `cell_ref` 派生而不是被选。让模型读五家财报自己列表，是这个仓库
从来不允许的一件事：没人能重算的表格，是一条带着表格权威的断言。

两个非平凡的结论，都写进了表自己的 `comparability_notes`：

- **五家四本日历。** ACN 财季结在 2/5/8/11 月，CTSH / EPAM / DXC 结在自然季末，IBM 在库里只有半年
  数据。所以列头是**自然季度**，每一格另外带自己的 `period_end`，注记明说「列与格最多差一个月」。
- **毛利率不可横向排序。** CTSH 与 DXC 申报的营业成本**不含折旧摊销**，ACN 与 EPAM 含。注记直接点名
  哪几家在哪一边。一张不说这件事的表，等于邀请读者把两家的列报选择读成一个发现。

`operating_margin` 这一行**整行为空**，而且是故意保留的：五家的建模规格没有一家绑定经营利润概念。
一张把这一行悄悄省掉的表，会让读者以为表里就是能看的全部。

### 2.3 缺口清单是派生的，因为它是 S 线的输入

蓝图把这份清单定为接 Guidepoint / IR / sales note 的依据。模型写的缺口清单，是模型当天觉得有意思的
那些缺口。所以：policy 的每一条 checklist 项声明它会告知哪些 driver pack driver、以及哪一类
`SourceCapabilityMap.CONTENT_KINDS`；状态由「本版有没有对这些 driver 写出带引用的句子」读出；候选来源
由 `sources_for(content_kind)` 加上任务自己的连接状态与受治理的日配额读出。

`gap:tam-and-share` 与 `gap:competitive-wins` 的 `driver_refs` 都是空的，因此**按构造永远 open**——
driver pack 里没有任何 metric 衡量 TAM，universe 之外的印度系厂商也不可能有 Claim。这是诚实的答案，
也正是让 S 线去接东西的那句话。

### 2.4 ADR-0008，比档案多一道

一版必须引用上一版没引用过的 ref，带 `change_reason`，被取代的内容不删不改。这份产出有两个褶皱，都
不是文字：

- **一份新财报落地会让表动而一个字的正文都不动**，而那确实是新证据。所以对比表的 accession 在
  `evidence_scope` 里面，表自己的哈希存在 `body_hash` 旁边。否则起草一停，这个行业就永远不再出新
  版本。
- **接上一个数据源同样会让缺口清单动而正文不动。**「Guidepoint 接上了」是这份交付物存在的全部目的所
  指的那件事，一个记不下它到达那天的系统，是一个缺口清单会悄悄过期的系统。所以 `gap_state_ref()` 把
  每条缺口的状态、每个候选来源的 slug / 连接状态 / 日配额摘成一个 `gap-state:` ref，也进
  `evidence_scope`。它刻意不摘 `what_is_missing` 与 `cost_note` 的文字：那两项来自 policy，而 policy
  一改，记录上的 `policy_hash` 本来就变了。

---

## 3. 只读冒烟（/tmp 副本，无模型调用）

只读副本 `/private/tmp/dalton-ro/core.sqlite` → `/tmp/p12e-core.sqlite`。live 任务是
`coverage-mission-version:us-it-services:13`，**已经授予 `deliverable` 写入范围**（比计划文档里那份
v1 json 新；lane 今天就是被授权的）。

### 3.1 五家横向对比表，今天渲染出来的样子

```
company	metric	2025Q1	2025Q2	2025Q3	2025Q4	2026Q1	2026Q2
ACN	revenue	16659301000	17727871000	-	18742125000	18044066000	18718144000
ACN	revenue_yoy_growth	5.4%	7.7%	-	6.0%	8.3%	5.6%
ACN	gross_margin	29.9%	32.9%	-	33.1%	30.3%	32.8%
ACN	operating_margin	-	-	-	-	-	-
CTSH	revenue	5115000000	5245000000	5415000000	-	5413000000	5481000000
CTSH	revenue_yoy_growth	7.5%	8.1%	7.4%	-	5.8%	4.5%
CTSH	gross_margin	33.6%	33.7%	33.9%	-	32.8%	33.4%
CTSH	operating_margin	-	-	-	-	-	-
EPAM	revenue	1301692000	1353443000	1394373000	-	1400061000	1414767000
EPAM	revenue_yoy_growth	11.7%	18.0%	19.4%	-	7.6%	4.5%
EPAM	gross_margin	26.9%	28.8%	29.5%	-	27.7%	30.4%
EPAM	operating_margin	-	-	-	-	-	-
IBM	revenue	-	-	-	-	-	-
IBM	revenue_yoy_growth	-	-	-	-	-	-
IBM	gross_margin	-	-	-	-	-	-
IBM	operating_margin	-	-	-	-	-	-
DXC	revenue	-	3159000000	3161000000	3194000000	-	2999000000
DXC	revenue_yoy_growth	-	-2.4%	-2.5%	-1.0%	-	-5.1%
DXC	gross_margin	-	24.4%	24.6%	23.8%	-	20.4%
DXC	operating_margin	-	-	-	-	-	-
# gross_margin is not comparable across the set: CTSH, DXC file a cost of revenue that excludes depreciation and amortisation, ACN, EPAM, IBM file one that includes it
# no revenue row at all for IBM: the model specification binds no filed revenue concept, so every cell in that row is unavailable rather than zero
# operating_margin cannot be computed for ACN, CTSH, DXC, EPAM, IBM: no operating-income concept is bound by the specification
# columns are calendar quarters; each cell carries its own fiscal period_end, which can be up to one month from the column
```

`cells: 72 computed / 160 total; columns ['2024Q3', '2024Q4', '2025Q1', '2025Q2', '2025Q3', '2025Q4', '2026Q1', '2026Q2']`

**这张表已经能读出三件事，而且都不需要模型**：EPAM 的同比增速一年内从 19.4% 掉到 4.5%，是五家里减速
最猛的；DXC 是唯一负增长（−5.1%），且毛利率从 23.8% 掉到 20.4%；ACN 与 CTSH 的增速收敛到 5.6% /
4.5%。中间那些 `-` 同样是信息：ACN 2025Q3、CTSH / EPAM 2025Q4 的季度在库里缺口，这是 SEC lane 的
覆盖问题，不是公司没披露。

### 3.2 缺口清单

行业主体的 canonical Claim 数 **0**；公司档案节 **0**（这份只读副本早于 P12a 合入）；行业 DebateMap
**None**。所以九条全部 `open`：

| gap | content kind | 候选来源（连接状态） |
| --- | --- | --- |
| `gap:tam-and-share` | expert_excerpt | company-wiki(undeclared), guidepoint(**not_connected**) |
| `gap:supply-capacity` | expert_excerpt | company-wiki(undeclared), guidepoint(not_connected) |
| `gap:high-frequency-demand` | web_page | gemini-web-search(connected/**generic**), web-fetch(connected/**generic**) |
| `gap:pricing` | expert_excerpt | company-wiki(undeclared), guidepoint(not_connected) |
| `gap:labour-market` | employee_review | employee-reviews(undeclared) |
| `gap:bookings-detail` | management_minutes | alphaengine(connected), company-wiki(undeclared) |
| `gap:ai-budget-pool` | sell_side_report | alphaengine(connected) |
| `gap:competitive-wins` | sales_note | sales-notes(undeclared) |
| `gap:cash-conversion` | filing | cninfo(undeclared), **sec(connected)** |

三条给 owner 与 S 线的读法：

1. **`gap:high-frequency-demand` 只有 generic 候选。** 能力表里没有任何一个专门的源能给出季度之间的
   需求读数。因果链第一环说预算重配是季度节奏，而我们只能按季度观察它——这是能力表自己承认的空洞，
   不是可以靠更努力检索补上的。
2. **`gap:cash-conversion` 的来源已经接了。** SEC 是 connected。缺的不是数据源而是建模规格没把现金
   流量表的科目绑上去，属于 P13-M2 的工作，不该被当成一条要接的源。
   `gap:tam-and-share` 与 `gap:competitive-wins` 则相反：它们的 `driver_refs` 是空的，按构造永远
   open，接源是唯一出路。
3. **四条指向 Guidepoint**（TAM、供给产能、定价、以及部分劳动力）。这就是蓝图说的「P12e 缺口清单驱动
   S 线」的具体样子：要接的第一个源是 Guidepoint，第二个是 sales note 的人工投喂入口。

---

## 4. 测试

三个新测试文件，共 139 项；六个 golden case 并入 Q1 既有的 golden 套件；`tests/test_mission_deliverable.py` 新增 7 项（§5.5 的格子引用契约）。

```
Ran 139 tests in 0.987s

OK
```
（`tests.test_industry_framework` 76 + `tests.test_industry_framework_draft` 37 +
`tests.test_industry_framework_lane` 26）

全量：

```
Ran 5191 tests in 525.419s

OK (skipped=1)
```
（`python -m unittest discover -s tests -t .`，合入 main `b1f1345`、复核修复之后）

值得单独点名的几项：

- `test_a_chain_that_gained_a_link_has_no_titles_and_is_refused` —— C3 的核心拒绝。
- `test_year_on_year_growth_is_the_ratio_of_the_two_filed_values` 等四项 —— fixture 的数字是故意取整的
  （100 → 110 就是 10.0%，成本 66 对收入 110 就是 40.0% 毛利率），公式改了会挂在一个数字上而不是一个
  哈希上。
- `test_a_newly_filed_quarter_is_new_evidence_even_with_unchanged_prose` —— 2.4 那一褶皱。
- `test_a_gap_with_no_driver_behind_it_is_open_by_construction`、
  `test_high_frequency_demand_has_only_generic_candidates` —— 缺口由能力表派生。
- `test_the_artefact_shape_matches_what_q1s_own_builder_produces` —— 防漂移（见 §5.1），
  顶层键与 section / number 行的键都比。
- `test_the_material_budget_is_allocated_per_company` —— 复核第 1 条，五家全填时每家都有可引的行。
- `test_a_remapped_chain_does_not_relabel_carried_forward_prose` —— 复核第 2 条。
- `test_connecting_a_source_moves_the_gap_state_and_occasions_a_version` —— 复核第 4 条。
- `tests.test_mission_deliverable.ComputedCellCitationTests` 七项 —— §5.5 的两种新格子与悬空引用被拒。
- `test_independence_fails_closed_on_an_unresolvable_family` —— D2 失败即关闭。
- `test_the_weekly_interval_holds_a_second_launch_and_says_so` —— 周频，且 `waiting` 带原因。

Q1 rubric：新增 `rubric:industry-framework`，哈希
`2574df8999249a7705f15521b7a5c60800b76665440eb5c818f1352329dd2d96`。**既有五份 rubric 的哈希一个都
没动**——已经写下的评分绑定着它们。

---

## 5. 集成待办

### 5.1 `research_quality_score.ARTEFACT_KINDS` 需要加 `industry_framework`

`ARTEFACT_KINDS` 是本分支不拥有的模块里的封闭元组，所以 `framework_artefact()` 自己拼了同样形状的
dict，`run_deterministic` 照常读它。有一项测试断言这个 dict 的键与 `artefact()` 产出的键完全一致，
防止两边漂移。集成时应当：把 `"industry_framework"` 加进 `ARTEFACT_KINDS`，加一个
`artefact_from_industry_framework` adapter，然后把 `framework_artefact` 改成调用它。

### 5.2 `new_version_cites_new_refs` 的口径差

Q1 这条检查只读 Claim ref。本交付物的新证据**通常只能是**新落的 filing accession（行业 Claim 路径还
没上线，行业主体 Claim 数为 0）。CLI 的 `rubric_gate` 沿用了 P12a 的 override（`new_refs()` 更宽者
胜，并在 summary 里记 `overridden_checks`），但这里比档案更依赖它——没有 override，这条 lane 会拒绝它
将来产出的每一个版本。长期正解是让 Q1 这条检查也认 filing accession。

### 5.3 行业主体 Claim 路径

`industry_claims()` 按名字对着 `query_company_research(company_ref=<industry_ref>, index_aspect="industry")`
写，等抽取吞吐分支（`w2-extraction-throughput`）把行业 Claim 路径接上就自动有料。在那之前，起草只能
靠对比表格子与公司档案节。

### 5.4 已完成但需要复核的共享改动

- `cockpit_plane.REGISTRY_LANE_LABELS` 加一行 `"industry_framework": "写行业框架：因果链、驱动、五家横向对比"`
  （主 agent 明确解禁这一个 dict）。
- `deploy/macos/install.sh` 加一个 all-or-nothing seed 块，只 copy policy 一个文件——**不写模型配置**，
  因为这条 lane 的产出一半是确定性的，只有 policy 的 Core 每周照样算出对比表。
- `scripts/rehearse_deploy.py` 的 `SeedSpec`、`LaneSwitch`（`seeded_by_install=True`）、
  `CORE_MIGRATIONS`、`REQUIRED_WRITE_SCOPES`（加 `deliverable`）。
- `bootstrap.SCHEMA_DATABASES` 加 `industry_framework_schema.sql`。
- `tests/test_rehearse_deploy.py` 里 seed 字面量集合同步。

### 5.5 复核后新增：`mission_deliverable` 的 `numbers[]` 扩项（对 P14f 也解锁）

复核裁定的一条决定，已实现。原来的规则是「每个时效性数字都追溯到一条定量 Claim」——它对要防的失败
判断是对的，对它假设的世界判断是错的：那个世界里，研究产出说出的每个数字都是抽取器从来源里读出来的。
现在有两层产出**算出来**的数字，而且都能被 Core 重新推导：P12e 的横向对比格（filed statement line 的
算术）与 P14f 的预测格（对某个存储的模型版本跑冻结公式）。两者背后都没有 Claim，也永远不会有——为一
次计算捏一条引文，才是真正的造假。

所以 `numbers[]` 加法扩展：一条数字条目引用一条 Claim **或**一个格子，不能都有也不能都没有。

```
{"text": ..., "claim_version_ref": ...}                       ← 原样，字节不变
{"text": ..., "cell": {"kind": "statement_accession",
                       "ref": "<cell_ref>", "accession": "<accession>"}}
{"text": ..., "cell": {"kind": "forecast_cell",
                       "ref": "<result@period:kind>", "version_ref": "<model version>"}}
```

发布时 `MissionDeliverableAuthority.cell_resolver()` 逐个解析：statement accession 要在
`coverage_mission_statement_filings` 里还在，forecast cell 要在那个模型版本的记录里找得到那个格子。
解析不了就拒绝——「这个 Core 造不出来的数字」这条纪律一点没松。Claim 路径的存储字典与既有测试**逐字
未变**。

**这对 P14f 的意义**：earnings preview / calibration 的预测数字现在可以从 summary 那一行搬进正文，
用 `forecast_cell` 引用，不必再为一个算出来的数造一条 Claim。

CLI 现在在 `IndustryFrameworkVersion` 发布成功后，同时 publish 一份 `kind="industry_framework"` 的
deliverable（`publish_deliverable()`）：认知层的产出都进版本化文档链。它是记录的**投影**而不是第二次
起草，所以两者不可能说出不同的话；它也刻意不致命——文档渲染被拒不会让已发布的 framework 版本消失，
summary 里的 `deliverable` 字段说明发生了哪一种。

### 5.6 尚未做的接线

- **Cockpit 页面。** 目前只有 lane 那一行。对比表与缺口清单值得一张自己的卡片。
- **verifier 模型配置。** 复用了档案的 `dossier-verifier-model-config.json` 文件名。如果两条 lane 要
  分别限流，各自需要一份。

---

## 5.7 复核修复（code-review，2026-09-10）

| # | findings | 处理 |
| --- | --- | --- |
| 1 | **BLOCKER** `comparison_material` 尾切 40 行，把排在前面的公司整家丢掉（五家全填时 ACN 消失），而 prompt 仍然展示整张表——模型看得见却引不了的数字 | 改成按公司分配额度（`limit // n`，各取自己最近的几格）；测试用五家全填的 filer 断言每家都至少有一行可引 |
| 2 | 因果链被重新映射后，沿用上一版的正文会被贴上新标题 | `_prior_units` 拿到当前链哈希，与上一版 bindings 里的不一致时不再沿用 `causal_chain:*`（三个 block 不是链形状，照常沿用）；测试 |
| 3 | deliverable 数字契约 | 见 §5.5，已实现并接线 |
| 4 | 缺口清单变动无法触发新版本 | 新增 `gap_state_ref()` 进 `evidence_scope` 与 `_fresh_evidence`；测试「接上一个源就能出新版本」 |
| nit | 两个相同的 `STATIC_UNITS` 分支 | 合并，并写明为什么每个 unit 看到同样三张表 |
| nit | 死代码 `drop_units` | 删除 |
| nit | parity 测试只比顶层键 | 同时比 section 行与 number 行的键 |
| nit | `gaps_are_actionable` 绑了一个不相干的 check | 改为 `layer="judge"`，并说明结构性的一半由 Constitution 的 `open_gaps_name_a_source` 承担（它读得到 `candidate_sources`，展平后的 artefact 读不到） |
| nit | `_pick_concept` 不看 `period_basis` | 只取 duration；instant 的行不能扮演流量角色 |
| nit | §3.2 漏了 `gap:competitive-wins` 也是按构造 open | 已补 |

rubric 因为 `gaps_are_actionable` 的改动重新冻结为
`79e410374685daca3363cefb0e76e5f2bfb0b72fd9db01877e3b96463dad4267`；其余五份仍未移动。

第 1 条修复在只读副本上复跑（`limit=40`，五家）：

```
citable rows: 32
  ACN: 8 rows      CTSH: 8 rows      EPAM: 8 rows      IBM: 0 rows      DXC: 8 rows
```

修复前，尾切会把 ACN 的八行全部挤掉；IBM 现在拿 0 行是对的——它一格都算不出来，所以它不贡献，
也不借别人的额度。

---

## 6. 未决问题（留给 owner / 主 agent）

1. **`operating_margin` 整行为空要不要现在补？** 补法是让五家的建模规格绑定
   `us-gaap:OperatingIncomeLoss`（P13-M2 范围）。在那之前这一行是诚实的空，但它也是横向对比里最该有
   的那一行。
2. **毛利率口径不可比，要不要在表里做归一？** 可以把 CTSH / DXC 的成本加回折旧摊销再算一遍。我没有
   做：那需要一个「哪一部分折旧属于交付成本」的假设，而一个带假设的格子和一个 filed 的格子放在同一
   张表里、只靠注记区分，是我不愿意默认的事。
3. **周频合适吗？** 现在是 coordinator 里的七天硬间隔。业绩季里五家会在三周内全部申报，届时可能想要
   更密。`--revise` 是现成的手动入口。
4. **`gap:high-frequency-demand` 没有专门来源，要不要新评估一个第三方数据源？** 这是九条里唯一一条
   现有能力表完全答不了的。

---

## 附录 A：记录形状

```
IndustryFrameworkVersion
├─ sections[]                     一环一节，link_index / link / title 来自 Constitution 与 policy
│   └─ slots[] = state | divergence      每句 {text, refs}，refs 只能是被展示过的 ref
├─ industry_characteristics       五个封闭词 + 每个一条 basis
│   classification                （复用 dossier 的 INDUSTRY_CLASSIFICATIONS）
│   cyclicality / revenue_visibility / capital_intensity / concentration
├─ long_term_drivers              driver pack 的 driver，policy 定时间跨度
├─ short_term_drivers             每个 driver 一个 stance（复用 evidence pack 的 DRIVER_STANCES）
├─ cross_company_comparison       代码算出：companies / quarters / cells / comparability_notes
├─ debates_summary                P12c 行业链的投影（问题、状态、两边引用数、转向原因）
├─ gaps[]                         policy checklist × 本版覆盖 × SourceCapabilityMap
├─ bindings                       constitution / playbook / driver_pack / policy / rubric /
│                                 causal_chain_hash / comparison_hash / debate_map_version_ref
├─ change_reason + evidence_refs  ADR-0008
└─ body_hash / content_hash
```
