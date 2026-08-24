# Transcript Claim Admission Gate v0.3

日期：2026-08-24  
状态：development candidate；未部署 live

## 裁决

原始 ASR 是不可变捕获记录，不是天然正确的事实。polished transcript 方便阅读和模型处理，但也不能成为第二份独立
来源。正式 Claim 的 transcript authority 由三部分组成：

`exact raw ArtifactVersion + human-admitted TranscriptCorrectionSetVersion + persisted raw-span citation binding`

因此，专有名词或数字若在原始 ASR 中识别错误，Claim 不会继续引用错误文字，也不会直接引用模型润色稿。Core 要求
Claim 同时绑定原始坐标和相交的正式修正；无法由音频、官方逐字稿或允许的一手 authority 证明的修正保持 unresolved，
并阻止相交 Claim 晋级。

## 最小合同变化

- 沿用 `CandidateEvidence 0.1` 与 `EvidenceVersion 0.2`，不新增平行 Evidence 合同；
- 新增明确的 `source_type=authenticated_transcript`；
- transcript Evidence 的 `artifact_refs` 固定为两项：第一项是 exact raw `ArtifactVersion`，第二项是
  `TranscriptClaimCitationBinding` ref/hash；
- `source_lineage` 在原始来源链末尾追加 citation binding id；
- 两份现有 JSON Schema 用 conditional 约束上述两项结构，其他 Evidence 不受影响。

`TranscriptClaimCitationBinding` 现在写入 Core 的 append-only 表。其身份由 correction-set ref/hash 和 raw span
确定，记录 exact raw source hash、accepted/unresolved correction indexes、citation mode 与 claim eligibility；SQL
update/delete 由 trigger 拒绝。

## 两级 gate

Candidate staging 没有 Core DB 权限，只检查：

- 来源不是 recorded fixture；
- 第一项仍是 verifier 看到的 exact raw ArtifactVersion；
- 第二项使用 citation-binding namespace；
- source lineage 与两项 artifact 顺序一致。

人工 review 后，正式 promotion 才是安全边界。Core 在同一提交路径中重新验证：

- citation binding 的 canonical JSON、content hash 与 SQL projection；
- exact correction-set ref/hash、source manifest/hash 与 source content hash；
- citation span 与 accepted/unresolved corrections 的实际 overlap；
- `claim_eligible=true`，且不存在 unresolved overlap；
- citation 的 raw source hash 同时等于 raw ArtifactVersion content hash 和 SourceEnvelope raw response hash。

任一 authority 缺失、hash 漂移、来源不一致或 unresolved overlap 都会在正式 Evidence/Claim 写入前 fail closed。人工
点“接受”不能绕过这道门。

## 用户提出的问题如何落地

“润色稿只能辅助阅读，Claim 仍引用原稿”确实不够严谨，因为原稿可能把数字和专名识别错。现在的准确表述是：

- raw capture 保留系统收到的原始内容，负责审计和坐标；
- admitted correction 负责纠正已被独立 authority 证明的识别错误；
- citation binding 把 Claim 精确绑定到 raw span 和这些修正；
- polished artifact 只改变展示，不改变证据身份；
- 证据不足时保留 unresolved，不把“看起来应该是”写成正式事实。

## 验证

相关超集 47/47 通过，覆盖：

- 已接受 correction 的 citation 可经过 staging、人工 review 和正式 promotion，正式 Evidence 保留两段 authority；
- unresolved citation 即使伪造成合法候选形状，也会在正式 promotion 被 Core 拒绝，正式 Claim 写入数为 0；
- citation binding 缺失在 staging 拒绝；binding hash 漂移在正式 promotion 拒绝；
- binding 可持久化重读且不可 update；
- CandidateEvidence / EvidenceVersion 的 transcript conditional schema 均可验收；
- 原有 ResearchPlan、contracts、packaging 关联回归继续通过。

同时通过 `compileall`、`git diff --check`、wheel 构建、隔离安装和 wheel 内 SQL 资源读取。全仓回归交给同提交的
独立 CI；本切片没有调用真实 AlphaEngine、音频或付费模型，也没有修改 live Core。

## 下一步

下一笔接 transcript routed model worker，并建立独立 frozen corpus。模型横评至少包含 DeepSeek V4 Flash baseline、
GLM 5.3 与 GPT-5.6 Luna；再按任务质量、安全失败、延迟和成本决定 profile。Planner corpus 的胜出结果不直接复用。
worker 只能产出 polish/correction candidates，不能发布 correction set，也不能跳过本报告的正式 Claim gate。
