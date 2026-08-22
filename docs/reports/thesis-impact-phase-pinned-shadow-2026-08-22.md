# Thesis-impact phase-pinned isolated shadow

日期：2026-08-22  
状态：单条真实 Claim/Thesis shadow 通过；未部署到 live

## 结果

修正版 3×30 gate 通过后，使用 commit `dc1d3e4482f580482216fec8065d3fb276cd9f21` 对一条隔离的
Microsoft SEC revenue Claim 和 owner-admitted ThesisVersion 运行完整 thesis-impact 流程：

- assessment policy 只允许 `profile:gpt-5-6-sol`；实际路由为 `openai/gpt-5.6-sol`；
- verifier policy 只允许 `profile:gemini-3-7-flash`；实际路由为 `google/gemini-3.7-flash`；
- 两个模型 family 不同，verifier route 绑定 producer family constraint；
- assessment 判断 `supports`，Gemini verifier 返回 `pass`、0 findings，最终 quality gate 为 `eligible`；
- verifier WorkOrder 绑定 `verifier_thinking_level=low`，ResultEnvelope 带 `required_provider_controls=true` 和
  provider-control schema hash；
- assessment 没有 provider-controlled thinking 合同，本报告不声称 GPT-5.6 Sol 的 thinking level 已受控。

隔离 source bundle 由当前代码重新读取 `data.sec.gov` 生成，锁定 Microsoft 2026-04-29 10-Q：季度收入
USD 82.886bn，对比期 USD 70.066bn，同比增长 18.30%。source/numeric verifier 均为 pass，并自动形成正式
ClaimVersion；shadow 没有把这些记录写入 live Dalton store。

## 恢复、成本和不变式

assessment 在 model accounting 落盘后注入进程退出，lease 到期后只通过 broker `replayOnly/duplicate` 恢复；
invocation、usage、cost 都没有重复。随后 verifier fresh execute：

- GPT-5.6 Sol：885 input、251 output、1,136 total tokens，记账 USD 0.008560；
- Gemini 3.7 Flash：2,091 input、104 output、2,195 total tokens，记账 USD 0.001958；
- 合计记账 USD 0.010518，assessment/verifier 各有 USD 0.25 hard cap，总授权 cap USD 1.00。

最终 formal replay 使用禁止 broker 访问的 adapter，仍收敛到同一 `eligible`，且 accounting 行数不变。shadow 前后
Thesis current pointer 完全一致；Core、review、coordinator、router 的 `PRAGMA integrity_check` 都为 `ok`。

脱敏 evidence：`docs/review-evidence/thesis-impact-phase-pinned-shadow-summary-2026-08-22.json`。完整 owner-only
bundle 保留在 `temp/thesis-impact-shadow-phase-pinned-20260822-v2/`，不入库。

## 边界

本次运行只证明一条隔离真实 Claim/Thesis 的 phase pin、provider-controlled verifier、恢复、记账和只读 shadow
合同。它没有激活 live production policy、没有重启 gateway、没有修改 live ThesisVersion、没有切换 cron。
