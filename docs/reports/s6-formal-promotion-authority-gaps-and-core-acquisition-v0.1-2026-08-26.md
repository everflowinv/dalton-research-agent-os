# S6 正式晋级前置缺口与 Core-hosted AlphaEngine 获取 v0.1

日期：2026-08-26  
状态：development candidate；未部署 live；未调用真实 AlphaEngine；正式 Evidence / Claim / Thesis 写入 0

## 结论

S6「ACN 首条正式研究记忆闭环」在 2026-08-25 走到 `awaiting_candidate_staging` 后停住，不是因为缺一个按钮，
而是两道结构性的门没有开：

1. **live Core 没有 connector authority。** `_commit_authorized_candidate` 在写正式 Evidence / Claim 前要从
   `connector_source_envelopes`、`connector_invocations` 和 `observability_artifact_versions_v2` 重新核对
   SourceEnvelope、raw ArtifactVersion 和 producer execution。live `core.sqlite` 从未打开过 connector schema，
   这三张表里两张根本不存在，`observability_artifact_versions_v2` 为 0 行。8/24 的 ACN 获取只存在于隔离 canary
   的 detached receipt reader 里，从未进入 Core。因此即便候选进了 staging、用户在 Cockpit 点了 accept，
   promotion 也会在 Core 这一步失败。
2. **CandidateStaging 0.1 只收数值型候选。** `validate_candidate_claim` 固定 `claim_kind == "quantitative"`，
   `CandidateStagingStore.stage()` 要求 numeric verification 能从同一份 material 的 `normalized_payload` 用
   JSON pointer 复算出来；AlphaEngine `get_document` 的 structured output 只有
   `source_record_refs / next_cursor / provider_status`，抽不出 transcript 里的「-3%」。S6 计划里的
   `required_secondary_numeric_authority`（SEC exhibit）也不在 live Core。这一道门需要合同层面的决定，见
   ADR-0003（Proposed）。

本切片关闭第一道门的开发候选，并把第二道门写成待 owner 裁决的 ADR。

## 本切片做了什么

### Core gate 明确 fail closed

`DaltonStore._commit_authorized_candidate` 现在先检查四张 connector / artifact authority 表是否存在；缺表直接
`GateRejected("Core connector authority is unavailable; candidate SourceEnvelope cannot be verified")`，不再冒出
`sqlite3.OperationalError`。回归：无 connector schema 的 Core 对合法形状的 accepted candidate 拒绝晋级，
`evidence_versions / claim_versions / reviewed_candidate_commits` 保持 0。

### writer 打开 connector authority schema

`WriterServer._open_store` 现在在同一 Core 上构造 `ConnectorStore`，schema 为 additive `CREATE TABLE IF NOT
EXISTS`，不动任何既有行。部署后 live Core 会拥有 connector 表，但没有任何 connector 记录；promotion 仍会在
「SourceEnvelope is not exact Core authority」处拒绝，直到真实获取进入 Core。writer 服务测试 21/21 通过。

### `alphaengine_core_acquisition`：把 AlphaEngine 获取写进 Core

新模块只做组合，不加合同：

- `StaticConnectorGovernance`：一份闭合、hash 绑定的 owner 审批记录，同时充当 `CapabilityCatalog` 的
  approval resolver 和 policy resolver。`status` 只有 `proposed / approved` 两种；`proposed` 记录可以读，但任何
  approval / policy 查询都直接抛错。approval 只对 exact `expected_source_hash / expected_schema_hash` 生效，
  两者从 packaged inventory 里的 AlphaEngine `get_document` 合同派生，owner 审的就是 catalog 之后会问的那份 schema。
- `AlphaEngineCoreAcquisition.ensure_governed_authorities()`：幂等发布 capability descriptor、
  `connector-profile:alphaengine-get-document:v1`、零价 price rate 和按 `governed_daily_quota("alphaengine",
  "get_document")` 派生的 rate policy（每日 80 份文档、Asia/Shanghai 00:00 重置）。
- `_CorePagePort`：每页一条 WorkOrder，经 `CapabilityCatalog.prepare` 取 lease、`ConnectorStore` 登记 call spec /
  invocation、Scheduler claim、`CredentialAuthorityStore` 发一次性 grant（`max_calls=1`），再由既有
  `LiveMcpRunnerAdmissionGate + ConnectorTransportExecutor` 执行。Core 里落下的是 executor 原生写出的
  physical attempt、usage、cost、settlement、`ArtifactVersion` v0.2 和 `SourceEnvelope`。
- 文档由既有 `AlphaEngineDocumentAcquisitionCoordinator` 拼接，authority reader 是
  `ConnectorCompletionReceiptReader`，也就是从 Core 回读 receipt，而不是 canary 里的内存 reader。
- 重放：同一 plan 再跑一次时，每页先查 `RunnerJournal`；已 `responded` 的页直接返回 duplicate response，
  不再发 lease、grant 或 provider call。
- `core_transcript_authority_probe()`：只读投影，检查 page-1 SourceEnvelope 是否满足 transcript gate 的
  `alphaengine_document_binding`：`source_record_refs == [doc:sha256:<assembled digest>]`、
  `raw_response_hash == artifact.artifact_content_hash`、artifact 绑定 invocation 的 execution_ref。

### 验证

`tests/test_alphaengine_core_acquisition.py` 9/9：

- 两页文档进入 Core：`connector_source_envelopes = 2`、`connector_invocations = 2`、
  `observability_artifact_versions_v2 = 2`，manifest `complete`、`document_quota_units = 1`，page-1 envelope 的
  source record 绑定整份文档 digest；正式表仍为 0。
- **Core-held authority 可以晋级 transcript 候选**：对同一 Core 发布 correction set、绑定 citation
  （`claim_eligible = true`），把候选 Evidence 的第一项 artifact 指向 Core 里的 page-1 raw artifact，
  `commit_reviewed_candidate` 返回 `fresh`，`evidence_versions = 1`、`claim_versions = 1`。这条测试证明第一道门
  在 Core-held authority 下能过；数值 spec 仍然是占位 hash，第二道门不在本测试范围内。
- 第二次 `acquire()` 全部走 journal 重放：provider call 0、`replayed_pages = 2`、manifest 逐字节相同。
- 篡改 governance 的 `expected_schema_hash` 后 catalog 拒绝发布，provider call 0。
- `proposed` governance 对 approval / policy 查询直接抛错；hash 篡改、非 human `approved_by` 被拒；
  写出的 proposal 文件权限 0600。
- 无 connector schema 的 Core 拒绝晋级；writer 启动后 connector 表存在。

关联回归 121/121（research review / review control / live MCP / document acquisition / transcript polish /
transport executor / contracts / store / connector），writer 服务 21/21，`compileall` 与 `git diff --check` 通过。
全仓慢回归和 sdist/wheel 本轮未重跑，交同提交 CI。

### 隔离演练（无网络）

`scripts/run_isolated_alphaengine_core_acquisition_canary.py --fake-document-file` 用 8/24 真实取回的 ACN Q3 FY2026
原文（51,034 字，spool object `a8a9fbff…bd96bd`）按 30,000 字/页喂给同一条 Core 路径：2 页、2 次 physical
call、1 个 document unit、assembled digest 与 2026-08-25 用户在 Cockpit 确认的 correction set / citation binding
所绑定的 `a8a9fbff…bd96bd` **逐字节一致**，`transcript_authority_probe.ok = true`。机器可读证据：
`docs/review-evidence/alphaengine-core-acquisition-rehearsal-summary-2026-08-26.json`。这是演练不是获取：
transport 为 `fake`，没有任何 AlphaEngine 调用。

同一 CLI 用仓库里 `proposed` 的治理记录加 `--allow-network` 运行，在任何网络或 catalog 写入之前以
`owner approval is required` 退出，退出码 1。

## 明确没做

- 没有重新从 AlphaEngine 取 ACN 文档。真实获取要用 `--allow-network` 加已批准的治理记录；仓库里的
  `deploy/connector-governance/alphaengine-get-document-v1.json` 是 `proposed`，`approved_by` 写的是预期审批人
  `human:lumos`，需要 owner 把 `status` 改成 `approved` 并重算 `content_hash` 才会生效。CLI 明确拒绝
  「内存里伪造 approved 记录 + 网络」这种组合。
- 没有部署 live。live writer 打开 connector schema 需要重新安装 wheel 并重启 `space.lumos.dalton.writer`。
- 没有把获取接进 writer RPC。`ConnectorTransportExecutor` 的 watchdog 用 `SIGALRM`，只能跑在主线程；writer 的
  store executor 是单线程且 `STORE_REQUEST_TIMEOUT = 30s`。把获取放进 writer 进程需要独立的 acquisition 线程和
  第二组 Core 连接，这是 S6c 的内容。
- 没有为 transcript 候选造一条能过 `stage()` 的数值 spec。第二道门见 ADR-0003。
- 没有改 Cockpit、trajectory 投影、production pointer。

## 下一步（按依赖顺序）

1. Owner 裁决 ADR-0003（transcript 候选如何进 staging）。
2. Owner 审阅并批准 `deploy/connector-governance/alphaengine-get-document-v1.json`。
3. S6c：writer 内的 acquisition 线程 + `acquire_alphaengine_document` human-governance op；部署 writer；
   用真实 Tailscale 会话触发一次 ACN 获取，确认 digest 仍为 `a8a9fbff…bd96bd`（否则 8/25 的 correction set /
   citation 需要重新审）。
4. 按 ADR-0003 的结论把 ACN 候选写进 staging，用户在 Cockpit accept，Core 写正式 Evidence / Claim，再做 brief v3。

## 顺带发现（与本切片无关，未处理）

live Agenda Shadow 的万华 cycle 在 2026-08-25 与 08-26 00:5x UTC 连续两次 `failed`，原因都是
`PROVIDER_BUDGET_EXCEEDED: provider max_input_tokens telemetry exceeds WorkOrder budget`（openclaw-model-adapter）。
08-24 那次仍是 `delivered`。这是 Agenda 侧的预算口径问题，不影响本切片，另立处理。
