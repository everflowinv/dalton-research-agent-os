# P11b consensus 双路交付报告 v1.0

日期：2026-09-09
分支：`w2-consensus`（worktree `~/Projects/dalton-w2-consensus-worktree`）
分叉基线：worktree HEAD `eaf48f0`（已含 main `7708d43` 与 `6da8f82`）；交付前 `git merge main`
全量测试：`Ran 4529 tests in 755.116s` / `OK (skipped=1)`（合并 main `eb8e5fb`、含 owner 的标签别名裁决之后；逐字见 §7）

---

## 0. 一句话

Dalton 现在知道街上怎么想：一条 append-only 的 `ConsensusEstimateVersion`（每一版绑一次 yfinance
`analyst_estimates` 调用与原始产物哈希，Yahoo 的 `0q/+1q/0y/+1y` 被显式映射到公司自己的财年，映射不出来就拒绝
发版）；一个 `StreetEstimateClaim`——从已入库研报首页逐字核出的券商目标价与评级，带第三种 figure grade
`broker-research-report`，**永远不可作为关于公司的定量 Claim**；以及一条「两家独立券商才算 consensus」的
互证规则，它在实盘上对 ACN 与 EPAM 成立，对 DXC 不成立（DXC 仅有的两个目标价都是 TD Cowen 的）。

---

## 1. 提交清单

| commit | 内容 |
| --- | --- |
| `21e480d` | `ConsensusEstimateVersion` 权威 + schema + 财年映射 |
| `c09b3c5` | `StreetEstimateClaim` + schema + 首页抽取 + 第三种 grade + `broker-estimate` basis |
| `29ae3ef` | lane + 子进程 CLI + launcher + `model_input` 的 consensus 角色 |
| `b3a6f39` | 测试（四个文件） |
| `426b13a` | CLI 测试与本报告 |
| `a862929` | lane 的两项登记：`MigrationSpec` + cockpit lane 标签 |
| `c24310a` | `git merge main`（迁移清单已按字母序，两条 spec 就位；`bootstrap.py` 同样两行） |
| `181cd73` | P15d 的 consensus reader 现在能解析到东西（模块级 `latest_consensus`） |
| （本次）| owner 裁决：按 grade 划定的标签别名表，产出 9 → 15 条 |

未推送。未部署。未写 live 状态。未发布 mission 版本。未做任何真实模型调用。

---

## 2. 形状

### 2.1 `ConsensusEstimateVersion`（`consensus_estimate.py` + `consensus_estimate_schema.sql`）

照 `MarketPriceSeriesVersion` 抄，理由也一样：**consensus 和价格是同一类东西**——它在你脚下移动，没有人
公开重述它，而上周引用过「街上预期 14.66」的估值必须在街上改主意之后仍然可复算。所以它被版本化而不是被
维护。

- 每公司一条链（`consensus_ref = consensus-estimate:{company_ref}`），三触发器（`dalton_authorized()`
  insert guard、no_update、no_delete）、`content_hash`、写后读回校验。
- **发版规则**：只有当某个值变了才发新版，否则 `duplicate`。参与比较的是
  `ticker / currency / price_target / recommendations / eps_estimates / revenue_estimates /
  report_consensus / fiscal_calendar` 的三个关键字段；**不比较** `fetch` 块与时间戳——明天用另一次调用取到
  同样的数字，是同一件事实，为它发版会让版本链变成 tick 日志而不是修订史。新版携带 `changed_fields`，直接
  说出哪一块动了。
- **每一版绑定一次调用**：`fetch = {invocation_ref, artifact_hash, governance_ref, governance_hash,
  captured_at, observed_on}`。四个块来自同一次 `analyst_estimates` 调用，所以绑定在版本层而不是逐字段重复。
- **`observation_basis: "vendor-observed"` 写在记录上**，不留给读者从 `source_ref` 推断。「观测到的」和
  「申报的」之间的区别是唯一一件读者不该需要去查的事。
- 字段：`price_target`（current / high / low / mean / median / number_of_analysts）；
  `recommendations[]`（按 `month` 的 strong_buy…strong_sell，缺失保持缺失——「没有分析师给卖出」和
  「Yahoo 没说有几个给卖出」是两件事，零只能表达其中一件）；`eps_estimates[]` 与 `revenue_estimates[]`
  （avg / low / high / year_ago / growth / number_of_analysts / currency）。
- 读者：`latest_consensus(company)`、`consensus_for_period(company, period)`、`versions()`、`version()`。
  `consensus_for_period` **只认映射后的标签**（`FY2027`、`FY2026Q4`），不认 `0y`：後者是一个「问题不变而
  答案每季度变一次」的问题，回答它正是 consensus 落在错误 actual 旁边的方式。

### 2.2 `StreetEstimateClaim`（`street_estimate.py` + `street_estimate_schema.sql`）

一条记录 = 一家券商在一份研报里对一家公司说的话：`broker`（slug）+ `broker_as_named` + `broker_basis`
（`document_metadata` / `page_text`）、`analysts[]`、`published_on`、`rating`（三码 + 券商原词 + 所用
scale + quote_id）、`target_price`（value / currency / horizon / quote_id）、`figures[]`（逐字核过的
numeric candidate）、`source_grade`、`statement`（带 qualifier 的人可读句子）。

- append-only，`UNIQUE(company_ref, document_ref)`：同一份研报读两次是一行。
- 另有 `street_estimate_document_scans`：**哪些研报读过、读出了什么**。没有它，lane 分不清「还没读」和
  「读过但里面没有目标价」，会每天重读同样那几十份读不出东西的研报。被拒的扫描连理由一起记下来。

**为什么不进 `coverage_mission_document_figures`。** 那张表的 `source_grade` 有 CHECK 约束，只列了
`company-filed-document` 与 `earnings-call-transcript`；`coverage_mission_schema.sql` 在本片的禁改清单里，
加第三个值要走别人的迁移。更根本的是那张表存的是**关于公司的**数字，而券商的目标价不是——它还需要券商、
分析师、评级、期限并排才有意义。自己一张表把这件事结构化地说清楚，而不是靠约定。见 §5 集成待办。

### 2.3 第三种 grade（`document_figure_grade.py`，additive）

```python
BROKER_RESEARCH = "broker-research-report"
BASIS_BY_GRADE[BROKER_RESEARCH] = "broker-research-report-estimate"
QUALIFIER_BY_GRADE[BROKER_RESEARCH] = "as stated by this broker in its own research note; "
                                      "it is that broker's estimate of the company, not a "
                                      "figure the company published or management spoke"
ALL_GRADES = (FILED, SPOKEN, BROKER_RESEARCH)   # GRADES 不变
```

**它刻意不进 `GRADE_BY_SPEC`，也不进 `GRADES`。**

- 进 `GRADE_BY_SPEC`（即 `{"sell-side-reports": BROKER_RESEARCH}`）会让 `figure_worthy("sell-side-reports")`
  变成 True，把普通数字抽取 pass 放到研报上去——正是该模块 docstring 存在的理由：「we model revenue of
  $17.9bn」能完美通过数字核对，而它不是公司的收入。
- 进 `GRADES` 会让 `record_document_figures` 接受它，然后在数据库的 CHECK 上炸出一条没人读得懂的约束错误。

它自动不可 FILED admissible：`research_verification.FIGURE_ADMISSIBLE_GRADES` 是 filed-only，
`claim_index_figures.promote_figure` 会具名拒绝并给理由。有测试钉住这三条。

`document_numeric_claim.ALLOWED_BASES` 增加 `broker-estimate`（additive）。原有四个 basis 描述的都是
**公司**得出的数字；把券商的目标价填成 `management-reported` 是一句关于「谁说的」的假话。副作用见 §5.4。

---

## 3. 映射规则（最容易悄悄错的一块）

Yahoo 只给 `0q / +1q / 0y / +1y`，从不说那是哪个季度。**Accenture 财年 8 月底结束，IBM 12 月底结束**，
所以同一个下午、同一个行业的两家公司，同样四个 key 意思不同。规则写死一句话，并记在每一版上：

> **`0` 是「下一个将被披露的期间」。**

`0q` = 结束日晚于 `last_reported_period_end` 的第一个财季；`0y` = 同理的第一个财年；`+1` 是再下一个。
两个必需输入都由公司自己的申报给出，lane 从 `coverage_mission_statement_filings` 读：

- `fiscal_year_end` = 最新一份 `form='10-K'` 的 `report_date` 的 `MM-DD`；
- `last_reported_period_end` = 任意 form 的最新 `report_date`。

**缺任一个就拒绝发版**（`FiscalMappingError`），lane 记 `fiscal_calendar_unknown` 并跳过这家公司。这不是
保守，是必要：填错季度的 consensus 会被拿去和一个它从来不是关于的 actual 对账。仓库里没有可引用的财年表，
也不新造一份——`claim_index_tagging.py:243` 出于同样理由拒绝过同样的诱惑。

验证（2026-09-09，`last_reported_period_end` 取各公司最新申报）：

| 公司 | FYE | `0q` | `+1q` | `0y` | `+1y` |
| --- | --- | --- | --- | --- | --- |
| ACN | 08-31 | FY2026Q4 @2026-08-31（已结束、未披露） | FY2027Q1 @2026-11-30 | FY2026 @2026-08-31 | FY2027 @2027-08-31 |
| IBM | 12-31 | FY2026Q3 @2026-09-30（进行中） | FY2026Q4 @2026-12-31 | FY2026 @2026-12-31 | FY2027 @2027-12-31 |
| DXC | 03-31 | FY2027Q2 @2026-09-30 | FY2027Q3 @2026-12-31 | FY2027 @2027-03-31 | FY2028 @2028-03-31 |

ACN 那一行由 fixture 独立佐证：`earnings_estimate["0y"].avg == ["+1y"].yearAgoEps == 13.86199`，即 `0y` 的
去年正是**最后一个已披露**的财年，所以 `0y = 上一个已披露财年 + 1`。「我们所在的季度」这个更直觉的规则会
把 ACN 判成 FY2027Q1，错一个季度。

季度端点：若财年结束日是当月最后一天，则每个季度都取当月最后一天（ACN → 11-30 / 02-28 / 05-31 / 08-31）；
否则取同一日并按月长截断。财年标号 = 结束所在的日历年（`label_basis` 记在每一版上）。

**评级期间不是财年期间。** `0m / -1m / -2m / -3m` 映射到日历月（`{"month": "2026-09"}`）：一个评级计数是
关于某个供应商在哪个月做的汇总，不管公司财年在哪结束。

---

## 4. 研报抽取：精度与产出

### 4.1 规则

`street_estimate_extraction.extract(context)` 是确定性的，这是决策不是取巧。研报上真正会伤人的失败不是
读错数字，而是**读到别人的数字**：把一份 125 页覆盖三十家公司的行业回顾的首页目标价填给 Accenture，模型
质量再高也救不了，只有「哪些文档可以被读」的规则能救。因此模块的大半是拒绝：

| 拒绝理由 | 意思 |
| --- | --- |
| `not_sell_side` | 不是研报类文档 |
| `multi_company_report` | 文档 metadata 名下不止一家公司 |
| `subject_not_named` | 首页窗口里从没出现这家公司的名字（与 `figure_recordable` 同一条规则） |
| `broker_unknown` | metadata 与首页正文都认不出券商 |
| `no_target_price` | 首页没有带货币符号的目标价 |
| `ambiguous_target` | 首页给出两个不同的**在生效**目标价 |
| `label_does_not_name_a_line` | 标签没有命名一条线（owner 裁决后，`PT/TP/price target/target price` 不再落入此项，见 §4.3） |
| `no_currency` / `digits_not_in_citation` | 没有币种 / 数字不在所引原文里 |

三条正则对应实盘的三种版式：行首标注（`\nPrice Target: $11.00`）、行内正向（`PT to $97`）、行内反向
（`$270 PT`）。**标注版式命中后不再跑正文版式**——否则一份既有 masthead 目标价、又在正文讨论 bull case 的
研报会「自己和自己不一致」。`Prior / Previous / From / Old / Was` 引导的目标价被标 `superseded` 而不计入；
这个前视窗口**只看同一行**（写测试时发现按固定宽度回看会连上一行的 "Prior" 一起吃掉，于是两个目标价全被
标成过期，页面反而一个在生效的目标价都不剩）。

评级用**按 scale 的**别名表，不是一张全局表。四个 scale：`overweight`（OW/EW/UW，中间那档 Morgan Stanley
叫 Equal-weight、J.P. Morgan 叫 Neutral，是同一档）、`outperform`（OP/SP/MP/PP/UP）、`buy-hold-sell`、
`buy-neutral-sell`。每家券商声明自己跑哪个 scale；**别家 scale 的词一律拒绝**——RBC 没有 Overweight 这一档，
RBC 研报里的 "Overweight" 是在说别人的评级，记下来就等于把别家的观点挂在 RBC 名下。两字母缩写只在
**大写**、且 ±40 字符内有评级线索词（maintain / reiterate / remain / rating / upgrade…）时才算数：
"UP" 是 RBC 的 Underperform，也是英文里最常见的两字母串。

### 4.2 实盘烟测（只读，无模型调用，无网络）

对 live spool 的 AlphaEngine 研报对象跑本片交付的抽取器（`document_code ∈ foreignReport /
domesticReport / sellSideReport / researchReport`，首页窗口 4,000 字符，quote 按 1,200 字符切）：

```
spool 里的研报对象：227 份（名下 0 家公司 26、1 家 171、2 家 14、5 家以上 16）
提到覆盖池内公司的研报：68 份（单一发行人 50、多公司 18）
记录的 StreetEstimate：15 条
拒绝：multi_company_report 18、no_target_price 18、subject_not_named 17
```

| 公司 | 记录数 | 独立券商 | 券商 | 带评级 | report_consensus |
| --- | --- | --- | --- | --- | --- |
| ACN | 4 | 4 | td, ubs, citi, wells-fargo | 4 | **成立**：low 173.00 / high 275 / mean 208 USD |
| EPAM | 7 | 5 | jpmorgan ×2, td ×2, morgan-stanley, citi, guggenheim | 7 | **成立**：low 97 / high 165 / mean 122.6 USD |
| CTSH | 1 | 1 | wells-fargo | 1 | 不成立（只有一家） |
| DXC | 2 | 1 | td ×2 | 2 | **不成立**（两份都是 TD Cowen）——互证规则存在的理由 |
| IBM | 1 | 1 | rbc | 1 | 不成立（只有一家） |

版式分布：`labelled` 8、`inline_reverse` 4、`inline_forward` 3。标签：`price target` 9、`PT` 4、`TP` 2
（后 6 条**只因为 owner 的别名裁决才读得出来**，见 §4.3）。券商来源一律来自 metadata
（`document_metadata` 15/15），从未需要解析正文。

**精度：目标价 15/15 正确，评级 15/15 正确（人工逐条核对所抽 quote，样本量 15）。** 6 条别名新增的全部
逐条看过 quote：RBC「Maintain our OP rating and $270 PT」、Morgan Stanley「Remain EW, PT to $97」、
Guggenheim「we reiterate our Buy rating and $165 PT」、UBS「Valuation: $275 PT—based on ~16x 2028E EPS」、
Citi「We reiterate our Neutral rating and $100 TP」、Citi「TP: US$190.00; Recomm: Neutral」——最后一条的
同一行还独立印着「Fiscal year end 31-Aug」，与 §3 里从申报算出的 ACN 财年端点相符。

**召回是被刻意牺牲的。** 计划书 §2 的口径（首 6k 字符有目标价数字）数出 ACN 8 / EPAM 8 / CTSH 5 / DXC 4 /
IBM 3 共 28 份；本片只记 9 条。差额几乎全在两条规则上：

1. `multi_company_report`：live spool 里共 227 份研报，其中 30 份名下不止一家公司（16 份名下 5 家以上，
   典型是支付与 IT 服务季度回顾，三十家一份、一家一节、每节自己的 `PT:` 行）。落到覆盖池内的 68 份里有
   18 份是这种。整份拒绝，不做分节解析：分节解析要在没有页码的纯文本里判断「这一段属于哪家公司」，
   而判错的代价正是把别人的目标价填给 Accenture。
2. `label_does_not_name_a_line`：4 份实盘研报只写了 `$270 PT` / `$275 PT` / `$165 PT` / `PT to $97`。
   `document_numeric_claim._names_a_line` 要求标签命名一条线而不是复述金额，"PT" 两个字母不够。
   **这条不绕过**：绕过的唯一办法是把研报没印过的词（"price target"）递给核对器，而核对器存在的全部意义
   就是拦住这件事。见 §6 待决问题。

### 4.3 标签别名（owner 裁决，2026-09-10）

owner 的裁决：`PT` / `TP` / `price target` / `target price` 在研报首页是**普遍公认的目标价行名**，可以满足
`_names_a_line`——**但只对 `broker-research-report` 这一个 grade**，走一张按 grade 划定的封闭别名表；申报与
纪要那两条路一字不改。

落地（`document_numeric_claim.py`，additive）：

```python
LABEL_ALIASES_BY_GRADE: Mapping[str, frozenset[str]] = {
    "broker-research-report": frozenset({"pt", "tp", "price target", "target price"}),
}
```

`validate_numeric_candidate` / `verify_numeric_candidate` / `verify_numeric_candidates` 各多一个
**关键字参数** `grade=None`。不传 grade 的调用方（全部现有调用方）逐字节地走原来的路；只有传了 grade 的
figure 才会在 wire 上多一个 `label_grade` 字段——**有条件地加**，否则每一条已入库 figure 的 `content_hash`
都会移动。三条测试钉住这一点：10-K grade 下的 `PT` 仍被拒、纪要 grade 下同样被拒、不传 grade 时哈希不变。

别名表**不**豁免逐字核对：标签仍必须出现在所引原文里。研报写 `PT` 而候选说 `TP`，照拒（有测试）。

正则同时加了 `TP` 并补上词边界：没有 `\b`，`PT` 是 `adopt` 的子串、`TP` 是 `output` 的子串，两者都会愉快地
和行内下一个美元数字配对（有测试）。

**产出变化：9 → 15 条，其中 6 条只因这条裁决才读得出来**；`label_does_not_name_a_line` 归零，
`no_target_price` 从 20 降到 18（两份用 `TP` 的研报现在被正则匹配到）。ACN 的独立券商从 2 家变成 4 家，
EPAM 从 2 家变成 5 家，IBM 从 0 条变成 1 条（RBC）。

### 4.4 互证规则

`street_estimate.report_consensus(estimates, as_of=..., window_days=90)`：

- **按不同券商计数，不按研报计数**——`metric_discovery` 的 corroboration 规则平移一个字段。
- 少于 2 家 → 返回 `None`（不是错误）：街上对这家公司只有一种公开看法，诚实的记录就是那条已经存在的
  单条 `StreetEstimate`。
- 同一家券商在窗口内发两篇 → 取最新那篇。
- 两种币种 → 不成立（把美元目标价和欧元目标价平均出来的东西不是一个区间）。
- 产出块带 `policy_ref` / `policy_hash`，挂到 `ConsensusEstimateVersion` 上（`source_kind:
  report_consensus`），并把 vendor 块原样带过；下一次 vendor 观测又把 report 块原样带回去。
- **区间不能开链**：一家公司若还没有 vendor 观测，range 会被拒（`not_attached`），但那条
  `StreetEstimate` 本身照存不误。

---

## 5. 集成待办

### 5.1 mission 授权

下一版 `coverage-mission:us-it-services` 的 `autonomy.may_write` 需要加 `consensus_estimate`
（词表在 Wave 0 已加）。在那之前 lane 每 tick 返回 `ungranted`，不发网络调用、也不读任何研报——两条路都
在这个开关后面。

### 5.2 `deploy/macos/install.sh`

P11a 已经把 `yfinance-analyst-estimates-v1.json` 种进 `{state}/connector-governance/`（`proposed`）。
**owner 需要就地把它改成 `approved`**，lane 才会起孩子；改完之后 LaunchAgent 下次渲染自动带上
`--consensus-governance`（`argv_fragment` 已按文件存在与否判断）。install.sh 在禁改清单里，本片没动。

### 5.3 Initial Screen S6 现在可以引用街上的预期

`model_input.py` 的 `consensus` 角色从「词表里有、系统里没有」变成可绑定：新增第三种 source authority kind
`consensus_estimate_version`，在 `consensus_estimate_versions` 里按 ref + hash 解析，和另外两种一样。
（表不存在的 Core 上按「该权威不存在」处理，不抛裸的 `OperationalError`。）

S6 建议的 context 行形状——**一句话，三个 ref，永远带日期与来源标签**：

> 街上预期 FY2027 EPS 14.65516 美元（27 位分析师，Yahoo 汇总，观测于 2026-09-09），
> 目标价均值 184.1884 美元、区间 130–275（25 位分析师）；卖方研报侧两家独立券商的目标价区间
> 173.00–194.00 美元（TD Cowen 2026-08-25、Wells Fargo 2026-08-12）。
> ——`consensus-estimate-version:…` / `connector-invocation:yfinance:…` / `street-estimate:…`

三条硬规则给写这一行的人：(1) **永远说「街上预期」，不说「预期是」**；(2) **永远带观测日**，因为这是一个
观测而不是一个申报；(3) 研报区间必须同时给出券商名与日期，因为它是两个人的意见，不是一个统计量。

### 5.4 `document_numeric_extraction` 的 task hash 会变一次

`ALLOWED_BASES` 加了一个值，而 `document_numeric_extraction.OUTPUT_SCHEMA` 用 `list(ALLOWED_BASES)` 做
enum，`TASK_HASH` 由 schema 哈希得来。因此该 pass 的 task hash 变一次，work order id 与 idempotency key
随之重排一次。没有测试钉住那个字面量，也没有任何权威记录持有它；这是合同变更的正常代价，写在这里是为了
让集成时看到而不是发现。

### 5.5 `coverage_mission_document_figures` 的 CHECK（如果 owner 想统一）

若 owner 希望 street estimate 也落在那张表里，需要：`source_grade` 的 CHECK 加第三个值 + 一份
`MigrationSpec`，并把 `document_figure_grade.GRADES` 扩成三个。本片**不建议**这么做（理由见 §2.2），但把
它写下来作为一个明确的选项。

### 5.6 cockpit

- 公司卡需要一行「街上怎么想」：目标价 mean 与区间、分析师数、评级分布（buy/hold/sell 三色条）、
  最近一版的 `changed_fields`、`as_of`。**`observation_basis` 必须显示出来**——一个 vendor 观测和一个
  申报数字在卡片上长得一样是不可接受的。
- `report_consensus` 块要显示券商名而不只是数量：读者要能看出这是两个人的意见。
- lane 状态面板需要 `dispatch_mission_consensus` 的 `status`（`launched` / `scanned` / `idle` / `busy` /
  `ungranted` / `rejected` / `unconfigured`）、`skipped[].reason`（尤其 `fiscal_calendar_unknown`）与
  `scan.reason`（八个拒绝理由）。**`ungranted` 必须可见**，否则一条因缺授权而永远沉默的 lane 和一条健康的
  空闲 lane 在界面上一模一样。
- 研报抽取的拒绝分布本身是有信息量的：`multi_company_report` 占多数说明素材以行业报告为主，这直接支持
  「AlphaEngine 配额要不要调」这个决定。

### 5.7 P15d 的 conviction call 现在读得到 consensus（已完成）

P15d 的 `conviction_call_cli.consensus_gap` 是在本片还不存在时写的：它**按名字**在调用时查
`dalton_core.consensus_estimate.latest_consensus(store, company_ref)`，而不是 import，「这样这条 lane 会在那个
权威落地的当天自己开始工作」。那一天到了，所以函数写在本片而不是去改那条 lane。

它回答的是「我们的预测 vs 街上的预测」，需要两边。街上那边是本片；我们那边是 forecast model，两者用**唯一
一件不需要共享词表就能对上的东西**连接：**财季/财年的结束日**——本片从公司自己的申报算出它，forecast model
的每个 cell 上都带着它。用标签匹配不行：`FY2027` 在本片有精确含义，而它不是 forecast model 给自己的列起的
名字，按猜测连接就是把 gap 算到错误的年份上。

没有预测、或没有重叠期间，报成**缺失**而绝不报成一致——一份 conviction call 如果 consensus 段落悄悄消失，
读起来就是「我们和街上看法一致」，而那正是它绝不能不小心说出口的一句话。

那条 lane 的三个测试随之移动：一个原本断言理由里写着「P11b 还不存在」，现在写的是「这家公司还没有
consensus」——同一个答案、同一个理由；另外两个把假模块只塞进 `sys.modules`，在真模块不存在时够用，在它存在
的当天就不够了（`from . import consensus_estimate` 走的是 package 属性），改成两处一起替换。

### 5.8 P13-M3 的 sensitivity 也按名字解析本模块（已完成）

除 §5.7 的 `latest_consensus` 外，P13-M3（`w3-sensitivity`）按名字要
`consensus_estimate.report_consensus(store, company_ref)`，期望**每家券商一行**的
`{broker, value, refs}`，不足两家独立券商时给空。已交付为 `street_estimate.report_consensus` 的薄适配器：
规则仍然只有一处（按不同**券商**计数、90 天窗口、混币种不成立），同一家在窗口内发两篇取最新那篇——与它所
依据的那个 range 保持一致。**每行恰好三个键**，因为按名字解析的调用方如果封闭了形状，多一个键就会被拒；
币种不在其中也不需要在：混币种的 range 本来就不成立，所以每一行都在同一个币种里。

两个按名字解析的读者都不抛异常：读不出来的街就是没有街，不是一次故障。`tests/test_consensus_estimate.py`
的 `ResolvedByNameTests` 把两个签名与两个形状都钉住了——它们是与看不见它们的代码之间的合同，正是那种会
悄悄坏掉的合同。

### 5.9 模型路径（未接线）

`street_estimate_extraction` 交付了 estimates table 的模型面（`build_request` / `build_prompt` /
`verify_estimate_table`，冻结 `TASK_HASH`，purpose `street_estimate`——`model_fallback_chain` 里已有
`"street_estimate": TIER_CHEAP`），但 **lane 在 v1.0 不调用它**，也没有 `register_purpose`。理由：确定性
那条路今天就能挣到饭钱，而一个「读对了数字、读错了列」的模型调用不能。接线时需要：一份 routing policy、
一个 budget policy、`cockpit_model.register_purpose("street_estimate")`，以及给 lane 加一个
`--consensus-model-config`。`verify_estimate_table` **整块拒绝**而不是逐行拒绝，这是它和普通数字 pass 的
唯一实质差别：那边一个模型答对三个、编造第四个，应当只丢第四个；这边的失败模式是读错了整整一列，一行不
对是关于这次阅读的证据，不是关于那一行的。

---

## 6. 待决问题

1. **AlphaEngine 配额（owner，一句话的问题）：** 在现行 130 篇/24h 下，五家里只有 ACN（4 家独立券商）与
   EPAM（5 家）越过「两家独立券商」这条线，CTSH / DXC / IBM 各只有 1 家（分别是 Wells Fargo、TD Cowen 两篇、
   RBC）——**要把这三家也抬过线，配额该提到多少，以及是否同意把检索明确偏向「单一发行人的公司更新」？**

   支持这个问题的实测：spool 里 227 篇研报，只有 68 篇名下有覆盖池内的公司，其中 50 篇是单一发行人；多公司
   的行业回顾（16 篇名下 5 家以上）对本片**零产出**且必然零产出，所以配额里花在它们身上的份额是纯损耗。
   CTSH / DXC / IBM 的缺口不是抽取率问题（这三家的单一发行人研报里，凡有目标价的都抽出来了），是**素材里
   就只有那一家券商**。
2. ~~**bare-PT 的两个字母。**~~ **已由 owner 裁决（2026-09-10）并实现**，见 §4.3：按 grade 划定的封闭别名
   表，只对研报生效。产出从 9 条升到 15 条。
3. **正文被截在 30,000 字符的 172 份。** 目标价与评级在首页，不受影响（烟测证实）；受影响的是 EPS/收入
   估计表。若模型路径接线，需要先确认被截的那些文档的表格是否还在窗口内。
4. **空格分隔的表格打败 `numbers_in`。** 写测试时发现的：`document_numeric_claim._NUMBER_RE` 的
   `\d[\d,\s]*` 会跨空格连读，于是一行 `2027E 4,900 5,000 5,100 5,200 20,200` 被读成一个二十位数，其中任何
   一个单元格都核不出来。这是研报估计表的常见版式。修它要动共享模块，本片只记录；模型路径接线前必须先解决，
   否则 `verify_estimate_table` 会对着正确的表整块拒绝。
5. **`published_on` 目前取 review 的 `created_at`。** 那是「本系统看到它的日子」，不是「研报发表的日子」，
   而 90 天窗口是按后者算的。AlphaEngine 的 metadata 里有 `publish_time`，但它没有落到
   `coverage_mission_discovered_documents` 上。烟测直接读 spool metadata，所以 §4.2 的日期是真的；lane 里
   的那条路暂时用 review 日期，偏差是采集延迟（实盘一般是天级）。修法有两种：把 `publish_time` 记进发现记录
   （动 `coverage_mission.py`，禁改），或让抽取器从首页正文的日期行读出来并逐字核对（可做，未做）。

---

## 7. 测试

新增四个测试文件 + 一个 CLI 测试文件，全部离线：

```
tests/test_consensus_estimate.py           41 项
tests/test_street_estimate.py              37 项
tests/test_street_estimate_extraction.py   30 项
tests/test_mission_consensus_lane.py       23 项
tests/test_consensus_estimate_cli.py       11 项
```

钉住的行为，按交付要求逐条对应：

- **权威链**：first version / duplicate / changed_fields / ticker 不可中途改 / 篡改记录读不回来 /
  未授权连接不能 insert / 不可 update 不可 delete。
- **duplicate**：同样的数字换一次调用仍是 duplicate（`test_the_same_numbers_tomorrow_are_a_duplicate`）。
- **财年映射与拒绝**：ACN / IBM / DXC / 2 月财年四组端点；未知 FYE 与未知 last_reported 各一条拒绝；
  非法 key 一条拒绝；评级月份是日历月。
- **评级别名表**：每家券商映射自己的词；别家 scale 的词被拒；没有声明 scale 的券商拒绝而不猜；
  大写缩写需要线索词（`test_an_abbreviation_needs_a_cue_to_be_a_rating`）。
- **逐字核对拒绝**：没过 `verify_numeric_candidate` 的 figure 被拒；basis 不是 `broker-estimate` 被拒；
  bare-PT 被拒且不改标签（`test_a_bare_PT_is_refused_rather_than_relabelled`）。
- **互证**：同一家券商两次 ≠ consensus；两家独立券商成立；同一家发两篇取最新；窗口外不算；两种币种不算。
- **grade 不可 FILED admissible**：`test_a_broker_figure_is_never_admissible_as_a_quantitative_claim`、
  `test_the_grade_is_not_a_mission_document_figure_grade`、
  `test_sell_side_research_is_still_not_read_for_company_figures`。
- **lane 注册**：operation / driver_key / init_kwarg / order 88 / 空 param_fields；writer 三处从注册表推导；
  driver tick 顺序在 market price 之后、model spec 之前；没有治理文件时 lane 不装、argv 为空。

全量：

```
Ran 4529 tests in 755.116s

OK (skipped=1)
```

---

## 8. 没有做的事

- 没有改 `writer_server.py`、`coverage_mission.py`（含 schema）、`bounded_planner_driver.py`、
  `macos_launchagent.py`、`install.sh`、`cockpit_*`、`PROJECT_STATUS.md`、`tests/test_service.py`、
  `tests/test_lane_registry.py` 的字面量、`connector_inventory`（`analyst_estimates` 操作 P11a 已备齐）。
- 没有发真实模型调用，没有注册模型 purpose，没有加模型配置。
- 没有向 live state 写入任何东西：烟测全部只读，Route 2 的实跑写在 `mktemp -d` 的临时目录里。
- 没有提交任何真实研报正文：测试里的三种版式是为测试写的，只用了券商机构名（那是事实，不是受版权保护的
  文本）。
