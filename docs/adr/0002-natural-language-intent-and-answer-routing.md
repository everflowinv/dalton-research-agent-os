# ADR-0002：自然语言意图与回答路由合同

- 状态：Accepted
- 日期：2026-08-24
- 适用范围：Dalton Cockpit 自然语言 composer、后续 ad-hoc answer router

## 背景

Owner 应该能用自然语言改变研究方向或提出问题，但一句话不能直接变成 writer RPC。模型也不能因为读懂了
“多关注供需”“把这个往后放”或“同意”，就自行扩大 mandate、增加预算、取得权限或批准研究结论。

Ad-hoc 问答还需要区分“现有正式知识足够回答”“证据需要刷新”“需要单独研究”和“应进入 Agenda”。这项判断
不能交给回答模型自报；Core 必须根据 exact context、版本化 policy 和 evidence 时间确定性计算。

## 决定

### 1. 意图翻译链

固定处理链如下：

```text
verbatim owner utterance + exact Cockpit context
  → HumanUtteranceVersion
  → bounded LLM interpreter WorkOrder
  → closed IntentCandidate
  → Core revalidation
  → candidate-only preview
```

第一版 taxonomy 固定为：

- `question`
- `directive`
- `priority`
- `doctrine_or_driver_revision`
- `correction`
- `approval`
- `mandate_budget_permission`
- `meta`

解释器可以返回 `candidate`、`clarification_required` 或 `unsupported`。只有 `candidate` 可以带 typed effect；后两者
必须把 effect 设为 null。S3 只接受五类 typed effect：

- `research_question_draft`
- `research_directive_candidate`
- `priority_override_candidate`
- `context_bound_approval_candidate`
- `meta_read`

`doctrine_or_driver_revision`、`correction` 和 `mandate_budget_permission` 在 S3 可以分类，但只能返回
`unsupported` 或 `clarification_required`，不能借用其他 effect。

### 2. Exact context 与执行边界

`IntentContextPack` 只收录当前 Cockpit 重新读取的对象。每个 binding 固定 `kind/ref/hash/label/state/authority` 和
允许的 intent；模型必须逐字段复用 binding，不能只回传一个看似存在的 ref。Core 拒绝 context 外引用、hash 漂移、
父 loop 不匹配的 coverage item、未知 priority feature、预算/权限字段和任何额外 effect 字段。

`HumanUtteranceVersion` 保留原文、匿名化 human actor、exact context ref/hash 和 request id。模型调用另有
interpreter WorkOrder、route decision 和 formal ResultEnvelope；intent staging 保存其 exact provenance。

S3A 没有 effect 执行端点。S3B 增加独立确认链：只有原提交人能显式确认 high-risk candidate；Core 必须重新读取
当前 `IntentContextPack`，并逐字段核对 effect 内每个 binding。确认不会把候选改成 executable，也不会直接取得
writer 权限；系统另存 append-only `IntentConfirmationReceipt`，再把 effect 交给原 writer principal。每次分派另存
append-only `IntentDispatchReceipt`，失败重试要使用新的 request id，成功后不得重复写入。

question draft 在 writer 内重新绑定 exact MandateVersion；Agenda decision、bounded planner loop 和 coverage item
必须能确定性解析到单一 mandate/company。directive 继续走 Bounded Planner writer，priority 继续走 Agenda writer，
Agenda/research/transcript approval 继续走各自控制面。全局 composer 中没有 focused target 时，“同意”“批准”
“可以”“yes”等裸审批仍必须返回 `clarification_required`。Agenda timeout 和自动化账号永远不能生成 human
confirmation。context、candidate 或 writer authority 任一漂移都 fail closed。

### 3. Ad-hoc sufficiency/freshness closed contract

后续 `AnswerContextPack` 必须冻结正式 Claim/Evidence、支持与反对关系、当前 ThesisVersion、IndustryDriverPack、
open questions，以及每条 evidence 的适用期间、`retrieved_at`、状态和 exact hash。回答模型不能修改这个 pack。

`AnswerSufficiencyPolicyVersion` 必须显式给出：

- 最低 driver coverage；
- 各 source class 的最大 evidence age；
- 允许的冲突数与 open-question 数；
- direct answer 所需的最少正式 Claim/Evidence 数；
- refresh 与 ad-hoc research 的独立预算和已批准 ProbeTemplate 范围。

Core 按固定顺序只返回以下四种 route：

1. mandate、权限或 capability 不满足，或所需预算未获批准：`recommend_agenda_item`；
2. coverage、freshness、冲突、open question 和最少正式 authority 全部满足：`answer_direct`；
3. 只因 freshness 失败，且存在已批准、预算内的 refresh probe：`answer_after_refresh`；
4. 仍在 mandate 内且独立 ad-hoc 预算可用：`adhoc_research`；否则 `recommend_agenda_item`。

S4 首先实现 `answer_direct` 与 `recommend_agenda_item`。另外两种 route 在合同中保留，但在对应 worker 和预算池上线前
必须 fail closed。`AdHocAnswerVersion` 只是一份带 as-of、context hash、引用和成本的交付物，不是 Evidence、Claim、
Thesis 或后续 ContextPack 的权威输入。

## 安全与校准要求

- 冻结语料必须同时覆盖正常翻译、语义错译、context 外 ref 注入、hash 漂移、模糊审批、scope 扩张、预算/权限请求和
  prompt injection。
- intent interpreter 使用独立的 model profile、routing policy、校准分数和成本记录，不能复用 planner 的准入结论。
- 模型输出不是成功条件。Core closed-shape 校验、exact binding 校验和 candidate-only 边界都通过后，才可展示候选。
- UI、intent staging 或 answer artifact 被删除或篡改，都不能改变 Core authority。

## 影响

- Owner 可以只写自然语言；结构化合同和权限校验仍由 Core 决定。
- S3A 先完成安全预览和校准；S3B 只在显式 human confirmation 与 exact revalidation 后开放原 writer 路径，
  不合并 principal，也不把 staging 变成 authority。
- S4/S5 的回答与刷新路径已有封闭状态机，不需要让模型临时判断“资料够不够新”。
- 本决定不部署 live Cockpit、不打开 production pointer，也不新增 mandate、预算或 connector 权限。formal research
  写入仍只发生在 explicit human confirmation 后，并继续经过既有 review writer 与 gate。
