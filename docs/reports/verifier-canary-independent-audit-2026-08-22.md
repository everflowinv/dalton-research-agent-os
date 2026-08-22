# 3×30 verifier canary 独立复核与更正

日期：2026-08-22
状态：原生产资格撤销；修正版 3×30 已在同日重新运行并通过

## 复核结论

原始产物证明 Gemini 3.7 Flash low 的质量表现成立：90 条记录都是 `fresh_execute`，共有 90 个不同
ModelInvocation，模型均为 `google/gemini-3.7-flash`；每轮从 records 重新评分都是 30/30、0 false positive、
0 high-severity miss。实际成本重算为 USD 0.12744450，与报告一致。OpenClaw host patch 链也通过
`apply_all.sh --check` 和 provider-controls fake-transport 测试。

但旧 campaign 不能作为正式生产 gate：三轮 calibration manifest 的 `run_ref` 完全相同。旧 run identity 没有
包含实例时间，导致三个不同运行复用了同一 authority identity。旧 evaluator 也没有要求 run identity 唯一，且
直接读取 `score.json`，没有从 records 和 frozen corpus 重算。修正后的 evaluator 对原产物复核结果为：三轮各自
仍 accepted，但 campaign `eligible=false`，原因是 `round run identities are not unique`。

## 同批发现的问题

- broker 将非 ProtocolError 的 host 裸错误回传给客户端；既有测试构造了包含 prompt 的 host error，当前 HEAD
  实测只有 21/22 通过，证明存在输入泄漏风险。
- day-budget authority 把 `created_at` 纳入重复调用的语义比较；真实时钟下相同 register/admit/settle 会被误判
  为冲突。settle 也没有阻止实际成本超过 reservation，alert identity 比较不完整，claim select 不在写事务内。
- `3d9cdab` 及前面的 7 个提交只存在于本地 main；复核开始时 `origin/main` 仍停在 `141c27e`，所以没有远端 CI
  能独立证明这些提交。

## 修复

- 非 ProtocolError 恢复为固定安全错误，不再回传 host message。
- calibration run manifest 升至 0.4，campaign manifest 升至 0.2；instance timestamp 进入 content-derived ID，
  resume 使用已持久化 timestamp。旧 0.3/0.1 manifest 只保留审计读取兼容。
- campaign gate 从 records 重建 exact WorkOrder、route/profile binding 和 score；要求每条调用是 fresh execute，
  三轮 run identity 唯一，profile version 不漂移，stored score 必须与重算结果一致。
- budget duplicate 比较忽略重试时间并返回原记录；actual settlement 不得超过 reservation；rejection/alert
  语义冲突 fail closed；alert claim 的读取和写入放入同一 immediate transaction。
- 新增 assessment phase policy，只允许 `profile:gpt-5-6-sol`；worker 在 broker 调用前验证 assessment 和 verifier
  phase policy 都只 pin 一个 profile。assessment thinking level 尚未做 provider-control pin，本批不把模型 pin
  说成 reasoning-control 证明。

## 当前门槛

旧 90 次调用可保留为模型质量证据，不能再写成生产 gate 已通过。需要 owner 重新授权一次付费 3×30，使用修正后
的 runner 生成三个不同 run identity。该 gate 通过前不进入 shadow，不 activation production policy，不重启
gateway，也不修改 ThesisVersion。

## 后续闭合

Owner 随后授权修正版 3×30。新 campaign `thesis-impact-verifier-canary:16a542de99e16404da89bdcd589a1097`
生成三个不同 run identity，90 条均为 fresh provider-controlled execution，三轮重新评分均为 30/30，合计成本
USD 0.12906825，修正版 gate 返回 `eligible=true`。详情见
`verifier-canary-3x30-corrected-passed-2026-08-22.md`。这只解除 shadow 的前置门，不等于 production activation。
