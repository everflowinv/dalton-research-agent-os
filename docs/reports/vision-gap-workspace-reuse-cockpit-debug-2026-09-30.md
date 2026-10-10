# Dalton Research OS：历史愿景缺口、研究环境复用与 Cockpit 系统性排查（2026-09-30）

历史技术评审：保留当时结论与局限；当前运行状态见 [项目状态](../PROJECT_STATUS.md)。原始操作证据私有保存。

审查范围：main `7826ff21`（已部署源 `3124792e`，release `d02d07bc`），三个线上环境（legacy 8793、ws-7d 8795、ws-e399 8794），`docs/reports` 全部 vision / review / progress 报告、9 份 ADR、`docs/PROJECT_STATUS.md` 全文，归档目录 `Projects/dalton-archive-20260915`，以及线上 Core 数据库的只读查询。

本轮**未改任何代码、配置、运行状态**，未触发任何写操作或付费问答。所有条目只列开发/修改内容，不实施。

---

## 一、历史 vision / status 中承诺但至今未开发的功能

### 0. 一句话结论

从 v0.1（08-13）到 09-13 的所有承诺里，架构与认知层几乎全部有 authority 与 lane（市场层、档案、DebateMap、事件流、修订候选、专项研究、周报、质量评分在 live Core 都有非零行）。真正"历史上要做、至今没做"的集中在四类：

1. 09-13 明确列出的四个高级合同（Conviction v2 / 持有期回报与买方预期 / 结构化情景 / 模板 registry）；
2. 研究手册第 3–5 阶段的执行闭环（industry_model → company_model → Investment Memo，live 阶段账本从未过门，memo 0 份）；
3. owner 操控杠杆与投递（暂停/停止/紧急停机、改覆盖名单入口、Feishu/Discord 投递、模型回退通知）；
4. 一批 09-10 W4/W5 记录在案但"待排期"的小切片。

另有若干"代码有、线上零产出"的项，属于激活/验收而非开发。

### 1.1 文档自己承认"仍待开发"的合同级缺口

| # | 功能 | 出处 | 当时期望 | 现状证据 | 判定 | 建议开发内容 |
|---|---|---|---|---|---|---|
| 1 | **Conviction v2** | onboarding v1.0 §5.2 P15d；`owner-research-prompt-contract-audit-2026-09-11`；`vision-owner-coverage-refresh-2026-09-13` 第一行 | 允许"市场同向但回报仍有吸引力"的 call；把证据置信度、情景概率、期望回报分开表达；版本化 policy 由人发布 | `conviction_call.py:61 SCHEMA_VERSION="0.1"`；全仓无 `conviction v2`；v1 仍要求 `where_market_is_wrong` 与"真实分歧"（`conviction_call.py:479`）；live 仅 1 条提案（IBM 09-14）、1 条决定（09-17）；09-16 审计：四家公司无 thesis → 第 1 道闸门永久关闭 | 未开发 | 新 schema `conviction-call-proposal-v0.2`：`evidence_confidence(low/med/high)`、`scenarios[{prob, payoff, trigger}]`、`expected_return`、`market_view_alignment(agree/disagree)`；producer/verifier 提示词与 rubric 更新；policy 版本化（`conviction_policy` authority）+ owner 发布门；cockpit 提案卡展示三分量；测试：同向-高回报提案不再被拒、inf/nan 仍拒、独立家族 verifier |
| 2 | **持有期回报归因 / 买方预期来源 authority**（含 EPS / 收益桥） | v0.8（08-23）"市场与估值数据缺 authority"；`foundation-research-prompt-contract-review-2026-09-11` Memo 行；`owner-foundation-gap-priorities-2026-09-11` P0 第三行 | 冻结的 holding-period return bridge、buy-side expectation source 分类；typed non-operating income bridge 与 per-share（季/年独立期间语义）合同 | grep 无 `holding_period / return_attribution / buy_side` 对象（仅 `conviction_call.py:175` 一句注释）；`company_financial_statement_structure.py` 有 `diluted_eps` 角色名、`company_model_report.py:406` 能显示 EPS，但无 income-bridge / operating-reconciliation authority；09-11 实测 ACN/CTSH/EPAM 报表净利 ≠ 营业利润 − 税（−74m / −7m / −12m），DXC 无稀释股数行 | 未开发（EPS 展示为部分） | `IncomeBridgeVersion`（营业利润→税前→净利→归母，逐项 filing 回指）；`PerShareSeriesVersion`（加权稀释股数按期间语义，禁止累计相减）；`HoldingPeriodReturnBridge`（估值倍数变化 × EPS 变化 × 股息，derived_deterministic）；`ExpectationSourceKind` 封闭词表（sell-side / buy-side / management / internal_prior）挂到 consensus 与 DebateMap；memo / conviction 读取这些对象而非提示词推断 |
| 3 | **结构化情景假设与动态跟踪** | onboarding §5.2（catalyst / scenario 对象）；review 09-11 Dossier 行；09-13 refresh 第三行 | 档案里的情景 / 触发条件成为程序可持续监控的对象，而非带引用的文字 | `grep "class .*Scenario\|ScenarioVersion"` 零命中；dossier scenario 仍是 sections 文本；`tracking_cadence_versions` live 0 行 | 未开发 | `ScenarioVersion{company, name, prob, triggers[{metric_ref, comparator, threshold, observed_by}], invalidators[], linked_thesis/driver}`；事件判断 lane 对 trigger 命中写 `scenario_observation`；cockpit 公司卡"情景与触发"面板；周报读 scenario 状态变化；ADR-0008 版本化规则 |
| 4 | **参考基金 Excel 模板 registry / select 闭环** | owner 09-10（AMZN 样本）；09-13 refresh 第四行（候选 `8e66ee01`，31 项检查通过，"尚待合入"） | 注册多套基金样式模板、按公司 / 行业选择、导出时套用 | main 只有 `fund_xlsx_template_import.py` 与 `fund_xlsx_template.py`（无 registry / select）；候选代码只存在于归档 patch：`dalton-archive-20260915/{continuous__0913-fund-template-registry, agent__fund-xlsx-template-integration, w5-cost-templates}` 与 stash `agent/fund-xlsx-template-integration: strict-template-corrections` | 代码在未合并分支（已归档为 patch） | 从归档 patch 重建 `FundXlsxTemplateRegistry` authority（模板 hash、来源文件、适用范围）、cockpit 导出时选择模板、`fund_xlsx_export` 读取 registry；回归 31 项检查 + 五公司导出对比 |

### 1.2 研究手册后半段：合同 / 代码有，线上从未走到

| # | 功能 | 出处 | 现状证据（live legacy Core，09-30 只读） | 判定 | 建议 |
|---|---|---|---|---|---|
| 5 | **industry_model / company_model 阶段过门** | v1.1 P10e/P10f；Phase 9 退出门槛；09-10 gap refresh"模型阶段缺实证" | 阶段账本：ACN/EPAM/IBM `industry_model entered`（09-17），无 `gate_passed`；`company_model` 五家均无记录；`mission_model_stage_lane.py` 已能确定性关门，但 `model_stage_readiness.py:75/110` 注释说明"预测全 estimated 或无真实历史 = 门不可能过"；`STAGE_SPINE` 对这两阶段仍为空元组（`metric_base.py:118-119`） | 已实现未激活（门永远不满足） | 明确 industry / company model 出口门的可判定证据：update-calendar 绑定、filing 逐行对账证明、peer-relative sensitivity（09-10 gap refresh 列的三项）；把缺项分解为 lane 可产出的 authority，或经 owner 修订 Playbook 门槛；给这两阶段填 `STAGE_SPINE` |
| 6 | **Investment Memo（人审）→ active_coverage** | v1.1 P10g；onboarding P15；09-10 refresh P1"实际缺失"；Phase 9 退出门槛 | `investment_memo_draft.py`、`mission_investment_memo_lane.py` 已在 `LANE_MODULES`；live `mission_deliverable_versions` 无 `investment_memo` 类型；被 #5 阶段门阻塞 | 已实现未激活 | 依赖 #5；随后验收"ACN 一份数字全可回指的 memo 草稿 + 12 问 + anti-thesis + 独立核验 + owner 一次裁决" |
| 7 | **CTSH / DXC 的 Deep Insight Gate** | v1.1 P10d | DI 交付物五家都有（DXC v2 09-29），但阶段账本只有 ACN/EPAM/IBM `deep_insight_gate gate_passed`（09-17）；CTSH/DXC 无 `entered` 记录 | 人审 / 激活缺口 | 排查 DI lane 对已过初筛公司为何未 `entered`（可能与 mission 版本折叠或 reopen 后状态有关）；owner 裁决两份草稿 |
| 8 | **四家公司 thesis 准入** | ADR-0001；09-16 审计 §4"四家没有任何生效 thesis，conviction 第 1 道闸门永久关闭" | `thesis_versions` 4 行 = 行业级 + ACN 各 2 版；CTSH/EPAM/IBM/DXC 无 thesis | 人审缺口（非开发） | cockpit 增加"从 DI 草稿一键生成 thesis 准入候选"的入口（小开发） |
| 9 | **thesis-impact 定时任务停泊未重开** | v0.7 Gate 2、v0.9 S7d；PROJECT_STATUS 09-08"到 thesis 切片再一次性修好并重开" | live `service.json#thesis_impact.enabled=false`；09-23 仍在为该 lane 对齐 verifier 路由；ADR-0007 候选路径已取代其职能（09-14 两条 thesis v2 由 `thesis-revision-candidate` 人裁决产生），但 `thesis_revision_candidates` 现 0 行、`forecast_revision_proposals` 0 行、`thesis_reflections` 0 行 | 方向已被 ADR-0007 覆盖，但未正式退役；修订候选链路线上零持续产出 | 二选一：正式退役 thesis_impact lane（写 ADR，删配置对齐工作），或重开；同时排查为何 09-14 之后事件判断 5,359 次却无新修订候选（09-16 审计：520/522 次 NO_CHANGE） |
| 10 | **已核验数字进 Ledger 的 policy 签署** | ADR-0007 后半；PROJECT_STATUS 09-18 23:10 | `quantitative_claim_promotion.py`、`sign_auto_commit_rules.py` 已合入；09-18 记录"SEC 与 figure 三条定量规则仍需 owner 另行签署"；09-16 审计：6,390 条 claim 中定量仅 22 条且全是收入增速，16,903 行 statement_lines 从未提升 | 代码有，待 owner 签署 + 覆盖广度未验 | owner 跑签署脚本；验收定量 claim 覆盖分部收入 / bookings / 利用率 / 指引；dossier `numbers_without_refs` 失败率下降 |

### 1.3 owner 杠杆与投递（v0.1 §5.1 / §8 承诺，至今没有）

| # | 功能 | 出处 | 现状 | 判定 | 建议 |
|---|---|---|---|---|---|
| 11 | **暂停 / 取消 / 紧急停机 / 优先这个** 的 owner 命令面 | v0.1 §5.1（pause / resume / emergency-stop）、§8 steering；PROJECT_STATUS"蓝图阶段"横切"全局 agenda pause、通用 cancel / approve / emergency-stop 均未完成" | ADR-0006 五视图的"调整方向"只改研究问题 / 目标；ADR-0009 用 `TrackingCadenceVersion` 取代 priority_overrides，但 live 0 行且无 cockpit 入口；停车道靠 `docs/ops/pause-support-backfill.sh` 之类脚本 | 未开发 | cockpit 增加公司级 / 车道级 `pause / resume / stop_line` 治理 op（append-only、带 TTL 与理由）；全局 emergency stop 写 heartbeat 拒绝新 admission；owner 可发布 `TrackingCadenceVersion`（"优先看这家"） |
| 12 | **改覆盖名单 / 扩预算的页面入口** | v1.1"贯穿各切片的 cockpit 变化"；ADR-0006 后果第二条 | 预算已有页面（09-14）；覆盖名单变更仍需命令行发布 mission 版本（`scope_expansion` 只在 steer 草稿里提示） | 部分（预算有，名单无） | `scope_expansion` 人审卡：owner 在页面加 / 减公司 → 草案 → 确认发布 mission 版本，联动 SEC issuer 解析与 discovery selector |
| 13 | **周报 / call / 模型回退通知的外部投递（Feishu / Discord）** | v0.1 §9.2；onboarding P15e；并行计划"模型选择"第 4 条留 `notice_delivery` 接缝；owner 09-09"搁置到最后" | `model_selection.py:183-186` 明写 no-op；全仓无 `feishu`；Discord 只剩 legacy agenda bridge；`weekly_brief_deliveries` 4 行为旧机制 | 未开发（owner 主动搁置，未取消） | 一个 `delivery` connector（Feishu 优先，宿主 OpenClaw 已连）+ receipt 账本；接 weekly brief、conviction 提案、fallback notice、"需要你处理"摘要 |
| 14 | **多用户身份 / 供应商配额公平排队** | multi-analyst-workspace-plan 09-10；README 12.3 明确"属于另外的能力" | `workspace_manager.py` 仅 `owner_login` 单人；三个环境共用一个 owner | 未开发，方向上已降级 | 若要做：Tailscale 身份 → workspace ACL；共享 provider 配额的按环境公平调度 |

### 1.4 09-10 W4/W5 与后续记录在案、仍"待排期"的小切片

| # | 项 | 出处 | 现状 | 建议 |
|---|---|---|---|---|
| 15 | market-proxy claim 存储与第一个生产者（F10）、按行业分类的成本侧模板（D3） | 并行计划 §6c/6d"W5 待排" | `market_proxy_claim_versions` 表存在但 0 行；成本模板仅在归档 patch `w5-cost-templates` | 合入 / 重做 W5：`evidence_kind=market_proxy` 标注规则 + yfinance 商品 / ETF 序列生产者；五类分类的 `supply_and_cost` 槽位模板 |
| 16 | 全市场回购 tape 日缓存（F13）与 HK 周级增量归组 | §6d"实现待 acquisition / view 治理身份设计；未上线" | 无 | lane 层同日 xls 单次取回、多公司过滤；周调度归组 |
| 17 | 10-Q Item 5 Trading Arrangements（10b5-1）解析（D2） | §6c W5 候选 | 无 | 事件 `insider_plan_adoption`，进判断层上下文 |
| 18 | 失败账本剩余 13 条 lane 未接协调器（F14） | `resume-failure-ledger-next-2026-09-10.md` | 未见后续关闭记录 | 逐 lane 接 `LaneFailureBudget`，cockpit 四格覆盖全部 lane |
| 19 | Ask 答案充分性 policy 发布、`answer_after_refresh` 真实闭环、"10 个真实问题 ≥8 有用" | 09-11 P2；09-13 owner-review-remaining | 候选 policy `pending_owner_review`；refresh receipts 09-11 为 0；quality scores 5、journal 2、brief feedback 1（08-27） | 非开发：owner 发布 policy 并做一次固定问题集验收 |
| 20 | 港股 universe 扩展（D4）、HK 交易所原始数字入 claim（D5）、依赖探测分层（D6）、AlphaEngine 配额（D9）、8-K discovery spec 激活（D1） | §6c；`sec-8k-owner-install-packet` | live `discovery-plans/us-it-services-sec-filings-v1.json` 仍只有 `['10-K']`；hkex / cn-hk 治理已批但 universe 无港股 | 全部是 owner 裁决，不是代码 |
| 21 | IR 页面监视 adapter | S 线"changedetection.io IR 监视"；live 治理 `ir-page-watch-*` 已批 | `src/dalton_core` 未找到 `ir_page_watch` / `ir_watch` 模块（仅治理与 inventory 记录）；需核实是否以 host_tool 形式存在 | 若确无：补 adapter + lane，否则撤回治理记录 |
| 22 | Antigravity 网关地区封锁；web 来源可靠性 backlog | `pending-deploy.md`（commit `3124792e`，09-30） | 运维项，需 owner 处理账号 | 非开发 |

### 1.5 Phase 9 / v1.1 退出门槛里尚待"发生"的事（不是开发）

- ACN 10/1 Q4 实际数落库后自动 `forecast_reconciliation` → live 目前 0 行（10-01 验证）。
- EPAM 无 `earnings_preview`（其余四家各 1 份）。
- 每周简报 5 期、feedback 仅 1 条（08-27）；"三期周报的质量评分与反馈进入后续起草"未验收。

### 2. 明确已放弃 / 冻结 / 方向改变，不计入

| 项 | 依据 |
|---|---|
| Skill 自主生成闭环（gap detector / builder / sandbox / 自动晋级） | v0.5 冻结；v1.0 review 表"drop"；09-13 refresh"明确冻结或排除"；README 12.5 |
| 多 runtime / DeepSeek Harness / Pi 生产 worker、Temporal / Postgres 迁移、hostile-code OS/container 隔离 | 同上；`spikes/` 只留 deepseek-harness、pi-agent-core 两个 spike |
| Interrupt / park / resume | v0.5 后置 → v1.0 review"drop，预算池即可" |
| embedding sidecar / embedding-first 检索 | v0.4 降为可删 sidecar；v1.0 review drop；README 12.5 |
| Model IR 超出 v1（任意单元格、VBA、循环引用、通用估值引擎）、Tier 1/2/3 evaluator、formula census | PROJECT_STATUS"下一阶段顺序"末段；v1.0 review E |
| 万华 Agenda Shadow、Perception / Agenda 事件平面、priority_overrides、Discord agree / disagree 投递 | ADR-0009 退役；`legacy_agenda_plane` 默认关 |
| 旧 OpenClaw `dalton-coverage-*` 17 条 cron 的迁移 / 对账 | v1.0 review E"不管"；legacy-workflow-disposition 仅作历史；`~/.openclaw/cron` 已无 dalton 任务 |
| 四池预算的强制执行、单次调用费用上限作为硬限 | owner 09-15 简化为三道上限（日总费用 / AlphaEngine 次数 / 阅读份数），池执行开关 off、机制保留 |
| 通用 Excel exporter"盲做" | owner 09-10：等样本审读后按基金格式做 → 已由 `fund_xlsx_export` 兑现 |
| 完整 Cockpit 视觉重设计 | owner 09-11"保留现有风格，只做语言与信息层级" |
| roic（403）、Reddit（上游已死）、Firecrawl 回退、Bloomberg CSV | "攒到最后裁决"，均为 owner 裁决而非开发 |
| Agenda 认可率 75%、人类逐条批准等 v0.5–v0.7 门槛 | 已达成或被 ADR-0004/0005"默认自治"取代 |
| `multi-analyst-workspace-plan` 的 `~/Library/Application Support/Dalton/workspaces/<slug>` 布局 | 实际改为 `~/.dalton/workspaces/ws-<uuid>`（socket 路径长度），方向调整非缺口 |

### 3. 文档间矛盾或状态不清

1. **thesis_impact 的身份**：v0.9 / v0.7 把它当"发动机"，09-08 停泊说"到 thesis 切片一次性修好并重开"，ADR-0007 实际用修订候选取代了它，但没有任何文档宣布退役；09-23 PROJECT_STATUS 仍在为这条 `enabled=false` 的 lane 对齐 `thesis_impact_assessment / verifier` 路由。需要一条 ADR 收口。
2. **"阶段账本非空 / 三家过初筛"已达成，但 Phase 9 退出门槛的其余条件（reconciliation、memo）没有任何后续文档给出结论**；09-13 之后的状态文件全是运维批次，没有再对照 v1.1 / Phase 9 / 蓝图门槛做过一次盘点。
3. **`STAGE_SPINE` vs 模型阶段门**：onboarding 蓝图把"手册被执行"定义为每阶段有 metric base；现实实现是 `model_stage_readiness` 用 authority 存在性关门，`STAGE_SPINE` 四阶段仍空。两种口径并存，没有文档裁决哪个算"阶段被执行"。
4. **09-16 审计 §4 的实质缺口**（定量 claim 仅 22 条、事件判断 99.6% NO_CHANGE、61% 事实来自网页新闻）此后无复核记录；09-28/29 的 claim 质量批次可能已改变这些数字，但 PROJECT_STATUS 顶部没有更新后的分母。
5. **`remaining-development-queue` 09-13 与 `owner-review-remaining` 09-13** 把"dossier / DebateMap / 市场 / 估值"从开发范围里划出，但同一天的 `vision-owner-coverage-refresh` 仍把"最终交付与真实多工作区验收"列为开发项，而 09-14 的 workspace 报告又以"验收完成"口吻收尾——多环境这一项到底剩什么（安装升级 / 回滚隔离 / 共享容量），没有一份文档给出闭合清单。
6. **ADR-0006 说 owner"不会看到 hash 或 ref"**；09-13 owner 审阅 101 页后仍列出大量泄漏，09-14 修复；但 09-16 审计的"待办 47 条过期 reopen 提案"与 09-18 的"需要你处理 一定会出现"说明审批面的语义至今在修，ADR-0006 的承诺仍不能视为兑现（见第三部分 P1-5）。
7. **仓库 `deploy/connector-governance/*.json` 绝大多数为 `proposed`，live 目录全部 `approved`**：这是设计（安装只播种、owner 签署），但 09-13 的两份 owner 文档还在说 yfinance / sales-notes / IR"待批"，而 live 显示已批——历史文档未标注这些已在 09-16 之后关闭。
8. **PROJECT_STATUS 约行 2287 的"当前判断"仍是 08-20 时代的文字**（"live Core 尚无 ThesisVersion…隔离 canary"），与顶部 09-23 条目并存于同一文件；阅读规则说"顶部覆盖历史"，但这一段位于文件中部却以"当前"自称。

---

## 二、新建研究环境能否完美复用成熟环境的配置状态

### 结论

**不能完美复用。** 差距不在"研究内容"（设计上本就不复制），而在于约 12 项运行配置：新环境要么拿不到、要么拿到的是 09-16 的旧快照、要么要等 owner 事后手工补。当前部署的创建代码（release `d02d07bc`，源 `3124792e`，含 09-18 的"三项统一设定"和 09-27 的治理基线）**从未在生产上创建过任何环境**——两个现有 workspace 都是旧代码建的（`runtime-ready.json` 里没有 `host_scheme` 段），因此新路径只有单元测试证据，没有线上证据。

**成熟环境是哪个**：legacy（8793，"美国 IT 服务研究"）。依据：core.sqlite 4.2 GB（ws-7d 3.1 GB，ws-e399 4 MB）；mission v28 / policy-20 / mandate v12 / constitution v18；44 条 connector 审批（ws-7d 33、ws-e399 13）；有 weekly brief v4、Discord outbox、publication worker、241 个 deliverable 版本。ws-7d 是次成熟（mission v8、policy-8、0 个 deliverable）；ws-e399 从未成功发布任务（policy-1）。

### 配置项对照表

| # | 配置项 | 成熟环境（legacy）现状 | 新建环境会得到什么 | 继承方式 | 差距 / 风险 | 建议开发内容 |
|---|---|---|---|---|---|---|
| a1 | 模型路由（4 档链 + 逐环节钉定） | 21+1 份 `*-model-config.json`，各谱系 :55/:56/:57/:58/:62；owner 09-23 已扇出到全部环境 | 先装模板 v5（09-16 快照），再由 `workspace_host_scheme.adopt_host_scheme` 只读 legacy router 重新发布当前选择 | 模板 + 创建时对齐 | 对齐"永不抛错"，失败只写回执；legacy 自己 brain / verifier 两档内部不一致时被跳过（09-28 脚本注释）；`agreed_selections` 只读 legacy 路径要经 `manager.json.legacy_workspace` → 无 `state_dir / config_path` → 退回 `LEGACY_SERVICE_CANDIDATES` 常量 | manager.json 显式加 `legacy_workspace.state_dir`；创建回执里 `host_scheme.model_routing.status != aligned` 时在 cockpit 创建结果页醒目提示，而非只写文件 |
| a2 | 模型配置非路由字段（`purpose_call_budgets`、`max_output_tokens`） | `mission-document-draft` 09-30 已提到 16000 输出 token；`quality-verifier-model-config.json`（第 22 份）09-25 已加 | 模板 v5 中 `mission-document-draft` 仍是 `{max_cost_usd:1.0}`（4096 默认），没有 quality-verifier 配置 | 模板（过期） | 09-30 刚修的"glm-5.3 思考耗尽 4096 输出为空仍计费"会在新环境复现；ws-e399 也未被 09-30 脚本覆盖（实证漂移） | 重新导出模型模板（`workspace_model_setup export`），manager.json 指向 v6；或让 `install_runtime_template` 后再从 legacy 复制非路由字段——`adopt_host_scheme` 新增 `align_call_budgets` |
| a3 | 凭证槽位 `credential_slot_refs` | 9 个槽位（含 muse-cli-gateway、xai） | `align_credential_slots` 按 legacy 并集补齐 21 份配置 + service.json 各段 | 创建时对齐 | 已实现；ws-e399 仍缺 openai / claude-cli（24 处差异，parity 检查实证） | 无需新开发；对 ws-e399 跑 `repair_workspace_lane_parity.py --apply` |
| b1 | 共享每日预算 | `~/.dalton/connections/shared-daily-budget.json` rev1：$500 / 100000 次 / AE 130 | `workspace_runtime_setup._shared_daily_budget` 写 `shared-daily-budget-binding.json` | 创建时绑定 | 已实现，三环境一致 | 无 |
| b2 | 单次调用费用上限 | `model-call-budget-policy.json` rev4（默认 $1，plan $1.5） | 路径钉进 manifest 与模板 | 共享文件 | 一致 | 无 |
| b3 | 日预算账本策略（thesis-impact-budget） | legacy `configured:fca2…` = $500/天 | 模板 v5 带 5 条谱系到 `fca2…`（$500）；但 `service.json` 的 `thesis_impact.budget_policy_version_id` 由服务模板给 `production:3`（$180） | 模板 | ws-e399 实证：所有 21 份配置钉 `production:3`（$180），legacy $500——同一机器两套日上限 | 服务模板导出时把 `budget_policy_version_id` 改成安装时从模型模板最新谱系解析；或首个任务发布时 `set_research_budget_authority_chain` 自动跑一次 |
| b4 | 任务级预算池 `pools` | legacy mission 有 `pools{adhoc 25, coverage 100, event_response 50, maintenance 5}`，`pools_enforcement: off` | `research-foundation.json.budget_ceilings` 无 pools | 不继承（设计） | 可接受，但 README 11.2 承诺"专项资金池"在新环境不存在 | 若要一致：`draft_first_mission` 从 foundation 带默认 pools |
| c1 | 连接器审批（共享目录来源） | 13 条（AE、web、SEC×3、wiki、sales-notes、guidepoint） | `_connector_records` 按 catalog 逐条重新签发（署名新 owner） | 创建时重签 | 已实现 | 无 |
| c2 | 连接器审批（宿主级 lane） | yfinance×3、sec-ownership×4、hkex×4、prior-research×2、xueqiu×3、xreach×3、blind | `_host_lane_inputs` → `plan_parity_actions`：yfinance / ownership / hkex 无宿主输入要求 → 会签；prior-research / crowd 要求宿主文件存在 | 创建时按条件签 | **control 进程无 `DALTON_PRIOR_RESEARCH_DIR` / `DALTON_OPENCLAW_WORKSPACE` 环境变量，且 `resolve_host_sources()` 在创建时不传 `source_state_dir`**，host-tools / xueqiu、xreach、credential-grants、prior-research 只在 legacy state 里存在 → 新环境 crowd / prior-research 两条 lane 必然缺失（ws-7d 是 09-27 脚本手工补的） | `_host_lane_inputs` 调 `resolve_host_sources(source_state_dir=<legacy state>)`（legacy 路径已由 `adopt_host_scheme` 解析出来，可复用） |
| c3 | 连接器审批（legacy 独有） | cn-hk-findata×6、ir-page-watch×2、roic×2、sec-company-facts v1/v2、sec-financial-statements v1/v2 | 无 | 不继承 | 这些不在 `lane_parity_table` 也不在 catalog；findata / ir-page-watch 若新任务是港股就缺 | 把 findata / ir-page-watch 加进 `lane_parity_table`（含 governance 文件名），或加入 connections catalog |
| d1 | 治理 policy：自动入账 6 条规则 + plan auto-start 2 条 + 闭合 `research_budget` + 独立性谓词 | legacy policy-20 全有 | 首个任务发布时 `ensure_first_mission_auto_commit_policy` 按 `workspace_governance_baseline` 补齐；谓词来自 fresh Core policy-1 | 首任务发布时 | 已实现；但 parity 检查在发布前对 ws-e399 报 38 条 pending，会误导 owner | parity 检查对"无 mission"环境把这些标为 info 而非 pending |
| d2 | `weekly_brief_auto_publish` 与 weekly brief 计划 | legacy plan v4、每周三 07:00 ET、Discord 目的地 | `weekly_brief.enabled=false`，`weekly_brief_plan: None`，无 UI 可配 | 设计上不继承 | README 第 7 节承诺"每周简报"交付物，新环境没有任何入口能开启 | 新增 writer 操作 `publish_workspace_weekly_brief_plan` + cockpit 表单（目的地可选，无 Discord 时落到 cockpit 页面 / 文件） |
| d3 | 文风契约 / 语言策略 | `research-language-policy.json {required:true}` | 服务模板同样种子 | 模板 | 一致 | 无 |
| d4 | writer 参数白名单 / 权限范围 | `writer-tokens.json` principals 操作列表 | bootstrap 生成，operations 列表与 legacy 一致 | 代码生成 | 一致 | 无 |
| e1 | 任务契约（companies、industry、constitution） | mission v28 US IT services | owner 在 cockpit 写目标 → 草案 → 确认 | 设计上不继承 | 正确 | 无 |
| e2 | SEC lane mandate 命名 | — | ws-7d 出现 `mandate:us-it-services-sec-lane:company:…`（`sec_company_facts_lane.LANE_SLUG` 写死） | 代码常量泄漏 | 命名污染，审计时会把 Hyperscaler 环境读成 IT services | `LANE_SLUG` 改为按 workspace slug 派生 |
| f | 连接目录 | `catalog-20260914-v1.json`：24 模型 / 9 来源 | 钉入 display.json | 共享文件 | 目录 09-14 后未更新；09-23 上线的"OpenClaw 模型目录自动跟随"走 `model-catalog-sync.json`（三环境一致），目录文件本身过期只影响创建页展示 | 定期重导 catalog 或让创建页读 router 而非目录文件 |
| g | lane 启用（writer plist 参数） | legacy 99 个 flag | ws-7d 已与 legacy 完全同集；ws-e399 缺 32 个（crowd、feed、guidepoint、hkex、ownership、market、calendar、consensus、prior-research） | 由 state 文件存在与否驱动 | ws-e399 的差距即"新建后不发布任务"的真实形态；发布任务后 `materialize_first_mission_lane_plans` 生成 feed / guidepoint / crowd 计划 | 见 c2 |
| h1 | service.json 引擎参数 | planner_call_budget legacy `{1.0, 80000 in, 4000 out}`、`projection_min_interval 60`、`raw_spool` | 模板 v7：`{1.0, 80000, 16000 out}` | 模板 | 一致或更好；`legacy-control/service.json`（09-15 副本）与真实 legacy 配置已漂移（planner 3.0/250000、路由 :2/:1），任何读它的代码会读到旧值——目前 `model_routing_sync` 走 `LEGACY_SERVICE_CANDIDATES`，需确认候选顺序 | 删除或自动同步 `~/.dalton/legacy-control/service.json`；manager.json 加 `legacy_workspace.config_path` 指向真实文件 |
| h2 | 常驻服务 | legacy 4 个 LaunchAgent + `com.dalton.research-publication-worker`（中文本地化 / 发布 worker，只指 legacy state）+ log-rotate | 3 个 LaunchAgent（writer / controller / control），thesis-impact 未配 | 代码安装 | **publication worker 是 legacy 独占**：workspace 的交付物不会跑 `research_output_preparation`（本地化草稿 / 校对 / 发布）；`research-localization` 目录只 legacy 有 | 让 publication worker 按 manager.json 枚举全部环境，或 `workspace_process.install_workspace` 为每个 workspace 渲染 worker plist |
| h3 | 公开只读看板 | legacy `static_dashboard` 发布到 COS（但已停，见第三部分 P1-3） | `public_publish_enabled: false` | 模板关闭 | README 10.1"公开看板"对新环境不成立 | 创建页给开关 |
| h4 | 备份、日志轮转 | keep_latest 3 / 86400；log-rotate 已覆盖 workspace 子目录 | 同 | 模板 / 脚本 | 一致 | 无 |
| i | 页面显示配置 | `legacy-display/display.json` | `display.json` 由创建写 | 代码 | 一致 | 无 |
| j | owner 手工一次性操作 | 09-24 起 11 个 ops 脚本：`sign_auto_commit_rules`、`sign_research_plan_auto_start`、`restore_independence_predicates`、`align_model_routing`、`repair_workspace_lane_parity`、`raise-document-draft-output-budget`、`split_deliverable_tier`、claim 撤回 / 恢复、`pause-support-backfill`、`bind_shared_daily_budget` | 前 5 项已并入创建 / 首任务流程；后 6 项均硬编码 legacy + ws-7d 两个路径，不会自动覆盖新环境 | 部分 | `claim-support-verification.json`（backfill 暂停 / second-opinion）在 ws-e399 不存在；`deliverable` 第四档拆分依赖 legacy 已拆的策略再对齐 | 把仍需按环境执行的脚本改为读 `manager.json` 枚举环境（`host_environments`）而非写死路径 |

### 新建后 owner 仍需手工执行的步骤（按顺序）

1. 创建前：重导模型模板（含第 22 份 quality-verifier、16000 输出上限）与服务模板，更新 `manager.json.runtime_templates`。
2. 创建后立即读 `state/dalton-core/runtime-ready.json`：确认 `host_scheme.model_routing.status == aligned`、`credential_slots.status in (aligned, unchanged)`、`runtime.shared_daily_budget.status == installed`。
3. 跑 `python -m dalton_core.workspace_parity_cli --workspace <slug> --source-state-dir "<legacy state>"`；对 prior-research / crowd 两条 lane 跑 `scripts/repair_workspace_lane_parity.py --apply`（需停服务）。
4. cockpit 写目标并确认发布首个任务（此时才签入治理基线）。
5. cockpit "模型与预算"保存一次预算（把 `budget_policy_version_id` 从 `production:3` 抬到与 legacy 一致）。
6. 若需要 `mission-document-draft` 16000 输出上限：需另行准备包含新环境的配置同步计划；当时的操作脚本原件已私有归档。
7. 若需要 weekly brief / 公开看板 / 本地化发布：当前无入口，需开发（d2、h2、h3）。

### 建议代码改动清单（不实施）

1. `workspace_runtime_setup._host_lane_inputs`：`resolve_host_sources(source_state_dir=legacy_state)`，让 crowd / prior-research 在创建时自动接入（legacy 路径可从 `resolve_source_state_dir` 复用）。
2. `workspace_host_scheme.adopt_host_scheme`：新增第四步 `align_call_budgets`，从 legacy 复制各 `*-model-config.json` 的 `purpose_call_budgets` / `call_budget`（非路由字段）。
3. `workspace_service_setup.install_service_template`：`thesis_impact.budget_policy_version_id` 改为安装时解析模型模板最新 `budget_policies` 尾版本，而非模板里的 `production:3`。
4. `workspace_model_setup.export_runtime_template`：`ACCEPTED_CONFIG_SETS` 已允许 22 份，但 manager 仍指 21 份的 v5——增加"模板陈旧"巡检（模板 sha 与 legacy 当前配置 hash 不符时在创建页警告）。
5. `manager.json` schema：`legacy_workspace` 增加 `state_dir` 与 `config_path`；`model_routing_sync.legacy_environment` 已支持，只需写入并删除过期的 `~/.dalton/legacy-control/service.json`。
6. `research_output_preparation` worker：配置改为枚举 `host_environments`，或 `workspace_process.install_workspace` 为每个 workspace 渲染 publication worker plist。
7. Weekly brief：新增 writer 操作与 cockpit 表单（`workspace_mission_setup` 里 `weekly_brief_plan: None` 处接入）。
8. `sec_company_facts_lane.LANE_SLUG` 按 workspace slug 派生。
9. `lane_parity_table` 增加 cn-hk-findata、ir-page-watch 两组 lane（含 governance 文件名），或并入 connections catalog。
10. `workspace_health_parity`：无 mission 时把"首任务发布时签入"的 9 条从 pending 降为 info。
11. ops 脚本（`raise-document-draft-output-budget`、`pause-support-backfill`、`run-2026-09-27-support-lane-owner-steps`）改为 `host_environments` 枚举，去掉写死的路径。

### 实证漂移（两个已有 workspace 之间）

- ws-e399 vs ws-7d：凭证槽位 24 处缺失；`budget_policy_version_id` $180 vs $500；`mission-document-draft` 输出上限 4096 vs 16000；连接器审批 13 vs 33；`claim-support-verification.json`、`credential-grants/`、`host-tools/`、`feeds/` 缺失；planner 路由钉 :18 vs :62。
- legacy vs ws-7d：`prior-research` 在 ws-7d 是指向 legacy state 的符号链接（`feeds/prior-research -> <state-dir>/feeds/prior-research`），legacy 一旦迁移即断；ws-7d SEC 发现连续 6 次 `LeaseNotUsable`（审批回执与治理记录不符）；17 条文档研究 admission 待人工恢复。
- 09-23 已上线的 `service_config_location` 修复解决了 legacy 符号链接下读不到 service.json 的问题，但 `~/.dalton/legacy-control/service.json` 作为旧副本仍在。
- 顺带：legacy `agenda` 段仍指向 `wanhua` / `workspace-chem` 的化工旧配置且 `enabled: true`。

---

## 三、Cockpit 系统性排查

### 环境与版本核对

| 环境 | 端口 | release | 源码 commit | cockpit 代码 |
|---|---|---|---|---|
| legacy「美国 IT 服务研究」 | 8793 | d02d07bc | 3124792e | 与 main 逐字节一致（cockpit_plane.py / agenda_control.py / cockpit_control.html md5 相同） |
| ws-7d「美国 Hyperscaler 研究」 | 8795 | d02d07bc | 同上 | 同上 |
| ws-e399「新研究环境」 | 8794 | d02d07bc | 同上 | 同上 |

- main `7826ff21` 与已部署源 `3124792e` 的差异只有 docs/ops 两个文件，**main 上没有"已合并未部署"的 cockpit 修复**。三个未合并分支 fix/cockpit-load-speed、fix/cockpit-translation-delivery、fix/cockpit-budget-verify 相对 main 均为 0 个提交（已全部并入）。
- PROJECT_STATUS 顶部标"源码，待部署"的 09-17～09-23 条目（symlink service.json、模型页十秒、needs-human 名单等）均已在 d02d07bc 中（release 包里存在 `service_config_location.service_config_path`、`chain_link_digest`）。
- 访问方式：127.0.0.1 直连返回 403（需 Tailscale Serve 注入身份头，设计如此）；经 `https://your-node.your-tailnet.ts.net:<port>` 全部 200。本机 shell 设了 `https_proxy=127.0.0.1:7897`，curl 需 `--noproxy '*'`，否则 15s 超时——可能也是 owner 浏览器偶发"读不到"的原因之一。
- 公开只读看板：8792 上的 "PM Cockpit" 是 `~/.openclaw/workspace/scripts/pm_cockpit/server.py`，**不是 Dalton**；Dalton 的 static_dashboard 插件把页面写到 `<state>/public/index.html`，见 P1-3。

### 测试运行结果

`.venv/bin/python -m unittest` 跑全部 29 个 cockpit 测试模块：**500 个用例全部通过，skipped 1**，耗时 85 s。告警：`model_deployment.py:590` unclosed sqlite（4 处）、TemporaryDirectory 隐式清理（4 处）；测试输出里夹带 "cockpit overview background refresh failed: RuntimeError: broken snapshot" 与 "database is locked … retry 8651" 这类预期内的桩输出，建议静音。

### 缺陷清单

#### P0（数据错误 / 功能不可用）

**P0-1 legacy 公司卡片显示已过期的"报告生成失败"，与真实状态相反。**
- 现象：legacy `/v1/cockpit/overview` 中 EPAM `latest_generation_failure` = "最新一次报告生成失败：MissionDeliverableValidationError: number.text must be text of 1..200 characters"，公司详情「概览」页直接显示（`cockpit_control.html:775`）。
- 真值：`initial-screens/` 中 EPAM 最近两张票 09-09 10:28 与 09-14 19:18 均 `succeeded`（deliverable v3 fresh）；那条失败是 09-09 10:13 的。
- 根因：`cockpit_plane.py:132 _latest_initial_screen_failures` 对 `succeeded` 票 `continue`，只在失败票之间取最新，**成功永远不会清掉之前的失败**。
- 修改：改为按公司取"最新一张票"（无论状态），仅当最新票 status==failed 才产出 label；`failure_reason` 里的异常类名经 `_reason()` / 人话映射再显示。测试：成功票晚于失败票时 label 为空；反之保留。

#### P1（明显错误或误导）

**P1-1 legacy「模型与预算」「数据源」页 15 s，每次都是。**
- 实测（warm）：`models` 15.4–16.5 s / 187 KB，`sources` 14.5–15.3 s；ws-7d 分别 0.08 s / 0.11 s。前端 `getJson` 对这两页无超时，但 ui-texts 10 s 超时会先断，control.stderr 里 ui-texts BrokenPipe 与此相关。
- 已定位的成分（离线只读复现，同一份线上库）：
  - `ModelRouter.chain_link_digest()` 1.9 s：`model_route_chain_links`（47,829 行）只有 3 个自动唯一索引，`SELECT tier, MAX(link_sequence) … WHERE served=1 GROUP BY tier` 和按 purpose 的同款查询是全表扫（`model_router.py:1352`）。建议加索引 `(served, tier, link_sequence)`、`(served, purpose, link_sequence)`、`(served, tier, skip_reason)`。
  - `sources()` → `_routing()`（`cockpit_plane.py:7310`）调用 `routing_overview` **没传 `link_digest`**，退回整本账本重放（离线 1.7 s）。09-23 那次修复只改了 `models()`，漏了这里。
  - `models()` 按 11 个 policy pin 逐个 `routing_overview(policy_version_ref=…)`，离线合计 3.1 s（zero-base-review-verifier 0.9 s、event-verifier 0.7 s）。
  - 离线合计约 6 s，线上 15 s；剩余差额未能归因（本机 `sample` 是坏的 homebrew shim），建议在 `models()` / `sources()` 内加分段计时日志后再看一次。
- 另：legacy `model-router.sqlite` 2.1 GB，其中 `model_route_decisions` 953 MB、`model_route_idempotency` 921 MB（dbstat）；后者只是幂等键表却存了近 1 GB，值得看是否把完整请求体存进去了。

**P1-2 legacy Excel 导出 19 s：为了导出一家公司，把整个 4.2 GB core.sqlite 复制一遍。**
- 实测 `export?format=xlsx` 18.9 s（HTML 导出 0.37 s）。离线 cProfile：6.44 s 中 5.24 s 在 `sqlite3.Connection.backup`（`fund_xlsx_export.py:1654 export_company_workbook`），Excel 本身 0.9 s。
- 修改：改为只读 `mode=ro` 连接直接查询，或仅 backup 需要的表 / 按公司过滤；前端下载按钮在等待期间没有进度提示（`cockpit_control.html:785`）。

**P1-3 公开只读看板：本地页面 51 MB，公网副本 16 天没更新。**
- legacy `public/index.html` 51,027,937 B，其中 `EMBEDDED_DATA` 48 MB，全部来自一个键 `/v1/workflows/workflow%3Aprojection-unassigned/tree`（49,436 KB）；其余键合计 <0.5 MB。ws-7d 的同一文件 9.2 MB。手机或普通网络打不开。
- 公网 `https://eve.lumos.space/dalton/` Last-Modified 2026-09-14，12 MB；heartbeat `plugins.static_dashboard.result.publish` = null。原因：legacy service.json 的 static_dashboard 块没有 `public_publish_enabled`（默认 False，`plugins/static_dashboard.py:278`），ws-7d 显式 false。README 10.1 说看板"定期刷新"，与现状不符。
- 修改：(a) 渲染时排除或截断 `projection-unassigned` 工作流树（或只嵌入摘要）；(b) 决定是否恢复公网发布，并在 cockpit 或 README 写明当前发布状态。

**P1-4 legacy 页头显示"研究系统"而不是环境名"美国 IT 服务研究"。**
- `overview.workspace.name` = null；而 `/v1/cockpit/workspaces` 列表里 legacy 名为"美国 IT 服务研究"（`~/.dalton/legacy-display/display.json` 存在）。
- 根因：`workspace_cockpit.py:25` 用 `DALTON_STATE_DIR/../legacy-display/display.json`，legacy 的 control plist 没有设 `DALTON_STATE_DIR`（只有 RAW_SPOOL 与 PYTHON* 变量），于是拼成相对路径找不到；前端 `workspace.name||"研究系统"` 回退。同一类问题 09-23 已在 service.json 上修过一次。
- 修改：legacy 模式改用 `config.state_dir` 推 host root（或读 manager.json 的 `host_root`），不依赖环境变量；测试：未设 env 时也能读到 display_name。

**P1-5 needs-human 卡片把人引到不存在的页面，且没有可按的按钮。**
- legacy 50 条、ws-7d 18 条 `controlled_recovery` 项 `where` = "运行"，前端渲染为"在哪里做：运行 页"，但导航只有 workspace / goal / steer / log / ask / approve 六个 view，没有"运行"页。
- `action` 文本是一个 JSON 文件路径（`…/mission-document-research-runs/holds.json`）加"逐条看下面"，`needsHumanItem()`（`cockpit_control.html:1750`）不渲染任何 action 按钮；09-18 记录的"有得按"指的是 CLI `document_recovery_cli authorize-reentry`，页面上并没有。
- 标题直接带 32 位 admission hash（"等人决定：0746fe99…"），`detail.reason` 为英文枚举（`unproved_send_failed_after_automatic_retry` 等，legacy 49 条）。
- 修改：`where` 指向真实 view（approve）；reason 枚举映射成中文（"送达未确认，自动重试一次仍失败"等）；标题用公司 + 问题摘要代替 hash；若维持 CLI 门，至少把完整命令放进"技术详情"可复制。

**P1-6 ws-e399「新研究环境」从建好到现在没有一次目标发布成功。**
- `history?kind=goal` 4 条全部 failed（09-15、09-16）：两次 `claude-fable-5-1 [INVALID_HOST_RESULT]: host returned invalid text`，一次 "every model in the brain chain failed"，一次预算拒绝。overview 仍 `awaiting_mission`。
- 预算拒绝那条的文案自相矛盾："committed 949140 + reserved 158190 against day cap 180000000"（1.1 美元 vs 180 美元）却报 `mission_budget_exceeded`——`thesis_impact_budget.py:664` 在 mission 自身额度超限时仍把 day cap 的数字写进 message，owner 无法看出是 workspace-setup mission 的小额度超了。
- 修改：拒绝信息按实际触发的那一档（mission / outer / shared / day）打印对应的上限；ws-e399 的 brain 链需补 openai / claude-cli 凭证槽（pending-deploy 已列）。

**P1-7 日志页与运维页仍把英文异常原文送到前端。**
- ws-7d `log` 事件 detail："Document original is durably unreadable; no read-completion receipt or Claim was produced. PublicWebSourceError: fetched page is not valid UTF-8…"（前端 `readableLogText` 只对两种正则做人话，其余靠 `looksTechnical` 折叠）。
- `ops` 页 `dependencies[].items[].reason` 含 "CockpitModelError: the model call did not succeed (POOL_EXHAUSTED)"（legacy 200 处、ws-7d 76 处）；前端显示的是 `display_reason`，但 `technical_details.reason` 同文，且 legacy `terminal_items` 1,849 条、`ops` 响应 2.2 MB，全部随每次打开运维页下载（前端只用到 terminal_items 一处）。
- 修改：日志 detail 走同一套 `_reason()` 中文映射；ops 端点对 terminal / historical 分页或只返回计数 + 前 N 条。

#### P2（体验 / 文案 / 一致性）

- **P2-1 结论计数口径不一致**：legacy overview `totals.claims` 13,878 vs 结论页 `total` 11,402；ACN 卡片 2,539 vs 结论页按公司 2,304；AMZN 1,901 vs 1,696。结论页默认 `canonical=1`，卡片数的是全部版本，页面上没有任何一处说明差在哪。建议卡片也用 canonical 计数或在结论页标"含已合并 / 已撤回 N 条"。
- **P2-2 车道状态词超出文档口径**：README 10.5 说车道只有六种状态，实际 overview.activity.lanes 出现 `launched / busy / dispatched / waiting / held / idle`；`ops.lanes.counts` 只统计六种，"dispatched 2 + waiting 3"被塞进 `other: 5`，headline "11 项执行中，23 项待命，5 项受阻"漏掉这 5 条。前端没有渲染 `other`。
- **P2-3 轨迹页混合中英文**："工单retryable：goal-0ef0…"（ws-e399 9 条）；`WORK_STATE_LABELS`（`cockpit_plane.py:1555`）缺 `retryable`、`running`。所有工单标题带完整 hash（"工单完成：dossier-6f12513b…"），建议显示车道 + 公司。
- **P2-4 legacy overview 快照几乎持续重建**：TTL 8 s，前端在 goal 页每 6 s 轮询；实测 legacy 一次重建约 16 s、ws-7d 约 12–15 s，只要有人开着页面就是"刚建完立刻再建"。09-23 已处理 controller 读盘，cockpit 这条没处理。建议 TTL 提到 30–60 s 或按 heartbeat `last_tick_at` 变化才重建。
- **P2-5 ws-7d 车道每小时重复拉取同两份失败的 MSFT 10-K**：`log` 显示 07:17/07:22/08:23/08:28/09:29/09:34/10:28/10:43 八次"网页获取失败（MSFT）sec.gov …msft-20250630.htm / msft-20260630.htm"，走的是 web-fetch 而不是 SEC 连接器，ops 页里也没有对应挂起项。legacy 同类：websets.exa.ai 三次。应有失败计数上限并进 ops 待办。
- **P2-6 模型页**：`claim_support_verifier` / `claim_support_backfill` 两个环境都 `unconfigured`（pending-deploy 已让 owner 配），`street_estimate` 显示"未配置"但来源是 mission_consensus_lane（是否应显示为"确定性 / 由车道决定"待确认）；三条"grok-4-6 已消失"通知在两个环境各重复 3 次（按环节一条），无合并。
- **P2-7 approvals 两条 deep_insight_gate 卡标题完全相同**（"深度认知评审十二问：是否让这家公司进入完整覆盖"），不带公司名，只能展开才知道是哪家。
- **P2-8 legacy 配置残留**：service.json `agenda.enabled=true`（wanhua 配置）而 heartbeat `agenda.state=retired`，controller.stderr 里 "legacy agenda plane retired (ADR-0009)" 142 行；`~/.dalton/legacy-control/service.json` 是 09-15 的旧副本，与 live 配置不同，仓库内无人读取（grep 为空），容易误导排障。
- **P2-9 参数容错不一致**：`log?limit=abc` 静默用默认 150 并返回 200，`claims?limit=999999` 静默封顶 300；`history?kind=zzz`、`job?id=` 则 400 且 message 是英文（"unknown history kind"、"job not found"）——这些 400 body 没有 `display_message`，前端会把英文原样显示。

#### P3（代码质量 / 日志）

- writer.stderr（legacy 16 MB）**每行没有时间戳**，无法按天统计错误；本轮只能确认部署行在第 180,785/180,798 行之后无新错误。部署前累计：HostToolRunError prior-research / company-wiki / sales-notes `child failed: exit 1` 各 ~1,030 次；`document research recovery hint drifted` 369；`multiple owned terminal tick` 237；`No space left on device` 9 次（当前磁盘 53 GB 可用，EveSSD 1.7 TB，历史事件）；引用已不存在的 release `59b2a605…/venv/bin/python` 22 次。
- legacy control.stderr 在 B1-3 修复前累积 1,273 条 BrokenPipe / ConnectionReset 完整 traceback（09-25 前），现已改为单行。
- 单测 ResourceWarning：`model_deployment.py:590` 未关闭的 sqlite 连接（多次）；tempfile 隐式清理告警。
- `~/.dalton/workspace-logs/` 与 `~/.dalton/workspaces/*/logs/` 两套日志目录都停在 09-16，真实日志在 `~/Library/Logs/Dalton/`（launchd plist 指向），文档 / 工具若还指向前者会读到旧数据。

### 已验证正常的部分

三环境数据隔离正常（各自公司、claims、needs-human 互不串）；`workspaces` 列表三环境一致；needs-human 与 approvals 无重复项（09-13 的 42 条重复 gate reopen 已消失）；EPS / 单位缩放已修（figures 走 `display_label` + unit）；历史问答记录中未见跨公司串数据；HTML 页面三环境 md5 一致且等于 main；CSP / Cookie / CSRF 头正常；日志 `since`、轨迹 `before`、结论 `cursor` 分页均可用；`ui-texts` 单键 17 ms。

### "已修复但未部署"清单

空。main 相对已部署源仅多两份 ops 文档；所有 cockpit 相关修复（`e71947ba` 输出上限、`4ffc905b` 在途封顶、09-23 的 symlink / 模型页 / needs-human 三条）都在 d02d07bc 里。但 09-23 "模型页十秒 → 0.14 s" 的效果**在 legacy 线上没有兑现**（仍 15 s），见 P1-1。

### 越界观察

`~/Library/Application Support/Dalton/state/dalton-core/public/index.html` 与 8792 端口 "PM Cockpit" 是两套东西，README 10.1 的"公开只读看板"应指明是哪一个；ws-7d `expired_lease_count=1`、ws-7d 周报 `state=disabled` 与 legacy 不同，属于第二部分 d2 的问题。

---

## 四、建议的处理顺序（汇总）

1. **先修会误导 owner 的显示**：P0-1 过期失败标签；P1-4 页头环境名；P1-5 needs-human 卡的 where / action / reason；P1-6 预算拒绝文案。
2. **再修 legacy 的性能**：P1-1 索引 + `_routing()` 传 link_digest + 分段计时；P1-2 Excel 导出去掉整库 backup；P2-4 overview TTL。
3. **然后收口多环境配置继承**（第二部分改动 1–5、8–11），并重导模板 v6。
4. **文档收口**：thesis_impact ADR、Phase 9 门槛盘点、PROJECT_STATUS 中部"当前判断"归档、README 10.1 看板说明、legacy-control/service.json 处置。
5. **最后才是新功能**：第一部分 1.1 的四个合同与 1.3 的 owner 杠杆 / 投递。
