# Thesis-impact verifier 真实校准结果

日期：2026-08-21
状态：DeepSeek 完成 12/12 但质量不合格；Claude Fable 首条即超预算并停止；没有候选模型获准上线

## 结论

当前两个非 OpenAI verifier 候选都不能用于 production。DeepSeek V4 Flash 能稳定返回 `0.2` 合同，但冻结评分只有
75%：7 个错误样本检出 5 个，检出率 71.4%；5 个正确样本误杀 1 个，误报率 20%；还漏掉 1 个高严重度错误。
这低于 90% 检出率和高严重度零漏检门槛。

Claude Fable 5 没进入内容评分。第一条调用的 provider telemetry 是 6,795 input / 958 output tokens，超过 WorkOrder
的 3,000 / 400 上限；实际费用 USD 0.11585，也超过单条 USD 0.04 admission cap。adapter 按约定 fail closed，runner
没有再跑后面 11 条。broker journal 中的原始文本还是“一个 JSON 对象 + 额外解释”，同时违反 exactly-one-JSON
合同。因此不提高预算重试，也不切 Claude Opus 5。

## DeepSeek 12/12

- 运行 commit：`4491ef0d2b0ffb8dd1df292afc019d43ddfc87a9`。
- 12 次调用全部得到可解析的严格 `0.2` 输出；总 usage 6,623 input / 2,059 output / 8,682 total tokens。
- 实际费用 USD 0.002816；低于 12 条单条 cap 合计 USD 0.12 和本轮 admission cap USD 0.25。
- 错误样本：
  - case 006 漏掉 claim hash 绑定错误，属于高严重度漏检；
  - case 011 漏掉无法关闭现金流缺口的模糊追问；
  - case 003 把正确的 `weakens` assessment 误报为 `impact_mismatch`。
- production consistency guard 还会拒绝 case 003、007、012 的输出：前两条把 assessment 自己的 impact 重复写成
  `expected_impact`，case 012 又报了 authority 已经排除的 `binding_mismatch`。冻结 scorer 没有事后改 gold 或改
  评分规则；这三条作为单独的 production-admissibility 审计记录。
- 完成后用相同 output dir 运行 resume，broker journal 的 SHA-256 和文件大小均未变化，新增 provider call 为 0。

## Claude Fable 失败与控制修复

- 运行 commit：`d50b3a7bc2ae0cc8ee3f54f4afb341e11f8262fe`。
- 第一条 invocation：`invocation:b8ae9bb56feba809cf859d8cf2c71676`。
- 实际 usage 6,795 input / 958 output / 7,753 total tokens，费用 USD 0.11585。
- 这说明当前 broker 的预算是 admission 和事后 telemetry gate，不是 provider-side 费用保证。broker 能传
  `maxTokens`，但不能在调用前阻止 host-side context 或 provider 输出超出 WorkOrder；因此不能把超出部分记成
  “未花费预算”。
- 旧 runner 在 adapter 返回 `PROVIDER_BUDGET_EXCEEDED` 后先抛错，导致失败 invocation 没写入本地 checkpoint。
  commit `8e0dfe564a69728b57ccc93392fd938b913b0f16` 已修复：先 fsync 失败 invocation、usage、费用和错误，再停止后续
  样本；失败记录不进入模型质量 coverage。
- 本次历史失败通过 `replayOnly` 补写，broker journal 的 hash 和大小都未变化，没有第二次 Claude 调用。

## 费用与边界

- DeepSeek：USD 0.002816。
- Claude Fable：USD 0.11585。
- 本轮新增实际费用：USD 0.118666。
- Gate 2 此前 accounted + uncertain reserve 上界为 USD 0.419986；加上本轮后总上界 USD 0.538652，仍低于 owner
  授权的 USD 1.00。
- 没有部署 live，没有修改 ThesisVersion，没有改 cron，没有数据库 schema 变化，也没有扩大自动化权限。

脱敏、可复算摘要：
[`docs/review-evidence/thesis-impact-verifier-live-calibration-summary-2026-08-21.json`](../review-evidence/thesis-impact-verifier-live-calibration-summary-2026-08-21.json)。

## 下一步

保持 verifier 质量门关闭。现有 catalog 中，OpenAI GPT-5.6 与 assessment 同 family，不能独立复核；Claude 两个
profile 已暴露超预算或超时问题；DeepSeek 未达到冻结质量门。下一步应先改 broker 执行边界，让 verifier 使用
真正 provider-side 的结构化输出和可执行 token/cost 限制，再引入新的非 OpenAI family 候选。不能靠提高重试次数、
放宽 scorer 或删除失败样本来得到 pass。
