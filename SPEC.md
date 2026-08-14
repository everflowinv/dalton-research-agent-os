# Dalton Core slices 1–7：契约规格

本仓库是独立于 OpenClaw 的 Dalton Core 原型。它冻结可迁移的语义和 interchange contract，
并可通过受限 broker 接入 OpenClaw 模型。只读导入器能归档 `workspace-chem`、Coverage OS
database 和 cron 定义，但归档不代表新系统采用旧约束、旧研究结论或旧执行方式。domain 对象
使用 `schema_version: "0.1"`，writer RPC
使用 `protocol_version: "0.1"`；不可变对象用新的 `id`/version 追加，不通过原地
覆盖形成历史。

## 已冻结契约

### 通用规则

- 六十二份 JSON Schema 都是 Draft 2020-12 文档，根对象有
  `additionalProperties: false`。
- 每个对象都有 `schema_version`、稳定 `id` 和 `created_at`；引用字段使用稳定
  ref 字符串，时间字段保持 RFC 3339 形式的字符串（具体时区策略留给实现层）。
- 角色、capability、runtime、provider 和 model 都是字符串/引用，不能由本契约
  固化为角色 enum。
- Python 端只依赖 stdlib；`from_dict` 严格拒绝未知字段，`to_dict` 产生可 JSON
  序列化的 round-trip 表示。首版不做省略核心字段的兼容解析：即使允许为空，
  `declared_side_effects`、`artifact_refs`、`actual_side_effects`、`usage_refs`
  也必须显式传入；版本迁移时另发新 schema。

### WorkOrder / ResultEnvelope

`WorkOrder` 是 Core 派发的最小执行请求：问题、所需 capability、
`runtime_profile_ref`、预算、幂等 key、输入 refs 和声明副作用。`ResultEnvelope`
是 runtime 返回的边界对象：work order/invocation refs、状态、结构化 outputs、
artifact refs、实际副作用、usage refs 和可选 error。runtime 自报完成不等于
verification 或 commit 通过。

### RuntimeProfile / ExecutionInvocation / ModelInvocation

`RuntimeProfile` 版本化声明 capability、隔离、工具、网络/文件系统、副作用、
资源限制、支持的 envelope 版本、runtime version 和 environment hash。
`ExecutionInvocation` 是 runtime-neutral execution 超类型；`ModelInvocation` 与
`ConnectorInvocation` 以 1:1 subtype link 引用它。`ModelInvocation` 记录实际
provider/model/model family、profile、capability、usage、输入/输出 refs、副作用、runtime 和 actor
provenance。新 ModelInvocation 在同一事务原子登记 execution supertype；历史模型调用只做等值 backfill，
不改原有 hash。

本地 deterministic executor 会检查输入/输出 schema version、WorkOrder budget、
RuntimeProfile limits，以及“声明副作用 ⊆ profile 许可、实际副作用 ⊆ 声明”。它只
运行 Core 内已注册的可信 handler，不是运行任意第三方代码的 sandbox。

`InvocationGranularity` 冻结为：`work_order`、`task`、`verification`、
`adjudication`、`system`。这只描述审计粒度，不描述角色。

### DomainEvent

事件 envelope 固定包含：事件 `id`、`event_type`、aggregate type/id/version、
可空的 `version_ref`、`content_hash`、
`created_at`/`occurred_at`、actor ref、payload、`idempotency_key`、
`correlation_id` 和可选 `causation_id`、`change_ref`、`verification_ref`。staged
事件允许 `aggregate_version: 0` 且没有 version ref；committed 的非空 version ref
及其与 immutable version 的对应关系由 store/commit gate 强制。事件是时序权威；
current pointer 和物化视图不是本 slice 的契约。

### Research Ledger 对象

- `ThesisVersion` 以 `thesis_ref + version` 形成 append-only 链，引用 claims、
  catalysts、falsifiers、verification 和 prior version，并保存 `content_hash`。
- `VerificationRecord` 记录 verifier invocation、findings、确定性检查、
  `revise_round`、独立性 policy ref、被验证 invocation refs 和目标
  `target_content_hash`。
- `GovernancePolicyVersion` 记录生效区间、policy payload、变更原因、actor、
  prior version、`content_hash` 和 independence predicates。
- `EvidenceVersion` 保存稳定 evidence identity、来源、检索时间、有效期、artifact
  refs、source lineage、independence group、actor 和不可变版本链。
- `ClaimVersion` 保存结构化断言、定量值/单位、产生它的 invocation refs、actor 和
  不可变版本链；`status` 不允许写入，只能由 relation、数值冲突和 adjudication
  投影得到。
- `EvidenceRelation` 以 `supports / contradicts / qualifies` 把不可变 evidence
  version 连接到不可变 claim version；source lineage 和 independence group 必须
  继承 EvidenceVersion，调用者不能重写。
- `AdjudicationVersion` 按 claim 保存独立 adjudicator、producer invocation refs、
  policy ref、findings、status 和严格版本链。调用者不能另选一个“方便独立”的
  subject invocation。

Thesis commit 引用的每个 ClaimVersion 必须存在，并且至少有一条 immutable
EvidenceRelation。三种 relation 都只满足“证据链存在”的结构 gate；证据是否足以
支持结论、contested claim 是否可用于某类决策，由 verifier 和版本化 governance
policy 判断，不在存储层硬编码。

0.1 只允许提交 `ThesisVersion`，且 verification 是不可关闭的结构约束；
`required_verification: false` 会被拒绝。若未来某类 decision 允许免验证，必须用
新 contract version 定义它的 provenance 和 event 语义，不能借 policy 值绕过。

同一 `policy_ref` 的历史生效区间可以重叠；执行和回放都以当时的 active pointer
以及 verification/committed event 固定的 policy version 为准，不按时间区间猜测
唯一版本。区间负责判断某个已指向版本能否生效，pointer 负责选择版本。

### CapabilityProposal

Proposal 记录 gap、预期收益、I/O/error/side-effect contract、权限、fixtures、
参与者和 artifact refs。`kind` 和角色参与者保持可扩展字符串；builder 不能借此
绕过独立验证或 promotion gate。`fixture_manifest_hash` 把 fixture ID 对应的 input /
expected-output manifest 固定进 proposal canonical hash；只重放相同 ID、却替换输入或
预期输出，不再属于同一 proposal。

### Capability Registry

`CapabilityVersion`、`CapabilityEvaluation` 和 `CapabilityDecision` 构成 append-only
能力生命周期：

- proposal 必须是 `proposed`，记录 proposal/artifact hash、builder invocation、
  actor 和 prior version；
- evaluation 记录 fixtures、baseline、明确 pass 结果、environment hash、builder /
  evaluator invocation，以及当时 active policy version/hash；builder 不能自评；
- approval 只接受明确成功的 evaluation；policy 在 evaluation 后变化时必须重评；
- reusable capability 只有经过 writer 认证的 `human:*` principal 才能进入 active
  registry；payload 里的 `human:` 字符串本身不构成认证；
- requested permissions 不能超过 proposal；未提供时冻结为 proposal 的完整权限；
- active pointer 只追加，rollback 只能回到历史上曾经 active 的版本，不能借
  rollback 激活未审批 proposal。

Capability Registry 的 proposal、artifact、environment、policy 和各级 content hash
统一使用 64 位 lowercase SHA-256 hex。Registry 不接受“任意非空字符串”冒充 hash；
这让 attestation 能直接绑定 immutable Registry row，不需要格式转换。

本 slice 不执行 proposal 中的任意代码。它只接收外部 sandbox 产生的 evaluation
证据；CapabilityAttestation 已定义这些证据的闭合结构，但真正的隔离 backend、
静态分析和历史回放执行器仍未实现。

### Writer command / response

`WriterCommand` / `WriterResponse` 是 `AF_UNIX` JSON-lines 协议，冻结
`protocol_version`、request id、operation、params、result/error envelope。writer
进程独占 DB path；client 不导入 SQLite，也没有数据库路径。

token 同时限制 operation、invocation refs、work order refs 和 actor：Core 先登记
真实 invocation，worker/verifier/researcher/adjudicator/builder/evaluator 只能引用被
分配的 invocation，不能内联伪造 provider、model family 或 actor。Capability
promotion/rollback 和 governance policy 变更的 actor 都由认证后的 human principal
token 注入；Core token 也不能更改 policy，调用者不能通过 payload 覆盖 actor。
writer 还阻止 scoped researcher 给其他 actor 的稳定 Claim/Evidence ID 追加版本，
或向不属于自己的 claim graph 写 relation；相同 relation 重放使用确定性 identity 和
幂等 key，返回 `fresh / duplicate / conflict`，不会悄悄追加等价关系。

writer 对连接设置 1 秒 idle timeout，并用有上限的连接线程处理 frame；SQLite store
仍由一个独立 writer thread 串行持有。未认证客户端发送半截 frame 不会占住 accept
loop 或阻塞其他客户端，但这不是面向公网的服务，也不替代进程/容器级资源隔离。

### Dalton-native Process Runtime

`ProcessRuntimeAdapter` 是第一个不依赖 OpenClaw 的 runtime 实现。它只接收严格的
`WorkOrder + RuntimeProfile`，向 argv 子进程的 stdin 写入一条 JSON request frame，
并要求 stdout 返回一条严格的 `ModelInvocation + ResultEnvelope` frame。adapter 会在
临时 cwd 启动子进程，使用最小环境白名单，设置 wall-clock、stdout、stderr 和 frame
上限；超时、超量、坏 frame、schema/ref/budget/side-effect 不匹配都会返回可判定的
错误。子进程不拿到 Core store、writer token 或任何权威路径。

实际 wall-clock deadline 取 adapter timeout、WorkOrder 和 RuntimeProfile 时间预算的
最小值，不采用 child 自报耗时。协议 0.1 还要求 `ModelInvocation.output_refs` 与
`ResultEnvelope.artifact_refs` 完全一致；未来若需要非 artifact output，必须发布明确的
映射契约，不能让两个 envelope 各写一套来源。

provider、model、model family 和 actor 不能由 child 自证。Core 在构造 adapter 时传入
可信 `invocation_identity`，adapter 要求 child 回传值完全一致；environment hash 必须
与 RuntimeProfile 一致。相同 identity 也必须写入版本化 RuntimeProfile 的 metadata，
避免它只存在于进程内配置。任一处不一致时，该 invocation 不得登记，更不能用于
independence gate。

内置的 stdlib formatter fixture 把同一组记录规范化为 canonical JSONL，并把 usage、
artifact、environment 和 invocation provenance 写入 envelope，作为 runtime 替换与
自生成 formatter capability 的最小基准。这个 adapter 不是安全 sandbox：同一 OS
用户仍可能观察进程或打开其可访问文件；执行未知代码前仍需要独立 OS identity、
container/VM 或具备独立授权身份的服务。

### Scheduler lease / retry / idempotency

`Scheduler` 把 immutable WorkOrder 排入 SQLite 队列，并用 append-only attempt event 与
lease revision 派发任务。`BEGIN IMMEDIATE` 保证同一 ready attempt 只能被一个 owner
领取；worker 只拿高熵 lease token，数据库只保存 SHA-256。续租必须匹配
work/attempt/owner/token，并受单次续租和总生命周期上限约束。

Scheduler 使用注入的可信 clock 判断到期，不接受 worker 自报时间。每个 WorkOrder 的
max attempts、默认/最大 lease、renew 和总生命周期都按入队时的 immutable policy version
执行；Core 后来激活更宽松的 policy，也不能放宽旧任务。过期 lease 会先追加
`expired`，再按入队时冻结的 policy 追加下一个 `ready` 或终态 `failed`；旧 owner 的迟到
结果一律拒绝。completion 在服务端重算 ResultEnvelope hash，用 fresh / duplicate /
conflict 三态处理幂等 key，并保证每个 WorkOrder 最多一个 formal result。retryable
结果会保留 immutable receipt，但只有受 policy 约束的新 attempt 可以继续执行。

### Capability sandbox attestation

`CapabilityAttestation` 把 proposal、artifact、dependency lock、environment/image、policy、
fixture manifest、runner、权限、limits、实际耗时/峰值内存/输出字节数、observed effects、
stdout/stderr 和结果 hash 固定成闭合、可重算的证据。
可信 launcher 提供 authority 字段；untrusted report 只能提供观测结果，不能选择 runner、
policy、grants 或 limits。fixture 集合和 input/output hash 必须精确匹配；network、credential、
Core DB、未声明写入或 builder 自评都会 fail closed；标记 `passed` 的结果只要超过 wall
clock、memory、stdout 或 stderr 上限也会被拒绝。

所有 hash 与现有 Capability Registry 对齐为 64 位 lowercase SHA-256 hex，不做第二套
`sha256:` 前缀格式。这个验证器不执行代码，也无法发现 runner 隐瞒的副作用；正式 promotion
要等独立 sandbox service 能以受限 writer 身份登记 attestation 后，才能把它设为强制 gate。

### Model endpoint 与确定性路由

`ModelEndpointProfileVersion` 保存 exact provider/model/family、adapter ref、逻辑
credential slot ref、capabilities、modalities、context、availability、cost 和 hard limits。
它不保存 API key、OAuth token、endpoint header 或认证库路径。`ModelRoutingPolicyVersion`
保存闭合的 profile/provider/family/adapter filters、required modalities、family independence
要求和有序 preference。

`ModelRouter` 只读取每个 profile 的最新 immutable version，并按 WorkOrder capability、
预算、context、credential slot availability、endpoint availability、modalities 和 policy
确定性筛选。每次 initial/retry/switch 都追加 `ModelRouteDecision`，绑定 WorkOrder hash、
attempt、policy/hash、完整 candidate snapshot/hash、constraints 和 exact selected endpoint。
fallback 不能由 provider 或 OpenClaw 静默执行；retry/switch 必须形成新 decision，且同一
attempt 不能 A→B→A 循环。verifier 需要 family independence 时，同 family 候选 fail closed。

`credential_slot_refs` 必须由可信 credential controller 注入；worker 自报的 slot 不构成
可用认证。Router 不读取 token，也不调用模型。

### 按需 Capability Catalog

`CapabilityDescriptor` 是版本化、带 namespace 的轻量能力目录。它分开保存检索摘要、
source/hash、contract refs、permissions、routing、eligibility 和 governance refs；catalog
只存 `credential_slot_ref`，不存 credential。目录调用分三步：

1. `search` 只查 compact index；
2. `describe` 在选中后返回一份完整 descriptor；
3. `prepare` 重新检查 catalog epoch、revision/hash、visibility、policy、WorkOrder 和
   side effects，再发短期 `CapabilityLease`。

命中结果不能直接执行。skill 只在命中后加载一份 instruction；tool 只在命中后加载一份
schema；实际 call 仍须经过 adapter、policy 和 lease 检查。MCP tool list/schema 变化会使
旧 epoch/lease 失效。Capability Catalog 不执行 skill/tool，不解析 credential，也不替代
Capability Registry 的 evaluation、attestation 与 human promotion。

`publish` 必须通过可信 approval resolver 取得 active human promotion receipt，并把 Registry
revision、artifact/schema/fixture-manifest/attestation hash 和批准权限固定进 descriptor；caller
自报的 approved 状态无效。`prepare` 通过可信 policy resolver 读取当前有效 policy，核对真实
hash、principal、TTL 和 permission subset。历史 `get_lease` 只证明曾签发；每次使用前必须
调用 `validate_lease_for_use`，重新检查 expiry、catalog epoch/current revision、approval、
principal、WorkOrder hash、policy、permissions 和 side effects。

### Workflow、Usage、Cost 与 Artifact authority

`WorkflowRunVersion` 与 `WorkOrderLink` 建立总任务和任务树。link 首版关系只允许
`decomposed_from / verifies / follows_up`；服务层拒绝 self-link、cycle、multi-parent、
孤立父节点和移除仍承载子树的 root。Scheduler retry attempt 仍属于同一个 WorkOrder，
不会被画成子任务。

`UsageEntry` 绑定真实 ModelInvocation，标准化 input/output/reasoning/cache tokens、request、
duration 和 byte metrics，并记录 metering source、measurement status、provider usage ref、
raw usage 和线性 correction chain。未知值必须为 `null`，不能写成 0；worker 自报只保留
较低信任来源。`PriceRateVersion` 保存精确生效区间和整数 micro-unit rate；`CostEntry`
绑定 immutable UsageEntry 与 exact rate versions，以 `half_up` 规则在服务端复算。校正只
追加新版本，不同币种必须分开汇总。

`ArtifactVersion` 只保存 title/kind/media type/hash/size/storage locator、producer execution、
WorkOrder、ResultEnvelope、access class 和 preview status。v0.2 通过 `producer_execution_ref` 支持
connector 等非模型执行；跨 v0.1/v0.2 index 防止同一 artifact identity 分叉。authority 验证 producer
output ref 与 ResultEnvelope hash，但不读取 artifact 内容；dashboard 默认不投影 storage locator。

### Connector Fabric authority

Connector 面向数据源和 operation，不面向单家公司。`ConnectorProfileVersion` 冻结 source identity、
adapter/source/schema hashes、runner environment hash、operation、host、credential slot、分页、完整性和三项 policy ref；
`ConnectorCallSpec` 冻结单次查询，`ConnectorInvocation` 与 `ExecutionInvocation(kind=connector)` 做 exact
subtype equality。

每次 physical attempt 必须先取得 `ConnectorQuotaReservation`。rate policy 通过 append-only activation
event 选择 exact active version；admission 以稳定 `quota_scope_ref + UTC window` 跨版本聚合，并用
`BEGIN IMMEDIATE` 串行化。RatePolicy 冻结 exact price refs、required meters 和 price-book hash；
admission 按 Profile 最大 bytes/records 与固定 call 数计算保守成本下限。同一 profile/meter/currency 的
rate 生效区间不得重叠。released reservation 不得登记 attempt；consumed/indeterminate settlement
必须精确引用该 reservation 的 physical attempt、latest Usage 和 latest Cost revision。consumed 只接受
final Usage 与按 frozen price book 计算的 actual CostEntry；physical actual 等于 Usage metrics，
cost/currency 等于 CostEntry 和 quota
policy。Usage/Cost correction 领先 settlement 时，在 correction 写入事务立即追加 blocking incident，
不依赖当前 quota window。
超 reservation 或窗口上限的 actual 会在同一事务追加 blocking `quota_drift` incident。

`SourceEnvelope` 只能引用同一 connector execution 生产的 ArtifactVersion v0.2；source、operation、output
schema hash、access/retention/terms policy、provider request、明确的 result attempt、raw artifact content
hash 都必须与 Profile/CallSpec/Attempt/Artifact authority 相等；source/schema/content hash 分别绑定冻结的
source identity、operation schema bundle 和规范化 source metadata/record refs，不声称绑定 record body。
Connector output 先停在 raw artifact 和
SourceEnvelope；未经 source/numeric verifier 不得进入 Evidence、Claim 或 Thesis。

Dalton 可以生成 `ConnectorProposalManifest`、adapter package、schema 和 recorded fixtures，并在无网络、
无凭据、无 Core DB 的 sandbox replay。静态 resolver、networked canary 和 production promotion 都需要
独立人工 gate；运行时不得从 proposal path 动态 import/exec。

可信 Connector Runner 的外部 command 只携带 authority refs/hashes。Runner 先核对 exact-current
Scheduler lease、CapabilityLease、Profile/CallSpec/Execution、静态 environment manifest 和 adapter binding，
再由 Runner 专用 authority API 派生 quota reservation；transport 前先核对 reservation 的 hash、有效期、
active policy、price book、incident/circuit state，最后再核对两类 lease。`ConnectorAdapterRequest` 的参数、
host、schema、deadline、上限和 raw sink handle 全部从最后一次 authority 重读结果派生。P0-2a 只允许
`auth_mode=none`。CapabilityDescriptor 的 source 表示 capability 实现来源，ConnectorProfile 的
source identity 表示目标数据源；Runner 分别核对 live descriptor contract 和 target source binding。
Adapter 只能返回 transport observation，不能自报 authority 时间、raw hash、artifact、
Usage/Cost 或 settlement；它只能报告 `succeeded/rate_limited/failed`，timeout 和 indeterminate 必须由
Runner 的 deadline/journal 证据判定。P0-2b 用 Core DB 内 append-only runner journal 固定
`admitted → reserved → transport_started → observed → responded` 四个 durable barrier；只有
`transport_started` 之前的 reservation 能 released，之后崩溃一律按 indeterminate 保守结算。

raw response 经 bounded write-only sink 流入 content-addressed spool，finalize 后才允许登记
ArtifactVersion/SourceEnvelope；超 `max_response_bytes` 不产 partial success。成功与空结果必须有 raw
artifact 和 SourceEnvelope，429/timeout/failed 不伪造空 artifact。为表达这项条件，
`ConnectorRunnerResponse` 使用 wire 0.2，raw artifact 与 SourceEnvelope ref/hash 按 outcome 可空；
request、adapter observation、SourceEnvelope 和其他既有 domain contract 仍保持各自的 0.1 epoch。

post-transport 写入只经过窄 `ConnectorAuthorityPort`。Core Connector/Observability 共用 DaltonStore，
Scheduler 是独立 SQLite authority，不存在跨库原子性；journal 与按 invocation/attempt/step 派生的
幂等 key 负责重放收敛。observed 后任一 authority 写入缝隙崩溃都必须只生成一份 attempt、Usage、Cost、
Settlement、Artifact、SourceEnvelope 和 ResultEnvelope。W2 恢复不持久化 Scheduler lease token：先做
indeterminate 结算，再由 Scheduler 自己让 lease 到期重排。

当前只允许 `auth_mode=none` 的 recorded fixture adapter。credential grant、真实网络 transport、SSRF
防护和 writer RPC 属于后续 slice。

### OpenClaw model broker adapter

OpenClaw OAuth/provider 复用走外部可信 `dalton-openclaw-model-broker` 插件。插件只调用
host-owned `api.runtime.llm.complete`，用 dedicated agent 和 exact model allowlist 选择
OpenClaw 内已管理的认证；Dalton 不读取 `openclaw.json`、认证数据库、OAuth token 或
refresh token。插件通过 mode-0600 UDS 接受一条 closed JSONL request，不允许 caller
传 agent、base URL、header、API key 或 auth profile。每条请求还必须带 HMAC-SHA256 admission，
绑定 client、timestamp、nonce 和完整请求；错误、过期和重放认证都 fail closed。

broker 在调用 host 前把 invocation reservation 以 owner-only journal 原子持久化；完成后才
写 completed response。重启后同请求返回 duplicate，不重复计费；不同请求为 conflict；崩溃
留下 pending 时返回 indeterminate，禁止静默重跑。journal 有 TTL、条数和字节上限，但为支持
duplicate 会保存模型正文，因此属于敏感缓存，必须和 HMAC key 一样置于独立可信 OpenClaw
identity 的私有 state directory。

`OpenClawModelAdapter` 只接受 `WorkOrder + accepted ModelRouteDecision + exact profile`；
它必须通过注入的只读 route resolver 按 decision ID 取回 Router authority wire，并与 caller
内容做 exact canonical equality，不能接受自洽但未入账的 decision。adapter 再校验 Node/Python
两侧 canonical hash、actual provider/model/agent、usage/cost、frame、timeout 和预算，产生未
提交的 `ModelInvocation + ResultEnvelope`。成功结果使用 Scheduler 接受的
`succeeded`，并生成确定性 usage ref；provider cost 只作为 uncommitted telemetry，必须再
进入 Usage/Cost authority。broker 不持 Dalton DB path 或 writer token。

broker 已作为本机 linked plugin 安装，operator policy 只允许六条 exact model route，并把
agent override 限定到 Dalton dedicated agent。六条 route 已分别做真实 completion smoke test；
broker 仍不是 commit boundary，模型输出必须回到 Core verification/writer。当前 broker、可信
controller 与普通 worker 仍运行在同一 OS identity，正式 hostile-worker 隔离仍需要独立
OS/container identity。

### Legacy Dalton 影子迁移

`legacy_migration` 只读遍历旧 Dalton 的约束、研究产出、模型、wiki、references、memory、
scripts、config 和 price data；live Coverage SQLite 通过 backup API 取得一致快照，不复制 WAL/
SHM。每个文件进入 owner-only content-addressed store，并登记不可变 `ArtifactVersion`、producer
invocation 和 ResultEnvelope hash。`temp/`、`.git/`、共享 skill symlink 和 runtime cache 明确排除。

旧 Thesis/Task 只作为 legacy export 和数据库快照导入：belief 标为
`quarantined_pending_core_verification`，task 标为 `shadow_only`。旧 cron 定义完整归档并标为
`shadow_registered_not_scheduled`。每个旧任务都要重新判断是删除、改成事件、保留确定性 schedule，
还是按新 WorkOrder/Capability contract 重写；不得因完成归档就自动创建 Core schedule。

### 只读工作监督台

`DashboardProjector` 用 SQLite `mode=ro + query_only` 读取 Core/Observability 与 Scheduler
authority，选择 latest workflow/usage correction/cost correction/artifact version，生成可随时
重建的独立 projection DB。租约已过期但 Scheduler 尚未 sweep 时，页面显示“等待调度回收”，
保留底层 `source_state/source_ref/status_reason`，不能继续显示“执行中”。缺表、缺归属、未定价
或 usage 缺失都会设置 `partial_data` 和 warnings。

projection 永远在同目录私有临时文件中构建，再用 `os.replace` 原子替换；projector 不直接打开
caller-controlled 最终路径写入，避免 hardlink/symlink TOCTOU 污染 authority。Dashboard HTTP
服务只绑定 `127.0.0.1` 或 `::1`，校验 literal loopback Host header，只开放固定 GET query、
只读 projection DB；不拿 Core DB path、Scheduler mutation 权限或 writer token。API 不返回 prompt、raw usage、storage
locator、完整 outputs 或 credential。成本按 currency 分组，不能用当前价格或汇率重算历史。
网页用通俗中文显示总任务、任务树、Scheduler 状态、模型/usage、能力批准状态和 artifact
metadata；批准、暂停、取消仍属于未来 command service，不塞进 query API。

### 常驻 controller 与静态看板插件

`daltond` 是确定性常驻控制进程，不是长生命周期 LLM session。它只负责 Scheduler lease
回收、authority 变更检测、projection rebuild、内建插件重试和 owner-only heartbeat；当前
版本不 claim research WorkOrder，也不冒充 Agenda Engine、worker coordinator 或 verifier。
LaunchAgent 只对 writer 和 controller 设置 `RunAtLoad + KeepAlive`，研究 worker 仍按
WorkOrder 启动并在完成后退出。

插件只从内建 registry 选择，配置不能任意 import Python 模块。`static_dashboard` 只读
disposable projection DB，把固定 query surface 嵌入单一 HTML；Tencent COS publisher 只允许
写 `dalton/index.html`，不得修改 bucket website configuration。上传后必须回读并核对 SHA-256，
同时确认受保护的站点根页和其他看板没有变化。publisher 只从 macOS Keychain 取 credential，
配置和 heartbeat 不保存 secret。

heartbeat 的 `starting / running / degraded / stopping` 是服务健康状态，不是研究任务状态。
健康检查必须同时确认 controller 状态、心跳新鲜度、writer socket、authority/projection 文件和
插件结果；旧 projection 仍存在时不能把 `degraded` controller 报成健康。

### Phase 1 Agenda Shadow

Phase 1 冻结 `Mandate`、`PriorityOverride`、`ResearchQuestion`、`AgendaCycle` 和
`AgendaDecision`。AgendaDecision 与 cycle event 只追加；agenda 运营记录不走 Thesis commit
gate，也不能直接改 Evidence、Claim、Thesis 或 Model。

controller 每日最多启动一个试点公司 cycle。legacy adapter 只读旧 Coverage SQLite 的一致快照，
输出小型 `PerceptionSnapshot`；Agenda 只认该契约，不引用旧库字段。LLM 只能提出 3—6 个问题、
回答标准、可追溯 source refs、展示理由，以及四个 0—3 整数 feature。feature schema 闭合，越界、
缺字段、未知 source 或非严格 JSON 都 fail closed。权重由版本化 agenda policy 持有；最终 score、
配额、选择和 question id 字典序 tie-break 都由 Core 确定性执行。

每次模型调用必须真实经过 Scheduler lease、ModelRouter decision 和受限 broker，并登记 invocation、
UsageEntry 与 CostEntry。route、adapter 或 output contract 失败时，coordinator 必须先把已租用
WorkOrder 终结为 failed，再把 AgendaCycle 终结为 failed，不能留下可被其他 worker 重领的僵尸任务。
全局 agenda pause 在任何 broker 调用前生效。

AgendaDecision 会原子创建 owner-only durable outbox message。OpenClaw/Discord bridge 必须先原子
claim，并把 attempt、lease expiry 和 endpoint 写进 append-only event；只有外部 API 返回 message id，
或用确定性 marker 在目标频道找回已发消息后，才能写 delivered receipt。claimed lease 到期可回收，
failed event 带 retry time，旧 attempt 不能覆盖新 claim。发送成功与 receipt commit 之间崩溃时，下一轮
必须先搜索 marker，再决定是否补投，因此不能把“CLI 已退出”直接当作未发送。

Discord 只投递通知，不承载审批。agree/disagree 由只监听 loopback 的 HTML control service 接收；
Tailscale Serve 负责 HTTPS、tailnet ACL 和不可伪造的 `Tailscale-User-Login`，backend 再校验登录 allowlist、
SameSite session 与 CSRF。control service 使用独立的 feedback-only principal，不复用 Core 或 human
governance token，且数据库只保存登录名的 SHA-256 派生 subject，不保存原登录名。

已送达 decision 在 24 小时内没有任何显式 human feedback 时，由第二个 automation-only principal 写入
`source=auto_accept_timeout` 的 agree。这个结果可作为有效默认接受，但 projection 必须与 human agree
分列；它不计入 `labeled_decisions` 或人工认可率。后到的显式 human feedback 覆盖有效展示，但不能删除
历史 timeout event。policy、mandate、pause 等人类治理操作仍使用一次性 token CLI：签发、执行一个 RPC、
立即移除，token 配置不得保留 live human principal。

### 冻结词表

`Verdict` 只有五个值：`pass`、`conditional_pass`、`revise`、`blocked`、`reject`。
`ValueKind` 只有五个值：`observed`、`assumption`、`derived_deterministic`、
`estimate`、`simulation`。value kind 不在实体 schema 中重复建模，但 Python
类型将其作为 Model IR 后续契约的冻结词表。

### Independence predicate

predicate 是闭合 shape：`{left_path, operator, right_path|value}`，只支持 `eq`、
`ne`、`in`、`not_in`。路径必须来自白名单：

`producer.*` 和 `verifier.*` 下的 `provider`、`model`、`model_family`、
`profile_ref`、`capability`、`runtime_ref`、`actor_ref`、`granularity`、
`environment_hash`。

`eq`/`ne` 可以用 `right_path` 比较两侧属性，也可以用 `value` 比较策略常量；
`in`/`not_in` 的 value 必须是非空数组。名单只限制可访问的字段路径，不硬编码
任何具体 policy 值；具体值由版本化 governance policy 提供。

默认 0.1 policy 只允许 `pass`，并要求 producer 与 verifier 的 `model_family`
不同；同一 invocation 无论 policy 如何配置都不能自证。

### Context、Memory 与 Log 边界

Dalton 不把聊天 transcript、compaction summary 或 runtime scratch 当作研究事实。durable research
memory 是 Research Ledger 的 immutable Evidence/Claim/Thesis 版本链；execution/event audit 由
ExecutionInvocation、DomainEvent、Scheduler attempt event、ArtifactVersion 和 Usage/Cost 承担。

后续 `ContextPack` 是 derived、content-hashed model input projection，必须冻结 builder、selector、
tokenizer 和 truncation algorithm 的 ref/hash；`RunState/Checkpoint` 是 per-attempt derived scratch，
只能由 attempt event 引用；`ClaimIndex` 是可重建的 Ledger 只读投影；`OpsTelemetry` 放 authority DB
之外。四者的保留期都由版本化 retention policy 决定，不能把临时天数写成架构常量。

## 占位契约（本 slice 不冻结）

以下对象只在架构文档中定义方向，暂不伪装成已实现契约：Model IR
datapoint/computation/scenario、CommitRecord、完整 bridge command-event、Excel exporter、
Tier 1/2/3 computation contract、
实际 capability sandbox backend/monitoring、checkpoint，以及尚未选定的生产级存储/队列/runtime（Postgres、Temporal、
Pi、DeepSeek Harness 等）。本 walking skeleton 可使用 SQLite，但不把 SQLite
写成 Dalton 的长期架构不变式。

## E1 / E2 验证映射

整体 slice 的 E1 由 store/integration tests 与本目录的 E2 contract tests 共同
覆盖；以下只把确实尚未实现的语义标为排除项：

| 语义 | 验证层 | 本 slice 验证物 |
| --- | --- | --- |
| schema 可解析、required/properties、未知字段 | E2 | `tests/test_contracts.py` |
| WorkOrder/ResultEnvelope 核心字段与 round-trip | E2 | `tests/test_contracts.py` |
| DomainEvent envelope 与非负 aggregate version | E2 | `tests/test_contracts.py` |
| verdict/value_kind/granularity 词表 | E2 | `tests/test_contracts.py` |
| independence predicate shape、路径白名单、四种 operator | E2 | `tests/test_contracts.py` |
| immutable version chain、verification gate pass/reject、幂等事务回滚 | E1 | store/integration tests |
| deterministic executor 的版本、预算和副作用边界 | E1 | `tests/test_executor.py` |
| scheduler lease、续租、到期回收、并发 claim 与 completion 幂等 | E1 | `tests/test_scheduler.py` |
| Dalton-native process runtime 的 timeout/limits/envelope | E1 | `tests/test_process_runtime.py` |
| Evidence → Claim → Thesis、adjudication 与冲突 projection | E1 | `tests/test_ledger.py` |
| scoped writer RPC、身份/subject 绑定和错误脱敏 | E1 | `tests/test_writer_service.py` |
| proposal → eval → human promotion → rollback | E1 | `tests/test_capability_registry.py` |
| ledger commit 与 capability promotion 的跨进程闭环 | E1 | `tests/test_slice2_integration.py` |
| capability attestation 的 hash、fixture、权限与 authority 字段 | E2 | `tests/test_capability_attestation.py` |
| capability search/describe/prepare、epoch/hash 与权限 lease | E1/E2 | `tests/test_capability_catalog.py` |
| workflow/link、usage correction、rate/cost 与 artifact authority | E1/E2 | `tests/test_observability.py` |
| model endpoint/profile/policy、route/retry/switch 决策 | E1/E2 | `tests/test_model_router.py` |
| OpenClaw broker adapter 的 UDS/hash/route/usage/budget | E1/E2 | `tests/test_openclaw_model_adapter.py` |
| Agenda schema、append-only cycle/decision、确定性排序与 durable outbox | E1/E2 | `tests/test_agenda.py` |
| PerceptionSnapshot、backup/restore 与 ephemeral governance | E1/E2 | `tests/test_perception_backup.py`、`tests/test_governance_cli.py` |
| Scheduler → Router → broker adapter → usage/cost → AgendaDecision thin slice | E1 | `tests/test_agenda_coordinator.py` |
| outbox claim/lease、Discord reconciliation、receipt 与 reaction feedback | E1/E2 | `tests/test_openclaw_agenda_bridge.py` |
| connector profile/invocation、quota/settlement、provenance、incident 和 self-generated manifest | E1/E2 | `tests/test_connector.py` |
| connector runner closed frame、双 use-time lease gate、静态 resolver 与 authority-derived adapter request | E2 | `tests/test_connector_runner.py` |
| recorded transport journal、bounded raw spool、W0–W4 recovery 与全链幂等重放 | E1/E2 | `tests/test_runner_journal.py`、`tests/test_raw_spool.py`、`tests/test_connector_transport_executor.py` |
| authority → read-only dashboard projection 与敏感字段隔离 | E1 | `tests/test_dashboard_projector.py` |
| dashboard 固定 GET API、只读连接与页面资源 | E2 | `tests/test_dashboard.py` |
| 实际 hostile-code sandbox backend 与自动 monitoring | E1 | 本 slice 排除 |

本目录不会通过 schema 或 `from_dict` 冒充实际 sandbox backend；Scheduler 和 commit service 的
组件时序由 E1 测试验证。影子迁移只建立可审计迁移基线，不代表 live cutover 已完成。

## SQLite 信任边界

SQLite trigger 和 commit service 在本 slice 中负责防止普通代码误写、保证单事务
原子性；连接级 UDF 不是面对恶意同用户进程的安全边界。任何能直接打开 DB 文件的
进程都不属于可信执行面，因此 Pi、DeepSeek Harness、OpenClaw adapter 或自生成
工具在接入前必须满足两条：运行进程拿不到 DB 路径；权威写入只能调用独立 writer
service。若部署形态无法隔离文件权限，就要改用支持独立写入身份和授权的存储，而
不是把 SQLite trigger 当作 sandbox。

writer 进程把“外部 runtime 不拿 DB path、正式写入只走 service”落成可测试边界；
但 owner-only socket/token/DB 仍不能抵御同一 OS 用户下读取进程参数、token 文件或
直接打开 DB 的恶意进程。面对该 threat model，生产部署必须使用不同 OS identity、
container/VM 或有独立授权身份的存储服务。

本 slice 已实现 proposal/eval/promotion/rollback 的治理账本，但没有执行未知代码的
sandbox，也没有给外部副作用 capability 发放实际凭据。在 sandbox 和 permission
attestation 完成前，active registry 只能证明“版本获批”，不能解释为“代码已安全
执行”。

## 部署边界声明

迁移器只读 legacy workspace，不修改旧 Coverage DB、Excel 或旧 Dalton 代码。模型接入只使用
operator-owned OpenClaw allowlist 和 Dalton broker，不赋予 worker credential 或 Core 写权限。
任何旧任务都必须经过重新设计、shadow reconciliation、单项 cutover 和 rollback 验证；源码仓库
不记录某台机器上的 live cron 状态，也不把旧 cron 定义解释为新架构的待办清单。
