# 全部 vision 讨论的复盘：遗漏、修正与派发建议

日期：2026-09-09 · 只读复盘（Opus 5 subagent 完成，主 agent 整理），未改动任何代码
基线：蓝图 v1.0、并行开发计划 v1.0（含 owner 两批裁决）、ADR-0007 / 0008（accepted）、main `888a814`
方法：通读 9 份愿景 / 方向文档 + SPEC + ADR-0001~0008 + legacy disposition + Phase 8/9/10 阶段报告；`git log` 400 条；对 22 个「被承诺的对象」逐个 grep 代码判定 live / 半 / 无

---

## A. 愿景版本时间线

**v0.1（08-13）** 定义系统是「持续覆盖行业和公司的自主研究分析师」，人只做四件事（定目标边界、校正航向、讨论、读结果），机器做八件事。承诺九个平面（Mandate、Perception、Ledger、Agenda、Planner、Execution Fabric、Verifier、Commit Gate、Reflection、Skill Lifecycle）。已兑现：Ledger、Scheduler/WorkOrder、Verifier + independence predicate、Commit Gate。未兑现：Reflection（零实现）、容量配额、Skill 自写（v0.5 起冻结）。

**v0.2（08-13）** 收敛为「architecture-first, implementation-thin」，定下五条至今未破的语义：Ledger 与 Model Store 分离、Model IR 是数字唯一权威而 Excel 降级为编译产物、Evidence 经 Claim 挂 Thesis、hybrid temporal、双咽喉。全部仍成立；Excel-as-compiled-artifact 落在 P13-M5。

**v0.3 / v0.4（08-15）** 工程基线：Slice 1 无损 promotion、ClaimIndex 从快照派生、DocumentIndex FTS5、ContextPack materializer、PerceptionSnapshot 进 Core。全部 shipped 且仍 live。v0.4 把 embedding 降为可删除 sidecar，先 FTS5，至今未破。

**v0.5 / v0.6（08-15）** Kimi「没有发动机的精密变速箱」批评后的收窄：价值只看固定成本下经人工接受的 Claim 数量与质量；冻结 connector 品类、Interrupt/Reflection、gap detector/builder/sandbox、Model IR、embedding、多 runtime。门槛已达成；冻结项除 connector 外仍冻结。

**v0.7（08-21）** 三条至今最有价值的纪律：breadth 先于 depth；开发过程也受 gate；第一产品是每周 5 家公司的验证简报。Gate 0–4 全部完成，第一产品由 weekly brief v4 兑现。

**v0.8（08-23）** 定数据源优先级与配额，并第一次写下「市场与估值数据缺 authority，没有这条链前不生成正式估值输出」——即今天 Wave 1A 要补的洞，隔了 17 天。Model Input Ledger v1 六类对象，前四类由 `model_input.py` + P13ao 兑现。

**v0.9（08-26）** 诊断「隔离 canary 与 live 之间有一道没人跨的墙」，定 Phase 7（S7a–S7e），全部 shipped。

**Phase 8 v1.0（08-27）** 单主题自主认知闭环，主题「美国 IT 服务需求是否见底」。P8a–P8c4 全部 shipped，含 ProbeTemplate 目录 + BoundedPlannerLoop 准入、观察→研究注意力、doctrine ContextPack + LLM planner。

**Phase 9 v1.0（09-02）** 任务驱动：chem 手册合同化为 Playbook，CoverageMission + ADR-0004。P9a–P9d 全部 shipped。

**v1.1（09-07）** 子任务应是「ACN 的 Initial Screen 和财务预测」而非资料计数；Phase 10 按 Playbook 阶段执行。P10a/b/c + 模型层 shipped；P10d/e/g → 蓝图 P12d/P12e/memo。

**蓝图 v1.0 + 并行开发计划 v1.0（09-09）** 五个缺失层 + 两条贯穿线；Wave 0 已 merge；ADR-0007 / 0008 accepted。

---

## B. 承诺清单与状态

| 项 | 首次承诺 | 今天状态 | 现计划覆盖？ | 裁定 | 理由 |
| --- | --- | --- | --- | --- | --- |
| Research Ledger | v0.1 | live `store.py` | 全程 | 已覆盖 | 底座 |
| Agenda Engine + AgendaCard | v0.1 | 代码 live，但唯一主体万华 09-04 退役 | 无 | MODIFY | 没有服务对象；P14a 要么继承要么显式退役 |
| Perception & Event Plane | v0.1 | 半：`perception.py` 只服务已退役 agenda | Wave 3 P14a | MODIFY | 别建第二套事件平面 |
| 容量配额四池 | v0.1 §5.4 | 无（`LaneSpec` 只有 `order`） | 无 | **ADD** | adhoc 解禁后必然抢同一日预算 |
| Planner / WorkOrder / checkpoint | v0.1 | live | 全程 | 已覆盖 | |
| Independent Verifier + independence predicate | v0.1、SPEC | live；3×30 canary 已过 | 仅 thesis-impact（停泊） | **MODIFY** | 新的认知层产出无独立验证要求 |
| 五词决定 | v0.1 | `DECISION_VOCABULARY` 无第二消费者 | Wave 3 / ADR-0007 | 已覆盖 | |
| Reflection & Replanning | v0.1、v0.4 冻结 | 无 | 无 | **ADD**（小） | 周会「为什么没改主意」缺原料 |
| Skill Lifecycle / gap detector / sandbox | v0.1 Phase 4 | 治理链 live，探测生成零实现，五版冻结 | 无 | drop | |
| 多 runtime / Temporal / Postgres | v0.1 Phase 5 | 无 | 无 | drop | |
| Model IR / Excel 编译产物 | v0.2 | P13al–ao 兑现 | Wave 1C + M5 | 已覆盖 | |
| ClaimIndex / DocumentIndex / ContextPack | v0.3–0.4 | live | Wave 1B | 已覆盖 | |
| embedding sidecar | v0.3 | 无 | P12b「稳定后」 | drop | 解冻条件未满足 |
| ResearchQuestionBacklog | v0.4/0.5 | live，P8c-3 起自动登记 | Wave 3 P14e | 已覆盖 | 缺下游 |
| ProbeTemplate 目录 + loop 三重预算 | P8c-1 | live | 计划未提 | **MODIFY** | P14e 应建在其上 |
| DoctrinePack / PlannerContextPack | P8c-4b | live | | 已覆盖 | |
| Constitution `method` 三字段 | P8a | 无消费者 | 无 | **ADD** | Wave 2 dossier / DebateMap 正需要 |
| IndustryEvidencePack debates | v0.8、S7d | live，debates 人工发布 | Wave 2 P12c | 已覆盖 | |
| thesis-impact 生产者 + verifier + 30 例 | v0.7 | 停泊（config flag） | Wave 3 P14b | 已覆盖 | |
| weekly brief lane | v0.7 / S7e | live tick lane，每周真发 | Wave 3 末尾 | **MODIFY** | Q 线现在就能打分 |
| 自然语言方向控制 | 08-24 | live | | 已覆盖 | |
| ad-hoc 四选一路由 / `answer_after_refresh` | S4/S5 | 机制 live 但 `adhoc_research_enabled: False` | Wave 3 | **MODIFY** | 是开关不是新建 |
| Claim 挑战与退役 | P10b | live | Wave 1B | 已覆盖 | |
| MissionDeliverable / Initial Screen | P10c | live | Wave 1D + Wave 3 reopen | 已覆盖 | |
| Deep Insight Gate / 行业框架 | P10d/e | 合同有 lane 无 | Wave 2 | 已覆盖 | |
| 市场 / 估值 authority | v0.8 | 无 | Wave 1A | 已覆盖 | 承诺 17 天才动 |
| AnalystJournal | 蓝图 | 无 | Wave 1D | 已覆盖 | |
| 事件日历 / 8-K / Form 4 / 13F | v0.1 §3.4 | 无 | S 线「之后」 | **ADD** | ④持续覆盖没有事件源 |
| 自主规划质量指标 | v0.1 §10.2 | 无 | 无 | **ADD**（小） | |
| Interrupt / park / resume | v0.4 | 无 | 无 | drop | 预算池即可 |

---

## C. 遗漏项

**C1. 事件日历（`CatalystCalendarVersion`）**：company_ref × event_kind {earnings, guidance, investor_day, filing_due} × 预期日期 × 来源 invocation ref × confirmed/estimated；yfinance earnings date 与 SEC 8-K Item 2.02 互证，只有 confirmed 驱动 preview；append-only，日期变化出新版本带 `change_reason: driver_event`；cockpit 公司卡「下一个催化剂 T-N 天」。建于 Wave 1A 的 yfinance connector 之后。

**C2. 容量配额池**：`LaneSpec` 增 `budget_pool` 与 `pool_share`；mission budget 增 `pools{coverage, event_response, adhoc, maintenance}`，和 ≤ `max_daily_cost_usd`；未用份额只允许 `coverage` 借用并留痕；超池 = 该 lane 当日 `skipped:pool_exhausted` 进 cockpit 日志。随 P14e。

**C3. Constitution `method` 接消费者**：DebateMap 候选争议先过 `question_admission` 确定性检查（绑定 driver、双方证据、在 mission 因果链上），不过的记 `rejected_by_constitution`；dossier 的 `demand_drivers` / `supply_and_cost` 分节由 `causal_chain` 派生；发布前结构检查读 `output_rubric`。作为 Wave 2 各 agent 的规格。

**C4. 规划质量指标 + 轻量 Reflection**：`ResearchCycleReflection`（每周一条，append-only）：各 lane 花费 / 池上限、新登记问题数与被回答数、退役 Claim 数、inquiry 被派发比例、闲置 tick 比例；只提出 backlog 候选与 policy 建议，不写 Ledger、不改 policy。

## D. 修正项

**D1** `ResearchTask` 不是新对象：是一条以 planner inquiry 为 question、以已准入 ProbeTemplate 为动作集、带 rounds/cost/seconds 三重预算的 `BoundedPlannerLoop`；解禁 = `agenda_control.py:267` 的 `adhoc_research_enabled` 置 True + 为 web-search / AlphaEngine / SEC 各发布一个 ProbeTemplate。
**D2** Q 线 rubric 评分调用必须与被评产出的 producer 属于不同 `model_family`（既有 independence predicate）；Wave 2 dossier / DebateMap 每版发布前跑同样约束的结构 + 证据校验，verdict 沿用五值词表。
**D3** Wave 3 P14a 前先裁决 `perception.py` / `agenda_coordinator.py` 去留：迁移（PerceptionSnapshot 作为 ResearchEvent 来源）或显式退役。两套事件平面并存是 ADR 级债。
**D4** 周报 rubric 直接对 live `weekly_brief_issue_versions` 打分（≥3 期）；搁置的是投递不是评估。
**D5** gate reopen 判据 = 用当前证据重跑同一份出口门结构自评，任一项从「缺」变「有」即生成 reopen 提案；`gate_reopen` 仍是人类检查点。

## E. 冲突或已覆盖，不管

Skill 自主生成闭环；多 runtime / Temporal / Postgres；Interrupt / park / resume；embedding-first 检索；万华 Agenda shadow 及其指标；v0.5–v0.7 的 gate 门槛（已达成）；v0.7 第一产品（已由 brief v4 兑现）；legacy 日报与 17 条旧 cron；roic / Reddit / Firecrawl / Bloomberg（已列为最后裁决）；Model IR 超出 v1 的部分。

## F. 派发建议

现在：(1) D1 专项研究解禁面；(2) C1 事件日历（Wave 1A 合并后）；(3) C3 作为 Wave 2 规格；(4) D4 + C4 进 Q 线。
Wave 2：C2 预算池、D2 推广、S 线。Wave 3：D3、D5、C4 完整版。
依赖链：Wave 1A → C1 → P14f；C3 → Wave 2 dossier/DebateMap → P12d；D1 → C2 → P14e；D3 → P14a → P14b/c/d。
