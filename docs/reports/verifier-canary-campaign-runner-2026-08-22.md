# 3×30 provider-controlled verifier canary campaign runner

日期：2026-08-22
状态：开发候选已实现并通过本机测试；campaign 本身需要 owner 对具体付费金额的单独授权才会运行

## 结论

新增 `dalton-thesis-impact-verifier-canary`（`src/dalton_core/thesis_impact_verifier_canary.py`）。它把
wrapper-selection 报告第三项开闸条件——"用生产预算和真实 broker 路径至少重跑 3×30 cases"——收敛成一条显式
授权即可执行的命令，并内置冻结的验收门：

- 逐轮调用既有 `run_live_calibration`，强制 `execution_tier=provider-controlled-v1` 与冻结 thinking level，
  针对 one exact broker profile（默认 `profile:gemini-3-7-flash` + `low`）；
- 三重硬顶（per-case / per-round / campaign）在 campaign manifest 构建时做预留校验，运行期在每轮开始前
  复核已花费+预留不超 campaign cap，超过即 fail closed 停止后续轮次；
- 每轮从 durable records + score report 评估验收：0 false positive、0 high-severity miss、coverage 30/30、
  每个 record 的 ResultEnvelope 都带 `required_provider_controls=true` 与 schema hash、每个 WorkOrder 都绑定
  campaign 的 thinking level、无失败调用、无 schema 解析失败；round manifest 必须与 campaign 的 tier、
  thinking、profile、corpus hash、case 集合 exact 绑定；
- 生产门（`production_gate.eligible`）要求至少 3 轮全部通过且总花费在 cap 内；每轮失败即停止，不再花后续
  轮次的付费调用；
- `campaign.json` 与 `campaign-summary.json` 是可提交的 review evidence：包含逐轮成本、验收拒绝原因与最终
  裁决；支持跨轮 resume（manifest 不一致即拒绝）。

Campaign manifest 是 closed contract（schema 0.1）：rounds（1–10）、case_refs、三重 cap、token/timeout 预算、
tier、thinking level 全部进入 run identity。CLI 退出码：0 = 生产门通过，2 = 跑完但未达标，1 = fail closed。

## 边界

- 本模块自身绝不发起付费调用；`run_verifier_canary` 只有在 owner 显式提供 socket/auth 与 cap 时才逐轮调用
  既有 runner。测试全部为纯函数/临时目录验证，无网络、无付费。
- campaign 通过不改变 live route、gateway 或 ThesisVersion；production policy activation 与 gateway restart
  仍需单独批准。前置仍是 host 侧 Gemini profile providerControls/rate card/thinkingLevel 配置与 OpenClaw
  host patch safe restart——没有它们，第一轮第一个 case 会在 broker 处 fail closed 为
  `REQUIRED_CONTROLS_UNAVAILABLE`，campaign 以 failed 终止且只花费 0。
- 无 contracts/ JSON Schema 变化；无 Core/Schema SQL 变化。

## 验证

- 新增 `tests/test_thesis_impact_verifier_canary.py` 7/7：manifest 冻结与三重预留算术 fail closed、closed
  校验（tier/thinking/case_count/rounds/cap 篡改全部拒绝）、轮次评估（干净轮接受；FP/high miss/控制缺失/
  thinking 换绑/调用失败/解析失败/coverage 不全全部计数并拒绝）、生产门（<3 轮、任一轮拒绝、超 cap 均不
  eligible；3 轮全过且在 cap 内 eligible）、round 目录评估绑定 campaign 合同、无 resume 拒绝已存在输出目录。
- CLI `--help` 冒烟通过；全量 `unittest discover`：628/628 `OK`；`compileall`、显式 hermetic research replay
  canary（0 provider calls）、wheel/sdist build 与 `git diff --check` 全部通过。本轮没有付费调用。
