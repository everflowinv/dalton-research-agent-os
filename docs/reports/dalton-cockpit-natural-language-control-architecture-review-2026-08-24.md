# Dalton Cockpit 与自然语言方向控制：架构审阅与开发裁决

日期：2026-08-24
审阅：Fable 5（独立只读审阅）
最终裁决：Go（有条件）

## 结论

Dalton 收敛为一个 tailnet-only 私有 Cockpit。现有 `:8793` 是唯一操作入口；Agenda、研究审阅、轨迹、自然语言方向控制和 ad-hoc 问答共用一个 UI shell。公开 dashboard 继续只读，不接收控制输入。

这不是对既有 Vision 的转向。v0.1 已规定人类面对研究组合和认知，而不是底层任务队列；v0.2 的 scheduler policy boundary 与 commit service boundary 是逻辑边界，不要求拆成多个网站。最新要求真正增加的是两份合同：自然语言意图路由，以及 ad-hoc 问答的充分性与时效路由。

首个开发切片锁定为：**把 research review 与 transcript correction review 并入 `:8793`，用 ACN Q3 FY2026 真实 transcript 打通第一条人工审阅路径。** 在此之前不做 composer 和完整 trajectory，避免先造没有真实消费者的界面与 schema。

## 产品语义

### 唯一入口

Cockpit 共用：

- Tailscale Serve 入口、身份校验、session、CSRF 和页面 shell；
- Agenda、待审、轨迹和自然语言 composer 的导航；
- inline 审批卡片。

Cockpit 不合并：

- `dashboard-control`、`agenda-timeout`、`research-review-control` 和 human governance 的 writer principal；
- Agenda 超时自动接受与人工研究入库；
- read model 与正式 authority；
- 私有 Cockpit 与公开只读 dashboard。

Cockpit 进程不得持有 Core 数据库路径。一个页面不等于一个写权限，所有状态变更仍由 Core 的 scoped writer、closed contract 和 fail-closed gate 决定。

### 自然语言方向控制

Owner 只输入自然语言，不填写 priority、mandate、directive 等结构化表单。处理链为：

```text
owner utterance（原文 + 当前界面对象 ref/hash）
  → HumanUtteranceVersion
  → LLM interpreter（只读 exact context pack）
  → IntentCandidate（封闭枚举，不能执行）
  → Core 验证与分派
  → 低风险 receipt / 高风险 typed effect 回显并二次确认
```

第一版 intent taxonomy：

- `question`：进入 ad-hoc 路由或生成 ResearchQuestion 草案；
- `directive`：生成 ResearchDirectiveVersion，从下一轮规划生效；
- `priority`：生成 PriorityOverride / AgendaPolicy 候选；
- `doctrine_or_driver_revision`：生成 DoctrinePack / IndustryDriverPack 新版本候选；
- `correction`：进入 transcript correction 或 Claim challenge；
- `approval`：必须绑定用户当时看到的 exact candidate ref/hash；
- `mandate_budget_permission`：永远回显并显式确认；
- `meta`：只读查询。

LLM 只能翻译和起草候选，不能修改 scope、预算、权限、Evidence、Claim、Thesis 或 production pointer。

### Ad-hoc 问答

Ad-hoc 问题不是自动 WorkOrder。以“你怎么看 ACN 的竞争壁垒”为例，Core 先物化 AnswerContextPack，冻结：

- 正式 Claim/Evidence 及其支持、反对、冲突关系；
- 当前 ThesisVersion；
- IndustryDriverPack 与 company overlay；
- open questions、contested claims；
- 每条证据的 `retrieved_at`、状态和适用期间。

Core 按版本化 policy 确定性计算 driver 覆盖率、证据年龄、冲突数、未决问题和 coverage-complete unobservable。路由结果是封闭四选一：

1. `answer_direct`：从现有知识网络回答，每个事实性论断绑定 Claim/Evidence ref，并注明 as-of；
2. `answer_after_refresh`：只做已批准 ProbeTemplate 内的有限 connector 刷新，新事实仍进入 candidate staging；
3. `adhoc_research`：启动独立小预算 Bounded Planner Loop，不自动升级为 Agenda；
4. `recommend_agenda_item`：超出 mandate、能力或预算时只提出建议或审批请求。

模型不得自判“知识已经够新”。`AdHocAnswerVersion` 是带 snapshot/hash/cost 的交付物，不是 belief authority，不能写 Claim、改 Thesis pointer，也不能成为后续 ContextPack 的权威输入。

### 审批

自然语言可以表达审批，但入库必须满足：

1. 绑定 exact candidate ref/hash；对象变化即 fail closed；
2. Core 回显 typed effect；高风险动作需要明确确认；
3. production pointer、预算、权限、mandate 和 doctrine 变更永远二步确认；
4. 自动化账号、Agenda 超时接受和仅收到 owner 消息都不算人工研究审阅。

## ACN 第一条真实轨迹

ACN Q3 FY2026 transcript 继续作为第一条端到端轨迹：

```text
AlphaEngine 双页采集
  → raw manifest / SHA-256 / pagination lineage
  → targeted transcript correction review
  → Claim raw-span citation binding
  → candidate Evidence / Claim review
  → formal Evidence / Claim
  → US IT Services brief v3
```

选择 ACN 的原因：它已经覆盖 connector 分页、模型调用、保真 gate、成本、错误和候选 Claim；当前唯一硬阻塞正是缺少 authenticated human correction review。换样本会丢失已经积累的真实边界证据。

Cockpit 用两张连续的待审卡完成这条路径。Transcript correction 卡展示 raw span、五个 ASR flag、引用区间、`-3%` 符号和 SEC secondary numeric authority，只执行 exact correction set 的明确发布与 citation binding；它不为“拒绝”另造 authority。随后 candidate Claim 卡继续使用既有 `accept / revise / reject` 决策。citation 与 unresolved flag 重叠时必须继续阻断。

## 后端边界

- trajectory 是既有 append-only authority 的 disposable read projection，不新增 authority 表；
- 默认只显示 research-event：Agenda → question/plan → WorkOrder → connector/model → artifact → candidate → review → Claim → brief；
- 分页、retry 和低层调用折叠显示；拒绝事件可聚合但不能隐藏；
- 只展示输入、工具调用、结果、artifact、成本、错误和 lineage，不展示或编造 hidden chain-of-thought；
- transcript correction 与 citation RPC 继续走 human governance gate；research candidate commit 继续走 `research-review-control`；Agenda timeout 不能获得 review 权限。

## 对当前未提交改动的审阅

- correction/citation writer RPC：方向正确，使 Cockpit 不需要 Core DB 路径；
- AlphaEngine SourceEnvelope promotion：真实分页响应 artifact hash 与 assembled transcript digest 本来就不同，按 exact document binding 放宽合理；合入前必须增加伪造 `source_record_refs` 的敌对测试；
- transcript numeric authority policy：authenticated transcript 不得成为 numeric 或 numeric-and-semantic KPI 的唯一 observed authority，保留；
- canary `acquire_only`：只降低重复采集成本，无 authority 影响；
- 独立 `research_review_control` 部署入口：首切片完成后物理移除，保留内部 plane，不再形成第二个网站。

## 开发顺序

### S1：Cockpit 合并与 ACN review

把 research review 和 transcript correction review 嵌入 `:8793`；共用 session/CSRF，writer principal 保持分离。用 ACN 真实 packet 走通一次。

验收：

- Agenda 行为和超时规则不变；
- review commit 只能经 `research-review-control`；
- transcript correction/citation 只能经 explicit human governance；
- candidate ref/hash 漂移、packet/hash 漂移、未决 correction 与 citation 重叠均 fail closed；
- 独立 review serve/config/CLI 入口消失；
- production pointer 仍关闭。

非目标：composer、trajectory、新 authority 表、production rollout。

### S2：Trajectory 只读投影

先渲染 ACN 一条完整 research-event 轨迹。投影可以删除重建，篡改投影不能影响 admission；每个节点能回指 exact ref/hash。

### S3：自然语言 composer

先接已有的 directive、priority 候选、question 草案和 context-bound approval。冻结包含错译、越权与模糊审批的意图语料；任何效果都不得绕过 Core。

### S4：Ad-hoc 路由 v1

先实现 `answer_direct` 与 `recommend_agenda_item`。AnswerContextPack、freshness policy 和事实引用均确定性校验；回答不写正式 authority。

2026-08-25 development candidate 已完成这两条只读 route；refresh 与 ad-hoc research 仍按本裁决关闭。实现记录见
[Ad-hoc 回答路由 v0.1](ad-hoc-answer-routing-v0.1-2026-08-25.md)。

### S5：有限刷新与 ad-hoc research

接入独立日预算池和 Bounded Planner Loop。预算耗尽必须以 `budget_exhausted` 结束；新事实全部进入 candidate staging。

2026-08-25 的 S5A development candidate 已先完成 `answer_after_refresh`：只有 stale-only 的 exact answered question
可以绑定一个 human-created、未启动、单轮 Bounded Planner Loop。独立日预算先生成 append-only reservation，再复用
既有 Scheduler/WorkOrder；无命中形成 `coverage_complete_unobservable_candidate`，有命中必须绑定 exact
ResultEnvelope、SourceEnvelope 和 CandidateStaging receipt。Cockpit 目前只显示获准计划，没有 dispatch endpoint；
ad-hoc research 继续关闭。实现记录见
[有限回答刷新 S5A v0.2](answer-after-refresh-s5a-v0.2-2026-08-25.md)。

## 主要失效模式

- 语义错译：封闭 taxonomy、原文留档、typed effect 回显；
- 陈旧回答：强制 as-of 与 Core freshness gate；
- ad-hoc 绕过 Agenda 扩大范围：独立预算池，不能创建新 connector/ProbeTemplate；
- “同意”指向不明：context ref/hash 绑定，变化即拒绝；
- UI 成为第二权威：不持有 Core DB，principal 不合并，自动化不能代填 `human:`；
- 轨迹噪音：research-event 默认层级，物理调用折叠；
- 过早 production：Cockpit 上线不自动打开 production pointer；
- intent interpreter 与 planner 的相关性错误：两者模型 profile、校准语料和计费分开。

## 最终 Go 条件

1. 首个切片严格为 S1；
2. SourceEnvelope 放宽补伪造 binding 敌对测试；
3. S3 前用短 ADR 冻结 intent taxonomy 和 sufficiency/freshness closed contract；
4. 独立 research review 网站不部署；
5. 至少一次真实人工 ACN correction review 完成后，才讨论下一道 production gate。
