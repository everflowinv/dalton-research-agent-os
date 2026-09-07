# Dalton 愿景复盘与下一阶段裁决 v1.1：按研究手册的阶段执行任务

日期：2026-09-07
状态：当前执行顺序基线；替代 Phase 9 v1.0 的近期顺序，不反向改写历史报告
触发：owner 在新 cockpit 上指出「子任务」与其理解不一致——目标是「建立 US IT services 行业的首次覆盖」，子任务应当是「建立 ACN 的 Initial Screen 和财务预测」这一类，而不是资料计数或常驻研究问题；并要求先详细列出后续开发计划再更新愿景
相关：[ADR-0005](../adr/0005-autonomous-document-extraction.md)、[ADR-0006](../adr/0006-owner-cockpit.md)、[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)、[v0.9](vision-review-and-next-phase-v0.9-2026-08-26.md)

## 结论

1. **方向仍然没有漂移，但系统做的事停在了研究手册的「阶段之前」。** 愿景从 v0.1 起就是：人定目标与边界，机器按团队方法论自主形成议程、拆计划、积累证据与模型、检验 thesis；Phase 9 把方法论写进了合同（`ResearchPlaybook`：Initial Screen → Deep Insight Gate → 行业模型 → 公司全模型 → Investment Memo → 持续覆盖，每阶段有必读、必交、出口门）。Phase 9 之后的 P9d 系列把「搜资料 → 拿原文 → 读出观点 → 入库为 Claim」做成了全自动，live 已有 190 条 Claim（185 条定性、5 条 SEC 数字）。但 **阶段账本至今 0 行**：没有任何 lane 把工作组织成「ACN 的 Initial Screen」，没有资料底座清单核对，没有一份交付物文档，没有一次出口门自评。cockpit 展示的「搜集→阅读→持续跟踪」只是真实存在的那一小段。
2. **owner 的理解是对的，而且合同早已这样定义。** mission v7 的 `deliverables` 就是行业框架、Initial Screen、行业模型、公司模型、预测线、投资备忘录、每周简报；`autonomy.may_write` 已经授予 `stage_record`。缺的不是授权，是执行它的 lane 和承载交付物的 authority。
3. **常驻研究问题不是子任务，也不会自己更新。** 它们是 mission 的 `research_questions`，只由 owner 通过目标/方向修改，作用是引导抽取与问答。子任务是「公司 × 阶段」和「行业 × 阶段」。cockpit 要把这两层分开展示。
4. **下一阶段定为 Phase 10「按 Playbook 阶段执行任务」。** 目标是 live 上五家公司各有一份由机器起草、数字全部可回指、出口门四问自评通过的 Initial Screen，阶段账本非空，ACN 进入 Deep Insight Gate 等 owner 裁决；随后行业框架与公司模型。**不再新增来源，不做通用能力**，一切以「阶段过门」验收。

## 现状盘点（2026-09-07，live）

| 项 | 状态 |
| --- | --- |
| 阶段账本 `coverage_mission_stage_records` | 0 行；stage authority 完整（entered / gate_passed / gate_failed；自动化可过非人审阶段的门，gate_passed 必须带 evidence refs；Deep Insight Gate 与 Investment Memo 只接受人类） |
| 交付物 authority | 不存在。周报有 `weekly_brief_issue_versions` 可作版本化文档的范式；Initial Screen / 行业框架 / memo 没有落点 |
| Claim | 190 条：185 定性（ADR-0005 自动准入，主要来自 CTSH 13 份、EPAM 7 份读完的文档），5 条 SEC 季度收入（每家一条）。约 50 条早期误归属/免责声明类 Claim 仍在 Ledger 里（append-only，需挑战/退役机制） |
| 资料 | AlphaEngine 已获取 33 / 发现 122（24h 上限 30）；网页已获取 72 / 发现 174；ACN、IBM、DXC 尚未读完任何一份（抽取按最旧优先，不按公司优先级） |
| 数字与模型 | Model Input Ledger v1 有 3 个版本，无 ForecastLine，无 reconciliation；SEC company-facts lane 已连接但只跑过一次窗口 |
| 来源 | connected：SEC、AlphaEngine、web-search；not_connected：company-IR、Guidepoint |
| Thesis | 2 条（行业级、ACN），人签发 |
| cockpit | ADR-0006 五视图已部署；「子任务」= 资料计数（待改为阶段视图） |

## Phase 10 切片（按依赖顺序）

每个切片都要：隔离测试 + live 部署 + 以 owner 身份在 cockpit 上看到结果。需要 owner 跑的授权发布单独标出；其余全部自动化。

### P10a：阶段账本启动与资料底座核对（Initial Screen 进入）

- **做什么**：新 lane `mission_stage_driver`（controller tick 调用，与 discovery/extraction 同形）。对 universe 每家公司：若阶段账本为空则记录 `initial_screen entered`（evidence ref = mission 版本）；把 Playbook `initial_screen.required_readings` 翻译成机器可核对的 **资料底座清单**：过去 4 个季度财报（SEC company-facts 按季）、过去 4 次电话会转写稿（AlphaEngine 文档类型 transcript）、最新年报 10-K、近 6 个月多方与空方券商观点（AlphaEngine broker research，按评级/结论分多空）、公司 IR 页（web fetch）。每项记录 已有 / 缺失 / 来源不可用。
- **顺带改两件事**：抽取队列按公司 `bootstrap_priority`（P0 ACN 先）与文档类型（transcript、10-K 先于新闻页）排序；发现计划的查询由清单缺项驱动（缺哪一季电话会就搜哪一季），不再只按固定间隔轮询。
- **cockpit**：研究目标页的「子任务」改为 **公司 × 阶段** 表：每家公司当前阶段、清单勾选（已有 / 缺失 / 不可用）、下一步在补什么。研究问题移到「目标」卡片下方单独一节，注明「只由你修改」。
- **授权**：mission v7 已授予 `stage_record` 与 `source_discovery`，无需新发布。
- **验收**：live 五家公司均有 `initial_screen entered`；每家公司的清单在 cockpit 可读；缺项触发的定向搜索能在日志里看到「为 ACN 补 FY26Q3 电话会」这样的句子；ACN 在两个 tick 内开始被读。

### P10b：Claim 质量底座（挑战、退役与按公司索引）

- **为什么先做**：交付物要从 Claim 起草；Ledger 里约 50 条误归属和免责声明类 Claim 会直接写进 Initial Screen。
- **做什么**：`ClaimChallenge` authority（append-only：challenge → 状态 `retired` 的新版本，理由、发起者；自动化可对自己 producer 的 Claim 以确定性规则发起：主题公司不在陈述里且引文所在文档不属于该公司、命中免责声明模式；人可对任何 Claim 发起）。读侧：ClaimIndex 按 `company × aspect × period`，退役的不再出现在问答与交付物上下文里。
- **cockpit**：问答与公司卡只用活跃 Claim；日志里出现「退役了 49 条误归属结论」。
- **授权**：`may_write` 新增词表 `claim_challenge`（代码改 `AUTOMATION_WRITE_SCOPES`）+ **owner 发布 mission v8**（用现有 chain 脚本加 `--add-write-scope`）。
- **验收**：误归属 Claim 全部退役且可追溯；新准入 Claim 的误归属率在一周内为 0（人抽查 20 条）。

### P10c：交付物 authority 与 Initial Screen 自动起草 + 出口门自评

- **做什么**：
  - `MissionDeliverableVersion` authority（human 或 mission automation 写；版本链、hash；字段：kind ∈ DELIVERABLE_KINDS、company_ref 或 industry、template_ref（Playbook `deliverable_templates.initial_screen` 的 S1–S7）、sections[{title, body, claim_refs, evidence_refs, numbers[{value, unit, period, claim_ref}]}]、gaps[]、model_invocation_ref）。**数字纪律 fail closed**：正文里每个数字必须回指一条 quantitative Claim（SEC lane）或标注「缺来源」，不允许模型自由写数。
  - 起草 lane：按公司，用活跃 Claim（按 aspect 分组）+ SEC 数字 + Thesis + Constitution 因果链，分节调用模型（每节一次调用，预算内），组装成 Initial Screen v1；consensus/估值节在市场数据 connector 冻结前写「数据源未接入」。
  - 出口门自评：Playbook `initial_screen.exit_gate` 四问逐条打分（是/否 + 证据 refs）；四问全是且文档非空壳 → automation 记 `gate_passed`（evidence refs = 交付物版本 + 支撑 Claim）；否则 `gate_failed` 并写明缺什么，回到 P10a 的清单补资料。
  - 周报改为「Mission 进度（各公司阶段）+ 新增认知 + 预测变动」（Phase 9 P9f 的定义）。
- **cockpit**：公司卡可点开阅读 Initial Screen（分节、每个数字可点回来源）、出口门自评；日志有「ACN 的 Initial Screen 第 1 版写好了 / 四问自评 3/4，缺 street 盲点」。
- **授权**：`may_write` 新增 `deliverable` → 与 P10b 合并为 **同一次 mission v8 发布**。
- **验收**：五家公司各有一份 Initial Screen 且数字零无源；至少三家 `initial_screen gate_passed`；一份文档从起草到过门的模型费用 < 0.2 USD。

### P10d：Deep Insight Gate 十二问草稿与人审

- **做什么**：对已过 Initial Screen 的公司，自动化按 12 问逐题起草「判断 / 证据 / 反方证据 / 未知项 / falsifier」（每题回指 Claim），作为 `deep_insight_gate` 阶段的交付物草稿并记 `entered`。过门只能由人：cockpit「待你审批」出现「ACN 是否通过深度认知门」，展示 12 题草稿，owner 逐题同意或改写后裁决 → 人类 `gate_passed` / `gate_failed`（失败写回缺口，机器继续补）。
- **授权**：无新增（人审节点本来就在 `human_checkpoints`）。
- **验收**：ACN 的 12 问草稿进入审批页；owner 一次裁决完成；结果写入阶段账本并出现在周报。

### P10e：行业框架与行业模型（行业级子任务）

- **做什么**：行业级阶段账本（company_ref = industry）；行业框架交付物 = Constitution 因果链 + 行业级 Claim + 五家公司横向对比（同 aspect 的 Claim 并列）；行业模型阶段只做 Playbook 规定中数据可得的部分（五家公司收入/增速/margin 的同行对比，来自 SEC facts），需求/TAM、供给、高频数据标注「来源未接入」并列入缺口，不补数。
- **验收**：行业框架 v1 可读；行业模型的缺口清单明确，成为后续接来源的依据（这才是接 Guidepoint / IR 的正当理由）。

### P10f：公司模型与预测线（M2，Phase 9 P9e 顺延）

- **做什么**：SEC XBRL 历史 → 按 `model_discipline` 期间标准的标准化三表（年度 2018A 起、单季 2024Q1A 起）；IT services driver（headcount、utilization、bookings、book-to-bill，能从 filing 与转写稿 Claim 取到的部分）；预测线写入 ForecastLine authority（automation 已获 `forecast_line`）；估值 fail closed 直到市场数据 connector 解冻（owner gate）。每次更新留旧值、新值、差异原因、受影响 thesis。ACN 财年 8 月底结束，Q4 业绩后自动生成第一条 forecast reconciliation（Phase 9 退出门槛保留）。
- **验收**：ACN 三表历史与 filing 勾稽零错误（人抽查 10 个数）；五家公司各有收入与 margin 预测线；ACN Q4 实际数落库后出现 reconciliation 并进周报。

### P10g：Investment Memo（人审）与持续覆盖

- 在 P10c–P10f 之后：memo 按 `deliverable_templates.investment_memo` 起草，12 个 Key Questions 逐条回答，Anti-thesis 为完整反向观点；人审通过后进入 `active_coverage`，事件按五词决定映射到 driver/thesis。本阶段只列不做。

### 贯穿各切片的 cockpit 变化

- 研究目标页三层：**目标**（标题、目标；研究问题作为「引导问题，只由你改」）→ **子任务**（行业 × 阶段、公司 × 阶段，含清单、交付物、出口门）→ **进展**（阶段计数与日志摘要）。
- 「调整方向」增加两种 owner 杠杆的明确路径：**改覆盖名单**（走 `scope_expansion` 人审，本阶段只提示需要 owner 发布 mission 版本）、**改研究问题**（已有）。
- 审批页新增：Deep Insight Gate、Investment Memo、Claim 挑战复核（可选）。

## 需要 owner 跑的授权发布（预计一次）

- **mission v8**：`may_write` 增加 `claim_challenge` 与 `deliverable`。用 `scripts/publish_extraction_authority_chain.py --live --add-write-scope claim_challenge --add-write-scope deliverable`（P10b 时把该参数加进脚本；policy / 常量不变，只发 mission 版本）。发布前在 live Core 副本上排练，与 P9d-17 相同。
- Deep Insight Gate、Investment Memo 的裁决在 cockpit 内完成，不需要命令行。

## 继续冻结

新来源接入（Guidepoint、company-IR 只在 P10e 缺口清单明确指向时才开）、自动 thesis revision、Capability builder/sandbox、第二 runtime、embedding-first 检索、multi-agent fleet、市场数据/估值引擎（owner gate）。cockpit 不再加视图，只在五视图内填内容。

## Phase 10 退出门槛

- live 阶段账本非空：五家公司 `initial_screen` 至少三家 `gate_passed`，其余有明确缺口；ACN `deep_insight_gate` 已由 owner 裁决一次。
- 五份 Initial Screen 可在 cockpit 阅读，正文数字零无源（人抽查）。
- 行业框架 v1 与行业模型缺口清单存在。
- ACN Q4 实际数落库后自动生成 forecast reconciliation 并进周报（Phase 9 门槛保留）。
- 全程逐条人工审批数为 0；只有 Deep Insight Gate、Investment Memo、Claim 挑战复核、扩范围、扩预算、Thesis 变更走人。
- **止损**：到 2026-09-28 仍没有一家公司 `initial_screen gate_passed`，停止 P10d 之后的切片，只修 P10a–P10c。

## 与愿景的关系

v0.1 第 1 节：「人只定目标和边界、校正航向、讨论、读结果；Dalton 自主形成议程、拆计划、积累证据和模型、检验 thesis、重新规划。」Phase 9 把「拆计划」的方法论写进合同，P9d 把「积累证据」做成全自动，ADR-0006 把「定目标、校正航向、讨论、读结果」收进一个页面。Phase 10 补的是「按计划把证据变成交付物并过门」——这是 owner 看到子任务时期待的那一层，也是从「有很多 Claim」到「有一份可读的首次覆盖」之间唯一缺的东西。
