# Dalton 架构复审与 next phase v0.3

日期：2026-08-15  
状态：开发基线；Human Review 薄切片已实现，未部署

## 结论

独立复审的主判断成立：Dalton 已有可靠的 authority、审计和恢复骨架，但 Agenda、知识生产和 ContextPack
都缺生产消费者。下一阶段不再横向扩 connector，而是把“选题 → 一条真实研究 → 人审入库 → 反思回流”
接成第一条闭环。

复审漏掉了一个必须先修的兼容问题。CandidateClaim 保存 canonical Decimal、currency、scale，并允许结构化
period；正式 ClaimVersion 0.1 只收 float/int 和字符串 period。CandidateEvidence 还比 EvidenceVersion 0.1
多出 SourceEnvelope 与 source-verification hash。若直接把“接受”接到旧 Ledger，数值精度和验证链会在
promotion 时丢失。因此 Slice 1 先加入无损 Ledger promotion 0.2，再做 HTML 入口；不能把旧写入 API 勉强
拼成一个看似闭环的流程。

## 冻结边界

- Dalton Core 继续是 headless、event-driven authority；没有 WorkOrder 时不维持 LLM session。
- OpenClaw 继续只做人机桥、投递和 connector adapter，不持有 Dalton 权威状态。
- WorkOrder、Scheduler、WorkflowRunVersion、WorkOrderLink、ContextPack、Checkpoint、candidate staging 和
  durable outbox 继续复用，不新建第二套 DAG 或任务系统。
- pi、DeepSeek Harness、Prime Agent 都只可作为 RuntimeProfile 后面的可替换 worker，不进入控制面依赖。
- embedding 只可作为可删除、可重建的 recall sidecar；DocumentIndex 先用 SQLite FTS5。任何召回结果必须
  回指 immutable ArtifactVersion、EvidenceVersion 或 ClaimVersion。
- Agenda 的 `auto_accept_timeout` 只是一条反馈统计事实，永远不授权启动 plan、访问新权限或提交 Ledger。
- ReflectionRecord 只能提出 backlog/policy 变更，不能直接修改 Ledger、policy 或 capability 权限。

## 修订后的执行顺序

### Slice 1：Human Review + 无损 promotion（本轮）

1. HumanReviewDecision 对 exact CandidateEvidence/CandidateClaim version 做一次终态决定：
   `accept / revise / reject`。修改候选必须生成下一 candidate version，不能覆盖原记录。
2. reviewer identity 只能由 Tailscale 登录派生；没有 timeout/automation review 路径。
3. accept 在 review authority 内原子写决定与 commit intent；正式 Ledger commit 失败时保留可重试意图。
4. scoped writer 从 Core SourceEnvelope 反查 producer ExecutionInvocation，不接受调用方自报 producer。
5. EvidenceVersion/ClaimVersion 0.2 保留 candidate、SourceEnvelope、Artifact、source verifier、review decision、
   canonical Decimal、currency/scale 与结构化 period 的 ref/hash。
6. Evidence、Claim、supports relation 和 promotion receipt 在同一个 Core 事务提交；任一缝隙失败全部回滚。
7. accepted claim 初始状态仍是 `proposed`。人工语义核对不等于独立 corroboration；connector producer 的后续
   model adjudication 需要专门的跨 execution policy，不能伪造 ModelInvocation。

验收：真实 SEC authority candidate 可被明确接受并无损写入正式 Ledger；reject/revise 只留痕；重复提交收敛；
四个事务故障缝隙无残留；scoped review token 不能调用其他 writer operation。

### Slice 2：投影完整性和文档检索

- 修复 ClaimIndex：status 必须由 exact Ledger snapshot 投影，builder 不再接收 caller 提供的 status。
- 增加 DocumentIndex FTS5，对 ArtifactVersion 的抽取文本、公司、来源、类型、日期做可重建投影。
- 冻结 ContextPack materializer：只从 ref/hash authority 水合正文，记录预算、裁剪和缺失账。
- AgendaCoordinator 迁到同一 materializer 后，删除手工 prompt 双轨。

ClaimIndex 修复和 materializer 是 Planner 开闸前置条件，不能推迟到 Planner 之后。

### Slice 3：ResearchQuestionBacklog

- 稳定 question ref + immutable versions。
- 状态机：`open → selected → planned → in_progress → answered / blocked / retired`。
- answered 必须绑定 formal ClaimVersion refs；同一问题跨 cycle 保持身份，Agenda 不再每天重新发明问题。
- Mandate 增加可重建进度投影，但 objective/constraint authority 不改成可变记录。

### Slice 4：Planner 薄闭环

- selected AgendaDecision 编译为 immutable ResearchPlanVersion。
- 复用 WorkflowRunVersion + WorkOrderLink 形成任务树；首版只允许一个 SEC public read-only plan。
- plan 启动要求明确人工批准。Agenda agree 和 timeout agree 都不构成执行授权。
- 执行链固定为 connector → authority resolver → verifier → candidate staging → Slice 1 review。

### Slice 5：Interrupt / park / resume

- Scheduler 增加 append-only cancelled terminal event；coordinator 增加 parked state。
- InterruptRequest 使用 `pool=interactive`，与 planned pool 分容量，不能靠提高 priority 抢占全部 worker。
- 临时问题先查 ClaimIndex/DocumentIndex；检索不足时生成新 research plan，仍走 plan 人工 gate。
- 原计划恢复前继续复用现有 WorkOrder hash、DAG version、lease/epoch 重验。

### Slice 6：Reflection 回流

- workflow 终态后写 append-only ReflectionRecord。
- 记录回答的 question、新增 open question、预期与实际信息增益、失败原因。
- Reflection 只能向 backlog/policy 提 proposal；Core 确定性校验后再进入对应人工或自动 gate。
- 验收目标是计划 N 完成后生成可审计的计划 N+1，不是让模型自行改治理规则。

## 每个 slice 的能力门槛

从本阶段起，每个实现 slice 必须让 Dalton 多完成一个真实研究动作，并提供对应的 authority/recovery 测试。
只增加 schema、connector inventory 或 dashboard 字段而没有新消费者，不算闭环进展。新增 connector 只按当前
闭环需要补，不再用覆盖面代替能力。

## 继续保留的人工闸门

- 正式 Ledger commit：每条 explicit human review；没有自动接受。
- plan 启动：至少前两个稳定周期逐 plan 人批。
- connector network canary 与 production promotion：分别审批。
- 新 credential、外发、旧 cron cutover、Agenda 扩公司数：继续独立审批。
- 放宽任一闸门都要基于运行校准数据单独决策，不能随某个 slice 一并默认开放。

## 本轮实现范围与未完成项

已实现 HumanReviewAuthority、review commit intent、Evidence/Claim 0.2、原子 promotion、scoped writer operation、
Tailscale/CSRF HTML review 入口和故障恢复测试。当前代码仍是开发候选：未部署、未把真实 SEC canary candidate
复制到 live staging、未修改 Agenda、未关旧 cron。

下一笔提交应做 Slice 2 的 ClaimIndex status 派生修正；DocumentIndex 与 materializer 分开提交，避免把三个
只读投影问题混成一次大迁移。
