# 有限回答刷新 S5A v0.2

## 结果

S5A 已进入 development candidate。`AnswerSufficiencyPolicyVersion 0.2` 可以为 stale-only 的已回答问题批准一次
`answer_after_refresh`，但只能复用一个已由人类创建、尚未启动的单轮 Bounded Planner Loop。Core 不接受模型临时生成的
ProbeTemplate、参数、权限或预算，也不把这条路径扩展成 ad-hoc research。

本切片没有部署 live `:8793`，没有打开 production pointer，没有调用真实 connector 或付费模型。Cockpit 只显示
`answer_after_refresh` 的只读判定和 exact plan；页面没有 dispatch endpoint 或执行按钮。

## 准入边界

只有原路由失败原因恰好为 `stale_evidence` 时，Core 才检查有限刷新。以下条件必须同时成立：

- 问题与已入库、状态为 `answered` 的 ResearchQuestion 完全一致；
- policy 绑定 exact ProbeTemplate ref/hash，模板为 read-only，并使用固定 output contract 和 verifier；
- 只有一个尚无 round、无 terminal 的匹配 loop；
- loop 只有一个 coverage item、一个 template binding 和一轮预算；
- template cost 同时不超过 loop budget 和独立的 policy 日预算；
- policy、question、loop、template、参数、过期 Evidence 和预算快照全部进入 `AnswerRefreshPlan` 的 hash。

没有模板、匹配不唯一或日预算不足时，路由分别返回 `refresh_probe_unavailable`、
`refresh_probe_ambiguous` 或 `refresh_budget_exhausted`，并回到 `recommend_agenda_item`。ad-hoc research 继续强制
`enabled=false / max_cost_units=0 / max_rounds=0`。

## 预算与执行

`AnswerRefreshControlPlane` 要求认证 `human:*` actor。它先在 Core 内追加不可变的日预算 reservation，再调用既有
`BoundedPlannerControlPlane` 生成 proposal、执行 Core admission，并复用原 Scheduler WorkOrder 和 Observability workflow。
它不执行 connector，也不另建 queue 或 DAG。

reservation、dispatch 和 outcome 都有 append-only receipt。decision、policy、loop 或 template hash 漂移会拒绝；同一
decision 重放只返回原 reservation/WorkOrder。测试在 reservation 持久化后注入崩溃，重试仍只产生一条 reservation、
一条 PlanRound 和一个刷新 WorkOrder。

## CandidateStaging gate

成功 WorkOrder 的 `matches=[]` 会形成来源级 `not_found_in_scope`，随后进入
`coverage_complete_unobservable_candidate`；这不是负面 Claim。

有命中时，finalize 必须先重读独立 CandidateStaging authority，并校验：

- CandidateEvidence 与 CandidateClaim 的 exact ref/hash 和相互绑定；
- exact candidate stage request hash；
- CandidateEvidence 的 SourceEnvelope ref/hash 与本次 ResultEnvelope 输出一致；
- outcome receipt 同时冻结 ResultEnvelope、SourceEnvelope 和 staging receipt 的 exact binding。

缺少或漂移任一绑定时，系统会在写 ResearchOutcome 之前停止。有限刷新不会直接写正式 Evidence、Claim 或 Thesis；
正式 promotion 仍由原 human review gate 决定。

## Cockpit

问答页现在区分三种状态：`answer_direct`、`answer_after_refresh` 和 `recommend_agenda_item`。有限刷新卡只显示 template、
loop 和预算，并明确标注“尚未 dispatch”。本切片没有把 human dispatch 写权限交给 `dashboard-control`。

## 验证

- Answer Routing、隔离 ACN canary、Cockpit、writer 权限、Bounded Planner、Backlog、ResearchPlan、Industry、
  CandidateStaging、Coverage Admission、Contracts、Packaging、Scheduler 与 Observability 关联矩阵：153/153；
- 最后两项 crash-hardening 写入后，Answer Routing、Contracts 与 Packaging 最终超集：24/24；
- human-only dispatch、日预算耗尽、重复 dispatch、reservation 后崩溃恢复、非法 verifier、observed 结果绕过
  CandidateStaging 等敌对 case 均通过；
- 无命中 finalize 前后正式 Evidence/Claim/Thesis 计数不变；
- Cockpit JavaScript、Python compileall、全部 JSON schema 解析与 `git diff --check` 通过；
- 本机 Python 3.13/3.14 当前都没有 `build` 模块，因此没有本地重跑 sdist/wheel。

## 下一步

下一切片先补真实 connector-to-CandidateStaging 的隔离 canary，再决定是否在 Cockpit 增加显式 human dispatch。独立
ad-hoc research 仍留在后续切片，不能复用本切片的 stale-only route 扩大范围。
