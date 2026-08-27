# P8c-1 常驻问题、Tier 1 ProbeTemplate 与 Bounded Planner Loop 准入 v0.1

日期：2026-08-27  
状态：development candidate 已验收并部署；live 准入（常驻问题、ProbeTemplate、循环 v1）已执行；
循环驱动（controller 自动 propose/admit/execute）留给 P8c-2

## 结果

P8c 第一个切片把 Tier 1 Bounded Planner 的准入面接进 writer 并在 live 建立了第一批 authority：

1. **writer 新增 human-governed ops**：`record_backlog_question`（把问题直接记录进
   ResearchQuestionBacklog，绑 exact MandateVersion + company scope）、`publish_probe_template`
   （human-only ProbeTemplate 目录准入，v1 强制 read-only side effects）、`create_bounded_planner_loop`
   （exact question version + 每 coverage item 一个 template binding + 预算），以及读 ops
   `bounded_probe_template` / `bounded_planner_loop`。BoundedPlanner 冲突/未找到/校验类错误已在两侧
   错误映射中。邻接回归 76/76、全仓 950/950。
2. **live 准入（2026-08-27，`human:lumos`）**：
   - 常驻研究问题 `research-question:8359290284e7ba3e9c17c6cfcbbe52d9`（"Has US IT services
     demand bottomed?"）——Phase 8 固定主题，主体为 `industry:us-it-services`，绑 P8a mandate；
   - ProbeTemplate `probe-template-version:3c374282…e8cb9`（SEC company-facts revenue-growth：
     `get_company_facts`，source-level coverage 合同，public SEC read-only，cost 1 unit/2 attempts/120s）；
   - Bounded Planner Loop `bounded-planner-loop-version:ae3363ca…88f6` v1：5 个 coverage item
     （`coverage:revenue-growth:{acn,ctsh,epam,ibm,dxc}`，各绑 template + locator/查询词参数），
     预算 6 rounds / 6 cost units / 900s。**循环已准入并停泊，等待 P8c-2 的 controller 驱动。**
3. **隔离 canary**（`scripts/run_p8c_bounded_planner_canary.py`，live Core+Scheduler 只读副本，
   `status=passed`，0 付费调用）：常驻问题 → template → loop → 确定性 planner 完整驱动 5 轮
   （proposal → Core admission（decision accepted）→ Scheduler WorkOrder → stub source-level result
   （真实 accession 的 matches）→ ResearchOutcome `observed`）→ 覆盖完成后 planner 提交 terminate
   → 终态 `evidence_observed_for_review` 被接受；重放均为 duplicate/terminal；Core 与 Scheduler
   integrity 均 ok。stub 结果只证明循环机制；真实 probe 执行接线是 P8c-2。

## 边界

- Tier 1 planner 只能从已准入 ProbeTemplate 目录中选择；Core 冻结 source/operation/参数/预算/权限/
  terminal gate 的既有合同未动。Reflection 登记新问题候选的通道未开放（P8d 前不需要）。
- `create_loop` 的 doctrine_binding 仍为保留位（fail closed）；LLM planner、
  `propose_next_with_context` 与 CompanyResearchView 的 ContextPack 接线随 P8c-2/P8c-3 进入。
- 三条 live 准入是不可变 authority：问题/template/loop 均带确定性 id，重放幂等。

## 下一步

1. **P8c-2**：controller 驱动——`daltond` 周期唤醒时对停泊循环执行 propose_next（确定性 planner）→
   admit → 以 SEC lane 执行 probe WorkOrder → record_outcome，循环至终态；WorkOrder 执行复用
   既有 lane transport。
2. **P8c-3**：LLM planner（Qwen/DeepSeek V4 Flash development policy）与 doctrine
   ContextPack（含 CompanyResearchView 切片）接入同一循环。
3. 9/3 首个自动 weekly brief 窗口验证。
