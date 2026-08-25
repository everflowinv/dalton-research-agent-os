# 自然语言 Intent 二次确认与 writer dispatch v0.2

## 结果

S3B 已进入 development candidate。Dalton Cockpit 的高风险 typed candidate 现在可以由原提交人显式确认；系统先用
最新 Cockpit context 逐字段重验 ref/hash/state/parent/allowed intents，再交给既有 writer principal。候选记录本身
仍是 `candidate_only=true / executable=false`，确认和分派用独立 receipt 表达。

本切片没有部署 live `:8793`，没有修改 production pointer，也没有对真实 ACN 数据执行候选。正式
Evidence/Claim/Thesis 写入仍为 0。

## 确认合同

`POST /v1/intent/confirm` 只接受 `request_id`、candidate ref/hash 和固定 decision `confirm`，并复用 Cockpit 的
Tailscale identity、session 与 CSRF。服务端拒绝另一位 human、候选 hash 漂移、context binding 缺失或变化、
非 candidate effect 和不支持的 effect。

intent staging 新增三组 append-only 记录：

- `IntentConfirmationReceipt`：绑定 candidate、原 utterance/context、确认时 context、human actor 和 effect kind；
- `IntentDispatchReceipt`：按 confirmation 保存每次 succeeded/failed writer attempt；
- confirmation request receipt：冻结 request id 的幂等结果，失败重试必须换 request id。

SQLite trigger 禁止更新或删除这些记录。读取时会重新核对 canonical JSON、content hash、candidate/utterance/context
lineage 和最近一次 dispatch 状态。成功 dispatch 后再次确认只返回既有结果。

## 原 writer 路由

- `research_question_draft`：临时 `human:*` governance principal 调用 Core `IntentWriterAuthority`。writer 重新解析
  active MandateVersion，或从 exact Agenda decision、bounded planner loop、coverage item 解析唯一 mandate/company，
  然后调用原 `ResearchQuestionBacklog.record_question`。
- `research_directive_candidate`：writer 重新核对当前 open loop 和 coverage state，再调用
  `BoundedPlannerAuthority.issue_directive`；不能改预算、template、参数或权限。
- `priority_override_candidate`：临时 `human:*` principal 调用原 Agenda priority override writer，scope ref 来自确认时
  exact bindings，时效和 idempotency identity 由服务端生成。
- `context_bound_approval_candidate`：Agenda decision 走 `dashboard-control`；Candidate Claim 走
  `research-review-control`；transcript packet 走原 transcript human governance。

`dashboard-control` 只增加一个只读 `intent_context_bindings` operation，用来读取 active mandate、open loop 和
coverage binding。Agenda feedback、research review、transcript governance 和临时 human writer 仍是不同 principal；
Cockpit 不持有 Core DB 路径。

## 模型与合同

解释器 prompt、closed candidate contract、16-case frozen corpus、interpreter hash 和 corpus hash 均未改变。S3B 只消费
已通过 S3A 校准的 typed candidate，因此没有新增真实模型调用。S3A 的 GPT-5.6 Terra 结果仍是 semantic 16/16、
safety 9/9。

新增 JSON Schema：

- `contracts/intent-confirmation-receipt.schema.json`
- `contracts/intent-dispatch-receipt.schema.json`

## 验证

- S3B 与关联 authority 回归：172/172；
- 新增 Agenda decision → exact mandate question admission 敌对/正常路径；
- Cockpit JavaScript 语法检查；
- `compileall`；
- 全量 JSON Schema contract check 与 packaging manifest check；
- `git diff --check`。

全仓 `unittest discover` 未重跑；既有 connector inventory 热点仍可能让全仓矩阵长时间停住。sdist/wheel 也未重跑；
本机 Python 3.13/3.14 都可导入 `build`，但缺少 `setuptools` backend。

## 下一步

S4 实现只读 `AnswerContextPack`、版本化 sufficiency/freshness policy，以及首批
`answer_direct / recommend_agenda_item` 路由。refresh 和 ad-hoc research 在独立预算池、worker 与 gate 上线前继续
fail closed。
