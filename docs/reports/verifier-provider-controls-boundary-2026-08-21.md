# Verifier provider controls boundary — 2026-08-21

> 2026-08-22 更新：原生 OpenAI Responses 路径的宿主控制和 broker 证明已完成开发；当前 ChatGPT Responses、
> DeepSeek completions 和 Claude CLI 路由仍保持 fail closed。见
> [openai-responses-provider-controls-2026-08-22.md](openai-responses-provider-controls-2026-08-22.md)。

## 结论

独立 thesis-impact verifier 现在必须声明并绑定五项 provider 控制：JSON Schema、输入 token 上限、输出 token 上限、总 token 上限和费用上限。OpenClaw 2026.7.1 的公开 `api.runtime.llm.complete` 不能执行完整合同，因此 broker 会在调用模型前返回 `REQUIRED_CONTROLS_UNAVAILABLE`。这条路径不会再把 prompt-only JSON 和事后 telemetry 检查当作硬控制。

普通 completion 没有 `requiredControls`，继续沿用原有 broker 路径。改动只关闭独立 verifier 的不安全降级，不影响一般研究任务。

## 本轮确认的宿主边界

本机安装的 OpenClaw 版本是 2026.7.1。公开 SDK 的 `LlmCompleteParams` 没有 JSON Schema、输入 token 上限、总 token 上限或费用上限；实现传给 simple completion 的控制字段只有 `maxTokens`、`temperature` 和 abort signal。额外塞入未声明字段不会传到 provider。

因此，本轮没有把以下能力标成“已支持”:

- prompt 指示模型返回 JSON；
- provider 返回后再做 JSON 解析；
- provider 返回后再比较 tokens 或费用；
- 只传 `maxTokens`，却把它外推成输入、总量或费用硬上限。

## 实现

- broker 协议增加可选的 closed `requiredControls` 对象；JSON Schema 文档和 SHA-256 hash 一起进入认证帧、请求 hash 和幂等身份。
- Python adapter 只对带 `producer_family` 的独立 verifier 路由生成该合同。普通 model work 不带此字段。
- controlled invocation 的 identity 额外绑定整个控制合同 hash，避免与旧的无控制 invocation 共用同一身份。
- broker 0.1.0-spike.2 对当前宿主能力 fail closed，并在任何 host completion 前返回 `REQUIRED_CONTROLS_UNAVAILABLE`。
- verifier 的 package resource 与根目录 canonical contract 由测试逐字解析对比，防止 schema 漂移。

## 验证口径

Node broker 测试证明：合同字段闭合、schema hash 必须一致、输出上限必须和 `maxTokens` 一致，控制不可用时 host call 计数保持为 0。Python adapter 测试证明：一般研究请求不携带控制合同；独立 verifier 请求携带完整预算和 schema；真实 Node UDS 路径把 controlled verifier 变成失败的 `ResultEnvelope`，不伪造成功。

## 后续门槛

在 OpenClaw 提供并实际透传 provider-side structured output、输入/总 token 与费用控制前，不再对 DeepSeek、Claude Fable 或新的非 OpenAI verifier 候选做付费校准。宿主能力补齐后，先用 fake-provider transport 证明每项控制确实进入 provider request，再恢复一条有费用上限的 live case。
