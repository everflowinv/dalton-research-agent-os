# Dalton 愿景与执行优先级 v0.6

日期：2026-08-15
状态：当前架构与执行顺序基线；修订 v0.5 的“第一条闭环前全部冻结”表述，不反向改写历史报告

## 裁决

Dalton 的价值最终要由首批真实投研产物和人工判断证明。第一条完整 ResearchPlan 仍是当前第一优先级，因为没有
端到端运行，就无法知道治理内核是否改善了研究质量，也无法定位真正缺少的数据和建模能力。

这不等于冻结全部 connector 或 model 工作。首家公司覆盖需要的数据源质量、字段完整度或建模能力如果不足，
可以同步补齐，但每项工作必须满足以下条件之一：

- 直接解除已选 ResearchPlan 的执行阻塞；
- 对首轮产物质量提出可检验的改善目标，并进入同一次人工 review；
- 修复真实运行暴露的来源、数值、期间或模型推理缺口。

没有具体 ResearchPlan 消费者、没有质量验收标准，或只是扩大架构完整度的 connector、Model IR、sandbox、
dashboard/schema 扩建继续后置。

## 当前顺序

1. 完成 coordinator 的逐边 admission，并接上真实 SEC public 四步 executor；
2. 运行一份逐 plan 人工批准的首家公司覆盖计划；
3. 根据运行阻塞和候选产物，补必要 connector 数据与 model 能力；
4. candidate 进入 HTML review，人工接受后写正式 EvidenceVersion/ClaimVersion 并回答 Backlog question；
5. 记录人工接受/修改/拒绝原因、来源与数字错误、成本和审阅时间，再决定下一轮建设。

这里的先后是资源优先级，不是串行禁令。第 3 项可以与第 1、2 项并行，但必须能回指同一条真实计划及其验收。

## 近期边界

- 保持 SEC public read-only、逐 plan 人工批准、逐条人工 Ledger gate；
- 不部署、不切旧 cron、不开放凭据、不启用 auto-commit，除非 owner 对具体动作另行批准；
- Interrupt / park / resume 与 Reflection 暂不抢占第一条真实闭环；
- Temporal/LangGraph 代码量与恢复语义只通过同任务 spike 比较，不采用外部估算；
- 到 2026-08-29，仍以至少一条真实端到端 ResearchPlan 和一条人工接受的正式 Claim 作为首要产出。若未达到，
  只继续能解除该闭环阻塞或提高其产物质量的工作。

## 价值判断

系统价值不由 contract、schema、测试或 connector 数量决定。近期只看四类证据：

- 真实计划能否稳定重放并到达人工审阅；
- 来源、数字、期间和推理是否达到人工可接受标准；
- 人工接受的 Claim 是否进入真实投资讨论或后续研究；
- 为达到该质量付出的模型成本与人工审阅时间。

因此当前裁决仍是 **Conditional Go**，但条件从“闭环前停止所有横向能力”修订为“完整流程第一优先，允许直接
服务首条计划与产物质量的数据源和建模增量”。
