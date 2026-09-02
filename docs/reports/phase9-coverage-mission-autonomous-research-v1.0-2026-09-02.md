# Dalton Phase 9：任务驱动的自主研究（Coverage Mission）v1.0

日期：2026-09-02  
状态：当前执行顺序基线；替代 Phase 8 v1.0 的近期顺序，不反向改写历史报告  
触发：owner 2026-09-02 指令——①把 chem agent（workspace-chem）`references/manual` 里的研究方法论、原则和任务理解植入 OS；②记录恰当的 next step 并开始开发。owner 同日说明目标形态：「我给 research OS 下达任务（比如建立对 US IT service 行业的首次覆盖），OS 能开始 7×24 自主研究，自己从 SEC、AlphaEngine、Guidepoint、web search 等途径找资料，建立行业认知、公司财务模型和预测」。

## 结论

1. **Phase 8 的控制面已经建完，缺的是驱动方向。** live 上「获取 → 验证 → 入库 → thesis-impact → 周报」每一环都单独跑通；但自主的那半边只会「看」不会「记」：Bounded Planner 六个 loop、80 条提案、31 条 outcome 全部终态 `evidence_observed_for_review`，8/27 后 live Core 没有新增 Claim。原因不是缺 connector，而是系统里没有一个对象承载「建立 US IT services 首次覆盖」这种指令，planner 只能盯 ticker 列表。
2. **Phase 9 的目标是把驱动方向反过来：人给任务，机器跑全程，人在检查点看结果。** 新增两个 human-only、append-only 的 authority——`ResearchPlaybook`（团队分析师手册的合同化）和 `CoverageMission`（任务层对象），并用 ADR-0004 定下自动化身份的写入范围与人类检查点。
3. **方法论不进 prompt，进合同。** chem 手册里的三段式流程、Deep Insight Gate 12 问、memo 12 个 key questions、analyst level 验收标尺、tracker 清单、模型纪律和证据纪律，全部转写为 `ResearchPlaybookVersion 0.1` 的冻结字段；能被 gate 检查的（阶段顺序、人类检查点、五词决定、数字溯源规则）由 validator fail closed，不靠模型自觉。
4. **P9a 已完成 development candidate 并通过隔离 canary（含 live Core 只读副本）**；未部署、未激活任何自动化写入。首个 live 激活（发布 playbook v1 与 US IT Services mission v1）保留 owner gate。

## 从 chem 手册植入了什么

来源：`workspace-chem/references/manual/{research-process,analyst-levels,memo-template,tracker-checklist}.md`、`references/coverage-architecture.md`、`config/coverage.json`、`references/templates/chem-model-template.md`、`JOURNAL.md`。转写结果在 `deploy/phase9/p9a-research-playbook-v1.json`，合同在 `contracts/research-playbook-version.schema.json`。

| 手册内容 | Playbook 字段 | 机器可检查的部分 |
| --- | --- | --- |
| 三段式：Initial Screen（≤1 周）→ Investment Memo → Position Monitoring | `stages`（六个冻结阶段：initial_screen → deep_insight_gate → industry_model → company_model → investment_memo → active_coverage） | 阶段顺序冻结；每阶段有 required_readings / required_outputs / exit_gate |
| Deep Insight Gate 12 问 + pass rule | `stages[deep_insight_gate].exit_gate` | `human_checkpoint=true` 不可去掉；gate_passed 只接受 human actor |
| 「Gate 未通过不得把工作假设写成正式观点」 | mission 阶段账本 | 进入第 k 阶段必须先 gate_passed 第 k−1 阶段 |
| memo 12 个 Key Questions、IS/IM 结构 | `key_questions`、`deliverable_templates` | 非空、唯一；IM 阶段 exit gate 引用 |
| 五种决定 NO_CHANGE / STRENGTHENED / WEAKENED / BROKEN / NEW_THESIS | `decision_vocabulary` | 与冻结词表逐字相等，否则拒绝 |
| Analyst Level Basic 1–4 / Advanced 1–4 | `analyst_levels` | 作为 mission 验收标尺，后续 P9d 评测集引用 |
| Tracker & alerts 清单 | `tracker_classes` + `agent_binding` | 每类 tracker 如实标注 connected / probe_only / not_connected |
| 风险回报标准（3–5 年 3–5x、50% upside、30% downside） | `risk_reward_standards` | 文本 |
| 模型模板：期间标准、单元格溯源、单季做差、空值 gate、勾稽检查 | `model_discipline` | 文本；M2 时映射到 ForecastLine 与 reconciliation 合同 |
| 信源规则、百度百科禁用、关键数据两源 | `evidence_discipline` | `number_provenance_rule` 冻结；banned_sources |
| JOURNAL 教训（累计口径误当单季、分部数抄错、长任务不落盘） | 写进 `model_discipline` 与 P9b 的 reconciliation 要求 | — |

没有植入的：化工产品链、价差周报等行业特有内容，留给 Driver Pack / Constitution；chem 的 cron 注册表与 coverage.db 状态机（已被 Core 的 Scheduler / Bounded Planner 替代）。

## 与愿景的关系

v0.1 愿景第 1 节写的就是「人只定目标和边界、校正航向、讨论、读结果；Dalton 自主形成议程、拆计划、积累证据和模型、检验 thesis、重新规划」。Phase 1–8 建的是让这件事可审计、可回滚、可预算的控制面；Phase 9 补的是 v0.1 第 5.1 节 Mandate & Policy Plane 里一直缺的「任务层」。第 7 节的三层权限模型不变，只是「条件自主」一层从 policy prose 变成 mission 合同字段（ADR-0004）。

## Phase 9 切片（按依赖顺序）

### P9a：ResearchPlaybook + CoverageMission authority（本轮，development candidate 已完成）

- `research_playbook.py`：human-only、append-only、版本化，pointer 指向 active 版本；六阶段与出口门、12 问、五词决定、level 标尺、tracker、模型/证据纪律；validator 拒绝改阶段顺序、去人类检查点、改决定词汇、弱化数字溯源规则。
- `coverage_mission.py`：mission 版本（行业、universe+tier、研究问题、交付物、来源计划、exact 绑定 playbook/constitution/mandate、autonomy、budget）+ 阶段账本 + 进度投影。autonomy.may_write 只能从冻结词表选，thesis 永远不在其中；human_checkpoints 只能加不能删。
- writer ops（human-governed + core）、JSON 合同 ×3、`deploy/phase9/` 两份 manifest、`scripts/run_p9a_playbook_mission_canary.py`（in-memory 与 `--source-core` live 只读副本两种模式）。
- 实现与验收见 [P9a 报告](p9a-research-playbook-and-coverage-mission-v0.1-2026-09-02.md)。

### P9b：观察 → Claim 闭环 + 10-K/Q4 派生（目标 10/1 ACN Q4 业绩前就位）

进展（2026-09-02）：P9b-1 的 10-K 同 filing 季度对比已激活 live；P9b-2 的 mission observation → 持久 dispatch
queue → exact-accession SEC lane → formal Claim/Evidence → stage-claim ledger 已完成 development candidate 与 live Core
只读副本 canary，尚未激活自动写入。FY − 9M 跨 accession 派生仍未做。

- `automation:coverage-mission` 按 mission `may_write` 中的 `claim/evidence` 触发 SEC lane：`record_observation_followup` 发现新 accession 后，在 policy 授权与 mission 日预算内调用 lane，人工路径保留。这是 Phase 8 退出门槛第 2、3 条真正需要的一步。
- 追加式扩 company facts scope 到 10-K；Q4 = FY − 9M 作为新的冻结公式合同（照 S7d-5 预算注册表先例）；旧 plan 继续按 10-Q 重验。不改，ACN 10/1 那天 planner 会报 not_found。
- 每条自动 Claim 同时写 mission 阶段账本的 observation，让进度投影能回答「这家公司卡在哪一步、缺什么」。

### P9c：Forecast reconciliation（第一条 Outcome 对象）

- 新 authority：实际 Claim 落库后自动生成 reconciliation（预测线 exact 版本 vs 实际 Claim exact 版本、偏差、方向），喂 thesis-impact 与下一期周报。playbook `model_discipline` 最后一条在此兑现；愿景里 Evidence→Claim→Thesis→Driver→Question→Outcome 的最后一环从 0 条开始记。
- `forecast_overturn` 是人类检查点：偏差超过 mission 定义的阈值时不自动改预测，只登记并升级人工。

### P9d：多源接入按 mission source_plan 逐条开

- AlphaEngine：AE-2 search 驱动发现（search_library → stage 语义候选 → 人 accept），把 `probe_only` 变为 `connected`；不违反 ADR-0003 B。
- web search、Guidepoint：各自走 Connector Protocol 的 shadow → gate → live，每接一条让 mission 多回答一类研究问题。顺序：web search（行业数据/管理层变动，只读公开）先于 Guidepoint（付费、一手、需 query 纪律）。

### P9e：建模自主化（M2）

- 从 XBRL 历史生成标准化三表（playbook 期间标准），套 IT services driver（headcount、utilization、bookings、book-to-bill）产出预测线；估值 fail closed 直到市场数据 connector 解冻（owner gate）。
- 每次模型更新按 playbook 纪律留下旧值、新值、差异原因、受影响 thesis。

### P9f：Mission 交付物与验收

- Mission 完成的标志是一份可读的初始覆盖报告（Initial Screen 模板）+ 五家公司通过 Deep Insight Gate（人类裁决）；周报改为「Mission 进度 + 新增认知 + 预测变动」。
- 用 playbook `analyst_levels` 做验收标尺：P9 结束时对照 Basic L1（溯源零错误）与 L2（模型历史数与 filing 对得上）逐条打分，作为 P8d 评测集的第一批样本。

## Phase 9 退出门槛

- live 有 1 个 active CoverageMission，五家公司阶段账本非空，至少 ACN 通过 Deep Insight Gate（人类裁决）。
- 至少 1 条 Claim 由 `automation:coverage-mission` 在 mission 预算内自动触发 lane 产生，并走完 thesis-impact → 周报。
- ACN 10/1 Q4 实际数落库后自动生成 1 条 forecast reconciliation，并出现在下一期周报。
- 至少 2 条 source_plan 从 `not_connected/probe_only` 变为 `connected`。
- 全程逐条人工审批数为 0（检查点裁决除外）；扩范围、扩预算、Thesis 变更仍走人工。
- **止损**：到 2026-10-15 仍没有自动触发的 Claim，停止新增 connector，只修这条链。

## 继续冻结

自动 thesis revision、Capability builder/sandbox、第二 runtime、Temporal/Postgres、embedding-first 检索、multi-agent fleet。P8d 评测集在反馈样本 ≥5 条前不启动。

## 顺手要清的三件事（不属于 P9a 范围）

- `src/dalton_core/llm_research_planner_worker.py` 有一处未提交改动（放宽 Scheduler 同连接检查）已部署在 live venv 里，仓库 HEAD 没有；应单独 commit 并补进 P8c-4c 报告。本轮未动。
- 8 个已合并 worktree 分支可删；`s7d-connector-governance` 未合并但 S7d-2 已重做，确认后一起清。
- PROJECT_STATUS 头部日期已随本轮更新。
