# Owner expectations gap refresh — 2026-09-10

基线：只读审查最新集成树 `a1301cc4f62e3ca5b8fccd825c468b8e9111e225`。对照材料为
`analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md`、
`vision-review-against-plan-v1.0-2026-09-09.md`、
`vision-and-next-phase-v1.1-2026-09-07.md`、它们引用的并行计划、owner 部署步骤、release/activation
报告及 `docs/PROJECT_STATUS.md`。本报告只核对代码、配置与已有验收记录；没有读取或修改 live，
也不把 rehearsal、stub launch 或 authority 存在本身算作产品成功。

## 结论

09-09 蓝图列出的主体架构缺口已经大幅收敛。市场/估值、ClaimIndex、CompanyDossier、DebateMap、
Deep Insight Gate、行业框架、预测、事件日历、ResearchEvent、事件判断、thesis revision candidate、
ResearchTask、预算池、Reflection、ask v2、ConvictionCall、质量评分和 AnalystJournal 都已有 authority、
生产者或协调器。Constitution 的 `question_admission`、`causal_chain`、`output_rubric` 也已进入 dossier、
DebateMap、Deep Insight 和行业框架的真实生产路径。因此不能再按旧报告把这些项目标成“无”。

当前最高优先级已经从“再造 authority”变成两件事：第一，部署最新修复并验收五家公司真实产物；
第二，补齐研究流程末端仍不存在的 Investment Memo 与 Excel 公式导出。另有少量能力已完成但仍需
owner 激活，以及若干已有组件没有接到产品闭环。

## 当前差距清单

| 优先级 | owner 期待 | 当前分类 | 证据 | 尚缺动作 | 可判定的验收标准 |
| --- | --- | --- | --- | --- | --- |
| P0 | 五家公司形成可读、可核验且持续更新的认知产物 | **已实现，尚未完成 live 产品验收** | `docs/PROJECT_STATUS.md:5-7` 记录 mission v14 已签；`mission-v14-product-acceptance-progress-2026-09-10.md:3-14` 仍是 0/15，且列出 dossier、scheduler、event judgement 的部署后修复；`continuous-wave4-release-2026-09-10.md:37-41` 明确 stub 不算完成 | 冻结并部署这些 post-signature 修复，在相同已签 mission 下让 coordinator 真实结算；逐条检查产品而不是 child/tick 状态 | ACN、EPAM、IBM、DXC 各出现 dossier、DebateMap、event judgement；CTSH 只在合法 gate 后加入。每条记录可重算 hash、引用存在、producer/verifier 独立、费用只记一次；失败必须给持久且可恢复的具体原因 |
| P0 | CTSH Initial Screen 满足首次覆盖资料门 | **实际数据缺失，不是规则缺口** | `mission-v14-product-acceptance-progress-2026-09-10.md:34`：只有 2/4 条有效 earnings calls，8 条错公司资料已 dismissed；旧蓝图要求四次电话会并禁止误归属材料 | 补两条 CTSH 正确公司、正确期间的电话会原文；等待 AlphaEngine 24h 配额窗口。不得降低 gate 或恢复 dismissed 证据 | 四个要求期间均有 archived、company-bound transcript；重跑 gate 后通过。8 条 dismissed 记录继续被排除且保留审计链 |
| P1 | 从 Deep Insight/公司模型推进到 Investment Memo，并经 owner 人审进入 active coverage | **实际缺失** | `src/dalton_core/research_playbook.py:46-51`、`mission_stage.py:53`、`mission_deliverable.py:47` 只有词表/模板/检查点；全库对 `investment_memo` 的生产代码搜索只命中这些合同和测试。`src/dalton_core/metric_base.py:115-121` 的 `investment_memo` spine 仍为空 | 建 memo draft/verify/coordinator，读取 dossier、DebateMap、forecast、valuation、events、12 问和 anti-thesis；用现有 `MissionDeliverableVersion`，保持 human checkpoint | 至少 ACN 生成一份数字全可回指的 memo draft；12 个 Key Questions 与完整 anti-thesis 均有证据/未知项；独立 verifier 通过；owner 一次裁决写入 stage ledger，随后才进入 active coverage |
| P1 | “交付时才做”的 Excel 公式模型 | **实际缺失** | `analyst-onboarding...:181-186` 明确 P13-M5；`docs/PROJECT_STATUS.md:40` 仍列“Excel 导出继续后排”。源码仅有 prior Excel 的 `openpyxl` **读取**（`prior_model_import.py`、`prior_research_core.py`），没有 forecast/model 的 xlsx 公式导出器 | 建只读投影到 XLSX 的 deterministic exporter；公式引用而非硬编码预测值，绑定 model/spec/forecast 版本 hash | ACN 导出文件可重算来源 manifest；历史 actual、assumption、result 分层；关键公式可由 LibreOffice/Excel 重算并与 authority Decimal 值一致；改一个 assumption 后下游公式变化且 actual 不变 |
| P1 | 行情、估值与事件进入真实覆盖闭环 | **实现但激活/产物证据不足** | `market_price.py`、`valuation_snapshot.py`、`catalyst_calendar.py`、`tracking_lane_cli.py` 已有生产路径；但当前正式 product acceptance 只验 dossier/debate/event 15 项，未证明五家公司 ≥3 年日线、S6 或一次真实 ≥3% 异动闭环 | 把蓝图 Phase 11 的质量验收加入 deployment acceptance，而不是以 schema/lane 存在代替 | 五家公司各 ≥3 年日线；ACN S6 同时显示 consensus 与估值分位并可回指；至少一条真实或固定历史 ≥3% abnormal move 生成 ResearchEvent、judgement、driver/thesis 映射 |
| P1 | SEC 8-K 成为 discovery 计划的一部分 | **完整候选链存在，owner 尚未激活** | `sec-8k-owner-install-packet-2026-09-10.md:3-11`：prepare/apply、receipt、selector 都已完成且 live target 均 absent；`continuous-wave4-release...:41` 仍列 owner gate | owner 审批并 apply 候选，再重装/渲染 LaunchAgent；保持原 10-K spec、五家公司和预算 | active plan 新版本 `prior_hash` 连续，仅追加 8-K spec；重复 apply 幂等；首次 discovery 可归档真实 8-K，空结果明确为空，不改变 10-K 行为 |
| P1 | HK 回购低成本日缓存 | **实现但特意未种子/未授权** | `f13-hk-shared-acquisition-2026-09-10.md:31-44` 明确 `daily_buyback_tape` 是 deliberately unseeded proposal | owner 独立审阅 capability/governance 后安装；不应借已有 HK operation 权限隐式启用 | 两家公司共享一次日拉取，raw artifact/invocation 可重放；按 company/day 隔离；缓存损坏 fail closed；未批准时零网络调用 |
| P1 | 所有 producer/verifier 路由可用且家族独立 | **实现管理面，仍有一个 owner 元数据 gate** | `dynamic-broker-catalog-sync-2026-09-10.md:5-13` 与 `live-copy-model-route-acceptance-2026-09-10.md:62-73`：DeepSeek Flash 路由存在但 family 为 `unclassified:deepseek`，作为独立核验链会零调用拒绝；Cockpit 已有 route-bound declaration UI（`cockpit_control.html:690-695`） | owner 核对当前 provider/model 后发布 family 声明；另决定 retired thesis-impact verifier pin。不要按 alias 猜 family | 同一实际 catalog route 上声明 hash 绑定；DeepSeek producer 后 verifier 能选择不同 family；同 family/unknown 仍在预算前零调用拒绝；catalog alias 再变化时旧声明不继承 |
| P2 | PM 问答达到“10 个真实问题 ≥8 可用”，需要时能补搜 | **功能存在，质量验收未完成** | `answer_routing.py:1465` 已有 `answer_after_refresh`；Cockpit 有明确 dispatch 流程；`research_quality_score.py:75-77` 和 `analyst_journal.py:46-58` 支持 ask rubric 与 PM 五值反馈。没有找到这 10 题真实 PM 验收结果 | 用五家公司设计固定问题集，覆盖数字、forecast、dossier、debate、事件和明确不知道；记录 PM 反馈，并检查 refresh 真正形成受预算 WorkOrder 和新证据 | 10 个真实问题至少 8 个 `useful`；所有数字有引用；不足时明确 unknown/needs_more_evidence；一次 `answer_after_refresh` 从确认、执行到新回答全链闭合且预算入账 |
| P2 | 周会与反馈回路 | **主体存在，部分通知断接/产品验收不足** | weekly brief authority、Discord bridge、feedback ledger 已存在；质量对象支持 weekly brief（`research_quality_score.py:75-77`）。但 `model_selection.py:130-148` 的 fallback notice delivery 仍是 no-op，只在 Cockpit；未见当前版本 ≥3 期 brief 的 rubric/PM 验收证据 | 对现有最近三期 brief 跑 rubric并收 owner 反馈；若 owner 仍要求主动模型故障通知，再把已留 seam 接到已治理 delivery，而不是新造通知账本 | ≥3 期 brief 有 score 与 human feedback；一次 `needs_more_evidence` 或 `revise` 进入下轮 drafting context；fallback notice 在约定渠道只投递一次并有 receipt（若 owner 选择继续只用 Cockpit，则更新期待为已满足） |
| P2 | “所有预算可配置”在部署后仍成立 | **代码已实现，等待最终冻结/部署验收** | `mission-v14-product-acceptance-progress...:26-34` 记录 call/run、pool、planner、extraction、event、thesis overlay 已接线，但当时新 full-suite/frozen acceptance 尚未开始；这比 09-09 蓝图更晚，应以最终 release 报告为准 | 完成统一 full suite、wheel、临时 live-copy 演练；部署后从 Cockpit 修改各代表 purpose，确认重装不擦除 | 修改 budget 会改变 WorkOrder identity、reservation/token/timeout/cost 同步；非法配置拒绝；重装与切 tier 后四类 override 逐字保留；mission pool 总和不超过日上限 |
| P3 | 模型 fallback 主动通知 | **存在明确断接** | `model_selection.py:130-148` 明说 `notice_delivery()` 当前 no-op，渠道固定为 cockpit | 待 owner 选择 Discord/Feishu 后，将该 seam 接现有 delivery/receipt，不改 fallback authority | 同一 `(model,purpose)` 仅一条通知；成功有 delivery receipt；失败可重试；Cockpit ledger 始终是权威来源 |

## 已兑现、不要重复派发

以下是旧报告最容易造成重复建设的项目：

- C1 事件日历、C2 四池预算、C3 Constitution method 消费、C4 Reflection 已有实现；旧报告中的“无”是 09-09 快照。
- D1 ResearchTask 已建在 `BoundedPlannerLoop` 上，`answer_after_refresh` 也已接真实确认/dispatch。
- D2 producer/verifier 独立性已推广到 dossier、DebateMap、ZeroBase、事件、业绩前瞻与对账；unknown family fail closed。
- D3 已由 ADR-0009 显式退役旧 Perception 路径，不能再建第二套事件平面。Agenda 若复活仍需对齐 stale policy pin。
- D5 gate reopen 已进入 append-only stage ledger 与 human checkpoint。
- Claim aspect/as-of/importance、数字 authority、三表/规格 join、driver forecast、sensitivity、consensus bridge、guidance profile、prior research 入职均已实现。

## 文档自身需要纠正的地方

`docs/PROJECT_STATUS.md` 顶部出现同页冲突：第 7 行说 mission v14 已签，第 11 行仍说“现在只需 owner 签署”，
第 23 行又沿用部署前措辞。阅读顺序规则虽然说顶部覆盖历史，但这三句都在顶部。下一次状态更新应以
`mission-v14-product-acceptance-progress-2026-09-10.md:3-5` 为准：mission 已签，当前缺的是部署 post-signature
修复及真实产品验收。

09-09 的八周蓝图适合作为目标定义，不再适合作为实现状态表。后续状态报告应对每项固定使用四态：
`implemented`、`implemented_unactivated`、`connected_no_product_acceptance`、`missing`，并把 owner gate 与代码缺口
分列。这样不会再把“有 authority 但 live 0 产品”写成完成，也不会把已合入的 Constitution/Reflection/预算池重复派发。

## 建议执行顺序

1. 冻结、全量验证并部署 mission-v14 后续修复；完成 15 项真实产品验收，CTSH 保持独立的 evidence gate。
2. 同批完成 DeepSeek route-bound family 声明等必要运行 gate；SEC 8-K 与 HK 日缓存分别走 owner 审批，不捆绑 mission 重签。
3. 用真实五家公司补 Phase 11、ask 与 weekly brief 的产品质量验收，先证明系统产物可用。
4. 开发 Investment Memo 闭环；这是从已有深度研究到 active coverage 的最大真实代码缺口。
5. 开发 deterministic Excel 公式导出；保持它是交付层，不反向成为 actual authority。
6. 最后按 owner 渠道选择接主动通知，并用真实 PM 反馈驱动下一轮质量修复。
