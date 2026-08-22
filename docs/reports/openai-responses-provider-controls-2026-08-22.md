# OpenAI Responses provider controls — 2026-08-22

## 结论

OpenClaw 2026.7.1 的宿主补丁和 broker 0.1.0-spike.3 已完成开发与离线验证。独立 verifier 只有在 exact profile 使用
原生 `openai/openai-responses` transport、宿主声明匹配能力、并配置当前有效的可信 rate card 时，才会进入 provider
调用。其他 transport 会在 provider 调用前拒绝，普通 completion 不受影响。

本机现有 `openai/gpt-5.6-*` 使用 `openai-chatgpt-responses`，DeepSeek 使用 `openai-completions`，Claude 使用 CLI
gateway；三类路由都不满足该边界。现有 broker profile 没有启用 `providerControls`，所以 live 独立 verifier 仍保持
fail closed。本轮没有增加凭据、没有改 profile、没有付费模型调用。

## 宿主控制

受控请求使用闭合的 `openai-responses-input-count-v1` 合同：

- 把 hash-bound JSON Schema 注入 Responses API 的 strict `text.format`；
- 用相同的可计数字段调用 `/responses/input_tokens`，并关闭 SDK retry；
- provider 返回的 input tokens 超过上限时，不发起 `/responses`；
- `input tokens + 完整 output reserve` 超过 total 上限时，不发起 `/responses`；
- 用 exact model、default service tier 和有有效期的可信 rate card 做最坏成本预留；费用超限时不发起 `/responses`；
- 只接受 `provider=openai` 且 `api=openai-responses`。ChatGPT Responses、OpenAI-compatible completions、CLI gateway
  和其他 provider 均在 transport preflight 阶段拒绝；
- 宿主返回 schema、rate card、token reserve 和费用预留证明。broker 重新核对证明、实际 usage 和 cost telemetry。

费用计算使用 12 位定点十进制和 `BigInt`，输入侧按 input、cached input、cache write 三种单价的最高值预留，输出侧按
全部允许 tokens 预留。rate card 由 broker profile 提供，client 不能提交或覆盖价格；过期、尚未生效、model 不符、
service tier 不符或价格非正数都会失败。

## 验证

- OpenClaw patch runner `--check`：6/6 目标已应用；全部 OpenClaw patches 版本匹配 2026.7.1。
- fake OpenAI transport：准入路径为 1 次 input count + 1 次 fake model request；输入超限和费用超限路径均为
  1 次 input count + 0 次 model request。
- fake ChatGPT transport：runtime preflight 后 provider request 为 0。
- broker：20/20 Node tests 通过，覆盖能力和 transport 声明、可信 rate card、证明绑定、usage/cost 越界和旧幂等边界。
- fake provider 只监听 loopback 并在捕获 payload 后返回错误；`paidCalls=0`。

## 官方依据

- OpenAI Structured Outputs：<https://developers.openai.com/api/docs/guides/structured-outputs>
- OpenAI Responses per-run spending controller：
  <https://developers.openai.com/cookbook/articles/per_run_spending_controller_responses_api>

官方接口没有直接的 `max_input_tokens` 或 `max_cost` 请求字段。这里使用官方 input token count endpoint、严格输出
Schema、冻结 rate card 和最坏输出预留，在生成前完成 admission；不把事后 telemetry 当成硬费用控制。

## 部署边界

宿主 patch 已写入本机 managed OpenClaw install，并登记到 workspace `patch/apply_all.sh`。gateway 在安全重启前
仍使用此前加载的 runtime；broker 代码由现有 path install 直接指向本仓库，但运行中服务同样要等 gateway 重启后
加载 spike.3。没有兼容 profile 和原生 OpenAI API credential 之前，不应添加 `providerControls` 配置或运行付费
canary。
