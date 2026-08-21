# Claim → thesis 影响判断与独立复核

日期：2026-08-20
状态：开发候选；隔离 recorded-model canary 已通过；未部署 live；未调用真实付费模型

## 本轮交付

新增 `ThesisImpactAuthority`，把一条 exact formal ClaimVersion 与一条 exact current ThesisVersion 交给模型判断：

- 输出只允许 `supports`、`weakens`、`no_change`、`insufficient`；
- 输出必须回写 ClaimVersion/ThesisVersion 的 exact ref、hash 和原 thesis `mechanism`；
- `insufficient` 必须给出具体 follow-up question，其他结果不得夹带 follow-up；
- producer 的 Core ModelInvocation、Scheduler WorkOrder、formal ResultEnvelope 必须绑定同一组 input refs；
- verifier 必须读取 exact assessment ref/hash，且通过 active governance policy 的不同 model family 检查；
- 判断和复核都是 append-only authority。该模块不调用 `stage_change` 或 `commit`，不会生成新 ThesisVersion，
  也不会改 current thesis pointer。

没有 current thesis 时，`route_claim` 不创建占位 thesis，而是把 formal Claim 变成一条稳定、幂等的
ResearchQuestionBacklog 问题，供后续议程继续研究。

## 验证

- `tests.test_thesis_impact`：6/6 通过；
- Core、Scheduler、ResearchQuestionBacklog、contracts 相关回归：75/75 通过；
- happy-path recorded canary：1 条 assessment、1 条不同 model family 的 pass verification、1 条原
  ThesisVersion；Core 与 Scheduler `PRAGMA integrity_check` 均为 `ok`；
- 覆盖 exact hash 不符、非法 follow-up、同 model family 复核、reject 留痕但不可消费、直接 SQL 写入拒绝、
  缺失 thesis 自动生成且重放不重复的 Backlog 问题；
- 两份新 JSON Schema 已通过解析，`git diff --check` 通过。

## 尚未完成

- ResearchPlan closure 还没有自动创建 assessment/verifier WorkOrder；
- 本轮模型输出是隔离 recorded ResultEnvelope，不代表真实模型质量；
- 当前 ThesisVersion 没有一等公民 company/driver ref，首版只能把既有 `mechanism` 当作 driver statement；
- `eligible_assessment` 只是未来 thesis updater 的输入，不是 thesis commit 授权。

下一切片只接 closure → route → 两个受预算约束 WorkOrder，并跑一条隔离真实模型 canary；不顺带扩建 Model IR、
first-class driver、Reflection、dashboard 或 live 部署。
