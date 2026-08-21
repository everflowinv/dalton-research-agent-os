# Thesis impact 模型调用崩溃恢复

日期：2026-08-21

## 结果

模型已经返回、Core 已写入 ModelInvocation、UsageEntry 和 CostEntry，但 worker 尚未执行
`Scheduler.complete` 时，进程崩溃可能让下一次 Scheduler attempt 重复付费。本切片把恢复规则固定为：

`unaccepted route → exact invocation → broker replayOnly → durable completion → current Scheduler attempt`

worker 只恢复尚无 Scheduler ResultEnvelope 的最新 route。已经被 Scheduler 接受为 retryable 的 attempt 会正常建立
下一条 route，不会被误判成进程崩溃。Scheduler lease 如果在首次 routing 前过期，ModelRouter 也允许第一个 route
从后续 attempt 开始；route lineage 仍必须单调前进。

## Broker 边界

OpenClaw broker 增加经过 HMAC 认证的可选 `replayOnly` 字段。它不属于 provider request identity，因此与原请求使用
同一个 request hash 和 invocation id：

- durable journal 已完成：返回原 completion，标记 `duplicate`；
- journal 仍是 pending：返回 `IDEMPOTENCY_INDETERMINATE`，不自动重放；
- journal miss：返回 `IDEMPOTENCY_MISS`，不创建 claim，也不调用 host 模型；
- invocation id 对应不同 provider request：继续 fail closed 为 conflict。

worker 收到 replay-only miss 后生成 terminal `MODEL_RECOVERY_MISS`，不登记虚假的 model invocation，也不估算一笔
没有发生的 provider cost。命中 journal 时，如果 crash 前的 invocation 已存在，worker 重验稳定身份字段并复用原
记录；Usage/Cost 写入使用原 invocation 的幂等键。

## 隔离崩溃测试

测试在 model accounting 完成后、Scheduler complete 前注入一次崩溃，再把冻结时钟推进 31 秒使 lease 过期。
第二次运行领取 attempt 2，复用 attempt 1 的 route，并从 recorded broker 取 duplicate completion。结果为：

- 2 次 Unix socket 请求，只有 1 次模拟 provider call；
- ModelRouteDecision、ModelInvocation、UsageEntry、CostEntry 各 1 条；
- Scheduler formal success 的 attempt number 为 2；
- recovered result 标记 `broker_request_mode=replay_only`；
- assessment 可以继续写入 impact authority；
- Core、Scheduler、ModelRouter 的 SQLite `integrity_check` 均为 `ok`。

## 验证与边界

- OpenClaw broker：16/16；
- Python adapter：16/16；
- ModelRouter：11/11；
- thesis impact control：6/6；
- `py_compile` 与 `git diff --check` 通过。

本轮没有调用真实或付费模型，没有访问网络，没有部署 live，也没有重跑 Python 全量测试。下一步仍需单独取得付费
调用授权，才能在隔离 authority 中运行第一条真实模型 canary。
