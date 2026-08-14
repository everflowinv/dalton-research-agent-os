# Dalton Runtime：DeepSeek Harness、Pi 与自研边界

> 日期：2026-08-13  
> 状态：runtime ADR 讨论稿；结论需通过 spike 验证  
> 目标：判断哪些现有 runtime 理念值得吸收、哪些实现可以直接复用、哪些部分必须由 Dalton 自研

## 1. 决策原则

不在 OpenClaw、DeepSeek Harness、Pi 或自研之间做整套单选。

先按职责拆开：

1. **Domain kernel**：研究状态、议程、模型、能力增长、治理和 commit；
2. **Agent loop**：模型请求、工具调用、上下文投影、取消和事件；
3. **Execution runtime**：进程、sandbox、subagent、workflow 和恢复；
4. **Human bridge**：聊天、审批、投递、告警和运维。

每一层单独判断 build / embed / adapt。外部 runtime 必须适配 Dalton 的 domain contract，不能反过来让 Dalton 的研究状态迁就其 session、plugin 或 workspace 数据结构。

## 2. 当前建议

- **Dalton Core：自研。** 这是唯一掌握 Research Ledger、Model Store、Capability Registry、scheduler policy boundary 和 commit transaction boundary 的控制面。
- **Pi agent core：优先作为轻量 agent-loop 候选。** 适合嵌入或作为短生命周期 worker 进程，不作为研究状态宿主。
- **DeepSeek Harness：作为 execution adapter 候选和架构素材。** 先借 capability seam、typed event、reversible lifecycle、fresh-agent handoff 等机制；不直接采用其完整 plugin tree 作为 Dalton Core。
- **Prime Agent：作为长上下文/能力构建实验 runtime。** 借 persistent Python data plane、RLM 和 refinement proposal；不作为默认日常控制面。
- **OpenClaw：Human Bridge + connector adapter。** 不再做 Dalton 的默认 runtime。
- **薄 agent loop 自研：保留退路。** 如果 Pi 的语言栈、session 语义或扩展机制仍产生不必要耦合，自研一个只支持 Dalton invocation contract 的小循环。

这不是最终产品选型。最终决定取决于同一组 spike 和可替换性验收。

## 3. DeepSeek Harness

### 3.1 值得吸收的部分

截至 2026-08-13，DeepSeek Harness `0.1.0-rc.5` 仍标注为 developer preview，并明确提醒将出现兼容性破坏。其当前主线采用 Cordis，核心理念是 everything is a plugin。

以下机制值得吸收：

- **Capability seam**：把能力拆成 Service Definition、Provider 和 Consumer；filesystem、shell、sandbox、subagent 等实现可以替换，consumer 不需要跟着改。
- **Typed events + reversible effects**：插件注册和副作用随生命周期卸载，适合 runtime adapter、临时工具和隔离 profile。
- **Profile / bundle composition**：通过配置组合能力，而不是不断修改 agent loop。
- **Durable session event 与 projection**：模型看见的内容必须能从日志重建；这适合 invocation audit 和 replay。
- **Tool execution pipeline**：pre-execute / execute / post-execute 分层，适合权限、审计和结果归一化。
- **Fresh-agent Ralph handoff**：每轮 fresh child 不继承父对话，只共享 workspace 和一份有界结构化 handoff；能降低长会话污染。
- **Workflow 与 subagent provider seam**：同一 orchestration contract 可以替换 in-process、ACP、Codex 或其他 provider。
- **Headless + Python SDK / JSON-RPC**：可以把 Harness 当 worker 子进程使用，而不是把整个 Dalton 搬进去。

### 3.2 不应照搬的部分

**“没有 privileged core，所有东西都是插件”不适合 Dalton 的治理核心。**

Dalton 必须有不能被普通 runtime 插件替换或卸载的强边界：

- scheduler policy enforcement；
- commit gate；
- immutable version semantics；
- governance policy；
- capability promotion 权限；
- audit 与 idempotency。

这些属于 domain kernel，不应成为和 UI、tool provider 同等级的任意插件。

DeepSeek Harness 当前 workflow 官方限制也说明它还不能直接做 Dalton Core：

- workflow 只支持前台收割；
- 没有 journaling 或 process-resume；
- 没有 saved/nested workflow；
- 没有跨 child 的 token budget 词汇；
- Ralph 没有独立 evaluator，完成状态由 worker 自报；
- Ralph 没有后台 job、scheduler、checkpoint、费用或墙钟预算。

这些限制不妨碍它作为 bounded execution adapter，但不满足 Dalton 的长期控制面要求。

### 3.3 对 Dalton 的定位

- 借它的 seam 设计语言；
- 借 typed event、provider replacement、lifecycle cleanup 和 fresh-agent handoff；
- 用 Python SDK/headless profile 做一次 bounded work order spike；
- 不把 session log 当 Research Ledger；
- 不把 Cordis plugin tree 当 governance kernel；
- 不依赖尚未实现的 workflow resume 和 evaluator。

## 4. Pi

### 4.1 值得吸收的部分

Pi 当前把产品拆成：

- `pi-ai`：多 provider 模型接口；
- `pi-agent-core`：stateful agent loop、tool execution 和 event streaming；
- `pi-coding-agent`：session、compaction、extensions、skills、RPC/SDK 和终端工具。

`pi-agent-core` 对 Dalton 有直接价值：

- 小而清楚的 agent loop；
- `AgentMessage → transformContext → convertToLlm`，允许 domain event 与模型消息分离；
- tool schema、parallel/sequential execution；
- `beforeToolCall` / `afterToolCall` policy hooks；
- `shouldStopAfterTurn`，方便 Core 用有界 invocation 控制继续或停止；
- steering、follow-up、abort 和 event stream；
- provider/model 与 agent loop 分离；
- session backend 可以独立替换。

Pi 的 coding agent 同时支持 print/JSON、RPC 和 SDK，适合作为嵌入式 worker，而不是只能在 TUI 中使用。

### 4.2 不能替代 Dalton Core 的原因

- Pi 的中心仍是 message/session state，不是 research question、claim、thesis、model IR 和 commit；
- 没有 agenda、durable work-order scheduler、governance policy 或 capability promotion；
- 官方明确说明 Pi 没有内建 filesystem/process/network/credential 权限系统，默认继承启动用户权限；
- coding-agent 默认围绕项目文件和 shell 工作，不理解投资研究事务；
- session branching 和 compaction 不能替代 immutable belief/model versions；
- extension/skill 安装也不能替代 capability registry、独立验证和 promotion policy。

### 4.3 对 Dalton 的定位

Pi 是目前最合适的**agent-loop library 候选**，不是完整 runtime 选型。

两种接法都保留：

1. TypeScript worker service 嵌入 `pi-agent-core`，Dalton Core 通过 invocation envelope 调用；
2. 只借 Pi 的 event/tool-loop 设计，用 Python 实现一个更小的 Dalton-native loop。

是否直接用 Pi，取决于 spike 证明其 session 语义、语言边界和权限外置成本是否低于自研小循环。

## 5. Prime Agent / Pi 衍生 runtime

Prime Agent 在 Pi 之上增加：

- persistent IPython data/control plane；
- prompt-as-variable 和 RLM fresh subagents；
- daemon、detach/reattach、goals、heartbeats 和 schedules；
- bounded autonomous mode；
- Continual Harness refinement state。

对 Dalton 最有价值的是：

- **Context as data**：大批 filing、研报和表格留在 Python/data plane，模型只读取筛选结果；
- **Persistent analytical workspace**：DataFrame、索引和中间计算不必反复塞进 context；
- **Refinement ledger**：把反复出现的经验转成小型、有证据、可回滚的 capability proposal；
- **Fresh recursive workers**：主模型通过程序分派切片后的任务。

但它不适合作为默认 Core：

- 官方仍说明 worker/kernel 只是生命周期隔离，不是安全 sandbox；
- persistent REPL/session 容易成为第二套隐含权威；
- Continual Harness 管的是补充 prompt/memory/skill 描述，不等于 Dalton 的 executable capability promotion；
- coding/research session 的目标和 Dalton portfolio agenda 不同；
- 它比 Pi agent core 更重，也与 Core 的 queue、schedule 和 goal 职责重叠。

合适定位是 builder、长上下文研究或 capability-proposal 实验 runtime。

## 6. 哪些必须自研

以下部分不应交给任何通用 harness：

- mandate、coverage policy 和 priority override；
- research question 与 agenda decision；
- evidence / claim / thesis / falsifier 语义；
- Model IR、scenario、uncertainty 和 Excel export contract；
- capability gap、proposal、eval、promotion、registry 和 rollback；
- scheduler policy boundary；
- T-stage / T-verify / T-commit；
- invocation independence 和 commit policy；
- domain events、idempotency、usage 和 cost ledger；
- OpenClaw bridge 的 command/event/outbox 语义。

这是“重写 Dalton Core”的范围。

## 7. 哪些不值得重写

除非现有实现无法满足 contract，不自行重造：

- 各模型 provider 的 HTTP/streaming/OAuth 客户端；
- MCP 协议和现有 connector；
- SQLite 引擎；
- 通用 JSON Schema / validation；
- container、micro-VM 或远程 sandbox；
- 文档解析、浏览器抓取和金融数据连接器；
- Discord/飞书渠道；
- 基础 tracing、logging 和 metrics exporter。

自研应集中在真正形成 Dalton 差异和权威性的部分，而不是重复造基础设施。

## 8. 三个必要 spike

三个 spike 在 Core 的 versioned contract 和首条 commit skeleton 验收后再开始。第一批实现先做 deterministic executor 与 stub worker，把 Dalton-native thin loop 作为 contract 基准；不能为了比较候选 runtime 反过来修改 domain contract。

### Spike A：轻量 invocation

同一项结构化抽取/格式化 work order 分别用：

- Pi agent core；
- DeepSeek Harness headless/Python SDK；
- Dalton-native thin loop。

比较启动时间、常驻内存、token、事件完整性、工具权限、取消、结果 envelope 和实现代码量。

### Spike B：研究与独立验证

完成一项带 2–3 个工具的真实研究任务，并由不同 invocation 验证：

- runtime 只接触 staging；
- Core 持有 evidence/claim/thesis 和 commit；
- 更换 runtime 后不迁移 domain state；
- runtime 自报 complete 不等于 gate pass。

### Spike C：故障与能力增长

- 运行中 kill worker/runtime；
- 验证 lease/retry/idempotency；
- OpenClaw 离线时 Core 继续工作；
- 触发一个 formatter capability gap；
- 生成工具 proposal、跑 fixtures、拒绝权限升级、人工批准或驳回；
- 恢复后 outbox 不重复投递。

## 9. 决策门槛

只有候选满足以下条件，才进入默认 worker 路径：

- domain state 完全外置；
- 可用稳定 envelope 启动、取消和收割；
- 权限可在 runtime 外强制执行；
- 每次 invocation 可追溯 model/runtime/capability 版本和 usage；
- kill 后不会破坏正式状态；
- 替换 adapter 不改 Core schema；
- 没有 session/plugin state 绕过 commit boundary；
- 总复杂度明显低于 Dalton-native thin loop。

若没有候选通过，采用自研 Dalton-native loop。这不是失败，而是已经把重写范围限制在最小必要层。

## 10. 当前裁决

现阶段不把 DeepSeek Harness、Pi coding agent 或 Prime Agent 任一项目指定为 Dalton 的日常 runtime。

更准确的方向是：

```text
OpenClaw Human Bridge
          │
          ▼
     Dalton Core                 ← 自研、权威
          │
          ├── deterministic executor
          ├── Pi-based lightweight worker      ← 首选 spike
          ├── Dalton-native thin loop           ← 基准与退路
          ├── DeepSeek Harness adapter           ← workflow/plugin spike
          └── Prime/RLM experimental worker      ← 长上下文与能力构建
```

先让候选实现竞争同一个 `RuntimeProfile + WorkOrder + ResultEnvelope` contract，再按实测决定保留哪一个。不能通过 contract 的 runtime 不进入 Core，无论其功能多完整。

## Sources

- DeepSeek Harness README（developer preview）：https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md
- DeepSeek Harness architecture：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md
- DeepSeek workflow contract 与限制：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/workflow/workflow/README.md
- DeepSeek Ralph contract 与限制：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/workflow/tool-ralph/README.md
- DeepSeek Python SDK：https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md
- Pi repository：https://github.com/earendil-works/pi
- Pi agent core：https://github.com/earendil-works/pi/blob/main/packages/agent/README.md
- Pi coding agent：https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md
- Prime Agent：https://github.com/PrimeIntellect-ai/prime-agent

本文件没有授权安装候选 runtime，也没有修改 Dalton live 系统。
