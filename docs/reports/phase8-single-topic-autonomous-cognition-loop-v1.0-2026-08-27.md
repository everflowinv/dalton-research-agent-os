# Dalton Phase 8：单主题自主认知闭环 v1.0

日期：2026-08-27  
状态：当前执行顺序基线；owner 已要求更新文档并继续开发  
触发：第三方对 Dalton 长程自主研究与知识检索能力的评审

## 裁决

第三方对长期缺口的判断基本成立：Dalton 已有可靠的控制、账本和重放能力，但还没有形成完整的研究认知循环。
不过评审使用的是 S7d / S7e 完成前的状态。2026-08-27 的 live Core 已不再是 0 条正式研究记录，canary 与 live
之间的第一道墙已经跨过。

下一阶段定为 **Phase 8「单主题自主认知闭环」**。目标不是继续增加孤立合同，也不是先做向量数据库，而是让同一个
研究主题完成并重复运行：

```text
事件或定时唤醒
→ Agenda
→ 读取已有研究状态
→ Bounded Planner 选择已批准 probe
→ connector 获取
→ verifier / policy commit
→ thesis-impact
→ weekly brief
→ 人类内容反馈
→ 下一轮 Agenda
```

首个主题固定为「美国 IT 服务需求是否见底」。人类负责研究方向、方法、初始 Thesis 和内容反馈，不再逐条派任务或
批准低风险 SEC 研究。

## 本轮核对的当前状态

- live Core 有 5 条正式 Claim / 5 条 Evidence：4 条 SEC policy 自动提交，1 条 transcript 经 owner 人工确认。
- ACN、CTSH、EPAM、IBM 的 live driver pack、industry evidence pack 和 company overlay 已注册；lane-only brief 可逐字节重放。
- 首期 `weekly-brief-version:us-it-services:2026-w35` 已发布并投递 Discord；DeliveryReceipt 和 1 条内容性反馈已写入
  append-only authority。
- 首期 issue 是基线，4 条既有 SEC Claim 不算本周新增；第二期才能依据 exact prior issue 计算 delta。
- live 没有 ThesisVersion，`company_thesis_refs` 为空，所以四家公司在首期 brief 中全部为 `insufficient`；
  thesis-impact runner 每 300 秒只做零模型调用的 idle 检查。
- Weekly Brief 只有 human-governed publish / delivery / feedback operation，尚未建立定时 coordinator 或 cron。
- Doctrine、LLM Planner、DocumentIndex 和通用 ContextMaterializer 已有 development candidate，但没有进入这条 live 研究链。
- 2026-08-27 08:15 UTC 本轮检查：`deploy/macos/health.sh` 为 `ok=true / state=running`，Agenda 最新 cycle 已交付；
  `5756c5b` 和 `2cecd12` 的 GitHub Actions 均成功。

## 对第三方评审的采纳与校正

采纳：

- 长程自治的主要缺口是认知循环，不是更多调度或审计合同。
- Doctrine 0.1 只表达注意力和最低证据标准，不能完整承载研究方法、反证、估值、停止条件和写作审美。
- 当前知识层擅长保存和 exact rehydrate，语义检索、跨措辞召回和自动上下文装配仍不完整。
- Planner 需要按权限逐级扩展，不能从受限 checklist 一步跳到任意工具执行。
- 多 agent、Temporal、Postgres 和更多 connector 不是当前瓶颈。

校正：

- live 正式 Evidence / Claim 已不是 0；S7d 已完成计划内四家公司 SEC lane。
- Weekly Brief 的 issue、delivery 和反馈 authority 已存在，不需要另起一套平行账本。
- Agenda 的 provider token 预算问题已经修复，最新 live cycle 正常交付。
- 当前语料只有 5 条正式 Claim 和少量原始材料。现在先做向量库不会解决 Thesis、Planner 和 weekly loop 未接通的问题。

## 执行顺序

### S7f：关闭 Phase 7 的剩余门槛

1. 更新 README 和 PROJECT_STATUS，删除「SEC 未部署、live 为 0」等过期表述。
2. 用现有 SEC lane 增加 DXC，补足第 5 条 policy 自动提交 Claim，并把第五家公司追加为新的 immutable evidence pack / overlay
   版本；不原地修改四家公司版本。
3. 增加 policy-controlled Weekly Brief coordinator：按研究窗口生成 issue、投递、记录 DeliveryReceipt；相同窗口重放必须
   返回原结果，不重复投递。
4. coordinator 的代码、隔离 canary 和部署接线可以先完成；从 human-governed publish 扩为自动发布属于 production
   authority 扩权，live activation 保留单独 owner gate。

### P8a：最小 Research Constitution 与初始 Thesis

不建立一个复制全部规则的巨型 schema。增加版本化 Constitution manifest，绑定现有 Mandate、Doctrine、Driver Pack、
Verifier policy、Weekly Brief rubric 和权限 policy 的 exact ref/hash，并只补当前对象无法表达的研究方法：

- 什么问题值得研究，怎样判断信息增益；
- 美国 IT 服务需求的 driver / KPI / 因果机制；
- 来源等级、冲突裁决和最低独立证据；
- falsifier、替代解释和必须主动寻找的反方证据；
- 量级、盈利、估值和市场预期的映射要求；
- 继续、refresh、停止和升级人工的条件；
- 好坏研究产物的冻结样本与评价 rubric。

先由 human gate 准入 1 条行业 Thesis 和 1 条 ACN Thesis，并建立 company→thesis mapping。低影响 assessment 可以自动运行，
重大 Thesis 变化继续由人工批准。

### P8b：最小知识调用层

先增加可重建的 `CompanyResearchView`，不创建新的事实 authority。投影至少包含：

- 当前 Thesis 和关键 driver；
- 最新有效、contested、superseded 的 Claim；
- open questions、falsifiers 和证据 freshness；
- 最近一次 weekly issue 和上次研究停点。

增加 company / aspect / period / status 的结构化查询，返回 immutable ref/hash；再从 Ledger / RawSpool 重读并验 hash，交给
现有 ContextMaterializer 生成 token-bounded ContextPack。DocumentIndex 先接真实 Artifact 和 passage offset；embedding、
QuestionEquivalenceLink 和跨公司语义召回等真实语料和检索 eval 出现后再做。

### P8c：Tier 1 Planner 进入 live

- 固定一个 ResearchQuestion：「美国 IT 服务需求是否见底」。
- Planner 只能从 human-admitted ProbeTemplate catalog 中选择；Core 继续冻结 source、operation、参数、预算、权限和 terminal gate。
- 接通 Agenda → CompanyResearchView → Planner → SEC / AlphaEngine → verifier → Claim → thesis-impact → Weekly Brief。
- Reflection 可以登记新问题候选，但 Tier 1 Planner 不能自行增加 connector、template、参数或预算，也不能执行尚未准入的问题。

### P8d：反馈形成评测集

现有 WeeklyBriefFeedback、AgendaFeedback 和 Claim review decision 是反馈层的第一批输入。先积累真实数据，再派生冻结的
Research Evaluation corpus，记录：保留、修改、反对、缺证据、agenda 误选和事后证伪。系统可以提出 Constitution / Doctrine
新版本，但不能自行生效；新版本必须先做历史回放和对照评测，再由 owner 批准。

### P8e：通过四周 gate 后再扩权

通过 Phase 8 验收后，Planner 才进入 Tier 2：可以提出新子问题、查询词、比较对象和受限参数，由 Core 做权限、预算和来源检查。
同一阶段再根据检索 eval 决定 passage embedding、问题等价关系和跨公司召回。Tier 3 connector / skill builder、第二 runtime、
Temporal / Postgres 和 multi-agent fleet 继续后置。

## Phase 8 退出门槛

- 连续 4 周自动生成并投递 weekly brief；第二期起都绑定 exact prior issue 并正确计算 delta。
- 至少 1 条新 Claim 完成「获取 → 验证 → 正式提交 → thesis-impact → brief」链路。
- 人类只给方向、初始 Thesis 和周报内容反馈，不逐条批准低风险研究任务。
- 完成一次 provider 返回后和投递前的崩溃演练；provider call、费用、Claim、issue 和外部消息均不重复。
- 至少 1 条 owner 反馈在下一周期改变 Agenda 选择或研究 rubric，并留下版本与回放记录。
- 所有调用在 mandate、connector quota、日预算和权限内；扩大范围、预算、权限或重大 Thesis 变化继续人工 gate。

## 继续冻结

- 不先做 embedding-first retrieval 或独立向量事实库；
- 不增加多 agent swarm、第二 runtime、Temporal 或 Postgres；
- 不扩更多 connector 品类；
- 不自动修改重大 Thesis；
- 不建立没有真实反馈样本的通用 Preference Ledger；
- 不让同一个 builder / planner 自行修改并批准治理规则。

## 下一开发切片

本报告后的第一笔代码是 **S7f Weekly Brief coordinator development candidate**：先完成可重放的 issue / delivery 计划、
production policy gate、隔离 canary 和 macOS 调度接线，不激活 live 自动发布。DXC live lane 与 automatic delivery activation
分别保留 exact owner gate。
