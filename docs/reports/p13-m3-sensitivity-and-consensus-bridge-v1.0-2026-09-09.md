# P13-M3 敏感性与 consensus bridge v1.0

日期：2026-09-09
分支：`w3-sensitivity`（worktree `dalton-w3-sensitivity-worktree`），基线 main `62b54fd`，已并入 main（stage-ladder / S5 / ask v2 / P14f / **P11b consensus** 之后）
作者：Wave 3 Agent「sensitivity」（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 1 节、[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) 5.2 Phase 13 P13-M3、[P13-M2 预测行 v1.0](p13-m2-forecast-lines-v1.0-2026-09-09.md)（尤其 §9 开放问题 5）、[P15d 高 conviction call v1.0](p15d-conviction-call-v1.0-2026-09-09.md)、playbook `model_discipline`

---

## 0. 一句话

driver 模型（P13-M2）把每条假设摆得一样重，但真实公司里一两条决定底线、其余是零头，读记录的人看不出来，于是**读记录的人在争论错的那个数**。这一片回答三个问题——哪几条假设真的动结果、每条在这家公司自己的历史里摆到过哪里、模型在那些位置上分别算出什么——外加一座通往 street 的桥。全部**在内存里用 M2 自己的 `compute_results` 算**，`SensitivityProjection` 是 `derived_deterministic`、内容哈希、绑定 ForecastModelVersion 的 `content_hash` 与报表 `inputs_hash`，**一条预测版本都不发**。live 只读副本上四家全部跑通，每家 5 条 driver、5 条历史带，consensus 如实 `unavailable`。

## 1. 做了什么

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `forecast_sensitivity_schema.sql` | 新增 | `sensitivity_projections`：append-only、`content_hash`、`dalton_sensitivity_projection_authorized()` 授权触发器、禁改禁删、按公司的版本链 |
| `forecast_sensitivity.py` | 新增 | 历史带 + 弹性 + swing 排序 + what-if 表 + `SensitivityProjectionAuthority` |
| `consensus_bridge.py` | 新增 | 按名字解析 consensus 读者、`report_consensus` 双券商区间、**直接复用 P15d 的 `validate_consensus_gap`** |
| `forecast_sensitivity_cli.py` | 新增 | lane child，`summary.json`，状态 `published` / `duplicate` / `refused:<reason>` / `nothing_to_project` |
| `forecast_sensitivity_launcher.py` | 新增 | `LaneChildLauncher` 子类，digest = 公司 + 模型版本哈希 + consensus 指纹 |
| `mission_sensitivity_lane.py` | 新增 | 无队列协调器，order 96（预测行 lane 95 之后） |
| `company_model_report.py` | 改（共享，只加） | `render_sensitivity`，`--sensitivity` 开关 |
| `lane_registry.LANE_MODULES` | 改（一行） | `dalton_core.mission_sensitivity_lane` |
| `budget_pools.LANE_POOLS` | 改（一行） | `dispatch_forecast_sensitivity: coverage` |
| `cockpit_plane.REGISTRY_LANE_LABELS` | 改（一行） | `forecast_sensitivity: 算哪些假设最要紧、历史上摆到过哪里` |
| `bootstrap.SCHEMA_DATABASES` | 改（一行） | `forecast_sensitivity_schema.sql` |
| `scripts/rehearse_deploy.CORE_MIGRATIONS` | 改（一行） | 同上的 `MigrationSpec` |
| `pyproject.toml` `[project.scripts]` | 改（一行） | `dalton-forecast-sensitivity` |
| `tests/test_forecast_sensitivity.py`、`tests/test_consensus_bridge.py`、`tests/test_mission_sensitivity_lane.py` | 新增 | 112 项（49 / 38 / 25） |
| `src/dalton_core/research_event.py` | 改（**不是本片的文件**，见 §9） | 补回 main `427f684` 合并时丢掉的一个 `}),` |

**没有碰**：`writer_server.py`、`coverage_mission.py`（及其 schema）、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、`cockpit_control.html`、`PROJECT_STATUS.md`、`tests/test_service.py`、`tests/test_lane_registry.py` 的字面量、`model_forecast_driver.py`（一个字没改——`compute_results` / `chain_base` 就是接缝）、其他 agent 的模块。

## 2. 三个交付物，和每个背后的失败模式

### 2.1 driver 选择：排序规则不是弹性，是 swing

蓝图说「3–5 个关键 driver」，我先按字面做了：每条假设加一个百分点，重算，按底线移动多少排序。**在真数据上立刻塌了**：

```
CTSH  SG&A 份额 +1pp  → 净利润 -312,533,819.07133690
CTSH  重组   份额 +1pp → 净利润 -312,533,819.07133688
CTSH  成本   份额 +1pp → 净利润 -312,533,819.07133687
CTSH  D&A    份额 +1pp → 净利润 -312,533,819.07133687
```

这不是巧合，是恒等式：这个模型里每条费用假设都是**同一条预测收入的份额**，所以一个百分点就是收入的一个百分点，对所有费用 driver 是同一个数。按弹性排序等于按最后一位的舍入噪声排序——一个既确定又毫无意义的顺序。

把成本和折旧分开的不是斜率，是**幅度**：CTSH 的 SG&A 份额三年里走过 13.28%–17.35（4.07 个点），D&A 走过 2.49%–2.75%（0.26 个点）。所以**排序按 swing**：把这条 driver 放到它自己的历史谷、再放到历史峰，看底线相差多少。弹性照样算、照样进记录（`impact.delta` / `delta_per_unit`），但排名由 `swing` 决定。规则连同这段理由写死在 `SELECTION_RULE` 里并进 `SELECTION_RULE_HASH`，ref 是 `rule:swing-rank:1`（**不是** `elasticity-rank`——按弹性排出来的 projection 绝不能和按 swing 排出来的混为一谈）。测试 `test_every_share_driver_has_the_same_unit_elasticity` 把这个恒等式钉住，所以没有人能在没有一条红测试解释原因的情况下把规则「简化」回弹性。

没有历史带的 driver **不与有带的混在一个刻度上排**：它被**降级而不是剔除**——排在所有有带的之后，彼此之间按弹性。给它编一个幅度去和别人比大小，正是这一片要从 filing 里取幅度的原因。只有**两个数都没有**的 driver 才会被丢掉；丢一个只是缺其中一个数的 driver，等于对读者只字不提它，而只字不提会被读成「它不要紧」。

**弹性要求这条假设在整个预测期是一个数。** 一条只被改过某一个季度的 driver 没有「那个水平」可以加一个百分点；拿第一季的值去加，等于把其余季度压平到它上面、再把压平的效果报成敏感性。这条 driver 的 `impact` 是 `unavailable` 并说明理由，但它照样按 swing 参与排序（`test_a_driver_with_no_single_level_gets_no_elasticity`、`test_a_driver_with_no_elasticity_is_still_ranked_on_its_swing`）。

排序用的指标是**这个模型算得出来的最靠下的那条线**，precedence 冻结为 `free_cash_flow → net_income → operating_income → revenue`。按收入排会让顶线在每家公司都是第一——这是构造出来的，不是发现。

### 2.2 历史带：单位必须是被替换的那条假设的单位

带取在**假设自己的 measure** 上，不是水平值：`quarterly_growth` 取相邻季度环比（相邻判定沿用 M2 的 80–100 天，因为 10-Q 拼出来的历史每第四季是个洞，跨洞两季当一季就把两季增速塞进一季的率里），`revenue_share` / `operating_income_share` 取逐季比值。水平值的带没法代进假设里，代不进去的带就是装饰。

每个极值带**它发生在哪个季度**和 accession。「峰值 70.14%」是个数字；「70.14%，2025 年 2 月那个季度，0001467373-26-000014」是个可以去查的事实。并列极值归给**最早**达到它的季度（有测试），两台机器不能对「哪个季度是峰」有分歧。

每个极值还带 **runner_up**：往里数第一个**不同**取值的观测和它的季度。CTSH 的税率峰 70.79% 和第二高之间隔着一大截——那个峰是一次性事件，不是税率。要替读者判断哪些极值是异常就得有一条剔除规则，而没有原则的剔除就是挑数；把第二个数印在旁边不花什么代价，人自己就能看见那道缝（`test_each_extreme_carries_the_next_observation_in`）。取「不同取值」是为了让两个并列在峰上的季度不会互相当 runner-up 而把真实的缝藏起来。

`latest` 和三个统计量并排，因为带本身会引诱读者把我们的估计放进区间里就算完；**这条 measure 最近在哪**才是估计要被拿去辩护的那个数，而且经常就在某一端。

守卫两条：
- **观测不足 4 个就没有带**（`MIN_BAND_POINTS`），返回 `unavailable` 加数量。三个季度是三个数，「峰」这个词预设了一个分布。
- **分母在窗口内变号就没有带**。M2 的生成器已经有这条；这里重复它，理由相同：一家上季亏这季盈的公司，「税率」是「负若干」和「正若干」，两者里较高的那个描述的是亏损，不是税。

### 2.3 what-if：每一格追到它替换掉的那条假设

四个情景 `trough / mean / ours / peak`，四条线 `revenue → operating_income → net_income → free_cash_flow`，逐季度加区间合计。每格带 `replaced_assumption_refs`（本模型里被换掉的那几条假设的 ref）和 `input_refs`（情景值来自哪几个 filing 格，带 accession）。

**每一列都是把那个水平在所有未实现季度上摊平持有，不是一条路径**，而且每一行自己说出来（`held_flat`、`quarters_held`），`SELECTION_RULE` 里说、渲染视图的「HOW TO READ IT」里也说。「谷」不是「谷那个季度再来一次」，是「整段预测期都待在那儿」——这是个更狠的问题，读者必须知道被问的是这个。不知道的人读到的是一张比眼前这张温和得多的表。

**`ours` 走同一条路重算，不是从记录里抄出来的。** 如果重算我们自己的估计算不出模型本身，其余三列就是在一个不是模型的基线上量出来的——而且看不出来，因为印出来的那个数还是我们的。测试 `test_our_own_column_reproduces_the_stored_model` 逐格比对。

**只算还没被 filing 回答的季度。** 已实现的季度是定局，对它做情景等于给一个人人都能查到的数造区间。全部季度都已实现的模型整体 `refused`。

**partial 不求和。** 三个季度算出来的合计和四个季度的合计并排放，是两个不同东西的比较，而差值会被读成敏感性。

**一格都不发布。** `compute_results` 在内存里跑，结果落进 `SensitivityProjection`。what-if 能被发成预测版本的那一天，版本链就从「我们当时怎么想」变成「我们试过什么」。真要改假设是 `revise_assumptions`，它要理由和证据，不在这个模块里。

## 3. consensus bridge

形状**就是 P15d 的**：`{metric, period, ours, consensus, unit, gap_percent, refs}`，外层 `{status, reason, metrics}`。校验器是 `conviction_call.validate_consensus_gap` 本身，`import` 进来而不是照抄——测试断言的是 `cb.validate_consensus_gap is validate_consensus_gap`，不是「二者一致」。两个今天一致的校验器半年后会分歧，而分歧会在两次模型调用付完钱之后以「call 被拒」的形式出现。

绝对差 `gap_abs` 也留着，但留在 projection 的 `bridge_detail` 里、**在**被校验的行**旁边**而不是里面：收入差 2% 和 EPS 差 2% 不是同一个分歧量级，但被校验的形状是闭合的、归 P15d 所有。

三条设计：

- **读者按名字解析**（`latest_consensus`、`report_consensus`），不 import。P11b 在 `w2-consensus` 分支上还没并。P11b 已经并进来了，这条桥当天就接上了——本模块**一个字没改**就从「没有 consensus 模块」变成「读到了、这家公司还没有估计」（见 §5）。签名**按 `inspect.signature` 读**，两种写法（`(store, company_ref)` 与 `(company_ref)`）都能用。原来是先调一次、`TypeError` 了再换个调法——但读者内部抛出的 `TypeError`（比如拿字符串和 Decimal 比大小）和「参数个数不对」长得一模一样，于是一个本来能用的读者会被用错的参数再调一次，报出来的是第二次的失败。签名是事实，调用抛出的异常是猜测（`test_a_type_error_from_inside_the_reader_is_not_read_as_an_arity_mismatch`）。P11b 已确认是 `latest_consensus(store, company_ref)` 与 `report_consensus(store, company)`。
- **两家券商，否则不算 consensus**。vendor 那一行没数字时 `report_consensus` 可以顶上，但必须来自 ≥2 家**不同**券商，给区间与中点、带两家的 refs。一份 note 是一个分析师，把它叫作「街上」正是 variant view 被凭空造出来的方式。**计数只在「既有券商名、又有数字」的行上做**：review 抓到的 blocker 是这里原本分两次数——券商名在所有行上数、数字在有值的行上数——于是「Alpha 两份 note + Beta 一份没数字」同时满足两个条件，发出一个两端都是 Alpha 的区间，还叫作 consensus。现在只有一份名单，`test_two_notes_from_one_house_plus_a_valueless_second_is_one_house` 钉住那个确切形状。
- **缺就是缺，从不编**。整段消失会被读成「我们和 street 一致」，那是这个对象唯一绝不能不小心说出口的话。

**P11b 并入后发现的两处形状差异，都已对齐（第三轮）：**

1. **`latest_consensus` 返回的已经是算完的 gap 行**，不是「街上的数」。它的 `ours` 读的是 `ForecastModelAuthority.latest`——和这份 projection 所投影的**同一个 driver 模型**。所以这里**原样透传**（校验后），不自己再算一遍：再算一遍就是同一个数的第二份实现，正是本文件对校验器坚决不肯做的事，对算术也没有理由破例。透传时**不拿本模块的 `BRIDGE_METRICS` 再筛一次**——P11b 用模型 result 的 label 作 metric 名（`Revenue` 而不是 `revenue`），再筛一遍会在 live 上把每一行都悄悄丢掉。旧的 `{metric, period, value}` 形状仍然认得，`ours` 仍由这边补上；两种形状按「每一行是否已经两边都有」区分。
2. **`report_consensus(store, company_ref, *, as_of)` 只发目标价**，签名里没有 metric / period。已改成按它的写法调用；`metric` / `period` 变成**本模块自己的检查**：问它要收入而它只发目标价时，回答是「它不发这个」，而不是把一个目标价换个名字递回去（`test_the_broker_reader_does_not_stand_in_for_a_metric_it_never_publishes`）。P11b 底下 `street_estimate.report_consensus` 也按「不同券商家数」计数，所以两家券商这条规则现在两层各锁一道——对于「我们到底有没有 variant view」这个数，两道锁是对的。

**EPS 今天由 P11b 桥**（它自己有 EPS 行）；本模块自己算 `ours` 的那条旧路径仍然桥不了 EPS，并说明理由而不是悄悄跳过：driver 模型没有稀释股数（M2 §9 开放问题 5c）。目标价要 P11a 的估值快照里真有一条 target price 才桥；没有就说「没有估值快照绑到这家公司」。

## 4. lane

order **96**，在预测行 lane（95）之后——敏感性表是模型的投影，从一个这一 tick 正要被替换掉的模型上算出来的表，存进去之前就已经过期了。

无队列：每 tick 把每家公司的「模型 `content_hash` + consensus 指纹」哈希一次（`fingerprint`），和它已有 projection 的比。相等就静默。四家公司拿到四张表，然后这条 lane 什么也不做，直到某个模型被修订或某家券商改了主意。

写入范围 **`model_run`**（既有词），刻意不是 `forecast_line`。这条记录就是「模型被跑了很多次、跑在没人采纳的假设上」的结果。live manifest `deploy/phase9/p9a-us-it-services-mission-v1.json` 的 `may_write` **已含 `model_run`**（M2 §6.1 记过它当时没被用到；现在被用上了），所以**不需要新 mission 版本**。

一次一个 child，下一 tick 结算；失败的 (公司, 指纹) 本进程内 hold 住，不占坑；`--company-ref` 也过 mission universe。

## 5. live 只读副本冒烟（`/tmp` 拷贝，未触碰 live）

`cp /private/tmp/dalton-ro/core.sqlite /tmp/dalton-smoke-m3-run/`，先跑四次 `company_model_forecast_cli` 建模型，再连跑 `forecast_sensitivity_cli`：

```
company:sec-cik:0001058290 published drivers 5 bands 5 bridge unavailable
company:sec-cik:0001352010 published drivers 5 bands 5 bridge unavailable
company:sec-cik:0001467373 published drivers 5 bands 5 bridge unavailable
company:sec-cik:001688568 published drivers 5 bands 5 bridge unavailable
None nothing_to_project
```

第五次静默——正是设计的静止态。IBM 没有 driver 模型（M2 已如实拒绝），所以这里也没有它。

consensus：**并入 P11b 之前**四家都是 `this Core carries no consensus authority module`；**并入之后**，同一段代码一个字没改，四家变成 `no consensus estimate is held for <company>`——读者真的被调用到了，只是 live 副本上 consensus lane 还没跑过。这正是「按名字解析」这个设计要证明的事。

带与排序（数字来自记录，每个极值在记录里都带 accession；下表的季度就是 accession 所在的季度）：

### Accenture plc（`company:sec-cik:0001467373`，指标 net income，8 个预测季，12 季历史）

| # | driver | measure | 谷 | 均 | 峰 | 最近 | 我们 | swing（占指标） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Revenues | quarterly_growth | -5.82% (2025-02-28) | 0.99% | 6.41% (2025-05-31) | 3.74% | **0.15%** | 56.19% |
| 2 | CostOfGoodsAndServicesSold | revenue_share | 66.42% (2023-11-30) | 67.86% | 70.14% (2025-02-28) | 67.23% | 67.76% | 23.01% |
| 3 | IncomeTaxExpenseBenefit | operating_income_share | 17.88% (2024-02-29) | 21.98% | 24.85% (2024-05-31) | 24.00% | 23.62% | 9.13% |
| 4 | SellingAndMarketingExpense | revenue_share | 9.68% (2026-05-31) | 10.14% | 10.63% (2024-05-31) | 9.68% | 9.83% | 5.91% |
| 5 | GeneralAndAdministrativeExpense | revenue_share | 6.01% (2024-11-30) | 6.39% | 6.87% (2024-02-29) | 6.13% | 6.27% | 5.32% |

**这张表把 M2 §9 开放问题 2 量化了**：ACN 的第一 driver 是收入环比，我们的假设 0.15% 落在均值 0.99% 之下、最近一季 3.74% 之下，而在这条 driver 自己的历史区间里净利润要摆动 **56%**（142.3 亿 → 246.8 亿，八个季度合计）。「季节性盲」不再是报告里的一句话，是排名第一的那一行。

### COGNIZANT（`0001058290`，net income，8 季，11 季历史）

| # | driver | measure | 谷 | 均 | 峰 | 最近 | 我们 | swing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | IncomeTaxExpenseBenefit | operating_income_share | 21.49% (2023-06-30) | 29.22% | **70.79% (2025-09-30)** | 26.43% | 37.06% | 78.33% |
| 2 | SellingGeneralAndAdministrativeExpense | revenue_share | 13.28% (2026-06-30) | 15.78% | 17.35% (2023-03-31) | 13.28% | 14.68% | 25.81% |
| 3 | Revenues | quarterly_growth | 0.23% (2023-09-30) | 2.10% | 4.00% (2024-09-30) | 1.26% | 2.76% | 16.57% |
| 4 | RestructuringCharges | revenue_share | 0.00% (2023-03-31) | 0.71% | 2.39% (2023-06-30) | 1.53% | 0.38% | 15.18% |
| 5 | CostOfGoodsAndServiceExcluding…D&A | revenue_share | 65.32% (2023-03-31) | 66.13% | 67.21% (2026-03-31) | 66.63% | 66.57% | 12.00% |

**CTSH 第一名是一次性事件，而表把它指出来了**：税/营业利润的峰 70.79% 落在 2025-09-30 那一个季度。我们的 trailing-4 假设 37.06% 也被那一季拖高到均值 29.22% 之上。这既是这条 driver 真的最要紧（swing 78%），也是**看到「峰」旁边那个季度才能判断它是不是一个税率**——这正是带必须写季度的原因（见 §8 开放问题 2）。

### EPAM（`0001352010`，net income，8 季，11 季历史）

| # | driver | measure | 谷 | 均 | 峰 | 最近 | 我们 | swing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CostOfRevenue | revenue_share | 65.44% (2024-09-30) | 70.30% | 73.14% (2025-03-31) | 69.64% | 70.91% | 79.30% |
| 2 | IncomeTaxExpenseBenefit | operating_income_share | 6.71% (2024-03-31) | 24.03% | 32.65% (2026-03-31) | 24.68% | 27.75% | 35.91% |
| 3 | RevenueFromContractWithCustomer… | quarterly_growth | -3.36% (2023-06-30) | 0.48% | 3.98% (2025-06-30) | 1.05% | 2.47% | 30.11% |
| 4 | SellingGeneralAndAdministrativeExpense | revenue_share | 16.61% (2023-06-30) | 17.08% | 17.71% (2024-09-30) | 17.33% | 17.11% | 11.37% |
| 5 | DepreciationAndAmortization | revenue_share | 1.69% (2024-09-30) | 2.07% | 2.42% (2025-03-31) | 2.27% | 2.27% | 7.46% |

### DXC（`001688568`，**指标退到 operating income**，12 季，11 季历史）

| # | driver | measure | 谷 | 均 | 峰 | 最近 | 我们 | swing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CostOfGoodsAndServiceExcluding…D&A | revenue_share | 74.88% (2024-09-30) | 76.93% | **79.63% (2026-06-30)** | 79.63% | 76.71% | 237.03% |
| 2 | SellingGeneralAndAdministrativeExpense | revenue_share | 8.65% (2023-12-31) | 10.16% | 12.47% (2025-06-30) | 10.94% | 11.17% | 191.07% |
| 3 | DepreciationAndAmortization | revenue_share | 8.86% (2025-12-31) | 9.83% | 10.52% (2022-12-31) | 8.90% | 9.18% | 82.75% |
| 4 | RestructuringCharges | revenue_share | 0.58% (2023-06-30) | 1.06% | 1.37% (2022-12-31) | 0.87% | 0.94% | 39.67% |
| 5 | Revenues | quarterly_growth | -1.08% (2023-12-31) | -0.10% | 1.04% (2025-12-31) | 1.04% | 0.19% | 13.61% |

DXC 的净利润在 M2 上就 `unavailable`（营业利润变号，税率没有意义），所以指标按 precedence 退到营业利润，**表照常出**。三个可读的事实：
- 第一条 driver 的**峰就是最近一季**（79.63%，2026-06-30），而我们的假设 76.71% 在它下面 2.9 个点；
- 把这条 driver 放到那个峰上，12 个季度合计营业利润是 **-3.334 亿**（谷上是 +13.947 亿）；
- 前四条 driver 的 swing 都超过营业利润本身（237%、191%、83%、40%），因为 DXC 的营业利润率薄到任何一条费用线的历史幅度都能把它吃掉。这不是模型的毛病，这就是这家公司。

纯文本视图（`dalton-model-inputs --state-dir … --sensitivity`，或 `company_model_report.render_sensitivity`）把上面每一行连同 what-if 四列印出来，引用列表超过三条时截断成「and N more filed cells」。

## 6. 测试

```
Ran 4905 tests in 681.099s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，并入 main `427f684` 之后的本分支。并入前、基线 `62b54fd` 上是 `Ran 4489 tests in 592.590s / OK (skipped=1)`，其中本片 +112：`test_forecast_sensitivity` 49、`test_consensus_bridge` 38、`test_mission_sensitivity_lane` 25。）

fixture 是八个季度的手算算术：收入按 +20% / -10% / +10% 循环，成本份额按 80% / 82% / 78% 循环，**SG&A 恰好是收入的十分之一、税恰好是营业利润的四分之一**——后两条是故意的：两条历史带宽度为零的 driver，逼排序去破平局，而且必须在每台机器上以同样方式破（按 driver ref）。带的期望值都是手算出来写全的。

值得点名的几项：

- `test_every_share_driver_has_the_same_unit_elasticity` —— 把 §2.1 的恒等式钉住。
- `test_our_own_column_reproduces_the_stored_model` —— `ours` 列必须逐格等于记录里的估计格。
- `test_a_what_if_is_bit_identical_when_recomputed` —— `canonical_json` 逐字节相同。
- `test_a_tied_extreme_is_attributed_to_the_first_quarter_that_reached_it`。
- `test_a_base_that_changes_sign_gets_no_band_rather_than_a_meaningless_one`。
- `test_the_ranking_puts_the_widest_band_first_and_breaks_ties_by_ref`。
- `test_a_model_with_nothing_still_ahead_is_refused_whole`、`test_too_little_history_is_unavailable_with_the_count_in_the_reason`、`test_an_unavailable_line_keeps_its_reason_rather_than_printing_a_zero`。
- `test_a_revised_assumption_moves_our_column_and_nothing_else` —— 假设在横跨预测期内不是一个数时，`ours` 列 `unavailable` 并说明，而不是挑一个。
- `ShapeTests.test_the_validator_is_p15ds_own`（`assertIs`）、`test_a_built_bridge_passes_the_conviction_calls_validator_unchanged`、`test_p15ds_own_reader_accepts_what_this_module_hands_the_authority`（走 `conviction_call_cli.consensus_gap` 这扇正门）。
- `test_one_broker_is_not_consensus`、`test_two_notes_from_one_broker_are_still_one_broker`。
- 注册：order 96 在 95 之后、`LANE_MODULES`、`LANE_POOLS`、cockpit 词条、`bootstrap.SCHEMA_DATABASES`、`rehearse_deploy` 的 `MigrationSpec`（schema / module / symbol / kind 四项都断言）。
- authority：禁改禁删、绕过 authority 的 INSERT 被拒、重算 = `duplicate`、模型被修订 = 第二版带 prior ref、两家 ref 末段相同的公司各自成链。

## 7. 集成待办

1. **授权：不需要新 mission 版本。** live manifest 的 `may_write` 已含 `model_run`；缺了就 `refused:the mission does not grant the model_run write scope`（有测试）。
2. **打包**：`*_schema.sql` glob 自动生效；`bootstrap.SCHEMA_DATABASES` 与 `rehearse_deploy.CORE_MIGRATIONS` 两处已登记（两处都有测试守着）。
3. **cockpit「company model」页**要的字段全在 `projection_readiness(record)` 里，无需新算：`drivers_selected`、`drivers_with_bands`、`drivers_without_bands`、`selection_status`、`impact_metric`、`horizon_quarters`、`history_quarters`、`what_if_cells` / `what_if_cells_unavailable`、`bridge_status` / `bridge_metrics`。纯文本视图可直接嵌 `company_model_report.render_sensitivity(record)`。cockpit lane 词条已加（`forecast_sensitivity`）。
4. **CLI 入口**：`dalton-forecast-sensitivity`（已加 `[project.scripts]`）。只读视图 `dalton-model-inputs --sensitivity [--json]`。
5. **`install.sh` / 连接器 / 模型配置**：无改动需求。这条 lane 不调模型、不连外部源、没有自己的模型配置名（所以 `cockpit_model.PURPOSES` 与 `raise_day_budget_cap.MODEL_CONFIG_NAMES` 也不用加）。
6. **`writer_server.py`**：按分工没碰，不需要新 writer op——lane 走 registry。
7. **给 P15d 的接线（建议，本片没做）**：`conviction_call_cli.consensus_gap()` 今天自己去查 `consensus_estimate`。等 P11b 并入后，它可以改为读**这条 lane 已经存好的** `SensitivityProjection.consensus_bridge`——形状完全相同，而且顺带把 `variant_view` 要的「我们的数 vs 街上的数」和 driver 排名一起拿到。改动是 `latest(company)` 一次读，不需要改校验。是否要改由集成时定；**本片不动 P15d 的任何文件**。
8. **给 P14a / P14c 的接线（建议）**：事件层判断「某条 driver 变了」时，`SensitivityProjection` 的排名回答「这条 driver 值不值得动模型」，`band` 回答「新值在历史里算不算离谱」。`ForecastRevisionProposal` 的「差异原因」栏可以直接引 `drivers[i].band` 与 `what_if`。

## 8. 开放问题

1. **swing 排序是我改的，不是蓝图写的。** 蓝图说「按 unit move 的弹性」；真数据上它给出并列（§2.1）。我按「对分析师的要求」自行定夺，把弹性保留在记录里、排名改用 swing，并把整条规则连同理由哈希进记录。**如果 owner 或主 agent 认为排名必须是弹性，改 `select_drivers` 的排序键一行 + `SELECTION_RULE` 一段即可**，代价是每家公司的费用 driver 排名变成按 ref 字典序。
2. **一次性事件的极值仍然是极值。** CTSH 税/营业利润的峰 70.79% 是 2025-09-30 单季的事，它不是一个税率。这一版**不做异常剔除**：剔除规则本身是判断（剔掉多少个标准差？谁定？），而没有规则的剔除就是挑数。带把季度和 accession 印在数字旁边，让人能自己判断。如果要做，建议做成 `band_rule_ref` 的第二个版本（`rule:historical-band:2`），并且**在记录里同时保留两条带**，而不是替换。
3. **弹性只跑一个方向。** 链在除收入增速外的每条假设上都是线性的；增速会复利，所以 +1pp 和 -1pp 不完全对称。一个百分点的不对称远小于任何人读这张表的精度，跑一个方向并写明是哪个方向，好过暗示一个从没被验算过的对称性。要双向的话是 `driver_impact` 里多一次 `recompute`。
4. **EPS 桥不了，因为模型没有股数。** 这是 M2 §9 开放问题 5c 原样落在这里：需要 `us-gaap:WeightedAverageNumberOfSharesOutstandingDiluted` 成为一个 driver 角色，而 P13ao 的输入表按单位把 per-share 行排除掉了。这是 M2/M1 侧的一行角色表改动，不是这一片能单方面做的。**在那之前 consensus bridge 只能桥收入和（有估值快照时）目标价。**
5. **scenario 概念仍然没有。** M2 §9 开放问题 5b 问 M3 要 `forecast-model:<company>:<scenario>` 还是自己存一层。**我的回答：两者都不要。** what-if 不是 scenario——它没有作者、没有理由、没有证据，它是一个函数在四个点上的取值。真的 bull/base/bear 是三份**有人签字的假设集**，每份都该走 `revise_assumptions` 拿到自己的 `because` 和 refs；那时候再给 `model_ref` 加 scenario 段。这一片刻意不为 what-if 造 scenario，因为一旦造了，「我们试过什么」和「我们认为什么」就会长在同一棵树上。
6. **历史窗口就是输入表的全部季度，没有单独的 horizon 参数。** 规格的 `horizon.historical_quarters` 已经决定了输入表有多长，再在这里截一次窗只会让带和模型说的不是同一段历史。如果 owner 想要「只看最近 8 个季度的带」，那是规格的事。
7. **`report_consensus` 的返回形状是我定的**（`[{broker, value, refs}]`），因为 P11b 还没并。`w2-consensus` 落地时如果形状不同，改 `consensus_bridge.report_consensus` 的解析一处；`read_consensus` 已经对两种签名都做了尝试。**建议 w2-consensus 的 agent 看一眼这两个函数的期望**，比事后对齐便宜。
8. **DXC 的 swing 超过 100% 是真的，不是 bug。** 一家营业利润率薄的公司，任何一条费用线的历史幅度都能把营业利润吃穿（峰上是 -3.334 亿）。表照实印。要不要在视图里给「swing > 100%」加一句提示，是个显示决定，我没加——加了就等于替读者判断这件事是不是异常。

## 9. 两处不是本片的修复

跑全量时撞上两处 main 上已有的红，都不是这一片的文件，都按「一行、说明是谁的」处理，并即时报给了主 agent：

1. **`bootstrap.SCHEMA_DATABASES` 缺 `conviction_call_schema.sql`**（P15d 合并时只登记了 `rehearse_deploy` 的 `MigrationSpec`）。基线 `62b54fd` 上 6 项测试因此失败——`test_service` 的 INT3 守卫加 5 个 `bootstrap(...)` 调用者。这条守卫存在的全部理由就是「没被应用的 schema 不会让安装失败，它会在几分钟后让某一条 lane 在没人看的心跳里报 OperationalError」，所以让守卫红着就等于把守卫关掉。**主 agent 确认 main 当时已自行修复**，并入时我丢掉了自己那一行。

2. **main `427f684` 不能解析**。`research_event.PAYLOAD_FIELDS` 里 S5 的 `"ir_page_change": frozenset({…` 丢了收尾的 `}),`，于是 P14f 的 `"calibration"` 落进它里面、整个 `MappingProxyType({` 再也没闭合。两个 merge 父节点各自都能解析，括号是在解决冲突时丢的。补之前先验过「丢的真的只是一个分隔符」：修好后 `PAYLOAD_FIELDS` 17 个 kind，`EVENT_KINDS` 与它互为满射，两个父节点都没有贡献被合并丢掉的 kind——两边确实都留住了，只是少了个界符。主 agent 已在 main 上落同一行修复；本分支这一条并回去应是 no-op。

## 10. review 之后改了什么（第二轮）

| # | 改动 | 为什么 |
| --- | --- | --- |
| 1（blocker） | `report_consensus` 只数「有券商名**且**有数字」的行 | 两次分开的计数会让「Alpha 两份 + Beta 无值」发出一个两端都是 Alpha 的区间并叫它 consensus |
| 2 | 假设在预测期内不是一个数时 `impact` 为 `unavailable` | 拿第一季的值加一个点，等于把其余季度压平到它上面，再把压平报成弹性 |
| 2b（连带） | 排序改成「swing 或 elasticity 有其一即入选」 | 否则第 2 条会把一条被改过的 driver 整个踢出排名，而它可能正是最要紧的那条 |
| 3 | `held_flat` / `quarters_held` + `SELECTION_RULE` 与视图明说 | 「谷」不是谷那个季度再来一次，是整段预测期都待在那儿 |
| 4 | 每个极值带 `runner_up` | 让 CTSH 那种一次性极值看得见，而不需要一条没人同意的剔除规则 |
| 5 | `SELECTION_RULE_REF` = `rule:swing-rank:1` | ref 要说出它按什么排 |
| nit | `content_hash` 改为对**校验后**的 wire 取 | 校验器会规范化（P15d 的 `_text` 会 strip），否则存进去的哈希不是它旁边那段 JSON 的哈希，而回读校验就变成拿一个数和它自己比 |
| nit | `inspect.signature` 取代 `except TypeError` 重试 | 见 §3 |
| nit | `open_periods` 去掉 realised 过滤 | `actualize_model` 会把已实现季度移出 `forecast_periods`，`validate_forecast_model` 也拒绝同时出现在两边的记录——那个过滤只会在一个存不进去的记录上生效。**顺带发现我原来的两条测试就是建在那种非法形状上的**，已改成 authority 真会产生的形状，并加一条断言证明重叠的记录会被 `publish` 拒绝 |

这些改动都动了 `SELECTION_RULE_HASH`，因此也动了每条 projection 的 `content_hash`——今天不要紧，live 上一条都还没发。

## 11. 第三轮：P11b 并入后的对齐

`git merge main` 把 P11b（`consensus_estimate.py` / `street_estimate.py`）带了进来，两处形状与我并入前的假设不同，都已对齐并补了测试：

| 差异 | 我原来的假设 | P11b 实际 | 处理 |
| --- | --- | --- | --- |
| `latest_consensus` 返回 | `{metric, period, value, unit, refs}`，`ours` 由我算 | 已经是算完的 gap 行，`ours` 来自同一个 `ForecastModelAuthority` | 透传（校验后），不重算；不再用本模块词表二次过滤 |
| `report_consensus` 签名 | `(store, company, metric, period)` | `(store, company_ref, *, as_of)`，只发目标价 | 按它的写法调用；metric 检查移到本模块，问非目标价时如实拒绝 |

两处如果不改都不会报错，只会**安静地退化成「桥不了」**——第一处每一行都因为「没有 value」被跳过，第二处每次调用都 `TypeError` 被吞成 unavailable。这正是「缺就是缺」的降级策略最危险的地方：它让接错线看起来和没有数据一模一样。所以补了 `test_the_real_p11b_readers_are_called_the_way_they_are_written`，直接对**树里真实的函数**断言签名，而不是只对 fake 断言。
