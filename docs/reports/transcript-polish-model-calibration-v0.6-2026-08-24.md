# TranscriptPolish 模型初轮校准 v0.6

日期：2026-08-24  
状态：初轮结果已落盘；Flash low 与 GLM 复测等待 safe gateway restart；未发布 transcript production policy

## 运行口径

四个 paid run 都绑定 Dalton commit `35ff3d1e05f58c8e0a3e0d958d800238456d5f92`、frozen corpus
`1fc70068eca00bbb980ffa6c81790f5fd5ef7b0b00ba48dd9bf4f30f08298346`、10 个 exact case、
12,000 input-token cap、4,000 output-token cap、180 秒 wall-clock cap。模型实际 thinking 为：

- Qwen DeepSeek V4 Flash 0731：`xhigh`；
- ZAI GLM 5.3：`high`；
- GPT-5.6 Luna：`xhigh`。

原始 run manifest、逐 case response、usage/cost 与 scorer 输出保存在 owner-only workspace temp 目录，没有提交模型原文。
仓库只保存汇总证据。

## 结果

### DeepSeek V4 Flash

前 9 个 case 全部通过 contract、conservation 与 quality gate。第 10 个多 span 长文本的 provider 请求达到 exactly
4,000 output tokens；gateway 日志显示 provider status 200、input 1,227、output 4,000、cost USD 0.00290994，但正文为空，
broker 因 `INVALID_HOST_RESULT: host returned invalid text` fail closed。Dalton 正式 usage 无法采纳这份未通过 broker
contract 的 telemetry，只对该 case 保留 USD 5 hard-cap reserve；前 9 个已正式记账 USD 0.00370172。

因此本轮是 9/10、safety 8/9，不是一次内容保真失败。按本机 DeepSeek 降级规则，截断且正文为空时不能继续提高 thinking
或先扩 output cap；只允许同一 case 用 `thinking=low` 重试一次。

### GLM 5.3

10 个 case 都在约 0.03 秒内返回 `HOST_COMPLETION_FAILED`，provider usage 全部为空。核对宿主配置发现 broker profile 已存在，
但 plugin `llm.allowedModels` 漏了 `zai/glm-5.3`；runtime 在 provider 前拒绝 model override。

这 0/10 只表示基础设施未准入，不能写成 GLM 模型成绩。runner 因 telemetry 不可用保守 reserve USD 50，但 provider call
为 0，不能把 reserve 当成实际花费。

### GPT-5.6 Luna

第一轮 9/10、safety 8/9、实际成本 USD 0.0061726、单 case 中位延迟 6.132 秒。失败 case 返回合法 JSON，但在第一个
source span 中把 5 组重复金额删成 4 组；Core 在 `segment 0 numeric expressions drifted` 拒绝。

独立第二轮 10/10、safety 9/9、实际成本 USD 0.0065446、中位延迟 6.025 秒。两轮合计 19/20；同一长文本一轮失败、一轮
通过，说明 Luna 能完成任务，但还没有达到两轮全通过的稳定性门槛。

## 当前裁决

- Planner 的 development policy 继续固定 DeepSeek V4 Flash；本轮不改 Planner 选择。
- TranscriptPolish 暂不发布 production 或 development exact policy。Luna 没有稳定胜过 Flash；GLM 尚未获得有效模型成绩；
  Flash 的唯一失败需要按规定做一次 low-thinking recovery。
- Core verifier 的价值已被真实验证：Luna 的输出 JSON 完全合法，但少了一组重复金额；没有独立 conservation gate 时，
  这类“读起来更顺”的删减会静默进入派生稿。

## 已准备的宿主修正

OpenClaw disk config 已做两项最小、可回退修正，并通过 `openclaw config validate`：

1. 把 `zai/glm-5.3` 加入 broker plugin 的 `allowedModels`；
2. 新增 calibration-only `profile:qwen-deepseek-v4-flash-0731-low-calibration`，固定同一 Qwen Flash 模型和
   `thinkingLevel=low`。

这两项在 safe gateway restart 前不会进入运行中 PID，不能写成已生效。restart 后只重跑 Flash 长 case一次，以及 GLM
完整 10 case；不得用 xhigh 重复消耗来掩盖 Flash 的截断，也不得把 GLM 之前的 host failure 计入模型质量。

## 边界

本轮没有写 live Core、Evidence、Claim、correction、Thesis 或 production routing；没有访问 AlphaEngine 或音频。模型
产物只在 owner-only calibration 目录，正式 Claim authority 仍是 raw coordinates 加 exact admitted correction lineage。
