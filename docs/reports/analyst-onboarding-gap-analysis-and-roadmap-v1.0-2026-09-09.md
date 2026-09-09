# Dalton 作为「新分析师」入职：能力差距分析与开发蓝图 v1.0

日期：2026-09-09
状态：最终版，供 owner 裁决；替代同日 v0.1 草稿，不改写任何历史报告
基线：[愿景 v1.1](vision-and-next-phase-v1.1-2026-09-07.md)、[PROJECT_STATUS](../PROJECT_STATUS.md)（2026-09-09）、ADR-0001～0006、Playbook v1（`deploy/phase9/p9a-research-playbook-v1.json`）、live 账本只读抽样（core.sqlite 副本）
方法：通读 SPEC、六份 ADR、CONNECTOR_PROTOCOL、愿景与 Phase 8/9/10 报告、PROJECT_STATUS 全文、`src/dalton_core` 研究层与模型层模块；只读复制 live Core，抽样读了 ACN Initial Screen v2 全文、两条 Thesis、最新周报、最新 research plan、Claim 与文档分布

---

## 0. 一句话结论

Dalton 今天是一个**纪律极好的 Level 1 分析师**（对应 Playbook `analyst_levels` 的 Basic Level 1 — Desktop Research）：会按手册找资料、读原文、把每句话记成可溯源的 Claim、写出一份数字零无源的 Initial Screen，而且 7×24 不停。它还不是一个**能被 PM 问「你怎么看」的分析师**：它没有市场（价格、consensus、估值），没有「认知」这一层（对公司的凝练理解、多空辩论地图、管理层风格），没有真正的财务模型与预测，thesis 一旦人签就再也不动，不会解释股价为什么涨跌，也开不了周会。

差距不在方法论（手册已经完整进了 Playbook 合同），不在治理（人闸、预算、溯源都很扎实），而在**手册六阶段里只有第一阶段被代码执行**，以及**「信息 → 认知 → 观点 → 预测 → 复盘」链条中间三环缺失**。

---

## 1. 团队对分析师的要求（owner 原文，2026-09-09）

> 我们是一家 fundamental long-biased 对冲基金，今天我们有一个新的分析师入职。新的分析师是金融学背景，有非常强的财务分析能力，但对具体股票和行业的知识不算多。
>
> 我们基金维护有一个 wiki，里面包括了我们对一个公司建立初次覆盖、完整覆盖、后续跟踪时，主要需要关注的问题，以及分析的基础方法论（这只是一个通论，每个行业当然会有每个行业的特点）。
>
> 新分析师入职之后，我们会先让他学习掌握这个 wiki。然后，我们对新分析师的要求一般是先建立对一个公司或者行业的首次覆盖。
>
> 他会自己去各个地方查找资料（比如卖方研报、公司公告、互联网检索、专家访谈纪要、sales note 等），从里面逐渐归纳凝练出他对于这个行业和公司的认知。比如，公司主要业务是什么，这个行业有什么特性（比如是周期性行业），行业的长期和短期驱动因素是什么，某一家公司有怎么样独特的竞争位置，长期以来形成了什么样的竞争壁垒，或者有什么壁垒在被破坏，历史上股价和估值的主要驱动因素是什么，现在市场主要的 bull bear debate 在关注什么问题，当前股价位置如何，如何去预想后面 narrative 的演绎，有没有什么 high conviction 的超预期变化机会值得关注（比如市场现在计价过于悲观，事实上发现因为一个事情叙事可能就会反转，或者反之，或者实际上是某个宏观因素在驱动整体板块的股价等等）。分析师进行了初步覆盖后，建立了对这个行业的初步认知。
>
> 然后，PM 可能会安排分析师对这个行业或公司进行完整覆盖。他会找更多资料，完整拆解这个公司的业务，理解行业、竞争格局、公司管理层、市场情绪，着重进一步分析他认为的对公司这个阶段最关键的问题，建立财务模型和预测；这个过程中，他可能形成了更深度的认知，也可能修正了之前的错误认知。
>
> 此后，他就成为了这个行业/公司的覆盖分析师。他需要每天保持对这个行业的覆盖和跟踪，捕捉公司股价为什么变化，市场围绕这个公司争论的问题向哪个方向转变（还是市场的 debate 转向了新的问题），分析师认为市场是在正确定价，还是加深了错误定价，及时更新财务预测。在关键时间点（例如业绩），对公司认知进行校准。如果遇到新的认为值得研究的问题，会自己主动去进行专项研究。这个过程中，他对于行业和公司的认知越来越深入（比如，他知道了这个公司管理层每次给 guidance 的风格，懂得了怎么前瞻性地捕捉影响 driver 变化的指标等等）——换句话说，越来越多的信息凝练成了越来越深的认知，而且这一认知不是固化不变的，是与时俱进的。
>
> 作为分析师，PM 可能经常会问他当前对这个公司的看法，或者对某个事件对这个公司的影响的看法等等。这个分析师要根据他对行业和公司的最新观点，以及必要时进行新的检索去响应 PM 的问题。在每周周会上，分析师还会被要求对过去一周的股价表现进行复盘，讨论观点变化，如果有高 conviction 的投资机会要主动 call。

把这段描述压成六个环节，后文逐环对照：
**① 学 wiki → ② 首次覆盖（找资料、凝练认知、识别 debate 与预期差）→ ③ 完整覆盖（拆业务、关键问题、建模型、修正认知）→ ④ 持续覆盖（日跟踪、股价归因、debate 转向、更新预测、业绩校准、专项研究、认知加深）→ ⑤ 响应 PM（当前看法、事件影响，必要时补搜）→ ⑥ 周会（股价复盘、观点变化、主动 call）**。

---

## 2. Dalton 现状速写（2026-09-09 live）

| 项 | 数值 / 状态 |
| --- | --- |
| 仓库 | 约 13.4 万行 Python、94 份 JSON Schema、226 份文档、398 次提交（2026-08-14 起，约 4 周） |
| 任务 | 一个 CoverageMission `us-it-services`（ACN / CTSH / EPAM / IBM / DXC），版本已到 v9+ |
| 跑在 tick 上的 lane | source discovery、document extraction（数字 / 定性 / 指标发现三条 pass）、mission stage、claim review、SEC quarters、statements、company model spec、research plan、initial screen |
| Claim | 2,170 条：定性 2,148、定量 22；`aspect` 字段全空；有 2023 年 DXC 人事新闻等陈旧条目，同季收入存在三条重复 Claim |
| 文档 | 3,501 份（web-search 1,978、AlphaEngine 1,503、SEC 20） |
| 报表 | 26 份季报、10,023 行报表行（P13ak）；9 份逐公司建模规格（P13am） |
| Initial Screen | 5 份已发布；ACN / EPAM / IBM / DXC `gate_passed`，CTSH `entered`（缺电话会，卡在 AlphaEngine 130 次/24h） |
| Thesis | 2 条（行业级、ACN），人签，`claim_refs` 为空；thesis-impact 定时任务已停泊（09-06） |
| 预测 | 4 条 ACN 收入线（一条冻结外推公式）；forecast reconciliation live 0 条，等 ACN 10/1 |
| 市场数据 | 价格 / 股本 / 汇率 / 利率 / consensus 五类 authority 全部未接入；估值 fail closed |
| 连接器 | 12 个模板；SEC、sec-financials、AlphaEngine、web-search、web-fetch connected；Guidepoint 身份已批 lane 未建；roic 整站 403；x / reddit / xueqiu / cninfo shadow；company-IR 未接 |
| 人闸 | mission 发布改版、connector 治理、Deep Insight Gate、Investment Memo、Thesis 准入 / 修订、forecast_overturn、扩范围 / 预算 |
| 测试 / 成本 | 1,995 通过；当日模型开销 $7.14 / $100 |

设计哲学（SPEC、ADR）可以概括为「boring kernel, smart edges」：Core 是 headless、append-only、哈希绑定的权威层；模型只在三个刀刃上用（提炼问题、执行研究、验证与反方挑战）；自动化能写的东西由 `may_write` 封闭词表限定，Thesis / Constitution / Playbook / Mission 永远不在词表里；每个数字必须回指 filing 或工具结果。这些原则在本蓝图里全部沿用。

---

## 3. 逐环对照：Dalton 欠缺什么

判定：**有** = live 上跑着；**半** = 有对象 / 合同但没有 lane 或没接线；**无** = 仓库里没有对应对象。

### ① 学 wiki

| 要求 | 现状 | 判定 |
| --- | --- | --- |
| 掌握团队方法论 | Playbook v1 六阶段、出口门、Deep Insight Gate 12 问、交付物模板、`model_discipline`、`evidence_discipline`、`risk_reward_standards`、`tracker_classes` 全部合同化（`research_playbook.py`），人类发布、哈希绑定 | 有 |
| 方法论被「执行」而不只是「存着」 | 只有 `initial_screen` 有 stage driver 与出口门评估器；`metric_base.STAGE_SPINE` 对其余五阶段为空元组；Constitution 的 `method`（question_admission / causal_chain / output_rubric）全仓无消费者；DoctrinePack 只服务 Phase 8 的 bounded planner；`DECISION_VOCABULARY` 五词无代码消费 | 半 |
| 行业特有方法（周期性等） | 只有 `IndustryDriverPack`（4 driver / 13 metric spec，实际入账的只有 revenue growth）与 Constitution 因果链；Deep Insight Gate 第一问的行业分类（商品周期 / 资本周期 / compounder / 结构成长 / 转型）没有对象承载 | 半 |

### ② 首次覆盖

| 要求 | 现状 | 判定 |
| --- | --- | --- |
| 找资料：卖方研报、公告、互联网、专家访谈、sales note | SEC、AlphaEngine、网页搜索 connected；Guidepoint lane 未建；company IR、sales note、insider、第三方数据 not_connected；roic 不可用 | 半 |
| 公司主要业务是什么 | S1 由 LLM 从 Claim 起草；没有结构化的业务拆解对象（分部、收入 mix、客户、定价模式）。`CompanyModelSpec` 有 revenue_drivers / expense_lines，但服务建模不服务认知 | 半 |
| 行业特性、长短期驱动 | S2 行业概览（简版）；`industry_framework` 只在 `DELIVERABLE_KINDS` 枚举里，无生成代码（P10e 未做） | 半 |
| 竞争位置、壁垒及其破坏 | 散落在定性 Claim；无 competitive-position / moat 对象；`IndustryEvidencePack.debates` / `driver_views` 有形状但由人手工发布 v1–v5，不自动填充 | 无 |
| 历史股价与估值驱动 | 无任何价格、行情、consensus、估值数据源（全仓 grep 零命中）；Initial Screen S6 永远留空 | 无 |
| 当前 bull / bear debate | 研报进了账本，但只被切成「某人说了什么」的 Claim；无 debate 对象组织阵营、焦点与转向 | 无 |
| 当前股价位置、narrative 演绎预想 | 无价格；无 narrative 对象 | 无 |
| high conviction 超预期机会 | 无 consensus、无价格即无「预期差」；无 call 对象 | 无 |
| Initial Screen 文档本身 | 文字是真研究文字（ACN v2 识别了 reinvention 重组、AI 转化、联邦业务与 DOJ 调查、合同可随时终止、EMEA/APAC vs 美洲分化）。但：只有收入一个数字系列；S7 引用标记剥离后留下「、、（同一季度数据重复）显示」残句；同季收入被三条重复 Claim 并列引用；`gate_passed` 是终态，弱证据下过闸的文档永不重写（PROJECT_STATUS 待办第 3 条） | 半 |

### ③ 完整覆盖

| 要求 | 现状 | 判定 |
| --- | --- | --- |
| 完整拆解业务、理解管理层、市场情绪 | Deep Insight Gate 12 问有合同无 lane（P10d 未做）；管理层评估（资本配置、承诺兑现、guidance 风格）无对象 | 无 |
| 着重分析当前阶段最关键问题 | `research_planner` 已能产出高质量 `inquiries`（live 例：「EPAM 的 adjusted margin 三种口径是否可对账」「IBM 的 ARR / NNARR 描述的是哪个业务与期间」），但 inquiry 没有下游，不会变成专项研究任务 | 半 |
| 建财务模型与预测 | 报表行入库、逐公司建模规格、季度序列纯函数（P13an，未接线）都在。**规格 → 序列 → 模型输入表的 join 未写**；预测只有 `formula:quarterly-growth-extend:1`；无三表联动、driver 模型、敏感性、consensus bridge、Excel 导出 | 半 |
| 修正之前的错误认知 | Thesis 只能人签；thesis-impact 停泊；代码注释写着 "a later thesis updater"；gate 无重开机制 | 无 |

### ④ 持续覆盖与跟踪

| 要求 | 现状 | 判定 |
| --- | --- | --- |
| 每天捕捉股价为什么变化 | 无价格、无事件检测 | 无 |
| debate 向哪转、有无新 debate | 无 debate 对象；周报只做 Claim diff | 无 |
| 市场定价对不对 | 无 consensus / 估值 | 无 |
| 及时更新财务预测 | 非 derived 预测行 human-only；无自动「预测修订提案」 | 无 |
| 业绩时点校准 | `forecast_reconciliation` 三档（<1% / 1–3% / ≥3% 进人审）已部署，live 0 条；无业绩前 preview / 业绩后校准工作流；无事件日历 | 半 |
| 主动专项研究 | `adhoc_research` 路由被合同硬禁用；planner inquiry 无落点 | 无 |
| 认知加深（guidance 风格、前瞻指标） | 无「公司档案」层：Claim 是原子引文，不按主题聚合、不衰减、不去重；手册的 analyst journal 无对象 | 无 |

### ⑤ 响应 PM

| 要求 | 现状 | 判定 |
| --- | --- | --- |
| 当前看法 | cockpit「随时提问」只读正式 Claim + Thesis（≤400 条），Claim 级引用，不读 figures / statement_lines / forecast / 规格；`answer_after_refresh` 补搜 live 0 次 | 半 |
| 事件影响 | thesis-impact 有生产者 + 独立 verifier + 30 例校准集，但已停泊；只回答 supports / weakens / no_change / insufficient，不给量级、不给预测影响 | 半 |
| 入口 | 只有 tailnet cockpit；无 Feishu / CLI；Discord 只收 ✅/❌ | 半 |

### ⑥ 周会

| 要求 | 现状 | 判定 |
| --- | --- | --- |
| 复盘上周股价表现 | 无价格 | 无 |
| 讨论观点变化 | 周报 v1 = 确定性 Claim diff + thesis binding + reconciliation，无叙事；v1.1 计划的「阶段进度 + 新增认知 + 预测变动」未做；反馈词表无 UI | 半 |
| 主动 call 高 conviction | 无 | 无 |

### 横切面

- **没有研究质量评估**：1,995 项测试全是管道正确性；Initial Screen、Claim 相关性、问答、周报没有 rubric、golden set 或 PM 评分回路。
- **Claim 层噪声**：`aspect` 全空、无时效、无去重、无重要性，会直接污染交付物与问答上下文。
- **信息瓶颈**：AlphaEngine 130 次/24h（owner 已定）是 CTSH 卡住的直接原因；roic 不可用；Guidepoint 未建。
- **单人自证**：仓库长期风险项；同一 agent 写代码、写测试、写报告。

---

## 4. 差距的结构性归纳：五个缺失层

按「对 PM 有用」的价值排序：

1. **市场层（Market）**：价格、股本、consensus、估值。是 S6、memo S7/S8、股价复盘、预期差、周会复盘的共同前提。Dalton 现在对市场是「盲」的，所以它的 thesis 永远不能对照「市场已经 price in 了多少」。
2. **认知层（Understanding）**：Claim（信息）与 Thesis（观点）之间缺一层可演化的「公司档案」：业务拆解、driver tree、竞争位置、管理层与 guidance 风格、KPI 字典、debate 地图、催化剂日历。owner 描述的「越来越多的信息凝练成越来越深的认知，而且与时俱进」正是这一层。
3. **模型层（Model）**：规格已在、数据已在、join 没写；预测、敏感性、consensus bridge、导出都没有。
4. **演化层（Evolution）**：thesis 更新、预测修订提案、五词决定、gate 重开、事件 → driver → thesis 映射。现在系统只会「加」，不会「改」。
5. **对话层（Dialogue）**：读全部权威的问答、事件影响问答、专项研究派发、周会简报、投递、PM 反馈回写。

外加两条贯穿：**质量评估回路**与**来源补齐**（Guidepoint、company IR、sales note、insider）。

---

## 5. 开发蓝图

### 5.1 设计原则（沿用仓库现有哲学）

- 新增的每一层都是 append-only、哈希绑定的 authority；自动化写入需 `may_write` 扩项与 owner 发新 mission 版本。
- 市场数据与 consensus 是 `observed` 值，必须经 connector transport 哈希入库；估值倍数是 `derived_deterministic`，公式冻结、可重放。
- 认知层由自动化写，但每一版必须带 `change_reason` 和新 Claim / 新数字 refs；不带新证据的改写被权威拒绝（防「复述漂移」）。
- Thesis 仍由人准入；自动化只能提交 **thesis revision candidate**（带五词决定与证据），人一键裁决。这是对 ADR-0001 / ADR-0004 的最小扩展，需要新 ADR。
- 每一片都要有「质量验收」，不只是「管道跑通」。

### 5.2 五条主线与切片

编号沿用仓库风格，从 Phase 11 起。每片：隔离测试 + live 部署 + cockpit 可见 + 质量验收。

#### Phase 11 市场层：让 Dalton 看得见市场（1.5–2 周，关键路径）

- **P11a 价格 authority**：`MarketPriceSeriesVersion`（日频 OHLCV、股本、市值；每点绑 connector invocation）。connector 走现有 `ConnectorProfileVersion` 治理，owner 批一次。数据源需 owner 定（候选：Polygon / EODHD / Yahoo 非官方；无 API 时允许人工 CSV 投喂过渡，先有再好）。
- **P11b consensus authority**：两路并行。(1) 从 AlphaEngine 研报抽「目标价 / 评级 / 下一年收入与 EPS 预测」为 `StreetEstimateClaim`（定量 Claim 新子类，走 `document_numeric_claim` 逐字核对；两份研报互证才成 consensus 区间，沿用 metric_discovery 互证规则）；(2) 若有付费源（Visible Alpha / FactSet / Bloomberg 导出），作 `ConsensusEstimateVersion` 接入。
- **P11c 估值投影**：`ValuationSnapshot` = derived_deterministic（P/E、EV/EBITDA、FCF yield、历史分位；公式冻结）。解冻 `VALUATION_AUTHORITY_ROLES` 闸门，Initial Screen S6 开始起草。
- **P11d 价格事件检测**：日 tick 计算相对行业等权 / 大盘的 abnormal move（阈值进 policy）；产生 `MarketEvent` 供演化层消费。
- **验收**：五家有 ≥3 年日线；ACN 的 S6 写出 street 预期与估值分位且数字全可回指；cockpit 公司卡显示价格与估值；一次 ≥3% 异动被记录。

#### Phase 12 认知层：从 Claim 到「公司档案」（2–3 周，可与 Phase 11 并行）

- **P12a `CompanyDossierVersion`**：按 aspect 分节的版本化档案（business_model / segments_and_mix / demand_drivers / supply_and_cost / competitive_position / management_and_capital_allocation / guidance_style / kpi_dictionary / catalyst_calendar / history_of_price_drivers）。自动化按公司 × aspect 起草，输入 = 活跃 Claim（按 aspect 分组）+ 数字 + 规格；发布规则：新版必须引用至少一条上一版没有的 Claim / 数字，否则 duplicate。
- **P12b Claim 索引升级**：给 Claim 打 `aspect`（封闭词表，与 dossier 分节一致）、`as_of` 时效、`importance`（确定性：一手 filing > 管理层原话 > 卖方 > 新闻）、同 subject × metric × period 去重合并。这是 P10b「按 company × aspect × period 索引」的未完成部分。同时把已核验数字接进 Ledger（PROJECT_STATUS 待办第 6 条，需 ADR 放开 `CandidateStagingStore.stage` 对 cited-original 定量候选的拒绝）。稳定后再考虑 embedding 检索（当前冻结项，不动）。
- **P12c `DebateMap`**：每公司 / 行业的争议清单 `{question, bull_position[claim_refs], bear_position[claim_refs], status(open/shifting/resolved), last_shift_reason}`；自动化从研报 Claim 与 dossier 起草；周报读它。
- **P12d Deep Insight Gate 12 问草稿 + 人审**（即 v1.1 的 P10d）：从 dossier + DebateMap + 数字起草；cockpit 待审批页裁决；行业分类作为 dossier 字段落地。
- **P12e 行业框架交付物**（即 P10e）：Constitution 因果链 + 行业级 Claim + 五家横向对比；TAM / 供给 / 高频数据缺口如实列缺口，成为接 Guidepoint / IR 的依据。
- **P12f 管理层与 guidance 档案**：从历次电话会 Claim 抽「guidance 原话 → 实际 → 偏差」，形成 guidance_style（保守 / 激进 / beat-and-raise 模式）。这是 owner 说的「知道管理层每次给 guidance 的风格」。
- **验收**：ACN 档案十节可读、每节引用可点回；ACN 12 问草稿进入审批并被 owner 裁决一次；DebateMap ≥3 条争议且 bull / bear 各有 ≥2 条独立来源；分析师抽查 20 条 Claim 的 aspect 标签，错误 ≤2 条。

#### Phase 13 模型层：真正的公司模型与预测线（2–3 周，依赖 P13an 序列）

- **P13-M1 规格 × 序列 join**（PROJECT_STATUS 待办第 1 条）：`ModelInputTable` 投影，行 = 规格科目、列 = 季度、格 = {value, kind(reported/derived), accession}。
- **P13-M2 driver 模型与预测行存储**（待办第 2 条）：`ForecastLineVersion` 扩为 driver → assumption → result 三层；自动化可写 assumption 行但标 `estimate` 并带 `because` 与 Claim refs；三表最小联动（收入 → 毛利 / 费用 → 营业利润 → 净利润 → FCF），资产负债表只做规格标 required 的。
- **P13-M3 敏感性与 consensus bridge**：3–5 个关键 driver 对照历史峰 / 谷 / 均值；与 P11b 的 street 预期做 bridge。
- **P13-M4 业绩对账接通**：ACN 10/1 后第一条 reconciliation 自动生成；对账结果同时喂 dossier（guidance_style）与演化层。
- **P13-M5 Excel 公式导出**（owner 已定：交付时才做，导出公式非数值）。
- **验收**：ACN 历史三表与 filing 勾稽零错误（人抽 10 个数）；五家有收入与 margin 预测线；ACN Q4 后出现 reconciliation；`company_model` 阶段账本非空。

#### Phase 14 演化层：让认知和观点会动（2 周，依赖 11/12/13 各自最小版）

- **P14a 事件流**：`ResearchEvent` 统一对象，来源 = 新 filing / 8-K / 电话会 / 研报评级变化 / MarketEvent / 对账结果；每个事件由自动化映射到 driver 与 thesis，给出五词决定（`DECISION_VOCABULARY` 首次被代码消费）；写入 `active_coverage` 阶段账本。
- **P14b thesis revision candidate + ADR-0007**：自动化可提交 `ThesisRevisionCandidate`（statement 变化、confidence 变化、falsifier 命中、证据 refs）；人裁决。修好并重开 thesis-impact 定时任务，产出流向 candidate。
- **P14c 预测修订提案**：事件或对账触发 `ForecastRevisionProposal`（旧值 / 新值 / 差异原因 / 受影响 thesis）；≥ policy 阈值走 `forecast_overturn` 人审，否则自动化写 estimate 行。
- **P14d gate 重开策略**：`gate_passed` 增加 `superseded_by_reopen` 事件（证据厚度或模型换代触发，条件进 policy），解决待办第 3 条。
- **P14e 专项研究派发**：planner 的 `inquiry` 变成 `ResearchTask`（预算、截止、交付物）；解除 `adhoc_research` 硬禁用但保持 mission 预算内；cockpit 可见「正在专项研究 X」。
- **P14f 业绩季工作流**：财报前约 1 个月生成 preview（预期 vs 我们 vs 关键看点）；财报后 48 小时内生成校准（inline / better / worse 及原因、模型更新、五词决定）。
- **验收**：ACN 10/1 业绩从 preview → 对账 → 校准 → thesis candidate 全链路自动跑通、人只裁决一次；一次 ≥3% 异动当天日志里有「为什么动」并映射到 driver。

#### Phase 15 对话层：PM 能用（1.5 周，可与 Phase 14 并行）

- **P15a ask v2**：上下文扩到 dossier、DebateMap、数字表、预测线、估值、最近事件；回答带 confidence 与「我不知道 / 需要补搜」；支持一次受预算的补搜（打通 `answer_after_refresh`）。
- **P15b 事件影响问答**：「X 对 ACN 影响如何」→ P14a 事件映射 + thesis impact 生产者 / verifier；回答含方向、量级（若可算）、对预测与 thesis 的影响、下一验证。
- **P15c 周会简报 v2**：上周价格表现与归因（P11d）、观点变化（P14b）、debate 转向（P12c）、预测变动（P14c）、缺口与下周计划；叙事由 LLM 起草，结构与数字由权威强制。
- **P15d 高 conviction call**：`ConvictionCall` 对象（方向、variant view、consensus 差、event pathway、是否满足 `risk_reward_standards`、时间跨度）；自动化只能提案，人裁决后进周报。
- **P15e 投递与反馈**：Feishu / Discord 投递；PM 对回答与周报的 `read / useful / needs_more_evidence / disagree / revise` 反馈回写为 `AnalystJournalEntry`，成为后续起草上下文（手册的 analyst journal）。
- **验收**：PM 用自然语言问 10 个真实问题，≥8 个被评为「可用」；周报被 PM 读完并留反馈；一次 call 提案进入审批。

### 5.3 贯穿主线

- **Q 质量评估回路**（从 Phase 11 起同步建）：为 Initial Screen、dossier、问答、周报各建 rubric 与 5–10 例 golden set；PM 打分入 `AnalystJournal`；每片验收必须含质量指标。第一周先修 P10c 的引用标记残句与重复 Claim 并列引用。
- **S 来源补齐**（按 P12e 缺口清单驱动，不提前接）：Guidepoint lane（待办第 10 条）；company IR / 8-K exhibit；sales note 与内部会议纪要的人工投喂入口（`tracker:third-party-data` 的「人工输入喂 wiki」）；insider 交易（Form 4 / 144）；roic 撤回或换 transport（待办第 4 条）。
- **G 治理扩项**：`may_write` 增加 `dossier`、`debate_map`、`market_event`、`forecast_revision_proposal`、`thesis_revision_candidate`、`research_task`、`conviction_call`；`CHECKPOINT_KINDS` 增加 `thesis_revision_candidate`、`conviction_call`、`gate_reopen`。预计 owner 需发布 mission 版本 3–4 次。

### 5.4 顺序、依赖与时间

```
第 1–2 周   Phase 11 市场层 P11a→d          ‖ Phase 12 P12a / P12b（含数字进 Ledger）‖ Q: rubric + 修 P10c 残句
第 3–4 周   Phase 13 模型层 M1→M3           ‖ P12c / P12d / P12e / P12f              ‖ S: Guidepoint lane
第 5–6 周   Phase 14 演化层 P14a→f（ACN 10/1 业绩为实战）‖ P13-M4
第 7–8 周   Phase 15 对话层 P15a→e          ‖ P13-M5 Excel 导出                      ‖ S: IR / sales note 投喂
```

按仓库近期节奏（09-07 至 09-09 三天约 110 次提交、每片当日部署），八周是紧但可行的估计。关键路径是 Phase 11，因为 S6、股价复盘、预期差、周会全部压在它上面。

### 5.5 需要 owner 裁决的事项

1. **市场数据源**：用哪家（免费 / 付费）、是否允许人工 CSV 投喂过渡。
2. **consensus 来源**：只从研报抽（免费但稀疏）还是接付费源。
3. **ADR-0007**：自动化可否提交 thesis 修订候选（人裁决）。
4. **gate 重开策略**：证据变厚是否重出 Initial Screen；条件写进 policy。
5. **解除 `adhoc_research` 硬禁用**的边界（预算、频率、写入范围）。
6. **AlphaEngine 130/24h**：维持则 CTSH 与研报 consensus 抽取都受限，建议至少为 P11b 开一个独立窗口。
7. **投递渠道**：周报与 call 走 Feishu 还是 Discord。

### 5.6 与现有下一步清单的关系

PROJECT_STATUS 的 11 条待办里：第 1、2 条（规格 join、预测行形状）就是 P13-M1 / M2；第 3 条是 P14d；第 6 条（已核验数字进 Ledger）是 P12b 与 P13-M1 的前提，提前到第 1 周；第 10 条 Guidepoint 归 S 线；第 4、5、7、8、9、11 条是维护项，不挡主线。v1.1 的 P10d / P10e / P10f / P10g 分别对应 P12d / P12e / Phase 13 / P15d 之后的 memo。本蓝图没有推翻它们，只是把市场层与认知层提到它们前面或并行，因为没有这两层，Deep Insight Gate 第 7、8 问（股价复盘、共识与多空逻辑）和 memo 的 S7 / S8 根本写不出来。

---

## 6. 风险与止损

- **市场层做不出来**：S6、复盘、call 继续 fail closed；届时至少完成认知层与模型层，Dalton 仍是「不看价格的基本面分析师」。
- **认知层复述漂移**：dossier 版本必须绑定新证据才能发布；不满足就 duplicate。
- **thesis 自动修订失控**：候选制 + 人裁决 + 独立 verifier；不放开自动 commit。
- **质量停在「像研究」而不是「是研究」**：每片验收必须含 PM 打分；连续两片打分不过就停新功能修质量。
- **单人自证**：建议每个 Phase 结束由 PM 或第二位分析师做一次盲评。

---

## 附录 A：本次分析读到的 live 样本摘录

- **ACN Initial Screen v2**（2026-09-09，10 次模型调用）：S3 核心 Thesis 写出「AI 驱动的市场份额获取正在推动收入增长，但增速尚未稳定在更高水平……证据能支持方向性判断，不足以证明拐点」；S4 Anti-thesis 提出「收入增长但需求空心化」的完整反向观点；S5 给出对 universe 内二线厂商的空头含义与跨区域读法；S6 留空；S7 残句「、、（同一季度数据重复）显示」。数字只有 4 个季度收入及同比。gaps 列表清楚：缺 bookings、utilization、分部、指引原文、consensus。
- **两条 Thesis**：行业级「US IT services demand is approaching a bottom」（confidence low）、ACN「AI and reinvention plus managed services can offset soft discretionary consulting」（medium）；均为 2026-08-27 人签，`claim_refs` 为空，之后无版本变化。
- **最新 research plan**（2026-09-09）：assessment 指出「文档收集基本完成，模型就绪度不足：五家公司只有五个数字」；12 条 directives 全是 stop（取消已满足清单项的排队获取、为 CTSH 保留 AlphaEngine 额度）；3 条 inquiries 全是高质量的口径核对问题。说明规划大脑已经够用，缺的是把 inquiry 变成行动的下游。
- **最新周报**：2026-08-27 至 09-03，确定性 Claim diff，`changed_driver_refs` 只有 `driver:revenue-growth-usd-gaap`。

## 附录 B：连接器层逐模块审读的补充发现

这些发现与第 2 节的连接器状态一致，但对蓝图的 S 线（来源补齐）与 Phase 11 有直接影响：

- **SEC 只实现了 `submissions` 与 `companyfacts` 两个端点**（`sec_public_adapter.py`）；`frames` API 不存在。`get_company_facts` 无日配额条目，靠 plan 的 60 秒窗口约束。`xbrl_concept_resolver.py` 只被测试引用、未接线，生产上的 concept 选择仍是 `research_plan.py` 里三项平铺 allowlist。P13-M1 做 join 时应顺手把它接上或删掉。
- **`edgartools` 是可选 extra `[sec-financials]`**，仓库 `.venv` 未安装（live 的 Dalton venv 由 `install.sh` 安装，不受影响）；未安装时 statements lane 会带理由拒绝而非崩溃。
- **治理记录缺口**：`sec-filings-index-v1.json` 在 `deploy/connector-governance/` 与 `install.sh` 里都不存在，但 `macos_launchagent.py` 引用它；只能在机器上用 `dalton-connector-governance propose` 生成。属于「一个决定只改了一处」的老问题，建议随 S 线一起补。
- **AlphaEngine** 的 `SEARCH_DOCUMENT_TYPES` 覆盖 research_report / foreign_report / domestic_report / sell_side_report / sell_side_comment / meeting_minutes / announcement / news，地域过滤 US / HK / A；Dalton 桥只放行 `search_library` 与 `get_document`。代码常量 `MAX_CALLS_PER_WINDOW=30`，live 130 由启动参数覆盖。P11b 从研报抽 consensus 走的就是这条通道，所以第 5.5 节第 6 项的独立窗口是必要的。
- **公开网页**：Gemini web search 每次最多 10 条；`PublicHttpTransport` 不读 robots.txt；web-fetch 是每主机一个 profile、无通配。live 403 站点：seekingalpha、alphastreet、spglobal、stockanalysis、reddit；成功：quartr、tikr、futunn。Phase 11 若走非官方行情站点会撞同样的墙，是选付费 API 的一个理由。
- **公开网页财报电话会连接器**（`deploy/connector-proposals/earnings-call-transcript/`）为 `inventory_state: proposal_only`，无治理记录、无 capability、无 lane，是桩不是通道。电话会仍只能靠 AlphaEngine。
- **Bloomberg / FactSet / Refinitiv / Koyfin / Tegus / Feishu 在仓库中零出现**；Firecrawl 仅散文提及无代码。P15e 的 Feishu 投递需从零建 connector。
- OpenClaw skills 调研位于 `PROJECT_STATUS.md` 2026-09-09 条目，无独立报告。

## 附录 C：审阅覆盖范围与未覆盖项

- 已读：SPEC、ADR-0001～0006、CONNECTOR_PROTOCOL、愿景 v1.1、Phase 8 / 9 / 10 报告、P9c、M1、P10a-c、PROJECT_STATUS 全文、Playbook / Mission / Constitution JSON、研究层、模型层与连接器层核心模块、live 账本抽样。
- 未做：运行测试套件、复核 cockpit 页面实际渲染、评估各模型 profile 的产出质量差异、核对 live Dalton venv 的依赖安装状态。
