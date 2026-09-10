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

## 5. 主 agent 的集成流程

每个 agent 交付后：读报告 → `/code-review` 该分支 → 合并到 main → 跑全量测试 → 做 cockpit / install.sh 接线（若需要）→ 更新 PROJECT_STATUS 顶部两块与本文档第 6 节 → 通知 owner 需要的裁决或 mission 版本。部署到 live 由 owner 或主 agent 在 owner 同意后做。

## 6. 进度账（随时更新）

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

---

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
