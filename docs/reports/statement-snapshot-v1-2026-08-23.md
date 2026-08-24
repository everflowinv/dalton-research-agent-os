# StatementSnapshot v1

日期：2026-08-23
状态：development candidate；未部署 live connector / worker

## 完成范围

StatementSnapshot v1 已实现一条最小、可重放的资产负债表本地处理链：

`exact SEC Company Facts ArtifactVersion → human-admitted concept set → flat Decimal fact rows → balance-sheet reconciliation → bounded-loop observed Outcome`

worker 不发 HTTP 请求，也不重新抓 filing。它只接收已有 ArtifactVersion 的原始 bytes，并同时重验：

- ArtifactVersion ref、record hash 与 raw-source kind；
- raw content SHA-256 与 size；
- public/internal access class 与 JSON media type（restricted 拒绝）；
- exact CIK、accession、form 和 period end；
- exact concept-set ref/hash。

因此，同一 accession 的完整 raw authority 只需由 connector 取得一次；后续研究问题可以从同一
StatementSnapshot 本地抽取指定 line item，不再重复请求 SEC。

## Authority 与数值规则

`StatementConceptSetVersion 0.1` 是 human-only、append-only authority。v1 只支持
`us-gaap / USD / balance_sheet`，每个 canonical line item 绑定一个有顺序的 exact XBRL concept allowlist。
系统按人批准的顺序选择第一个在 exact accession 上可用的 concept；label 只保留为 provenance，不参与映射。
即使 issuer-specific tag 的 label 也是 “Assets”，没有进入 concept set 就不能成为正式 fact。

`StatementSnapshotVersion 0.1` 保存扁平 fact rows：canonical line item、taxonomy、concept、label、unit、
canonical decimal value、period、accession、form、filed、FY 与 FP。v1 拒绝 Python float，只接受整数或 canonical
decimal string，避免二进制浮点进入正式数值。

concept set 同时冻结 equation 与 tolerance。首个 vertical slice 使用：

`assets = liabilities + stockholders_equity`

计算全部使用 `Decimal`。exact accession 上出现同 concept 多个不同事实、ArtifactVersion/hash/size 漂移、
缺少任一 required line item，或勾稽差额超过 tolerance，都会在 snapshot 写入前 fail closed。

## Planner Loop 接线

新增的本地能力边界为：

- capability：`capability:dalton:local:statement-snapshot`
- operation：`materialize_statement_snapshot`
- runtime：`runtime-profile:dalton-core-local-decimal:0.1`
- permission：`read_exact_sec_company_facts_artifact`
- output：`schema:statement-snapshot-probe-output:0.1`
- verifier：`verifier:statement-snapshot-decimal-tie-out:0.1`

测试用一份 human-admitted ProbeTemplate 把该能力放进现有 Bounded Planner Loop。Core 仍生成原有 WorkOrder，
Scheduler 仍是唯一队列；worker 生成一个 verified snapshot match，ResultEnvelope 完成后由原有
CoverageManifest / ResearchOutcome 路径记为 `observed`。本轮没有增加第二套 queue 或 DAG。

Snapshot 只是经过勾稽的派生事实 authority，不自动成为 Evidence、Claim 或 Model Input actual。后续消费者仍需
走各自 admission gate。模型若认为某个模糊 tag 应映射到 canonical line item，只能提出候选；正式使用必须由人
发布新 concept-set version。

## 验证与边界

专项 4/4 覆盖：

- concept set 的 human-only、版本链、重复与 SQL immutability；
- raw ArtifactVersion 三重 hash/size binding、扁平 fact rows、Decimal 勾稽和本地重复查询；
- exact fact ambiguity、资产负债表不平、fuzzy-label 偷渡全部 fail closed；
- ProbeTemplate → PlannerProposal → Core admission → 原 Scheduler WorkOrder → snapshot worker →
  ResultEnvelope → observed Outcome 的完整隔离链。

本轮没有做：真实 SEC connector canary、10-K/10-Q 之外的 form、非 USD、利润表/现金流量表、跨 statement tie-out、
任意 XBRL ontology、AI 自动 mapping、正式 Evidence/Claim/Model Input 写入或 live deployment。生产接线时必须把
`artifact_resolver` 绑定到重验后的 `ObservabilityStore.get_artifact_version` 或等价 exact authority resolver，
不能用调用者自报的 artifact metadata。

下一笔按既定顺序进入 AlphaEngine TranscriptPolishWorker：原稿为唯一 authority，polished artifact 必须保留
span mapping，并通过数字与专名守恒检查。
