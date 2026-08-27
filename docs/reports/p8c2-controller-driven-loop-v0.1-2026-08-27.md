# P8c-2 Bounded Planner controller 驱动 v0.1

日期：2026-08-27  
状态：已部署 live 并完成第一次全自主循环（5 轮探测 + 终态收尾，无人干预，0 付费模型调用）

## 结果

P8c-2 把停泊的 Tier 1 循环接入 `daltond` 周期驱动，闭合了 Phase 8 认知循环的 planner 执行段：

```text
daltond 周期唤醒（300s）
→ writer RPC：列出未终态循环（bounded_planner_active_loops）
→ 确定性 planner 提案（bounded_planner_propose_next）
→ Core 准入（bounded_planner_admit_proposal，冻结 scope/参数/预算/权限，Scheduler WorkOrder 入队）
→ controller 经公共 SEC transport 真实执行 probe（每 tick 至多 1 次）
→ 源级 ResearchOutcome（bounded_planner_record_outcome）
→ 覆盖完成后 planner 提交 terminate，Core 接受终态
```

### 组成

1. **writer 驱动 ops**（CORE principal，照 `run_weekly_brief_cycle` 模式）：
   `bounded_planner_propose_next` / `bounded_planner_admit_proposal` /
   `bounded_planner_record_outcome` / `bounded_planner_active_loops`（authority 新增
   `active_loops()`：每个 loop_ref 的最新版本且无终态者）；`BoundedPlannerPending` 纳入错误映射。
2. **`bounded_probe_executor.py`**：Tier 1 SEC company-facts 源级覆盖——每次 probe 一次有界公共 HTTPS
   抓取（无凭据、`PublicHttpTransport`、8 MiB 上限、data.sec.gov 白名单），在 400 天窗口内选最新
   10-Q accession 作为 `matches` 的 source_location；HTTP 失败/传输异常 → `SOURCE_UNAVAILABLE` failed
   envelope → outcome `source_unavailable`；窗口内无命中 → 空 matches → `not_found_in_scope`。
3. **`bounded_planner_driver.py`**：core-principal RPC + 直开 Scheduler claim/complete（thesis-impact
   runner 同款多进程模式）；闭合 config 合同（含 `max_probes_per_tick`、`filed_window_days`）。
4. **daltond 服务块**：`bounded_planner: {enabled, interval_seconds, config}`——独立线程池、心跳
   `bounded_planner` 状态（照 weekly_brief 轮询模式）；SEC 礼貌由 interval（300s）× 每 tick 1 probe 保证。

## live 首次全自主循环（2026-08-27 17:06–17:2x UTC）

对 P8c-1 准入的 `bounded-loop:us-it-services-demand:v1`（5 coverage items）逐 tick 自动推进：

| Round | Coverage | Outcome | 说明 |
|---|---|---|---|
| 1 | revenue-growth:acn | **observed** | 选中 10-Q `0001467373-26-000032`——比 lane 上午取的 `…000031` 更新的一期 ACN 10-Q |
| 2 | revenue-growth:ctsh | **observed** | 10-Q `0001058290-26-000016` |
| 3 | revenue-growth:epam | **not_found_in_scope** | EPAM 不报告 us-gaap:Revenues 概念——源级覆盖如实记录未命中，不伪造 |
| 4 | revenue-growth:ibm | **observed** | 正常 |
| 5 | revenue-growth:dxc | **source_unavailable** | SEC 侧 `companyfacts/CIK001688568.json` 在当天 12:22 lane 成功抓取之后变成 404 NoSuchKey（SEC 重建/回收了该文件）；实测 curl 复现 |
| — | 终态 | planner 自主提交 terminate，Core 接受 | `evidence_observed_for_review` |

此后每 tick driver `idle`（无未终态循环）。全程 0 付费模型调用（确定性 planner）。

## 验收

- 新测试：executor 4（成功选择 / 窗口外未命中 / HTTP 与传输失败 fail closed / 非 Tier 1 WorkOrder 拒绝）、
  driver 2（在进程内 writer + 真实 Scheduler 上逐 tick 推到终态并 idle；config 闭合形状）、service 2
  （bounded_planner 块解析 + 心跳 disabled 状态）；邻接 32/32；
- **全仓 957/957**；部署后 health 全绿，心跳含 `bounded_planner` 状态。

## 观察与后续

- **EPAM 概念缺口**：loop v1 的 query_terms 只有 `Revenues`；EPAM 用
  `RevenueFromContractWithCustomerExcludingAssessedTax`。lane 的冻结 allowlist 有序回退，probe 的
  query_terms 应对齐——loop v2（不可变续版）把概念候选写进 query_terms 即可，无需改代码。
- **DXC companyfacts 404**：SEC 侧当天移除了该 key；`source_unavailable` 是正确记录。SEC 恢复后由
  后续 loop 版本重探。
- coverage manifest 与 outcome 已成为 `evidence_observed_for_review` 的机器可复核输入；下一步把
  「observed 的 accession → lane/claim 链」接上（P8c-3），以及 LLM planner 与 doctrine ContextPack。
