# Thesis-impact per-day 预算硬顶与失败告警（Gate 3 控制面）

> 2026-08-22 独立复核补丁：原实现已修正真实时钟下重复调用误冲突、policy 版本切换重置当日累计、
> settlement 超过 reservation、alert identity 比较不完整、claim 读取不在写事务内，以及文件权限未显式锁到
> owner-only 的问题。专项测试现为 11/11；本报告下方 7/7 是首次提交时的历史结果。

日期：2026-08-22
状态：开发候选已实现并通过本机测试；未部署、未接 cron、未改变 live routing

## 结论

落实 direction-review v0.7 Gate 3 的控制面前置："在任何自动定时运行前增加 per-plan/per-day hard budget
和失败告警"。新增 `src/dalton_core/thesis_impact_budget.py`（含 `thesis_impact_budget_schema.sql`，第 17 份
packaged SQL schema）——独立 owner-only SQLite authority，不持有 Research Ledger、Scheduler 或 broker handle：

- **版本化日顶 policy**：`thesis_impact_budget_policies` 不可变、prior 链校验、调用方始终传 exact version ref；
- **admission/settlement**：每个 (work order, attempt, phase) 在任何 broker 调用前按 WorkOrder
  `max_cost_usd` 预留日额度；调用入账后 settle 为实际 `amount_micros`（幂等）。未 settle 的 admission 持续
  按全额预留计数——付费调用与结算之间崩溃时保守占额，不会静默释放预算；
- **durable rejection**：超顶 admission 在同一事务里写入 `thesis_impact_day_rejections`（含 committed/cap
  数字）后抛出 `ThesisImpactDayBudgetExceeded`；被拒身份永久不可再 admit（后续结算释放额度也不能复活）；
- **告警**：`thesis_impact_alerts` + 事件链（pending/claimed/delivered/failed，claim TTL，最多 5 次投递
  尝试），`record_alert` 幂等且防语义漂移。kinds：`day_budget_exceeded`（high）、`work_order_failed`（medium）。

## Worker 集成

`ThesisImpactModelWorker` 新增可选 `budget` + `budget_policy_version_id`（必须成对）：

- route selected 后、broker 调用前 admission；超顶 → 正式 `DAY_BUDGET_EXCEEDED` failed ResultEnvelope
  （Scheduler formal result，非内存态拒绝）+ `day_budget_exceeded` 告警，不发生任何 broker 调用；
- `_account` 后立即 settle 为实际成本（绑 usage entry ref）；
- 终态失败（route rejected、不可重试 adapter 失败、recovery miss、输出合同在最后一次尝试被拒）记录
  `work_order_failed` 告警；可重试失败不告警；
- 未配置 budget 的 worker 行为完全不变（所有告警/admission 路径为 no-op），既有测试不受影响。

## 故障注入证明（v0.7 Gate 3 验收口径）

- **超预算会停止并留下 decision**：E2E 测试以 250,500 micros 日顶运行完整 runtime——assessment 预留
  250,000、settle 实际 1,000；verifier 预留 250,000 时 1,000+250,000 > 250,500，落成 durable rejection 行
  （含 committed/reserved/cap 数字）、正式 failed ResultEnvelope（`DAY_BUDGET_EXCEEDED`）、high 告警、0 次
  verifier broker 调用、0 条 verification 记录；重放不产生新调用、新 rejection 或新告警。
- **崩溃保守占额**：store 级测试证明 admission 未 settle 时持续全额计数，后续 admission 按真实余量裁决。
- **告警投递**：pending → claim（TTL、attempt 上限 5）→ delivered/failed 全链路幂等且有界。

## 验证

- `tests/test_thesis_impact_budget.py` 7/7：policy 链、admit/settle 算术、durable rejection 与不可复活、
  幂等/冲突、开放预留保守性、closed 字段校验、append-only 触发器、告警生命周期与投递上限。
- `tests/test_thesis_impact_control.py` 新增 E2E 1 项（见故障注入证明）。
- 全量 `unittest discover`：636/636 `OK`；`compileall`、显式 hermetic research replay canary（0 provider
  calls）、broker 22/22、wheel/sdist build 与 `git diff --check` 全部通过。本轮没有付费调用。

## 边界与后续

- 本切片只建控制面：没有定时器、没有接 controller 的轮询投递（controller 交付与 Discord 通知是部署期
  接线），没有 schema（contracts/）变化，没有改 live。
- "per-plan" 维度由既有机制承担：WorkOrder 自带 `max_cost_usd` 逐单硬顶 + campaign 三重硬顶；本切片补齐
  跨 WorkOrder 的 per-day 聚合硬顶。
- scheduled 运行仍需单独验收；告警的 controller 投递接线在部署批次完成。
