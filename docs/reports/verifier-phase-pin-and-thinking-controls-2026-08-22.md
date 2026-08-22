# Phase-pinned verifier policy 与 thinking-level 控制合同

日期：2026-08-22
状态：开发候选已实现并通过本机测试；生产 3×30 canary 仍待 owner 单独授权，live 路由未变

## 结论

本批次落实 wrapper-selection 报告列出的三项 production conformance 缺口中可以在仓库内完成的两项：

1. **Phase-pinned immutable verifier policy**：新增不可变 policy chain
   `model-routing-policy-version:dalton-openclaw-verifier:1`，只允许 exact
   `profile:gemini-3-7-flash`。`ThesisImpactModelWorker` 新增可选
   `verifier_routing_policy_ref`；verification 相位改在该 policy 下路由，assessment 相位继续使用共享
   policy。worker 在 claim lease 之前重读该 policy 并要求 `allowed_profile_ids` 恰好一个 profile，共享的按成本排序
   policy 不能再冒充 verifier policy（fail closed 为 `ThesisImpactModelWorkerRejected`）。producer 与被 pin
   profile 同 family 时，路由器既有的 `model_family_not_independence` 过滤使全部候选被拒绝，worker 落成
   `MODEL_ROUTE_REJECTED` 正式失败，不发生 broker 调用。
2. **thinking low 进入 broker request、request hash 与 provider-control proof**：
   - 控制面在 verifier WorkOrder identity/metadata 中冻结 `verifier_thinking_level: "low"`；旧 WorkOrder
     不带该字段，按旧合同 replay。
   - adapter 的 `_required_provider_controls` 在 metadata 存在该字段时向 `requiredControls` 增加 closed
     `thinkingLevel`（当前只接受 `"low"`）；该字段随请求进入 broker request hash 与 invocation 的
     `required_provider_controls_hash` 幂等身份。未校准的等级在 admission 前被拒绝。
   - broker（0.1.0-spike.5）协议接受可选 `requiredControls.thinkingLevel`（closed enum `"low"`）；profile
     的 `providerControls` 可声明 `thinkingLevel`，请求要求时必须 exact 匹配，否则在任何 host 调用前返回
     `REQUIRED_CONTROLS_UNAVAILABLE`。host `providerControls` 帧携带该字段，host proof 必须 exact 回显
     `thinkingLevel`，proof 形状按请求是否要求该字段闭合。要求由请求驱动：不带该字段的请求即使 profile 声明了
     thinking level 也不会获得该控制或 proof 字段。
   - calibration runner 的 run manifest 升到 0.3，新增 closed `thinking_level`（`null` 或 `"low"`），进入 run
     identity；`--thinking-level` CLI 冻结同一值到 calibration WorkOrder metadata。旧 manifest（0.2、无该字段）
     不再通过校验，fail closed，需要新开 run。

第三项（用生产预算与真实 broker 路径重跑 3×30 cases）需要 owner 对付费调用的单独授权，本批次不执行；
runner 已具备 `--thinking-level low --execution-tier provider-controlled-v1` 的完整入口。

## Contract 改动

- `model_deployment.py`：`VERIFIER_POLICY_REF`、`VERIFIER_PROFILE_ID`、`openclaw_verifier_policy()`；
  `upgrade_openclaw_broker_catalog` 追加注册该 policy（幂等，返回 `verifier_policy` 键）。
- `thesis_impact_control.py`：`VERIFIER_THINKING_LEVEL = "low"` 冻结进 verifier WorkOrder identity 与
  metadata；新 WorkOrder 的 work id 因此与旧 epoch 不同，旧 WorkOrder 保持原 id 原合同。
- `openclaw_model_adapter.py`：`VERIFIER_THINKING_LEVELS = {"low"}`；`requiredControls.thinkingLevel`。
- `thesis_impact_model_worker.py`：`verifier_routing_policy_ref` 参数与 `_phase_policy_ref` admission gate。
- `thesis_impact_calibration_runner.py`：manifest schema 0.3、`thinking_level` 字段、CLI `--thinking-level`。
- broker `protocol.mjs`/`broker.mjs`：可选 `thinkingLevel`、profile 声明与匹配、proof 回显校验；
  `BROKER_VERSION` 升至 `0.1.0-spike.5`。协议版本保持 0.1（additive optional 字段）。
- broker README 更新 required-controls 合同说明。

## 行为边界

- 没有为 `profile:gemini-3-7-flash` 写入 broker 侧 providerControls 配置或 rate card：这是 host 侧
  openclaw.json 的 operator 动作，仓库只定义合同。live gateway 重启、Google host patch 对 thinkingLevel
  的透传与证明仍是开闸条件。
- production verifier 仍为 0：phase-pinned policy 只在显式传入 worker 时生效，live 部署与 ThesisVersion
  mutation 未变化。
- 无 schema（contracts/ JSON Schema）变化；run manifest schema 0.2→0.3 的迁移说明见上，旧 manifest 不兼容
  replay 是有意 fail closed。

## 验证

- Node broker：22/22 通过（新增 thinking end-to-end：协议拒绝未校准等级、profile 不匹配 0 host call、
  request hash 绑定导致 IDEMPOTENCY_CONFLICT、proof 缺失/不匹配 INVALID_HOST_RESULT、无要求请求不携带控制）。
- Python targeted：`test_model_deployment`、`test_thesis_impact_calibration_runner`、
  `test_openclaw_model_adapter` 32/32 通过（新增 thinking-level admission 与 controls 绑定测试）。
- Python E2E `test_thesis_impact_control`：9/9 通过。新增两条：
  - phase-pinned policy 下 verifier 固定路由到被 pin 的较贵 profile（即便更便宜的 verify-capable profile
    存在），assessment 仍走共享 policy；verifier broker 帧的 `requiredControls.thinkingLevel == "low"`，
    assessment 帧无 `requiredControls`。
  - producer 与被 pin profile 同 family 时 verification fail closed（durable rejected decision、0 次 verifier
    broker 调用、0 条 verification 记录）；未 pin 的共享 policy 传给 `verifier_routing_policy_ref` 在任何
    scheduler/broker 动作前被拒绝。
- 全量 `unittest discover`：621/621 `OK`（较上一基线 +5：adapter thinking admission 1、deployment pin 1、
  calibration manifest 1、E2E 2）；`compileall` 通过。
- 显式 hermetic research replay canary：`passed`，0 provider calls、recorded-only network、0 errors。
- wheel 与 sdist 构建成功；`git diff --check` 通过。
- 本轮没有付费调用、没有修改 live route、gateway 配置、Core authority 或 ThesisVersion。

## 下一步（开闸条件不变）

1. operator 在 host 侧为 `profile:gemini-3-7-flash` 配置
   `google-generative-ai-count-tokens-v1` controls、有效 rate card 与 `thinkingLevel: "low"`，并完成 OpenClaw
   host patch 与 safe restart。
2. owner 授权后，用 `dalton-thesis-impact-calibrate-matrix`/runner 以
   `--execution-tier provider-controlled-v1 --thinking-level low` 在真实 broker 路径跑 3×30 cases；
   要求 0 false positive、0 high miss、0 schema/control failure，并核对 exact model/profile/thinking/schema
   proof、retry 与 replay。
3. canary 通过后先 shadow，不修改 ThesisVersion；production policy activation 与 gateway restart 单独批准。
