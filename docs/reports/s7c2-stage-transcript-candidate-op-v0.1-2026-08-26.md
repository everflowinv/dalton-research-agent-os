# S7c-2：writer human-governance op `stage_transcript_candidate` v0.1

日期：2026-08-26
状态：development candidate；未部署；未调真实 AlphaEngine；live Core 写入 0；未合并 main

## 结论

writer 现在能把 Core-held AlphaEngine 获取结果（S6b/S7c-1）变成 CandidateStaging 里的语义候选（S7b，ADR-0003 选 B）。
人类 principal 调一次 `stage_transcript_candidate`，writer 只读 Core、只写 Cockpit 共用的 candidate-staging 文件，
verification mode 固定为 `transcript_core_authority`；`transcript_candidate_status` 按 candidate ref 回读 staged / decided / commit 状态。
隔离测试证明 8/24 ACN fixture 经 writer 落进 staging 后，Cockpit 侧的 `HumanReviewAuthority` 在同一文件里能看到这条候选，
Ledger 的 Evidence / Claim 表仍为 0。

## 做了什么

### writer（`src/dalton_core/writer_server.py`）

- 新 op `stage_transcript_candidate`，参数与 S7b 入口 `stage_transcript_qualitative_candidate` 对齐：
  `correction_set_ref, citation_ref, subject_ref, metric_or_aspect, period, basis, normalized_statement, idempotency_key`；
  `actor_ref` 走 `OPERATION_ACTOR_FIELDS` 由 principal 注入，调用方传别的 actor 直接 `PermissionError`。
  放在 `HUMAN_GOVERNANCE_OPERATIONS`，非 `human:*` principal 即使持有该操作集也拒绝。没有 `verification_mode` 参数，
  传了就是 `protocol_error`；模式由 S7b 入口写死为 `transcript_core_authority`。
- 新 op `transcript_candidate_status`（参数 `candidate_claim_ref`），同样 human-only；接受 claim version id 或稳定
  `candidate_claim_ref`（后者解析到最高版本）。找不到返回 `not_found`。
- 新构造参数 `candidate_staging_path` 与 CLI `--candidate-staging`。writer 之前没有任何 candidate-staging 路径约定
  （只有 Cockpit 的 `research_review.candidate_staging_path`），所以这里不是复用而是新增，但语义就是「同一个文件」：
  `_open_store` 在该路径上打开 `CandidateStagingStore`（写）和 `HumanReviewAuthority`（读），`_close_store` 一起关。
  没配置时两个 op 都返回 `rejected`，不会 fallback 到别的路径。
- 有 `--transcript-spool-dir` 时把 spool 的 `read_object` 作为 `artifact_reader` 传给 resolver，让 verifier 复核 spool
  字节与 `artifact_content_hash` 一致；没有 spool 时按 S7b 既有行为跳过该 finding。
- 错误映射：`ResearchVerificationConflict` / `ResearchReviewConflict` → `conflict`；
  `ResearchVerificationError`（含 `TranscriptCoreAuthorityError`、`VerificationRejected`）/ `ResearchReviewError` → `rejected`。
  之前这些异常会落到 `internal_error`。

### review reader（`src/dalton_core/research_review.py`）

- `HumanReviewAuthority.candidate_status(candidate_claim_ref)`：只读，返回 claim / evidence / decision / `commit_state` 和
  汇总的 `review_state ∈ {staged, decided, queued, committed, failed}`。复用 `_candidate_pair` 的列漂移校验，不新建 review。

### policy 路径

没有改。S7b 已让 `authorize_policy_candidate` 与 `commit_policy_candidate` 在触碰 policy 状态前拒绝 qualitative；
本切片的测试用 writer 真实 staged 出来的候选再走一遍这两条路径，确认仍然拒绝。

## 验证

worktree 内以主仓库 `.venv`（Python 3.13.14）+ `PYTHONPATH=src` 运行，已确认导入的是 worktree 的 `writer_server.py`：

- `tests.test_transcript_candidate_writer_ops`：6/6
  - human principal 经 in-process writer stage ACN 语义候选：`write_status=fresh`、claim_kind=qualitative、value 与 numeric ref 全 null、
    actor 为 principal、`provenance_mode=transcript_core_authority`、source verification 无 fail；
    Cockpit 侧 `HumanReviewAuthority.list_candidates` 看到同一条；`transcript_candidate_status` 用 version id 和稳定 ref 都能读到 `staged`；
    Core 的 `evidence_versions / claim_versions / reviewed_candidate_commits` 仍为 0
  - idempotency 重放：同 key 返回 `duplicate`，staging 计数不变；同 key 改 statement 返回 `conflict`，计数不变
  - 拒绝：dashboard principal（操作集外）、持有操作集但 actor 为 `system:*` 的 principal、human principal 冒充别的 actor_ref
  - 拒绝：缺失 citation、citation 不属于指定 correction set、传 `verification_mode` 参数（`protocol_error`）；未知 candidate ref `not_found`
  - policy 路径对 writer staged 的候选仍拒绝（`authorize_policy_candidate`、`commit_policy_candidate`）
  - 未配置 `--candidate-staging` 的 writer 返回 `rejected`
- 回归：`test_writer_service` + `test_alphaengine_acquisition_launcher` + `test_alphaengine_core_acquisition`：36/36；
  `test_transcript_qualitative_candidate` + `test_research_verification` + `test_research_review` + `test_research_review_control` + `test_contracts`：51/51
- `python -m compileall -q src tests`、`git diff --check`：通过
- 全仓慢回归未跑，交 CI

## 明确没做

- 未部署 live、未调真实 AlphaEngine、未写 live Core、未合并 main、未 push。
- `deploy/macos/install.sh` / LaunchAgent 没加 `--candidate-staging`（本切片不改 deploy/）。部署前必须把它指向 Cockpit 配置里
  `research_review.candidate_staging_path` 同一个文件，否则 live writer 上这两个 op 只会返回 `rejected`。
- Cockpit 没有「stage 候选」按钮；触发方式仍是 `dalton-gov`（ephemeral human principal）或后续接线。
- 没有为 `dalton-gov` CLI 加对应子命令。
- 没有跨进程并发测试：writer 与 Cockpit review server 同时打开同一 staging 文件在测试里只验证了「writer 写、另一个连接读」，
  没有压双写。两边都用 autocommit + `BEGIN IMMEDIATE`，SQLite 默认 busy timeout 5 秒，理论上够，但没实测。

## 需要主 session 注意

- `stage_transcript_candidate` 的成功返回体是 S7b 入口的完整字典（staging / citation / material / source_verification / evidence / claim），
  单次响应几 KB，够 Cockpit 直接渲染；如果后续要瘦身，改 op 不改入口。
- `transcript_candidate_status` 用稳定 `candidate_claim_ref` 查询时取最高版本，revise 后会返回新版本；需要固定版本时传 version id。
