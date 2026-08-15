# Dalton Connector Protocol 0.1

## 目的

Connector 只负责访问一个明确数据源，执行一个明确 operation，并返回可审计的原始结果。它不做研究
判断、不生成 Claim、不投递消息，也不持有 Dalton Core DB 路径。

Dalton 可以发现重复任务、生成 connector proposal、adapter 代码、schema 和 fixtures，但不能自行取得
生产网络、凭据或晋级权限。

## 分层

1. `ConnectorProposalManifest`：自生成 connector 的不可变提案清单；
2. `ConnectorProfileVersion`：人工批准后冻结 source identity/version、adapter、operation、host、schema
   ref/hash、runner environment hash、分页、完整性、
   credential slot、保留和网络边界；
3. `ConnectorCallSpec`：单个 WorkOrder 的 source-specific 请求，hash 进入 WorkOrder input refs；
4. `ConnectorInvocation`：logical invocation，1:1 绑定通用 `ExecutionInvocation(kind=connector)`；
5. `ConnectorQuotaReservation`：每次 physical attempt 调用前预占，并绑定 active policy ref/hash 与稳定
   quota scope；
6. `ConnectorPhysicalAttempt`：每次上游调用的真实结果，包括 429、timeout 和 indeterminate；
7. `ConnectorUsageEntry / ConnectorCostEntry / ConnectorQuotaSettlement`：计量、定价和结算；
8. `SourceEnvelope + ArtifactVersion v0.2`：来源时间、完整性、原始 artifact 和 provenance；
9. `ConnectorIncident / ConnectorSourceHealthEvent`：quota drift、schema drift、认证和源故障。

## Adapter 最小接口

Adapter 是 source-specific 纯边界。可信 Runner 传入：

```json
{
  "protocol_version": "0.1",
  "runner_request_ref": "connector-runner-request:...",
  "runner_request_hash": "<sha256>",
  "connector_invocation_ref": "connector-invocation:...",
  "profile_ref": "connector-profile:...",
  "profile_hash": "<sha256>",
  "call_spec_ref": "connector-call:...",
  "call_spec_hash": "<sha256>",
  "reservation_ref": "connector-reservation:...",
  "reservation_hash": "<sha256>",
  "physical_attempt_number": 1,
  "operation": "list_announcements",
  "parameters": {},
  "allowed_hosts": ["www.cninfo.com.cn"],
  "credential_grant_ref": null,
  "deadline_at": "2026-08-14T12:00:30.000000+00:00",
  "max_response_bytes": 1000000,
  "max_records": 1000,
  "raw_sink_ref": "raw-sink:opaque-handle"
}
```

Adapter 只能返回 transport observation，不得伪造 authority id：

```json
{
  "protocol_version": "0.1",
  "request_hash": "<sha256>",
  "outcome": "succeeded",
  "provider_request_id": "...",
  "provider_status_code": 200,
  "retry_after_ms": null,
  "structured_output": {"records": []},
  "source_record_refs": [],
  "cursor": null,
  "provider_usage": null,
  "error": null
}
```

Adapter 不报告 authority 时间、raw hash、artifact ref、Usage/Cost 或 settlement；这些值只能由可信 Runner
观察、计量并经 writer 登记。Runner 在派生 reservation 前和 transport 前分别重做 use-time gate，且
transport 前的最后一次 lease gate 必须晚于 reservation use-time validation。Runner 负责
lease/profile/schema/hash 复核、quota reservation、网络策略、credential grant、frame/timeout 上限、
durable journal/raw spool、raw artifact 注册、Usage/Cost/Settlement 和 ResultEnvelope。Adapter 不收到
reservation 写权限、Core token、真实 credential value 或数据库路径。

当前实现已完成 control-plane contract、静态 resolver、双 use-time gate、durable journal、bounded
content-addressed raw spool、窄 AuthorityPort 和 recorded transport executor。AuthorityPort 只暴露七类
authority 写入、闭合 immutable receipt 读取和受约束的 Scheduler journal reconciliation，不把 Store 或
SQLite connection 交给 coordinator。它只执行仓库内 fixture，
没有 socket、credential grant、writer RPC 或真实 source。这个切片只允许 `auth_mode=none`；
Runner 专用 authority API 派生 policy version、连续 reservation 序号、Profile 最大 bytes/records、保守成本和 TTL；
released reservation 仍占序号，所以 physical attempt 可以留洞。Runner
并按 invocation/reservation/attempt 派生 opaque raw sink handle。

`ConnectorRunnerResponse` 从 0.1 升到 wire 0.2：physical attempt、Usage、Cost、Settlement 和
ResultEnvelope 仍是每份正常 response 的必需引用；raw ArtifactVersion 与 SourceEnvelope 的 ref/hash
成对可空。`succeeded` 必须两者都有，429/timeout/failed 不用零字节 artifact 伪造来源事实。

Runner journal 在 Core DB 内保存 append-only request/event，状态为：

`admitted → reserved → transport_started → observed → responded`

- W0：reserved 前崩溃不动 quota，Scheduler lease 到期后重排；
- W1：reserved 后、transport_started 前可证明没调 adapter，恢复为 released；
- W2：transport_started 后、observed 前结果未知，恢复写 indeterminate attempt、unavailable Usage、
  reserved upper-bound estimated Cost 和 indeterminate Settlement；Scheduler 独立到期重排；
- W3：observed 后按确定性 key 重放所有 authority 写入；
- W4：responded 后恢复为 no-op。

journal 整体缺失但存在未结算 reservation 时，恢复按 indeterminate，不按 released。这个兜底把
reservation `created_at` 作为 unknown-start lower bound；它是保守恢复标记，不声称知道真实 provider
调用时点。journal 不保存 scheduler lease token。

## P0-3 metadata 与 transport safety

OpenClaw skill/MCP 先进入独立的 metadata snapshot authority，不直接进入 live Catalog：

- skill 只导入 compact metadata、opaque instruction ref/hash；不导入 skill instruction 或路径；
- MCP tool 只导入 compact metadata 与闭合 input/output schema ref/hash/body；不导入 server config、prompt、
  tool output 或 credential；
- imported metadata 生成 descriptor proposal 后，仍要经过 Registry 的 active human approval。Catalog 会
  把 proposal 的 kind/name/summary/source/contract/source hash/schema hash 与 current imported metadata
  exact 对照；
- complete scope 的 metadata/source/schema 漂移或删除会撤下 current descriptor 并推进 epoch；partial
  scope 缺项不作删除。这样旧 lease 在新版本获批前已经 fail closed，而不是继续调用 stale MCP schema。

公开 HTTPS transport 与 authenticated transport 是两条不同权限边界：

- public transport 没有 credential authority 参数，只允许 HTTPS/443、exact host allowlist 和受限
  header/body；URL、query、header 或 JSON/form body 中出现 credential-shaped 字段即拒绝；
- 每个 URL/redirect hop 都解析完整 DNS answer set；任一非公网 IP 即拒绝。socket 连接已验证 IP，TLS
  仍用原 host 做 SNI/证书验证，阻止验证后再次 DNS lookup 的 rebinding；
- `CredentialGrantEnvelope` 只携带 grant/profile/lease/adapter exact ref/hash、logical slot、operation、
  expiry 和 max calls。真实 credential 与不可序列化 handle 留在 host-owned `CredentialAuthorityPort`；
- AdapterRequest 0.1 仍要求 `credential_grant_ref=null`。AlphaEngine 这类 loopback MCP 必须发布独立的
  mcp-managed profile/runner wire，不能伪装成 public HTTPS host，也不能复用 public transport。

当前 public transport component 尚未接 source-specific adapter 或真实网络；P0-3 仍是离线实现。

### P0-4a trusted snapshot sync

Snapshot wire 0.2 新增 `source_instance_ref`、`exporter_version`、严格整数 `catalog_generation` 和成对的
`prior_snapshot_ref/prior_snapshot_hash`。Exporter 的本地排序只负责重试效率，真正的顺序 authority 在
CapabilityCatalog：

- source instance 必须先由 trusted operator resolver 返回 active human registration；source reset 必须换
  新 instance，旧 instance 不能自行重新注册；reset 会先撤下旧 source 的 current metadata/descriptor，并在
  存在 live descriptor 时推进 catalog epoch；registration receipt 必须绑定 reset 前的 exact active
  source/hash，并发 reset 只有一个能提交；首次注册同样会撤下 P0-3 legacy current state；registration
  wire 0.1 要求 `effective_until=null`；
- source head 为空时只接受 generation 1 且 prior 为空；之后只接受 exact next generation 和 exact prior
  head；
- exact current generation/ref/hash 是 duplicate；低 generation 是 stale；同 generation 异内容是
  equivocation；跳号是 gap；prior 不匹配是 fork；
- 所有拒绝都追加只含 ref/hash/generation/outcome 的 ingest event，但不能改 current metadata、descriptor、
  source head 或 catalog epoch；接受路径把 ingest event、snapshot、head、metadata/schema 和 descriptor
  withdrawal 放在一个事务；同一 generation 的并发 loser 要在锁内重新分类并写 equivocation event；
- host-owned exporter 以 owner-only SQLite 保存一个 pending snapshot。Catalog 接受后若在 acknowledge 前
  崩溃，重启必须先重放同一 snapshot，收到 duplicate 后才能推进下一 generation。

Exporter 只接收已过滤的 compact records，不读取或持久化 skill path/instruction、MCP server config、
credential 或 tool output。这是可测试的 sync component，不代表已连接 OpenClaw live inventory；live attach
仍须单独 gate。

P0-3 的 immutable snapshot base table 不原地 ALTER。Wire 0.2 source instance、generation、prior chain、FK、
unique 和 CHECK 约束放在严格 1:1 sidecar table；因此 fresh DB 与升级 DB 使用同一 DDL，legacy wire 0.1
snapshot 不需要伪造 source registration。

### P0-4a Connector Shadow projection

Connector Shadow projection schema 发布为 0.2。它是可丢弃、可重建的只读派生库，不是 admission
authority。Projector 以 SQLite `mode=ro`
读取 Core 和 CapabilityCatalog，完整替换 projection DB；Runner、Scheduler、Catalog 和 quota gate 都不能反向
读取这个投影。

- metadata source 只投 active source、generation/head、freshness、最新 ingest 和最新 reject outcome；
- connector 只投 profile/operation/source identity/auth mode、最新 source health/circuit、physical attempt outcome/
  retry、每个 attempt 最新 Usage revision 及其 Cost、reservation 最新 Settlement、quota window 汇总与 incident
  状态；
- projection 不含 raw body、`record_json`、incident detail、credential value/slot、provider request/usage ref、
  artifact locator 或 Core/Catalog 路径；API 只提供固定 `GET /v1/metadata-sources` 和 `GET /v1/connectors` 查询；
- 旧 Core/Catalog 完全没有新 authority 表时返回空数据并标 warning；只出现部分 authority 表时 fail closed；
- source watermark 纳入会影响投影的 connector authority 和 metadata registration/head/ingest event；这些
  authority 的新事实会让下一次 rebuild 生成新水位。投影失败不会改变 source DB。

`ConnectorCallSpec.parameters` 不保存 token、cookie、password、API key 或其他 credential-shaped 字段；
凭据只能通过 profile 声明的 slot 交给 Runner。authority 会核对 query hash 和拒绝这些敏感字段；可信
Runner 使用 operator 安装、由 environment/package manifest 约束的 input validator，按 profile 冻结的
input schema ref/hash 验证完整参数结构。

CapabilityDescriptor 的 `source` 表示 capability 实现来自 skill/MCP/tool/plugin 的哪一份版本；
ConnectorProfile 的 `source_identity` 表示 adapter 实际访问的目标数据源。两者不是同一个 ref。Runner
分别核对 live Descriptor contract 与 target source binding，不能用一个含糊的 `source` 字段替代两者。

## P1-0 十类 Inventory 与 recorded reference shadow

P1-0 inventory 固定十个独立 connector profile：CNINFO、SEC、AlphaEngine、X/xreach、X/x_search、
Reddit/last30days keyless、Guidepoint、Gemini web search、public web fetch、雪球。Profile 拆分按实际
transport、auth、completeness 和 operation 语义，不按“都是搜索”或“都是社交数据”合并。

Inventory 只表示 contract 已冻结并可离线验证。所有 profile 都必须满足：

- `readiness.level=inventory_connected`、`lease_eligible=false`、`live_execution_allowed=false`；
- proposal 为 `proposal_only` 且 `requested_canary=null`；
- 每个 operation 都有 success、empty、partial、schema drift、429、timeout、malformed fixture；有分页的
  operation 另有 pagination，host-auth profile 另有 permission denied/revoked；
- packaged profile/fixture/proposal/index graph 与 deterministic build 完全一致，不能仅靠攻击者可重算的
  content hash 证明可信；
- prompt、instruction body、local path、server/OAuth config、credential material 和真实 tool output 不进入
  inventory package。

雪球 profile 的主 route 是 host-owned `agent-reach XueqiuChannel`。`cn-hk-findata xq_hot_rank` 只作为
`get_hot_stocks` 的 operation-scoped fallback；fallback 必须带独立 target/source/adapter/provenance label，
不得用于 `get_hot_posts`、`search_stock` 或 `get_stock_quote`，也不得冒充雪球帖子正文。

CNINFO 与 SEC 的 recorded reference shadow 继续使用 offline adapter，不打开 socket。Runtime adapter 的
fixture 必须与 packaged deterministic fixture 逐对象相同；fixture 冻结 parent operation、完整 base
parameters 和可复算 query hash。执行计划还显式绑定 selected scenario ref/hash/behavior，不能用 adapter
constructor 的隐藏状态改写结果。一个 bounded logical query 可以产生多次 physical attempt，但每一页都要
独立 reservation、计量、结算和 raw artifact version。
第一页使用 AdapterRequest 0.2 的空 prior authority；后续页必须同时绑定：

- parent `ConnectorCallSpec.query_hash`；
- 上一页 AdapterRequest hash 和 AdapterTransportObservation hash；
- 上一页 succeeded PhysicalAttempt ref/hash；
- 连续 page/cursor 和当前 page-specific parameters/query hash。

任何 prior hash、cursor、ordinal、fixture/profile/template graph 或日期窗口不一致都在调用 adapter 前拒绝。
到达分页终点才可声明 `enumerated`；达到 `max_pages` 但仍有 cursor 必须写 `partial`。每页标准化记录的
`revision_of_ref` 只能指向本次 shadow 更早出现的记录。Adapter 的 normalized page output 必须通过 inventory
冻结的 closed output schema；缺字段、多字段或类型漂移都不能进入 authority。

每个成功页先注册自己的 ArtifactVersion，再把闭合 page receipt 写入 journal。`reserved` 可证明未执行时释放，
`transport_started` 按 indeterminate 结算，`observed` 使用 transport 前冻结的 commit context 完成，不在恢复时
重新授权已经发生的 transport；page2 capacity failure 会保留 page1 已注册 artifact，并让 parent 得到闭合
retryable completion。

Parent runner journal 在 Scheduler completion 前持久化闭合 response、ResultEnvelope、page receipts、commit
context 及其 hash；任何恢复都先完成 closed validation 和 immutable authority 交叉核对。Scheduler 只从构造时
绑定的 trusted RunnerJournal reader 读取 parent responded event 和 page completion event，caller 不能提交 event
hash/time 充当 proof。受限 reconciliation 只接受 lease 期内已落 journal 的 exact attempt/result authority；即使
恢复发生在 lease 过期后也可收敛，但 later attempt 已被重新 claim 时必须 fail closed。重放不能多写
SourceEnvelope、artifact、usage 或 settlement，也不能让 response outcome 与 Scheduler outcome 分叉。

这两个 shadow 不进入 Research Ledger。只有后续 ContextPack/ClaimIndex builder 和独立 verifier 才能把
SourceEnvelope 转成 candidate Evidence/Claim；P1-0 不写 Evidence、Claim 或 Thesis。

### Connector 粒度与路由

Connector 是 source、transport、auth、completeness 和 provenance 的权限边界，不要求“一个 connector 只能有
一个动作”。同一个稳定 source/authority 可以在一个 profile 中声明多个闭合 operation；findata、公司深研等跨
多个 source/auth 边界的能力属于 research recipe，由 coordinator 编排多个 connector，不能把不同 authority
塞进一个不透明 connector。

Planner 每个 WorkOrder 只做一次语义选择，冻结需要的 source、operation、参数、completeness 和允许的 fallback。
Runner 不在每个 physical call 重跑模型或语义 Router；它只执行本地、确定性的 use-time lease、quota、host/auth、
schema、retry 和 provenance 校验。分页和并发调用复用已冻结计划，但每个 physical attempt 仍须独立 admission、
计量和结算。`CompiledConnectorPlan` 在 P2 coordinator 开始消费该对象时再引入。

### 可扩展 proposal package

冻结的 P1-0a 十件 inventory 继续由 deterministic build 和 exact package comparison 保护。新增 connector 不再要求
修改这份中央 Python allowlist；offline proposal loader 接受一个只含以下三份 JSON 的目录：

- `profile.json`：source/transport/auth/operation/schema/readiness contract；
- `fixture.json`：逐 operation synthetic scenario matrix；
- `proposal.json`：wire 0.2 proposal-only manifest 与跨对象 hash/ref binding。

loader 只产出经过验证的数据对象，不注册 Catalog、不生成 lease、不请求 canary，也不加载或执行 adapter code。
任一 live/lease escalation、非 synthetic fixture、敏感配置或 credential、额外文件、schema/hash 漂移、跨对象
source/transport/auth/operation/fixture 分叉都会 fail closed。Human promotion、Catalog publish、静态 resolver 安装
与每次 physical call admission 仍使用原有 authority gate。

## 自生成模板

Dalton 发现同一种 source/operation 重复出现或现有 connector 缺能力时：

1. 新建 `CapabilityProposal(kind=connector)`；
2. 生成闭合 `ConnectorProposalManifest`；
3. 生成 adapter package、input/output schema 和 recorded fixtures；
4. fixture 必须覆盖：正常、空结果、分页、partial、schema drift、429、timeout 和 malformed response；
5. 在无网络、无凭据、无 Core DB 的 sandbox 中 replay；
6. 独立 evaluator 验证 output schema、completeness、provenance、资源上限和无越权副作用；
7. 人工批准 canary 后，operator 将 immutable adapter 安装到可信 Runner 的静态 resolver；
8. canary 使用固定 host/operation、短期 credential slot 和独立 quota；
9. canary eval 通过后再次人工批准 production promotion；
10. source/adapter/schema/MCP epoch 任一变化都使旧 lease 失效。

可信 Runner 不得从 proposal path 动态 `import`、`exec` 或加载任意 entrypoint。独立 OS/container identity
上线前，自生成 adapter 只能 offline replay；networked canary 只运行 operator-reviewed immutable package。

## 完整性声明

- `enumerated`：在明确 bounded window 和分页终点内可对 ID/revision chain 对账；
- `ranked`：搜索/推荐结果，只代表排序后的部分结果；
- `partial`：上游或本地明确缺页、截断或失败；
- `unknown`：不能证明完整性。

`xreach` 与 `x_search`、web search 与 web fetch 必须使用不同 profile。聚合 research skill 不能冒充一个
不可拆分的 connector。

## 计量与故障

- 每个 physical attempt 先 reservation，再调用；没有 reservation 就不 admission；
- attempt 的 `started_at` 必须处于 `reservation.created_at <= started_at <
  min(expires_at, window_ends_at)`；当前所有 outcome 都是终态，必须有 `completed_at`，且完成时间不能晚于
  authority 写入时观察到的时间；
- rate policy 用 append-only activation event 选择 exact active version；固定 UTC window 按稳定 quota scope
  跨 policy version 聚合，旧版、未来版和过期版 fail closed；
- 只有确认未登记 physical attempt/Usage/provider request 的 reservation 才能 released；released 后禁止
  再登记 attempt；
- RatePolicy 必须冻结 profile、币种和整个 policy interval 内的完整 canonical price book，包括显式
  zero-rate；每个 required meter 只有一条覆盖 policy 生效区间的 rate，同一 profile/meter/currency 的
  rate 生效区间不得重叠。admission 按当前时点重新枚举 canonical rates 并要求 exact equality，再按
  profile 最大 bytes/records 和 1 次 call 计算保守成本下限；
- 结算顺序是 `PhysicalAttempt → latest Usage → latest Cost → Settlement`。Cost 按 physical attempt 的
  `started_at` 再核对 canonical price book；漂移时不写 Cost，并写 blocking incident。consumed 只接受
  final Usage 和按 frozen price book 计算的 actual CostEntry；physical metrics 必须等于 Usage，
  cost/currency 必须等于 CostEntry 和 quota policy；
  非最终或未定价计量只能 indeterminate，并按 `max(reserved, measured)` 保守占额；
- Usage 或 Cost 出现新 correction、但 settlement 尚未同步时，在 correction 写入的同一事务立即写
  blocking `quota_drift` incident；这项检查不受 quota window 切换影响；
- 同一 `price_rate_ref` 的版本生效区间不得重叠，定价按 physical attempt 的 `started_at` 选择 exact rate；
- 429 写 `rate_limited + retry_at`，交回 Scheduler `not_before`，worker 不 busy wait；
- timeout 写 physical attempt `timeout`；若计量不完整，settlement 写 `indeterminate` 并保守占额；
- adapter observation 不能自报 timeout/indeterminate；Runner 用 hard watchdog、deadline 和 journal 判定；
- provider-reported overage 写 blocking incident，后续 reservation fail closed；
- local quota 只能保证不主动超发，不能承诺供应商计量永不漂移。

## 进入研究账本

成功或空结果永远先进入 raw ArtifactVersion v0.2 和 SourceEnvelope；没有 finalized raw object 就不能
登记 SourceEnvelope。429/timeout/failed 只保留 attempt、Usage、Cost、Settlement 和 ResultEnvelope，
不伪造 raw source。SourceEnvelope 必须绑定同一
execution 生产的 artifact version，并核对 raw hash、source、operation、schema、policy、provider request
和明确的 `result_physical_attempt_ref`；complete/empty 的 result attempt 必须 succeeded，error 的 result
attempt 不得 succeeded。`source_content_hash` 对 source、operation、record refs、四类时间、cursor、
provider request、completeness、status 和 error 的规范化投影计算。这个 hash 绑定 metadata 和 record
refs，不声称绑定 record body。只有 source/numeric verifier 生成的
candidate Evidence/Claim 才能进入人工审阅；正式 Evidence/Claim/Thesis 仍走现有 Ledger gate。聊天
transcript、adapter 日志、compaction summary 和 runtime scratch 都不是研究事实。
