# Ad-hoc 回答路由 v0.1

## 结果

S4 已进入 development candidate，S4.1 隔离 ACN canary 也已通过。Dalton Cockpit 新增「问答」页；Core 会先冻结只读 `AnswerContextPack`，再按
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

## 隔离 ACN canary

`scripts/run_isolated_acn_answer_canary.py` 在单个 in-memory Core 中回放仓库已有的 ACN SEC authority。它先重建
Agenda、ResearchQuestion、ResearchPlan、正式 Evidence/Claim/Relation、Industry Evidence Pack、Company Overlay
和 human-admitted Thesis，再发布 development-only answer policy。脚本不连接网络，不调用付费模型，也不打开 live
数据库。

回放结果：

- 精确问题命中两条 answer binding，返回 `answer_direct`：Q3 FY2026 new bookings 为 USD 19.32 billion，
  local-currency bookings growth 为同比 -3%；
- 改写后的宽泛问题没有命中已入库问题，返回 `recommend_agenda_item / question_not_admitted`；
- 同一精确问题在证据超过 30 天后返回 `recommend_agenda_item / stale_evidence`；
- route 前后的所有表计数、SQLite `total_changes` 和完整 `iterdump` 指纹一致；
- policy v1 换成 v2 后，旧 subject binding 被拒绝；
- 最终 authority 有 1 条 EvidenceVersion、6 条 ClaimVersion、6 条 EvidenceRelation、1 条 ThesisVersion；
  付费模型调用、成本记录、网络调用和 live 数据库写入均为 0。

canary 首次把 human-admitted Thesis 放进 answer context 时发现一处验证口径错误：router 误把整个
`ThesisVersion` wire 当成正文计算 hash。现在会先按 ThesisVersion v0.1/v0.2 闭合合同重建 wire，再只对八个 thesis
正文字段复算 `content_hash`，并同时核对 version、thesis、authority 和 legacy verification binding。

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

- S4.1 canary、Answer Routing、Coverage Admission、Agenda、Backlog、ResearchPlan、Industry、Bounded Planner、
  Contracts 与 Packaging 关联回归：153/153；
- Cockpit JavaScript 语法检查；
- Python `compileall`；
- JSON Schema closed-shape 与 packaging manifest；
- `git diff --check`。

全仓 `unittest discover` 没有重跑；既有 connector inventory 回归热点仍可能长时间停住。本轮尝试运行
`python3 -m build --sdist --wheel`，但仓库本地 `build/` namespace 没有 `build.__main__`，且当前解释器也没有
`setuptools`，因此没有生成 sdist/wheel。

## 下一步

进入 S5 前先冻结有限刷新的独立预算、权限和 candidate-staging gate。`answer_after_refresh` 与 `adhoc_research` 仍须
分开实现和验收；在相应 worker 与 human promotion gate 完成前，S4 policy 继续强制两条 route 关闭且预算为 0。
