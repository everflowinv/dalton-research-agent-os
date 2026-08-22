# Thesis-impact verifier wrapper binding 与候选选择

日期：2026-08-22
状态：wrapper-owned contract 已完成开发验收；Gemini 3.7 Flash low 已选为主候选；生产仍锁定

## 结论

verifier 不再负责回抄 `assessment_ref` 和 `assessment_hash`。模型只返回 closed semantic decision：
`schema_version / verdict / findings`；trusted worker 从 immutable WorkOrder 绑定 exact assessment identity，
再生成正式 `0.2` verifier output。原始模型 ResultEnvelope 保持不变，authority 可用原始 decision 和 WorkOrder
重建正式结果；历史 WorkOrder 继续按旧合同 replay。

同一 30-case v0.2 corpus、strict prompt、temperature 0、thinking low、16,000 output cap 的重新实跑中，
Gemini 3.7 Flash 和 GPT-5.6 Luna 都是 30/30；Qwen DeepSeek V4 Flash 为 27/30。Owner 已选择 exact
`google/gemini-3.7-flash`、thinking low 作为生产 verifier 主候选。该选择没有改变 live routing，也没有授权
自动修改 ThesisVersion。

脱敏结果见
[`docs/review-evidence/thesis-impact-verifier-wrapper-selection-summary-2026-08-22.json`](../review-evidence/thesis-impact-verifier-wrapper-selection-summary-2026-08-22.json)。

## Contract 改动

- 新增 `thesis-impact-verifier-decision-provider-output-v0.1.schema.json`。provider-visible schema 只允许
  `schema_version`、`verdict` 和 `findings`，模型返回 target binding 会被 closed validator 拒绝。
- verifier WorkOrder 冻结 `verifier_binding_mode=wrapper-owned-v1`、decision schema version、正式 output schema
  version，以及 exact assessment/Claim/Thesis refs 和 hashes。
- worker 先复核 WorkOrder 与已持久化 assessment 的 exact identity，再验证 semantic decision，并从 WorkOrder
  注入 assessment ref/hash。authority persistence 与 replay 重走同一 binding 函数。
- 老 WorkOrder 没有 binding mode 时继续验证完整旧输出，不重写历史 ResultEnvelope 或 verification record。
- calibration runner 使用相同 semantic contract；评分前由 trusted wrapper 绑定，gold label 仍不进入 prompt。

## 三模型同口径结果

- **Gemini 3.7 Flash low**：30/30；detection 100%；false positive 0；high miss 0；调用失败 0。
  input 25,257、reasoning 5,633、visible output 1,597、total 32,487 tokens；平均 2.452 秒，P95 3.911 秒，
  最大 4.506 秒；按当前配置折算 `$0.04605525`。
- **GPT-5.6 Luna low**：30/30；detection 100%；false positive 0；high miss 0；调用失败 0。
  input 21,349、reasoning 3,841、visible output 1,924、total 27,114 tokens；平均 7.829 秒，P95 12.687 秒，
  最大 13.885 秒；按当前配置折算 `$0.0111878`。
- **Qwen DeepSeek V4 Flash low**：27/30；detection 83.33%；false positive 0；high miss 2；调用失败 0。
  input 21,865、reasoning 29,034、visible output 1,821、total 52,720 tokens；平均 12.997 秒，P95 32.499 秒，
  最大 93.702 秒；按当前配置折算 `$0.0251746`。case 006 漏 `binding_mismatch`，case 008 把
  `unsupported_inference` 错报为 `impact_mismatch`，case 011 漏 `follow_up_quality`。

90 条 raw model output 都没有 `assessment_ref/hash`，wrapper 后 90 条均有 exact target bindings；三份 manifest
使用同一个 corpus hash `e736c0fc6f6c3dcf569dde76aa40fdaa6f7cfa2311e78e8efa2153771c625703`。token 来自各
provider 自己的 tokenizer，只适合比较计费量级；延迟是端到端 wall clock，包含不同 provider transport 开销。

Gemini 3.7 Flash 的选择依据是本轮零误杀、零 high miss、最低延迟，以及此前 direct strict run 同样 30/30。
Luna 保留为低成本候选，需增加重复轮次和 pass-heavy adversarial corpus。Qwen 与 Ox-alpha 都有 high miss，暂不进入
生产候选。

## 验证

- Python 全量：616/616，`OK`。
- OpenClaw broker：21/21，`pass`。
- wheel 与 sdist 构建成功，新 decision schema 已进入 package data。
- wrapper/authority/adapter/calibration 专项、历史 replay、routed worker slow test 均已覆盖。
- 90 条付费输出测试没有修改 live route、gateway 配置、Core authority 或 ThesisVersion。

## 生产阻塞与下一步

这轮三模型比较是 provider-direct calibration，不是 production broker conformance。当前
`profile:gemini-3-7-flash` 没有 `providerControls`；broker profile/protocol 也没有强制并证明 `thinking=low` 的字段。
此外，现有 thesis-impact worker 的 assessment 与 verifier 共用同一 routing policy，broker policy v3 按估算成本在
全部 profile 中排序，不能证明 verifier 固定选择 Gemini。

生产开闸前必须完成：

1. 建立 phase-specific immutable verifier policy，只允许 exact `profile:gemini-3-7-flash`；producer 为同 family
   时 fail closed。
2. 把 thinking low 纳入 broker request、request hash 和 provider-control proof；为 Gemini profile 配置有效 rate
   card 与 `google-generative-ai-count-tokens-v1` controls。
3. 用生产预算和真实 broker 路径至少重跑 3×30 cases，要求 0 false positive、0 high miss、0 schema/control failure，
   并验证 exact model/profile/thinking/schema proof、retry 和 replay。
4. canary 通过后先 shadow，不修改 ThesisVersion；再单独批准 production policy activation 和 gateway restart。
