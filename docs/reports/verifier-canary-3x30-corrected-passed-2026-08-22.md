# 修正版 3×30 provider-controlled verifier canary 通过

日期：2026-08-22  
状态：`production_gate.eligible=true`；只解锁 shadow，未激活 production policy

## 结论

Owner 授权后，修正版 runner 在 commit `83ef2ac8dd9384150e10847ab57a8e3b4b4510b6` 上重新运行
3×30。三轮都使用 exact `profile:gemini-3-7-flash`、`google/gemini-3.7-flash`、thinking low 和
`provider-controlled-v1`：

- 三轮各 30/30，accuracy 100%，0 false positive，0 high-severity miss；
- 90 条记录全部为 fresh execution，90 个不同 ModelInvocation；
- 90 条记录全部带 provider-control 证明，90 个 WorkOrder 全部绑定 `verifier_thinking_level=low`；
- 0 failed call、0 parse failure、0 control failure、0 thinking-binding failure；
- 三轮 run identity 分别为 `534828e8…`、`6bd1c154…`、`ede70441…`，不再重复；
- 总用量 65,871 input tokens、21,244 output tokens、87,115 total tokens；
- 总实际成本 USD 0.12906825，低于 campaign hard cap USD 27.00。

修正后的 evaluator 不读取 `score.json` 作为权威结论。它从 frozen corpus、WorkOrder、route、ResultEnvelope 和
records 重新构建输出并评分，同时验证 exact profile version、fresh execution、provider controls、thinking binding
和唯一 run identity。首次独立复核指出的 gate 缺口已经由这次新产物闭合。

脱敏证据：`docs/review-evidence/verifier-canary-3x30-corrected-summary-2026-08-22.json`。完整 owner-only 产物保留在
`temp/verifier-canary-3x30-corrected-20260822/`，不入库；summary 内保存关键文件 SHA-256。

## 运行前与运行后验证

- 新 invocation 的 `replayOnly` 零付费探针返回 `IDEMPOTENCY_MISS`，证明 broker、签名、socket、profile pin 与
  fail-closed 路径可用，且没有调用 provider；
- campaign exit code 0，离线再次调用修正版 evaluator 得到同一 `eligible=true`；
- 关键文件均为 owner-only `0600`；
- GitHub Actions run `32594853663` 的 broker job 已通过，Python 3.11/3.13 全量 suite 在本报告生成时仍在运行。

## 下一步边界

本 gate 只允许进入 isolated shadow。它没有修改 live Dalton store、ThesisVersion、OpenClaw route、cron 或
production policy，也没有重启 gateway。production activation、gateway restart 与任何 ThesisVersion mutation
仍是后续独立决策。
