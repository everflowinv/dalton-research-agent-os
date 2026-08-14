# Dalton Runtime Spike 与 v0.3 默认路径

> 日期：2026-08-14  
> 状态：Spike A 已完成；默认 worker 路径已选，仍未接入 Dalton live 系统

## 1. 裁决

Dalton 第一版日常执行采用两层组合：

- **Dalton-native process runtime 是默认边界。** Core 用严格的
  `WorkOrder + RuntimeProfile` 启动短生命周期 worker，只接受
  `ModelInvocation + ResultEnvelope`；
- **Pi agent-core 是需要模型/tool loop 时的首选 worker library。** 它放在 process
  adapter 后面，不承载 Research Ledger、scheduler、governance 或 commit；
- **DeepSeek Harness 暂不进入默认路径。** 保留为 builder/evaluator、fresh-agent
  workflow 和复杂插件组合的实验 adapter；
- **OpenClaw 继续只做人机桥、审批与 connector bridge。** Dalton 日常唤醒和执行不以
  OpenClaw session 为前提。

这不是“完全重写所有基础设施”。自研范围只包括 Dalton 的 domain kernel、process
envelope、policy 和状态机；模型 provider、Pi 的 agent loop、DeepSeek Harness 的插件
机制、现成 connector 和真正的 sandbox 都通过 adapter 使用。

## 2. 本轮实测

三条路径都在 Apple Silicon 本机冷启动，且不调用真实模型、不读取 API key、不接网络。
数字是单次工程 spike，不是统计 benchmark；它们只用于判断数量级。

### Dalton-native process runtime

- 同一个 formatter WorkOrder 完整跑通；
- 子进程把两条记录转成 canonical JSONL，并返回严格的 invocation/result envelope；
- 冷启动加任务：约 **0.06 秒**；
- maximum resident set size：约 **25.7 MB**；
- 无新增运行依赖，stdlib-only；
- timeout、stdout/stderr/frame 上限、临时 cwd、环境清理、budget/side-effect/ref 校验
  已进入测试。

### Pi agent-core 0.84.1

- 使用本地 fake provider，不调用模型 endpoint；
- fake provider 先产生 `format_records` tool call，Pi 执行 formatter，再完成第二个 turn；
- 产物通过同一个 Python `ProcessRuntimeAdapter` 的严格 envelope 校验；
- 事件序列包含两次 turn 和一次 tool start/end；usage 汇总为 34 个 fixture tokens；
- 冷启动加任务：约 **0.18 秒**；
- maximum resident set size：约 **80.0 MB**；
- 当前 pinned 依赖安装目录约 **131 MB**；
- 完整 coding-agent RPC 的 credential-free `get_state` 冷启动约 **0.39 秒**，maximum
  resident set size 约 **169 MB**。

Pi 的直接 agent-core 成本可以接受，coding-agent CLI 对默认 worker 没有必要。第一版
只嵌 `pi-agent-core`，不继承其 coding session、默认 shell/filesystem tools 或配置目录。

### DeepSeek Harness 0.1.0-rc.6

- 官方 Python SDK 和 bundled runtime 安装成功；
- 在清空 credential 环境后完成 stdio JSON-RPC `initialize`；
- 首次安装后的第一轮启动/initialize：约 **2.90 秒**；OS cache 变热后的复跑约
  **0.19 秒**，因此 2.90 秒不能当作每次固定成本；
- warm rerun 的 maximum resident set size：约 **116.0 MB**；
- venv 约 **212 MB**，其中 bundled macOS ARM runtime executable 约 **200.2 MB**；
- 没有发送 prompt，也没有跑完整 formatter WorkOrder。

这只是 Harness 的握手下限，不能和 native/Pi 的完整任务数字直接等同。它目前也不原生
理解 Dalton WorkOrder、budget、side effects、usage refs 或 ResultEnvelope；adapter 还要
做 prompt projection、session-event 收割和 provenance 归一化。结合 developer preview
状态与明显更高的固定成本，它不适合做第一版日常 worker。

## 3. 为什么选择 native + Pi

Native process boundary 解决最重要的替换问题：Core 不关心 worker 是 Python、Node、Pi、
Harness 还是未来的其他 runtime。Pi 只在确实需要模型循环、工具调用、steering、abort 和
event stream 时出现；确定性 formatter、parser、validator 直接用更小的 native worker。

这样可以避免两种反向耦合：

- 不让 Dalton 的 research state 迁就 Pi session 或 Harness plugin tree；
- 不为了少量 deterministic transform 启动完整 agent harness。

Pi 的工具 hook、event stream 和 provider 分离可直接复用，但 Pi 没有 filesystem、process、
network 或 credential 权限系统。它只能运行在 Dalton 外部的受控执行环境中。Pi session
可以做工作记忆，不能做正式 research memory；正式状态仍只经 writer service 写入。

DeepSeek Harness 的以下理念已经吸收进设计，而不是照搬实现：

- headless subprocess / JSON-RPC；
- capability service/provider/consumer seam；
- typed event 和可逆 lifecycle；
- fresh-agent structured handoff；
- model-visible state 可由 durable events 重建。

## 4. 自我写工具如何进入这条路径

自生成工具不能因为“测试跑过”就直接上线。第一版流程固定为：

```text
Capability gap
  → builder 生成 proposal + artifact hash + fixtures
  → 外部 sandbox 执行
  → 独立 evaluator 记录 evaluation
  → human approval
  → Capability Registry active pointer
  → RuntimeProfile allowlist
  → ProcessRuntimeAdapter 执行
```

当前已实现 proposal/evaluation/promotion/rollback 账本和 process envelope，但还没有真正
的 hostile-code sandbox。临时 cwd、清理环境和同 UID 子进程不是安全隔离；在独立 OS
identity、container/VM 或等价 service boundary 落地前，自生成代码只能在 fixture 环境
测试，不能获得网络、凭据、Core DB 或生产写权限。

## 5. 可复现资产

- Dalton-native adapter：`scripts/dalton-core/src/dalton_core/process_runtime.py`；
- deterministic formatter：`scripts/dalton-core/src/dalton_core/formatter_worker.py`；
- Pi pinned spike：`scripts/dalton-core/spikes/pi-agent-core/`；
- DeepSeek Harness handshake：`scripts/dalton-core/spikes/deepseek-harness/`。

## 6. 验收

- 全量 unittest：**85/85** 通过；process runtime 定向测试：**13/13** 通过；
- Python compileall、`node --check`、`git diff --check` 通过；
- Pi spike 从空目录执行 pinned `npm ci --ignore-scripts` 后，通过 Python
  `ProcessRuntimeAdapter`；两次输出一致，result SHA-256 为
  `0a64a2af20fb6c1ab7d0fecc4efe73e73ed5382e2e944f47138998e1dbea1a8a`；
- Dalton wheel 重建、隔离安装、installed demo、installed process runtime import 通过；
- Slice 3 release-candidate wheel SHA-256（Slice 4 后已由新构建替代）：
  `fe8c4a1148b1b144247c7bb6f2767152ee5ed6f24fc5868016b0d5126a14488d`；
- wheel 仍包含 18 份 JSON Schema 和两份 SQL schema；
- 独立敌对复审确认跨 envelope artifact provenance、真实 wall-clock deadline、
  process-group 清理和可信 invocation identity 均已执行；最终未发现 P0/P1；
- Slice 3 source 不引用 `workspace-chem` 或 live DB path。

## 7. 下一切片

下一步不再扩大 runtime 选型，而是补两项让这条路径能长期运行的基础设施：

1. **Scheduler lease / retry / idempotency**：Core 分配 WorkOrder，worker kill/timeout 后可
   重试，重复结果不会产生第二次正式写入；
2. **Capability sandbox 与 attestation**：先以 formatter proposal 为样本，固定 artifact、
   dependency、environment 和 fixture result hash，验证 evaluator 与 worker 都不能扩大
   权限。

真实模型接入放在这两项边界之后。到那时 Pi adapter 只需替换 fake provider 并记录实际
provider/model/usage，不需要修改 Research Ledger 或 commit schema。

## Sources

- Pi repository：<https://github.com/earendil-works/pi>
- Pi agent-core：<https://github.com/earendil-works/pi/blob/main/packages/agent/README.md>
- Pi RPC：<https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md>
- Pi security：<https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md>
- DeepSeek Harness：<https://github.com/deepseek-ai/deepseek-harness>
- DeepSeek Harness Python SDK：<https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md>
- DeepSeek Harness architecture：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md>
