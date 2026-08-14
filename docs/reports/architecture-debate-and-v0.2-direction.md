# Dalton Research Agent OS：架构辩论与 v0.2 收敛方向

> 日期：2026-08-13  
> 状态：架构讨论结论；不是实施规格，也不授权修改 Dalton live 系统  
> 输入：`vision-and-architecture-v0.1.md`、`fable-5-independent-review-v0.1.md`，以及 Eve 与 Fable 5 的三轮反方讨论

## 1. 本轮修正的前提

当前优先级不是尽快让 Dalton 交付更多研究，而是先把这个 agent 的核心架构设计正确。

这不等于一次实现完整系统。采用以下原则：

> **Architecture-first, implementation-thin**：先定义难迁移的语义、权威边界和事务契约，再用一条薄的端到端路径和 contract tests 验证；不为架构图的完整度提前实现所有 runtime、工作流和存储引擎。

Fable 上一轮把“不要现在建完整系统”扩大成了“不要现在设计完整边界”。本轮讨论后，Fable 撤回这一点，并承认：数据比代码长寿，语义比引擎长寿；错误的数据契约、Excel 权威和覆盖式更新会迅速形成迁移债。

## 2. 结论先行

1. **现有 Coverage OS 是迁移起点，不是目标架构边界。** 可以复用其数据和部分机制，但不能让现有 CLI、表结构或 Excel 文件格式决定长期 domain model。
2. **Research Ledger 与 Model Store 分离。** Ledger 记录我们相信什么、为什么改变、谁验证和批准；Model Store 记录数值、假设、计算、依赖、场景和校验。
3. **agent-native Model IR 是模型数字的唯一权威。** Excel 降级为人类审阅和交付的编译产物，不再与结构化模型形成双权威。
4. **Evidence 不直接挂 Thesis。** Claim 是证据与 thesis 之间的第一等研究记忆单元；Thesis version 引用 claims，evidence 以 supports / contradicts / qualifies 关联 claims。
5. **存储采用 hybrid temporal，不做 full event sourcing。** Immutable version rows 是内容权威；domain events 是时序权威；current pointers 和物化视图是可重建 projection。
6. **模型路由按 capability 和 policy 设计，不把 Planner、Researcher 等角色写死成 enum，也不绑定具体 model id。** 每次 invocation 记录实际模型、profile 版本、能力、成本和 provenance。
7. **双咽喉是逻辑边界。** 所有唤醒必须经过 scheduler policy boundary；所有正式 belief/model 写入必须经过 commit service transaction boundary。未来可以有多个 CLI、adapter 或进程，但不能绕过这两层。
8. **具体数据库、runtime 和工作流引擎暂不冻结。** SQLite、DuckDB、Parquet、Postgres、Temporal、OpenClaw 或第二 runtime 都是可替换实现；先冻结语义契约。

## 3. 权威边界

```text
Sources / raw artifacts
        │
        ▼
Evidence ── supports / contradicts / qualifies ──► Claim versions
                                                        │
                                                        ▼
                                                Thesis versions
                                                        │
                                                        ▼
                                                  Decisions / views

Model inputs ──► Model IR computations ──► materialized outputs
                        │                         │
                        └──── lineage/delta ─────┘
                                  │
                                  ▼
                         Thesis / valuation commit
                                  │
                                  ▼
                         Excel / report exporters
```

### 3.1 Research Ledger

保存：

- mandates、governance policies 和 priority overrides；
- questions、work orders、runs 和 agenda decisions；
- evidence、claims、thesis/falsifier versions；
- verification、adjudication 和 commit records；
- model/profile invocation、usage、成本和 incidents；
- immutable domain events、artifact refs 和 content hashes。

Ledger 回答：

- 我们目前相信什么；
- 旧观点是什么；
- 哪条证据支持或反对哪项 claim；
- 哪次模型调用产生了什么；
- 谁验证、谁批准、为什么改变；
- 当前状态如何从不可变版本重建。

### 3.2 Model Store / Model IR

保存 agent 需要读取、计算、比较和更新的结构化金融模型。它不是 Excel 单元格的镜像，也不应与 Ledger 混成一张万能表。

每个 datapoint 至少包含：

- entity / metric；
- period、fiscal calendar；
- scenario 和可扩展 dimensions；
- value、unit、currency、scale、basis；
- `value_kind`；
- evidence refs 或 assumption provenance；
- computation ref 和 dependency refs；
- model version、prior version 和 delta；
- quality、uncertainty 和 validation status。

`value_kind` 采用：

- `observed`：有外部来源可核的观测；
- `assumption`：人或 agent 明确提出、待检验的假设；
- `derived_deterministic`：可由声明的计算和输入复算；
- `estimate`：模型或方法产生的估计；
- `simulation`：带随机性或分布语义的模拟结果。

`estimate` / `simulation` 必须记录 method、seed（如适用）、uncertainty、model/code provenance。依赖这些值的下游结果必须保留 taint/uncertainty lineage，不能经过一道公式后伪装成纯确定性结果。

### 3.3 Artifact Store

保存：

- 原始 filing、访谈、PDF、网页快照；
- 大型数据文件；
- 研究报告、wiki 和图表；
- 导出的 Excel；
- 代码包、测试夹具和其他大对象。

Ledger 只保存 path/URI、hash、版本、MIME、来源和 lineage，不把大型原文塞进关系库。

## 4. Excel 的新定位

Excel 不再是模型数字的 canonical source，而是 exporter 的目标格式。

### 4.1 为什么不继续让 Excel 做权威

- agent 难以稳定解析公式语义、依赖和口径；
- diff 与版本审计弱；
- 依赖特定计算引擎；
- 来源和计算 lineage 难与 cell address 分离；
- 自动验证和跨模型查询成本高；
- Excel 与数据库镜像会形成双权威漂移。

### 4.2 导出原则

- 常见、可声明的计算导出为可交互 Excel 公式；
- 复杂 SQL/code 计算可导出已物化值，但必须附 lineage/audit sheet；
- assumptions、observed、estimate、simulation 用明确样式和注释区分；
- 导出文件携带 IR version、content hash、generated_at 和 exporter version；
- 人类在 Excel 中的修改不直接回写 canonical state，只能转成 `proposed assumption change`，经 verifier 和 commit gate 进入 IR。

## 5. Computation Contract

不采用“任意 Python 脚本就是公式”的设计，否则只会把 Excel 黑箱换成 Python 黑箱。计算分三层：

### Tier 1：Declarative expression

- 封闭、版本化、可解析的表达式 AST；
- 用于常见财务计算；
- evaluator 可以独立复算；
- exporter 可以编译为 Excel 公式；
- operator registry、版本机制和扩展流程现在定义；
- v1 的具体算子集在现有万华及行业模型 formula census 后通过 ADR 冻结，不凭讨论猜测。

### Tier 2：Restricted SQL transform

- 用于表级 reshape、aggregation 和 window calculation；
- 声明 inputs、outputs、dimensions、units 和 checks；
- SQL 与执行环境版本化；
- 输出物化并记录依赖。

### Tier 3：Exceptional sandboxed code

- 只处理 Tier 1/2 不适合的少数逻辑；
- 必须记录 code hash、environment hash、declared I/O、determinism、checks 和 justification；
- 输出物化；
- Tier 3 使用占比是常设可观察指标，防止例外通道成为默认实现。

Scenario 属于数据层维度。公式默认 scenario-blind；未覆盖的 assumption 是否回落到 base，由版本化的 scenario resolution policy 决定。

## 6. Claim、Evidence 与 Adjudication

Claim 是 agent-native research memory 的最小可审计断言，但不建设通用知识图谱或强制 SPO 本体。

Claim 至少包含：

- stable id 和 immutable versions；
- subject/entity ref；
- metric/aspect；
- period；
- basis/口径；
- normalized statement；
- status projection。

只强制把以下断言 claim 化：

- 被 thesis version 引用；
- 被 commit 用作依据。

背景叙述和过程笔记不要求全部拆成 claims。

Claim status 不能靠“有两个来源就 corroborated”这类简单计数推导。系统必须保留：

- evidence relation；
- source lineage；
- independence group，识别两个二手来源是否实际同源；
- immutable adjudication versions；
- adjudicator invocation provenance。

同 subject / metric / period / basis 下的精确数值冲突可以确定性标记为 contested。定性冲突由 verifier 形成 adjudication version。与正式 commit 相关的 adjudication 也必须满足对应 governance policy 的独立性要求。

## 7. Hybrid Temporal 与事务边界

不使用 full event sourcing。权威分工如下：

- immutable version rows：内容权威；
- domain events：时序权威，只引用 version id、content hash、actor、usage refs 和 idempotency key；
- current pointers / materialized views：可重建 projection。

三段事务：

### T-stage

原子写入 staging state 和 `staged` event。Staging 可修改，因为它不是正式权威。

### T-verify

原子写入 immutable verification/adjudication record 和 `verified` event，包括 verifier invocation provenance、verdict 和 findings。

### T-commit

一次原子事务完成：

1. 按已生效 governance policy 检查 required verification、independence predicate 和权限；
2. 插入新的 immutable version rows；
3. 插入 `committed` event，引用 staging、verification、version 和 hashes；
4. 更新 current pointer；
5. 消费并校验 idempotency key。

任一环节失败，整个事务回滚。语义字段不得被原地 UPDATE 覆盖；存储层应阻止绕过 commit boundary 的写入。

## 8. LLM 与执行模型架构

不把具体 model id 写进 domain architecture，也不把 Planner / Researcher / Verifier / Coder / Summarizer 固化成 enum。

冻结的是 `ModelProfile` / capability policy schema：

- capabilities；
- context 和 modality requirements；
- tool/data permissions；
- cost/latency class；
- provider/model family；
- determinism；
- allowed side effects；
- independence attributes；
- profile version。

角色只是可配置 profile。一次 work order 可以组合多种 capabilities。

每次 model invocation 都记录：

- profile id/version；
- actual provider/model id；
- capability in use；
- input/output artifact refs；
- tokens、费用、时长；
- session/run ref；
- side effects；
- parent/child provenance。

Commit gate 检查的是产生被验证物的 invocation 与执行验证/adjudication 的 invocation 是否满足版本化的 independence predicate，而不是只看它们是否叫不同角色。

第一版 router 可以只是静态配置查表；现在不做动态成本优化、自动 benchmark 选择或 runtime capability negotiation。

## 9. Capability Growth Plane

Dalton 的自我增长包含两条同等重要的路径：

1. **认知增长**：积累 evidence、claims、thesis、模型、行业结构和历史校准；
2. **能力增长**：发现重复劳动和能力缺口后，自己设计、编写、测试、评估并迭代可复用工具。

能力增长不能只靠“能写代码”的模型，也不能等同于修改 live skill。它需要独立的 `Capability Registry`、生命周期和治理边界。

### 9.1 能力对象分类

- **Ephemeral transform**：某个 work order 内临时生成的转换、解析或计算代码；在 sandbox 运行，随 run 归档，不自动注册为长期能力。
- **Deterministic tool**：可复用的 parser、formatter、calculator、data normalizer、exporter 或 validator；有结构化 I/O、fixture、版本和性能记录。
- **Connector / adapter**：连接外部数据源、模型 provider、runtime 或投递渠道；额外记录认证、权限、速率限制和副作用。
- **Skill / playbook**：告诉 agent 何时用什么工具、如何处理例外和怎样验收的工作流知识；不与底层 executable tool 混为一谈。
- **Model / runtime profile**：能力、成本、权限和适用范围的版本化配置。
- **Core / policy change**：改变 scheduler、commit gate、权限、预算或治理规则的改动；Dalton 可以提出，但不能自行批准或生效。

### 9.2 能力增长循环

```text
重复劳动 / 失败 / 人工修复 / 性能瓶颈
                  │
                  ▼
           Capability Gap
                  │
                  ▼
       Proposal + expected benefit
                  │
                  ▼
       Sandbox build + static checks
                  │
                  ▼
      Fixtures / historical replay / eval
                  │
                  ▼
       Independent verification
                  │
                  ▼
       Promotion policy / approval
                  │
                  ▼
      Capability Registry + monitoring
                  │
                  └──► regression / rollback / new proposal
```

`Capability Gap` 可以来自：

- 同类临时代码或人工步骤重复出现；
- 某类数据长期需要格式化、清洗或对账；
- tool/provider 反复失败；
- 现有工具成本、延迟或准确率不合格；
- verifier 发现稳定的错误模式；
- 研究问题因缺少特定 parser、计算器或数据连接而阻塞。

每个 proposal 至少声明：

- 要解决的 gap 和触发证据；
- 预期调用频率、节省时间/成本和质量改善；
- 输入、输出、错误和 side-effect contract；
- 权限、网络、依赖和凭据需求；
- fixtures、baseline、eval 和停止条件；
- builder、verifier、approver 和 rollback owner；
- 是否替代旧 capability，及兼容策略。

### 9.3 自我修改边界

- Dalton 可以自主生成临时代码，并在无外部副作用的 sandbox 中执行；
- Dalton 可以自主创建、修订和测试 tool/skill proposal；
- builder 不能批准自己写的 capability；
- promotion 必须经过已生效的治理政策和独立 verifier；
- 当前治理默认要求人类批准任何 reusable capability 进入 live registry；
- 权限、凭据、外发、采购、core runtime 和 governance policy 变更始终由人批准；
- 所有运行必须记录 capability version、code/content hash、依赖锁、执行环境和实际 side effects；
- registry 持有可回滚的 active pointer，历史版本不可覆盖。

长期可以按风险政策给纯本地、确定性、无网络、无外部写入的低风险工具有限自动晋级，但这属于 governance policy，不是架构默认值。

### 9.4 如何衡量能力是否真的增长

- 同类任务从临时代码转成复用 capability 的比例；
- capability 的实际复用次数，而不是 proposal 数量；
- 与 baseline 相比的准确率、成本和耗时；
- regression、rollback 和 incident 数；
- capability gap 从发现到可用版本的时间；
- 长期无人使用的 capability 数和依赖负债；
- 未经授权扩大权限或副作用的次数必须为零。

## 10. Runtime Topology

OpenClaw 不应成为 Dalton 的日常控制 runtime。它功能完整，但同时带着频道、插件、agent session、通用工具、模型路由和多种产品级能力；让每个日常检查和小型计算都经过整套环境，会增加启动成本、状态耦合和运维面。

目标结构分三层：

### 10.1 Dalton Core：轻量控制 runtime

Dalton Core 是 headless、event-driven 的长期控制面，但不保持一个长期 LLM session。它负责：

- event inbox、timers 和到期唤醒；
- scheduler policy boundary；
- question backlog 和 agenda cycle；
- work order 创建与状态机；
- budget、pause、lease、retry 和 idempotency；
- Research Ledger、Model Store 和 Capability Registry 的事务入口；
- ModelProfile / RuntimeProfile 静态路由；
- verifier、adjudication 和 commit service boundary；
- usage、audit、incident 和 health telemetry。

Core 每次只在需要判断、研究或验证时启动短生命周期 model invocation 或 worker。没有事件、没有到期工作时，它只维护状态和 timer，不消费推理 token。

第一版优先使用现有 Python 生态做一个小型 headless service / CLI package，加单机 SQLite 和短生命周期 worker。这里是实施起点，不是永久技术绑定；v0.2 先冻结行为契约，不先选 Temporal 或重写成分布式系统。

### 10.2 Execution runtimes：按 work order 选择

“Dalton 用什么 runtime”不是单选题。Core 为每个 work order 根据 `RuntimeProfile` 选择最轻且满足要求的执行器：

- 本地确定性进程：SQL、Model IR evaluator、parser、formatter、exporter；
- 轻量 LLM invocation：普通提取、分类、问题生成和摘要；
- sandboxed builder：编写和测试临时代码或 capability proposal；
- 高能力研究 worker：长上下文、复杂工具调用和深度研究；
- 并行/迭代 harness：只有任务确实需要 dynamic workflow 或 fresh-agent iteration 时启用；
- OpenClaw adapter：调用当前只有 OpenClaw 暴露的 connector、skill 或 channel 能力。

`RuntimeProfile` 至少声明：

- capabilities 和 isolation level；
- startup overhead、latency 和 cost class；
- allowed tools、network、filesystem 和 side effects；
- max wall time、checkpoint/retry 能力；
- persistence、concurrency 和 cancellation semantics；
- supported input/result envelope versions；
- runtime version 和 environment hash。

第一版 router 可以是显式规则：确定性任务优先本地 executor；简单模型任务优先轻量 invocation；只有满足已声明需求时才升级到昂贵 runtime。动态 benchmark 路由以后再做。

### 10.3 OpenClaw：Human Bridge 与可选 adapter

OpenClaw 保留以下职责：

- Discord、飞书等人机入口；
- 人类 mandate、priority override、暂停和审批操作；
- 研究结果、incident 和 approval request 的投递；
- 当前 OpenClaw 已接入工具和 connector 的兼容 adapter；
- 运维查看、紧急停止和手工干预入口。

OpenClaw 不再承担：

- Research Ledger 或 Model IR 的权威状态；
- Dalton 的日常 agenda loop；
- 默认 scheduler、commit gate 或 capability registry；
- 每个 work order 的默认执行环境；
- 只有聊天 transcript 才存在的记忆。

桥接协议应是双向、幂等的 command/event envelope。OpenClaw 向 Core 发送带 actor、权限、TTL 和 idempotency key 的 mandate/override/approval；Core 向 OpenClaw 发送 deliverable/alert/approval request。OpenClaw 掉线时，Dalton 的本地研究循环继续运行，消息进入 outbox，恢复后补投。

### 10.4 Runtime 解耦的验收条件

- 关闭 OpenClaw 后，Dalton Core 仍能处理 timer、agenda、确定性 work order、model invocation、verification 和 commit；
- OpenClaw 恢复后可补收事件和 deliverable，不重复提交或投递；
- 同一 work order 可以更换 runtime adapter，而不迁移 research state；
- runtime 失败不会直接破坏 Ledger、Model Store 或 current pointer；
- Core 的 pause/budget/commit policy 对所有 runtime 一致生效；
- 每次执行都能追溯到 RuntimeProfile、runtime version、capability version 和 invocation provenance。

## 11. 现在设计、薄实现、以后再决定

### A. 现在完整设计的难迁移语义

- stable identity、semantic key、dedup 和 append-only version rules；
- evidence / claim / thesis / falsifier 关系；
- Model IR datapoint、computation、scenario 和 uncertainty contract；
- staging / verification / commit 语义；
- hybrid temporal 与事务原子性；
- domain events 和 idempotency；
- ModelProfile、invocation provenance 和 independence predicate；
- governance policy schema、版本、生效区间和变更审计；
- scheduler policy boundary 与 commit service boundary；
- pause、预算和权限的 enforcement points；
- capability identity、version、proposal、eval、promotion 和 rollback contract；
- RuntimeProfile、execution envelope 和 bridge command/event envelope；
- artifact manifest 与 lineage。

### B. 现在定义接口，只做薄实现

- Dalton Core headless control loop；
- OpenClaw human bridge 和兼容 execution adapter；
- work order / result envelope；
- 静态 model router；
- 静态 runtime router 和本地 deterministic executor；
- 到期触发、question backlog 和 agenda selection interface；
- commit service；
- Model IR Tier 1 evaluator 和 Excel exporter 的最小路径；
- Tier 2 SQL executor；
- Capability Registry、proposal sandbox 和 tool/skill/run version tracking；
- schema migration、reconciliation 和 rollback tooling。

### C. 真实负载出现后再决定

- 第二个复杂 worker runtime 和完整 adapter lifecycle；
- checkpoint / heartbeat / resume / streaming partial result；
- Temporal / Postgres；
- DuckDB / Parquet 是否成为物理层；
- 动态模型路由和 capability negotiation；
- 知识图谱；
- skill 自动晋级和 canary；
- 信息增益数值化；
- 并行 worker 池；
- 通用公式 DSL；
- 高维 scenario 系统。

## 12. 验收原则

架构语义不能只存在于文档。采用两级验收：

### E1：真实闭环

用于事务原子性、权威切换和多组件时序，包括：

- scheduler/commit 两个 policy boundaries；
- gate 的 pass 和 reject；
- thesis/model immutable version chain；
- evidence → claim → thesis version；
- invocation usage/provenance；
- Tier 1 计算与 Excel 公式导出；
- human override + TTL；
- commit 回滚和幂等；
- capability gap → proposal → sandbox eval → rejected/approved registry version；
- OpenClaw 断开时 Core 继续运行，恢复后 outbox 幂等补投。

### E2：版本化 contract tests / fixtures

用于纯函数语义，包括：

- claim conflict；
- source independence grouping；
- scenario fallback；
- value_kind 和 uncertainty taint；
- Tier 2/3 contract；
- independence predicate；
- revise 上限与 blocked；
- seeded-error variants；
- capability permission escalation rejection；
- RuntimeProfile routing 和 adapter substitution。

A 层契约不得只有文档、没有 E1 或 E2 验证物。尚未准备验证的语义降到 C 层，不伪装成已冻结契约。

## 13. Governance 与架构的边界

以下内容属于架构不变式：

- 放权前必须存在已生效、已版本化的 governance policy；
- gate 必须执行该 policy；
- policy 变更必须保留 actor、reason、时间和 prior version；
- 不允许通过 prompt 或手工改表绕过 policy。

以下内容不是架构不变式：

- 议程认可率阈值；
- verifier 检出率阈值；
- 人工 commit 的期限；
- 日/月预算；
- 四池比例；
- 哪类 decision 可以自动 commit。
- 哪类 reusable capability 可以自动晋级；
- 哪类 work order 可以使用昂贵或有外部副作用的 runtime。

这些具体值由用户拍板，进入可版本化 policy。此前提出的 75%、90% 和 12 个月只保留为候选值，不作为本轮共识。

## 14. 与 Fable 最终共识和保留意见

### 共识

- Architecture-first 与 thin implementation 可以同时成立；
- Model IR canonical，Excel 是编译产物；
- Claim 进入核心语义；
- hybrid temporal 优于 full event sourcing；
- ModelProfile 按 capability/policy 设计；
- 双咽喉是逻辑 policy boundaries；
- 估计/模拟的不确定性必须沿 lineage 传播；
- 架构契约必须有真实闭环或 contract test 验证。

### 保留意见

- 不在讨论阶段冻结 Tier 1 具体算子；先做 formula census；
- 不把任何未经用户批准的阈值和期限写成不变式；
- 不把 non-deterministic output 错标为 observed；
- 不用简单证据计数决定 claim status；
- 不在架构阶段直接原地迁移 live database。正式实施应先设计 versioned migration、reconciliation、cutover 和 rollback，再决定原地或并行迁移。

## 15. 下一步应产出的设计物

1. **v0.2 semantic spec**：实体、关系、版本、事务、policy、IR 和 invocation schema。
2. **formula census**：统计现有万华及行业模型的公式类型、依赖、维度、scenario 和 Excel 特性，判断 Tier 1/2/3 覆盖率。
3. **Model IR ADR**：根据 census 冻结 v1 operator registry、scenario resolution 和 exporter contract。若 Tier 1 覆盖率明显低于预期，调整算子集还是调整三层 computation contract，必须回到架构评审共同裁决，不能由实现期单方扩展算子。
4. **migration ADR**：定义旧 Coverage OS / Excel 到新 schema/IR 的映射、校验、双跑、cutover 和 rollback。
5. **walking skeleton spec**：只定义要行使的 E1 契约和 E2 fixtures，不以短期研究产出作为验收目标。
6. **Capability Growth spec**：定义 gap、proposal、sandbox、eval、promotion、registry、monitoring 和 rollback。
7. **runtime/bridge ADR**：定义 Dalton Core 最小职责、RuntimeProfile、execution envelope，以及 OpenClaw bridge 的 command/event/outbox 协议。

Runtime 的 build / embed / adapt 初步裁决与 spike 设计，见 `runtime-options-and-build-vs-adopt.md`。

本轮没有修改 Dalton live 代码、数据库、cron、模型或 Excel。
