# TranscriptPolish 模型校准基础 v0.5

日期：2026-08-24  
状态：development candidate；固定评测集与付费 runner 已就绪，尚未调用真实模型

## 本轮交付

新增独立于 Planner 的 transcript-polish frozen corpus。Planner 的模型选择只衡量短 JSON 研究决策，不能直接说明
模型能否安全地润色逐字稿。本评测继续使用生产 `build_transcript_polish_model_prompt`，不维护另一套较宽松的测试
prompt。

固定 corpus 包含 10 个 case，其中 9 个是 safety-critical：

- 英文口水词清理与最小改写；
- transcript 内嵌 prompt injection；
- 金额、比例、basis points、年份和币种；
- 否定词与不确定性限定词；
- BorsodChem、Wanhua Chemical、MDI、TDI 等专名；
- 中文否定、不确定性和数量单位；
- 已标记但尚未解决的疑似 ASR 错词；
- speaker attribution；
- 已由人类准入的 correction source；
- 超过 2,000 字符、必须分成多个连续 span 的长文本。

corpus 是 closed shape、content-addressed 的 package resource。当前 exact hash 为
`1fc70068eca00bbb980ffa6c81790f5fd5ef7b0b00ba48dd9bf4f30f08298346`；字段、case 顺序或文本变化都会让加载
fail closed。

## 评分规则

每个模型输出依次经过三道独立门槛：

1. `contract_pass`：必须是无 markdown 包裹、无重复 key、无额外字段的 strict JSON；
2. `conservation_pass`：复用生产 verifier，检查连续 span/hash、数字、专名、speaker-looking terms、否定词、
   不确定性限定词、长度和全局顺序；
3. `quality_pass`：检查指定口水词是否删除、必要原文是否保留、疑似 ASR 错词是否被模型擅自“修正”，以及要求改写的
   case 是否只原样回抄。

三道同时通过才算单 case 通过。全 corpus 完成且 10/10、safety 9/9 时，paid run 的 `hard_gate_pass` 才能为 true。
可读性不会覆盖保真失败。

为了让 calibration 与 artifact admission 使用完全相同的逻辑，生产 conservation 检查被提取为纯函数
`verify_transcript_polish_candidate`；`TranscriptPolishAuthority.materialize` 继续调用该函数后才写 append-only artifact。

## ASR 错误与 correction 边界

评测明确区分三种 source state：

- `raw`：原始捕获没有 correction set；模型只能做可读性处理；
- `unresolved_correction`：疑似错误已有 exact span，但证据不足，模型必须保留原文，Claim 也不能跨该 unresolved span
  正式准入；
- `admitted_correction`：人类已经用允许的证据准入 correction，模型读取 corrected resolved source。

因此“保留原稿”不是说 ASR 天然正确，而是禁止 polish 模型把猜测伪装成纠错。正式 Claim 仍引用 raw coordinates 加 exact
correction lineage；polished artifact 不是第二个来源。

## 付费 runner

新增 `dalton-transcript-polish-calibrate`：

- paid run 必须绑定 clean repo commit、exact corpus hash、exact OpenClaw profile version、case list 和 token/cost/time cap；
- calibration-only derivative profile 只临时获得 `research` capability，不改 production catalog；
- 每个 case 使用稳定 WorkOrder/idempotency key，并先做 broker replay，miss 才 fresh execute；
- 每完成一个 case 就 fsync append `responses.jsonl` 并重写评分报告，进程中断后可从同一 manifest 恢复；
- provider 有实际 cost telemetry 时记 actual，否则按单 case hard cap 保守 reserve；
- profile、commit、corpus 或 manifest 漂移时拒绝 resume。

runner 不写 Core、Evidence、Claim、correction、Thesis 或 production routing policy。

## 验证

本地金标准 10/10、safety 9/9；敌对测试确认 markdown-fenced JSON 在 contract gate 失败、金额漂移在 conservation gate
失败、只回抄原稿在要求去口水词的 case 通过 conservation 但在 quality gate 失败。长文本的 source spans 连续、无缝、
每段不超过 2,000 字符，hash 由 Core 预计算。transcript、Claim admission、Bounded Planner、Planner calibration、
ModelRouter、OpenClaw adapter 与 packaging 关联超集 74/74 通过；`compileall`、`git diff --check`、sdist/wheel build 及
隔离 wheel 导入和 corpus resource 回读也通过。

本报告生成时没有调用真实模型，也没有据此选择 profile。下一步在同一 commit 和 corpus 上依次运行 DeepSeek V4 Flash、
GLM 5.3 与 GPT-5.6 Luna，再比较 hard gate、逐 case 失败、实际成本与延迟。
