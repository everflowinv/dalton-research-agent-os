# Thesis-impact verifier 校准基础

日期：2026-08-21
状态：冻结 12 个样本和评分标准；新 verifier 合同已收紧；尚未运行新的付费模型校准

## 结论

Gate 2 的质量问题已转成一组可复算的校准资产。第一版 corpus 冻结 12 个样本：5 个应当通过，7 个应当拒绝；
其中 5 个高严重度错误、2 个中严重度错误。模型可见输入与人工 gold label 分开，评分器只在模型输出落盘后读取
gold，不把 `seeded_error`、严重度、预期 verdict 或人工理由放进 prompt。

目前只有 Gate 2 的 DeepSeek V4 Flash 结果属于真实观测。它在应当 `pass` 的样本上给出 `reject`，因此基线是
1/12 coverage、1 个误报、该已测 pass 样本误报率 100%。这只是单个已测样本的事实，不代表 12 个样本上的模型
准确率，也不能用来比较模型。

## 冻结标准

- impact 只允许 `supports / weakens / no_change / insufficient`。
- `pass` 必须没有 finding；`reject` 必须有 1–8 个 finding。
- finding code 和严重度固定：
  - 高：`binding_mismatch`、`driver_mismatch`、`unsupported_inference`、`impact_mismatch`；
  - 中：`rationale_contradiction`、`follow_up_quality`。
- `impact_mismatch.expected_impact` 必须使用相同的 closed taxonomy；其他 finding 必须为 `null`。
- `insufficient` 不是天然失败。只要 assessment 没有把证据缺口写成已实现结论，并提出能关闭缺口的具体追问，
  verifier 应当通过。
- 放权门槛保持不变：至少 30 个 seeded cases、检出率不低于 90%、高严重度错误零漏检。12 个样本即使测试
  全对，也不能解锁自动化。

## 合同与 fail-closed 行为

新 verifier WorkOrder 要求输出 schema `0.2`。每个 finding 必须严格包含 `code / severity / detail /
expected_impact`，未知 code、错误严重度、重复 code、taxonomy 外标签、`pass` 带 finding、`reject` 无 finding 都会
让当前 attempt 失败并进入有限重试。

历史 `0.1` 输出仍可按原 WorkOrder 做 immutable replay，但新 WorkOrder 不能回退到 `0.1`。authority 会重新读取
WorkOrder 冻结的输出版本，因此不能绕过 worker 写入旧格式。

worker 还会对已经由 authority 证明的事实做二次检查：

- refs/hashes 已一致时，不接受 `binding_mismatch`；
- assessment driver 与 Thesis mechanism 已逐字一致时，不接受 `driver_mismatch`；
- `impact_mismatch.expected_impact` 不能重复 assessment 自己的 impact；
- 非 `insufficient` assessment 不能出现 `follow_up_quality`。

Gate 2 那类“文字先说匹配，随后仍报 mismatch”因此不会再形成正式 verification。

## Corpus 分布

- 应通过：正确的 `supports`、`weakens`、`no_change`，以及两条证据缺口和追问都合格的 `insufficient`。
- 应拒绝：claim hash 漂移、driver 替换、把收入增速直接写成利润率扩张、方向写反、rationale 与 impact 自相
  矛盾、追问无法关闭缺口、忽略 constant-currency 口径。
- corpus hash：`c5f6928860f043fb4f3a01962dc68d7e53fd8c93b3291fafbc85e96aabb41797`。

## 验证和边界

- 校准合同、corpus closure、gold 隔离、评分器、历史误报基线和自动化门槛都有专项 unittest。
- routed worker 的合同重试、独立 family、记账、崩溃恢复和 replay 回归继续沿用原测试。
- package build 已确认包含 corpus JSON 和 `dalton-thesis-impact-calibrate` CLI。
- 本切片没有数据库 schema 变化，没有调用新模型，没有产生新费用，没有修改 Thesis，没有部署 live，也没有改 cron。

脱敏、可复算基线：
`docs/review-evidence/thesis-impact-verifier-calibration-baseline-v0.1.json`。

下一步才是把 12 个 model-visible prompts 分别交给候选 verifier，保存逐样本 invocation、usage、cost 和输出，
再由当前评分器生成完整报告。没有完整 12/12 coverage 前不报告模型检出率；没有达到 30 个样本前不讨论放权。
