# 分析师蓝图的并行开发计划 v1.0

日期：2026-09-09
状态：执行中；owner 已认可分法（2026-09-09）
基线：[能力差距分析与开发蓝图 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md)、PROJECT_STATUS（2026-09-09）、main `490e63f`
分工：主 agent（Claude Fable）定计划、写规格、集成与更新文档；写代码全部由 Opus 5 subagent 在各自 git worktree 完成

---

## 0. 一句话

蓝图八周的串行估计可以压到四到五周，条件是**先用半天把「加一条 lane 要碰 23 个共享文件」收成「一行 import」**，然后再分叉。没有这一步，三个 agent 同时加 lane 必然在 `writer_server.py` 的十个注册点上撞车。

## 1. 已定的决定（owner，2026-09-09）

| 蓝图 5.5 事项 | 决定 |
| --- | --- |
| 市场数据源 | **yfinance**（Yahoo 免费源），与 openclaw 现有 skill 同源。沿用 openclaw MEMORY 的教训：`yf.download(auto_adjust=False)`，Close 与 Adj Close 分列入库，绝不在 adjusted 价上再加股息 |
| consensus 来源 | **双路都走免费源**：(1) 从已入库研报抽 `StreetEstimateClaim`；(2) yfinance 的 `analyst_price_targets` / `recommendations` / `earnings_estimate` / `revenue_estimate` 作 `ConsensusEstimateVersion`。AlphaEngine 130/24h 暂不动，素材不够时 owner 再调 |
| 开发方式 | 主 agent 定计划，Opus 5 subagent 写代码，各自 worktree，主 agent 集成 |
| 文档 | 每片进度即时写进 PROJECT_STATUS；每个 agent 交付时附自己的报告 |

**owner 第二批裁决（2026-09-09 下午）**：

| 事项 | 决定 |
| --- | --- |
| ADR-0007 | **接受**：自动化可提交 thesis 修订候选，人裁决；已核验数字可进 Ledger |
| gate 重开 | **证据变厚可重出 Initial Screen，但必须版本化**：老版本永不删除，认知迭代要能从版本链上看出来。`gate_passed` 不再是终态，而是某一版的状态 |
| `adhoc_research` 硬禁用 | **解除**。边界由主 agent 按「对分析师的要求」定：在 mission 预算内、每条专项研究是一个带预算与截止的 `ResearchTask`、写入范围不超过 `may_write`、cockpit 可见 |
| 周报投递 | **搁置到最后**（P15c / P15e 排到 Wave 3 末尾） |
| 工作方式 | 主 agent 持续推进不停；遇到问题按分析师要求自行定夺；必须人来解决的问题攒到最后一并提出 |

**owner 补充的设计原则（2026-09-09 下午）：所有研究产出都要能版本化更新，不只是 Initial Screen。**
有新数据、新信息进来就要能出新版本：财报后 estimate 变 actual；业绩之间观察到 driver 变化（比如新签大单）
带来 estimate 修订；档案、debate、估值、thesis 同理。落实为四条硬规则，适用于每一个产出类 authority：

1. 没有终态。`gate_passed`、`published`、`accepted` 都是「某一版的状态」，不是对象的状态。
2. 每一版带 `change_reason`（`filing_actual` / `driver_event` / `assumption_review` / `evidence_thicker` /
   `human_revision` 之一）和触发它的证据 refs；不带新证据的改写被权威拒绝（`duplicate`）。
3. 被取代的值不删除、不覆盖：estimate 被 actual 取代时，estimate 那一格保留并标 `superseded_by`，
   这样对账（forecast_reconciliation）和 guidance_style 校准才有原料。
4. 认知迭代要能从版本链上读出来：任何产出都能按版本回放「当时知道什么、为什么这样判断」。

**owner 的澄清：版本化是机制，不是触发器。** 不是任何新闻出来模型都要更新一次；要不要更新、怎么更新，
由「大脑」判断决定。所以分两层：
- **机制层**（各 authority）：只提供 `revise` / `actualize` / `reopen` 这类入口，入口要求带 `change_reason`
  与证据 refs；authority 本身永远不主动出新版。
- **判断层**（Wave 3 的 P14a 事件流）：每个 `ResearchEvent`（新 filing、8-K、电话会、评级变化、价格异动、
  新签大单的 Claim）先由模型映射到 driver 与 thesis，给出五词决定（`DECISION_VOCABULARY` 首次被代码消费）；
  只有决定是「修订」时才调用机制层的入口，并把决定与理由一起写进新版本的 `change_reason`。
  决定「不动」也要留痕（事件账本记 `no_change` 与理由），这样周会能回答「为什么没改」。
- 唯一接近机械的动作是历史期 estimate 被 filing 的 actual 取代（`actualize`），但它也只改历史格；
  未来期 estimate 要不要因此修订，仍由判断层决定。

对各波次的影响：Wave 1C 的 `ForecastModelVersion` 每格区分 `estimate` / `actual`，并预留 `driver_event`
修订入口；Wave 2 的 dossier / DebateMap / 估值快照按同样规则；Wave 3 的 P14a 事件流是统一触发器，
P14d 的「gate 重开」推广为「任何产出的 reopen」。ADR-0008 草稿（Wave 0 顺手写）把这四条写成合同。

**主 agent 定的边界（依据 owner 授权）**：`adhoc_research` 解禁后，专项研究任务的单次模型开销上限沿用 `company_model_cli` 的 `MAX_COST_USD` 量级，日累计不超过 mission `max_daily_cost_usd` 的 25%；同一 inquiry 不重复派发（内容哈希去重）；产出只能是 Claim、observation、deliverable 三类既有写入范围。

## 2. 今日调查结论

**测试基线**：`python3 -m unittest discover -s tests -t .`，2,034 通过、1 跳过、3 分 03 秒（PROJECT_STATUS 记的 1,995 是 P13ao 之前的数）。仓库 `.venv` 没有 pytest，测试是 `unittest`，不需要装。

**行情**：yfinance 1.2.0 已在系统 Python 3.14；ACN 自 2023-01-01 起 924 个交易日到今天，含股本与市值。openclaw 的 `stock-move-analyzer`、`sentiment-dashboard`、`ah-premium-monitor` 三个 skill 都在用它。

**研报里的 consensus 素材**（扫 live `transcript-spool` 1,015 个 AlphaEngine 对象，483 个可解析为文档）：

| 公司 | 文档数 | 首 6k 字符内有目标价数字 | 券商 |
| --- | --- | --- | --- |
| ACN | 30 | 8 | TD、Wells Fargo、Deutsche、Wolfe |
| EPAM | 38 | 8 | TD、JPM、Morgan Stanley、HSBC、Wolfe |
| CTSH | 20 | 5 | TD、Wells Fargo、Wolfe |
| DXC | 27 | 4 | TD、RBC |
| IBM | 8 | 3 | RBC |

ACN / EPAM 足够做两份互证；IBM / DXC 稀薄，如实标缺口。注意 172 份正文截在 30,000 字符，目标价与评级在首页不受影响，EPS 表可能被截。

**接线现状**（只读调查，详见附录 A）：一条新 lane 目前要改 `writer_server.py` 的 10 个区域、`coverage_mission.py` 词表、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、`pyproject.toml` 的 package-data 列表、`cockpit_model.PURPOSES`、`scripts/raise_day_budget_cap.py` 的 `MODEL_CONFIG_NAMES`、cockpit 两个文件、三个共享测试文件。仓库里唯一现成的子类化接缝是 `LaneChildLauncher`（P13aj）。**没有 lane 注册机制。**

**P13ao 已经做完了蓝图的 P13-M1**（规格 × 序列 join，`company_model_inputs.py`），蓝图那条已过时；模型层从 M2（预测行形状）起步。

## 3. 波次

```
Wave 0（串行，1 个 agent，约半天）   lane registry + 词表扩项 + schema glob + ADR-0007 草稿
Wave 1（并行，4 个 agent，约 1 周）  A 市场层 ‖ B Claim 索引 ‖ C 预测行 ‖ D 质量回路
Wave 2（并行，3–4 个 agent，约 1 周） 档案 / DebateMap / consensus 双路 / 价格事件 / Guidepoint
Wave 3（依赖 owner 四项裁决）        演化层 P14 + 对话层 P15
```

### Wave 0：合同先行（P14-0，串行）

目标：之后每条 lane 只新增自己的文件，共享文件的改动缩到「一行 import」或零。

1. **Lane registry**：新模块 `lane_registry.py`，`LaneSpec(operation, core_only, param_fields, build_coordinator, argv_fragment, launcher_factory, driver_key)`。`writer_server` 从 registry 派生 `CORE_DISCOVERY_OPERATIONS` / `CORE_OPERATIONS` / `OPERATION_FIELDS` 的 lane 部分与 `_op_*` 分发；`bounded_planner_driver.run_once` 按 registry 循环；`macos_launchagent.render` 拼接各 lane 的 argv 片段。现有 statements、model spec、initial screen、research plan 等 lane 迁到 registry 上，行为不变。
2. **Schema 文件 glob**：`pyproject.toml` package-data 改为 `*_schema.sql` 通配；`test_packaging` 改为断言「每个 `*_schema.sql` 都被打包」。
3. **模型用途与配置名注册**：`cockpit_model.PURPOSES` 与 `raise_day_budget_cap.MODEL_CONFIG_NAMES` 改为可由 lane 模块登记。
4. **词表扩项**（蓝图 5.3 G 线，一次加齐）：`AUTOMATION_WRITE_SCOPES` 增加 `market_price`、`consensus_estimate`、`valuation`、`market_event`、`dossier`、`debate_map`、`forecast_revision_proposal`、`thesis_revision_candidate`、`research_task`、`conviction_call`；`CHECKPOINT_KINDS` 增加 `thesis_revision_candidate`、`conviction_call`、`gate_reopen`。只加词，不加消费者；live mission 不授予，owner 之后发一次新版本。
5. **ADR-0007 草稿**（`status: proposed`）：自动化可提交 thesis 修订候选、人裁决；同时覆盖「已核验数字进 Ledger」对 `CandidateStagingStore.stage` 规则的放开。owner 裁决前不实现。
6. 验收：全量 2,034 项测试通过；`test_service` 的 launchagent argv 断言不变；新增 registry 测试断言「注册一个假 lane 后 writer / driver / launchagent 三处同时出现」。

### Wave 1：四线并行

每个 agent 一个 worktree（`~/Projects/dalton-<slug>-worktree`，分支 `<slug>`），基于 Wave 0 合并后的 main。

| Agent | 切片 | 只新增的文件 | 允许触碰的共享文件 |
| --- | --- | --- | --- |
| **A 市场层** | P11a 价格 authority（yfinance connector + `MarketPriceSeriesVersion` 日频 OHLCV / 股本 / 市值）；P11c `ValuationSnapshot`（P/E、EV/EBITDA、FCF yield、历史分位，derived_deterministic，公式冻结）；解冻 `VALUATION_AUTHORITY_ROLES` 让 S6 可起草 | `market_price_*.py`、`market_price_schema.sql`、`valuation_*.py`、`yfinance_core.py`、connector 三件套 json、`deploy/connector-governance/yfinance-*-v1.json` | connector 层专属：`connector_inventory.py`、`connector_inventory/index.json`、`connector_governance.py`、`connector_quota_policy.py`、`pyproject.toml` optional-deps（`[market-data]` extra 钉 yfinance） |
| **B Claim 索引** | P12b：`aspect` 封闭词表（与 dossier 十节一致）、`as_of`、`importance`（一手 filing > 管理层原话 > 卖方 > 新闻）、同 subject × metric × period 去重合并。做成 append-only 的**索引投影 authority**，不改 `claim_versions` | `claim_index_*.py`、`claim_index_schema.sql`、`claim_aspect_vocabulary.py` | `research_context.build_claim_index`、`company_research_view.py` |
| **C 预测行** | P13-M2：`ForecastLineVersion` 扩为 driver → assumption → result 三层，读 P13ao 的 `ModelInputTable`；自动化可写 assumption 行但标 `estimate` 并带 `because` 与 Claim refs；三表最小联动（收入 → 毛利 / 费用 → 营业利润 → 净利润 → FCF）；对账层 `forecast_reconciliation` 读新形状 | `model_forecast_driver*.py`、`forecast_driver_schema.sql`、`company_model_forecast_cli.py` | `model_forecast.py`、`forecast_reconciliation.py`、`company_model_report.py` |
| **D 质量回路** | Q 线：Initial Screen / 问答 / 周报三份 rubric 与各 5–10 例 golden set（从 live 只读副本抽）；修 P10c 引用标记残句「、、（同一季度数据重复）显示」与同季重复 Claim 并列引用；`AnalystJournalEntry` 对象（PM 打分入账） | `research_quality_*.py`、`analyst_journal_schema.sql`、`tests/golden/**` | `initial_screen*.py`、`mission_deliverable.py` |

四个 agent 都可以：新增 `*_schema.sql`（glob 自动打包）、在 registry 登记 lane（一行）、新增自己的测试文件、在 `docs/reports/` 写自己的报告。四个都**不可以**：改 `writer_server.py`、`coverage_mission.py`、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、`PROJECT_STATUS.md`、cockpit 两个文件。cockpit 与 install.sh 的接线需求写进各自报告，由集成时统一做。

Wave 1 验收（蓝图 5.2 的验收原样沿用）：五家 ≥3 年日线且每点绑 connector invocation；ACN 估值分位可回指；20 条 Claim 抽查 aspect 错误 ≤2；五家有收入与 margin 预测线且 assumption 行带 refs；三份 rubric 可对现有 ACN Initial Screen 打分。

### S 线：来源补齐（owner 2026-09-09 点名；依据 [OpenClaw 数据源盘点](openclaw-data-source-survey-v1.0-2026-09-09.md)）

owner 要接的：sales note、员工调研、Twitter、雪球、cn-hk-findata。盘点后的落地分法，与 Wave 1 并行开工（connector 层共享文件的冲突由 `index.json` 哈希再生脚本解决，Agent A 负责提供脚本）：

| Agent | connector | 形态 | 关键事实 |
| --- | --- | --- | --- |
| **S1 人工 / vendor 投喂** | `sales-notes`（market-digest 的 Gmail 邮件原文）、`company-wiki`（管理层纪要 / 专家访谈 / 券商笔记） | `host_tool`，读 OpenClaw 工作区已落盘的文件，不碰 Gmail | 252 份 digest JSON 的 `emails[]` 原文；wiki 135 家含 ACN；层级：卖方具名 / 管理层 / 专家 |
| **S2 Guidepoint** | `guidepoint` lane（身份与两条治理记录已批） | `mcp_managed`，本地 OAuth 代理 `127.0.0.1:8943/mcp` | 上游只有 `search_library`，`get_transcript` 无对应物，收窄；逐字引用 ≤20 词的许可规则要进 contract |
| **S3 大众源** | `xueqiu`（shadow → connected）、`x-xreach`（shadow → connected）、`employee-reviews`（Blind 一路） | `host_tool`，凭证槽 `xueqiu_cookie`、`TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | 匿名 / 大众层级，只作趋势与情绪，不作 Claim 的一手来源；`x_search`、Indeed / Glassdoor 不做 |
| **S4 中国基本面**（Wave 2） | `cn-hk-findata` 的 akshare op 作为新模板或 `cninfo` 扩 op | `public_https` + `host_tool` | 87 个 intent 先挑报表 / 股东 / 回购 / 融资融券；`fallback_used` 时口径标注 |
| 之后 | `sec` 扩 Form 4 / 13D-G / 144 / 13F op；changedetection.io IR 监视 | | 持续跟踪层 |

不做或攒到最后裁决：roic 两条批准（撤回或换 transport）、Firecrawl 作 `web-fetch` 回退 transport（额度已用尽）、Reddit（上游已死，改指或撤回）、Bloomberg CSV（再分发裁决）。

### vision 回顾后的补充（2026-09-09 下午，依据 [全部 vision 讨论的复盘](vision-review-against-plan-v1.0-2026-09-09.md)）

owner 要求回顾全部 vision 讨论，补遗漏、修偏差。22 项承诺逐项核对后，补进计划的有：

| 编号 | 事项 | 处置 |
| --- | --- | --- |
| **D1 / P14e** | 专项研究不新造 dispatcher：`ResearchTask` = 以 planner inquiry 为 question、以已准入 `ProbeTemplate` 为动作集、带 rounds / cost / seconds 三重预算的 `BoundedPlannerLoop`；解禁 = `adhoc_research_enabled` 置 True + 为 web-search / AlphaEngine / SEC 各发布一个 ProbeTemplate | **现在派**（Wave 1.5，独立 agent） |
| **C1** | `CatalystCalendarVersion` 事件日历：earnings / guidance / investor_day / filing_due；yfinance 与 SEC 8-K Item 2.02 互证，只有 confirmed 驱动 preview；日期变化出新版带 `driver_event` | Wave 1A 合并后派（Wave 1.5） |
| **C3** | Constitution `method` 三字段接消费者：DebateMap 候选先过 `question_admission`；dossier 分节由 `causal_chain` 派生；发布前结构检查读 `output_rubric` | 写进 Wave 2 dossier / DebateMap 的规格 |
| **D4 + C4 / Q2** | 周报 rubric 直接对 live `weekly_brief_issue_versions` 打分（搁置的是投递不是评估）；`ResearchCycleReflection` 每周一条规划质量指标，不写 Ledger 不改 policy | Wave 1D 合并后派（Q2） |
| **C2** | 容量配额池：`LaneSpec` 增 `budget_pool` / `pool_share`，mission budget 增四池，超池 = `skipped:pool_exhausted` | Wave 2，随 P14e 之后 |
| **D2** | 认知层产出复用 independence predicate：rubric 评分与 producer 不同 `model_family`；dossier / DebateMap 每版发布前跑同约束校验 | Wave 2 规格 |
| **D3** | P14a 前先裁决 `perception.py` / `agenda_coordinator.py` 去留（迁移为 ResearchEvent 来源或显式退役），不建第二套事件平面 | Wave 3 前置 |
| **D5** | gate reopen 判据 = 重跑出口门结构自评，任一项从「缺」变「有」即提案；`gate_reopen` 仍是人类检查点 | Wave 3 P14d |

明确不做（连续多版冻结或主体已退役）：Skill 自主生成闭环、多 runtime、Interrupt / park / resume、embedding-first 检索、万华 Agenda shadow 指标。

### Daily tracking：Initial Screen 过闸后默认开启（owner 2026-09-09 晚）

owner 的要求：一家公司完成 Initial Screen 后，默认进入 daily tracking——每天股价、新闻、event 等，反馈给大脑，
由大脑决定要不要追加研究、要不要出报告（股价异动的可能原因、重大新闻的 implication），以及要不要更新对公司的
判断与模型。频率有基线，但由大脑调配：

| 来源 | 基线频率 | 大脑可调 |
| --- | --- | --- |
| 股价（yfinance） | 每个交易日收盘后一次，盘中一次 provisional | 固定 |
| AlphaEngine 研报 / 纪要 | 每天 2 次 | 覆盖少的公司降到 2–3 天一次甚至一周一次 |
| Twitter / X（xreach） | 每天 2 次取新闻，web search 验证 | 同上 |
| 股价异动（P11d） | 触发即取新闻（AlphaEngine + X + web search），不等基线 | 阈值进 policy |
| Guidepoint、sales note、wiki、员工评价 | 稀疏（周级） | 大脑调 |
| SEC 8-K / 财报日历（C1） | 每日检查 | 固定 |

**owner 补充（同日稍晚）**：(1) 越过 Initial Screen 后 daily tracking 是**常驻任务**，无论大脑此时决定做什么别的（该公司深度覆盖、下一家公司的 Initial Screen、专项研究），tracking 都不停，大脑只调频率不停任务；(2) sales note、wiki 也是 tracking 的信息源，sales note 与推特对获取卖方研报之外的市场看法尤其有用；(3) 大脑要知道每个 connector 能取到什么内容（`SourceCapabilityMap`：connector → 内容类型、证据层级、市场、完备度、配额、基线频率、是否通用），想要某类内容时知道去哪取；web fetch / web search 是通用的。

**owner 再补充（同日夜）：Dalton 要能自我反思。** 市场看涨你也看涨、市场看跌你也看跌，没有价值；要想的是市场定价
错在哪、有什么 pathway 让市场向我们的判断靠拢。如果因为一件事决定调整对公司的判断，或者股价走势持续与预想不一致，
要反思背后原因：是不是遗漏了关键 debate，是不是要追加 tracking 或研究。落实为：
- **variant view 是一等字段**：thesis / dossier / DebateMap / ConvictionCall 都要写「我们的看法 vs 市场的看法
（consensus、卖方评级、sales note 与推特里的主流叙事）」、差在哪、市场靠拢的 pathway 与可观察信号；
与市场同向且无 pathway 的 thesis 在质量 rubric 里得低分（Q 线加一条 criterion `variant_view`）。
- **`ThesisReflection`**（append-only，判断层产出）：两个触发——(a) 判断层给出任一 `revise_*` 决定；
(b) 股价持续背离（`price_divergence` 事件：N 个交易日内相对行业篮子的累计走势与 thesis 方向相反超过阈值）。
内容：`what_we_expected` / `what_happened` / `why`（引用 refs）/ `missed_debates[]` / `followup_tracking[]`
（新的 cadence 或来源）/ `followup_research[]`（P14e 任务候选）；候选进 planner backlog 与 DebateMap 草稿。
- 反思本身不改 thesis；它是 `thesis_revision_candidate` 的附件，人裁决时一起看。

落实为四个对象与一条 lane（Wave 3 的 P14a 提前到现在，编号沿用）：
- **`active_coverage` 阶段自动进入**：某公司任一版 Initial Screen `gate_passed` 即写 `active_coverage` 阶段记录（`STAGE_SPINE` 第一次为该阶段非空），tracking lane 只看这个阶段的公司。
- **`ResearchEvent`**（append-only）：`{event_ref, company_ref, kind ∈ {price_move, news, filing, transcript, rating_change, calendar, reconciliation, claim}, occurred_at, source_refs[], payload_hash, content_hash}`。价格异动（P11d `MarketEvent` 并入此对象）、新文档、新 filing、日历到期、对账结果都变成事件。C1 的 `CatalystCalendarVersion` 以 `kind: calendar` 发事件。
- **`TrackingCadenceVersion`**（大脑的调配结果，append-only）：company × source → 频率与理由；基线来自 policy，大脑按覆盖厚度与事件密度提出调整，每版带 `because` 与证据 refs。
- **事件判断 lane**（判断层）：每个未判定事件一次有界模型调用 → 五词决定之一 + 理由 + 映射到 driver / thesis；决定为 `no_change` 也写事件账本；`note` 出一段带 refs 的短报告（异动归因、新闻 implication）；`research` 调 P14e 入口派专项研究；`revise` 调机制层入口（forecast `revise_assumptions`、dossier / thesis 修订候选）——永远是候选，人裁决。独立 verifier 复用 thesis-impact 的 independence predicate。

### 模型选择与自动登记（owner 2026-09-10 提出）

owner：在 cockpit 上可以为各个调用环节选择用哪个模型；模型列表随 openclaw 对外展示的 provider 自动更新、自动登记为 Dalton 可用。

定案（建在模型路由与目录同步之上）：
1. **按环节选模型**：cockpit「模型」页列出每个 purpose 的当前档位链与最近实际服务的模型；owner 可选「跟随档位」或指定序列
   （主选 + 回退）；选择经治理 op 以 owner 身份发布成新的 routing policy 版本（append-only，可回滚）；verifier 环节的独立性
   约束保留——与 producer 同家族的选择被拒绝并说明。
2. **自动同步 lane**（maintenance 池，每小时）：读 openclaw `models.providers` 与 broker 插件 `allowedModels` / `config.profiles`；
   新模型自动登记为可用 profile（无 rate card 的标「未定价、只可作回退」）；消失的退役不删除；openclaw 有但 broker 插件未放行的
   在 cockpit 显示「可用但未放行」，owner 一键放行 = 带备份写 openclaw.json 的 broker 子树 + 提示重载 gateway。
3. 不自动改 openclaw 的 provider 配置本身。
4. **模型被 openclaw 移除时的退化路径（owner 09-10 追加）**：目录 lane 退役它；任何环节链里含它的那一环自动跳过（首选被移除即落到回退；
   显式链全部退役则落回该环节的档位链；verifier 独立性规则在回退后仍须成立，否则拒绝并说明）；通知先只落在 cockpit（owner 09-10：Feishu / Discord 投递链尚未建）：
   append-only `model_fallback_notices`（按 (模型, 环节) 去重），cockpit 模型页与待办列表显示到 owner 确认或重选为止；留一个
   `notice_delivery` 接缝给以后的投递线，tick 摘要标 `notification_channel: cockpit`。绝不静默。

### 既有资料的入职处理（owner 2026-09-10 提问，主 agent 定案）

owner：有些公司我们已有资料（以前的 Initial Screen、memo、维护中的 Excel 模型）。Dalton 入职时它们是重要参考、能省时间，
但 Dalton 仍要从中形成自己的认知、融入迭代流程，仍要独立写 Initial Screen；资料可能过时。

定案：**既有资料是受治理的来源，不是认知本身。**
1. `prior-research` connector（S1 投喂形态）：从声明目录 + 清单读入旧 screen / memo / 笔记 / Excel；证据层级 `internal_prior`；
   Claim 一律带 `as_of`（文档日期），超阈值标 `may_be_stale` 并降 importance；旧模型数字全是 `prior_estimate`，永不当 actual。
2. 旧 Initial Screen 导入为版本链 v0（`change_reason: imported_prior`），Dalton 自己写的是 v1 并 `prior_version_ref` 指向它；
   起草上下文含「上一版（内部，YYYY-MM）」参考块，提示词要求逐条判断旧关注点与 debate 哪些还成立、不得照抄结论；
   出口门自评加「相对上一版的变化」（新 filing、股价、debate 转向）。
3. DebateMap / ThesisReflection 增加带日期的 `prior_view`（我们当时怎么看），与市场立场、当前立场并列。
4. 旧 Excel → `PriorModelVersion`（假设 `kind: prior_human`，公式存文本），为 driver 模型的假设区间与敏感性提供「团队过去怎么假设」，
   并与 actual 对账做自我校准；不进报表。
5. 时效是一等属性：档案、DebateMap、判断层提示词看得到每条 prior Claim 的年龄，「自 prior 日期以来发生了什么」是必答项。

### Wave 2（Wave 1 合并后开）

P11b consensus 双路（研报抽取走 `document_numeric_claim` 逐字核对，新 grade `broker-research-report`；yfinance 走 A 建好的 connector）、P11d `MarketEvent`、P12a `CompanyDossierVersion`、P12c `DebateMap`、P12f guidance 档案、S 线 Guidepoint lane。cockpit 集成从 Wave 2 起给一个专门的 agent。

### Wave 3（需 owner 四项裁决）

P14 演化层（事件流、thesis revision candidate、预测修订提案、gate 重开、专项研究派发、业绩季工作流，ACN 10/1 业绩实战）与 P15 对话层。

## 4. 每个 subagent 的固定规则

1. 只在自己的 worktree 与分支上工作；基于指定的 main commit。
2. 文件所有权按第 3 节表格；越界改动一律不合并。
3. 交付前必须跑全量 `python3 -m unittest discover -s tests -t .` 并把「Ran N tests / OK」原文写进报告；失败不得静默跳过。
4. 每条新 authority：append-only、`content_hash`、`dalton_authorized()` 三触发器、版本链、读回校验，照 `statement_snapshot.py` 的样子。
5. 每个数字回指 connector invocation 或 filing；估值与派生量公式冻结、可重放。
6. 不改 live 状态目录、不部署、不发 mission 版本；测试里用 `p9a_fixtures.mission_params` 就地放宽 `may_write`。
7. 提交信息沿用 `P1xx: <小写一句话>` 与正文散文，末尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。
8. 交付物 = 分支 + `docs/reports/<slug>-v1.0-<date>.md`（做了什么、没做什么、集成时要接的线、验收结果）。
9. **加一条 lane 或一个 schema 的四处登记**（从 09-10 起测试强制）：`LANE_MODULES` 一行、`cockpit_plane.REGISTRY_LANE_LABELS` 一条中文名、
   `bootstrap.py` schema 表一行、`scripts/rehearse_deploy.py` 一条 `MigrationSpec`；新治理记录必须在 `install.sh` 里播种或列入
   `DELIBERATELY_UNSEEDED`。这四处对 lane agent 开放，不再算越界。
10. **重派前先看 worktree**：agent 静默不等于死亡；查改动时间与 dirty 状态，避免两个 agent 写同一棵树。
11. **主线只在全量绿时 push**；合并后若发现冲突标记或加载失败，先修再推。
13. **演练必须 fail-closed，且不得以能解析到 live 根目录的配置起 driver**（09-10 第二次复演：前置步骤失败后脚本继续，
    用未改写的 `service.json` 对 live Core 跑了一个 tick；写入被 writer 拒绝，唯一实际写入是 C2 tick 账本的一个文件，已隔离到
    /tmp。修法：前置失败即中止；driver 配置里任何路径不在临时根下即拒绝；复演时 HOME 指向临时根）。
12. **自动化冲突解决只允许用于「两边各追加一行」的字典 / 列表 / 元组条目**，且解决后必须先 `python -c "import <module>"`
    再提交（09-10 一次「两边都保留」把嵌套字面量的闭合括号吃掉，主线无法解析，被 P13-M3 agent 发现）。
14. **每次 live 事故必须产出一条测试或一条 policy 检查**（Chem 复盘 §7.5 的做法，我们已在做，写成规则）。

## 5. 主 agent 的集成流程

每个 agent 交付后：读报告 → `/code-review` 该分支 → 合并到 main → 跑全量测试 → 做 cockpit / install.sh 接线（若需要）→ 更新 PROJECT_STATUS 顶部两块与本文档第 6 节 → 通知 owner 需要的裁决或 mission 版本。部署到 live 由 owner 或主 agent 在 owner 同意后做。

## 6. 进度账（随时更新）

**2026-09-10 本轮恢复**：owner 要求继续开发，允许 GPT-5.6 Sol 并行；此前“限额恢复前只记录、不动手”暂停结束。主 agent 集成，三个新的隔离 worktree 修 F1–F3、F6–F8、F16–F17；不复用旧 agent 工作区，不使用不实的 Claude commit 署名。全量绿再 push 的门槛保留，D1–D9 仍是待裁决。


| 日期 | 事项 | 状态 |
| --- | --- | --- |
| 09-09 | 调查：yfinance、研报 consensus 素材、接线热点、测试基线 | 完成 |
| 09-09 | 本计划 v1.0 | 完成 |
| 09-09 | Wave 0 派出（worktree `dalton-wave0-lane-registry-worktree`，分支 `wave0-lane-registry`） | 进行中 |
| 09-09 | owner 第二批裁决记入第 1 节 | 完成 |
| 09-09 | Wave 1 四线提前派出（不等 Wave 0；lane 登记留到集成） | 进行中 |
| 09-09 | S1 / S2 / S3 三条来源线派出 | 进行中 |
| 09-09 | connector 打包哈希再生（`build_connector_inventory.py --check`）摘到 main `99f6a9b` | 完成 |
| 09-09 | **Wave 0 合并** `6e86fe8`：lane registry（加 lane = `LANE_MODULES` 一行）、`*_schema.sql` glob、`register_purpose` / `register_model_config_name`、G 线 13 个词、ADR-0007 / 0008 accepted；review 后修了三个静默失败模式；2,080 项通过 | 完成 |
| 09-09 | Wave 1A / 1B / 1C / 1D 交付并进入 review；A 与 D 各有一个 blocker 在修 | 进行中 |
| 09-09 | vision 回顾完成，8 项补进计划 | 完成 |
| 09-09 | **Wave 1 全部合并**：A `94f2475`、C `f8737c7`、D `56b0e63`、B `b239ee6`；main 2,627 项通过，已 push | 完成 |
| 09-09 | 派出：P14e、P14a（daily tracking）、C1、Q2、S4、INT1、模型路由；S1 / S2 / S3 / P14e 在按 review 修 | 完成 |
| 09-09 晚 | **合并**：S2 Guidepoint、S1 投喂 + host-tool runner、P14e 专项研究、模型路由（目录退役不删除 + 三层 fallback 链）、S3 大众源、C1 事件日历、S4 cn-hk-findata、**P14a daily tracking**（ResearchEvent、cadence、能力地图、事件判断、ThesisReflection）。main `f6eec59`，3,437 项通过，已 push。每条线都经独立 review，共修掉 12 个 blocker | 完成 |
| 09-09 晚 | 一次事故：S4 合并时 `connector_governance.py` 带冲突标记被提交并推上去（146 个模块加载失败），10 分钟内修复；此后合并只在全量绿时才 push | 完成 |
| 09-09 晚 | Wave 2 派出：档案 P12a/P12f、DebateMap P12c、consensus P11b、预算池 C2；INT1 与 Q2 在修 review 意见 | 进行中 |
| 09-09 夜 | 合并 INT1（cockpit 接线、journal op、install.sh 种子、claim-index lane 登记）与 Q2（周报 rubric、ResearchCycleReflection）。main `d509897`，3,620 项通过 | 完成 |
| 09-09 夜 | 派出 Wave 3 首批：P14f 业绩季工作流、P14b + P14d 修订候选裁决与版本化重出、INT2 第二批接线；抽取吞吐诊断在跑 | 进行中 |
| 09-09 深夜 | 合并 C2 预算池 + tick 账本、P12c DebateMap、P12a/P12f 档案 + guidance 档案、P14e 测试修复。main `64d3f94`，3,932 项通过。用量上限中断 5 个 agent 一次，全部从上下文恢复 | 完成 |
| 09-09 深夜 | 派出：planner 日账本（C2 发现 BoundedPlannerLoop 模型调用绕过日账本）、P12d Deep Insight Gate 12 问、P12e 行业框架、P15a ask v2、P15d ConvictionCall | 进行中 |
| 09-10 凌晨 | 合并 INT2（事件 / 判断 / 反思 / 催化剂 / 频率 / 专项研究面板、来源能力地图与模型路由视图、预算池面板、第二批 install 种子、owner 部署后步骤清单）+ 两处跟进修复。main `1fc7c5f`，3,983 项通过 | 完成 |
| 09-10 凌晨 | 派出：S5 SEC 内幕交易 / 13F op + IR 页监视、D3 perception 显式退役（ADR-0009）、部署演练（临时副本上跑迁移 / 种子 / 目录同步 / 一个 tick，产出 owner runbook） | 进行中 |
| 09-10 凌晨 | 合并抽取吞吐（诊断：lane 在饿而非堵；多主体归属、行业 Claim 路径、券商持久化、按证据价值排队）、ADR-0009 perception 显式退役、部署演练（52 个迁移在 live 副本全过，tick 零逃逸；runbook 12 步）；DXC CIK 补零已修。全量测试在跑 | 进行中 |
| 09-10 凌晨 | 派出：P14e 四条发现修复、INT3（install.sh 种子补齐 15 条记录 + 4 个开关文件、找回 `sec-filings-index-v1.json`、bootstrap 打开全部 schema） | 进行中 |
| 09-10 凌晨 | 合并 P14e 四条发现修复（probe 轮次可恢复不再永久失败；grant 解析器注入；两个绑不上的模板退役；行业 inquiry 拒绝理由）。P12d Deep Insight Gate、P15d ConvictionCall 交付在审；P12d 发现档案 `variant_view` 丢 `gaps` 的 blocker，档案作者在修 | 进行中 |
| 09-10 凌晨 | 更正：三个「静默死亡」的 agent 其实还活着，重派造成同一 worktree 双写；已裁决归属（consensus 归原 agent，P14f 与 planner 日账本归新 agent），多余的一方退出并交接笔记。教训：重派前看 worktree 改动时间 | 完成 |
| 09-10 凌晨 | 合并演练脚本修正与修订回路 P14b + P14d（含 socket 测试期限在 import 时算死的根因修复）。INT3 与演练修法三文件冲突，交回 INT3 调和。P12d review：三 blocker（重画时旧版 dossier 引用无法解析并反复付费；mission 版本一滚动裁决即失效），已发回修；派出 stage-ladder（阶段记录跨 mission 版本随行）。C1 日历事件与 P14a 合同不一致、lane 调用不存在的 writer 方法——交 C1 作者修桥接 | 进行中 |
| 09-10 凌晨 | 合并档案 `variant_view` 修复（附「normaliser 输出必须自校验」通用测试）；修订回路两个 schema 补进演练迁移清单。交付在审：S5（SEC 所有权四 op + IR 监视）、ask v2；P15d review 一 blocker（inf/nan 百分比过风险收益标准）已发回并要求补 ADR-0008 版本化 | 进行中 |
| 09-10 早 | 合并 C1 事件桥接（日历事件真正入 ResearchEvent 账本，payload 合同两侧共享测试）与 INT3（35 条记录 = 32 播种 ∪ 3 明确不播；`sec-filings-index-v1.json` 从合同推导找回；bootstrap 一次开 55 个 schema；演练 27 条 lane 零逃逸）。main 4,213+ 项通过，已 push | 完成 |
| 09-10 早 | 合并 P15d ConvictionCall（自动化只提案、人裁决；与市场同向不提案；inf/nan 拒绝；提案版本链与 `supersedes_ref`）。在修：P12d、S5、ask v2；在做：stage-ladder、P12e、consensus、P14f、planner 日账本 | 进行中 |
| 09-10 早 | 第二次用量上限打断 8 个 agent，全部从上下文恢复。规则：每个新 `*_schema.sql` 须同时登记 `bootstrap.py` 与演练迁移清单（测试强制）。P12d 修完合入（在跑全量）；stage-ladder 完成（阶段状态跨 mission 版本折叠；CTSH 折叠为 v9 `gate_failed`）待合；派出既有资料入职（`prior-research`）与重开账本续篇（reopen 后可再次 `gate_passed`） | 进行中 |
| 09-10 早 | 合并 P12d（4,486 项通过，已 push）；合入 stage-ladder、S5（SEC 所有权 op：13F 读真正的信息表、联名 Form 4 不丢人）、ask v2（补搜只取本次 discovery 的文档；policy 投影复用 authority；adhoc 路由的旧禁令按 owner 解禁去掉）。P14f 在审 | 进行中 |
| 09-10 上午 | S5 与 ask v2 合入，main `77ffe45`，4,704 项通过，已 push。consensus review：四 blocker（10-K 后年度期映射死区；新旧目标价取错；lane 喂空券商元数据；lane 序号撞 S5），已发回并定案；P14f 三 blocker 在修；planner 日账本在审 | 进行中 |
| 09-10 上午 | P14f 业绩季合入（4,810 项通过，已 push）。合并时自动解决吃掉一个闭合括号，主线一度无法解析，未 push，10 分钟修复，写成规则 12。reopen-ledger（重开成为阶段账本记录；修了 authority 授权标志按实例而非按连接的地雷）与 P12e 在审；planner 日账本、consensus 在修；P13-M3、prior-research 在做 | 进行中 |
| 09-10 中午 | reopen-ledger 合入（4,835 项通过，已 push）。合入 planner 日账本（planner 调用进日账本与四池，四处可观测性修复）与 consensus（财年末从「从不交 10-Q 的季度」推导；页首抽取 15 个目标价；两家独立券商规则）。在审：P12e、P13-M3、prior-research；后续：authority 授权标志统一 | 进行中 |
| 09-10 中午 | planner 日账本合入（4,876 项通过，已 push）；consensus 合入中。P13-M3 review 一 blocker（bridge 单券商可冒充共识）已发回；P12e、prior-research 在修 | 进行中 |
| 09-10 下午 | consensus 合入（5,045 项通过，已 push）；合入 authority 授权标志统一（19 个 authority 共享按连接的标志；受保护表自动识别）。在修：P12e、prior-research、P13-M3 | 进行中 |
| 09-10 下午 | authority 标志统一合入（5,055 项通过，已 push）。派出模型选择与自动登记（cockpit 按环节选模型 → 新 policy 版本；每小时目录 lane；一键放行写 broker 子树）。在修：P12e、prior-research、P13-M3 | 进行中 |
| 09-10 下午 | P13-M3 敏感性合入（5,167 项通过，已 push；按历史峰谷摆幅排 driver，bridge 单券商不再能冒充共识）；合入 P12e 行业框架（deliverable 数字可引用有 accession / 模型版本支撑的计算格）。在修：prior-research；开发中：model-selection（含模型被移除时的自动回退与 cockpit 通知） | 进行中 |
| 09-10 下午 | P12e 合入（5,313 项通过，已 push）；prior-research 与 P12e 在 `mission_deliverable.py` 冲突，作者调和后合入（既有资料作 `internal_prior` 来源；旧 screen 为 v0；`PriorModelVersion`）+ 集成：`DISCOVERY_SOURCES` 行、`[prior-models]` extra、撤 S1 针脚。复演 2 因脚本 fail-open 碰到 live 目录，唯一实际写入（tick 账本文件）已隔离，规则 13 已记；复演 agent 加固后重跑。model-selection 开发中 | 进行中 |
| 09-11 凌晨 | owner：限额紧、agent 返回只记录不动手不派。七条交付全部到齐（经济不变量已合入本地 main 未 push；其余六条未合），审读结果记在 6c：待派 F1–F19、待裁决 D1–D9；复演 2 fail-closed 通过（对 `8717de0`） | 待 owner 恢复后按 6c 派 |
| 09-10 晚 | prior-research 合入（5,381 项，已 push）；Chem 复盘对照分析 + 规则 14；model-selection 修 B1/B2/S1–S4 后合入（主线全量在跑）；W4 六条切片派出（见 6b）；owner 定：部署与授权随时可做，复演 2 通过即部署 | 进行中 |

---

### 6b. W4：Chem 复盘衍生切片（09-10 派出，见 `chem-retrospective-implications-v1.0-2026-09-10.md` §5）

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| w4-framework-by-classification | 按 `industry_classification` 的 driver 模板（规格 / 档案 / DebateMap 共用）+ `market_proxy` 证据种类与 `proxy_gap` 理由 | 交付（`c768473`，5,442 项）；未合；待审项 F9–F10，裁决 D3 |
| w4-economic-invariants | M2 / M3 经济不变量层（符号一致、历史带、率域、分部加总、单批 vs 累计）；失败 = unavailable + 理由 | 交付（5,451 项）；已合入本地 main 未 push；待修 F1–F3 |
| w4-zero-base-review（交付 `a208d04`，5,486 项通过；未合；待审项 F16–F17，裁决 D8）| `ZeroBaseReview`（月度 / 财报后，从零重问四件事）+ `no_change` / `revise` 的事后验证指标进 Q2 reflection | 派出 |
| w4-insider-buyback-tracking（交付 `e529cf5`，5,475 项通过；未合；待审项 F5–F8）| owner 09-10：tracking 要含 filings，尤其管理层减持与回购。Form 4 派生上下文（占持股比、90 日聚合、10b5-1、是否已被预期）进判断层提示；新增 `buyback_disclosure` 事件（10-Q/10-K Item 2、8-K 授权）+ 派生上下文（均价 vs 现价、节奏、趋势、占市值 / FCF、是否只对冲稀释）；ownership 与 filings index 进常驻 daily tracking；capability map 写明美股回购只在 10-Q/10-K/8-K/电话会 | 派出 |
| w4-hkex-filings（交付，5,534 项通过；未合；待审项 F11–F13，裁决 D4–D5）| 港股 `hkex-filings` 连接器：翌日回购申报、月报表、权益披露（DI）、公告索引；发 `buyback_disclosure` / `insider_transaction` / `ownership_change`；`company:hk-secucode:*` 仅在连接器内引入，universe 扩展留给 owner | 派出 |
| w4-failure-classes（交付 `51c4b81`，5,442 项通过；未合；待审项 F14–F15，裁决 D6–D7）| lane 公共失败分类 dependency_unavailable / content_refused / transient；dependency 类进 cockpit 运维待办并在依赖恢复后自动重试 + cockpit 概览「四格」 | 派出 |

### 6c. 待派修复清单（owner 09-10：agent 返回后只记录、不动手、不派；限额恢复后按此派）

| # | 来源 | 问题 | 修法（给 subagent 的完整指令） | 状态 |
| --- | --- | --- | --- | --- |
| F1 | w4-economic-invariants（已合入 main 本地，未 push，主线全量未跑） | 带宽不变量要求超出历史带的假设带 `outside_band.reason`，但判断层 `event_judgement.py` 的 `forecast_change` 词表被钉死为 `driver_ref / period_end / value / because` 四键（第 384–387 行），`revise_assumptions` 调用（第 1904 行）不传 `outside_band`。结果：大脑任何超出已申报区间的 `revise_forecast`（variant view 恰恰常在带外）在发布时被拒为 `unavailable`，且大脑无从得知要写什么 | 在 `event_judgement.py`：(a) 提示第 273 行后加一句：值若超出该公司已申报区间，须在 `forecast_change` 内加 `outside_band_reason: <一句话说明已申报区间为何不再约束>`，否则发布时拒绝；(b) 校验允许四键或四键 + `outside_band_reason`，reason 用 `_text` 截到 `MAX_BECAUSE_CHARS`，少于 3 个词拒绝；(c) `revise_assumptions` 的 change 字典在存在时带 `"outside_band": {"reason": ...}`。测试：`tests/test_event_judgement.py` `ForecastEffectTests` 加「reason 落到已发布 assumption 的 `outside_band.reason`」（用 `a_driver_and_period()`、`judge(...)` 返回体直接读 `forecast_change`，不是 `judged["judgement"]`）；`OutputContractTests` 加「一个词的 reason 被拒」。注意 harness 夹具历史点数少于 `MIN_BAND_POINTS`，带宽不变量在该夹具上 `not_applicable`，不要在这里断言 `unavailable`，带宽本身由 `test_economic_invariants.py` 覆盖。同样检查 `earnings_calibration.py` 与 `thesis_revision` 候选路径是否也需要携带该字段 | 已记录，未派 |
| F2 | w4-economic-invariants 报告开放问题 1 | `segment_sum` 因 `company_model_inputs` 过滤掉分部行而恒为 `not_applicable` | 在 `company_model_forecast_cli.py` 发布处把 `sec_financials_normalise` 的分部行经 `statement_rows=` 传入 `publish`；加一条端到端测试：分部之和 ≠ 合并时拒绝 | 已记录，未派 |
| F3 | w4-economic-invariants 报告开放问题 3 | 不变量拒绝理由已在 `overview()` / `company_model()` wire 上，`cockpit_control.html` 未渲染 | 在公司卡与模型页把 refusal 与 findings 渲染到「本该出现数字的位置」，不做单独的 checks 面板 | 已记录，未派 |
| F5 | w4-insider-buyback | 分支改了 `event_judgement.py`（渲染 insider / buyback 上下文），主线已合入的 economic-invariants 也改了同一文件（revise_forecast 的 `EconomicInvariantRefused` 捕获）；合并时会冲突 | 合并由主 agent 做：两处改动不相邻，手工调和后先 `python -c "import dalton_core.event_judgement"` 再提交（规则 12）；F1 的修法要在合并后的文件上做 | 已记录 |
| F6 | w4-insider-buyback | `deploy/phase9/p14a-tracking-policy-v1.json` 内容变了（加 `sec-ownership` 86400s 固定节奏、改 `sec` 的 rationale）。该文件若由 install.sh 播种且被 policy 签名 / 哈希钉住，live 上会出现「已签策略 ≠ 包内策略」 | subagent：查 `install.sh` 与 `tracking_cadence` 如何播种该文件；若哈希被 pin，改为新版本文件 `p14a-tracking-policy-v2.json` 并保留 v1，`DELIBERATELY_UNSEEDED` / 播种逻辑指向 v2，加测试「v1 内容不变」；owner 最终清单加一条「重签 tracking policy v2」 | 已记录，未派 |
| F7 | w4-insider-buyback 开放问题 5 | 一份 10-Q 产出三条 `buyback_disclosure`（逐月），恰好填满 `event_judgement_cli.MAX_EVENTS_PER_COMPANY = 3`，同日其他事件（价格异动、Form 4）会被挤出本轮 | subagent：不合并月份（哪个月停买是信号）；改为按 `kind` 配额：同一 kind 同一公司同一 accession 的多条事件在判断提示里合并渲染为一张表、只占一个名额；`per_company` 计数按「事件组」而不是行数；测试：3 条 Item 2 行 + 1 条 Form 4 都进同一轮 | 已记录，未派 |
| F8 | w4-insider-buyback 开放问题 3 | `anticipated: false` 在 Claim 覆盖薄的公司上等于「没写过」 | subagent：当该公司 claim index 中 `management_and_capital_allocation` aspect 的 claim 数低于阈值（建议 5）时降级为 `unknown` 并写明「coverage_thin」；测试两侧 | 已记录，未派 |
| F5b | w4-framework-by-classification | 同样改了 `event_judgement.py`（提示枚举五种证据种类、打印 proxy_gap）与 `model_forecast_driver.py`（`REF_KINDS` 加 `market_proxy`）；这两个文件现在有三条线各改一处（economic-invariants 已合入、insider-buyback、本条） | 合并顺序：先 insider-buyback，再本条；每次手工调和后 import 检查；F1 的修法最后在三方合并后的文件上做 | 已记录 |
| F9 | w4-framework-by-classification 开放问题 1 | 档案按 `UNITS` 顺序起草，`industry_classification` 在 demand 之后才定，首版档案用 generic 模板，下一 tick 才换成真模板 | subagent：把 `industry_classification` 移到 `UNITS` 首位（在 `company_dossier_draft.py`），demand_drivers 起草时读同版已定的分类；测试：首版档案的 demand 段用的是分类模板而非 generic；确认阶段折叠 / 版本链不因 UNITS 顺序变化而重排已存档案 | 已记录，未派 |
| F10 | w4-framework-by-classification 开放问题 3 | `claim_index` 行没有 `evidence_kind` 列，`market_proxy` 只有写法没有存放处，也没有生产者 | 拆成 W5 切片「market-proxy-claims」：(a) `claim_index_schema.sql` 加 `evidence_kind`（默认 `statement`，append-only 迁移 + rehearse MigrationSpec + bootstrap），标注规则把价差 / 挂牌价 / 期货连续等模式打成 `market_proxy`；(b) 第一个生产者用 yfinance 的商品或行业 ETF 序列作为 `market_proxy` 系列，`proxy_gap` 由 spec lane 在引用时写。不阻塞合并 | 待排期 |
| D3 | w4-framework-by-classification 开放问题 2 | 模板只覆盖需求侧，`supply_and_cost` 段没有按分类的成本侧模板 | 主 agent 意见：要做，按分类给成本侧槽位（commodity_cycle：原料价差 / 能源 / 开工率；capital_cycle：折旧曲线 / 维护性 capex；compounder：交付成本 / 人均产出；structural_growth：单位成本曲线；turnaround：固定成本剥离进度），与 F10 同一 W5 切片 | 待 owner 点头 |
| F11 | w4-hkex-filings × w4-insider-buyback | 两条线各自在 `research_event.py` 定义了 `buyback_disclosure`（字段表已协调一致），但 hkex 给 `insider_transaction` / `ownership_change` 加了 `notes_text_hash`，insider-buyback 给 `insider_transaction` 加了两个字段；`cumulative_shares_ytd` 在港股是「自回购授权起累计」不是日历年，港股行全 null；`cluster_key` 只在 hkex 的派生 context 里 | 合并顺序：hkex 先、insider-buyback 后，`research_event.py` 冲突手工调和为并集。随后一个 subagent 做契约统一：`cumulative_shares_ytd` → `cumulative_shares` + `cumulative_basis`（`calendar_year` / `since_mandate` / `fiscal_year`），两个生产者都写；`cluster_key`（ISO 周 + accession 或申报日）进 payload；两侧测试与 `contracts/` 同步；payload 哈希会变一次，live 无 `research_events` 表所以无迁移 | 已记录，未派 |
| F12 | w4-hkex-filings 开放问题 4 | `market_price._TICKER_RE = ^[A-Z][A-Z0-9.\-]{0,15}$` 不接受以数字开头的港股代码，港股价格比较恒 `unavailable`（作者已留一条会在放开当天翻红的测试） | subagent：正则放宽为允许 `^\d{4}\.HK$` 形式（只加这一种，不放开任意数字），yfinance 适配器的夹具加 `0700.HK` 一日；翻转 hkex 的那条测试为正向断言；均价 vs 现价的派生比较随之生效 | 已记录，未派 |
| F13 | w4-hkex-filings 开放问题 6 | 每日回购 tape 是全市场 xls（`SRRPT{YYYYMMDD}.xls`），现在每家公司各读一次；S4 对 A 股回购表指出过同样的浪费 | subagent：lane 层加「全市场表日缓存」：同一 `YYYYMMDD` 的 xls 只取一次（invocation 一次、artifact 哈希一次），五家公司各自过滤；A 股 `buybacks`（S4）复用同一缓存概念；测试：同日两家公司 = 一次 invocation | 已记录，未派 |
| D4 | w4-hkex-filings 开放问题 3 | `company:hk-secucode:<code>.HK` 只作引用方案，mission universe 无港股名字，lane 报 `idle` | 并入既有 owner 裁决「mandate 扩展」：是否加入港股覆盖名单、加哪几家 | 待 owner |
| D5 | w4-hkex-filings 开放问题 5 | 港股回购 / 权益披露行由交易所或证监会自己发布，tier 记 `primary_filing`，但 `HKEX_GRADE` 把它们挡在所有数字路径之外 | 主 agent 建议：翌日回购申报的股数 / 价格 / 金额是交易所发布的原始数字，允许以 filing 级进入回购上下文与 claim（不进财务报表行）；权益披露同理；月报表 PDF 数字继续不读 | 待 owner 点头 |
| F5c | w4-failure-classes | 改了 `cockpit_plane.py`、`cockpit_control.html`、`agenda_control.py`、`bootstrap.py`、`rehearse_deploy.py`，与已合入的 model-selection、economic-invariants 同文件 | 合并时手工调和；四格概览与 model-selection 的通知区、economic-invariants 的公司卡拒绝理由在同一页面，合并后由一个 subagent 跑一遍 cockpit 页面测试并核对三者共存 | 已记录 |
| F14 | w4-failure-classes 未迁移项 | 16 条 signature-hold / cool-off lane 的失败已能分类但没接到失败账本，依赖类停摆只在 lane 行详情里可见 | subagent：逐条检查这 16 条 lane 的「hold 理由」是失败还是「输入未变」（后者不进账本）；是失败的走 `LaneFailureBudget` + 账本；每条 lane 一个测试「依赖类失败不消耗预算、依赖恢复即续」；cockpit 四格的停摆数随之覆盖全部 lane | 已记录，未派 |
| F15 | w4-failure-classes 未映射项 | `gated:<gate_reason>`（document_extraction 的治理拒绝）没有归属，暂落 `transient` | 主 agent 决定：加第四类 `not_permitted`，不重试、不消耗预算、进 cockpit「待授权」而不是「停摆失败」（它本来就是 ungranted / unapproved 的同一件事）；`stop_reason` 契约同步加词并在 `contracts/` 与测试里钉住 | 已记录，未派 |
| D6 | w4-failure-classes 开放问题 2 | 依赖探测间隔一刀切 30 分钟 | 主 agent 建议分层：配额类 30 分钟自动探测；会话类（AlphaEngine 桌面、Guidepoint 登录）探测一次失败后转 owner 待办、不再自动探测直到 cockpit 标记已恢复；owner 只需确认这个分法 | 待 owner 点头 |
| D7 | w4-failure-classes 开放问题 3 | statements / sec_quarters 两条 lane 的失败预算在 SQL 里，迁移会改 P13「未归因 → 我们的配置」默认与 `attempt_voids` 语义 | 主 agent 建议：不迁移，只映射词表（作者现状），记入 ADR 备注 | 待 owner 点头 |
| F16 | w4-zero-base-review 开放问题 3 | 「月度」按日历月判定，1 月 31 日与 2 月 1 日各得一个版本 | subagent：改为「距该公司上一版 ZeroBaseReview ≥ 30 天」或「其后出现新的 earnings_calibration」两者之一触发；财报触发后 30 天计时重置；测试：31 日 / 1 日只出一版 | 已记录，未派 |
| F17 | w4-zero-base-review 开放问题 1 | 复盘无独立 verifier，判断 lane 有 | 主 agent 决定：要。复用判断 lane 的 verifier 谓词（verifier 家族 ≠ 生产者家族），校验四问答案每条都引用了在档的 thesis / debate / claim ref、且「下一个验证点」有日期；不通过 = refused 不发布 | 已记录，未派 |
| F5d | w4-zero-base-review | 改了 `thesis_revision.py`（加同形兄弟表读取）、`cockpit_plane.py`（判断结果面板）、`research_cycle_reflection`（第九个指标，版本 0.1→0.2）、`install.sh`、`bootstrap.py`、`rehearse_deploy.py` | 合并时与 failure-classes、model-selection 在 cockpit / bootstrap / rehearse 同文件；调和后 import 检查 | 已记录 |
| D8 | w4-zero-base-review 开放问题 2、4 | (2) 复盘是否也发布进 `MissionDeliverableAuthority`（需 `DELIVERABLE_KINDS` 加一个词）；(4) 事后验证窗口沿用 price_divergence 的 10 个交易日 / 6%，季度窗口要改 tracking policy | 主 agent 建议：(2) 要，词 `zero_base_review`，这样 cockpit 的交付列表与验收 rubric 能看到它；(4) 加第二个窗口 60 个交易日 / 15% 作为「慢背离」，写进 tracking policy v2（与 F6 同一次重签） | 待 owner 点头 |
| R2 | ops-rehearsal-2（交付 `84d9a14`，未合；5,345 项通过） | 加固后的复演对 main `8717de0`（model-selection 合入前）的 live 副本通过：61/61 schema、23 播种 / 10 按门拦下、目录同步幂等（5 登记 / 6 退役）、34 条 tick 零逃逸、156,420 行内容哈希逐字节一致；唯一 DDL 变化是 deliverable kinds 的 CHECK 加四个词。四项加固：前置失败即中止；`bounded_planner.config` 路径不在临时根即拒绝；HOME 指向临时根；`--source-root` 允许 `--live-root` 是副本 | 合并后用 `--source-root` 对当前 main 重跑一次（model-selection、以及待合的 W4 会改 schema / seed / lane 计数）；owner 清单 v2.0 在 `docs/reports/owner-steps-after-deploy-v2.0-2026-09-10.md` | 已记录，待合并 |
| F18 | ops-rehearsal-2 caveat | `sales-notes`、`company-wiki`、`xueqiu`、`x`、`blind` 五个来源没有 `source_plan` 行，`--set-source-status` 报错；清单里给的是手工编辑 | subagent：让 `--set-source-status` 在缺行时按 connector inventory 创建 `source_plan` 行（append-only、写明 `created_by=set-source-status`），而不是要求手工编辑；测试五个来源 | 已记录，未派 |
| F19 | ops-rehearsal-2 open question | ask-v2 policy 契约仍钉 `enabled: const false`，代码已允许开启 | subagent：契约改为 boolean，默认 false，playbook 里由 owner 显式置 true；测试两态 | 已记录，未派 |
| D9 | ops-rehearsal-2 open question | AlphaEngine 日配额 live 为 130，W2 实测已用 133 / 130；CTSH / DXC / IBM 各只能清一家券商。抽取 30/10/10 的节奏折算 14,400 次 / 日，超 9,000 上限（推导的边界是 15，满额分配 31） | owner：提高 AlphaEngine 配额，或接受三家公司 consensus 只有一家券商；抽取节奏按推导值 15 下调 | 待 owner（已在最终裁决清单） |
| D1 | w4-insider-buyback 开放问题 2 | 8-K 正文不可取（`sec_earnings_release` 记录了原因），回购授权抽取只在 Core 已持有 8-K 文本时触发；要真正生效需要 `form: 8-K` 的 discovery spec = 新 plan 版本 + owner 发布 | 进 owner 最终裁决清单 | 待 owner |
| D2 | w4-insider-buyback 开放问题 1 | 10-Q Item 5「Trading Arrangements」（10b5-1 计划的采用 / 终止，含人、日期、窗口、股数）比 Form 144 更强的「预期减持」信号，可解析 | 作为后续切片 W5 候选，不阻塞 | 待排期 |
| F4 | 我给七个 agent 的恢复消息 | 消息里写的 `pgrep -fc` 在 macOS 不支持 `-c` | 无需修代码；agent 自行改用 `pgrep -f ... \| wc -l`。记录以免误判为环境故障 | 已记录 |

## 附录 A：接线热点清单（Wave 0 要收掉的）

| 文件 | 区域 | 谁会碰 |
| --- | --- | --- |
| `writer_server.py` | `CORE_DISCOVERY_OPERATIONS` :409、`CORE_OPERATIONS` :468、`OPERATION_FIELDS` :537/:684、`__init__` kwarg :1031、实例属性 :1129、`close()` :1609、`_op_*` :2642、argparse :3713、launcher 构造 :3754、`WriterServer(...)` :3939 | 每条 lane |
| `coverage_mission.py` | 词表 :96–181、边界常量 :58、文件尾新方法 | 每条 lane |
| `bounded_planner_driver.py` | :284–296、:490–496 | 每条 tick lane |
| `macos_launchagent.py` | :18–26、:80–86、:209–224 | 每条 tick lane |
| `deploy/macos/install.sh` | 治理种子块、模型配置块 | 连接器与带模型的 lane |
| `pyproject.toml` | package-data :50–88、optional-deps :11–22 | 每条带 schema 的 lane |
| `cockpit_model.py` | `PURPOSES` :49 | 每条调模型的 lane |
| `scripts/raise_day_budget_cap.py` | `MODEL_CONFIG_NAMES` :37 | 每条有自己模型配置的 lane |
| `connector_inventory.py`、`connector_inventory/index.json`、`connector_governance.py`、`connector_quota_policy.py` | | 连接器 lane（Wave 1 只有 A） |
| `cockpit_plane.py`、`cockpit_control.html` | | 有 owner 可见输出的 lane（集成时统一做） |
| `tests/test_service.py`、`test_packaging.py`、`test_connector_inventory.py` | | 同上 |

参考实现：statements lane（P13ak，`107a20d` / `719206b`）、model spec lane（P13am，`c71136a` / `8bc019e`）、`LaneChildLauncher`（P13aj）、`statement_snapshot.py`（authority 样板）、`sec_financials_core.py`（连接器身份样板，「库在进程内调用、原始输出哈希替代字节校验」的先例）。


### 6d. 2026-09-10 恢复后的集成进度（覆盖 §6b / §6c 历史状态）

owner 已要求继续开发、并行 GPT-5.6 Sol、及时 commit/push。六条待合分支已全部合入，三条独立修复 worktree 完成交付与交叉审查，主线代码里程碑 `48cc315`。完整实现与验证证据见 [本轮集成报告](resume-w4-integration-2026-09-10.md)。下表为当前状态，§6c 的“未派”保留作当时审读记录。

| 项目 | 当前状态 |
| --- | --- |
| F1–F3 | 完成；含重复 filing 的一致分部选取与首次模型拒绝展示 |
| F4 | 无代码修改需要 |
| F5 / F5b / F5c | 六分支共享契约、cockpit、lane registry 集成完成 |
| F6–F9 | 完成；v1 保留/v2 新增、月度回购分组且不重复收费/评分、薄覆盖 unknown、首版先分类 |
| F10 | W5 待排：market-proxy claim 存储与生产者 |
| F11–F12 | 完成；单一跨市场回购契约、累计期间、四位 .HK 价格路径 |
| F13 | 可行性审查完成，实现待 acquisition/view 治理身份设计；未上线缓存 |
| F14 | 实盘代码为 13 个未接协调器，已拆三组；见 `resume-failure-ledger-next-2026-09-10.md`，旧估计 16 条仅作历史 |
| F15 | 完成 document extraction not_permitted；配置/mission/policy 更新恢复，cockpit 单列待授权 |
| F16–F17 | 完成；30 天/财报触发、独立家族 verifier、在档正文及超限拒绝 |
| F18–F19 | 完成；inventory 补 source_plan 行、ask 两种启用状态的契约同步 |
| HK 周调度 | 新增后续项：日期标识不是周级调度，需定义增量归组 |
| D1–D9 | 原裁决问题继续保留；本轮没有通过修改 live 隐式裁决 |

最终复演（冻结代码 `48cc315`，旧 live 快照副本）：66 schemas、35 lanes、38 entries、0 escaped；缺授权/开关与 retired verifier pin 仍需运行激活时解决。本轮未部署。下一步优先当前 live 新快照复演与五家公司产物验收，再做 F14 / F13 / W5；主线全量结果与 push 状态以集成报告及 PROJECT_STATUS 顶部为准。
