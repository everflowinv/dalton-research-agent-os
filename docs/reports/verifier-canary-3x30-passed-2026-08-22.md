# 3×30 provider-controlled verifier canary 通过

> **2026-08-22 独立复核更正：本报告的 `production_gate.eligible=true` 已撤销。**
> 90 次调用和 3 轮 30/30 的质量数据均可从原始记录重算，但三轮 manifest 共用同一个 `run_ref`；旧 gate
> 没有检查 run identity 唯一性，并且直接信任落盘 `score.json`。详情见
> `verifier-canary-independent-audit-2026-08-22.md`。修正版 3×30 后来已用三个不同 run identity 重新执行并通过；
> 当前有效结论见 `verifier-canary-3x30-corrected-passed-2026-08-22.md`，本报告只保留历史记录。

日期：2026-08-22
状态：生产验收门通过（`production_gate.eligible=true`）；production verifier 解锁至"待 shadow 与单独 policy activation"，live route 仍未变化

## 结论

Owner 授权（verifier = exact `google/gemini-3.7-flash`、thinking low、付费上限放宽）后，本日完成了从 host 补丁到真实 broker 路径 3×30 canary 的全部开闸链路。`dalton-thesis-impact-verifier-canary` 以 `provider-controlled-v1` + `thinking-level low` 在真实 OpenClaw gateway/broker 路径上完成 3 轮 × 30 case：

- 每轮 accuracy 100%、detection rate 100%、0 false positive、0 high-severity miss、coverage 30/30；
- 90 条 durable record 全部 `required_provider_controls=true` 且绑定 thinking level（0 control failure、0 binding failure）；
- 全部 `fresh_execute`（无重放/恢复混杂），broker journal、countTokens 事前准入、thinking=LOW provider 请求、proof 回显逐 record 成立；
- 聚合实际花费 **USD 0.12744450**（cap USD 27.00；单轮约 USD 0.042）。

脱敏证据：`docs/review-evidence/verifier-canary-3x30-summary-2026-08-22.json`；完整 manifest/records 保留在 `temp/verifier-canary-3x30/`（不入库）。

## 打通链路时修复的四个缺口（全部有真实运行证据）

1. **broker plugin manifest configSchema 不接受 `thinkingLevel`**：`openclaw.plugin.json` providerControls 增加 closed `thinkingLevel: ["low"]` enum，版本升至 spike.5（提交 `41605a1`）。
2. **gateway 剥离 plugin `runtime.llm.capabilities`**：plugin runtime 的 lazy llm facade 只暴露 `{ complete }`。新增 host patch
   `patch_openclaw_plugin_llm_capabilities.py`（已注册进 `apply_all.sh`）：facade 同步暴露冻结的
   `capabilities.providerControls` 广告（镜像 runtime bundle 的常量）。
3. **config 模型定义缺显式 `api` 字段导致 transport 误解析**：openclaw.json 的 `gemini-3.7-flash` 模型定义被解析为
   `openai-completions`，受控路径正确 fail closed。已给该模型写入显式 `api: "google-generative-ai"`（provider 级
   设置不覆盖模型级解析）。config 修改前已备份 `openclaw.json.bak_gemini_controls_20260822`。
4. **worst-case 预留预算口径**：首几次冒烟 fail closed 于 `worst-case token cost exceeds maxCostUsd`——4000 output
   tokens × $7.50/M（保守 rate card）≈ $0.03，超过最初的 $0.02 单案 cap。这是准入正确工作，非故障；正式 run 按
   worst-case 口径设置 per-case cap（$0.30）后一次通过。

另有两个由本轮促成的仓库改进：broker `REQUIRED_CONTROLS_UNAVAILABLE` 错误信息现在具名缺失项（capability
version/transport/mode）。非 ProtocolError 的 host 裸 message 透传后来被独立复核撤销：host error 可以包含原始
prompt，且当前 HEAD 的安全测试确实因此失败。

## 验证

- Host patch 链 `apply_all.sh --check`：CHECK OK（新增 `google_thinking_level`、`plugin_llm_capabilities` 两个
  patch，全部幂等可重放）。
- Host provider-controls 测试（fake transport）：全绿，新增 thinkingLevel 用例（provider 请求 LOW、proof 回显
  low、未校准等级 0 调用、caller thinking 意图无法覆盖、无要求请求不携带）。
- 单 case 受控冒烟（temp/verifier-controlled-smoke-11）：succeeded、$0.001143、controls/schema/thinking 全绑定。
- 3×30 campaign：exit code 0，`production_gate.eligible=true`，`reasons=[]`。
- 本轮 broker/Python 代码改动均带测试；仓库提交见 git log。

## 剩余开闸步骤（未变，需 owner 动作）

1. **Shadow**：按既定顺序先 shadow 运行（不改 ThesisVersion）；
2. **production policy activation 与 gateway restart**：单独批准后激活 phase-pinned verifier policy；
3. assessment producer pin（GPT-5.6 Sol）批次：补齐"大脑"位的确定性，避免共享 policy 按成本漂移。

## 边界

- 本轮未修改 ThesisVersion、未改 live route、未切 cron；`temp/` 产物不入库。
- rate card 采用保守口径（input/output $1.50/$7.50 每 M，intro 价的 2 倍上限），实际计费按 Google intro 价
  （$0.75/$3.75），所以实际花费远低于预留；expiresAt 2026-09-22，到期需更新。
