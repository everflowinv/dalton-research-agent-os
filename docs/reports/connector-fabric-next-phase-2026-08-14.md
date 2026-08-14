# Connector Fabric 下一阶段与独立复核更正

日期：2026-08-14

## 更正

提交 `df66d46` 时，本报告写成“Fable 5 最终结论为 Go”，但当时没有实际启动 Fable 5
独立审阅。那一版结论来自主 agent 的架构分析，出处写错了。这不是措辞问题，而是审计事实错误。

随后 Fable 5 已对 `df66d46`、未提交代码、测试和相关架构文档做只读独立复核。实际结论是
**有条件 Go**：方向成立，但必须先补专项测试、在生产库副本演练启动回填、修复 Artifact v0.2
投影盲区，并明确 Scheduler attempt event 的 hash epoch，才可进入 Connector authority。

## 结论

独立复核后的结论为 **有条件 Go**：下一阶段进入 Connector Fabric Shadow，不等待万华完成 10 个工作日。
10 日/20 个显式人工标签只限制 Agenda 从 1 家扩到 3 家；protocol、offline replay、connector shadow、
verifier、Model IR 和 sandbox 骨架可以并行建设。Connector Shadow 输出不得接入当前 Agenda
Perception，也不得写 Research Ledger。

A 股公告 connector 面向所有 A 股公司和公告 operation。CNINFO/巨潮是 source adapter，万华只是首个
fixture 和 shadow 对账对象。SEC、AlphaEngine、X、Reddit、Guidepoint、web search/fetch 等研究关键
来源都进入通用 connector 路线图，但不能把现有聚合 skill 整体复制进 Dalton。

## 架构分析与独立复核确认的问题

1. 现有 `UsageEntry` 强制记录 model/profile/token，只能继续作为模型用量权威；connector 必须单独记录
   Usage、PriceRate 和 Cost。
2. 现有 ArtifactVersion 的 producer 外键强绑 ModelInvocation。connector 如果直接产 raw artifact，
   会被迫伪造模型调用。
3. ConnectorResult 不能成为 ResultEnvelope 之外的第二份结果权威；source-specific 请求参数也不能只
   藏在 RPC body 中。
4. logical invocation 与 physical provider attempt 必须分开。429、timeout、retry 和结果不确定的调用
   都要计量，外部 API 不承诺 exactly-once。
5. completeness 必须区分 enumerated、ranked、partial、unknown。xreach 与 x_search、web search 与
   web fetch 必须使用不同 descriptor/profile。
6. CapabilityDescriptor 不足以承载 source runtime 参数，需要 ConnectorProfileVersion；MCP schema epoch、
   tool/operation、skill entrypoint/hash 或 adapter source hash 改变时，旧 lease 必须失效。
7. 当前 CapabilityAttestation 明确禁止网络和凭据，不能冒充真实 connector canary evidence。
8. 同一 macOS user 下的 trusted runner 不是 hostile-code sandbox，不能动态执行自生成 proposal code。

独立复核另发现四项实现债务：

1. `connector_schema.sql` 当时没有 Python loader 或测试，仍是未接线 DDL；
2. Execution/Artifact/Scheduler seam 当时没有专项测试；
3. Artifact v0.2 没进入 dashboard projector；
4. Scheduler 增加 `not_before` 后没有声明新旧 event hash 的 wire epoch。

当前 P0-0 修正已补专项测试、Artifact v0.2 投影和 `wire_version=0.2`；生产 Core/Scheduler
数据库副本的启动回填演练通过，原库未修改。`connector_schema.sql` 仍须在 P0-1 接进
`ConnectorStore` 后才算可执行 authority，不能因为 DDL 已存在就报完成。

## 已冻结的架构裁决

### Invocation 与 Artifact

- 新增通用 `ExecutionInvocation` 超类型，以及 Model/Connector 1:1 子类型；
- 采用新增表和 backfill link，不改写历史 ModelInvocation/Artifact hash；
- 新模型调用由 writer 在单事务内同时登记 generic 与 model row；
- ArtifactVersion v0.2 引用 `producer_execution_ref`；
- connector 使用独立 Usage/PriceRate/Cost authority，不把模型 Usage 泛化成含义不清的万能表。

### 调用与结果

- 复用 admitted WorkOrder、validated CapabilityLease 和 ResultEnvelope；
- source-specific 参数写入 immutable `ConnectorCallSpec`，其 hash 由 WorkOrder `input_refs` 绑定；
- transport-only `ConnectorRunnerResponse` 只携带 ConnectorInvocation、ResultEnvelope、SourceEnvelope refs
  和 quota settlement；adapter 强制各引用和状态 canonical equality；
- `ConnectorProfileVersion` 冻结 source/adapter version、operation/host、auth/credential slot、schemas、
  pagination/completeness、response limits、access/retention/terms 和 network policy。

### 计量、配额与故障

- 每个 physical attempt 调用前做 durable reservation，结束后 settlement；
- 上游是否已调用不确定时保守占额并标 indeterminate；
- 429 按 Retry-After 回到 Scheduler，不在 worker 内 busy wait；
- RatePolicy 只管理 admission、quota、concurrency、window 和 retry；PriceRate 单独管理费率；
- source health 和 circuit state 从 append-only event 投影；
- 最小 ConnectorIncident authority 记录 quota drift、schema drift、credential/auth 和 source outage；
- provider-reported overage 必须写 incident 并阻断后续调用，不能声称外部 quota 永不漂移。

### 自建 connector

复用现有 `CapabilityProposal(kind=connector)`，不新建平行 proposal registry。connector manifest/profile
必须是闭合 schema 并带 hash：

`gap → proposal → template → offline replay/eval → human canary authorization → trusted canary → canary eval → human production promotion → Catalog`

独立 OS/container identity 落地前，自生成代码只能 offline replay。networked canary 只能运行
operator-reviewed immutable adapter；可信 runner 不得动态 import 或执行 proposal code。

## Connector 顺序

P0 先完成 invocation/artifact seam、protocol、runner、usage/quota/incident 和 adapter resolver。

P1 先做三个代表性真实 thin slice：

1. A 股公告：公开文件、分页、revision chain、bounded-window 枚举与旧路径对账；
2. SEC：官方 filing/attachment/item/facts、修订与来源完整性；
3. AlphaEngine：authenticated MCP、schema epoch、credential revoke、权限和 quota。

随后扩展：X/xreach、X/x_search、Reddit public fetch、Guidepoint、web search、web fetch。其余来源可以
并行准备 manifest、schema 和 recorded fixtures，但不能把所有 connector 都写完后才做第一次真实 E1。

P2 选择公告或 filing connector 跑第一条只读研究 WorkOrder：

`connector → source/numeric verifier → candidate staging → human review`

candidate staging 需要新增闭合契约。P2 不做正式 Evidence/Claim/Thesis commit，不关闭旧 cron。

## Go / No-Go 边界

Go：protocol、通用 A 股公告、全部 connector 路线图、offline replay、自生成 proposal/build、connector
shadow、fixture-only coordinator、operational verifier contract、Model IR ADR 和 formula census。

No-Go：无隔离的自生成代码 network canary、自动 production promotion、旧 cron cutover、正式 Ledger
commit、生产 credential 扩权，以及把 connector 输出接入当前万华 Agenda input。

完整阶段状态和 E1/E2 门槛见 [../PROJECT_STATUS.md](../PROJECT_STATUS.md)。
