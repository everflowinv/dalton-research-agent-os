# GPT-5.6 Sol assessment producer phase pin

日期：2026-08-22
状态：开发候选完成；未激活 live policy，未重启 gateway

## 结果

新增不可变 `model-routing-policy-version:dalton-openclaw-assessment:1`，只允许
`profile:gpt-5-6-sol`。`upgrade_openclaw_broker_catalog` 会把该 policy 与 shared policy、Gemini verifier
policy 一起写入 ModelRouter authority。

`ThesisImpactModelWorker` 新增 `assessment_routing_policy_ref`。新 assessment 在 claim lease 和 broker 调用前读取
该 exact policy，并要求 `allowed_profile_ids` 恰好只有一个；generic shared policy 或缺失 policy 均 fail closed。
已经存在 formal result 的 WorkOrder 仍可按 authority replay，不依赖当前 policy 是否还在 catalog 中。

## 验证

- deployment test 证明 assessment policy 只包含 `profile:gpt-5-6-sol`，fresh install 和重复 catalog upgrade 均可重放；
- recorded E2E 同时绑定 assessment 与 verifier phase policy：assessment route 使用单 profile assessment policy，
  verifier route 使用单 profile verifier policy，并继续执行 producer-family independence；
- shared two-profile policy 作为 assessment phase policy 会在新调用前被拒绝；
- 相关 32 个快速专项、assessment/verifier recorded E2E、day-budget E2E、broker 22/22、hermetic replay、
  compileall 和 wheel/sdist build 均通过。

## 边界

本批 pin 的是 assessment 模型路由，不是 assessment reasoning level。当前 GPT-5.6 Sol broker profile 没有
provider-controls thinking proof，因此不能把 model pin 写成 thinking 已受控。production worker 何时传入该 policy
ref，仍属于后续 activation；本批没有改 OpenClaw config、live route、cron 或 ThesisVersion。
