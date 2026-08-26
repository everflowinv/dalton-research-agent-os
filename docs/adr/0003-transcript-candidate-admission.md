# ADR-0003：transcript 候选如何进入 CandidateStaging

- 状态：Accepted（2026-08-26，owner 授权 Eve 裁决，选 B；owner 可否决）
- 日期：2026-08-26（Proposed）→ 2026-08-26（Accepted）
- 适用范围：S6 ACN 首条正式 Claim、后续所有以电话会 / 访谈逐字稿为来源的候选

## 背景

S6 计划让「ACN Q3 FY2026 新签订单本地货币同比 -3%」从 AlphaEngine 逐字稿走完
`CandidateStaging → Cockpit accept → 正式 Evidence / Claim`。现有合同下这条路走不通：

- `CandidateClaim 0.1` 只接受 `claim_kind = quantitative`，并要求 `numeric_spec` 与 `numeric_verification`；
  `CandidateStagingStore.stage()` 会用 deterministic verifier 从**同一份** source material 的 `normalized_payload`
  按 JSON pointer 重新抽数并复算。
- AlphaEngine `get_document` 的 normalized payload 是 `{source_record_refs, next_cursor, provider_status}`，
  没有可抽的业务数字。逐字稿里的「3%」只存在于 raw text span。
- `HumanReviewAuthority.decide()` 和 `commit_reviewed_candidate()` 不再复算数值，但它们只接受 staging 表里已有的
  候选；staging 是唯一入口。
- 项目此前定下的原则：`authenticated_transcript` 不能作为 numeric / numeric_and_semantic metric 的**唯一**
  observed authority，semantic metric 可以。S6 packet 也把该候选标为
  `brief_v3_verdict: null_numeric; add transcript corroboration only`，并指定 SEC exhibit claim 为
  `required_secondary_numeric_authority`。

也就是说：这个候选在设计上就是「数值来自 SEC，逐字稿提供管理层口径与引用」。现在的 staging 合同表达不了这件事。

## 选项

### A. 双 material 候选：数值验证绑定 SEC exhibit material，来源验证绑定 transcript material

- `stage()` 放宽「numeric spec 只能绑定同一份 material」：允许 numeric inputs 指向另一份已在同一 staging 里、
  `source_type = official_filing` 的 material；transcript material 继续负责 `source_verification` 和 citation。
- 需要 SEC exhibit（`0001467373-26-000031` 的 `q3fy26earnings8-kexhibit.htm` 或 company-facts）先以 Core-held
  connector authority 进入同一个 Core，也就是 SEC connector 也要走 S6b 同样的 Core-hosted 路径。
- 正式 ClaimVersion 0.2 不变（仍是 quantitative），EvidenceVersion 0.2 也不变；transcript Evidence 与 SEC Evidence
  通过既有 `relate_evidence` 建 `supports` 关系。
- 改动面：`research_verification.py` 的 staging 校验、`candidate_authority_bundle` 的单 material 断言、
  CandidateClaim schema 允许 `numeric_material_ref`（新增字段，属于合同变更）。

### B. 语义候选：新增 `claim_kind = qualitative` 的 CandidateStaging 0.2

- 候选不带 `numeric_spec`；`value / unit / scale` 为 null；staging 只做 source verification 和 citation 校验；
  正式 ClaimVersion 0.2 也要放开 `claim_kind`（Ledger 0.1 的 `claim-version.schema.json` 本来就允许
  `qualitative`，0.2 收紧成了 quantitative-only）。
- 「-3%」这条就不能作为 transcript 语义 Claim 的 value 出现；只能写成「管理层称本地货币口径新签订单下降」这类
  语义陈述，数字回 SEC Claim。
- 改动面：CandidateClaim / ClaimVersion 0.2 schema 与 validator、`stage()` 分支、Cockpit 审阅页对空数值的展示。
  改动最小，但把 quantitative-only 这道 guard 打开了一个口子，需要 policy 明确 qualitative 候选永远不能自动晋级。

### C. span 数值抽取器：给 numeric spec 加 `extractor = "text_span_percent"`

- 让 deterministic verifier 从 raw span 里抽「3%」并按「decrease」取负号。
- 把逐字稿当成数值 authority，与「transcript 不能作为 numeric metric 唯一 observed authority」的既定原则冲突；
  抽取规则也很难做成闭合、可审计的确定性合同。不推荐。

## 裁决（2026-08-26）

**选 B，不选 A / C。** 实现见 `reports/s7b-qualitative-transcript-candidate-staging-v0.1-2026-08-26.md`。

- A 的隐藏成本比草案写的大。仓库里 ACN 的 SEC 数字（USD 19.32bn、-3%）来自
  `deploy/coverage/us-it-services-industry-evidence-v1.json`，是 `human:coverage-owner` 手工登记的 evidence pack，
  不是 connector 数值验证产物；SEC exhibit 是 HTML，现有 numeric extractor 只有 `number` / `count` 两种 JSON
  pointer。A 要先造一个 exhibit 解析器才能让 deterministic verifier 从 SEC material 复算 -3%，本质上又回到
  文本抽数——和 C 是同一个问题，只是换了来源。
- 2026-08-24 已定原则：`authenticated_transcript` 不能作为 numeric / numeric_and_semantic metric 的唯一 observed
  authority，但 semantic metric 可以。B 就是把这条原则写成合同：transcript 候选以 `claim_kind = qualitative` 进
  staging，数值字段全为 null，「-3%」回 SEC Claim。
- B 打开的口子用三道 guard 关住：qualitative 只接受带 exact citation binding 的 transcript evidence；
  `commit_policy_candidate` 与 `research_auto_commit` 的所有 policy 路径拒绝 qualitative；正式入库只经
  explicit human review。
- 代价：S6 的第一条正式 transcript Claim 是语义陈述，不是 -3%。数值 Claim 走 S7d 的 SEC lane。

## 推荐（裁决前原文，保留供追溯）

**A**，理由：它和 packet 里已经写下的 `required_secondary_numeric_authority` 一致，正式 Claim 仍是 quantitative，
不动 Ledger 0.2，且顺带把 SEC connector 也推上 Core-hosted 路径——这是 US IT Services brief v3 本来就需要的。
代价是 S6 要多一段「SEC exhibit 进 Core」的获取，以及一次 CandidateClaim 合同的 additive 变更。

如果 owner 认为 S6 的目标只是「第一条真实 Claim 进 Ledger」而不要求它是数值 Claim，B 更快，但要接受第一条正式
transcript Claim 是语义陈述而非 -3%。

## 不在本 ADR 范围

- Core-hosted connector 获取本身（见 `reports/s6-formal-promotion-authority-gaps-and-core-acquisition-v0.1-2026-08-26.md`）。
- 自动晋级 policy；无论选 A 还是 B，transcript 候选都只能经 explicit human review 进入 Ledger。
