# Transcript Correction Authority v0.2

日期：2026-08-23
状态：development candidate；取代 TranscriptPolishWorker v1 的 `original_only` 引用规则

## 更正的架构判断

原始 ASR 必须不可修改，因为它记录系统实际收到的内容，便于审计和重放；但它不等于语义正确的逐字稿。ASR 可能把
专有名词、金额、比例、否定词或 speaker 识别错。如果 Claim 永远只引用原始 ASR，系统会把已知的转录错误当成事实依据。

当前链路改为：

`immutable raw capture → human-admitted correction set → resolved source → polished derivative`

- raw capture 保存原始 bytes、manifest、hash 和原始坐标，不允许原地修改；
- correction set 记录对 exact raw span 的修正或 unresolved 标记，并绑定 exact evidence authority；
- resolved source 只由 raw capture 与 admitted corrections 确定性生成；
- polished derivative 只供模型上下文和人工阅读，永远不是 Evidence/Claim authority。

## 修正准入

`TranscriptCorrectionSetVersion` 是 human-only、append-only 的 Core authority。每条修正保存 raw start/end、raw slice
SHA-256、修正类型、处置、替换文本、理由和 evidence bindings。版本不能 update/delete，也不能在版本链中切换原始来源。

准入门槛按风险分层：

- `proper_name / terminology`：可以由音频、官方逐字稿或公司等一手资料支持；
- `numeric / negation / semantic / speaker_label`：必须有音频 span 或官方逐字稿 span；
- filing、公司公告或其他 primary reference 即使与某个数字一致，也不能单独证明说话人当时说了这个数字；
- 证据不够时记录 `unresolved`，不猜测，不把候选修正写入 resolved source；
- 模型可以在后续 worker 中提出 correction candidate，但不能发布正式 correction set，正式发布要求 `human:` actor。

## Claim 引用规则

`TranscriptPolishArtifactVersion` 现为 0.2，绑定 raw manifest/hash、可选 correction-set ref/hash、resolved-source hash、
raw-to-resolved correction mappings、resolved-to-polished span mappings 与 unresolved spans。artifact 固定：

- `citation_authority=source_lineage_only`
- `claim_citation_mode=raw_span` 或 `raw_span_plus_admitted_correction`

`TranscriptClaimCitationBinding` 使用 raw 坐标。Core 重新计算 raw slice SHA-256，并列出与该 Claim span 相交的 accepted
和 unresolved correction indexes。存在 accepted correction 时，Claim 必须连同 exact correction-set ref/hash 一起引用；
存在 unresolved overlap 时，`claim_eligible=false`，只能作为待审 candidate，不能成为正式 Claim。

这条规则避免两个相反错误：既不把 polished 文本伪装成第二个来源，也不把已知错误的原始 ASR 当成语义真相。

## TranscriptPolish 接线变化

模型的 polish candidate 合同仍为 0.1。Core 先解析 admitted corrections，得到 resolved source，再对候选做完整 span
partition、数字顺序、受保护专名顺序和长度守恒。模型不能在 polish 阶段偷偷改数字或专名；正确修正必须先进入 correction
authority。probe output 与 artifact 合同升为 0.2，并向 Planner Outcome 暴露 correction lineage 和 unresolved spans。

## 验证

专项 6/6、相关回归 33/33 通过，覆盖：

- 专名修正可由 exact primary reference 准入，并进入 resolved source；
- 数字修正仅绑定 primary reference 时拒绝，绑定官方逐字稿 span 时接受；
- unresolved 数字保留原文，并阻止相交 Claim citation；
- evidence ref/hash drift、非 human actor、raw span hash drift 全部 fail closed；
- correction set 与 polished artifact 都是 append-only，SQL update/delete 被拒绝；
- 原有完整 manifest、bytes、数字/专名守恒、Planner Loop probe 与 contract 回归继续通过。

## 未完成项

- 还没有把 `TranscriptClaimCitationBinding` 接入通用 Claim admission gate；
- 还没有 correction-candidate 模型 worker、人工审阅 UI 或真实 AlphaEngine/audio canary；
- 还没有 transcript 专用 frozen corpus 和模型横评；
- 目前 evidence resolver 只验证 exact authority id/hash，生产版还需按 authority kind 校验 location 是否落在对应音频或官方逐字稿范围内；
- 语义守恒仍不能完全机械证明，无法证明安全的内容必须保持 unresolved 或回退 raw capture。

下一笔先接通用 Claim admission gate；随后再接 routed transcript worker，并单独比较模型。Planner 已选的 DeepSeek V4
Flash 只对 Planner frozen corpus 成立，不能直接当作 transcript worker 的模型结论。
