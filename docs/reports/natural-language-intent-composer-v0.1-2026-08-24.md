# 自然语言 Intent Composer v0.1

## 结果

S3 的第一个可运行切片已经进入 development candidate。Dalton Cockpit 新增「方向」页，Owner 只提交自然语言；
后端冻结 `HumanUtteranceVersion`、exact `IntentContextPack`、模型调用 provenance 和 closed `IntentCandidateVersion`。
页面只展示 typed candidate，没有执行、确认或 writer dispatch 端点。

本切片实现五种 effect：

- `research_question_draft`
- `research_directive_candidate`
- `priority_override_candidate`
- `context_bound_approval_candidate`
- `meta_read`

`doctrine_or_driver_revision`、`correction` 和 `mandate_budget_permission` 可以被分类，但只能返回
`unsupported` 或 `clarification_required`。它们不能借用 priority/directive effect，也不能写 Evidence、Claim、
Thesis、预算、权限、connector 或 production pointer。

## Authority 与模型边界

`POST /v1/intent/compose` 只接受 `request_id` 和 `utterance`。Tailscale login 由服务端哈希成 `human:` actor；
客户端不能提交 actor、context、ref/hash、intent kind 或 effect。Cockpit 在请求时重新读取 Agenda、研究审阅、
transcript review 和 trajectory，生成 exact context bindings。

append-only intent staging 与 Core DB 分离，保存：

1. `IntentContextPack`；
2. verbatim `HumanUtteranceVersion`；
3. interpreter attempt；
4. contract-valid `IntentCandidateVersion`；
5. idempotent compose result。

SQLite trigger 禁止更新和删除。读取候选时重新核对 canonical JSON、content hash、utterance/context/attempt lineage、
candidate contract 和 `candidate_only=true / executable=false`。

解释器走独立、无 side effect 的 `extract` WorkOrder。Scheduler 保存 formal ResultEnvelope，ModelRouter 保存 exact
route decision；intent staging 另外保存 WorkOrder/result/route/profile ref/hash 和完整 ModelInvocation usage。模型不能
通过输出改预算、权限、actor 或 operation。

全局 composer 没有 focused target，因此“同意”“批准”“yes”等裸审批必须澄清。明确审批仍只能生成绑定 exact
target ref/hash 的高风险候选；当前没有执行按钮或确认 API。catalog 外 probe 不能伪装成 `request_replan`，缺少明确
priority delta 时也不能让模型补数字。

## 冻结合同

[ADR-0002](../adr/0002-natural-language-intent-and-answer-routing.md) 同时冻结了八类 intent taxonomy 和后续
ad-hoc answer router 的四个 route：`answer_direct`、`answer_after_refresh`、`adhoc_research`、
`recommend_agenda_item`。S4 仍未实现。

对应 JSON Schema：

- `contracts/intent-context-pack.schema.json`
- `contracts/human-utterance-version.schema.json`
- `contracts/intent-candidate-version.schema.json`

## 真实模型校准

冻结语料共 16 条，覆盖正常 question/directive/priority/approval/meta，以及裸审批、context 外 ref、hash 漂移、
catalog 外 scope 扩张、预算/权限升级、formal authority 绕过和 prompt injection。对明确 correction，
`unsupported` 与 `clarification_required` 都是 ADR 允许的安全结果，因此语料把两者列为 accepted outcomes。

最终使用 exact `profile:gpt-5-6-terra`、临时 Scheduler/Router 副本和真实 OpenClaw broker 复验：

- semantic match：16/16；
- safety：9/9；
- 总 token：30,295；
- provider-reported cost：USD 0.13069000；
- cost telemetry available：16/16；
- interpreter hash：`a705ab416b35000185d3764b2c58fd377c4f3e6771a470d8273f4157af861639`；
- corpus hash：`ec0d56411ea5900827ea5029228c9d9b6d50c366e631f978b65d9a4bb7a330f7`。

校准只写临时目录，没有改 live Router policy、service config 或 production pointer。

## 验证

- 相关 Python：89/89；
- Cockpit JavaScript：`node --check`；
- `compileall`；
- `git diff --check`；
- 三份新增 JSON Schema 已随 package data glob 收录；`human_intent_schema.sql` 已显式加入 wheel package data。

## 未完成

- S3B：typed candidate 二次确认，以及经原 writer principal 执行 directive/priority/approval；
- question draft 的 mandate binding 与 ResearchQuestion admission；
- intent interpreter 的 immutable live routing policy 与 live service config；
- S4 AnswerContextPack 和 sufficiency/freshness router；
- live `:8793` 部署与第二台 tailnet 设备验收。

本轮没有部署 live，没有执行任何候选，也没有新增正式 Evidence/Claim/Thesis。
