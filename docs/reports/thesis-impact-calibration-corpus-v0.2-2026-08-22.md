# Thesis-impact 校准语料 v0.2

日期：2026-08-22  
状态：30 个案例已冻结；候选模型尚未在新语料上重跑

## 结论

校准语料从 12 个案例扩到 30 个，达到 scorer 的最低数量门槛。v0.2 包含 12 个应通过案例和 18 个应拒绝
案例，覆盖全部 6 类 finding；其中 13 个 high、5 个 medium，并加入 1 个同时要求
`binding_mismatch` 和 `impact_mismatch` 的双错误案例。

v0.2 corpus hash 为 `e736c0fc6f6c3dcf569dde76aa40fdaa6f7cfa2311e78e8efa2153771c625703`。
旧 v0.1 文件和 hash `c5f6928860f043fb4f3a01962dc68d7e53fd8c93b3291fafbc85e96aabb41797`
原样保留。脱敏清单见
[`docs/review-evidence/thesis-impact-calibration-corpus-v0.2.json`](../review-evidence/thesis-impact-calibration-corpus-v0.2.json)。

## 新增覆盖

- 正确边界：稳定留存、RPO 转化缺口、直接利润率证据、低 churn、定价与销量条件、无关 capex、用户下降。
- 高严重度错误：错误 thesis hash、错误 claim ref、替换 driver、合同转收入过度推断、降本推断需求、方向倒置。
- 中严重度错误：impact 与 rationale 自相矛盾、缺少 follow-up、follow-up 与证据缺口无关。
- 组合错误：同一 assessment 同时绑定错误 ClaimVersion，并把客户下降误判为 supports。

模型只看到 Claim、Thesis 和 Assessment；`seeded_error`、severity、gold verdict、required finding 和人工理由不进入
prompt。gold 自检在 30/30 coverage 下得到 100% accuracy、100% detection、0 个 high-severity miss，说明语料与
scorer 自洽。这个自检不代表任何真实模型已经获准生产。

## 验收

- 校准专项 19/19 通过。
- broker 20/20 通过。
- v0.1 文件保持 12 个案例，hash 回归通过。
- v0.2 包含 30 个唯一 case id，12 pass / 18 reject，全部 6 类 finding 都有 gold 覆盖。
- 默认完整运行费用上限从 `$0.25` 调整到 `$0.30`，对应 30×`$0.01` 的硬预留；单案例 smoke 不受影响。

下一步只在上一轮四个满分模型上跑 30 案例：Qwen 3.8 Max、GPT-5.6 Terra、Gemini 3.1 Pro Preview、Claude
Opus 5。只有新语料分数达标且生产 provider control proof 完整，才生成生产 routing policy。
