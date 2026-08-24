# LLM Research Planner：GLM / Luna 复测与宿主 thinking 修正 v0.3

日期：2026-08-23  
状态：development calibration；OpenClaw safe restart 已排队，GLM 完整复测待宿主新进程

## 当前裁决

development-only Planner 继续使用 `profile:qwen-deepseek-v4-flash-0731`。GPT-5.6 Luna 在同一份
15-case frozen corpus 上得到 14/15，安全项 10/10；唯一错误是“股东回报”场景选择了
`cash-conversion`，而不是 gold action `shareholder-return`。本轮 Luna 成本为 USD 0.0058576，
单 case 中位耗时 3.191 秒，既没有达到 DeepSeek V4 Flash 的两轮 30/30，也没有在成本或延迟上形成优势。

Luna 本轮运行时仍继承旧宿主全局 `xhigh`。配置已把它改为显式 `xhigh`；该修改会在 safe restart 后生效，
但不会改变上述质量淘汰结论。

## GLM 5.3 为什么还不能计分

GLM profile 已固定为 `high`，broker 也把 `thinkingLevel=high` 交给了 OpenClaw
`runtime.llm.complete`。复查宿主实现后发现，OpenClaw 2026.7.1 的插件 completion 接口没有把该字段转成
底层 simple-completion 的 `reasoning` 选项；额外字段被静默忽略，模型仍继承全局 `xhigh`。因此第二次 broker
smoke 仍在约 0.3 秒内以 `HOST_COMPLETION_FAILED` 结束，没有产生 provider usage。这个结果属于宿主控制失效，
不能作为 GLM 质量分数。

本轮已补上可重复应用的 OpenClaw host patch：

- `runtime.llm.complete` 接受 closed `thinkingLevel` 枚举，并把它作为 exact `reasoning` 交给底层模型；
- runtime 公布 `thinkingLevel` capability 和可执行档位；
- 非法档位在 provider invocation 前拒绝；
- broker 只要配置了 profile-level thinking，就必须看到宿主能力并确认该档位可执行，否则启动时
  `INVALID_RUNTIME`，不再静默回退。

零付费 fake-provider 测试已经捕获 OpenAI Responses payload 中的 `reasoning.effort=high`，并证明
`ultra` 会在 provider 前被拒绝。OpenClaw config validation 与统一 patch `--check` 均通过，Node broker
25/25 通过。safe restart 因当前仍有 active embedded run 而按既定策略延后，没有强制中断。GLM 完整
15-case run 只能在新 gateway 进程加载 patch 后执行。

## Frozen corpus 与选择口径

本轮继续使用 corpus hash：
`124d3cf58f32196f399477d665f0f6a8f58dbdc0936d816ce06e7094f6e8fe1e`，统一上限为
12,000 input tokens、800 output tokens、300 秒。没有为任何模型放宽 action、schema、安全项或预算标准。

DeepSeek V4 Flash 仍是当前最合适的 Planner：两轮 30/30、安全项 20/20，两轮总成本
USD 0.00960668，单 case 中位耗时 2.018 / 2.002 秒。它只是 development policy v2 的 exact route，
尚未启用 production Planner worker，也没有改变 verifier route。

## 后续边界

1. safe restart 生效后，用 `profile:zai-glm-5-3`、显式 `high` 重跑同一 15-case corpus；
2. 除非 GLM 在 action、安全、成本和延迟上整体优于当前首选，否则不改 Planner policy v2；
3. 模型选择不再阻塞下一笔功能开发。按既定顺序，下一笔是 StatementSnapshot v1，并作为受限 probe
   接入现有 Bounded Planner Loop。

