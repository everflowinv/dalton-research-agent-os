# P13-M2 driver 模型与预测行存储 v1.0

日期：2026-09-09（v1.1：并入 Wave 0 的 main `888a814`，按 review 修 B1 / S1–S5，登记 LaneSpec）
分支：`wave1c-forecast-lines`（worktree `dalton-wave1c-forecast-lines-worktree`），基线 main `888a814`
作者：Wave 1 Agent C（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 C 线、[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) 5.2 Phase 13、PROJECT_STATUS 待办第 2 条、owner 两次追加裁决（版本化规则、"版本化是机制不是触发器"）

---

## 0. 一句话

规格 × 序列的 `ModelInputTable`（P13ao）之后，模型层现在有了**能存下来、能对账、能被人反驳、并且知道自己什么时候错了**的东西：`ForecastModelVersion` 一条记录里放 driver → assumption → result 三层，每个 result 格都能顺着 assumption 和 filing cell 走回一份 accession；一个季度被 filing 覆盖之后，**估计值不被覆盖**，它留在原地被标成 `superseded_by` 实际值。live 只读副本上五家跑通四家（ACN / CTSH / EPAM / DXC 从收入到净利润全链条 + 36 条预测行），IBM 如实拒绝。

## 1. 做了什么

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `forecast_driver_schema.sql` | 新增 | `forecast_model_versions`：append-only、`content_hash`、`dalton_forecast_model_authorized()` 授权触发器、禁改禁删、按公司的版本链 |
| `model_forecast_driver.py` | 新增 | authority + 三层记录 + 默认假设生成器 + `actualize_model` / `revise_assumptions` / `replay_cell` + 模型起草的 hook |
| `company_model_forecast.py` | 新增 | 编排：规格 + filing → 输入表 → 模型版本 → 对账层能读的预测行 |
| `company_model_forecast_cli.py` | 新增 | lane child，`summary.json`，状态 `published` / `duplicate` / `refused:<reason>` / `nothing_to_model` |
| `mission_model_forecast_lane.py` | 新增 | 无队列协调器，先结算上一个 child |
| `model_forecast_launcher.py` | 新增 | `LaneChildLauncher` 子类，digest = 公司 + 规格哈希 + 输入表哈希 + generator |
| `model_forecast.py` | 改（共享） | 第二条冻结公式 `formula:driver-model:1`；`quarter_after` 公开；`driver_model_version_ref(line)` 供对账层读回 |
| `company_model_report.py` | 改（共享） | `render_forecast_model`，`--forecast` 开关 |
| `pyproject.toml` | 改（仅 `[project.scripts]`） | `dalton-model-forecast` |
| `lane_registry.py` | 改（一行） | `LANE_MODULES` 加 `dalton_core.mission_model_forecast_lane` |
| `tests/test_lane_registry.py` | 改 | 新增 `POST_MIGRATION_LANES` / `POST_MIGRATION_TICK_ORDER` / `POST_MIGRATION_LAUNCHER_KWARGS`，让「迁移前的字面量」保持是迁移前的字面量 |
| `tests/test_model_forecast_driver.py`、`tests/test_company_model_forecast.py`、`tests/test_mission_model_forecast_lane.py` | 新增 | 75 项（46 / 15 / 14） |

**没有碰**：`writer_server.py`、`coverage_mission.py`（及其 schema）、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、cockpit 两个文件、`PROJECT_STATUS.md`、`tests/test_service.py`、`pyproject.toml` 的 package-data。`company_model_inputs.py` 与 `company_model_series.py` 一个字没改——join 层够用。

## 2. 记录形状

一条 `ForecastModelVersion`（略去重复项，真实记录 ACN 约 90KB）：

```json
{
  "schema_version": "0.1",
  "id": "forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:2",
  "model_ref": "forecast-model:company:sec-cik:0001467373",
  "version": 2, "prior_version_ref": "forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:1",
  "change_reason": "filing_actual",
  "evidence_refs": [{"kind": "filing", "ref": null, "concept": "us-gaap:Revenues",
                     "period_end": "2026-08-31", "accession": "0001467373-26-000099"}],
  "decision": null,
  "company_ref": "company:sec-cik:0001467373",
  "spec_ref": "company-model-spec:8fb8...", "spec_hash": "…", "inputs_hash": "…",
  "unit": "usd", "currency": "USD",
  "history_periods": ["2025-11-30", "2026-02-28", "2026-05-31", "2026-08-31"],
  "realised_periods": [{"start": "2026-06-01", "end": "2026-08-31", "calendar": "company:fiscal", "kind": "quarter"}],
  "forecast_periods": [{"start": "2026-09-01", "end": "2026-11-30", …}, …],
  "statements": {"income": "required", "balance": "supporting", "cash": "not_material"},
  "formula_ref": "formula:driver-model:1", "formula_hash": "…",
  "generator_ref": "rule:trailing-carry-forward:1",

  "drivers": [{
    "ref": "concept:us-gaap:Revenues", "kind": "revenue", "label": "Revenues",
    "concept": "us-gaap:Revenues", "statement": "income", "unit": "usd",
    "status": "filed", "role": "revenue", "spec_rows": ["consulting_revenue"],
    "note": null,
    "history": [{"concept": "us-gaap:Revenues", "period_start": "2026-03-01",
                 "period_end": "2026-05-31", "value": "18718144000",
                 "basis": "reported", "accessions": ["0001467373-26-000032"]}, …]
  }, …],

  "assumptions": [{
    "ref": "assumption:concept:us-gaap:Revenues@2026-11-30:estimate",
    "driver_ref": "concept:us-gaap:Revenues",
    "period": {"start": "2026-09-01", "end": "2026-11-30", …},
    "measure": "quarterly_growth", "value": "0.001500000000", "unit": "ratio",
    "kind": "estimate",
    "because": "the average of the 4 quarter-on-quarter changes filed between 2025-08-31 and 2026-05-31 (0.15%), carried forward unchanged; an average of quarters carries no seasonality, so a quarter unlike the ones averaged will be wrong by however much it is unlike them",
    "refs": [{"kind": "input_cell", "ref": null, "concept": "us-gaap:Revenues",
              "period_end": "2026-05-31", "accession": "0001467373-26-000032"}, …],
    "provenance": {"rule_ref": "rule:trailing-carry-forward:1",
                   "work_order_ref": null, "decided_by": "automation:coverage-mission"},
    "superseded_by": null
  }, …],

  "results": [{
    "ref": "result:revenue", "role": "revenue", "label": "Revenue", "unit": "USD",
    "formula": "revenue[k] = revenue[k-1] * (1 + growth[k])",
    "driver_ref": "concept:us-gaap:Revenues", "status": "computed", "reason": null,
    "cells": [
      {"ref": "result:revenue@2026-08-31:estimate", "kind": "estimate",
       "period": {…}, "status": "computed", "value": "18746348496.00000000",
       "reason": null, "superseded_by": "result:revenue@2026-08-31:actual",
       "assumption_refs": ["assumption:concept:us-gaap:Revenues@2026-08-31:estimate"],
       "input_cell_refs": [{"kind": "input_cell", "concept": "us-gaap:Revenues",
                            "period_end": "2026-05-31", "accession": "0001467373-26-000032", "ref": null}],
       "result_refs": []},
      {"ref": "result:revenue@2026-08-31:actual", "kind": "actual",
       "period": {…}, "status": "computed", "value": "17200000000.00000000",
       "reason": null, "superseded_by": null, "assumption_refs": [],
       "input_cell_refs": [{"kind": "input_cell", "concept": "us-gaap:Revenues",
                            "period_end": "2026-08-31", "accession": "0001467373-26-000099", "ref": null}],
       "result_refs": []}
    ]
  }, …],

  "mission_version_ref": "coverage-mission-version:us-it-services:1",
  "actor_ref": "automation:coverage-mission",
  "body_hash": "…", "content_hash": "…"
}
```

几条设计上的选择，每条都有一个具体的失败模式在背后：

- **driver 按 `basis_concept` 键，不按规格行键。** IBM 把一条 filed cost 拆成四行、ACN 拆成三行，filing 只报一个总数。driver 拿总数、`spec_rows` 列出必须加总到它的行、`status = share_of_filed`，**拆分不被发明**。
- **role 是冻结白名单 `CONCEPT_ROLES`，且必须来自对的那张表**（`ROLE_STATEMENTS`）。表里**只有分项、没有小计**——没有 `us-gaap:OperatingExpenses`、没有 `CostsAndExpenses`——因为这一层唯一可能犯了还发现不了的算术错误就是把小计和它自己的组成部分相加。`us-gaap:DepreciationAndAmortization` 只有报在**利润表**上才算营业费用；同一个 concept 报在现金流量表上是别的东西，当成费用会把已经含在成本里的折旧再减一遍。
- **值全是 Decimal**，计算在 `localcontext(prec=60)` 里、每一步 `quantize(1e-8, ROUND_HALF_UP)`，所以重算逐字节相同（有测试）。记录里没有 float。
- **`unavailable` 有理由、没有值。** 缺一个 concept 就是缺，不是零也不是猜。

### 版本机制（owner 追加裁决）

- **cell 有 `kind`**：assumption ∈ {`estimate`, `human`, `actual`}，result cell ∈ {`estimate`, `actual`}。filing 到了之后**估计值原地保留**并标 `superseded_by: <actual cell ref>`；对账层要评的是估计值，之后 guidance_style 校准要读的是「我们当时怎么想 / 结果怎样」。
- **`change_reason` 是闭合词表** `filing_actual` / `driver_event` / `assumption_review` / `evidence_thicker` / `human_revision`，且 `evidence_refs` 必须非空（否则 `ForecastModelValidationError`，CLI 报 `refused:`）；`driver_event` 还必须给 `decision` 字符串（Wave 3 放 `DECISION_VOCABULARY` 的五个词之一）。第一版用 `evidence_thicker` + 该模型所依赖的 accession——**见 §7 开放问题 1**。
- **两个显式入口，authority 自己从不决定发版本**：
  - `actualize_model(prior, table)`：只把已被 filing 覆盖的季度写成 actual，**未来季度一个字不动**（有测试逐格比对）。没有新覆盖的季度就返回 `None`。
  - `revise_assumptions(prior, changes, *, change_reason, evidence_refs, actor_ref, decision=None)`：改指定 (driver, period) 的假设并重算下游。已被 filing 回答的季度不可改。
- **重算不会偷偷 rebase。** 收入链的起点由 `chain_base` 给出：优先用**上一版对上一季度的估计值**，而不是刚报出来的实际值。否则「改一条假设」会连带把整条预测线挪到财报数上，读版本 diff 的人分不清哪个是决定、哪个是副作用。**要不要 rebase 到实际值，是一个决定**，走 `revise_assumptions`。
- **`published` 不是终态**：它是一次运行的状态。`replay_cell(versions, "result:revenue", "2026-08-31")` 回答「第 N 版对那个季度估了多少、什么时候说的、最后是多少」。
- **丢失更新会被拒绝。** `actualize_model` / `revise_assumptions` 在 body 上盖一个 `source_version_ref`（第一版盖 `None`），`publish` 发现它不等于链头就抛 `ForecastModelConflict`。没有这一条，拿着 v1 去改 Q2、而 v2 刚改过 Q1，会追加一版**悄悄回退 Q1**、而且带着第二个调用者的 `change_reason`——记录会为一个没人做过的改动写上 `assumption_review`。这个 key 不是记录字段，publish 读完就丢，不进 hash。
- **公司名用 `content_hash(company_ref)[:32]`，不用 ref 的最后一段。** live 里 `company:sec-cik:001688568` 和 `company:sec-cik:0001467373` 并存（前者少一位），两家公司如果最后一段撞上就会写进对方的版本链，而且看不出来。

## 3. 冻结的公式与词表

| 名字 | 值 | 位置 |
| --- | --- | --- |
| 模型公式 | `formula:driver-model:1`（`DRIVER_FORMULA_REF` / `DRIVER_FORMULA_HASH`） | `model_forecast.py`，因为预测行 authority 必须知道它接受哪些冻结公式 |
| 默认生成器 | `rule:trailing-carry-forward:1`（`GENERATOR_REF`） | `model_forecast_driver.py` |
| 记录 schema | `0.1` | 同上 |

公式语义（进 hash）：收入 driver 按 trailing 季度环比增速逐季复利；费用 driver 是预测收入的份额；`gross_profit = revenue − cost_of_revenue`；`operating_income = gross_profit − Σ operating_expense`；`net_income = operating_income − income_tax`（有 tax driver 时；否则 `operating_income × net_income_share`）；`free_cash_flow = operating_cash_flow − capital_expenditure`（仅当规格给现金流量表 required/supporting）；每个 result 格列出用到的 assumption 与 filing cell；没有 filed 历史的 driver 不产生假设，依赖它的结果 `unavailable`。

默认假设生成器：环比增速取**相邻**（期末相差 80–100 天）四对的算术平均；费用份额取最近四个季度 expense/revenue 的平均；tax 取 tax/operating_income 的平均（历史营业利润用**同一条链**算出来，不读 filed 的 operating income——用别的定义算出来的比率不会重放）。`because` 点名窗口的**两端**（增速的窗口从第一对里靠前的那个季度算起，只说后一端会把窗口说短一个季度），并且**在 `because` 里明说自己对季节性是瞎的**——读记录的人必须在数字旁边看到这句，而不是在一份他可能永远不会打开的报告里。

**分母变号就不给假设。** `trailing_share` 在窗口内 base 变号时直接返回 `None`：一家上季亏、这季盈的公司，税率是「负若干」和「正若干」，两者的平均不是税率，是亏损和盈利离得多远的产物。于是那些季度没有假设，依赖它们的行 `unavailable` 并说「its trailing history gives no usable rate」——一个人能盯着看的洞，好过一个没人能辩护的比率。live 上这条**真的命中了 DXC**（见 §7）。

资产负债表本轮不做（规格标 required 也不做，见开放问题 4）。

模型起草 hook：`draft_assumptions(drivers, periods, *, drafter, decided_by, work_order_ref=None)`。三道检查后整条拒绝、不修补：draft 只能命名本模型里有 filed 历史的 driver；`refs` 只能引用该 driver 真有的季度；`kind` 只能是 `estimate`（模型永远不能写 `human`）。

## 4. 与对账层的兼容

`forecast_reconciliation.py` **一个字没改**。做法：把 result 里 `METRIC_BINDINGS` 认识的角色（今天只有 `revenue` → `metric:revenue-usd`）作为普通 `model_forecast_line_versions` 行发出去：

- `value_kind = derived_deterministic`、`formula_ref = formula:driver-model:1`；
- `scenario_version_ref` = 那一版 `forecast-model-version:` 的 id，`scenario_version_hash` = 它的 `content_hash`——**driver 模型行的「scenario」就是模型版本**，这也是唯一能重放它的东西。校验强制这个 ref 必须以 `forecast-model-version:` 开头；
- `base_input_version_ref` / `growth_input_version_ref` / `model_run_version_ref` 必须为空（它确实不依赖 Model Input Ledger）；
- `unit = "one"`、`currency = "USD"`，因为对账层按 `SCALE_FACTORS` 把 actual 缩放到行的单位；filed 单位不是 `usd` 就拒绝发行、不假设。

**没有新增字段**，所以既有的行、既有的 hash、对账层已经读过的东西全都不变。对账层要读 driver ref 时用新加的 `model_forecast.driver_model_version_ref(line)`（非 driver 公式返回 `None`），有测试。

只发**未来季度的 `estimate` 格**：actual 不是预测；已实现季度的估计值在它还是预测时就已经发过了，换一版模型再发一次等于悄悄改掉对账层马上要评的那个数。值没变也不发（状态 `unchanged`）。

端到端测试（`ReconciliationTests`）：模型发 1,464.1m，Claim 报 1,500.0m → `pending_pairs` 找到、`reconcile` 给出 `deviation_percent = 2.4520`、`band = notable`，并能从 reconciliation 走回模型版本。

## 5. 接线：已经登记好了

Wave 0 的 registry 已在 main 上，所以这条 lane 自己声明了自己（`mission_model_forecast_lane.py` 末尾），`lane_registry.LANE_MODULES` 只加了一行。`writer_server.py` / `bounded_planner_driver.py` / `macos_launchagent.py` 一个字没碰。

```python
LANE = register_lane(LaneSpec(
    operation="dispatch_company_model_forecast",
    order=95,                       # 规格 lane（90）之后：它决定这个模型靠哪些 driver
    driver_key="company_model_forecast",
    handler=dispatch,
    init_kwarg="model_forecast_launcher",
    argparse=add_arguments,         # --model-forecast-lane，一个开关
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
))
```

三点值得说：

- **`ForecastModelAuthority` 在 handler 里建**（第一次 tick 时连同 coordinator 一起进 `server.lane_state`）。构造它就是安装它的 schema，writer 没有别的理由知道这条 lane 存了版本化模型。
- **`argv_fragment` 以 `state/core.sqlite` 存在为条件**。别的 lane 都是「装了治理记录 / 模型配置才开」，这条什么都不需要装；但「没装就不开」这个不变量还是要守住（registry 的测试也这么断言），所以它以自己唯一真正需要的东西为条件——一个能读规格和 filing 的 Core。
- **`param_fields` 为空**：tick 不带参数。`--company-ref` 只在手工跑 child 时用，而且**也要过 mission universe**（见 §6.7）。

`dispatch_once()` 的返回状态：`launched` / `idle` / `held` / `busy` / `rejected` / `unconfigured` / `unavailable`，都带 `settled`（上一个 child 的结算）。`test_lane_registry` 的「迁移前字面量」三个 pin 我没有就地改写，而是加了 `POST_MIGRATION_*` 三个集合并在断言里并进去——那些字面量的意义是「registry 之前 writer 手写的是什么」，把新 lane 塞进去就把那份记录改坏了。**其他 Wave 1 agent 如果也登记 lane，这三处是合并冲突点。**

## 6. 集成待办

1. **授权：不需要新 mission 版本。** live manifest `deploy/phase9/p9a-us-it-services-mission-v1.json` 的 `may_write` 已含 `forecast_line` 与 `model_run`。CLI 检查的是 `forecast_line`；缺了就 `refused:the mission does not grant the forecast_line write scope`（有测试）。`model_run` 目前没用到——driver 模型不记 model run（它没有 model 调用）。
2. **打包**：已由 Wave 0 的 `*_schema.sql` glob 解决，`forecast_driver_schema.sql` 自动进 wheel（`test_packaging` 在合并后的分支上通过）。
3. **cockpit「company model」页**要的字段（都在 `model_readiness(record)` 里，无需新算）：`forecast_quarters`、`realised_quarters`、`drivers` / `drivers_with_assumptions` / `drivers_without_history` / `drivers_without_a_role`、`results_computed` / `results_partial` / `results_unavailable`、`assumption_kinds`（estimate / human / actual 计数）、`actual_cells`、`superseded_estimates`。再加记录本身的 `change_reason` / `decision` / `evidence_refs` 与版本链。纯文本视图可以直接嵌 `company_model_report.render_forecast_model(record)`。
4. **CLI 入口**：`dalton-model-forecast`（已加 `[project.scripts]`）。只读视图 `dalton-model-inputs --forecast [--json]`。
7. **`--company-ref` 也过 universe**：手工跑一个不在 mission universe 里的公司会被 `refused:<ref> is not in this mission's universe` 拒绝。mission 是「这套自动化被允许对哪些公司工作」的唯一声明，一条能绕过它的手工路径就是一条用 mission 自己的 principal 给没人批准的公司写模型的路。
5. **install.sh / 连接器 / 模型配置**：无改动需求。这条 lane 不调模型、不连外部源、没有自己的模型配置名（所以 `cockpit_model.PURPOSES` 与 `raise_day_budget_cap.MODEL_CONFIG_NAMES` 也不用加）。
6. **`writer_server.py`**：按分工没碰。这条 lane 不需要新 writer op——`publish_forecast_line` / `extend_growth_forecast` 那两个 op 走的是人类/增长外推路径，driver 模型直接用底层 authority 函数。

## 7. live 只读副本冒烟（`/tmp` 拷贝，未触碰 live）

`cp /private/tmp/dalton-ro/core.sqlite /tmp/dalton-smoke-c/`，逐公司跑 child：

| 公司 | 结果 | driver（有假设/总数） | 预测季度 | 预测行 | 算出来的 result |
| --- | --- | --- | --- | --- | --- |
| ACN | published v1 | 5 / 16 | 8 | 8 | revenue、cost_of_revenue、gross_profit、S&M、G&A、operating_income、income_tax、net_income |
| CTSH | published v1 | 6 / 15 | 8 | 8 | 同上（费用行为 SG&A、D&A、Restructuring） |
| EPAM | published v1 | 5 / 16 | 8 | 8 | 同上（SG&A、D&A） |
| DXC | published v1 | 5 / 17 | 12 | 12 | revenue、cost_of_revenue、gross_profit、三条费用行、operating_income；**税与净利润 unavailable** |
| IBM | **refused**：`no revenue driver rests on a filed concept with quarterly history` | 0 | — | — | — |

四家都有收入线与 margin 线（蓝图 Wave 1 验收项之一），共 36 条预测行；`free_cash_flow` 五家都 `unavailable`（规格都没绑现金流量表的 concept）。

**DXC 的税与净利润被变号守卫挡下来了，这是真数据上的第一次命中**：DXC 的模型营业利润在 trailing 窗口里由负转正，于是「税/营业利润」的平均没有意义，`income_tax_expense` 报 `no usable rate`、`net_income` 跟着 unavailable。它上面的收入到营业利润全部照常。这正是这条守卫存在的理由：一个由亏转盈的公司，平均税率是个假象。

IBM 的拒绝**不是 bug，是 P13ao 已经发现的事**：IBM 的规格把每一条收入 driver 都绑到 null（adoption、price mix、rate mix 确实不在 GAAP 里），于是这个模型没有 filed 顶线。冒烟第一轮还抓到一个真的 bug：**IBM 排在最前面，选择器每次都返回它，协调器 hold 住之后另外四家永远排在后面**——正是规格 lane 上线第一个心跳抓到的那个形状。已修：`pending_companies()` 返回全部候选，协调器跳过被 hold 的（`StarvationTests` 覆盖）。

冒烟还促成一处真实的表格修正：CTSH / DXC 报的是 `us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization` + 单列的 `DepreciationAndAmortization`，两个都是分项，加进 `CONCEPT_ROLES` 之后这两家从「只有收入」变成「收入到净利润全链条」。同时加了 `ROLE_STATEMENTS` 守卫（见 §2）。

## 8. 测试

```
Ran 2170 tests in 265.868s

OK (skipped=1)
```

（`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`，合并 Wave 0 之后的分支；Wave 0 的 main 是 2,080，本片 +90：`test_model_forecast_driver` 57、`test_company_model_forecast` 16、`test_mission_model_forecast_lane` 17。）

review 之后新增的点：冻结的 `DRIVER_FORMULA_HASH` 与 `CHANGE_REASONS` 原文钉死、公司名用整条 ref 而不是最后一段；丢失更新（拿旧版本改另一个季度 → `ForecastModelConflict`，重新基于链头算就落得下去；第一版撞上已有模型也拒绝；旧版本的 actualize 同样拒绝）；`MAX_REALISED_PERIODS=1` 下老季度掉出 realised 列表后**估计值与实际值都还在**；分母变号不产生假设、税与净利润 unavailable；`because` 点名窗口两端并写明季节性盲区；`--company-ref` 越过 universe 被拒；LaneSpec 登记（顺序在规格 lane 之后、无 core.sqlite 时 argv 为空、launcher 只由那个开关决定）。

原有覆盖的点：driver 按 concept 归并与 `share_of_filed`、无 filed 对应物、无 role、两个 concept 抢 revenue 角色（拒绝而不是挑一个）、没有 filed 收入直接整体拒绝、历史有洞不当成连续季度、资产负债表 instant 不产生假设、现金流行报在利润表上不认 role；手算的收入 → 成本 → 毛利 → 费用 → 营业利润 → 税 → 净利润与 FCF；每个 result 格的 refs；重算逐字节相同；不变 = duplicate、改假设 = 新版本带 prior ref；自动化不能写 `human` 假设、`actual` 必须引用 filing、版本必须有 evidence refs；estimate → actual 的保留与 `superseded_by`；派生行只在分项都有 actual 时才有 actual；实现后的季度不可改；`replay_cell` 逐版本回放；对账层端到端；CLI 五种状态与「新 Claim 不产生新版本」；lane 结算、hold、饥饿；launcher ticket 命名与拒绝；渲染快照。

## 9. 开放问题

1. **第一版的 `change_reason` 用了 `evidence_thicker`。** owner 给的词表里没有「第一次建模」这个词，我没有自作主张扩词表。如果 owner 想要一个 `first_model`，加一个词 + 改一行即可。
2. **默认生成器对季节性是瞎的，ACN 10/1 会看得很清楚**（这句现在也写在每条增速假设的 `because` 里）。 trailing 环比平均把 ACN 的财年季节性抹平了（live 上 ACN 未来八个季度都在 187 亿附近，环比 0.15%），而 ACN 的 8 月季（财年 Q4）只出现在 10-K 里、10-Q 从不覆盖，所以**同比 carry-forward 也填不上这一格**。结论：这一版的收入估计对 ACN 大概率偏高若干个百分点，10/1 的第一条 reconciliation 很可能落在 `notable` 甚至 `overturn_candidate` 带。这是**已知的、写在脸上的**限制，不是数据问题；正解是 M3 的季节性假设，或者用 `draft_assumptions` 这个接缝让模型/人起草假设。要不要在那之前先给 ACN 手工写一版假设（`revise_assumptions`，`change_reason=assumption_review`），请 owner 定。
3. **净利润不含非经营项。** `net_income = operating_income − income_tax`，利息、汇兑、处置损益都不在链里。每条 result 都把公式印在数字下面、并且列出「filed lines this chain does not account for」（ACN：`InterestExpense`、`ShareBasedCompensation`、`RestructuringSettlementAndImpairmentProvisions`），所以它不是一个假装完整的数。M3 建议加 `nonoperating_expense` / `nonoperating_income` 两个角色、把税改成税前利润的份额。**在那之前净利润只应被当作"营业利润减模型化的税"读**。
4. **资产负债表没做。** 规格标 required 的也没做——本轮只有利润表加（规格要求时）现金流量表。三表联动的资产负债表需要营运资本假设（DSO / DPO / 递延收入），那是新的一层假设类型，建议归 M3。
5. **M3 的敏感性 / consensus bridge 会从这个形状要什么？** 我的判断：(a) assumption 已经是逐 driver 逐季度一行、带 measure 与 refs，敏感性只要在 `revise_assumptions` 上跑「同一版 × N 组假设」并把结果并排放，不需要改记录形状；(b) 但**scenario 概念本层没有**——今天一家公司一条版本链，做 bull/base/bear 需要要么加 `scenario_ref` 进 model_ref（`forecast-model:<company>:<scenario>`，只改一行），要么由 M3 自己存一层。请 M3 的 agent 先说要哪种，我倾向前者。(c) consensus bridge 要的是「我们的收入/EPS vs street」，收入已经有了，**EPS 没有**——需要股本 driver（`WeightedAverageNumberOfSharesOutstandingDiluted`），角色表里现在没有，因为 P13ao 的输入表会按单位把 per-share 行排除掉。
6. **Wave 3 的 `ResearchEvent` 怎么调这一层。** 事件层拿到一个事件（新 filing / 8-K / 电话会 / 评级变化 / MarketEvent / 对账结果）之后：先做一次模型调用，得到 `DECISION_VOCABULARY` 里的一个词和受影响的 driver；如果那个词意味着预测要动，就调
   `revise_assumptions(prior, changes=[{driver, period, value, because, refs}], change_reason="driver_event", evidence_refs=[<事件的 claim/figure/event ref>], decision=<那个词>, actor_ref=<mission principal>)`，然后 `authority.publish(body)` 并重发预测行。**这一层不会自己决定任何事**：没有 caller 给的理由和证据，`revise_assumptions` 直接拒绝。`ForecastRevisionProposal`（P14c）如果要做成「人裁决之后才落版本」，建议把 proposal 存在 P14c 自己的表里，裁决通过后再调这个入口——本层不需要为此改动。
7. **`model_run` 写入范围目前没用到。** driver 模型不产生 model run（没有模型调用）。等 `draft_assumptions` 真的接上模型时会需要，届时 `provenance.work_order_ref` 已经在记录里留好了位置。
