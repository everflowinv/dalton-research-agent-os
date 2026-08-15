# Dalton 架构方向与闭环开发基线 v0.4

日期：2026-08-15
状态：当前开发基线；Human Review、ClaimIndex、DocumentIndex、ContextPack materializer 与 Agenda 统一上下文路径已完成开发候选，均未部署

## 结论

Dalton 的目标不变：人只提供长期研究目标、约束和临时航向调整；系统自己维护问题、安排计划、执行研究、
沉淀知识、反思并继续下一轮。临时问题先查既有 Claim/Document 知识，信息不足时再生成响应式研究计划，完成后
恢复原计划。长期状态全部进入外部 typed authority；模型 session、transcript、compaction summary 和任一 harness
都不能成为事实或进度权威。

2026-08-15 的独立复审指出的四个近期缺口中，前三类底座已经补齐：

1. HumanReviewAuthority 已能对 exact candidate 做 `accept / revise / reject`，explicit human accept 才能生成
   Ledger commit intent；EvidenceVersion/ClaimVersion 0.2 与原子 promotion 保留 Decimal、结构化期间和完整来源链。
2. ClaimIndex status 已改为从 Core Ledger 一致快照派生，caller 不能提交 claim bundle、status 或 snapshot hash；
   DocumentIndex FTS5 已能从 ArtifactVersion/RawSpool 重建文档检索投影。
3. ContextPack materializer 已能从 exact ClaimVersion/ArtifactVersion 水合短生命周期 quoted JSONL，冻结预算、
   renderer、tokenizer、projection status 和 replay 账；caller 不能注入正文、路径或 resolver。
4. Agenda 的上下文双轨已经关闭。PerceptionSnapshot 进入 Core append-only authority；Mandate、Policy、Cycle 和
   Perception 都由 exact reader 重算；AgendaCoordinator 只把固定 instruction/output contract 与 materializer 的
   quoted JSONL 交给模型。ResearchQuestionBacklog、Planner、interrupt/resume 与 Reflection 尚未实现。

因此当前瓶颈已经从“缺少检索/水合底座和统一消费路径”移动到“问题不能跨 cycle 存续”。下一笔进入
ResearchQuestionBacklog。不能为了赶进度把 per-cycle candidate 直接当长期问题账，也不能让 AgendaDecision
自动获得 plan 启动权限。

## 冻结架构

- Dalton Core 是 headless、event-driven authority；没有 WorkOrder 时不维持 LLM session。
- OpenClaw 只做人机桥、投递和 connector/runtime adapter，不持有 Dalton 权威状态。
- WorkOrder、Scheduler、WorkflowRunVersion、WorkOrderLink、ContextPack、Checkpoint、candidate staging 和
  durable outbox继续复用；不新建第二套 DAG、任务队列或“长期聊天记忆”。
- pi、DeepSeek Harness、Prime Agent 和其他 runtime 只可作为 RuntimeProfile 后面的可替换 worker。长任务靠有界
  WorkOrder、durable checkpoint 和每 attempt 重建上下文完成，不靠单次超长会话。
- 文档检索先用可重建的 SQLite FTS5。embedding 只有在 FTS miss 率有测量证据后才作为 recall-only sidecar；
  结果必须回指 immutable ArtifactVersion/EvidenceVersion/ClaimVersion，向量库永不进入 authority。
- Agenda `auto_accept_timeout` 只是一条反馈统计事实，不授权启动 plan、访问新权限或提交 Ledger。
- 正式 Ledger commit、前两个稳定周期的 plan 启动、connector promotion、credential、外发、部署、旧 cron cutover
  和 Agenda 扩容继续使用各自独立的人工 gate。

## 已完成的闭环前置切片

### A. Human Review 与无损 promotion

- exact CandidateEvidence/CandidateClaim version 只有一个终态 review decision；review 身份由 Tailscale principal 派生。
- accepted candidate 通过 scoped writer 反查 producer execution；Evidence、Claim、supports relation 和 receipt 在
  同一 Core 事务提交，失败整体回滚。
- 人工语义核对不会把 claim 伪装成 corroborated；accepted claim 初始仍是 `proposed`。

### B. Claim 与文档检索投影

- ClaimIndex 从 exact Ledger snapshot 派生 status，保留 adjudication、历史版本、Decimal 和结构化 period 语义。
- DocumentIndex 只从 ArtifactVersion 与 RawSpool 派生，支持 FTS 和来源、类型、日期、公司、访问级别分面；投影可
  整体删除重建，不承担多租户安全隔离。
- 当前 trigram FTS 对两字中文词可能漏召回；SEC submissions JSON 只是 filing metadata，不是 filing 正文。

### C. ContextPack materializer

- Claim/Artifact 只从 Core Ledger、Observability 与 RawSpool exact reader 水合；ClaimIndex 冻结状态随 ClaimVersion
  一起进入模型正文，但不冒充 ClaimVersion authority。
- ContextPack 选择预算与最终 envelope-inclusive render 预算分开；wrapper 开销全部计入，超限 fail closed，
  不静默截断或重选。
- 现已支持 `claim`、`artifact`、`mandate` 与 `perception`；Agenda 使用独立 typed binding，SourceEnvelope 仍没有
  作为正文类型进入 materializer。

### D. Agenda context authority 与统一 materializer

本切片已经关闭 prompt 双轨，没有提前实现 Backlog 或 Planner：

1. PerceptionSnapshot 已登记为 Core append-only authority；保存 canonical record、content hash 与 immutable ref。
   AgendaCycle 启动时核对并冻结 exact perception、MandateVersion 和 AgendaPolicyVersion ref/hash。
2. MandateVersion、AgendaPolicyVersion、AgendaCycle 与 PerceptionSnapshot 已有 exact reader。reader 从 Core
   canonical row 重算并核对 hash，拒绝
   caller body、可变文件、路径和 callback resolver。
3. ContextMaterializer 已增加 `mandate`/`perception` reader，并为 Agenda 增加独立 closed typed binding，没有构造
   虚假的 CompiledConnectorPlan；旧 connector-bound ContextPack 保持可重放。
4. AgendaCoordinator 的事实输入现在只能是固定 instruction/output contract 加 materializer 输出的 quoted JSONL。
   可变 snapshot 文件只允许作为 adapter 写入前的临时交接，不再参与 cycle replay 或模型输入。
5. `max_input_tokens` 已覆盖最终完整 prompt。Mandate 与 Perception 都是 required input；任一被预算丢弃或 ref/hash
   漂移，cycle fail closed。

验收已通过：同一 AgendaCycle 在进程重启、snapshot 文件删除/篡改和无关 Ledger 增长后仍可从 exact authority
逐字重放同一 prompt；伪造 Mandate/Perception/body/hash/plan binding 均被拒绝；模型输入中不存在手工
`MANDATE=`/`PERCEPTION=` 数据双轨；现有 Claim/Artifact 历史 pack 回归不变。Agenda 专用 renderer 绑定 exact
AgendaContextBinding。ContextPack 0.1 的必填 ClaimIndex 字段使用只允许 Agenda binding 消费的显式 no-index
sentinel，不扫描 Ledger，也不会随无关 Claim 写入而改变 pack、prompt 或 WorkOrder。

## 后续顺序

1. **ResearchQuestionBacklog（下一笔）**：稳定 question ref/version，状态
   `open → selected → planned → in_progress → answered / blocked / retired`；answered 绑定 formal ClaimVersion refs；
   Mandate 增加可重建进度投影。
2. **Planner 薄闭环**：selected AgendaDecision → immutable ResearchPlanVersion → 复用 WorkflowRunVersion/
   WorkOrderLink；首版只允许 SEC public read-only plan，并逐 plan 人批。
3. **Interrupt / park / resume**：interactive/planned 分池，临时问题先查 ClaimIndex/DocumentIndex；新研究仍走
   plan gate，原计划恢复前重验 WorkOrder hash、DAG version、lease/epoch。
4. **Reflection 回流**：workflow 终态后写 append-only ReflectionRecord，提出新 backlog item 和 policy proposal，
   不能直接改 Ledger、policy 或权限。

从本阶段起，每个 slice 都必须让 Dalton 多完成一个真实研究动作，并同时交付 authority、replay、故障恢复和权限
测试。只增加 schema、connector inventory 或 dashboard 字段而没有消费者，不算闭环进展。
