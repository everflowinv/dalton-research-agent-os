# 人类研究意图与 Bounded Planner Loop v1 架构讨论存档

日期：2026-08-23
状态：两轮审阅后收敛；下一笔开发已获 Go 结论
参与者：Eve、Claude CLI Fable 5（第二轮复核使用持久 ACP 会话并锁定 `claude-fable-5`）

## 直接结论

“多关注供需”“看看股东回报”“复盘过往并购 track record”这类开放式研究意图，确实需要 LLM 做语义翻译；固定代码无法穷举自然语言及其行业含义。但 LLM 只负责把自然语言翻译成受限的候选问题、研究 lens、directive 或下一步 probe，不能据此自行扩大研究范围、修改证据标准、取得新权限、增加预算或宣布负面事实。

Dalton Research OS 应把开放式语义与确定性控制分开：LLM 处理“这句话在当前公司和问题里意味着什么”，Core 处理“它是否有权生效、何时生效、能调用什么、花多少预算、结果是否足以进入 Evidence/Claim”。因此，系统不需要把研究方法穷举成路线代码，也不能把控制权交给长会话式研究 agent。

最终采用两层循环：

1. Agenda 外循环负责跨问题的优先级与预算分配。
2. Bounded Planner Loop 内循环负责单个问题内、由上一轮来源级结果驱动的连续 probe。

## 本轮实际审阅范围

讨论同时检查了 Dalton 旧研究工作区与当前 OS 实现，重点包括：

- `workspace-chem/SOUL.md`、`AGENTS.md`、`RAMP.md` 与 `references/manual/research-process.md`；
- 仓库 `README.md`、`docs/PROJECT_STATUS.md`；
- `direction-and-source-modeling-order-v0.8-2026-08-23.md`；
- `architecture-review-and-next-phase-v0.4-2026-08-15.md`；
- Mandate、AgendaPolicy、PriorityOverride、IndustryDriverPack、Thesis、ContextPack、ResearchQuestion、ResearchPlan 等 contracts 与当前实现；
- Scheduler、WorkOrder、WorkflowRunVersion、WorkOrderLink、ResultEnvelope、Evidence/Claim 的现有 authority 边界。

旧研究方法论明确要求先回答事实、行业结构、公司边界、变化机制和证伪条件，再形成观点。它还要求保留原始来源、区分事实与判断、处理反证、避免把“没找到”写成“不存在”。这些原则不是一次性 prompt 技巧，而是 OS 应长期保留的研究约束。

## 哪些内容交给 LLM，哪些留在 Core

### LLM 可以解释

- 把人类自然语言修正分类为候选 question、research directive、driver/lens revision 或 policy revision；
- 结合公司、行业和历史 Outcome，提出下一步 probe；
- 从非结构化文档中提出候选事实、跨期归纳和 thesis impact；
- 解释“供需”在具体行业里的可观察量，例如 IT Services 的 bookings、利用率、交付人力和定价；
- 解释“并购 track record”需要拆成交易事实、整合结果、资本回报和减值记录。

这些输出都只是 candidate/proposal。模型不能直接写正式 authority。

### LLM 不能决定

- scope、权限、credential、connector、operation schema 和副作用等级；
- 预算、重试、最大轮数、最大成本和状态迁移；
- Evidence/Claim 是否正式准入；
- 数字事实是否成立；
- coverage 是否完整；
- “未发现”是否可以升级成“不可观察”或正式负面 Claim；
- ThesisVersion 的正式变化。

这些都由 Core 的 closed contract、确定性验证器或人类 gate 决定。

### 应作为版本化 authority data 的内容

- Mandate：目标、范围、权限和硬约束；
- AgendaPolicy / PriorityOverride：跨问题的确定性排序规则与限时提权；
- DoctrinePack：跨行业研究原则、研究 lens 和证据充分标准；
- IndustryDriverPack：行业 driver、KPI、机制、首选来源和 caveat；
- Company Thesis：公司假设、预期与 falsifier；
- ResearchDirectiveVersion：研究中途的人类修正，保留原文、作用范围、有效期和 closed control effect；
- ProbeTemplateVersion：人批准的受限研究动作，绑定现有 capability、参数边界、输出合同、验证器和成本上限。

Doctrine 不放进 `ArtifactVersion`。第二轮复核后撤回了该方案：Artifact 是由 WorkOrder/ResultEnvelope 产生的执行或证据对象，而 Doctrine 是人发布的指导 authority，两者生命周期和信任来源不同。Doctrine 也不塞进 Mandate，否则会把硬约束与可切换研究 lens 混在一起。

本次开发只在 loop contract 中预留可选的 doctrine ref/hash 槽位，不创建没有消费者的 DoctrinePack schema。下一笔 Doctrine 开发必须同时把它接入 ContextPack 和 Planner，形成第一个真实消费者。

## 人类中途修正怎样生效

人类修正永远不修改正在执行的 DAG，也不回写旧 PlanRound。系统新增一份不可变的 `ResearchDirectiveVersion`，Core 返回明确 receipt：当前 round 不变，directive 从下一 round 起生效。

受理规则如下：

- 只改变当前问题内的关注顺序：directive 指向现有 coverage item，下一轮优先该 item；
- 要改问题本身：旧问题保持可重放，新建或 supersede ResearchQuestionVersion；
- 要扩大来源或新增能力：不能靠 directive 偷渡，必须先发布新的 ProbeTemplate/Catalog authority，再创建新 loop version；
- 要改变跨问题优先级：走 PriorityOverride 或 AgendaPolicy；
- 要改变行业研究结构：走 IndustryDriverPackVersion；
- 要改变长期研究 lens 或证据标准：走 DoctrinePackVersion；
- 要停止或重做当前路径：directive 只能让下轮进入 `human_replan_required` 等 closed 结果，不能让 LLM 擅自改 scope。

自然语言可以先由 LLM 起草 typed candidate，但只有人类 admission 后 directive 才生效。v1 没有连接真实 LLM，因此只消费人已确认的 closed control effect；原文被完整保存，不被当成可执行 prompt。

## 三个例子的完整路径

### “多关注供需关系”

人先在 IndustryDriverPack 中定义本行业供需的可观察量和首选来源；若只是当前问题内临时提权，则发布一个 directive，指向已批准 checklist 中的 supply/demand coverage item。Planner 读取 exact ContextPack、历史 Outcome 和剩余预算，提议下一 probe。Core 只允许它从已批准 ProbeTemplate 中选择，并验证参数没有扩大 scope。

Worker 返回的只是来源级结果，例如“在 accession X、lease footnote、检索词 Y 范围内未找到”。CoverageManifest 从 ResultEnvelope 机器派生，模型不能自报 coverage。下一轮 Planner 可以继续利用率、bookings、交付人力或其他已批准来源。只有 checklist 全部完成后，Core 才能形成“coverage-complete unobservable candidate”；它仍不是正式 Claim。

### “看看股东回报”

IndustryDriverPack 把 FCF、分红、回购、净现金、股本变化和资本配置口径定义成 driver/metric。Agenda 可能因此生成并提权一个股东回报问题。Bounded Planner Loop 依次调用已批准的 filing、statement snapshot 或公司披露 probe，本地确定性计算器处理现金流、回购和股本变化，Verifier 复核数字与期间。

观察到的数字才能进入 Candidate Evidence/Claim，正式 Claim 继续走 Ledger admission。Doctrine 或 Thesis 可以改变“这些事实对投资判断的重要性”，不能覆盖数字、期间或来源。

### “看看之前公司并购的 track record”

这个要求先被拆成两个层次：交易清单、对价、商誉、减值和收购后披露属于可观察事实；“整合能力强”“资本配置差”属于跨期综合判断。前者由受限 probe 收集并逐项形成 Evidence/Claim，后者由 LLM 基于正式 Claim 提出 thesis candidate，再由人类或独立 verifier 准入。

如果当前 Catalog 没有并购历史 probe，Planner 必须返回 blocked / capability gap，不能自己上网、改参数或增加 connector。人批准新 ProbeTemplate 或新能力后，下一版 loop 才能继续。自然语言因此可以影响研究方向，但不能越过权限和证据边界。

## 为什么 Agenda 不能代替单问题内循环

Agenda 只处理跨问题候选与排序，无法表达一个问题内部的连续来源探索：

`lease keyword miss → lease footnote → commitments → debt maturity → latest 10-Q → transcript`

要让这条链可重放，系统必须保留每轮的 exact proposal、Core decision、WorkOrder/ResultEnvelope、source-level outcome、CoverageManifest、剩余轮次和剩余预算。把每个 follow-up 都重新塞回 Agenda 会丢失这些单问题状态，还会把来源级 miss 错误提升为公司级结论。

因此内循环不是第二个 Agenda，也不是第二套队列。它只决定“下一 probe 是什么”，执行仍使用现有 Scheduler、WorkOrder、WorkflowRunVersion 和 WorkOrderLink authority。

## Bounded Planner Loop v1 的最小合同

v1 只增加以下控制面对象：

- `ProbeTemplateVersion`：人批准的受限 probe；
- `BoundedPlannerLoopVersion`：绑定 exact question、checklist、template、预算与可选 doctrine slot；
- `ResearchDirectiveVersion` 与 receipt：记录人类修正及下一轮生效点；
- `PlannerProposalVersion`：probe 或 closed terminal proposal；
- `PlannerProposalDecision`：Core 接受或拒绝；
- `ResearchPlanRound`：绑定 exact proposal、existing WorkOrder、WorkflowRunVersion 和必要 WorkOrderLink；
- `CoverageManifest`：从 exact ResultEnvelope 机器派生；
- `ResearchOutcome`：来源级结果，不自动成为 Claim；
- terminal event：answered、coverage-complete unobservable candidate、human replan 或 budget exhausted。

确定性的 capital-lease checklist planner 先作为 reference planner，使用与未来 LLM Planner 相同的 proposal contract。以后换成 LLM 时，Core gate、round、coverage、budget 和 terminal 规则不变。

## 必须保持的系统不变量

1. Planner 输出永远是 proposal，不是命令。
2. 只有 Core 能创建 PlanRound 和终态。
3. Probe 只能引用 exact、human-admitted template ref/hash。
4. Planner 不能扩大 capability、operation、参数 schema、权限、副作用和预算。
5. 每轮不可变；mid-run correction 从下一轮生效。
6. Scheduler 是唯一队列；loop 不创建第二套 DAG 或 retry engine。
7. CoverageManifest 只能从 exact ResultEnvelope 机器派生。
8. source-level miss 不是 Claim。
9. coverage-complete unobservable candidate 仍不是正式负面 Claim。
10. Doctrine、DriverPack 和 Thesis 只能指导问题与解释，不能覆盖 Evidence/Claim。
11. 重放必须得到同一个 proposal、WorkOrder 和 round identity；同键不同内容 fail closed。
12. 达到轮数或预算上限时由 Core 直接终止为 `budget_exhausted`，不能写成“不存在”。

## 最终裁决与开发边界

裁决：**Go**。

本次开发只做：loop authority contracts、deterministic capital-lease checklist planner、Core proposal admission、budget/coverage gates、directive next-round receipt，以及复用现有 Scheduler/WorkOrder/WorkflowRunVersion 的证明性测试。

本次不做：真实 LLM Planner、真实 connector 调用、DoctrinePack 正式 schema、正式 negative Claim 自动创建、live writer/deploy、UI、通用多行业模板库。

验收必须覆盖：多轮 replay 与 crash convergence、重复 probe 拒绝或幂等、预算耗尽终止、CoverageManifest 篡改 fail closed、checklist 未完成时拒绝 `coverage_complete_unobservable`、以及没有第二套 queue/DAG。
