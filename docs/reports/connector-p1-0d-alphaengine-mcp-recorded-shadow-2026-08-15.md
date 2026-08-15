# Connector P1-0d：AlphaEngine `mcp_managed` 离线 Recorded Shadow

日期：2026-08-15

状态：本地候选；等待 Claude Fable 5 committed-tree 独立复核

## 目的

P1-0d 验证 authenticated MCP connector 是否能在不复制 credential、不混入 public HTTPS transport、也不访问
真实数据源的前提下，走完 Dalton 的 connector authority 链。本轮只覆盖 AlphaEngine `search_library`，不把
`get_document`、Guidepoint、雪球或 live MCP 一并打开。

这一步也明确 connector routing 的成本边界：Planner/研究协调器在逻辑任务开始时选一次 source、operation、
parameters、completeness 与 fallback；每个 physical call 只做 lease、quota、schema、credential、retry 和
provenance 的本地确定性检查。多个 operation 可以归在同一个稳定 source/profile 下；跨 source 的 findata
体验由 research recipe 组合，不把一个拥有多种数据能力的 skill 强行压成一个巨大 connector。

## 实现

### 独立 MCP wire

- `ConnectorRunnerRequest` 0.2 精确绑定 transport plan ref/hash；既有 public runner 明确拒绝 0.2；
- `McpManagedAdapterRequest` 0.2 只携带 authority refs/hashes、query/schema、limits 与 raw sink ref；
- `McpManagedTransportObservation` 0.2 闭合 success/failure、provider status、cursor、source status、
  completeness 和 error；
- `mcp_managed` profile 必须把 `allowed_hosts=[]`、`network_policy=null`，并声明至少一个 logical
  credential slot。它不能把 loopback MCP 冒充 public HTTPS host。

### Credential authority

新增 append-only SQLite authority：

- immutable grant metadata；
- one-time revocation event；
- 每次 physical attempt 的 credential use receipt；
- credential operation 的 idempotency rows。

Core 不保存 token、cookie、OAuth config、server config 或 host path。Grant 精确绑定 connector profile、
CapabilityLease、adapter、principal、credential slots、allowed operations、expiry 和 `max_calls`。Runner 在
reservation 后取得 durable use receipt，在真正调用 adapter 前再验证 receipt、revoke、expiry、max_calls 和
所有引用的 authority。Credential resolver 只能返回不可序列化的 host-owned opaque object。

### AlphaEngine recorded fixture

Packaged deterministic fixture 绑定 P1-0a inventory 中的 AlphaEngine profile、fixture cases、transport target、
`search_library` input/output schema、parent parameters/query hash，以及 selected scenario ref/hash/behavior。

场景包括：

- success、empty、partial、pagination；
- schema drift、rate limited、timeout、malformed；
- permission denied、revoked。

Adapter 是纯离线 replay stub，不导入 socket/HTTP/MCP client，也不接收可序列化 credential。Timeout 通过可控时钟
让 Runner 的 hard deadline 生效；失败场景不生成假 ArtifactVersion 或 SourceEnvelope。

## 当前验证

- MCP/credential/Runner/transport/packaging 组合：41/41；
- Python 全量：341/341；
- OpenClaw model broker：15/15；
- `compileall`：通过；
- `git diff --check`：通过；
- fixture regeneration：AlphaEngine packaged JSON 重建前后 SHA-256 一致，
  `51ddd1599ec243ccfcd04af372bdd89d0bc6dfc3de92e8763e65a4346caf09b3`。

全量测试仍会从既有测试夹具打印少量未关闭 SQLite connection 的 `ResourceWarning`，但 341 项结果全部通过；
新增专项单独运行没有这些 warning。Committed archive wheel、clean install 与 Fable 5 裁决将在冻结候选后补入。

## Go/No-Go 边界

当前只申请以下范围的 Go：

- AlphaEngine `search_library` 离线 recorded shadow；
- `mcp_managed` wire 0.2 contract；
- grant/revoke/max_calls use-time authority；
- synthetic fixture 下的完整 connector fact chain。

以下仍为 No-Go：

- 真实 AlphaEngine MCP、token 或数据访问；
- `get_document` 与多 operation research workflow；
- Guidepoint、雪球 authenticated host-tool runtime；
- public network canary；
- Research WorkOrder、ContextPack、Evidence/Claim/Thesis commit；
- 部署、旧 cron cutover。

## 下一步裁决原则

Fable 5 对 committed tree 给出 Go 后，再在以下两条中选最小消费者驱动切片：

1. 复用 `mcp_managed` wire 接 Guidepoint 的离线 `search_library`/`get_document` recorded shadow；
2. 进入 P2 research coordinator，先冻结 ContextPack/RunState/Checkpoint/ClaimIndex，再让 coordinator 消费
   已验证的 CNINFO、SEC 与 AlphaEngine reference shadows。

不为“未来可能需要”提前建立独立 Connector Router。只有 P2 coordinator 出现真实跨 connector 调度消费者后，
才把一次性 route decision 固化为 `CompiledConnectorPlan`。
