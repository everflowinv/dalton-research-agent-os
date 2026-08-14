# Dalton Research Agent OS：愿景与架构规划 v0.1

> 状态：讨论底稿；独立架构审阅已完成，见 `fable-5-independent-review-v0.1.md`
> 日期：2026-08-13
> 范围：定义我们想实现什么、为什么现有 Dalton 不够、目标系统应如何运行，以及从现有 Coverage OS 演进的路径。本文件不是实施规格，也不绑定某个模型或 agent harness。

## 1. 我们想实现什么

Dalton 的目标不是成为一个等待人派任务、再把任务做完的聊天 agent，而是成为一名持续覆盖行业和公司的自主研究分析师。

人类只负责：

1. 确定总目标和边界，例如研究什么行业、从哪些公司开始、投资期限和风险偏好；
2. 校正航向，例如临时要求优先研究某家公司、某个事件或某条产业链；
3. 基于 Dalton 已积累的数据、模型、证据和 thesis 与它讨论；
4. 阅读最终研究成果，决定是否采取投资行动。

Dalton 应自主完成：

1. 形成研究议程，决定当前应先做首次覆盖、业绩跟踪、新闻分析、模型更新还是 thesis 验证；
2. 按优先级、信息增益、紧迫性、成本和风险安排工作；
3. 把研究问题拆成可执行计划，自主编排工具、脚本、skills 和其他 agent；
4. 持续积累证据、模型和行业认知，而不是每轮从聊天上下文重新开始；
5. 检验支持和反对投资 thesis 的证据，根据边际变化调整判断、置信度、估值和下一项验证；
6. 发现重复劳动或能力缺口后，提出、编写、测试并迭代 skill；
7. 独立检查研究质量，失败时生成针对性返工，而不是把“写出文档”当成完成；
8. 完成一轮工作后重新规划，继续下一项高价值研究或进入休眠，等待事件触发。

“7×24 自主运行”不表示让模型永远保持一个活跃 Session，也不表示无休止搜索。它表示系统一直在线，状态可恢复，事件随时进入，agent 以有界的工作周期反复执行：

```text
感知 → 排序 → 计划 → 执行 → 验证 → 提交认知变化 → 重新规划 → 继续或休眠
```

## 2. 当前 Dalton 已有的基础

Dalton v1 已经不是空白聊天 agent。现有 Coverage OS 提供了可继续利用的骨架：

- 独立 chem agent、workspace、模型路由和 fallback；
- 公司 coverage registry 与 tier；
- SQLite Coverage Ledger；
- 公司、事件、thesis、证据、decision、任务、filing、行情和行业价格表；
- durable task queue、稳定 task key、lease、失败重试和过期 lease 回收；
- 确定性 collector 与模型任务分离；
- 日报、周报、月度 zero-base、source monitor 和失败告警；
- 公司 wiki、产业链页面、fact card、Excel 行业/公司模型和研究产物目录；
- filing、AlphaEngine、Guidepoint、公开网页和价格 tracker 等工具；
- Active Coverage Loop：事件去重和验证 → driver/thesis 映射 → 模型更新 → 定价判断 → decision → next check；
- Deep Insight Gate 与 `NO_CHANGE / THESIS_STRENGTHENED / THESIS_WEAKENED / THESIS_BROKEN / NEW_THESIS` 决策词汇。

参考现有文件：

- `/Users/everflow/.openclaw/workspace-chem/references/coverage-architecture.md`
- `/Users/everflow/.openclaw/workspace-chem/config/coverage.json`
- `/Users/everflow/.openclaw/workspace-chem/scripts/coverage_os.py`
- `/Users/everflow/.openclaw/workspace-chem/data/coverage/coverage.db`

## 3. 当前痛点

现有系统的本质仍是 **Task Execution OS**，不是 **Research Agenda OS**。

### 3.1 人工决定工作，agent 只决定怎么做

Cron、collector 或人类先生成任务，Dalton 再领取。系统没有一个权威组件持续回答：

- 现在最重要的未知问题是什么？
- 哪项研究最可能改变投资判断？
- 首次覆盖、突发新闻和深度研究如何争夺有限资源？
- 哪条 thesis 最脆弱，最值得寻找反证？
- 上一轮新发现的问题是否应该进入下一轮议程？

历史上 Dalton 曾因没有后续派单而闲置九天，说明“工具齐全”和“能够自主形成工作”是两件事。

### 3.2 任务优先级过于粗糙

当前 queue 主要用优先级和到期时间排序，缺少：

- 对投资决策的潜在影响；
- 当前不确定性与预期信息增益；
- 证据陈旧度；
- 催化剂时效；
- 预计成本、耗时和失败风险；
- coverage 类型之间的容量配额；
- 人类临时 priority override 及其失效时间。

### 3.3 “完成任务”和“解决研究问题”没有严格区分

现有任务可以凭输出文件和最小检查标记完成，但没有独立 verifier 证明：

- 核心问题是否被回答；
- 数字是否准确且可回溯；
- 是否遗漏关键反方证据；
- 新信息是否真正改变已有认知；
- 模型变化是否传到盈利、估值和 thesis；
- 结论强度是否与证据强度匹配。

### 3.4 Thesis 仍不是完整的可执行对象

现有 thesis 有 statement、confidence、falsifier 和 evidence summary，但目标系统还需要显式管理：

- 驱动机制和关键假设；
- 支持、反对和冲突证据；
- 关联模型 driver；
- 估值影响和市场隐含预期；
- 催化剂、期限和 evidence expiry；
- falsifier 当前状态；
- 变更前后版本与变更原因；
- 下次检查条件，而不仅是一个日历日期。

### 3.5 Skill 迭代没有自主闭环

Dalton 可以使用现有 skills，但还不能系统地：

- 从失败和重复劳动中识别能力缺口；
- 自动生成 skill proposal；
- 用历史任务、固定样本和安全检查评估；
- shadow/canary 运行；
- 比较新旧版本的质量、成本和稳定性；
- 自动回滚或申请晋级。

## 4. 设计原则

### 4.1 研究状态独立于模型和 Harness

证据、问题、thesis、模型变化、计划、执行记录和评估结果必须存进外部 durable state。模型、OpenClaw、DeepSeek Harness、Codex 或其他 runtime 都只是消费者和执行器，可以替换。

### 4.2 自主性来自权限和反馈闭环，不来自更长的 prompt

系统必须允许 agent 生成候选问题、选择工作、创建计划、调用执行器、提交证据并触发下一轮，同时用预算、策略、verifier 和审计限制它。

### 4.3 事件驱动，而非常驻推理

确定性 collector、定时器和外部事件负责唤醒。每次 agenda cycle 有明确预算和终态。没有高价值工作时，系统进入休眠。

### 4.4 计划与执行分离，执行与验收分离

- Planner 不应顺手宣布自己的计划正确；
- Worker 不应仅凭自己产出文档就宣布研究完成；
- Verifier 不应直接重写结论后自行通过；
- Thesis Commit Gate 只接受带证据和验证结果的状态变更。

这些角色可以由不同模型、确定性规则或同一模型的隔离上下文承担，但必须留下独立记录。

### 4.5 研究产出必须可重建、可审计、可回滚

每项认知变化都要回答：使用了什么来源、谁执行、哪个工具/skill 版本、验证了什么、旧观点是什么、为什么改变、是否可回滚。

### 4.6 优先保证幂等与副作用安全

每个 work order 都声明稳定 idempotency key、读写集合、外部副作用和 checkpoint。研究计算可以重跑；外发、权限、付费调用、生产配置和正式 thesis commit 必须使用更严格的 gate。

### 4.7 不用伪精确分数掩盖判断

早期可以用结构化的 ordinal confidence 和分项评分。只有经过历史校准后，才把某个综合分数解释为概率或预期收益。

## 5. 目标系统分层

## 5.1 Human Mandate & Policy Plane

保存人类给出的长期目标与边界：

- 行业、公司和产业链范围；
- coverage tier 与服务等级；
- 投资期限、风格、风险约束和 benchmark；
- 允许使用的数据源、模型和预算；
- 允许自主执行的动作；
- 需要人类批准的动作；
- 临时航向调整、优先事项及 TTL；
- 暂停、恢复、终止和 emergency stop。

人类说“本周先把聚氨酯做深”时，系统应记录一个有期限的 priority override，而不是依赖人类手工创建十个任务。

## 5.2 Perception & Event Plane

负责把外部变化转为结构化候选信号：

- filing、业绩、电话会；
- 公司公告和管理层动态；
- 新闻、政策、产能和项目变化；
- 行业价格、价差、库存、开工率和贸易流；
- 股价、成交量、估值和 consensus revision；
- 专家访谈和卖方研究；
- evidence 过期、数据源失败和模型缺口；
- 人类临时任务和讨论中产生的问题。

Collector 只负责采集、规范化、去重和基本质量标记，不直接决定 thesis。

## 5.3 Research Ledger

Research Ledger 是事实和工作状态的权威源，至少包含以下实体：

- `mandates` / `priority_overrides`
- `companies` / `coverage_requirements`
- `research_questions` / `unknowns`
- `events` / `claims` / `evidence`
- `theses` / `thesis_versions` / `falsifiers`
- `drivers` / `model_versions` / `valuation_versions`
- `agenda_cycles` / `agenda_candidates` / `agenda_decisions`
- `work_orders` / `work_runs` / `checkpoints` / `side_effects`
- `evaluations` / `verification_findings`
- `skills` / `skill_versions` / `skill_evals`
- `budgets` / `usage` / `incidents`
- `artifacts` 与 lineage

大型原文、Excel、PDF 和报告仍进入对象/文件存储；Ledger 保存索引、哈希、版本、来源、状态和关系。

## 5.4 Research Agenda Engine

Agenda Engine 每次唤醒后生成候选工作，来源包括：

1. 未完成的首次覆盖问题；
2. 新事件及其潜在 driver/thesis 影响；
3. 即将发生的财报、政策、项目投产和其他催化剂；
4. 被触碰的 falsifier；
5. 支持与反对证据冲突；
6. 过期或低质量证据；
7. 模型、估值和 consensus 缺口；
8. 上一轮新生成的问题；
9. 周期性 zero-base 和沉默期检查；
10. 人类临时任务或优先级调整；
11. skill/tool 失败引出的能力修复任务。

每个候选项形成一张 agenda card：

- 研究问题和预期决策用途；
- 相关公司、产业链、driver 和 thesis；
- 触发原因和截止时间；
- 已知信息、未知信息和所需证据；
- 预期信息增益；
- 潜在投资影响；
- 成本、耗时、数据可得性和执行风险；
- 依赖和冲突；
- 推荐执行策略。

排序考虑以下因素，但初期不宣称它们是概率：

```text
价值侧：决策影响 × 不确定性 × 信息增益 × 紧迫性 × coverage 权重 × 人类 override
成本侧：时间 + token/费用 + 数据成本 + 失败风险 + 副作用风险
```

除了分数，还必须执行 portfolio constraints，避免突发新闻把所有容量耗尽。例如为 event response、首次覆盖、深度研究、维护/验证分别设置可调容量池；未使用的容量才允许借用。

Agenda Engine 每轮必须持久化：候选项、选中项、未选原因、预算分配和下次重评条件。这样可以审计“Dalton 为什么今天做 A，没有做 B”。

## 5.5 Planner & Resource Allocator

Planner 把入选 agenda card 变成结构化 work order：

- objective 与研究问题；
- 输入、假设和上下文 snapshot；
- 子任务 DAG 与依赖；
- 每步适用的数据源、工具、skill 或 agent；
- 选择某个执行器的理由；
- 并发、时间、token、费用和数据预算；
- checkpoint 与恢复策略；
- 幂等 key、读写集合和副作用声明；
- 输出 schema；
- 成功、部分成功、阻塞和失败条件；
- verifier contract；
- 成果写入 Ledger、wiki、模型和报告的位置。

Planner 可以按任务性质选择：确定性脚本、单 agent、并行多 agent、Ralph 式迭代、外部 MCP 或人工协作。它不应该默认把所有工作都交给最强、最贵的模型。

## 5.6 Execution Fabric

Execution Fabric 是可替换的 worker 适配层，可能包含：

- OpenClaw isolated/persistent agent session；
- DeepSeek Harness dynamic workflow 或 Ralph；
- Codex、Claude、Gemini 或其他 ACP runtime；
- Python/SQL/Excel 等确定性执行器；
- filing、AlphaEngine、Guidepoint、web/X 搜索和其他 MCP；
- 专门的模型、数据或文档服务。

统一执行协议至少包含：

- `start / checkpoint / heartbeat / cancel / resume / collect`；
- 标准状态和错误分类；
- 结构化输出与 artifact manifest；
- 工具、模型、skill 和代码版本；
- usage、成本和时长；
- side-effect ledger；
- durable child/workflow id；
- partial result 和 retry policy。

短期可继续由 OpenClaw 负责调度、频道、模型 fallback、权限和工具；DeepSeek Harness 只接入适合动态并行或 fresh-agent 迭代的任务。长期由 Research Kernel 选择执行器，不让任一 harness 成为认知状态的唯一宿主。

## 5.7 Independent Verifier

Verifier 根据 work order 的 contract 进行独立检查：

### 数据与来源

- 时效性数字是否有原始来源、日期、单位和口径；
- 关键数字是否达到规定的交叉验证标准；
- filing 与二手来源冲突时是否正确处理；
- 计算、Excel 公式和期间口径是否通过 sanity check；
- 是否存在来源不可访问、截断或引用漂移。

### 研究完整性

- 是否真正回答问题；
- 是否考虑反方解释和替代机制；
- 是否区分事实、推断和观点；
- 是否把“不知道”错误写成否定结论；
- 是否找到了边际信息，而不只是复述旧知识。

### 投资联动

- 新证据影响哪个 driver、模型行和 thesis；
- 对盈利、估值、催化剂和市场定价有何影响；
- 结论强度是否与证据强度一致；
- falsifier 是否被满足、接近或仍未验证；
- 是否需要零基重审。

Verifier 输出 `pass / conditional_pass / revise / blocked / reject`，并给出可执行 finding。`revise` 生成局部返工任务；不应默认整批重跑。

## 5.8 Thesis & Model Commit Gate

执行结果在通过 gate 前只进入 staging，不直接改正式观点。Commit Gate 要求：

- 对应的 evidence、来源和 verifier 结果存在；
- thesis delta、driver delta、model delta、valuation delta 有明确映射；
- 旧版本、变更理由、置信度和下一检查条件完整；
- 冲突证据没有被静默覆盖；
- 需要人工审批的重大变化已获得批准。

提交后产生不可变版本和审计事件。正式 decision 仍使用既有五种词汇，同时可保留更细的内部 change set。

## 5.9 Reflection & Replanning

每个 work cycle 结束后，系统生成 reflection record：

- 原问题是否被解决；
- 实际信息增益与预期是否一致；
- 哪些假设错误；
- 哪些工具、来源或 skill 表现不好；
- 新增了哪些未知问题；
- 下一轮候选工作；
- 是否需要修改优先级、预算估计或执行策略。

Reflection 只能提出策略和 skill 改进，不允许未经验证直接改生产规则。

## 5.10 Skill Lifecycle

Dalton 可以自主发现和开发 skill，但采用受控生命周期：

```text
能力缺口/重复劳动
→ skill proposal
→ 隔离目录生成
→ 静态安全检查
→ contract tests 与固定 fixtures
→ 历史任务回放 eval
→ shadow mode
→ canary
→ promote / reject / rollback
```

每个 skill 版本记录：

- 要解决的问题和适用边界；
- 权限、依赖和外部副作用；
- 输入/输出 schema；
- 测试集和基线版本；
- 质量、成本、速度和失败率；
- 生成者、验证者和批准者；
- rollout 与 rollback 条件。

低风险、只读、无敏感数据且通过既定 eval 的版本，未来可以按政策自动晋级。涉及外发、付费调用、凭据、权限、生产配置、数据删除或正式 thesis commit 的 skill 仍需人工批准。提出 skill 的 agent 不能同时担任最终审批者。

## 6. 核心状态机

### 6.1 Agenda item

```text
candidate → selected → planned → executing → verifying
          ↘ deferred                         ↘ revise → executing
                                             ↘ blocked
                                             ↘ committed → follow-up candidate
                                             ↘ rejected
```

### 6.2 Work order

```text
draft → admitted → leased → running → checkpointed
                         ↘ retryable_failed → ready
                         ↘ blocked
                         ↘ cancelled
                         ↘ completed → verification_pending
```

### 6.3 Thesis

```text
hypothesis → open → strengthened/weakened/conflicting
                 → broken → zero-base review
                 → closed/superseded
```

每次状态变更必须带 actor、reason、evidence/version ref 和 timestamp。

## 7. 自主权限模型

自主性按动作风险分层，而不是给 agent 一个笼统的“完全自主”开关。

### 可自主执行

- 读取授权数据；
- 生成研究问题和计划；
- 创建低风险、只读 work order；
- 启动预算内的研究 worker；
- 写 staging artifact、临时模型和 wiki 草稿；
- 生成 verifier finding 和局部返工；
- 提出 skill proposal；
- 对未通过 gate 的草稿反复迭代。

### 条件自主

- 在预算和 coverage 范围内购买/调用数据；
- 晋级低风险 skill；
- 更新正式模型和 thesis；
- 对外发送常规日报/周报；
- 增加新公司进入机会池。

这些动作由可配置政策、额度、双重验证或 canary 控制。

### 必须人工批准

- 交易或仓位改变；
- 扩大行业/公司范围或长期预算；
- 敏感数据、凭据和权限改变；
- 不可逆删除；
- 新的高风险外部副作用；
- 绕过 verifier 或修改自身治理规则；
- 自动批准自己编写的高风险 skill。

## 8. 人机交互方式

人类面对的是研究组合和认知，不是底层任务队列。

主要界面应包括：

- 当前 mandate、coverage universe 和航向 override；
- 今日/本周 research agenda，以及每项入选和未入选原因；
- 重要未知问题和证据缺口；
- thesis、falsifier、confidence 和估值变化；
- 当前执行、阻塞、预算和 incident；
- 待审阅成果和需批准动作；
- 与 Dalton 围绕现有 Ledger 讨论的入口；
- “优先做这个 / 暂停这个 / 不要继续这条线 / 把这家公司加入观察”等 steering 操作。

临时 steering 应保留 provenance 和 TTL。它可以改变 agenda，但不能静默改写历史事实。

## 9. Harness 与基础设施选择

### 9.1 不把 Research OS 绑定到 OpenClaw 或 DeepSeek Harness

Research Kernel 自研，至少负责：

- Research Ledger；
- Agenda Engine；
- Planner/Resource Allocator；
- Work Order 与 checkpoint 协议；
- Verifier 和 Commit Gate；
- Skill lifecycle；
- Policy、budget、audit 和 evaluation。

### 9.2 OpenClaw 的近期角色

- 稳定 cron、事件触发和 cold session；
- Discord/飞书等人机界面和投递；
- 模型路由与 fallback；
- MCP、skills、权限和本地工具；
- 常规单 agent/子 agent 执行；
- 失败告警和运维。

### 9.3 DeepSeek Harness 的可选角色

- 动态编写并行多 agent workflow；
- fresh-agent Ralph 迭代；
- 独立 worker runtime；
- 插件化实验新的执行策略。

当前开发预览版缺少 workflow/Ralph 的后台恢复、完整调度、资源预算和独立 evaluator，因此只适合作为可替换 worker，不作为权威控制面。

### 9.4 Durable workflow engine

短期单机试点可以继续用 SQLite + OpenClaw cron + work lease。出现以下需求后，再引入 Temporal 等 durable workflow engine，并把状态迁至 Postgres：

- 多进程/多机器 worker；
- 跨小时或跨天的复杂 DAG；
- signal、timer、retry、child workflow 和精确恢复；
- 更严格的高可用和审计；
- 多个行业 agent 共用 Research Kernel。

不要为了架构完整度过早引入分布式系统。

## 10. 评估体系

### 10.1 研究质量

- 关键数字错误率；
- 证据溯源完整率；
- 反方证据覆盖率；
- 首次覆盖 gate 完成质量；
- thesis 变更的事后有效性；
- variant view 的命中率和校准；
- 重大事件从发生到有用判断的时间；
- 人类推翻或大幅返工比例。

### 10.2 自主规划质量

- Agenda 选择得到人类认可的比例；
- 高价值问题被延误或遗漏的比例；
- 预计与实际信息增益差异；
- 新闻、首次覆盖、深研和维护之间的容量健康度；
- 无任务闲置和无价值忙碌的时间；
- 新问题转为有效后续研究的比例。

### 10.3 执行与可靠性

- work order 成功、重试、阻塞和恢复率；
- checkpoint 恢复成功率；
- 重复副作用和重复投递数；
- 单位有效结论的 token、费用和人类时间；
- tool/skill/provider 失败率；
- incident 检出和恢复时间。

### 10.4 Skill 改进

- 新旧版本历史回放质量差异；
- shadow/canary 失败率；
- 自动回滚次数；
- skill 带来的时间/成本节省；
- 未经授权扩大权限或副作用次数必须为零。

## 11. 分阶段演进

### Phase 0：记录和可观察性

- 不改变现有执行逻辑；
- 补齐 agenda cycle、research question、work order、evaluation 和 lineage 记录；
- 记录当前人类/cron 为什么创建任务，形成真实训练和评估数据；
- 建立统一 run/incident/usage 视图。

### Phase 1：Agenda Engine Shadow Mode

- Dalton 每天自主生成候选议程和排序，但不执行；
- 与现有人类/cron 实际选择对比；
- 人类标注错过、误选和优先级；
- 校准排序特征和容量策略。

### Phase 2：低风险自主闭环

- 允许系统自主创建并执行只读、低成本、无外部副作用的研究任务；
- 初期限定少量试点公司；
- work order 必须带预算、checkpoint 和 verifier contract；
- 正式 thesis/model 仍人工批准。

### Phase 3：Verifier 与 Thesis Commit

- 建立数据、研究完整性和投资联动 verifier；
- 支持局部返工；
- 低影响 `NO_CHANGE` 和例行更新可按政策自动 commit；
- 重大 thesis/估值变化仍要求人工审阅。

### Phase 4：Skill 自主改进

- 从失败和重复劳动自动生成 skill proposal；
- 建立 fixtures、历史回放、shadow、canary 和 rollback；
- 先开放低风险 skill 自动晋级。

### Phase 5：多 runtime 与规模化

- 接入 DeepSeek Harness 等动态 workflow worker；
- 建立统一 execution adapter 和资源调度；
- 达到规模门槛后迁移 Postgres/Temporal；
- 扩展到多个行业 agent，共享底层 Research Kernel，但保持各自 mandate、知识和权限边界。

## 12. 非目标与反模式

系统不追求：

- 自动交易；
- 用输出数量代替研究质量；
- 让一个 Session 永久运行；
- 无边界地浏览、消费 token 或购买数据；
- agent 直接修改 live skill、权限和治理规则；
- 执行者自行验证并批准所有结论；
- 用单一总分替代证据、分歧和判断；
- 把聊天 transcript 当作唯一记忆；
- 因为用了多 agent 就假设研究一定更好。

## 13. 需要独立审阅的关键问题

1. Research Agenda Engine 应是单一 planner，还是候选生成、反方挑战和资源分配三个独立角色？
2. 如何定义并事后校准“信息增益”和“决策影响”，避免 planner 奖励投机性大题目？
3. 如何防止日常新闻吞噬长期首次覆盖和产业链深研资源？
4. Verifier 如何真正独立，避免同源模型的相关性错误？
5. Thesis confidence 应如何更新，既避免伪 Bayesian 精确，又能稳定比较版本？
6. 什么粒度的 research question、work order 和 checkpoint 最合适？
7. 哪些状态应进入关系型数据库，哪些应进入事件日志、对象存储或知识图谱？
8. Skill 自动晋级的风险分层和最低 eval 门槛应如何设计？
9. 什么时候应该升级到 Temporal/Postgres，避免太早复杂化或太晚迁移？
10. 如何衡量 Dalton 是否真的在“自主研究”，而不是自动制造更多任务？
11. 如何让人类 steering 清楚改变未来优先级，又不污染事实和 thesis 历史？
12. 如何设计系统级停止条件，避免研究循环自我激发且永不收敛？

## 14. 当前架构判断

最有前景的路线不是“选 OpenClaw”或“选 DeepSeek Harness”二选一，而是：

1. 以现有 Coverage OS 为 durable state 骨架；
2. 自研 runtime-agnostic Research Kernel；
3. 近期用 OpenClaw 承担生产控制、工具、频道和常规执行；
4. 用 DeepSeek Harness 或其他 runtime 承担适合它们的并行/迭代任务；
5. 先通过 shadow mode 和小范围闭环证明议程选择与 verifier 有效，再扩大自主权限；
6. 人类从派任务者逐步转为 mandate owner、航向校正者、研究讨论者和投资决策者。
