# Ad-hoc 回答路由 v0.1

## 结果

S4 已进入 development candidate。Dalton Cockpit 新增「问答」页；Core 会先冻结只读 `AnswerContextPack`，再按
版本化 `AnswerSufficiencyPolicyVersion` 决定 `answer_direct` 或 `recommend_agenda_item`。这条路径不调用模型，
不创建 ResearchQuestion、WorkOrder、Evidence、Claim、Thesis 或回答 authority。

本切片没有部署 live `:8793`，没有修改 production pointer，也没有对真实 ACN authority 执行正式写入。

## 直答边界

S4 只允许一个已经进入 `ResearchQuestionBacklog`、状态为 `answered`、且问题文本与
`question_ref_for(mandate, company, question)` 完全一致的问题进入直答判断。Core 从 answer binding 重新读取正式
ClaimVersion，再沿正式 EvidenceRelation 读取 EvidenceVersion；任一 ref、hash、版本、pointer 或 SQL row 绑定漂移
都会拒绝。

`AnswerContextPack` 冻结：

- exact answer subject、MandateVersion 与 policy ref/hash；
- 匹配的 ResearchQuestion 和正式 answer bindings；
- ClaimVersion、支持/反对/限定关系与 EvidenceVersion；
- Evidence 的 `retrieved_at`、`valid_until`、source type、age、状态和适用期间；
- 当前 ThesisVersion、DriverPack、CompanyOverlay、IndustryEvidencePack；
- open questions、不可观测终态和确定性 coverage/freshness 指标。

模型不能报告“资料够新”或“证据够多”。Core 按固定顺序检查 policy 可用性、问题状态、Claim 是否被替代、支持关系、
最低正式 Claim/Evidence 数、source-class 时效、争议数、open questions、不可观测终态和 driver coverage。任一条件失败
只返回 `recommend_agenda_item`，并明确记录 `write_performed=false`。

## 策略和权限

`AnswerSufficiencyPolicyVersion` 是 human-only、append-only authority，绑定 active MandateVersion 的 exact hash。
策略 pointer 换版会让旧 answer subject binding 失效。SQLite trigger 要求所有 version、pointer 和 idempotency 写入都
经过 `AnswerRoutingAuthority`。

writer principal 保持分离：

- 临时认证 `human:*` governance principal 可以发布策略；
- `dashboard-control` 只增加 `answer_subjects` 与 `route_answer` 两个只读 operation；
- worker、review principal 和其他 scoped principal 不能读取或发布回答策略。

Cockpit 的 `GET /v1/answer` 与 `POST /v1/answer/route` 复用现有 Tailscale identity、session 和 CSRF。页面显示
context/decision hash、正式 Claim/Evidence、时效状态和闭合原因。

## S4 明确不做的事

`answer_after_refresh` 与 `adhoc_research` 继续关闭。v0.1 policy 强制两条 route 的 `enabled=false`、预算为 0，且
不能绑定 ProbeTemplate 或 rounds。它们要等独立预算池、worker 和 candidate-staging gate 完成后才能进入 S5。

## 验证

- S4、Cockpit、writer、Agenda、Backlog、Industry、Bounded Planner 与合同邻接回归：122/122；
- Cockpit JavaScript 语法检查；
- Python `compileall`；
- JSON Schema closed-shape 与 packaging manifest；
- `git diff --check`。

全仓 `unittest discover` 没有重跑；既有 connector inventory 回归热点仍可能长时间停住。本轮尝试运行
`python3 -m build --sdist --wheel`，但仓库本地 `build/` namespace 没有 `build.__main__`，且当前解释器也没有
`setuptools`，因此没有生成 sdist/wheel。

## 下一步

先用隔离 ACN 正式 authority 发布一版 development-only sufficiency policy，确认已回答问题能生成首条只读直答，
未匹配或陈旧问题只返回 Agenda 建议。完成真实只读 canary 后，再进入 S5 的有限刷新和独立 ad-hoc research 预算。
