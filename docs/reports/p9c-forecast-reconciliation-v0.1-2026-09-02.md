# P9c：Forecast reconciliation——第一条 Outcome 对象 v0.1

日期：2026-09-02
状态：development candidate；专项与 live Core 只读副本 canary 通过；**未部署、未写 live**
上游：[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)、[M1](m1-model-engine-and-ae-probe-v0.1-2026-09-01.md)、[P9b-2](p9b2-mission-observation-sec-auto-lane-v0.1-2026-09-02.md)

## 这一片解决什么

M1 让 Core 有了版本化的预测线（ACN 4 条 `derived_deterministic` 收入预测），P9b 让实际数（SEC 10-Q/10-K 收入 Claim）
能自动落库。两者之间没有对象把「预测 vs 实际」记下来：playbook `model_discipline` 最后一条「预测被实际推翻时生成
reconciliation 记录」没有兑现，愿景里 Evidence→Claim→Thesis→Driver→Question→Outcome 的最后一环是 0 条。

P9c 新增 `ForecastReconciliation`：把一个 exact 的 ForecastLine 版本和一个 exact 的正式 ClaimVersion（同一公司、同一
metric、同一财季的实际数）绑在一起，确定性地算出偏差，登记方向和分档。它是派生 authority：不改预测线、不改 Claim、
永远不碰 Thesis。偏差进入 `overturn_candidate` 档时只登记 `forecast_overturn` 人类检查点，预测不会被自动修改；
人用 `ForecastOverturnDecision` 裁决，要改预测仍走既有的人工 forecast-line / model-input 操作。

## 合同（冻结在 `outcome:forecast-reconciliation:1`）

- **实际数来源**：正式 ClaimVersion 自己的 `normalized_statement`。SEC company-facts Claim 的语句由 policy 自动提交规则用
  固定模板从已验证 payload 渲染，所以反向解析后再用同一模板逐字节重渲染，必须与 Claim 自带的 hash 绑定文本完全一致，
  同时 Claim 的 `value`/`unit`/`period` 要和语句自洽；不一致就不对账（fail closed）。不读 staging，不猜数字。
- **metric 绑定表**：`metric:revenue-usd` ↔ Claim `quarterly_revenue_yoy_growth`（basis `official-filing-xbrl`）的
  `current` 值，货币 USD。新 metric 要加进表，是新合同版本。
- **换算**：实际数按预测线单位（one / thousand / million / billion）重标；`deviation_absolute = actual − forecast`，
  `deviation_percent = deviation_absolute / forecast × 100`（4 位小数，half-up）。
- **分档**：|deviation_percent| < 1 → `within_tolerance`；1 ≤ · < 3 → `notable`；≥ 3 → `overturn_candidate`，同时
  `human_checkpoint = forecast_overturn`。阈值改动 = 新合同版本。
- **只对最新版本**：只配对每条 line_ref 的最新版本与每个 claim_ref 的最新版本；identity 含两端 exact 版本，
  append-only，重复请求返回 duplicate。
- **actor 语义**：记录的 `actor_ref` 固定为 `automation:forecast-reconciler`（与 M1 的 `automation:forecast-extender`
  同一类派生写入者）；`requested_by` 记谁触发——`human:` 直接请求，或 mission 的 automation principal，此时必须带
  `mission_binding`（exact mission 版本 ref/hash）。

## 写权限：mission 词表追加一个词

ADR-0004 决定 2 的冻结词表追加 `forecast_reconciliation`（thesis 仍不在词表里）。automation 只有在**唯一**覆盖该公司的
active CoverageMission 同时满足「`may_write` 含 `forecast_reconciliation`」和「`human_checkpoints` 含
`forecast_overturn`」时才能写；mission 绑定的 playbook / constitution / mandate 仍按 exact hash 重验。live 的 mission v1
没有这个词，所以部署后 automation 路径会如实报 `skipped: mission does not grant forecast_reconciliation writes`，
直到 owner 发布 mission v2——这正是 ADR-0004「新的自动写入权限写在 mission 里」的用法。`human:` principal 可以不经
mission 请求对账（记录 `requested_by=human:...`，`mission_binding=null`）。

## 接到哪里

- **SEC lane 提交点**（`sec_company_facts_lane.run_issuer`）：正式 Claim/Evidence 落库、mission stage claim 写完之后，
  立刻对这条 Claim 做一次对账；结果进 summary 的 `forecast_reconciliation`（`reconciled / skipped / idle / failed` 加
  逐条原因），失败不影响已提交的 Claim。
- **controller tick**（`bounded_planner_driver.run_once`）：每 tick 调 writer 核心 op `reconcile_forecasts`，扫描全部
  待配对的（最新预测线, 最新实际 Claim）并在 mission 授权下写入；lane 那一步被跳过或 Claim 从别的路径进来时由这里兜底。
- **周报**（`weekly_brief`）：issue 新增「预测对账」一节和 `forecast_reconciliations` 字段，列出研究窗口内产生的对账
  记录（exact ref/hash 与摘要），`issue()` 重放时逐条核对 hash 与摘要；没有就明写「本期没有预测线被实际数对账」。
- **thesis-impact**（`thesis_impact_control`）：assessment WorkOrder 若发现该 Claim 已有对账记录，把 canonical JSON 作为
  数据块追加进 prompt，并把 ref/hash 写进 WorkOrder identity 与 `input_refs`；没有对账记录时 identity 与旧版完全一致。
- **CompanyResearchView**：新增 `forecast_reconciliations`（含 `checkpoint_status`）与 `forecast_reconciled` 停点。
- **writer ops**：`reconcile_forecasts`（core / human）、`forecast_reconciliations`、`get_forecast_reconciliation`（读）、
  `decide_forecast_overturn`（human-only）。core principal 对前三个 op 有显式豁免（`CORE_RECONCILIATION_OPERATIONS`），
  裁决只收 `human:`。
- 新合同：`contracts/forecast-reconciliation.schema.json`、`contracts/forecast-overturn-decision.schema.json`；
  新 schema：`forecast_reconciliation_schema.sql`（authorized-insert guard、不可 update/delete）。
- 工具：`scripts/build_mission_v2_params.py` 从 live 只读 Core 生成 mission 下一版的 `create_coverage_mission`
  参数（所有字段与 exact 绑定原样保留，只追加词）；`scripts/run_p9c_forecast_reconciliation_canary.py`。

## 验收

- 专项：`test_forecast_reconciliation` 11 项（模板解析只认往返一致、确定性对账与重放、overturn 检查点与人工裁决/
  重复裁决冲突、within_tolerance、automation 无 mission 授权即拒绝且 skip 不落库、非模板语句不猜、无 supporting evidence
  / 货币不符 fail closed、只配最新版本、跨期/跨公司不配、SQL 触发器挡直写）；lane 2 项（human 运行对账、mission v1 无
  scope 如实 skip → v2 授权后 tick 路径对账并带 mission 绑定）；writer ops 1 项（core/human idle、core 与 automation 不能
  裁决、伪造 requested_by 被拒）；thesis-impact control 1 项（prompt / identity / input_refs 带对账）；weekly brief 1 项
  （窗口内列出、渲染、重放、窗口外为空并明写）；CompanyResearchView 闭合键集更新。
- live Core **只读副本** canary（`temp/p9c-canary.json`，`ok=true`）：
  1. live 4 条 FY2026 预测线没有实际数，待配对数 0（如实等待 filing）；
  2. 在副本上以 `human:lumos` 发布两条标明「canary rehearsal」的 `estimate` 线（FY2025 Q4，17,300 / 16,800 百万美元）；
  3. 以 `automation:coverage-mission` 跑 10-K lane（真实 ACN companyfacts fixture，accession `0001467373-25-000217`）：
     Claim 6→7、stage claim fresh，对账 **skipped**，两条原因均为 `mission does not grant forecast_reconciliation writes`；
     `forecast_reconciliations` 仍为 0 行；
  4. 用 `build_mission_v2_params` 在副本发布 mission v2（只追加词），tick 路径对账 2 条：17,300 → 实际 17,596.26，
     **+1.7125% notable**；16,800 → **+4.7396% overturn_candidate**，`human_checkpoint=forecast_overturn`；重放 idle；
     `human:lumos` 裁决 `keep_forecast`，状态 `decided:keep_forecast`；
  5. CompanyResearchView 读到 2 条对账、最后停点 `forecast_reconciled`；reconciliation integrity ok、Core `PRAGMA
     integrity_check` ok；0 网络、0 付费、0 live 写入。
- 全仓 unittest 见提交信息与 PROJECT_STATUS（本机 `test_writer_service` partial-frame 用例的既有环境性超时不计为回归）。

## 仍未做 / 边界

- **未部署**。上 live 分两步且都是 owner gate：① 重装 wheel 重启四服务（新增表、lane 提交点与 tick 逻辑生效，此时
  automation 只会报 skipped）；② `dalton-gov create_coverage_mission` 发布 mission v2（用
  `build_mission_v2_params.py --add-scope forecast_reconciliation` 生成参数），automation 才能写对账。
- 10/1 前 live 没有可对账的实际数（FY2026 Q4 10-K 未出）；第一条 live 对账要等 ACN 10-K 落库，届时 4 条预测线里
  Q4 FY2026 那条会与新 Claim 配对。
- 只覆盖 `metric:revenue-usd`；其他 metric、非 SEC 来源的实际数要扩绑定表（新合同版本）。
- `revise_forecast` 裁决只记录意图，不生成新预测线；新假设/新线仍由人经 model-input / forecast-line 人工 op 发布，
  再由 M1 的 extend 重算。
- 周报只列窗口内新产生的对账；`pending_human` 未决的检查点不会在后续周报重复提醒（可作 P9f 交付物规则补）。
