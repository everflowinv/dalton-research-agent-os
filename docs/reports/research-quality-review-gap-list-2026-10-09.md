# 产物质量评审（2026-10-09）缺口清单与后续批次

历史技术评审：保留当时结论与局限；当前运行状态见 [项目状态](../PROJECT_STATUS.md)。原始操作证据私有保存。

> 只读核对：主报告与附件 01–05 逐行读完，11 个 CSV/JSON 读表头并脚本核对关键统计。状态记号：✔ 已合并或在跑批次覆盖；◐ 部分；▷ 只在计划批次（C/D/E/F/G/SV/HV/TM/H）；✘ 无批次覆盖。规则统一对所有公司、所有 workspace 生效，测试用合成公司（`tests/synthetic_company.py`），不用真实公司夹具冒充通用规则。

## 核实过的附件统计
- outcome checks：1906 unavailable / 482 pending / 227 held；Hyperscalers 59 个全部 unavailable；2674 个里 2673 个是 no_change 类。
- 事件研判 6275 条，6260 条 NO_CHANGE；只有 15 条证据来源是 primary_filing；792 条引用了已退休 claim。
- MSFT 污染 22 条 claim 全部未退休；直接引用：初筛 10、档案 13（加生成输入共 22）、分歧图 9、场景 1+1。
- META 2028Q2（收入 1019.9、营业利润 −313.0、FCF +185.9）计算过程保存在当时的逐单元审计表中（原件已私有归档）。
- 40 个回答绑定全部是「季度收入同比」模板题。

## 覆盖矩阵（摘要）
- **P0**：MSFT/Oracle ◐ al（`product_rewrite_flags` 只认 issuer_misattribution 一种退休原因；`PRODUCT_TABLES` 只覆盖初筛/档案/分歧图/场景，缺 gate、框架、前瞻、事件汇总、零基、thesis；重写应「重新研究」而非重跑提示）；META ◐ al（缺广告量价分解、费用挂驱动、capex 口径含融资租赁、敏感性引擎荒谬值）；GOOGL 净利率代理 ✔ al；GOOGL 分部→集团桥接不变量 ✘（segment_sum 跳过 CorporateNonSegment/AllOther）。
- **模型**：方法/链/三表 ✔ M；IBM 税基、CTSH 缩股 ✔ al；CTSH 只预测四季 ✘；无季节性 ✘；spec 驱动未进公式无检测 ✘；折旧/利息未强制滚动表 ◐；capex 口径 ◐；非核心项阻断核心营业利润 ✘；年度投影分类与整年历史缺失 ✘；历史 EPS 回填 ◐；模型假设无证据引用 ✘；敏感性绑旧版本/荒谬值 ✘；预测线 60/64 绑旧版本、单位混用 ✘；不变量判定绑旧版本、not_applicable 当通过 ✘；利润桥缺行阻断下游 ◐；每股序列（TTM EPS 重建、GOOGL 股息、DXC 稀释股数）✘。
- **估值/共识/收益桥**：历史股数、EV 债务、前瞻 P/E、DXC FCF 桥 ✔ B；分位有效样本展示与长历史 ◐；核心利润口径 P/E ✘；共识 adjusted/GAAP 与 NTM ◐；实现腿 look-ahead、股息近似、各腿窗口不一致 ✘；卖方腿 5.00% 疑似默认 ✘（疑点）；无模型/无估值须显式阻断原因 ✘。
- **场景**：无 EPS/倍数/回报 ▷ SV；触发用股价或自身判断（23 条）✘；owner 定性条件未编码（19 条）✘；非互斥穷尽 ✘；模板先验 ✘；截止日不一 ▷ SV；绑旧模型 ✘；有场景无模型 ✘。
- **事件/前瞻/催化**：`〔数字见研判记录〕` 占位（约 253 处）✘；主事件无 ID（56/108）◐ F；未读正文判 NO_CHANGE ▷ F；754 条 research 动作无后续 ▷ E（另缺决策-动作一致性 ✘）；无 thesis/模型的 NO_CHANGE 空判断 ✘；旧事件重判不标原始日期 ✘；价格事件缺 SPY/行业基准 ◐ B；前瞻共识绑定 ✔ B；指引三态与并排 ◐ B/▷ D/F；DXC 前瞻版本绑定 ✘；前瞻不滚动 ✘；催化日历过期/引用 0/estimated/日期冲突/财期混淆 ✘。
- **深度产物**：gate Q7/Q9/Q10 与置信度校准 ✘；gate 引用超精度数字 ◐ am；旧 gate 遗留 ✘；EPAM 三处分类不一致 ✘；档案用 2023/2024 分部数据 ✘；Hyperscalers supply_and_cost 全未映射、框架泛化、驱动包通用 ✘；AMZN 档案偏 AWS ✘；分歧图 our_position 全 none_yet ▷ TM/D；框架跨公司用 2024Q3 ✘；驱动包冻结 8 月、周报「新增=0」✘；初筛 S6 空且不回填 ▷ C/D；IBM 空节通过检查 ▷ D/H；「公司未披露」vs「系统没取到」▷ D/E。
- **推理纪律**（全 ✘）：TCV/RPO 当即期收入；单一券商当共识；固定滞后无队列分析；contract compounder 无反例检查；管理层「赢份额」当事实；调研意愿当兑现；产品改善直推 ROI；供应链与股价因果混写。
- **闭环**：零基过期 ✘；正式 thesis 无 claim_refs/量化证伪/期限 ◐ TM；同一瞬间 accept、reject 语义混用 ✘；outcome checks 无预登记 ✘；无 model/forecast reconciliation 记录 ✘（`forecast_reconciliation.py` 只对旧 forecast_line 且需授权，从未产出）；评分缺维度、不绑版本、未接车道 ▷ H；周报基底冻结、Hyperscalers 无周报 ✘；反思清单无负责人期限 ✘；IBM conviction 把季节性差异当 alpha ◐ SV。
- **引用/发布/底层**：引用退休 claim（初筛 2/10/4/1/5、分歧图 27/30、框架 15/20=100%、gate 11/9/9/1、事件 792）✘；中文附件源节为空仍为空 ✘；后台问题去重与双状态 ✘（DXC NPS ~50 条、AMZN ~60 条重复，59 条万华旧题 open）；档案裸数字转 backlog ▷ D；31 个 bounded 结果无正式 claim/否定 ▷ E；66 个 document-research 候选停在 staged ✘；AMZN「待人工抽取」/重读需 owner 授权/form type 靠猜/中文别名假阴性 ◐。

## 后续批次（系统规则）
- **an 跨产物一致性与版本绑定**：所有派生对象记录上游版本，旧绑定标 `stale_binding` 或阈值重建；不变量按版本门禁，not_applicable/checked=0 显示「未覆盖」；跨产物语义冲突检测（分类、variant/反论点、共识来源、置信度字段 vs 正文、零基 vs 催化、营业利润 vs 收益桥净利润、单位币种）写 conflicts 表并作重起草触发；引用生命周期推广到所有退休原因与产品类型（含 gate、框架、前瞻、事件、零基、thesis），有效引用占比低于阈值标 needs_rewrite；CIK 补零到 10 位。新建 `product_consistency.py`。
- **ao 新鲜度与生命周期**：催化条目过期必须转 occurred/missed/unconfirmed，引用 0 不发布，estimated 不驱动重判；零基 next_verification 过期自动 stale 并排重审；前瞻事件后自动滚动；事件带原始事件日与信息可得日并去重；定量段落必须用最新已申报期或标「历史对照」；分部命名跟随最新结构。
- **ap 正文实质完整性与发布门**：required_sections 检查实质，空节不得完成/发布/润色，源节空则中文附件拒绝生成；禁止间接占位符；每个数字带指标/期间/单位/来源，按量级取整；gate 每题回答标准（Q7 ≥5 年归因序列、Q9 高频指标、Q10 完整链且模型过不变量），不满足保持 unanswered 并发 MaterialRequest，置信度校准；缺口分 `company_not_disclosed` / `not_acquired`；旧 gate 遗留标 superseded。
- **aq 行业原型框架与新 workspace 引导**：框架为档案每方面（demand/supply_and_cost/pricing/cash）各提供可计量因果链，`causal_chain_unmapped` 触发框架修复；行业原型库（重资本算力、广告、零售/履约、IT 服务、消费型云）；workspace 初始化必须按原型具体化，否则标「未专业化」；驱动/证据包声明冻结范围与日期，周报按全部输入算新增；档案分部覆盖与权重相称；跨公司对照同口径同期间。
- **ar 模型第二波**：预测期 ≥8 季或到 FY+1 年末；季节性方法；spec 驱动未进公式即「驱动计划未执行」；分部桥不变量（含 CorporateNonSegment/AllOther，按身份映射禁按大小推断）；非核心项隔离不阻断核心营业利润；折旧强制资产滚动、利息强制债务×利率；capex/FCF 对齐公司定义；年度汇总四类标注；历史 EPS 读申报；模型假设引用证据段落；敏感性联动与量级检查、差异先过季节性/口径/新鲜度；每股序列重建；P/E 双口径、分位展示有效样本与窗口；收益桥 point-in-time 与统一窗口；每家覆盖公司必须有模型或显式阻断原因。
- **as 研究推理规则 lint**（扩展 am 的 lint，进入 D 的 verifier 与 H 的评分）：TCV/RPO/backlog 不当即期收入；「共识」必须引用共识 authority；固定滞后需队列证据；分类标签附反例检查；管理层「赢份额」标为公司观点；调研意愿不等于兑现；价格归因须有基准；产品改善推 ROI 需增量利润/资本成本；美元收入不代替本币有机增长，并购与汇率拆开。
- **at 研究问题与产出生命周期**：问题按规范键去重，改写只产新版本；单一状态 authority；answered 须满足回答标准且文档类型匹配期间；超范围题归档、系统状态题转运维；staged 候选 SLA 终态；无新材料不重扫，重复 source_unavailable 转 MaterialRequest；form type 取 SEC 元数据，跟进引用并入附件，别名/中文名相关性自动重跑无需 owner 授权。
- **au 闭环**：对账覆盖预测模型（8-K/10-Q 到来即对每个预测单元算误差并归因到假设与方法，重大偏差触发阈值重起草）；outcome check 预登记方向/阈值/基准/信息可得日，unavailable 单独统计；正式 thesis 必须有 claim_refs/量化证伪/期限；决策治理（一个终态、不同瞬间、无需修订与拒绝分开、通知去重、未附理由标明）；周报对账段与基底声明，每个 workspace 可出周报；反思清单负责人/期限/关闭证据，检查点超 14 天升级。
- **av 场景条件规则**（SV 前置）：条件词表禁止引用系统自身 judgement；价格阈值只做监测，价格位由 EPS×倍数推导；owner 定性条件编码为来源/时间/判定；同一指标三场景阈值由同一组有序切点生成（互斥穷尽）；概率写明依据、模板先验标出；统一 12 个月窗口，基准绑模型版本，现金条件用公司定义 FCF；无模型无估值不出场景，缺失显示「缺失」。
- **对 F/H/SV 的补充验收**：F 决策与动作一致、未读正文只能「证据不足、安排读取」并阻断重复 NO_CHANGE、无基准标「无基准」、分组链接主事件、价格事件对照 SPY/行业；H 评分加 issuer 归属/actual-estimate/分部桥/一次性正常化/现金桥/跨产物一致性/实质完整性并绑版本接车道；SV 依赖 ar 与长历史（5–10 年价格与 EPS），否则 P20/P80 不可信。

## 与 owner 决定或在跑批次的矛盾（协调者决定见下）
1. 观点与观察价 vs 证据标准：估值链 unavailable 时观察价怎么写。**决定**：不出价，写「观察价：暂无法给出（原因）」并列一致预期与卖方目标价区间作参照；评分要求必须有显式原因。
2. 收益桥严格度：al 对所有缺净利润公司改 unavailable，比评审更严。**决定**：接受；上线后 IT 服务几家驱动腿会变 unavailable，属于诚实退步，M 的 EPS 链补上后恢复。
3. SV 的分位深度不足。**决定**：B/ar 补 5–10 年价格与 EPS 历史（价格序列扩到 10 年；EPS 取 XBRL company-facts），SV 在此之后落地。
4. 重写节奏：issuer 错归属属于立即触发的阈值，且重写前必须先通过材料获取服务取得官方替代材料（E）。**决定**：采纳。
5. 存量人工门：AMZN「待人工抽取」、重读「需 owner 授权」等遗留门要回溯清理。**决定**：并入 at。
6. 「thesis_superseded：无需重判，仅归档」与「必需材料变了自动 reopen」冲突。**决定**：并入 ao/TM，以自动 reopen 为准。
7. 评审自身：Hyperscalers 零基审查「0 份」与 artifact-manifest 中四份不符。记录。
8. am 不能成为空节发布通道。**决定**：ap 第 1 条为准。
9. Oracle 报告里的 CIO 调研确实谈 Microsoft，不得误退休。**决定**：al 加专门验收用例。

## 无法从附件核实的评审主张
外部官方数据（MSFT FY26Q4、META 一次性费用与量价、GOOGL 浮盈与权益融资、DXC FCF 分项、ACN 日期）；IBM 中文附件为空；「累计 10,984 个版本」出处；占位符逐行计数；MSFT 利润桥评语自相矛盾；IBM conviction 推断；AMZN/MSFT form type；新模型是否无判定发布；阅读代理疑点（expected_consensus 腿疑似目标价上行+股息率、CTSH/ACN 卖方腿恰好 5.00%、EPAM 敏感性错位一季、META 敏感性绑旧共识）。
