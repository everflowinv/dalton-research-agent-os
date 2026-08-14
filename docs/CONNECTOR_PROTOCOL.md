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
  "connector_invocation_ref": "connector-invocation:...",
  "profile_ref": "connector-profile:...",
  "profile_hash": "<sha256>",
  "call_spec_ref": "connector-call:...",
  "call_spec_hash": "<sha256>",
  "operation": "list_announcements",
  "parameters": {},
  "allowed_hosts": ["www.cninfo.com.cn"],
  "credential_handle": null,
  "deadline": "2026-08-14T12:00:30Z",
  "max_response_bytes": 1000000
}
```

Adapter 只能返回 transport observation，不得伪造 authority id：

```json
{
  "protocol_version": "0.1",
  "request_hash": "<sha256>",
  "outcome": "succeeded",
  "provider_request_id": "...",
  "started_at": "...",
  "completed_at": "...",
  "retry_at": null,
  "response_bytes": 1234,
  "record_count": 12,
  "cursor": null,
  "raw_response_ref": "runner-private-spool:...",
  "raw_response_hash": "<sha256>",
  "source_record_refs": ["..."],
  "error": null
}
```

Runner 负责 use-time lease/profile/schema/hash 复核、quota reservation、网络策略、credential handle、
frame/timeout 上限、raw artifact 注册、Usage/Cost/Settlement 和 ResultEnvelope。Adapter 不收到 reservation
写权限、Core token、真实 credential value 或数据库路径。

`ConnectorCallSpec.parameters` 不保存 token、cookie、password、API key 或其他 credential-shaped 字段；
凭据只能通过 profile 声明的 slot 交给 Runner。authority 会核对 query hash 和拒绝这些敏感字段，可信
Runner 仍必须用 profile 冻结的 input schema ref/hash 验证完整参数结构。

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
- timeout 或调用结果不确定写 `indeterminate`，保守占额；
- provider-reported overage 写 blocking incident，后续 reservation fail closed；
- local quota 只能保证不主动超发，不能承诺供应商计量永不漂移。

## 进入研究账本

Connector output 永远先进入 raw ArtifactVersion v0.2 和 SourceEnvelope。SourceEnvelope 必须绑定同一
execution 生产的 artifact version，并核对 raw hash、source、operation、schema、policy、provider request
和明确的 `result_physical_attempt_ref`；complete/empty 的 result attempt 必须 succeeded，error 的 result
attempt 不得 succeeded。`source_content_hash` 对 source、operation、record refs、四类时间、cursor、
provider request、completeness、status 和 error 的规范化投影计算。这个 hash 绑定 metadata 和 record
refs，不声称绑定 record body。只有 source/numeric verifier 生成的
candidate Evidence/Claim 才能进入人工审阅；正式 Evidence/Claim/Thesis 仍走现有 Ledger gate。聊天
transcript、adapter 日志、compaction summary 和 runtime scratch 都不是研究事实。
