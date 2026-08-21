# ResearchPlan → thesis impact 控制面交付

日期：2026-08-21

## 结果

ResearchPlan closure 产出的正式 Claim 现在会自动进入 thesis impact 路由，不需要 owner 逐条审阅。系统先重读
exact plan、Backlog start/answer、Claim 和当前 Thesis，再生成两个 immutable Scheduler WorkOrder：

- assessment：输入 exact ClaimVersion 与当前 ThesisVersion，预算上限为 3,500 tokens、0.25 美元、120 秒；
- verifier：输入 exact assessment、ClaimVersion 与 ThesisVersion，预算上限为 14,000 tokens（10,000 输入、4,000 输出）、0.25 美元、120 秒。

两个 prompt 都把 authority JSON 标为不可信数据，要求模型只返回 closed JSON contract。verifier 不能只看
assessment 摘要；它必须重新读取 Claim 和 Thesis。两个 WorkOrder 都声明零 side effect。

## 状态与重放

新 coordinator 不建第二套状态表。它把现有 authority 当作 durable checkpoint：

- Backlog answer binding 确定 exact formal Claim；
- Scheduler WorkOrder、formal ResultEnvelope 确定 assessment/verifier 是否完成；
- append-only impact assessment 与 verification 保存模型判断和独立复核；
- deterministic WorkOrder id 和 idempotency key 让 crash/replay 收敛到同一记录。

通过的 `supports / weakens / no_change` 只形成已验证 pre-commit 判断，不改 ThesisVersion。通过的
`insufficient` 会把模型给出的 exact follow-up question 写入 Backlog；没有当前 thesis 时也只建题，不生成占位
thesis。verifier reject 会保留拒绝记录，但不会产生 eligible assessment。

## Recorded canary

端到端测试使用完整四节点 SEC Company Facts plan、policy-authorized promotion 和 closure，再以 recorded
ResultEnvelope 完成两个模型 WorkOrder。三个场景全部通过：

1. `supports → independent pass`：得到 eligible assessment，Thesis current pointer 和版本数不变；
2. 没有当前 thesis：只生成一条可重放 Backlog 问题，模型 WorkOrder 数不增加；
3. `insufficient → independent pass`：exact follow-up question 只写一次，重放返回 duplicate。

正常路径共保留原 plan 的四个 WorkOrder，加上 assessment/verifier 两个 WorkOrder；人工 review decision 为 0。
Core 与 Scheduler 的 SQLite `integrity_check` 都为 `ok`。

## 验证

- `tests.test_thesis_impact + tests.test_thesis_impact_control`：10/10 通过；
- `tests.test_research_plan_closure`：11/11 通过；
- wheel 在干净 Python 3.13 venv 安装后 `pip check` 通过，新 coordinator 和三个公开错误类型可从包根导入。

本轮没有重跑全量测试；验证范围是 impact authority、closure 接线、recorded canary 和安装包。

## 本轮修正

原 thesis impact verifier 只把 assessment ref 作为 Scheduler/ModelInvocation 输入。控制面接线时发现，这会让
verifier 在 authority 层无法证明自己重新读取了 Claim 与 Thesis。现在 verifier input refs 固定为
`[assessment_ref, claim_version_ref, thesis_version_ref]`；读取、记录、重验和测试均使用同一 closed binding。

## 边界与下一步

- 本轮没有部署 live，没有访问真实网络，也没有调用付费模型。
- coordinator 只创建和推进 WorkOrder，不自行执行模型。
- logical `thesis_ref` 仍由上游 coverage 映射提供；控制面会重读其当前版本并检查 Claim subject/company。
- 下一步把两个 WorkOrder 接入现有 ModelRouter/OpenClaw model worker，在隔离 authority 跑真实模型 canary，核对
  实际 token、成本、model-family independence 和输出质量；真实 canary 仍不得直接修改 thesis。
