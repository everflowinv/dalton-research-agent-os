# Connector P1-0d：AlphaEngine `mcp_managed` 离线 Recorded Shadow

日期：2026-08-15

状态：Claude Fable 5 对 committed tree `e48d76b` 给出 scope-limited Go

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
- 两次 committed-tree Python 3.13 no-build-isolation wheel 逐位一致，SHA-256 为
  `1d18058a2f00ecee014da41c0c1dd4a360067df1b700c20febd5899272b1349f`，每份 484,052 bytes；
- 干净 venv 安装后 `pip check` 通过，packaged AlphaEngine fixture 可加载，credential authority 创建四张表，
  SQLite integrity 为 `ok`；
- Claude Fable 5 逐文件敌对复核没有发现 P0/P1，对 committed P1-0d 给出 **Go**。

全量测试仍会从既有测试夹具打印少量未关闭 SQLite connection 的 `ResourceWarning`，但 341 项结果全部通过；
新增专项单独运行没有这些 warning。

## Go/No-Go 边界

本次 Go 只覆盖：

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

## Fable 裁决与下一阶段

Fable 5 认为 P1-0c 的轻量路由边界成立：Planner/协调器每个逻辑任务只选一次 source、operation、parameters、
completeness 与 fallback；physical page/retry 只做本地确定性 use-time gate。新 connector 的 contract 侧已经可以
通过三文件 proposal package 扩展；`mcp_managed` runtime gate 仍带 AlphaEngine 首个 reference chain 的硬编码，
等第二个真实 MCP consumer 出现时再按 template/operation/credential slot 参数化，不提前抽象。

下一阶段选择 **P2 coordinator foundation**，而不是继续复制 Guidepoint shadow。交付范围是：

1. 闭合 ContextPack、RunState、Checkpoint、ClaimIndex contract 与 validator；
2. fixture-only coordinator 消费现有 CNINFO、SEC、AlphaEngine recorded shadow，故障注入证明 checkpoint/resume 与
   bounded retry；
3. 由这个真实消费者首次引入 `CompiledConnectorPlan`，每个逻辑任务只生成一次并绑定到 RunnerRequest；
4. coordinator 只持窄 AuthorityPort，不能伪造 completion 或 Research Ledger 事实。

真实 MCP、Guidepoint/雪球、public network、Research WorkOrder live source、Evidence/Claim/Thesis commit、部署和旧
cron cutover 继续 No-Go。

Fable 另列出五项不阻塞离线 P1-0d、但 live MCP 前必须处理的债务：auth/permission failure 不能统一按 retryable；
live output validator 要覆盖 enum/maximum/maxLength/format；credential idempotency hash 要去掉 authority clock；
opaque handle denylist 要收紧；补 credential-table 并发探针。`authorize_use` 后、`transport_started` 前崩溃会保守消耗
一个 max_calls slot，当前列为 P3 liveness trade-off。
