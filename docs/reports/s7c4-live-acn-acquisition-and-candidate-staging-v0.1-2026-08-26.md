# S7c-4：live 首次真实 AlphaEngine 获取 + ACN 语义候选进 Cockpit staging v0.1

日期：2026-08-26
状态：已在 live 执行；候选 `staged`，等 owner 在 Cockpit accept；live Core 正式 Evidence / Claim 仍为 0

## 结论

live writer 以 human principal 真实调用了一次 AlphaEngine（`acquire_alphaengine_document`，8/24 的 ACN Q3 FY2026 transcript
`alphaengine-doc:130000095976806`），2 页、2 次 physical call、1 个 document unit，装配后 digest 与 8/25 owner 在 Cockpit 确认的
correction set / citation 所绑定的 `a8a9fbff…bd96bd` 逐字节一致，`transcript_authority_probe.ok = true`。随后 `stage_transcript_candidate`
把该 citation 变成一条 qualitative 候选写进 Cockpit 共用的 candidate-staging，30 项 source verification 全部 `pass`（含 raw page 字节复核），
`review_state = staged`。剩下一步是 owner 在 Cockpit accept，Core 才会写出第一条正式 qualitative Evidence / Claim。

执行前发现并修了一个会让这一步失败的路径问题（见下），修复 `0efe8f5` 已部署 live。

## 执行前修的问题：获取子进程和 writer 用的不是同一个 spool

- `alphaengine_acquisition_cli.run_acquisition` 把 raw page 对象写到 `<state>/connector-spool`（`RawSpool` 再在下面套一层 `connector-spool/objects`），
  而 writer 的 `stage_transcript_candidate` 通过 `--transcript-spool-dir`（`<state>/transcript-spool`）读 raw artifact 字节做 `raw_artifact_bytes` 复核。
  live 上两者永远碰不到，第一条真实候选会在 `transcript_core_resolution` 上 fail closed。隔离测试没暴露它，是因为 S7c-2 的 harness 让 writer 和 Core
  共用同一个 spool 对象；S7c-1 的 writer 启动测试则根本没传 `--transcript-spool-dir`。
- 修法（`0efe8f5`）：CLI 加 `--spool-dir`（默认仍是 `<state>/connector-spool`，隔离演练布局不变）；launcher 新增 `spool_dir` 并转发；
  writer 把自己的 `--transcript-spool-dir` 传给 launcher。`tests.test_alphaengine_acquisition_launcher` 的 writer 启动测试现在带
  `--transcript-spool-dir`，并断言子进程登记的每个 raw page ArtifactVersion 都能从 writer 的 spool 按 hash 读回、`<state>/connector-spool` 不存在。
- 回归：`test_alphaengine_acquisition_launcher` 6/6；`test_alphaengine_core_acquisition` + `test_transcript_candidate_writer_ops` +
  `test_writer_service` + `test_service` 46/46；`compileall`、`git diff --check` 通过。
- 部署：`deploy/macos/install.sh`（13:36 EDT），venv 装 `0efe8f5`，四个 LaunchAgent 重启；`installed` 的 launcher / cli / writer_server
  都含 `--spool-dir` 接线。health 仍 `degraded`，原因与 S7c-3 相同（Agenda snapshot_id 冲突，到 8/27 00:00 UTC 前预期内）。

## live 执行记录（全部 `dalton-gov --actor human:lumos`）

### 1. 获取

- ticket `alphaengine-acquisition:75a314bc4dac29482e5dbccb`，13:38:12 → 13:38:44 UTC-4，transport `loopback-mcp`（`127.0.0.1:8950/mcp`，
  AlphaEngine Desktop 已登录，token 到 2026-09-25），治理记录 `connector-governance:alphaengine-get-document:v1` hash `2f6ad555…997c49`。
- summary：`manifest_status=complete`，manifest `alphaengine-document-acquisition:9c1d4e72…45f33e`，`content_chars=51034`，
  `assembled_content_sha256=a8a9fbff…bd96bd`，`expected_digest_match=true`，`page_count=2`，`physical_calls=2`，`provider_calls=2`，
  `document_quota_units=1`，`replayed_pages=0`。
- probe 六项全 true；page-1 `source-envelope:067dc1d4…395cb1`，raw artifact `artifact-version:41255e07…4fc85`。
- live Core 计数：`connector_source_envelopes=2`、`connector_invocations=2`、`connector_physical_attempts=2`、`observability_artifact_versions_v2=2`，
  `evidence_versions=0`、`claim_versions=0`。
- raw page 对象落在 `transcript-spool/connector-spool/objects/{08,1a}/…`（88,257 / 70,874 字节），与 8/24 装配对象 `a8/a8a9fbff…` 同一目录；
  `<state>/connector-spool` 没有被创建。

### 2. stage

- 参数：correction set `transcript-correction-set-version:011c474ed952274b1fc31405008acafb`（`transcript-correction-set:acn:q3fy26:1`），
  citation `transcript-claim-citation-binding:fe6351c153c4f6350b71d4a79ed27aa5`（原文 [11823, 11962)：
  「New bookings were $19.3 billion for the quarter, a 2% decrease in US dollars and 3% in local currency, with an overall book-to-bill of 1.0.」），
  subject `company:sec-cik:0001467373`，aspect `aspect:new-bookings-direction-local-currency`，period FY2026Q3（2026-03-01 至 2026-05-31），
  basis `management-reported`，statement 与 S7b 测试一致（只断言本币口径同比下降，不断言数值），idempotency `stage:acn:q3fy26:qualitative:live:1`。
- 结果：`write_status=fresh`，candidate claim `candidate-claim-version:3fafc07d…e9a87d`，candidate evidence `candidate-evidence-version:e95fc8c3…d0b1a5`，
  `claim_kind=qualitative`、`value=null`、actor `human:lumos`，material `source-material:transcript-core:4672c5a7…6d04` 绑定上面的 page-1 envelope，
  source verification 30 项 `pass`、0 `fail`、0 `skip`（`raw_artifact_bytes` 为 `pass`——这就是上面那个修复的作用点）。
- `transcript_candidate_status`：`review_state=staged`，`commit_state=null`，`decision=null`。staging 文件 `candidate_claim_versions=1`、
  `candidate_stage_requests=1`。live Core `evidence_versions / claim_versions / reviewed_candidate_commits` 仍为 0。
- control（Cockpit）进程 13:37:08 起的实例运行正常；`control.stderr.log` 里的 `candidate staging schema is unavailable` 是 8/25 11:41 以前
  staging 文件还不存在时的旧记录，不是本次的问题。

## 明确没做

- 没有代替 owner accept。Cockpit accept 之后由既有 `_commit_authorized_candidate` 路径写正式 Evidence / Claim，本切片不碰。
- 没有重取文档第二次（会再计 1 个 document unit，S7c-1 已说明 journal 不跨进程重放）。
- 没有改 Agenda policy / snapshot_id 的两条待改点，也没动 `deploy/` 配置文件。
- `0efe8f5` 的 CI 与 `51d5820` / `9e78e2a` / `dc747de` 一样在 push 后才会起，部署依据是本地定向回归；全仓慢回归没在本地跑。
- brief v3（由正式 Claim 生成）等 accept 后再做。

## 下一步

1. owner 在 Cockpit（`https://everflowdemac-mini.taild2c767.ts.net:8793/`）对 `candidate-claim-version:3fafc07d…e9a87d` accept；
   之后用 `transcript_candidate_status` 确认 `review_state=committed`，live Core `evidence_versions=1`、`claim_versions=1`、claim_kind qualitative。
2. 8/27 00:xx UTC 的万华 cycle 是 S7a + policy v4 的首次真实验证，回看 heartbeat。
3. 再排 S7c-3 报告里的两条设计点（policy pointer 回退 prior chain；snapshot_id 带内容 hash）。
