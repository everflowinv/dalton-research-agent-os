# Thesis-impact 多模型校准简报

日期：2026-08-22  
状态：20 个模型完成 12 案例评分；生产自动化仍锁定

## 结论

23 个 OpenClaw 模型已进入 Dalton broker catalog，其中 20 个通过 smoke 并完成 12 个冻结案例评分。GPT-5.6
Terra、Claude Opus 5、Gemini 3.1 Pro Preview、Qwen 3.8 Max 均为 12/12；Qwen 3.8 Max 和 GPT-5.6
Terra 分别以 `$0.02296635` 和 `$0.028664` 成为两个成本最低的满分模型，进入下一轮主候选与独立候选。

这轮不能解锁生产自动化。冻结语料只有 12 个案例，而 scorer 要求至少 30 个 seeded case；真实生产仍要求
`provider-controlled-v1`。本轮使用的 `calibration-posthoc-v1` 只能校准，不能冒充 provider control proof。

脱敏结果见
[`docs/review-evidence/thesis-impact-multi-model-calibration-summary-2026-08-22.json`](../review-evidence/thesis-impact-multi-model-calibration-summary-2026-08-22.json)。

## 结果分层

满分模型：

- `profile:qwen3-8-max`：12/12，检出率 100%，误报率 0%，费用 `$0.02296635`。
- `profile:gpt-5-6-terra`：12/12，检出率 100%，误报率 0%，费用 `$0.028664`。
- `profile:gemini-3-1-pro-preview`：12/12，检出率 100%，误报率 0%，费用 `$0.083244`。
- `profile:claude-opus-5`：12/12，检出率 100%，误报率 0%，费用 `$0.587025`。

11/12 模型：

- `profile:grok-4-6`：检出率 100%，误报率 20%，费用 `$0.225386`。
- `profile:grok-4-3`：检出率 85.71%，误报率 0%，费用 `$0.03383045`。
- `profile:grok-4-20-beta-reasoning`：检出率 85.71%，误报率 0%，费用 `$0.08002045`。

其余模型为 7/12 至 10/12。完整 20 个结果、schema 有效率、漏检和费用都在脱敏 JSON 中；没有人工修补模型输出。

## 调用与费用

- 主矩阵：20 个模型，203 次 provider 调用成功，198 个 schema 有效输出；费用 `$1.109683119`。
- 高预算重跑：Fable 和三个 Grok 共 48 次调用成功，46 个 schema 有效输出；费用 `$1.62512265`。
- 最终评分覆盖 20×12=240 个模型-案例组合，其中 233 个输出通过 schema。
- 全量 12 案例阶段费用 `$2.734805769`；连同 smoke，整个接入和校准阶段已知费用 `$3.180277816`。
- 所有费用都有 provider telemetry；未知费用预留为 `$0`。

主矩阵中 Fable 被预估费用门槛挡下，Grok 4.6、Grok Build 和 Grok 4.20 reasoning 则因 4,000 token
输出上限中断。把单例预算提高到 `$1.00`、输出上限提高到 16,000 后，四个模型都完成 12 个案例。这说明前述失败是
校准预算配置问题，不是 provider 不可用。Fable 最终有 10/12 个 schema 有效输出；三个 Grok 分别为 12/12、
12/12、12/12。

## 未进入全量评分的三个模型

- `profile:claude-sonnet-5`：smoke 返回真实响应，但 provider 输入 token 遥测超过 10,000 token WorkOrder 上限。
- `profile:gemini-3-7-flash`：两次 smoke 均返回 `INVALID_HOST_RESULT`，没有有效文本。
- `profile:gemini-flash-latest`：两次 smoke 均返回 `INVALID_HOST_RESULT`，没有有效文本。

这三个 profile 仍在 catalog 中，但不能进入当前候选名单。

## 下一阶段

1. 把冻结语料从 12 个扩到至少 30 个，增加边界样本、混合错误、格式对抗和更难的 follow-up quality 案例。
2. 用相同 corpus hash 重跑 Qwen 3.8 Max、GPT-5.6 Terra、Gemini 3.1 Pro、Claude Opus；其他模型只在成本或分歧分析需要时重跑。
3. 只有模型同时满足准确率、检出率、误报、高严重度漏检和 provider control proof，才生成生产 routing policy。

本轮没有部署 live verifier，没有改生产路由，也没有放宽 `provider-controlled-v1`。
