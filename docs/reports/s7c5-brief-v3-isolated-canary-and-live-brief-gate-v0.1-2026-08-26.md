# S7c-5：live 第一条正式 qualitative Claim 已落库 + brief v3 隔离 canary + live brief 的合同门 v0.1

日期：2026-08-26
状态：live Core 正式 Evidence / Claim 各 1 条（owner 在 Cockpit accept）；brief v3 已在隔离 Core 确定性生成并 replay 一致；
live 上生成 brief v3 被两条既有合同挡住，需要 owner 裁决走哪条路

## 结论

1. owner 于 2026-08-26 17:42:45 UTC 在 Cockpit accept 了 S7c-4 stage 的 ACN 候选。Cockpit 的 review control 在 36 毫秒内
   把它经 `commit_reviewed_candidate` 写进 live Core：`evidence_versions = 1`、`claim_versions = 1`、`evidence_relations = 1`、
   `reviewed_candidate_commits = 1`。这是 Dalton live Core 自 2026-08-13 以来第一条正式 Evidence / Claim。
2. brief v3 按 ADR-0003 的 packet verdict「`null_numeric; add transcript corroboration only`」在隔离 Core 生成：v2 的
   21 条 SEC Claim 原样保留，新增这一条 transcript qualitative Claim 作为 ACN 本币口径新签订单方向的佐证。
   snapshot hash 与 markdown 的 snapshot hash 一致，同一 exact versions 二次渲染逐字节相同。
3. **live 上现在做不出 brief v3**。`IndustryResearchAuthority` 的两条既有合同要求 evidence pack 的每个 driver 至少绑定一条
   正式 Claim（`evidence pack must cover every industry driver`），且每家覆盖公司的 overlay 在每个 driver 上至少引用一条
   自己的 Claim（`driver_views[].claim_version_refs` nonempty）。live Core 只有 ACN 这一条 Claim，v2 的 21 条 SEC Claim
   只存在于隔离 canary。要 owner 在两条路里选（见「下一步」）。

## live 核对（accept 之后）

- `transcript_candidate_status`（`dalton-gov --actor human:lumos`，参数名是 `candidate_claim_ref`，接受稳定 ref
  `candidate-claim:transcript:08ae000b3fe3a8e4f7d8b5a0f2b0b77a`）：`review_state = committed`、`commit_state = committed`、
  `decision.verdict = accept`、`reviewer_ref = human:tailscale-9be9a3b6…`、`rationale = "Sounds good"`。
- staging 文件：`human_review_decisions` 1 条（`human-review:f721c8be…1b2577`，`authorization = explicit_human_review`，
  `source = tailscale_review`），`human_review_commit_events` 2 条（`queued` 17:42:45.423 → `committed` 17:42:45.459）。
- live Core：
  - `claim-version:e93760a1…df7a9`（`claim:transcript:08ae000b…`，schema 0.2，`claim_kind = qualitative`，value / unit / scale /
    currency 全 null，`candidate_origin_ref` 指回 `candidate-claim-version:3fafc07d…e9a87d`，`semantic_review_ref` 指回上面的
    decision，`producer_execution_refs = [connector-invocation:alphaengine-doc:3f477b57d89a9cf7074b]`）；
  - `evidence-version:954b29af…c58fb1`（`evidence:transcript:d3718396…`，`source_type = authenticated_transcript`，
    `artifact_refs` 绑定 raw page `artifact-version:41255e07…4fc85` 和 citation `transcript-claim-citation-binding:fe6351c1…`，
    `source_envelope_ref = source-envelope:067dc1d4…395cb1`，lineage 五段完整）；
  - `relation:reviewed:8e125ff8…d63ba`（`supports`）；`reviewed_candidate_commits` 幂等键
    `reviewed-ledger:human-review:f721c8be…`，`claim_status = proposed`。
- 没有第二次获取、没有重复 stage；health 仍 `degraded`，原因不变（S7c-3 的 snapshot_id 冲突，8/27 00:00 UTC 前预期内）。

## brief v3 隔离 canary

### 新增

- `deploy/coverage/us-it-services-industry-evidence-v3.json`：由 v2 派生。`driver_pack_v4` 给 `driver:bookings-mix-and-conversion`
  加一个 semantic aspect `aspect:new-bookings-direction-local-currency`（unit `qualitative`，来源 transcript / AlphaEngine，
  caveat 写明 transcript 只佐证、不替代 filing 的增速）；`transcript_candidate` 记录 ACN 候选的 citation 文本锚点、
  ASR flag 文本、subject / aspect / period / basis / statement 和三段幂等键；`evidence_pack` v3 = v2 的 21 个 binding + 1 个
  transcript binding，debate `ai-demand-versus-bookings-conversion` 升到 v3 并把 transcript Claim 加进「near-term bookings can
  still contract」这个 qualifies 立场；overlays ACN v3 / CTSH v2 / EPAM v2 / IBM v2，ACN 在新 aspect 上 `observed`，三家同行
  `not_found_in_reviewed_sources`（理由：Core 里没有该公司的 authenticated transcript）。fixture 不含 AlphaEngine 原文。
- `scripts/run_isolated_us_it_services_brief_v3_canary.py`：单个临时 Core 上依次跑
  fake-handle Core-hosted 获取（`run_acquisition`，无网络）→ `TranscriptCorrectionAuthority.publish` + `bind_claim_citation`
  → `stage_transcript_qualitative_candidate` → `HumanReviewAuthority.decide(accept)` → `commit_reviewed_candidate`
  → v2 canary 的 SEC 数据集与 pack v1 / v2 → driver pack v4 → pack v3 → 四份 overlay → snapshot → markdown → replay。
  文档正文由 `--document-file` 提供；`--output-dir` 写 `brief-v3.md` 和 `summary.json`（owner-only 权限）。
- `tests/test_industry_evidence_brief_v3_canary.py`：用 `test_alphaengine_core_acquisition` 的合成 ACN 文档跑 canary 子进程，
  断言计数、transcript Claim 的 kind / value、KPI evidence 里 filing 的「decreased 3%」与 transcript 的方向陈述并存、
  三家同行的 gap 理由、debate v3 引用 transcript Claim、来源节里出现 `authenticated_transcript` 和 citation binding。

### 真实文档运行（本机，8/24 ACN 原文 51,034 字，digest `a8a9fbff…bd96bd`）

- 获取：2 页、2 次 fake call、1 个 document unit，probe ok；correction set 5 个 unresolved ASR flag（与 live 的
  `transcript-correction-set:acn:q3fy26:1` 同一组：`r ight` / `g rowth` / `bu ying` / `sam e` / `clien ts`），citation span
  `[11823, 11962)` 与 live citation 完全一致，`claim_eligible = true`。
- stage `fresh`、verifier `pass`、accept → commit `fresh`，`review_state = committed`；Core `evidence_versions = 6`（5 SEC + 1
  transcript）、`claim_versions = 22`、`evidence_relations = 22`、`reviewed_candidate_commits = 1`、`thesis_versions = 0`。
- driver pack v4：4 driver、19 metric；pack v3：22 binding；overlays 4；brief：22 Claim、6 来源、4 scoreboard、20 行 KPI、
  80 个单元格；`industry_brief_hash = ba0302d3…d25bdc`，`report_hash = 776ce41d…bfe2b7`，replay identical。
- 输出在 workspace `temp/dalton-s7c5/out/`（gitignored）。

### 验证

- `tests.test_industry_evidence_brief_v3_canary`：1/1（首次跑时一条断言写错——正文是「decreased 3%」不是「-3」——改断言，
  canary 本身没改）。
- `tests.test_industry_evidence_canary` + `test_industry_research` + `test_transcript_qualitative_candidate` +
  `test_alphaengine_core_acquisition`：29/29。
- `compileall`、`git diff --check` 通过。全仓慢回归交 CI。

## live brief v3 的两条合同门

`src/dalton_core/industry_research.py`：

- `register_evidence_pack`：`if bound_drivers != set(driver_metrics): raise IndustryResearchConflict("evidence pack must cover every industry driver")`。
  4 个 driver 需要至少 4 条不同 driver 的正式 Claim；live 只有 1 条（bookings driver）。
- `_driver_views`：`claim_version_refs` 必须 nonempty；`register_company_overlay` 又要求 overlay 覆盖每个 driver。
  即 CTSH / EPAM / IBM 在 live 上各需至少 4 条自己的 Claim，ACN 需至少 4 条；live 全部为 0 / 1。

这两条不是 bug，是 v2 时就定下的「brief 只由正式 Claim 生成、不允许空 driver」的合同。它们意味着 vision v0.9 把「brief v3」
排在 S7c 的顺序在 live 上走不通：S7c 只产出 1 条 Claim，brief 需要每家公司每个 driver都有。

## 明确没做

- 没有在 live Core 注册 driver pack / evidence pack / overlay，没有用 `human:lumos` 做任何新的 governance 写入；
  live Core 在 accept 之后没有任何 Eve 触发的写入。
- 没有放宽上面两条合同；没有把隔离 canary 的 SEC Claim 搬进 live。
- 没有 brief 的「内容性反馈」记录机制：`record_agenda_feedback` 只绑定 Agenda decision，brief 没有对应 authority，这是 S7e 的活。
- `0efe8f5` 及本切片的 CI 仍未起；验证依据是本地定向回归。

## 下一步（要 owner 选）

- **A（推荐）：先 S7d，再 live brief v3。** 把 v2 canary 已证明的 SEC lane（ACN / CTSH / EPAM / IBM 四家季度 KPI，5 个
  accession）经 Core-hosted connector 路径进 live Core，driver pack v1→v4 与 pack v3 随之注册；这时 live 的 brief v3 = 本报告
  隔离 canary 的同一份 manifest + live 的那条 transcript Claim，合同不用动。代价：brief v3 的 live 投递推迟到 S7d 之后。
- **B：放宽合同。** 允许 evidence pack 留空 driver、overlay driver view 不引用 Claim（cell 全部 gap），live 立刻能出一份
  只有 1 条正式 Claim、79 个 gap 的 brief v3。代价：改 v2 的 additive 合同并补测试，而且这份 brief 的信息量很低。
- 与本切片无关但仍待办：S7c-3 报告里的 policy pointer 回退 prior chain、snapshot_id 带内容 hash；8/27 00:xx UTC 的万华
  cycle 回看 S7a + policy v4。
