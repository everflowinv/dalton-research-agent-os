# Thesis-impact 校准 v0.2 候选结果

日期：2026-08-22  
状态：两个模型通过校准质量门槛；生产仍锁定

## 结论

四个上一轮满分模型完成 30 个 v0.2 冻结案例。Gemini 3.1 Pro Preview 是唯一 30/30、检出率 100%、
误报率 0%、高严重度零漏检且 30 个输出全部符合 schema 的模型。Claude Opus 5 为 29/30，检出率
94.44%、误报率 0%、高严重度零漏检；它漏掉的是 1 个 medium follow-up quality 案例，因此仍满足当前校准质量
阈值。

Qwen 3.8 Max 和 GPT-5.6 Terra 都是 29/30、检出率 94.44%、误报率 0%，但各有 1 个 high-severity
miss，按现行门槛淘汰。主候选改为 Gemini 3.1 Pro，Claude Opus 作为独立 shadow 候选。

脱敏结果见
[`docs/review-evidence/thesis-impact-calibration-v0.2-shortlist-summary-2026-08-22.json`](../review-evidence/thesis-impact-calibration-v0.2-shortlist-summary-2026-08-22.json)。

## 四模型结果

- Gemini 3.1 Pro Preview：30/30；detection 100%；false positive 0%；high miss 0；费用 `$0.228186`。
- Claude Opus 5：29/30；detection 94.44%；false positive 0%；high miss 0；费用 `$1.483150`。
- Qwen 3.8 Max：29/30；detection 94.44%；false positive 0%；high miss 1；费用 `$0.05450115`。
- GPT-5.6 Terra：29/30；detection 94.44%；false positive 0%；high miss 1；费用 `$0.077762`。

具体失败：

- Claude Opus 在 case 011 返回空对象，严格 schema 将其判为 invalid；这是 medium `follow_up_quality` 案例。
- Qwen 在 case 025 判断应拒绝，但只报 `unsupported_inference`，没有报 gold 要求的 `impact_mismatch`；这是 high 案例。
- Terra 在 case 022 返回非法 `assessment_hash`；这是 high `driver_mismatch` 案例。

## 调用、费用和重放

- 120 次 provider 调用全部成功，119 个输出通过 schema。
- 实际费用 `$1.84359915`，未知费用预留 `$0`；整个接入、smoke、12 案例和 30 案例阶段累计费用
  `$5.023876966`。
- 同参数 `--resume` 只读完成，broker journal hash 和四份 responses log hash 前后不变，新增 provider 调用为 0。

## 生产边界

这里的 `calibration_quality_eligible` 只表示模型在冻结语料上过线。运行使用
`calibration-posthoc-v1`，它没有 production 所需的 provider control proof，不能满足 `provider-controlled-v1`。
因此 live routing 没有变化，生产可用模型仍为 0。

下一步是给 Gemini 主候选和 Claude shadow 候选接入可验证的 provider controls，并补 exact model、token、timeout、
cost proof 的 broker conformance。只有这条链也通过，才生成生产 routing policy。
