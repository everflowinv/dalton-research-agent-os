# LLM Research Planner 模型扩展校准 v0.2

日期：2026-08-23  
状态：development calibration；未部署 live production

## 裁决

`profile:qwen-deepseek-v4-flash-0731` 取代 Qwen 3.8 Max，成为 development-only Planner 首选。
它在两轮 frozen corpus 上累计 30/30，安全项 20/20；两轮完整成本合计 USD 0.00960668，单 case
中位延迟分别为 2.018s 和 2.002s。旧 policy v1 保留不改，新 policy v2 通过
`prior_version_ref` 追加并固定新 profile。

Ox Alpha、DeepSeek V4 Pro 0813 和 Gemini 3.7 Flash 也都连续两轮通过。Ox Alpha 是 stealth route，
模型身份和长期价格不稳定；Gemini 3.7 同时承担独立 verifier lane；V4 Pro 在本任务没有表现出足以抵消
额外成本的优势，因此三者都不替代首选。

Grok 4.6 未通过 frozen WorkOrder：5/15 次的 provider-reported output telemetry 超过 800-token 上限，
Core 以 `PROVIDER_BUDGET_EXCEEDED` 拒绝。这里不放宽预算重算成绩，否则不再是同一合同下的横评。

GLM 5.3 已完成新路由发现和 calibration-only 准入，但首轮暴露了 broker thinking 漂移：Planner 请求没有
profile-level thinking pin，因而继承宿主 `xhigh`；ZAI 当前只接受到 `high`，15 次均在供应商调用前失败。
独立 `high` smoke 已返回预期结果。broker 已增加版本化 `thinkingLevel` 字段并把 GLM profile 固定为
`high`；完整 corpus 要在 safe gateway restart 生效后重跑，不能把这次基础设施失败当成模型得分。

## Thinking 口径

此前校准 runner 没有发送 thinking 参数。OpenClaw 的宿主默认值是 `xhigh`，所以支持该档位的
Qwen DeepSeek V4 Flash/Pro、Grok 4.6 和 OpenRouter Ox Alpha 在本轮继承 `xhigh`。普通 completion
telemetry 不回显供应商最终执行档位，因此报告只把它称为宿主请求口径，不声称供应商端已独立证明。

Gemini 3.7 Flash 的受控 broker profile 已明确把 `thinkingLevel` 固定为 `low`。GLM 5.3 只支持
`off/minimal/low/medium/high`；后续固定为 `high`。

本轮修正后，broker profile 可冻结 `off/minimal/low/medium/high/xhigh/adaptive/max` 中的一个值并传给
host completion；若它与 provider-control thinking 冲突，配置在启动时 fail closed。这样模型横评不再依赖
OpenClaw 全局默认值。

## Frozen corpus 结果

所有正式 run 使用同一 15-case corpus：
`124d3cf58f32196f399477d665f0f6a8f58dbdc0936d816ce06e7094f6e8fe1e`。其中 10 个 case 是
safety-critical；统一上限为 12,000 input tokens、800 output tokens、300 秒。

| 模型 | Thinking 请求口径 | 两轮 Action | 两轮 Safety | 完整 run 成本 | 单 case 中位延迟 | 裁决 |
|---|---|---:|---:|---:|---:|---|
| Qwen DeepSeek V4 Flash 0731 | inherited `xhigh`；已改为 profile pin | 30/30 | 20/20 | USD 0.00960668 | 2.018s / 2.002s | development 首选 |
| OpenRouter Ox Alpha | inherited `xhigh`；已改为 profile pin | 30/30 | 20/20 | USD 0（当前 rate card） | 4.043s / 4.178s | 通过；stealth route 不入选 |
| Qwen DeepSeek V4 Pro 0813 | inherited `xhigh`；已改为 profile pin | 30/30 | 20/20 | USD 0.029532448 | 2.620s / 2.499s | 通过；不入选 |
| Gemini 3.7 Flash | profile/provider control `low` | 30/30 | 20/20 | USD 0.06202950 | 1.582s / 1.815s | 通过；保留 verifier 独立性 |
| Grok 4.6 | inherited `xhigh`；已改为 profile pin | 10/15 | 7/10 | USD 0.126796 | 10.718s | fail：5 次超 WorkOrder output budget |
| ZAI GLM 5.3 | 首轮误继承 `xhigh`；修正为 `high` | 基础设施失败 | 基础设施失败 | 无 observed usage | 0.033s（pre-provider fail） | 完整重跑待 safe restart |

Ox Alpha 的 USD 0 只反映当前 catalog rate card，不代表该模型长期免费。GLM 失败 run 的
`spent_or_reserved_usd=75` 是缺失 telemetry 时每 case USD 5 的风险准备金，不是实际账单。

## 即插即用边界

新模型不需要修改 Planner、proposal contract、Core gate 或 scorer。只要 OpenClaw catalog 已有路由，operator
把 exact provider/model、额度、timeout 和 thinking 加入 broker allowlist，calibration runner 就能派生一个
calibration-only research profile。该派生对象不能自动进入 production catalog。

本轮 GLM 说明“发现新模型”和“受治理地使用新模型”是两件事：前者可以动态完成；后者仍需要明确准入 exact route
和 thinking。这个显式 gate 是安全边界，不应改成任意模型自动拥有 production research 权限。

## 实现与验证

- calibration runner 支持未知但已 brokered 的 profile 派生 calibration-only research capability；静态非 research
  profile 仍拒绝。
- Qwen V4 Pro route identity 修正为 `deepseek-v4-pro-0813`，避免 endpoint/profile 漂移。
- broker profile 新增 `thinkingLevel`，并拒绝未知档位或与 provider-control thinking 冲突的配置。
- development Planner policy 从 immutable v1 追加到 v2；v1 继续绑定 Qwen 3.8 Max，v2 绑定
  Qwen DeepSeek V4 Flash 0731。
- Python 专项 22/22、全仓 Python 750/750、Node broker 24/24、`compileall` 与 `git diff --check`
  已通过；sdist 和 wheel 构建成功。

本次仍未启用 live Planner worker 或 production routing，也没有改变 verifier policy。GLM 完整 frozen-corpus 重跑是
唯一待补的模型项；它不影响 DeepSeek V4 Flash 的 development selection。
