# Thesis impact 模型执行器交付

日期：2026-08-21

## 结果

ResearchPlan closure 生成的 assessment/verifier WorkOrder 现在可以走现有模型执行边界：

`Scheduler claim → ModelRouter route → OpenClawModelAdapter → ModelInvocation → Usage/Cost → Scheduler complete`

新 `ThesisImpactModelWorker` 不拥有研究状态，也不能自建 WorkOrder。它只接受 coordinator 生成、Scheduler authority
已经冻结的 exact WorkOrder，并校验 phase、capability、input refs、side effects 和 WorkOrder hash。新 runtime 依次
推进 assessment 和 verifier；成功结果会交回现有 `ResearchPlanThesisImpactCoordinator` 写 append-only impact
authority，不会直接修改 ThesisVersion。

## 权威边界

- assessment 失败重试时，ModelRouter 记录 exact prior decision；重试次数由 Scheduler policy 限制。
- verifier 的 producer model family 从已持久化 assessment invocation 读取，不接受 caller 自报；routing policy 对
  `verify` 强制 family independence。
- broker 返回后，worker 先把 ModelInvocation、provider usage 和 cost 写入 Core authority，再处理 Scheduler 结果。
- 成功输出必须是 strict JSON，并通过 phase-specific closed contract、exact ref/hash 和 Thesis mechanism 检查；检查
  完成前不能写 formal success。
- 错误输出在尚有 attempt 时记为 retryable；最后一次失败记为 formal failed，避免 Scheduler exhausted 后没有可读
  formal result。
- coordinator 可以从 formal ResultEnvelope 的 invocation ref 读取已经持久化的 producer/verifier invocation，运行方
  不需要再传一份可漂移的 invocation payload。

## Recorded broker canary

隔离测试使用本机 Unix socket fake broker，不访问外部网络，也不调用真实或付费模型。完整序列为：

1. assessment 第一次返回字段不合约的 JSON；worker 记录 invocation、usage 和 cost，拒绝输出并进入 retry；
2. 第二次 assessment 返回与 exact Claim/Thesis 绑定的 `supports`；Scheduler 形成 formal success，impact authority
   才写 assessment；
3. verifier 自动排除 assessment 的 model family，选择另一 family，读取 exact assessment/Claim/Thesis 后返回 pass；
4. runtime 重放同一 plan，不再连接 broker，也不增加 invocation、assessment、verification、usage 或 cost。

验收结果：三次 broker 请求对应三条 ModelInvocation、三条 UsageEntry 和三条 CostEntry；第一次错误输出对应零条
assessment；最终只有一条 assessment 和一条 verification；Thesis current pointer 不变；人工 review decision 为 0；
Core、Scheduler 和 ModelRouter SQLite `integrity_check` 均为 `ok`。测试里的 token 和 0.001 美元单次 cost 是 recorded
telemetry，只用于验证入账，不代表真实模型价格或质量。

## 验证

- 新 routed worker canary：1/1 通过；
- bounded retry unit：1/1 通过；
- `tests.test_thesis_impact_control` 原有及新增 control canary：4/4 通过；
- `tests.test_thesis_impact`：7/7 通过；
- `tests.test_model_router + tests.test_openclaw_model_adapter`：25/25 通过；
- `tests.test_research_plan_closure`：11/11 通过；
- `py_compile`、`compileall` 与 `git diff --check` 通过；
- wheel 在干净 Python 3.13 venv 安装后 `pip check` 通过，新 runtime/worker 可从包根导入。

本轮没有重跑 Python 全量测试或真实 OpenClaw broker Node 测试；验证范围是 thesis impact、closure、ModelRouter、
OpenClaw Python adapter、recorded broker canary 和安装包。

## 边界与下一步

- 本轮没有部署 live，没有访问真实 source，也没有调用真实或付费模型。
- 当前只证明路由、调用归因、计量、合约拒绝、独立复核和重放；没有证明模型能给出合格的投资判断。
- 下一步须先取得单独付费调用授权，再用隔离 authority 跑一条真实模型 canary，检查实际 token、成本、输出质量和
  升级原因。即使通过，结果仍只形成 pre-commit thesis impact，不自动修改 ThesisVersion。
