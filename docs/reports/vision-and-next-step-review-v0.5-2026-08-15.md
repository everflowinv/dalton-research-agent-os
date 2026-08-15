# Dalton 愿景与下一步裁决 v0.5

日期：2026-08-15
状态：当前架构与执行顺序基线；取代 v0.4 的后续顺序，不反向改写历史报告
审阅范围：Kimi 外部讨论、Fable 5 独立复审、仓库 `eeeffd4`

## 结论

Kimi 所说的“没有发动机的精密变速箱”仍然成立：Dalton 已有真实 SEC 数据、验证、候选区、人工审阅和正式
Ledger promotion 的各段能力，但 committed 验收记录里还没有一条人批 ResearchPlan 完整执行四步，也没有一条
真实 candidate 经人工接受后进入正式 Ledger。项目继续推进，但下一笔不做 Interrupt / park / resume，也不做
Reflection；先跑通第一条真实 SEC 研究闭环并测量研究质量。裁决为 **Conditional Go**，范围只限 SEC public
read-only、逐 plan 人工批准、逐条人工入库。

## 事实基线

以下是本轮核对过的 committed 事实：

- HEAD 为 `eeeffd4bf99620a62015fb08734964e90743350b`，审阅时工作树干净。`docs/PROJECT_STATUS.md`
  记录的 live commit 为 `6356ceeecf7e937bc1aa6fb20d7635cc4370f792`，落后 HEAD 44 个 commit；
- `src/` 有 50,417 行 Python，`tests/` 有 21,953 行 Python，仓库有 105 份 JSON contract 和 18 份
  Core SQL schema。数量只说明治理代码的规模，不证明研究价值；
- Planner 已生成 connector → authority resolver → source/numeric verifier → candidate staging 四步任务树，但
  启动时只 admission 根 connector WorkOrder。三个子节点仍停在 `planned`，见
  `research-plan-thin-closure-2026-08-15.md`；
- `scripts/run_public_sec_authority_demo.py` 已在隔离临时 authority 中访问 `data.sec.gov`，并把真实 SEC public
  数据处理到 `human-review-ready-candidate`。它不是经 Planner 启动的完整 WorkOrder 树，也没有自动写 Ledger；
- HumanReviewAuthority 已能在人工 `accept` 后原子写 EvidenceVersion 0.2、ClaimVersion 0.2 和 `supports`
  relation；Backlog 也已能把正式 ClaimVersion 绑定为问题答案。这些段落尚未在同一条真实运行中串起来；
- Planner 专项 13/13、Planner + Backlog 47/47、Python 全量 507/507、broker 15/15 已通过；测试证明当前
  契约和故障语义，没有证明候选结论对投资研究有用；
- 根目录没有 LICENSE。这个问题会影响外部复用，但不会阻塞第一条真实研究闭环；许可证类型需要 owner 决定。

外部讨论中关于“Dalton 一半代码可由 LangGraph 用十分之一用户代码替代”或“约两千行即可复刻”的数字没有
可比实验，本报告不把它们当事实。它们只构成一个应当用 spike 检验的工程假设。外部讨论原文：
[Kimi｜投研 Agent 设计评析](https://www.kimi.com/share/1a006a0f-e042-87ed-8000-00006848bb68)。

## 对 Kimi 批评的裁定

### 仍然成立

1. **研究负载没有追上治理内核。** 真实 SEC canary、Planner、Human Review 和 Ledger promotion 都已存在，
   但 committed 验收记录里还没有第一条完整研究闭环。继续增加 authority、schema 或 connector 品类只会扩大
   这个缺口。
2. **研究质量没有数据。** verifier 有篡改和数值错误测试，但还没有一组接近真实研究错误的 seeded-error
   校准，也没有 candidate 人工接受率、每条 accepted Claim 成本或下游使用数据。
3. **自研 durable execution 是否划算，尚未裁决。** 当前代码证明 Dalton 能表达严格的 fail-closed 语义，
   但没有与 Temporal 或 LangGraph 做同任务、同故障条件的比较。
4. **产品成熟度仍低。** live 只运行万华 Agenda Shadow；研究执行、Human Review 和 Ledger promotion 都未部署。

### 已经回应，但还要用真实负载验证

1. **记忆漂移。** ContextPack materializer 与 Agenda context authority 已把模型输入绑定到 exact authority，
   超预算会 fail closed；模型 session、transcript 和 compaction summary 都不是事实权威。这是 Dalton 相对普通
   checkpoint/RAG 流程的实质增量，但目前只在测试和 fixture 负载下验收。
2. **错误进入正式认知。** candidate staging、HumanReviewAuthority、scoped writer 和 Ledger 0.2 promotion
   已把“候选”和“正式 Claim”分开；只有显式人工接受才能入库。还缺真实运行和人工质量数据。
3. **长期问题不会只活在一次 session 里。** ResearchQuestionBacklog 已给问题稳定身份、状态历史和正式
   ClaimVersion answer binding；Planner 也绑定 exact AgendaDecision 和 ResearchQuestionVersion。
4. **低监督要逐级挣得。** 逐 plan 人批、auto-accept 不授权、凭据和生产权限独立 gate，与 Kimi 提出的原则一致。
   当前问题不在方向，而在项目还没有用数据证明应当获得下一级权限。

## 修订后的愿景

> Dalton 是单一 owner 的投研控制内核：它维护不可变、可审计的 Evidence → Claim → Thesis 版本链，并让模型
> 作为不可信 worker，在人工逐级放权下执行研究。人类设定长期 mandate、审阅候选结论并决定是否放权；系统
> 维护问题、安排计划、执行只读研究，再把候选 Evidence 和 Claim 交给人审阅入库。近期不做通用 agent 框架、
> 多租户、多机、hostile-code 沙箱、自动 Ledger commit 或 skill 自动晋级。每一级自主权都要用真实人工标签、
> 可回滚执行和可量化错误率挣得。项目是否有价值，取决于固定成本下经人工接受的 Claim 数量、质量及其对真实
> 投资讨论的贡献，不取决于 contract、schema 或测试数量。

原方向保留：Dalton 仍要成为能长期维护问题、研究和认知的系统。表述收窄：在真实数据证明价值前，不再把
“持续覆盖行业和公司的自主研究分析师”当作已经兑现的产品承诺。

## 下一步顺序

v0.4 的顺序是 Backlog → Planner → Interrupt / park / resume → Reflection。前两项已完成开发候选，后两项顺延。
新的冻结顺序是：

1. coordinator 对 resolver、verifier 和 candidate staging 逐项 admission；
2. 一份人批 ResearchPlan 在隔离环境访问真实 `data.sec.gov`，完整执行四步 WorkOrder；
3. candidate 走 HTML review，人工接受后写入正式 EvidenceVersion/ClaimVersion 0.2，并绑定 Backlog answer；
4. 用 seeded errors、人工接受率、成本和下游使用情况做首轮质量校准；
5. 达到下面的门槛后，再决定是否部署、扩公司、放松人工 gate 或继续做 Interrupt / Reflection。

Interrupt / park / resume 只有在真实计划运行后反复出现“临时问题打断长计划”的需求时才提前。Reflection 至少等
10 条真实 reviewed run，再决定需要哪些记录和回流动作；没有真实运行时先建 Reflection 只会生成空协议。

## 自研边界

### 继续持有

- Research Ledger 的 Evidence → Claim → Thesis 不可变版本链与 relation 语义；
- HumanReviewAuthority、scoped writer 和正式 Ledger promotion；
- ResearchQuestionBacklog 的稳定问题身份、状态历史和 answer binding；
- ContextPack materializer 与 exact authority replay；
- SourceEnvelope、usage、cost、quota 和 verifier 的问责链；
- 单写者与 scheduler policy boundary 的逻辑边界。

这些部分表达 Dalton 的研究语义、责任和权限，不应交给通用 agent 框架定义。

### 第一条真实闭环前停止扩建

- 新 connector 品类；
- Interrupt / park / resume 与 Reflection；
- capability gap detector、代码生成器、sandbox service 和 skill 自动晋级；
- Model IR、embedding sidecar、多 runtime、多机和 hostile-code 隔离；
- 只有 schema、inventory、projection 或 dashboard 字段，没有真实消费者的提交。

### 只做 spike，不做迁移

第一条真实闭环完成后，可以在 `spikes/` 用 recorded fixture 对比 Temporal。任务固定为“四步 plan 树执行、
崩溃恢复、人工暂停、exact hash 重验”，记录应用代码量、运行依赖、恢复语义和 Dalton 必须保留的补丁。spike
不得修改 `src/dalton_core`，也不得先决定迁移。Kimi 的代码量判断只有在这项实验后才可证实或否定。

## 未来两周

| 顺序 | 切片 | 目标 | 退出条件 | 禁止扩张 | 部署 |
| --- | --- | --- | --- | --- | --- |
| 1 | 真实闭环收口 | coordinator 逐项 admission；一份人批 plan 完整执行真实 SEC public 四步树 | 产出一个 `human-review-ready-candidate`；崩溃和重放收敛到同一任务树 | 不新增 connector；除 admission 必需字段外不加 schema；不做 Interrupt/Reflection | 否，隔离运行 |
| 2 | 人工入库 | candidate 走 HTML review；人工接受后写 EvidenceVersion/ClaimVersion 0.2 并绑定 `answer_question` | 正式 Ledger 有至少 1 条来自真实 SEC 的 accepted Claim；来源、数字和期间可回指 | 不开 auto-commit；不放松人工 gate | 否 |
| 3 | verifier 首轮校准 | 注入至少 10 个接近真实研究错误的 seeded cases，记录检出、误报和严重度 | 产出第一份带样本明细的校准报告；该小样本不授予自动化权限 | 不用合成高分替代真实人工 review | 否 |
| 4 | live 候选验收 | 将最小 SEC public read-only 路径整理为可部署候选 | health、replay、权限和回滚检查通过；owner 对具体部署单独批准 | 不关旧 cron；不扩公司；不开凭据 | 只有 owner 单独批准后 |
| 5 | 数据报告与 Temporal spike | 报告 Agenda 标签、candidate 接受率、每条 accepted Claim 成本；用 recorded fixture 做 Temporal 对比 | 报告有真实分母和口径；spike 给出 keep/adopt 判据 | 不迁移 authority；不把外部框架 checkpoint 当 Ledger | 否 |

## 停止与继续门槛

以下数字是 v0.5 的建议门槛。Agenda 认可率阈值此前尚未由 owner 冻结，不能把 Fable 5 的候选值写成已批准政策。

- **到 2026-08-29：**至少 1 条真实端到端 ResearchPlan 和 1 条人工接受的正式 Claim。未达到时，冻结所有
  新内核子系统，只允许修复这条闭环；
- **首个 4 周观察窗：**至少 5 条真实 candidate 完成人工 review，接受率至少 50%。低于 50% 时不扩权限，
  回修问题选择、提取和 verifier；5 条只用于早期方向判断，不用于证明稳定质量；
- **verifier 放权：**至少 30 个 seeded cases，总检出率不低于 90%，高严重度错误零漏检。在达到前保持全量
  人工 review，不讨论 auto-commit；
- **Agenda 扩容：**保留已冻结的 10 个工作日和至少 20 个显式人工标签。建议再要求连续 4 周认可率不低于
  75%；这个 75% 只有 owner 批准后才能进入 policy；
- **产品价值：**从第一条 live Claim 起 8 周内，如果没有任何正式 Claim 被人类用于真实投资讨论、Thesis 更新
  或后续研究问题，停止扩建 autonomous research kernel，先收缩为研究记录和人工审阅工具；
- **成本：**从第一条 accepted Claim 开始记录模型成本和人类审阅时间。没有同口径人工研究基线前，不宣称系统
  提高了效率；连续两个观察窗成本上升而接受率、错误率和下游使用没有改善时，暂停扩权并复盘。

## 单一裁决

**Conditional Go。** 只允许推进 SEC public read-only、单一公司、逐 plan 人工批准、逐条人工 Ledger gate 的
第一条真实研究闭环及其质量测量。Interrupt / Reflection、新 connector、凭据、自动 commit、旧 cron cutover、
多公司和 runtime 迁移全部后置，分别等待数据与人工批准。
