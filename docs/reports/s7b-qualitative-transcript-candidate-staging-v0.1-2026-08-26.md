# S7b 语义 transcript 候选进入 CandidateStaging v0.1（ADR-0003 选 B）

日期：2026-08-26  
状态：development candidate；未部署 live；未联网、未调用真实 AlphaEngine；live Core 正式 Evidence / Claim 写入 0；全仓慢回归交 CI

## 结论

ADR-0003 按 owner 授权由 Eve 裁决为 **B**：电话会逐字稿来源的候选以 `claim_kind = qualitative` 进入 CandidateStaging，
数值字段全为 null，只经 explicit human review 入库。隔离端到端测试证明 ACN Q3 FY2026 transcript 候选能在 Core-held
AlphaEngine authority 下走完 `stage → HumanReviewAuthority.decide(accept) → commit_reviewed_candidate`，写出 1 条
EvidenceVersion 和 1 条 qualitative ClaimVersion 0.2，重复提交返回 duplicate。

S6 那条「-3%」不会作为 transcript Claim 的 value 出现；正式 transcript Claim 是管理层口径的语义陈述，数字回 SEC lane（S7d）。

## 做了什么

### 合同（additive）

- `contracts/candidate-claim.schema.json`、`contracts/claim-version-v0.2.schema.json`：`claim_kind ∈ {quantitative, qualitative}`。
  用 `allOf/if/then` 分支：quantitative 的 value / unit / scale / numeric_* 约束一字不变；qualitative 时
  value / unit / scale / currency 与 numeric_spec_ref / numeric_spec_hash / numeric_verification_ref / numeric_verification_hash
  必须为 null。
- `contracts/authority-source-verification-material.schema.json`：`provenance_mode` 放开为
  `connector_authority | transcript_core_authority`（见下）。
- `candidate_staging_schema.sql` 不用改：数值列本来就没有 NOT NULL / CHECK；numeric spec 是独立表，qualitative 候选不写它。旧库兼容。

### validator 与 staging 分支

- `research_verification.validate_candidate_claim`、`research_review.validate_claim_version_v0_2`：按 claim_kind 分支；
  qualitative 带任何数值或 numeric ref 明确报错；未知 claim_kind 拒绝。
- `CandidateStagingStore.stage()`：qualitative 分支跳过 numeric spec / numeric verification（参数允许 None），
  只接受 `source_type == authenticated_transcript` 且带 exact `transcript-claim-citation-binding` 的 evidence；
  其他 source_type 的 qualitative 一律拒绝。`candidate_authority_bundle` 对 qualitative 不再要求 numeric spec。
- **新增 verification_mode `transcript_core_authority`**。S6b 的 Core-hosted AlphaEngine 获取走 CapabilityCatalog WorkOrder，
  没有 ResearchCheckpoint、编译后的 connector plan 和 coordinator receipt，现有 `connector_authority` 模式（回放 checkpoint、
  要求 source-specific numeric normalizer）验不了这些页。新模式只读 Core、只收 qualitative，把
  `TranscriptClaimCitationBinding (claim_eligible) → TranscriptCorrectionSetVersion → page-1 SourceEnvelope → ConnectorInvocation → raw ArtifactVersion`
  这条链在同一个 Core 上重新推导一遍。这比 task 原定「沿用 connector_authority」多了一个闭合模式，是实现时发现的必要偏差。

### review / commit / policy guard

- `DaltonStore._commit_authorized_candidate`：qualitative 走同样的 connector / artifact / citation 校验，写出的
  ClaimVersion 0.2 为 qualitative、数值 null；若 `active_policy_binding` 非空或 authorization 不是 `explicit_human_review`，
  或 evidence 不是 transcript 类型，`GateRejected`。
- `DaltonStore.commit_policy_candidate` 与 `research_auto_commit.authorize_policy_candidate`：qualitative 在触碰 policy 状态前直接拒绝。

### Cockpit

- `research_review.project_candidate` 投影新增 `claim_kind`；`cockpit_control.html` 候选卡在 qualitative 或 value 为 null 时显示
  「语义候选（无数值）」，不再渲染 `null`。

### ACN 候选构造器

新模块 `src/dalton_core/transcript_candidate_staging.py`：

- `TranscriptCoreAuthorityResolver`：只读 resolver，从 Core 读 citation、correction set、page-1 envelope、invocation、raw artifact；
  可选 `artifact_reader` 复核 spool 字节与 `artifact_content_hash` 一致。
- `build_transcript_qualitative_candidate(...)`：纯函数，生成 CandidateEvidence + qualitative CandidateClaim。
- `stage_transcript_qualitative_candidate(core, staging_store, *, correction_set_ref, citation_ref, subject_ref, metric_or_aspect, period, basis, normalized_statement, actor_ref, idempotency_key, ...)`：
  校验 citation 属于指定 correction set，构造 material、跑 source verifier，通过真正的 `CandidateStagingStore.stage(verification_mode="transcript_core_authority")` 落进 staging。
  这是 S7c writer human-governance op `stage_transcript_candidate` 直接调用的入口；不开网络，不写 Ledger。

## 验证

本地（worktree，`PYTHONPATH=src` 压过 .venv 的 editable 主仓库包，已确认导入的是 worktree 的 src）：

- `tests.test_transcript_qualitative_candidate`：12/12
  - 端到端：ACN 语义候选 stage → accept → commit 返回 `fresh`，`evidence_versions=1`、`claim_versions=1`、claim_kind=qualitative、value null；重复提交 duplicate
  - Cockpit 投影对语义候选不出数值
  - 敌对：qualitative 带 value / numeric_spec_ref；未知 claim_kind；非 transcript evidence；transcript 标签但无 citation binding；
    带 numeric inputs 或错误 mode；篡改 source verification；错误 correction set / 缺文档；policy 路径收到 qualitative；
    citation 与未决 ASR 标记重叠（`claim_eligible=false`）——全部拒绝
- `tests.test_research_review` + `tests.test_research_review_control` + `tests.test_contracts`：32/32
- `tests.test_research_verification` + `tests.test_alphaengine_core_acquisition`：16/16
- `tests.test_store`：17/17
- `python -m compileall -q src`：通过；`git diff --check`：通过
- `tests.test_research_plan_closure` / `tests.test_research_plan_executor` / `tests.test_thesis_impact_control`（引用了 auto-commit 路径）：
  见 PROJECT_STATUS 更新时的结果；未在本报告成稿前跑完的部分交 CI

## 明确没做

- 未部署 live；未调用真实 AlphaEngine；live `core.sqlite` 没有任何写入。
- writer 没有 `stage_transcript_candidate` op，Cockpit 待审页还看不到真实候选——这是 S7c。
- `test_packaging` 未重跑（本切片不动 pyproject / 打包），交 CI。
- 没有改 Ledger 0.1 的 `claim-version.schema.json`（它本来就允许 qualitative）。

## 发现的问题

- 子任务 S7b 在 OpenClaw 25 分钟 `subagents.runTimeoutSeconds` 上限处被杀，代码写完但测试与 commit 未做；由主 session 接手补跑、修一处测试断言并提交。
- `connector_authority` 模式对 Core-hosted 获取不适用（无 checkpoint / plan / receipt），S7c 若要让 writer 用同一入口，须沿用 `transcript_core_authority`。

## 下一步（S7c）

writer 暴露 human-governance op `stage_transcript_candidate`，调用本模块入口；owner 经真实 Tailscale 会话触发 ACN 获取
（digest 须仍为 `a8a9fbff…bd96bd`）、候选进 staging、Cockpit accept，live Core 写出第一条正式 Evidence / Claim；brief v3 由正式 Claim 生成。
