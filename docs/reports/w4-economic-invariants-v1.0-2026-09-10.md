# W4 经济不变量层 v1.0

日期：2026-09-10
分支：`w4-economic-invariants`（worktree `~/Projects/dalton-w4-economic-invariants-worktree`），基线 main `ba99ef9`
规格：[并行开发计划 v1.0 §4 规则 1–14](parallel-development-plan-v1.0-2026-09-09.md)、
[Chem 复盘 §3.2 与 §5 第 4 条](chem-retrospective-implications-v1.0-2026-09-10.md)
一句话来源：Chem 的 Linde 工作簿过了 42 项结构检查，仍然把亏损情景的 15 年 IRR 报成 0.0%，
因为二分法上下界写死 `[0.0, 1.0]`，负 IRR 被夹到下界，而**下界看起来像一个答案**。

---

## 0. 一句话

新增一层**不需要模型就能判断「这个数不可能是真的」**的检查（`economic_invariants`），
挂在预测版本发布、敏感性投影发布、估值快照发布三条路上；
任何一条不变量失败 = **整份产出记为 `unavailable` + 逐条理由**（永不修补、永不发部分），
理由在 cockpit 的公司卡与模型页上可见。

---

## 1. 为什么 42 项检查全 OK 还是错的

Chem 那 42 项是**模型对自己的检查**：这一格是否引用那一格、这一列是否等于那一行。
它们抓不到夹边，因为**夹住的数在算术上完美**——它只是在经济上不可能。
本切片补的是另一类：不看模型怎么算出来的，只问「一家公司能不能是这样」。

六条不变量，全部从冻结公式推出来，不是拍脑袋定的：

| 不变量 | 内容 | 推导 |
| --- | --- | --- |
| `direction_consistency` | 成本占比不变时，营业利润与收入同比例；且隐含毛利非负时，收入涨营业利润不得跌 | `compute_results` 的链式代入：`operating_income[k] = revenue[k] * (1 - cost_share[k] - Σ opex_share[k])`。占比全部持平 ⇒ 营业利润是收入乘一个常数 |
| `assumption_band` | 每条假设落在该 driver 自己 measure 的历史 min/max 内；或显式带 `outside_band` **并写一句理由** | 带用 P13-M3 已冻结的 `measure_series` / `MIN_BAND_POINTS`，不另立一套会和敏感性表打架的带 |
| `rate_domain` | 毛利率可负、不得超过 1；增长率不得 ≤ −1；税率必须在 `[0,1]` | 毛利率来自 `line/revenue`；增长率 ≤ −1 会让下一季的 `revenue[k-1]` 为零或负；税率来自 `net = op − tax` |
| `segment_sum` | 有合并行也有分部行时，分部之和 = 合并（含四舍五入容差） | 直接用 `sec_financials_normalise` 已有的 `dimension_axis` / `dimension_member` 形状；**按轴分组**，业务分部与地区分部不相加 |
| `period_basis` | 一条 line 内不得混用单季与累计（YTD）；且同一季不得同时有活的 estimate 与活的 actual | `company_model_series.period_kind` 是唯一能区分「Q2」和「上半年」的东西（两者 `period_end` 相同）；后半条是 P14f 的 `superseded_by` 约定 |
| `solver_bounds` | 求解量必须**严格落在**搜索区间内，或报 `unbounded` 且不带值；落在边界上 = 拒绝 | Linde 那条本身。落在自己的下界上不是根，是「没找到根」 |

**方向检查为什么要分两句。** 比例那句（`op[k]·rev[k-1] == op[k-1]·rev[k]`）在精确算术下更强，
方向那句（收入涨、毛利非负、利润跌）才是读者需要看见的那句话，所以两条都发，方向那条排在前面。
亏损公司卖得越多亏得越多**不算失败**——那是公式在正常工作，拒绝它就成了不变量在替公式表态。

---

## 2. 做了什么

### 2.1 新模块 `src/dalton_core/economic_invariants.py`（1.4k 行，含文档）

- `check(subject) -> tuple[InvariantResult, ...]`：**纯函数**。不开库、不看表、不取时钟。
  返回冻结顺序的六条结果，每条是 `pass` / `fail` / `not_applicable`，
  `fail` 必须带 findings（`InvariantResult.__post_init__` 强制）。
  **空 subject 六条全是 `not_applicable`，不是 pass**——「没输入就算过」正是那第 42 项检查。
- `float` 进来直接拒绝（`EconomicInvariantValidationError`）：浮点数到这一层时，要比较的位数已经丢了。
  引擎用 Decimal 的地方本层全程 Decimal，精度 60，与 `model_forecast_driver._PRECISION` 一致。
- 三个 subject 构造器把三种产出翻成同一个规范形状：
  `forecast_subject` / `projection_subject` / `valuation_subject`；
  `segment_groups` 把 filed statement rows 重组成「合并行 + 各分部」。
- `InvariantReport`：`status` 为 `available` / `unavailable`，`reasons` 是 `<invariant>: <finding>` 的扁平列表。
- `EconomicInvariantAuthority` + `economic_invariant_schema.sql`：
  append-only、`content_hash`、三触发器（`dalton_economic_invariant_authorized()` / no-update / no-delete）、
  一公司一产出类型一条版本链、写后读回校验。
  **只记两种事**：一次拒绝，和「清掉一次挂着的拒绝」的那次通过。
  链上没挂拒绝时的通过不写行——否则这条链就不再是「什么时候拒绝过、为什么」的记录了。
- `gate(store, report)`：**先写后抛**。拒绝先落库，再抛 `EconomicInvariantRefused`，
  这样调用方吞不吞异常都不影响「有一条数没出现，且有记录说为什么」。

### 2.2 三条发布路上的闸门

| 路径 | 位置 | 失败后 |
| --- | --- | --- |
| 预测版本 | `ForecastModelAuthority.publish`，在 `validate_forecast_model` 之后、事务之前 | 抛 `EconomicInvariantRefused`；`forecast_model_versions` 一行不写 |
| 敏感性投影 | `SensitivityProjectionAuthority.publish`，在 content_hash 之后、事务之前 | 同上；`sensitivity_projections` 一行不写 |
| 估值快照 | `ValuationSnapshotAuthority.publish_snapshot`，在 metrics 算完、事务之前 | 同上；`valuation_snapshot_versions` 一行不写 |

三个 publish 都多了 `statement_rows=` 与 `solver_results=` 两个可选入参：
分部行与求解量不是这三条记录自己带的，得由调用方递进来，这是集成时要接的线（见 §4）。

### 2.3 假设多了一个字段：`outside_band`

`_ASSUMPTION_FIELDS` 加 `outside_band`，值是 `None` 或 `{"reason": "<一句话>"}`。
理由少于三个词直接拒（`_normalize_outside_band`）——「because」不是理由。
`revise_assumptions` 的 change 里可以直接带 `outside_band`。

**旧记录仍读得回**：`_normalize_assumption` 在闭合形状检查前 `setdefault` 这个键，
所以字段出现之前写的记录不会因为缺一个它当时不可能有的字段被拒。

这一条不是「拒绝越界假设」——分析师大部分价值就在历史里没有的那条假设上。
它拒绝的是**沉默的**越界：带没带一句话，是「有人论证过」和「没人论证过」的区别。

### 2.4 cockpit 能看见

- `CockpitPlane._invariants(core)`：读每条链的**链头**，只留 `unavailable` 的。
  没有这张表的旧 Core 返回 `{}`，卡片退化成原来的卡片而不是错误页。
- 公司卡新增 `invariants`（按产出类型分键，含 `reasons` 与逐条 `failed`），没被拒时是 `{}`。
- 模型页 `company_model()` 新增 `invariants`：
  被拒的版本根本没进表，页面否则会照常显示上一版而**不说有更新的一版被拦下来了**。
- 不变量中文名在 `INVARIANT_LABELS`，产出类型中文名在 `OUTPUT_KIND_LABELS`，都在模块里，cockpit 只读不定义。

### 2.5 lane 与 CLI 的失败分类

`company_model_forecast_cli` / `forecast_sensitivity_cli` 捕获 `EconomicInvariantRefused`，
报 `status: succeeded` + `forecast_status: "unavailable:economic_invariants"` + `failure_reason`（理由拼接）。
**这一跑是成功的**：闸门起了作用，没有一个不可能的数被发布。

两条 lane 的 `failed` 判定从 `startswith("refused:")` 扩到 `("refused:", "unavailable:")`——
否则同一个 digest 会每个 tick 被重试、每次都以同样的方式被拒。

`earnings_calibration.actualize_for_company` 与 `event_judgement`（`revise_forecast` 动作）
也各加一条 `except`，返回 `status: unavailable` + 理由，而不是让异常冒成 lane 崩溃。

### 2.6 登记

- `bootstrap.py` schema 表：`("economic_invariant_schema.sql", None)`
- `scripts/rehearse_deploy.py` `CORE_MIGRATIONS`：`MigrationSpec("economic_invariant_schema.sql", "dalton_core.economic_invariants", "EconomicInvariantAuthority", "core")`
- 没有新 lane，所以 `LANE_MODULES` 与 `REGISTRY_LANE_LABELS` 不动；没有新治理记录，`install.sh` 不动。

---

## 3. 没做什么

1. **没有实现任何求解器。** 全仓今天没有二分法、没有 IRR、没有 DCF 求根。
   `solver_bounds` 是**先于**求解器落地的闸门加上一个入参形状
   （`{ref, label, status: solved|unbounded, value, lower_bound, upper_bound}`）。
   规则 14 的做法：Linde 那次事故先产出检查，再允许写会踩它的代码。
2. **分部行不是自动接上的。** `company_model_inputs` 在建模型输入表时就把 `is_breakdown` /
   `dimension_axis` 的行滤掉了（它有它的理由：只加总不加明细）。
   所以 `segment_sum` 在 `publish(statement_rows=...)` 不传时是 `not_applicable`，不是 pass。
   接线见 §4。
3. **估值快照没有「率类」检查。** 倍数不是率，没有域；`fcf_yield` 虽是 fraction 但没有可推导的上下界，
   与其猜一个不如不查。估值这边真正生效的是 `period_basis`（见 §4 第 3 条为什么这条重要）与 `solver_bounds`。
4. **没有 cockpit HTML 的改动。** 数据在 `overview()` 与 `company_model()` 的 wire 上，
   `cockpit_control.html` 的渲染不是本切片的文件。
5. **没有「不变量豁免」入口。** 没有 override、没有 force 参数。
   一条不变量能被绕过，它就不是不变量。

---

## 4. 集成时要接的线

1. **分部行**：`company_model_forecast_cli.run_model_forecast` 里，
   模型 publish 时把该公司该期的 filed statement rows（含 breakdown）传成 `statement_rows=`。
   行的形状就是 `sec_financials_normalise` 的输出（`concept` / `period_start` / `period_end` /
   `value` / `is_breakdown` / `dimension_axis` / `dimension_member`），`segment_groups` 直接吃。
2. **求解量**：任何后续切片（DCF、IRR、隐含增长率）算出派生量后，
   把 `{ref, status, value, lower_bound, upper_bound}` 一起递给对应的 publish。
   报 `unbounded` 时 `value` 必须是 `None`——带着值的 `unbounded` 也是拒绝。
3. **估值的 `fundamental_windows`**：`_role_input` 今天检查「trailing_sum 恰好四个分量、不重复」，
   但**不检查每个分量是不是一个季度**。四个都落在季末、其中一个是九个月 YTD 的窗口，
   今天能过全部检查并产出一个按重叠倍数偏掉的 price/sales。这条现在被 `period_basis` 拦住了
   （测试 `test_a_year_to_date_figure_inside_the_trailing_year_refuses_the_snapshot`），
   但**喂窗口的那一侧仍应自己按季度取数**，闸门是最后一道不是第一道。
4. **cockpit HTML**：公司卡的 `invariants` 与模型页的 `invariants` 需要一个渲染位置。
   建议就渲染在被拒产出本该出现的地方（模型卡 / 敏感性表 / 估值行），
   而不是单开一个「校验」区——理由要长在数缺席的那个位置上。
5. **旧记录**：`outside_band` 缺省为 `None` 且读回兼容，live 上已有的预测版本无需迁移。
   但下一次 `revise_assumptions` 把某条假设推出历史带时，会被要求补一句理由，这是预期行为。

---

## 5. 验收

全量：

```
Ran 5451 tests in 542.609s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，2026-09-10，
worktree `~/Projects/dalton-w4-economic-invariants-worktree`，提交 `89ec793`。
唯一的 skip 是既有的那条，不是本切片引入的。）

新增 `tests/test_economic_invariants.py`，69 条，覆盖：

- 六条不变量各自的 pass / fail，以及「没输入不算 pass」；
- 夹边 IRR 的四种写法：夹在下界（拒）、严格在区间内（过）、`unbounded` 不带值（过）、`unbounded` 还带值（拒）；
- 带的四种情形：带内、带外沉默（拒）、带外带理由（过）、带外理由是空白（拒），
  以及「历史里有这个值却标了 outside_band」也拒——否则读者会开始无视这个标；
- YTD 混进季度序列、以及同一季同时有活的 estimate 与活的 actual；
- 分部之和差 5%（拒）、差 0.005（容差内，过）、两个轴不相加；
- 三条发布路各自的闸门：被拒时**表里一行都没写**、拒绝落库、同一次拒绝不写第二行、
  换成带理由的版本后发布成功且拒绝被清掉（链上读作 `unavailable → available`）；
- 触发器：verdict 行不可 UPDATE / DELETE，未授权 INSERT 被拒；
- cockpit：公司卡带理由、没被拒时是 `{}`、模型页说有更新的一版被拦下、旧 Core 退化成 `{}`；
- lane：`unavailable:` 与 `refused:` 一样进 held，不会每 tick 重试同一个 digest。

改到的既有测试**一处**：`tests/test_model_forecast_driver.py` 里那条把收入增长改到 0.05
（低于该公司填报过的每一个季度）的 fixture，现在按新规则补了一句 `outside_band` 的理由。
这正是规则要求的行为，不是为了让测试变绿而放宽的检查。
