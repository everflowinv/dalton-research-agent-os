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
