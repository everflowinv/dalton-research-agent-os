# Context、Memory 与 Log 子系统裁决

日期：2026-08-14

## 结论

Dalton 不把 harness 的长会话 transcript、自动 compaction 摘要或可自由改写的 `MEMORY.md` 当作研究记忆。
每个 WorkOrder 都从 typed authority 确定性重建上下文。Research Ledger 已经承担 durable research memory；
Scheduler、ExecutionInvocation、DomainEvent 和 ArtifactVersion 已经承担执行审计与粗粒度恢复。

仍需补四个最小组件：`ContextPack`、`RunState/Checkpoint`、`ClaimIndex` 和非权威 `OpsTelemetry`。
这四项不取代 Ledger，也不能把聊天内容绕过 verifier 写成 Evidence 或 Claim。

## 机制边界

| 机制 | 是否需要 | authority | 数据模型 | 保留策略 |
| --- | --- | --- | --- | --- |
| session compaction | 不作为 Dalton 跨任务机制 | 无 | runtime 单次 attempt 内部实现细节 | attempt 结束即可丢；摘要不能成为事实 |
| ContextPack | 需要 | derived、可重建 | mandate/claim/snapshot/artifact refs + builder/selector/tokenizer/truncation refs 与 hashes + content hash | 由版本化 retention policy 决定 |
| working memory | 需要 | derived、非权威 | `work_order_ref + attempt_number + step_ref` 的 scratch refs | 由版本化 retention policy 决定；事故可 legal hold |
| checkpoint/resume | 需要 | checkpoint 本身非权威；attempt event 引用为审计事实 | checkpoint artifact hash、DAG cursor、completed step refs、pending step refs | 由版本化 retention policy 决定；事故可 legal hold |
| durable research memory | 已有 | Research Ledger | immutable EvidenceVersion、ClaimVersion、ThesisVersion、relations、adjudication | 按研究记录长期保留，不自动压缩覆盖 |
| ClaimIndex | 需要 | derived projection | Ledger refs、subject/metric/period/basis/status、检索字段 | 可整体重建；索引版本切换后旧版短期保留 |
| execution/event log | 已有 | Core/Scheduler append-only authority | ExecutionInvocation、DomainEvent、attempt event、outbox event、Usage/Cost | 按审计和账务策略长期保留 |
| ops log/metrics | 需要 | 非权威 | latency、error class、queue depth、process health、redacted JSONL/metrics | 由版本化 retention policy 决定；事故片段单独冻结 |

## 已有机制覆盖范围

- Research Ledger 比自由文本 memory 更可靠：每个 Claim 都有主体、指标、期间、口径、生产者和证据关系，
  进入 Thesis 前还要过 verification/adjudication gate。
- immutable versions 防止“新摘要覆盖旧事实”；domain events 记录状态变化，不要求用完整 event sourcing
  重建所有领域对象。
- ArtifactVersion 已能登记并引用模型 completion、原始响应和派生产物的 metadata/provenance；完整内容的
  对象存储、retention 和 lifecycle 尚未验收。completion 即使登记为 artifact，也不能直接成为 Evidence。
- Scheduler 的 lease、attempt、idempotency 和 formal result 提供 WorkOrder 粒度的 at-least-once resume。
  `retry_at/not_before` 让 429 回调度器，不在 worker 内等待。

## 仍缺的能力

### ContextPack

`ContextPack = f(mandate_version, selected_claim_versions, perception_snapshot,
artifact_refs, builder_version, selection_policy_version, tokenizer_version,
truncation_algorithm_version)`。

构建必须确定性排序，冻结 builder、selector、tokenizer、truncation algorithm 的 ref/hash，并记录每个
输入 ref/hash、裁剪理由、token/byte 预算和 pack hash。模型调用只引用
pack artifact，不引用一段无法复现的历史聊天。若 runtime 内部压缩 pack，它必须把压缩前后 hash 和
算法版本写入 attempt artifact，但压缩结果仍是 derived data。

### RunState 与 Checkpoint

Planner DAG 每完成一个 step 就可以写 checkpoint artifact。checkpoint 至少包含：

- WorkOrder、attempt、workflow 和 DAG version refs；
- 已完成、待执行和被阻塞的 step refs；
- 已产出的 artifact/result refs；
- cursor、幂等键和 checkpoint content hash；
- 不含凭据、Core DB path 或未脱敏 raw secret。

恢复时先核对 WorkOrder hash、DAG version、capability lease/source epoch 和 artifact hashes；任何一项漂移
都不能原地续跑，要新建 attempt 或重新规划。

### ClaimIndex

先做结构化 SQLite projection，不先上 embedding。查询按 entity/subject、metric/aspect、period、basis、
adjudicated status、source type 和更新时间筛选。以后增加向量索引时，向量只用于召回；返回结果仍必须
回到 immutable ClaimVersion 和 Evidence refs。

### OpsTelemetry

运行日志和指标放 authority DB 之外。它们可丢、可轮转，不能用来证明研究事实。日志只保存事件类别、
redacted ids、耗时、状态和错误分类，不保存 token、cookie、完整 prompt、完整 completion、原始文档或
storage locator。

## Transcript 规则

1. 人在 HTML/Discord/飞书输入的研究要求先转成 Mandate、PriorityOverride 或 WorkOrder proposal；原聊天
   只作来源附件，不自动成为 authority。
2. agent transcript、compaction summary、scratch note 和 runtime memory 不能被下轮模型直接当作事实。
3. 需要长期保留的模型输出先写 ArtifactVersion；需要成为研究记忆时，再经 source/numeric verifier
   转成 candidate Evidence/Claim，并经人工或政策 gate 提交。
4. ContextPack 和 ClaimIndex 都是可重建 projection；删除它们不能损坏 Ledger。

## 实施顺序

1. Connector P0-0 seam 和 P0-1 authority 完成后，冻结 ContextPack 与 Checkpoint closed schema；
2. 在第一条只读研究 WorkOrder 前实现 ContextPack builder 和结构化 ClaimIndex；
3. Planner DAG 接入 step checkpoint/resume；
4. 上线独立滚动 OpsTelemetry；
5. embedding、跨任务语义召回和 runtime 内 compaction 只在有测量证据后追加。

## Go / No-Go

Go：继续 Connector Fabric、ContextPack/ClaimIndex 契约、离线 checkpoint replay 和非权威 telemetry。

No-Go：把 transcript/compaction summary 当研究事实、让模型自由写长期 memory、绕过 verifier 直接写
Evidence/Claim、把 ops log 放进 authority 账本，或用 checkpoint 绕过已过期 lease/source/schema policy。
