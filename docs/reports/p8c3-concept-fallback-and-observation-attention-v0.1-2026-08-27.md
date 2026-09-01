# P8c-3 概念回退与新观察→研究注意力 v0.1

日期：2026-08-27  
状态：已部署 live；loop v2 已准入并由 driver 自主推进，概念回退与观察问题登记均已在 live 验证

## 结果

P8c-3 关闭了 P8c-2 留下的两个真实观察，并把「探测到新事实」接入研究注意力：

1. **executor 概念候选有序回退**：query_terms 中所有形似收入概念的词（`Revenues` →
   `RevenueFromContractWithCustomerExcludingAssessedTax` → `SalesRevenueNet`，与 lane 冻结 allowlist
   同序）按序尝试，第一个在窗口内含 10-Q 的概念胜出。v1 循环的 EPAM `not_found_in_scope` 缺口由此关闭。
2. **新观察 → 研究注意力**：控制面新增 `record_observation_followup(round_ref, mandate_version_ref)`——
   该轮 coverage manifest 的 matched source location 与同 coverage item 的最近一次既往 outcome 对比；
   出现新 accession 时，以 `automation:bounded-planner` 名义在 ResearchQuestionBacklog 登记
   「观察到新 10-Q 源、尚无正式 claim 覆盖，是否刷新 lane？」的开放问题（幂等：同轮重放 duplicate、
   未观察 `not_observed`、无变化 `unchanged`）。问题进入 P8b CompanyResearchView 的 open_questions
   与后续 brief 的 open questions 段——观察转化为注意力，而不是静默丢失。不创建任何 Claim。
   writer 新增 core-only op `bounded_planner_record_observation`；driver 在 observed 结局后调用
   （config 新增 `observation_mandate_version_ref`；登记失败仅记录 `unrecorded:*` 不中断 tick）。
3. **loop v2 live 准入**（`bounded-planner-loop-version:52f3636c…`，prior=v1）：五家 coverage item 的
   query_terms 全部带上三个概念候选；driver 每 300 秒自主推进。

## live 验证（2026-08-27 晚）

- v2 round 1（ACN）：observed，accession 与 v1 相同 → observation **unchanged**（diff 正确，不重复提问）；
- v2 round 3（EPAM）：**observed**（概念回退生效，选中 `sec:accession:0001352010…`），对比 v1 的
  not_found → **自动登记首条观察问题**（`automation:bounded-planner`，EPAM 公司主体）；
- IBM/DXC 轮按节拍继续；DXC companyfacts key 仍被 SEC 移除时将如实再次 `source_unavailable`。

## 验收

- 新测试：executor 概念回退 1、driver e2e 扩展（观察问题登记 / v2 重放 unchanged / 幂等）与
  config 校验；专项/邻接 28→33 全绿；
- **全仓 958/958**；部署后 health 正常，心跳 `bounded_planner` 含 observation 状态。

## 设计边界

- 观察问题只是开放注意力：是否跑 lane 由 owner/后续策略决定（lane 的人工 actor 语义未动）；
- 循环 outcome 历史即 diff 源，不需要为 accession 建新索引；
- 概念候选必须在 loop 准入的 query_terms 里（immutable），executor 不自行扩大概念集。

## 下一步

1. 9/3 首个自动 weekly brief 窗口（Phase 8 第一个观测点）；
2. 观察问题 → lane 触发的策略裁决（owner 决定是否允许 core 发起 lane run，涉及 lane binding actor 语义）；
3. LLM planner 与 doctrine ContextPack 接入同一循环（P8c-4）。
