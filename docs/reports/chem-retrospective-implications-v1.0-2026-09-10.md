# Chem（原版 Dalton agent）复盘对现在这套 practice 的启示

日期：2026-09-10
输入：`docs/external/Chem项目复盘报告.md`（owner 提供，审阅区间 2026-07-18 至 09-10）、`docs/external/chem-review-20260910/` 支撑材料、`docs/external/thoughts-on-ai-for-hedge-fund-2026-08-25.md`
方法：主报告由主 agent 通读；支撑材料的数字由 Opus 5 subagent 提炼（附录 A）；逐条对照当前 Dalton Research Agent OS 的 practice
立场：不是照着 Chem 改。Chem 是 OpenClaw runtime + cron + 提示词 + SQLite 台账的一代实现；现在这套是 append-only authority + 哈希 + lane registry + 独立 review 的二代实现。二代在结构上明显更好，但 Chem 有几件事做对了、有几个坑我们正在走向，值得说清楚。

---

## 0. 一句话

Chem 54 天里最有价值的不是文件数量，而是三件事：**按商业模式换框架、把事实 / 市场代理 / 卖方估计 / 自有假设分开、把事故变成机器检查**。它最大的失败也只有一件：**产文件的速度快于形成投资能力的速度**——92 条决定 89 条 NO_CHANGE，预测槽位建了两个月一直空着，Gate 状态三处说法不一。我们这套在结构上把它的大部分坑都堵上了，但在「先跑出真实认知再扩系统」这件事上，我们正在重复它的错误：main 上 5,300 项测试、40 多条 lane，live 里一份档案、一张 DebateMap 都还没有。

## 1. Chem 做对了、我们该学的

| Chem 的做法 | 出处 | 我们现在 | 建议 |
| --- | --- | --- | --- |
| **不同商业模式用不同框架**：万华按商品周期 / 成本 / 资本扩张，Linde 按合同 / 网络密度 / 项目回报，BASF 按多元化资本周期 / 组合调整 / FCF | §7.1 | 档案有 `industry_classification`（五类），但建模规格与 driver 是逐公司的通用形状，没有按分类给的 driver 模板 | **按分类给 driver 模板**：commodity_cycle → 价差 / 开工率 / 成本曲线位置；capital_cycle → 资本开支 / 回报 / 产能周期；compounder → 定价 / 留存 / 单位经济；structural_growth → 渗透 / TAM；turnaround → 里程碑 / 现金跑道。规格 lane 起草时先按分类选模板再填 basis_concept；DebateMap 与 dossier 的 `demand_drivers` 也按模板组织 |
| **四类数字分开且写明口径理由**：未披露分部成本不强行分摊；MDI–纯苯粗价差不当装置现金利润；backlog 不当收入；政策毛收益不进基准 | §7.2 | 有 grade / tier（filing、management、sell_side、internal_prior、crowd），有 `estimate/actual`，但没有「**市场代理**」这一类——价差、挂牌价、期货连续代理这些「与公司实现值有距离的公开量」在我们这里会被当成普通 observation | 新增证据种类 `market_proxy`（带「与公司实现值的距离」说明），进 claim index 词表与判断层提示；forecast 假设引用 market_proxy 时必须写 `proxy_gap` 理由。这是 owner 8-25 思考里「不靠推测填缺口」的落地 |
| **月度 zero-base 复盘**：从零仓位重问「现在要不要建立观点」 | §4.5 | 有周度 `ResearchCycleReflection`（时间花在哪）、有事件驱动的 `ThesisReflection`（为什么错），没有「从零重问」 | 加 `ZeroBaseReview`（月度或财报后）：判断层对每家覆盖公司重问四件事——如果今天第一次看这家公司会不会建观点、现有 thesis 哪条会被重新写、哪条 debate 已经不重要、下一个验证点是什么。产出走 deliverable + 候选，人裁决 |
| **事故 → 机器检查**：负价差百分比错、行情断日、公式链重复派单，都变成 16 项测试 | §7.5 | 我们也这么做（今天的 socket 期限、括号丢失、CIK 补零都变成了测试与规则） | 写成硬规则：每次 live 事故必须产出一条测试或一条 policy 检查，记在 PROJECT_STATUS 的事故段 |
| **失败纪律**：Task 62 三次源不可用后保持 failed，不补假报告 | §6 例 6 | 一致：refuse-whole、never repaired、`unavailable` with reason 遍布各 lane | 保持；但见 §2 第 5 条的反面 |
| **持久队列 + 无到期任务就跳过**：dispatcher 168 次调度 148 次跳过（88.1%），只在有事时叫模型 | §7.3 | tick + 每 lane 有界 child + 池；第 36 周全 mission 只花 0.026 美元 | 一致；tick 账本（C2）现在能算出空转比例，Chem 当年算不出 |
| **报告只从唯一状态源生成** | §8.5 教训 | stage-ladder 刚合入：阶段状态跨版本折叠，四处读者共用一个 fold | 一致，且是 Chem 没做到的 |

## 2. Chem 踩过、我们已经堵上的坑

| Chem 的坑 | 我们的对应机制 | 状态 |
| --- | --- | --- |
| README 说的当前模型与实际文件不符（490 vs 1,908 个公式的两份 xlsx） | 每个产出是 append-only 版本链 + 指针；Excel 只在交付时导出公式 | 已堵 |
| Gate 状态在 Deep Insight、RAMP、注册表、周报四处矛盾 | 阶段账本 + 跨版本 fold + 重开记录；Deep Insight Gate 是人裁决的 checkpoint | 已堵（今天合入） |
| 「附来源」不等于数字准确（ELWIS 重复日期列误读；BASF 分部 EBITDA 抄错） | 数字逐字核对（`verify_numeric_candidate`）、报表行绑 accession、跨期勾稽在建模输入表里 | 已堵大半；跨分部加总校验还没有 |
| 结构检查全过但经济逻辑错（负 IRR 被截成 0；单批与累计 EPS 混用） | M2 用 Decimal、refuse-whole、`unavailable` 而非猜；P13-M3 敏感性带历史峰谷带 | 部分：我们没有 IRR 类求根器，但**缺「经济不变量」检查**——见 §3 |
| 日报在发送成功前就 `mark-reported`，失败会漏报 | 投递线还没建；已合入的通知先落 cockpit 并 append-only 记录 | 建投递线时必须遵守：正文与事件集合一起持久化，投递确认后才标已报 |
| 「健康 OK」与研究有缺口并存 | cockpit lane 面板区分 ungranted / unconfigured / unapproved / idle；行业框架有 gap 清单；抽取 backlog reader | 部分：三者在三个页面，见 §3 |
| 没有成本与效果账 | 日账本 + 四池 + tick 账本 + 周度 reflection | 已堵 |
| 早期长任务无分步落盘，中断即重来 | 每 lane 一次一个有界 child，ticket + summary.json，WorkOrder 幂等回放 | 已堵 |

## 3. 我们正在走向的 Chem 式错误

**3.1 系统建得比认知快。** Chem 8 月 12 日上线 Coverage OS 时已经有两份 Initial Screen、三份 Deep Insight、一个万华模型；我们今天有 44 条合入的线，live 上却因为 mission 版本没授权，档案、DebateMap、判断层、consensus 一条也没跑过。Chem 的 89/92 NO_CHANGE 是「研究没转化成判断」；我们的风险是「判断层建好了但没有输入」。**下一周的优先级应当是部署 + 授权 + 让五家公司各出第一版档案与 DebateMap，而不是再加切片。** 蓝图里剩下的（周报投递、Excel 导出、P15b）都不如让判断层真正跑一轮重要。

**3.2 缺「经济不变量」检查。** Chem 的 42 项 Checks 全 OK，Linde 模型仍然把亏损情景算成 0%。我们的 M2 也只有结构与算术校验。应在 M2 / M3 加一层不需要模型的不变量：结果符号与 driver 方向一致（收入涨、成本占比不变，营业利润不该跌）、每个假设落在历史带内或显式标 `outside_band` 并写理由、率类不得越过 [0,1]、分部之和等于合并、单批与累计不得混用（P14f 的 actual 与 estimate 分格已经是这个思路）。不变量失败 = `unavailable` + 理由，不是发布。

**3.3 「不动」要能被评价。** Chem 说得对：有条件的不改观点是有效产出，但它没法证明这些 NO_CHANGE 是对的。我们的判断层记 `no_change` + 理由，但 Q2 的 reflection 还没有「事后验证」指标。加两个：对每条 `no_change`，若其后 N 日股价相对行业篮子持续背离超过阈值，标为「当时该动没动」候选；对每条 `revise`，若后续 actual 证实方向，标为「动对了」。这不是绩效考核，是 Chem 缺的那本「观点—结果台账」。

**3.4 依赖类失败要归类处理，不能靠固定次数重试。** Task 62 卡在 AlphaEngine 桌面模块页三次失败后永久悬置。我们 P14e 刚把 writer RPC 失败改成可恢复的 hold，但各 lane 的失败预算仍是「N 次后 held」。应统一失败分类：`dependency_unavailable`（源 / 桌面会话 / 配额）→ 进 cockpit 运维待办并按依赖恢复后自动重试；`content_refused` → 终态；`transient` → 有界重试。抽取吞吐报告里那 6/14 永久不可读的 review 就是这一类。

**3.5 三张图要并排。** Chem §8.8：运行状态、来源缺口、待处理失败、产物验收要同时展示。我们有 lane 面板、行业框架 gap 清单、抽取 backlog、质量分数，但分散在四处。cockpit 概览页加一行「四格」：lane 状态 / 缺口数 / 悬置失败数 / 上周产物验收，每格点开到各自页面。

## 4. Chem 做了、我们不该照抄的

- **cron + 提示词编排**：Chem 的每个环节是一段提示词 + 一个 cron；状态在提示词与台账之间靠约定同步，这是 Gate 四处矛盾、日报先标已报的根因。我们用 authority + registry + 治理 op，不回头。
- **模型在每个环节里做「判断 + 写入」**：Chem 的 daily-report 既判断又写台账；我们把「机制」与「判断」分开，authority 永不自版本化，这是 owner 9 月 9 日定的原则，比 Chem 更对。
- **以文件存在性作完成标准**：Chem 80 项 done = 80 个非空文件。我们的验收是 PM 可用性、独立来源数、rubric 分数。保持。
- **一个 agent 自证**（写代码、写测试、写报告同一人）：Chem 的 §2 已经在替自己划证据边界。我们今天每条线一个作者一个 reviewer，共抓出 30 多个 blocker，这条差异是实打实的。

## 5. 直接落地的清单

| # | 事项 | 去处 | 大小 |
| --- | --- | --- | --- |
| 1 | 按 `industry_classification` 给 driver 模板，规格 / 档案 / DebateMap 共用 | 新切片 W3「framework-by-classification」 | 中 |
| 2 | `market_proxy` 证据种类 + `proxy_gap` 理由 | claim index 词表 + forecast 假设校验 | 小 |
| 3 | `ZeroBaseReview` 月度 / 财报后 | 判断层 + deliverable | 中 |
| 4 | M2 / M3 经济不变量层 | `model_forecast_driver` 校验 | 中 |
| 5 | `no_change` / `revise` 的事后验证指标 | Q2 reflection | 小 |
| 6 | 失败分类：dependency / content / transient | lane 公共层（`lane_child_launcher` + 各 lane 的 failure budget） | 中 |
| 7 | cockpit 概览「四格」 | INT 后续 | 小 |
| 8 | 投递线建成时：正文与事件集持久化、确认后再标已报 | P15e（owner 搁置到最后） | 记入规格 |
| 9 | 规则：每次 live 事故必须产出一条测试或 policy 检查 | 计划文档规则 14 | 已写 |
| 10 | **部署 + mission 版本授权 + 五家第一版档案与 DebateMap 先于任何新切片** | owner runbook v2.0 | 决策 |

## 附录 A：支撑材料数字（subagent 提炼）

（待提炼结果落地后补入：cron 任务与频率、88 项任务的类型分布、92 条决定的词表、176 份产物的版本 / 覆盖分布、Linde 复算细节、owner 8-25 思考的原则清单。）
