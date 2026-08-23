# Bounded Planner Loop v1 实施报告

日期：2026-08-23
状态：development candidate；未部署 live

## 交付结果

本轮新增了单个 ResearchQuestion 内的受限连续规划循环。它解决的不是“让模型自由研究”，而是把每一轮研究判断压缩成一个可审查 proposal：Core 接受后才创建不可变 PlanRound，并继续使用现有 Scheduler、WorkOrder、WorkflowRunVersion 和 WorkOrderLink 执行。

reference planner 使用 capital-lease checklist：上一轮来源级 Outcome 决定下一条尚未完成的 coverage item；人类可以发布下一轮生效的 directive 调整已有 checklist 顺序，不能修改当前 round，也不能借 directive 增加来源、能力、权限或预算。

## 新增 authority

- `ProbeTemplateVersion`：human-only、read-only，冻结 capability、operation、参数合同、输出合同、verifier、permission、side effects 和单轮成本；
- `BoundedPlannerLoopVersion`：绑定 exact ResearchQuestionVersion、template ref/hash、coverage checklist、planner ref/hash 和总预算；
- `ResearchDirectiveVersion` / receipt：保存人类原文、closed control effect、target coverage item 与 `effective_round`；
- `PlannerProposalVersion` / decision：Planner 只提交 probe 或 closed terminal proposal，Core 决定 accepted/rejected；
- `ResearchPlanRound`：绑定 proposal/decision、现有 WorkOrder、WorkflowRunVersion 和 WorkOrderLink；
- `CoverageManifest`：Core 从 exact Scheduler formal ResultEnvelope 机器派生；
- `ResearchOutcome`：只表达 `observed / not_found_in_scope / source_unavailable` 三类来源级结果；
- terminal event：只允许不可观察候选、已有证据待审、human replan、deprioritized 或 budget exhausted。

所有记录 append-only，并保存 canonical JSON 与 content hash。直接 insert、update、delete 受 DaltonStore writer gate 或 append-only trigger 约束。

## 执行与重放

每个 accepted probe 只生成一个确定性 WorkOrder，并通过既有 Scheduler 入队。首轮创建一个既有 `WorkflowRunVersion` root；后续 round 使用 `follows_up` WorkOrderLink 接到上一轮，并追加同一 workflow 的新 version。Bounded Planner 自己没有 queue、lease、retry 或第二套 DAG 表。

proposal decision 在 enqueue 前持久化，WorkOrder 时间与身份从 immutable proposal 派生。若进程在 decision、enqueue、link、workflow 或 round binding 之间崩溃，重放使用同一 idempotency identity 收敛；同键不同内容 fail closed。

## Coverage 与负面结论边界

Core 从 WorkOrder authority 取得 source、locator、query terms 和 coverage item，从 Scheduler formal ResultEnvelope 取得 terminal status 与 `matches`。Planner 或 Worker 不能提交 CoverageManifest。

一个 miss 只形成：

> 在 source X、locator Y、query terms Z 的本轮范围内未发现匹配。

只有所有 required coverage item 都得到 terminal no-match，manifest 才标记 `negative_candidate_eligible=true`。随后终态也只是 `coverage_complete_unobservable_candidate`，`formal_negative_claim_created` 固定为 `false`。本轮没有调用 Ledger writer，也没有自动创建 Evidence、Claim 或 ThesisVersion。

## Human directive

v1 支持三个 closed control effect：

- `focus_coverage_item`：只可指向本 loop 已批准的 coverage item；
- `request_replan`：下一轮进入 `human_replan_required`；
- `deprioritize`：下一轮进入 `human_deprioritized`。

receipt 明确返回当前 round ref、`current_round_unchanged=true` 和下一生效轮次。directive 保存 verbatim text，但 v1 不把它作为模型 prompt 执行；未来 LLM 只能把自然语言翻译成 typed candidate，仍需 human admission。

## Doctrine 槽位

`BoundedPlannerLoopVersion` 已保留 `doctrine_binding`，v1 只接受 `null`。在 DoctrinePack authority 与 exact resolver 尚未同时落地前，非空 doctrine binding 会 fail closed，避免记录一个无法复核的“伪治理”引用。

## 本地验证

新增 8 个专项测试，覆盖：

- 三轮 no-match → coverage-complete unobservable candidate，且不生成负面 Claim；
- 一轮 observed → checklist 完成后只路由到 evidence review；
- checklist 未完成时拒绝负面 terminal；
- round/cost budget 耗尽直接进入 `budget_exhausted`；
- mid-round directive 不改当前 round、下一轮按 target coverage item 提议；
- 重复 probe 拒绝；
- CoverageManifest 直接篡改被 append-only trigger 拒绝；
- enqueue 后崩溃与第二轮 WorkOrderLink 后崩溃均收敛到一个 WorkOrder / workflow tree；
- bounded 表中不存在 queue，执行只进入 `scheduler_work_orders`。

专项测试 8/8 通过。contracts、Scheduler、Observability、ResearchPlan 和 Bounded Planner 关联回归 59/59 通过；`compileall` 与 schema JSON 解析通过。完整仓库回归交给同一 commit 的 GitHub Actions 独立执行。

## 本轮明确未做

- 真实 LLM Research Planner；
- 真实 SEC、AlphaEngine、transcript 或其他 connector 调用；
- DoctrinePackVersion 正式 authority 与 ContextPack consumer；
- 自动 Evidence/Claim/Thesis 写入；
- live writer、service、UI 或部署；
- 通用多行业 probe template library。

下一笔应做 DoctrinePackVersion + ContextPack consumer，并用同一问题在“短期催化”和“资产负债表防御”两个 lens 下生成不同 proposal 路径。之后再把 LLM 接到本轮已经冻结的 `PlannerProposalVersion` contract，不能绕过 Core gate。
