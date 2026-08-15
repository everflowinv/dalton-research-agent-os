# P2 Coordinator Foundation

日期：2026-08-15

状态：本地候选；Fable 5 已批准阶段选择并对 committed tree 给出 Go，远端 CI 待完成

## 阶段裁决

本轮先回答 connector 路由是否过重、增加 connector 是否会迫使中央代码不断改动。

Fable 5 的裁决是继续 P2 coordinator foundation，但采用一次规划、轻量执行：

- Planner 每个 WorkOrder 只做一次语义选择，生成一个包含多个 connector step 的闭合计划；
- page、retry 和并发 physical call 不再调用模型或语义 Router，只执行本地 lease、quota、auth、schema、
  completeness 和 provenance gate；
- 新 connector 继续用自描述 profile/schema/fixture/proposal package 加入候选池，不为每个 connector 增加
  中央 if/else；
- findata 等多数据能力保留为 research recipe，由一次性计划编排多个 source/profile/operation。

Fable 5 认为 P1-0d 的 offline recorded scope 已可 Go，下一阶段应先建立 ContextPack、RunState、Checkpoint、
ClaimIndex 和一个真实消费者，再继续复制 Guidepoint shadow。

## 实现

### 一次性 connector plan

`CompiledConnectorPlan` 0.1 冻结：

- task、planner 和 routing policy ref/hash；
- 每个 step 的 source/profile/operation/parameters/query；
- input/output schema、completeness、dependency、fallback 和 max attempts；
- 连续 ordinal、step hash 和整个 plan hash。

`ConnectorRunnerRequest` 0.1/0.2 没有改 epoch。它只增加一组可选 plan binding；只要出现其中任一字段，
`compiled_connector_plan_ref/hash` 与 `compiled_step_ref/hash` 四项必须全部存在。既有 caller 不带这组字段时
继续使用原 wire。P2 fixture request 会绑定 exact plan、step、WorkOrder、profile 和 call-spec parameters。

### ContextPack 与 ClaimIndex

`ContextPack` 输出不保存正文，只保存 input ref/hash、优先级、是否选择、丢弃原因和冻结后的 token/byte 账。
builder、selector、tokenizer 与 whole-input truncation 都有版本 ref/hash。相同 ref/hash 会确定性去重；相同 ref
却带不同 hash 会 fail closed。

`ClaimIndex` 从已有 ClaimVersion、EvidenceRelation 和 ledger snapshot ref/hash 构建。它保存结构化检索字段、
status、source lineage 和 search terms，不提供任何 Ledger mutation。空 Ledger snapshot 也是合法投影。

### Fixture-only coordinator

`FixtureResearchCoordinator` 只接受一个 `ConnectorExecutionPort.execute`。它不持 ConnectorStore、Scheduler、
Credential Authority、Research Ledger 或 Core DB connection。参考 port 只读取仓库内三份 recorded fixture：

1. CNINFO `list_announcements`；
2. SEC `list_filings`；
3. AlphaEngine `search_library`。

port 返回的 `ConnectorCompletionReceipt` 必须绑定 exact RunnerRequest ref/hash。failed/retryable receipt 与
checkpoint 不能携带 SourceEnvelope、artifact 或 cursor。coordinator 只能把已验证 receipt 转成私有 scratch
checkpoint；它不能创建 connector authority 或 Evidence/Claim/Thesis。

### 恢复与重试

owner-only SQLite 保存 immutable plan、context、index、checkpoint 和 run-state version。checkpoint chain 精确
绑定 run attempt、plan、context、step、连续 connector attempt、receipt 和 prior checkpoint。run state 每次从
checkpoint 重建，scratch DB 不属于权威账本，可以删除重建。

Fable 5 对首个 committed tree 给出 scope-limited **Go**，同时指出 live admission、恢复链和投影诚实性仍需
收紧。本候选已补上以下边界：

- public runner 在还没有 compiled-plan authority resolver 时拒绝所有 plan binding，不能把调用方自报字段当权威；
- RunState 版本链固定 attempt、plan 和 ContextPack，第一份 checkpoint 写入前也不能换绑；
- 恢复时重新遍历 checkpoint sequence 与 prior ref/hash chain，不只相信 SQLite 排序；
- ContextPack validator 重算排序、去重、预算和选择结果，并扩大 credential-shaped 参数拒绝清单；
- fixture port 与 RunnerRequest 使用同一个幂等键，并对同键不同请求 fail closed。

Fable 的最终增量复核仍给出 **Go**，没有 P0；它另发现 MCP 0.2 gate 的 override 没有继承 public gate 的
plan-binding 拒绝逻辑。最终候选已把拒绝函数提升为两个 gate 共用，并补 MCP 回归；同时把敏感参数键做分隔符
归一化、恢复时重验 checkpoint 的 authority bindings、RunnerRequest ref/hash 和 idempotency key。真实 port 的
write-ahead intent 仍留到非 fixture transport 上线前实现。

故障注入覆盖：

- execute 已返回、checkpoint 尚未写入：恢复使用相同 idempotency key，port 不重复 physical transport；
- checkpoint 已写、run state 尚未更新：恢复先重建 state，不重复 connector call；
- state 已写：重复 run 直接返回相同 terminal state；
- 429/retryable：coordinator 立即返回 `retry_after_ms`，不 sleep；下一次调用使用预生成的下一 attempt request；
- retry 耗尽：达到 step `max_attempts=2` 后 run 终结为 failed，不执行第三次。

## 当前验证

- P2 专项：14/14；相关 runner/MCP/coordinator 组合 38/38；
- Python 全量：357/357；
- OpenClaw model broker：15/15；
- `compileall`：通过；
- `git diff --check`：通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 Python 3.13 no-build-isolation wheel 逐位一致，SHA-256 均为
  `0077ca167f0b7626910edb10aac719b11e7a08bbea3062f61be0d33eeb5cade6`，每份 507,712 bytes；
- 干净 venv 安装后 `pip check`、三步 plan build、packaged coordinator SQL 和 SQLite integrity 均通过；
- P1-0d 远端 CI：Python 3.11、Python 3.13、broker 全部通过；
- 当前 P2 候选尚待远端 CI。

全量测试仍会从既有 MCP/reference-shadow 测试夹具打印少量未关闭 SQLite connection 的 `ResourceWarning`；
357 项结果全部通过，P2 专项单独运行没有 warning。

## 当前边界

本阶段仍是 No-Go：

- live CNINFO、SEC、AlphaEngine 或其他数据源；
- credential、OAuth、MCP handle 或 public network；
- Guidepoint、雪球 live runtime；
- fallback branch execution；
- source/numeric verifier、candidate Evidence/Claim staging；
- Evidence/Claim/Thesis commit、部署和旧 cron cutover。

P2 候选复核通过后的下一步是 source/numeric verifier 与 candidate staging contract。它们先消费本轮 fixture
checkpoint 和 SourceEnvelope refs，继续保持离线、只读和人工 commit gate。
