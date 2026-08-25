# 有限回答刷新 S5C：Cockpit 显式 human dispatch v0.4

## 结果

development Cockpit 现可由登录人明确确认一次已获准的 stale-only 有限刷新。新端点是
`POST /v1/answer/refresh/dispatch`。它不会接受或生成 ProbeTemplate、Bounded Planner Loop、connector plan、参数或
预算，只接受上一轮只读 route 已返回的 exact subject、question、RouteDecision ref/hash 和 as-of。

本切片没有部署 live `:8793`，没有打开 production pointer，没有运行 connector 或付费模型，也没有写正式
Evidence、Claim 或 Thesis。独立 ad-hoc research 继续关闭。

## Human authority 与重验

Cockpit 继续复用既有 Tailscale identity、SameSite session 和 CSRF。服务端不接受浏览器自报 actor，而是从获准的
Tailscale login 派生稳定的 `human:tailscale-*` subject，再为这一笔 RPC 临时签发 owner-only human writer principal。
调用结束后临时 principal 立即移除。

writer 新增 `dispatch_answer_refresh` human-governance operation；`dashboard-control` 没有这个 operation，仍只有
`answer_subjects` 和 `route_answer` 两个只读 answer RPC。Core 重新执行以下检查后才 dispatch：

- as-of 必须属于当前 UTC 日；
- exact subject 与 question 必须重算出同一个 RouteDecision ref/hash；
- route 仍须为 `answer_after_refresh`，且 exact policy、loop、template、参数、stale Evidence 与日预算没有漂移；
- 同一 decision、loop 或 actor 的已有 reservation/dispatch 必须按原 receipt 重放，不能另建一笔。

因此，浏览器篡改 template、loop、connector plan 或预算会先被 closed request shape 拒绝；只篡改 ref/hash/as-of 也会在
Core 重验时拒绝。

## Core 与 Scheduler 分库接线

live writer 持有 Core SQLite 和独立 Scheduler SQLite。S5A 的隔离测试把两者放在同一连接，原
`BoundedPlannerControlPlane` 也据此直接从 Core connection 读取 `scheduler_work_orders`；这条假设不适用于真实 writer。

本切片给 Scheduler 增加只读 `work_order_authority`，会重验保存的 WorkOrder JSON、contract normalization 与 SHA-256。
Bounded Planner 继续要求自身 authority 与 Observability 共用 Core connection，但 WorkOrder 与 ResultEnvelope 改从其
绑定的 exact Scheduler 读取。writer 因此可以在单进程单写者边界内组合 Core authority、独立 Scheduler 和原
`AnswerRefreshControlPlane`。

enqueue 的 WorkOrder id 和 idempotency key 仍由 exact proposal 确定。即使进程在 enqueue 后、Core round 持久化前崩溃，
重放也只会命中同一 Scheduler WorkOrder，再补齐原 Workflow/round；不会排第二条任务或重复预留刷新预算。

## UI

问答卡只有在 route 为 `answer_after_refresh` 时显示“确认并运行有限刷新”。确认框明确说明 Core 会重算 exact decision。
成功后页面只显示已入队的 WorkOrder ref，不把“已排队”写成“已刷新”或“已回答”。CandidateStaging promotion 与正式
Claim review 仍走原 gate。

## 验证

专项与邻接回归共 197/197 通过：

- Answer Routing 14/14，覆盖 human gate、同 decision 幂等、独立 Scheduler 入队、预算、crash recovery、
  CandidateStaging 与正式 authority 零写入；
- Scheduler/Bounded Planner/Cockpit/writer/service/contracts/packaging/S5B canary/S4.1 canary 75/75；
- Doctrine、StatementSnapshot、TranscriptPolish、ResearchPlan、Backlog、Industry、CandidateStaging、Coverage Admission
  与 Observability 108/108；
- 其中分库专项覆盖 enqueue、enqueue 后崩溃重放、exact WorkOrder authority 与 no-match outcome；
- Cockpit 专项覆盖 CSRF、closed request shape、Tailscale login → hashed human actor 与 exact governance payload；
- writer 权限专项确认 dashboard/worker principal 和伪造 actor 均不能 dispatch，human principal 只能进入原 control plane。

Cockpit JavaScript、Python compileall、全部 JSON contracts、packaging manifest 与 `git diff --check` 通过。本机 Python
3.13 仍没有可执行的 `build.__main__`，因此没有生成 sdist/wheel；远端 CI 状态以提交交付记录为准。
