# 场景、事件、前瞻与研究闭环逐项评估

历史技术评审：保留当时结论与局限；当前运行状态见 [项目状态](../../PROJECT_STATUS.md)。原始操作证据私有保存。

[返回主报告](../research-artifact-quality-review-2026-10-09.md)

## 八套、二十四条场景：每个 trigger／invalidator

概率和为 1 是结构正确，不代表概率有统计校准。各场景缺对应 EPS、倍数、回报与 payoff，暂只能当监测清单；同一指标的条件未构成互斥穷尽分区，股价与自身 judgement 不是独立基本面验证。

### ACN 场景套

环境：legacy；对象：`company-scenario-set:0fd3d022fb1a61212eccc1d92888dca3`；生成：2026-10-07T09:56:18.575891+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 订单温和、增速维持中个位数，AI 兑现偏慢，p=0.50

环境：legacy；版本：`company-scenario-version:f6e19d4a331e3d379c9761addbb3b341`；生成：2026-10-07T09:56:18.575891+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

叙事：可选咨询仍偏弱，企业 IT 预算没有扩张，只是重新分配；AI 重塑类大单和托管服务托住订单，但大型托管交易推迟、转化偏慢，营收同比维持在中个位数附近。调整口径经营利润率基本持平。投资者日只给出定性说法，市场对 AI 兑现延后的担忧有所缓和但没有消除，空头持仓缓慢回落，估值在低位徘徊。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 单季营收超过 FY26 第三季度水平，规模继续温和增长 | concept:us-gaap:Revenues | level | gt | 18718144000 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t2 | 投资者日只给出 AI 效率侵蚀与增量需求的定性说法，没有量化拆分 | None | level | None | None | owner | 2026-10-31 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 营收同比跌破 2%，增速明显失速 | concept:us-gaap:Revenues | yoy_pct | lt | 2 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| invalidators | i2 | 出现论点被研判为失效的事件 | judgement:THESIS_BROKEN | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |


#### bull AI 增量需求兑现，订单拐点带动增速和估值修复，p=0.20

环境：legacy；版本：`company-scenario-version:662f3aa2b59ca10b1098146e2f0fab4a`；生成：2026-10-07T09:56:18.575891+00:00；正文 refs／claim_refs 引用 1 条，其中检查时退休 0 条。

叙事：投资者日讲清 AI 增量需求，FY26 下半年获批的企业 AI 项目开始形成咨询收入；新签订单转为上升，全年指引下限上调，推迟的大型托管交易在 FY27 签下。营收同比加速到 7% 以上，交付节省留在公司内部，经营利润增速快于营收。空头回补加上估值修复，股价明显走高。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 营收同比加速到 7% 以上 | concept:us-gaap:Revenues | yoy_pct | gt | 7 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t2 | 经营利润同比增速超过 9% | concept:us-gaap:OperatingIncomeLoss | yoy_pct | gt | 9 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t3 | 股价修复到 230 美元以上 | price:close | level | gt | 230 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t4 | 出现强化论点的事件研判 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| invalidators | i1 | 营收同比回落到 4% 以下 | concept:us-gaap:Revenues | yoy_pct | lt | 4 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |


#### bear AI 通缩侵蚀传统计费，增速和利润同时下滑，p=0.30

环境：legacy；版本：`company-scenario-version:492f7df81eee2d1223cf2758bcbf642c`；生成：2026-10-07T09:56:18.575891+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：GenAI 蚕食利润率更高的传统按人头计费业务，AI 咨询收入补不上缺口；地缘不确定性让 EMEA 决策继续拉长，可选支出被优先削减，大型交易继续延后。营收同比降到 3% 以下，经营利润转为负增长；举债进一步压低 FY27 EPS 增速。市场确认“AI 输家”定价，估值继续压缩。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 营收同比降到 3% 以下 | concept:us-gaap:Revenues | yoy_pct | lt | 3 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t2 | 经营利润同比转为负增长 | concept:us-gaap:OperatingIncomeLoss | yoy_pct | lt | 0 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t3 | 股价跌破 165 美元 | price:close | level | lt | 165 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| triggers | t4 | 出现削弱论点的事件研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| invalidators | i1 | 营收同比加速到 7% 以上 | concept:us-gaap:Revenues | yoy_pct | gt | 7 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 基准可能仍绑定 model v7，当前 v8／已发布 Q4需重新对齐。 |
| invalidators | i2 | 投资者日量化拆分 AI 增量需求，并确认 AI 项目开始形成咨询收入 | None | level | None | None | owner | 2026-10-31 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |


### CTSH 场景套

环境：legacy；对象：`company-scenario-set:6509211abb0e0c3f9f75f0cc634a99d9`；生成：2026-10-07T10:10:27.015150+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 大单爬坡托底，可选支出持平，温和增长，p=0.50

环境：legacy；版本：`company-scenario-version:06cf8bbb91d7a800d6937d1d78c2cce8`；生成：2026-10-07T10:10:27.015150+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：可选支出大致持平，与管理层下调后的全年指引中点假设一致。过去一年签下的大型合同继续爬坡，Astreya 从第三季度起全额并表，收入同比保持中个位数增长。Project Leap 和费用管控让营业利润增速略快于收入增速。订单在单季之间会有波动，但过去十二个月的 book-to-bill 仍高于1。AI 项目主要抵消传统可选业务的收缩，暂时不带来净增量。股价维持低估值区间，等待订单数据确认。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | Astreya 全额并表加上大单爬坡，第三季度收入环比增速超过2% | concept:us-gaap:Revenues | qoq_pct | gt | 2 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 利润率继续扩张，营业利润同比增速超过8% | concept:us-gaap:OperatingIncomeLoss | yoy_pct | gt | 8 | sec_actual | 2027-03-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 第三季度财报显示订单恢复增长，管理层维持全年指引中点 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 收入同比增速跌破2%，大单爬坡没能托住收入 | concept:us-gaap:Revenues | yoy_pct | lt | 2 | sec_actual | 2027-03-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 营业利润同比转负 | concept:us-gaap:OperatingIncomeLoss | yoy_pct | lt | 0 | sec_actual | 2027-03-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bull 金融服务带动可选支出回暖，AI 带来净增量，p=0.25

环境：legacy；版本：`company-scenario-version:158062a9d97e06586aee68fef2d9baff`；生成：2026-10-07T10:10:27.015150+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

叙事：金融服务的订单强势扩散到其他行业，可选支出开始回暖。AI 项目从试点进入规模化，带来净增量收入。大单爬坡叠加 Astreya 并表，推动收入加速增长；Project Leap 的节省开始兑现，利润率继续扩张。订单增速重新快于收入增速，自由现金流回升。市场对 AI 价格压力的担忧缓解，卖方陆续上调评级，估值修复。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入同比增速加速到7%以上 | concept:us-gaap:Revenues | yoy_pct | gt | 7 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 营业利润同比增速超过12% | concept:us-gaap:OperatingIncomeLoss | yoy_pct | gt | 12 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 估值修复，股价回到70美元以上 | price:close | level | gt | 70 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t4 | 出现论点被加强的事件研判 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| invalidators | i1 | 收入同比增速跌破3% | concept:us-gaap:Revenues | yoy_pct | lt | 3 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 出现论点被削弱的事件研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |


#### bear 订单下滑成趋势，AI 蚕食原有收入，p=0.25

环境：legacy；版本：`company-scenario-version:de68192416b94e29cee1ebeceb5536ad`；生成：2026-10-07T10:10:27.015150+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

叙事：第二季度的订单下滑成为趋势的开端。可选支出持续疲弱，AI 带来的价格压力和对原有收入的蚕食超过新增项目。大单爬坡放缓，年度合同价值继续下降，收入增速回落到低个位数，利润率扩张停滞。自由现金流迟迟不回升，公司继续用循环信贷支撑回购和分红。财报后股价再次大幅下跌，Citi、Goldman Sachs 担心的动量叙事破裂得到验证。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入同比增速跌破2% | concept:us-gaap:Revenues | yoy_pct | lt | 2 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 营业利润同比转负 | concept:us-gaap:OperatingIncomeLoss | yoy_pct | lt | 0 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 财报后股价再跌，收盘价低于48美元 | price:close | level | lt | 48 | market_price | 2027-03-31 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t4 | 出现论点被削弱的事件研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| invalidators | i1 | 收入同比增速回到6%以上 | concept:us-gaap:Revenues | yoy_pct | gt | 6 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 第三季度订单恢复同比增长，且增速快于收入 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |


### EPAM 场景套

环境：legacy；对象：`company-scenario-set:9c8d0ec6807ca9d1c6e7fc70667ea92c`；生成：2026-10-07T10:20:41.048818+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 收入环比持平，大单推迟到2027年，p=0.50

环境：legacy；版本：`company-scenario-version:b38df66560d195c3042088ae8a014af6`；生成：2026-10-07T10:20:41.048818+00:00；正文 refs／claim_refs 引用 5 条，其中检查时退休 0 条。

叙事：第三、四季度收入大致环比持平，和指引中点一致。AI原生收入继续增长，但只能部分抵消手工测试、前端等按任务计费业务的流失，北美和SaaS客户继续拖累。近十个大单仍在谈判，2026年内签约有限，到2027年上半年才开始贡献显著收入。公司维持利润率目标，靠效率改善让EPS继续同比增长。估值维持在低位，卖方继续观望。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 第三季度收入环比增速回落到1%以下，接近指引中点假设的持平 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | lt | 1 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 收入同比增速从4.53%放缓到4%以下 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | lt | 4 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 第三季度电话会上大单仍未签约，管理层维持2027年上半年开始贡献收入的说法 | None | level | None | None | owner | 2026-11-30 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 收入环比下滑超过3%，跌破指引中点 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | lt | -3 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 收入环比增长超过3%，明显高于持平假设 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | gt | 3 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bull 连续超预期叠加大单签约，估值修复，p=0.20

环境：legacy；版本：`company-scenario-version:1e74df2df47f96c1d30a43083c3b32ee`；生成：2026-10-07T10:20:41.048818+00:00；正文 refs／claim_refs 引用 5 条，其中检查时退休 0 条。

叙事：公司继续超预期并上调全年指引。首批AI主导的大单在2026年末前签约并披露规模，智能体BPO和大型机改造项目开始放量，AI预算抵消传统业务流失的效果增强。收入增速回升，EPS保持高增长。卖方按自己设定的条件改认AI是顺风，NTM市盈率从同业中收缩最多的位置开始修复。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入环比增长超过3%，高于指引中点的持平假设 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | gt | 3 | sec_actual | 2027-05-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 收入同比增速提升到7%以上 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | gt | 7 | sec_actual | 2027-08-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 滚动市盈率从14倍修复到18倍以上 | valuation:trailing_pe | level | gt | 18 | valuation | 2027-08-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t4 | 管理层在电话会上宣布首批大单签约并披露规模，同时上调全年指引 | None | level | None | None | owner | 2027-03-31 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 收入环比转为负增长 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | lt | 0 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 出现论点被削弱的研判事件 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |


#### bear AI替代快于新增，收入下滑、大单再推迟，p=0.30

环境：legacy；版本：`company-scenario-version:729a49c2d6fb9792db4bd7c33a488b76`；生成：2026-10-07T10:20:41.048818+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

叙事：宏观波动和客户预算转移继续压缩传统按任务计费的工作，AI节省的预算兑现慢，增长缺口扩大。北美和SaaS客户继续下滑，问题超出管理层所说的公司自身因素。大单到2026年末仍未签约，2027年上半年的收入时点再次推迟。收入落在指引低端甚至环比下滑，指引再次下调，股价像2025年初那样因不及预期大跌。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入环比下滑超过2%，落到指引低端以下 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | lt | -2 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 摊薄EPS同比转为负增长 | concept:us-gaap:EarningsPerShareDiluted | yoy_pct | lt | 0 | sec_actual | 2027-05-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 收盘价从109美元跌破90美元 | price:close | level | lt | 90 | market_price | 2027-03-31 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t4 | 出现论点被削弱的研判事件 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| invalidators | i1 | 收入环比增长超过2% | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | qoq_pct | gt | 2 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 出现论点被加强的研判事件 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-03-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |


### DXC 场景套

环境：legacy；对象：`company-scenario-set:362f1707dcb8f90cc83f948a7568790d`；生成：2026-10-07T10:40:38.097370+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 利润和现金流守住，营收继续温和下滑，p=0.50

环境：legacy；版本：`company-scenario-version:d38b9b748531950ac3f2e2f71a1d72ac`；生成：2026-10-07T10:40:38.097370+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：管理层按在手订单定的保守指引基本兑现：调整后 EBIT 利润率和自由现金流达标，但 CES 短期项目没有回暖，GIS 要到下半年才靠积压订单滚出收窄降幅。营收同比保持中个位数下滑，滚动 book-to-bill 在 1 附近徘徊，AI 和 Anthropic 合作的收入仍可忽略。市场继续按「短期止不住下滑」给估值打折，股价主要靠现金流收益率托底。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 9 月季度营收环比降幅收窄到 2% 以内 | concept:us-gaap:Revenues | qoq_pct | gte | -2 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 10 月底业绩会维持全年指引，Q3 book-to-bill 在 1 附近 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 营收同比降幅扩大到 8% 以上 | concept:us-gaap:Revenues | yoy_pct | lt | -8 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 营收同比降幅收窄到 2% 以内，说明拐点比基准更早 | concept:us-gaap:Revenues | yoy_pct | gt | -2 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bull 订单领先收入兑现，营收降幅接近收窄到零，p=0.15

环境：legacy；版本：`company-scenario-version:b0927252d131514e2aefa560b1ccaf28`；生成：2026-10-07T10:40:38.097370+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

叙事：Q3 book-to-bill 回到 1 以上，客户推迟的可自由裁量项目在出现可见的 AI 回报案例后陆续放行，CES 项目类服务回暖；GIS 按积压订单收窄降幅。营收同比降幅在 2027 年中前收窄到 2% 以内，调整后利润率扩张来自经营本身，满足 RBC 上行情景的两个条件，低倍数估值获得重估。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 营收同比降幅收窄到 2% 以内 | concept:us-gaap:Revenues | yoy_pct | gte | -2 | sec_actual | 2027-08-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 出现至少一次论点被强化的研判 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| triggers | t3 | 股价重估到 15 美元以上 | price:close | level | gt | 15 | market_price | 2027-10-06 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t4 | AI 合同在已报告订单或收入中单独显现，Q3 book-to-bill 高于 1 | None | level | None | None | owner | 2027-02-28 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 营收同比降幅扩大到 6% 以上 | concept:us-gaap:Revenues | yoy_pct | lt | -6 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bear 营收加速下滑，利润端也失守，p=0.35

环境：legacy；版本：`company-scenario-version:accb186bbf3fc1c2582e2428cbae44be`；生成：2026-10-07T10:40:38.097370+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

叙事：关税和成本压力让客户继续暂停短期项目，Q3 book-to-bill 仍低于 1，滚动订单跌破 1，订单领先收入的逻辑被证伪。营收同比降幅扩大到 7% 以上，经营杠杆有限，调整后利润率继续收缩，HPES 合并留下的大额商誉再次减值，落入 RBC 下行情景。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 营收同比降幅扩大到 7% 以上 | concept:us-gaap:Revenues | yoy_pct | lt | -7 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 季度经营现金流跌到 2 亿美元以下 | concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | level | lt | 200000000 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 先对齐公司 FCF 定义与一次性现金。 |
| triggers | t3 | 出现至少一次论点被削弱的研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| triggers | t4 | 股价跌破 9 美元 | price:close | level | lt | 9 | market_price | 2027-10-06 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| invalidators | i1 | 营收同比降幅收窄到 3% 以内 | concept:us-gaap:Revenues | yoy_pct | gt | -3 | sec_actual | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


### IBM 场景套

环境：legacy；对象：`company-scenario-set:8a768ff4971ca6fa7c2601936520fffc`；生成：2026-10-07T22:31:06.346920+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 基准：滑单陆续成交，软件落在下调后区间中段，p=0.50

环境：legacy；版本：`company-scenario-version:6c2fd084945909baa93056eec51d2b98`；生成：2026-10-07T22:31:06.346920+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

叙事：三季度剩余的软件滑单部分成交，丢单很少，软件增速落在下调后区间的中段。大型机和分布式需求继续撑住基础设施，咨询签约领先收入，咨询收入小幅增长。管理层维持审慎口径，全年总指引不动，只微调分项。总收入同比低个位数增长，市场仍按近期 EPS 定价，估值没有明显向软件同业靠拢，股价在年内低位附近震荡。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 三季度收入同比增速回升到 2% 以上，高于二季度的 1.09% | concept:us-gaap:Revenues | yoy_pct | gte | 2 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 三季报电话会确认大部分滑单已成交，全年总指引维持不变 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 三季度收入同比增速超过 5%，说明需求明显强于基准情景 | concept:us-gaap:Revenues | yoy_pct | gt | 5 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 三季度收入同比转为负增长 | concept:us-gaap:Revenues | yoy_pct | lt | 0 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bull 乐观：滑单全部成交且 AI 订单落地，估值向软件同业修复，p=0.20

环境：legacy；版本：`company-scenario-version:d2560e8debddc19db08c3a87fbe39645`；生成：2026-10-07T22:31:06.346920+00:00；正文 refs／claim_refs 引用 5 条，其中检查时退休 0 条。

叙事：三季度滑单基本全部成交，软件增速冲到下调后区间的高端。Red Hat 签约随联邦交易恢复而重新加速，GenAI 积压订单加快转成咨询收入。如果公司开始披露 AI backlog，RBC 说的「增长驱动清晰可见」就有了业绩支撑，市场不再只把 IBM 当硬件公司看。营收超预期，估值倍数向软件同业靠拢，股价明显脱离年内低点。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 三季度收入同比增速超过 5% | concept:us-gaap:Revenues | yoy_pct | gt | 5 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 市盈率从 19.4 倍升到 23 倍以上，说明估值在修复 | valuation:trailing_pe | level | gt | 23 | valuation | 2027-06-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 股价从 220 美元升到 260 美元以上 | price:close | level | gt | 260 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t4 | 公司首次披露 AI backlog 或 AI 订单规模，并且规模可观 | None | level | None | None | owner | 2027-04-30 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 AI business book 已有披露，不能把“首次披露”作为未经时点核查的催化。 |
| invalidators | i2 | 三季报显示 Red Hat 环比继续减速，或软件再次缺少上行 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |


#### bear 悲观：资本开支类采购持续推迟，软件跌到区间低端，p=0.30

环境：legacy；版本：`company-scenario-version:b6c9adef6654f37107b14ae9e5b04b90`；生成：2026-10-07T22:31:06.346920+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：二季度的预算压力延续到下半年，主机和 ELA 这类资本开支类采购继续被推迟，滑单成交率低于预期，软件落到下调后区间的低端，Red Hat 继续减速。咨询签约增长转化成收入的速度很慢，AI 重塑咨询的担忧加重。公司可能再次下调分项指引，收入同比走平甚至下滑，市场重演「deja vu」式抛售，股价跌破年内低点。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 三季度收入同比转为负增长 | concept:us-gaap:Revenues | yoy_pct | lt | 0 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 股价从 220 美元跌到 190 美元以下 | price:close | level | lt | 190 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t3 | 此后出现至少 1 次「论点削弱」研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| triggers | t4 | 三季报再次下调软件或全年指引 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 三季度收入同比增速超过 3% | concept:us-gaap:Revenues | yoy_pct | gt | 3 | sec_actual | 2027-01-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 此后出现至少 1 次「论点增强」研判 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |


### META 场景套

环境：hyperscalers；对象：`company-scenario-set:a5cdb497a990b09cd5d641f5d971b0f4`；生成：2026-10-07T22:28:42.540787+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 基准：广告稳健增长，资本开支回报仍待验证，p=0.50

环境：hyperscalers；版本：`company-scenario-version:36e21a46f463394f689368c0e1cfbeb8`；生成：2026-10-07T22:28:42.540787+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：AI 推荐和 Advantage+ 继续推动广告收入保持 20% 以上的同比增长，但增速不再明显提升。资本开支按惯例在年内上调，2027 年首次指引偏高。市场仍要等更清楚的 AI 变现证据，估值倍数大体持平。Watermelon、Hatch 发布后反响中性，下一次财报没有带来新的拐点。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 第三季度收入继续环比增长，单季超过 620 亿美元 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | level | gt | 62000000000 | sec_actual | 2026-11-30 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 财报后管理层给出 2027 年资本开支首次指引，口径延续先给区间、年内上调的做法 | None | level | None | None | owner | 2027-03-31 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 收入同比增速掉到 20% 以下 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | lt | 20 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 收入同比增速升到 32% 以上，说明已经进入乐观情景 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | gt | 32 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bull 乐观：AI 变现兑现，收入加速，估值重估，p=0.25

环境：hyperscalers；版本：`company-scenario-version:97b3f3f356a1c8ed97d04b815392aadf`；生成：2026-10-07T22:28:42.540787+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

叙事：Watermelon、Hatch 和 Muse 带动用户参与度和广告转化率继续提升，下一次财报同时展示 AI 变现和收入加速。富余云算力的出售让市场看到算力资产的价值。资本开支回报的疑虑减弱，资金回流，股价和估值倍数一起上行，年底前出现卖方所说的情绪拐点。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入同比增速升到 30% 以上 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | gte | 30 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 股价升到 820 美元以上，反映估值重估 | price:close | level | gt | 820 | market_price | 2026-12-31 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t3 | 出现研判为 THESIS_STRENGTHENED 的事件 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-01-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| invalidators | i1 | 收入同比增速掉到 25% 以下 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | lt | 25 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bear 悲观：资本开支继续上调，收入增速跟不上，p=0.25

环境：hyperscalers；版本：`company-scenario-version:462a55c40a8c8131b3ef55ecdc0bcf7f`；生成：2026-10-07T22:28:42.540787+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

叙事：资本开支继续大幅上调，但收入增速没有同步改善，EPS 预期上调持续停滞。Watermelon 和 Hatch 发布后，下一次财报仍拿不出 AI 变现证据，市场对 AI 资本开支回报的怀疑进一步加深。诉讼或海外监管再添不确定性，资金继续减配科技股，估值倍数下行。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入同比增速掉到 22% 以下 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | lt | 22 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 资本开支同比增速升到 90% 以上 | concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | yoy_pct | gt | 90 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 股价跌到 620 美元以下 | price:close | level | lt | 620 | market_price | 2026-12-31 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 |
| triggers | t4 | 出现研判为 THESIS_WEAKENED 的事件 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-01-31 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| invalidators | i1 | 收入同比增速升到 30% 以上 | concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | yoy_pct | gte | 30 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


### GOOGL 场景套

环境：hyperscalers；对象：`company-scenario-set:926831719ef2d50d62326b92b0e420fe`；生成：2026-10-08T14:37:25.736305+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base Cloud 收入持续兑现，资本开支增速放缓，p=0.50

环境：hyperscalers；版本：`company-scenario-version:5a13ee9d4cc54f7241b56c246ea69f4a`；生成：2026-10-08T14:37:25.736305+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：企业客户对算力和 Gemini 的需求继续推动 Google Cloud 增长，积压订单按公司预计节奏逐步确认收入，整体收入同比保持在 20% 以上。资本开支仍处高位，但环比增速从二季度的约 26% 回落，自由现金流压力不再扩大。市场对 AI 回报的争议没有消失，但随着收入兑现逐步缓和。Anthropic 占比高带来的客户集中风险仍在，短期内不会暴露。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 资本开支环比增速从二季度的 25.93% 回落到 15% 以下 | concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | qoq_pct | lt | 15 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 财报中管理层表示资本开支指引维持不变，Cloud 利润率继续改善 | None | level | None | None | owner | 2026-11-15 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 收入同比增速从 24.23% 跌到 15% 以下 | concept:us-gaap:Revenues | yoy_pct | lt | 15 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 此后出现论点被削弱的研判事件 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-02-28 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |


#### bull 收入加速超预期，AI 回报得到验证，p=0.25

环境：hyperscalers；版本：`company-scenario-version:a0652ca33080cddfe46dace9041086e3`；生成：2026-10-08T14:37:25.736305+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：Anthropic、Blackstone 等大客户的 TPU 和云容量承诺比预期更快转化为收入，Cloud 增速和利润率继续超出预期，搜索广告同时保持稳健。收入同比加速到 27% 以上，经营利润增速快于收入，市场把定价重心从自由现金流缺口转回收入兑现，我们与市场在时间上的分歧以有利方向收敛。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 收入同比增速从 24.23% 加速到 27% 以上 | concept:us-gaap:Revenues | yoy_pct | gt | 27 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 经营利润同比增速从 30.38% 升到 35% 以上 | concept:us-gaap:OperatingIncomeLoss | yoy_pct | gt | 35 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 此后出现论点被强化的研判事件 | judgement:THESIS_STRENGTHENED | level | gte | 1 | event_judgement | 2027-02-28 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| triggers | t4 | 财报或公告披露 TPU 外部订单明显增加 | None | level | None | None | owner | 2027-02-28 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |
| invalidators | i1 | 收入同比增速跌到 20% 以下 | concept:us-gaap:Revenues | yoy_pct | lt | 20 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 资本开支环比增速超过 30%，说明投入仍在加速 | concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | qoq_pct | gt | 30 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |


#### bear 资本开支继续上调，自由现金流长期承压，p=0.25

环境：hyperscalers；版本：`company-scenario-version:c741f45280914b3fb82c237aa65e5854`；生成：2026-10-08T14:37:25.736305+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

叙事：资本开支指引再次上调，环比增速继续高于收入，自由现金流率连续多个季度下滑。积压订单确认慢于预期，或大客户 Anthropic 的用量节奏放缓，导致收入增速回落，2027 年积压增长见顶的担忧提前兑现。市场维持现金流定价框架，TMT 和 AI 基础设施资金继续流出，估值承压。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 资本开支环比增速从 25.93% 进一步升到 30% 以上 | concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | qoq_pct | gt | 30 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t2 | 收入同比增速从 24.23% 回落到 18% 以下 | concept:us-gaap:Revenues | yoy_pct | lt | 18 | sec_actual | 2027-08-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| triggers | t3 | 自由现金流收益率从 1.24% 降到 0.9% 以下 | valuation:fcf_yield | level | lt | 0.009 | valuation | 2027-08-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 先对齐公司 FCF 定义与一次性现金。 |
| triggers | t4 | 此后出现论点被削弱的研判事件 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-02-28 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 |
| invalidators | i1 | 收入同比增速加速到 28% 以上 | concept:us-gaap:Revenues | yoy_pct | gt | 28 | sec_actual | 2027-02-28 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 |
| invalidators | i2 | 管理层表示资本开支已见顶，指引不再上调 | None | level | None | None | owner | 2027-02-28 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 |


### MSFT 场景套

环境：hyperscalers；对象：`company-scenario-set:d6477e1eb679aaad117ffe6c85676242`；生成：2026-10-09T00:02:28.386056+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

概率总和：1.00；评估 C：缺概率证据、条件联合发生率、数值经营／估值结果。

#### base 需求持续超供，产能逐步释放，现金流承压后修复，p=0.50

环境：hyperscalers；版本：`company-scenario-version:81a2675c8feadb1ee12c54856f38c607`；生成：2026-10-09T00:02:28.386056+00:00；正文 refs／claim_refs 引用 3 条，其中检查时退休 0 条。

叙事：企业预算继续向AI倾斜，微软在CIO调研中保持份额第一，已签约的承诺性工作支撑云收入。数据中心供给仍是增长瓶颈，Azure增速大致达到指引，收入同比维持在中高双位数。新增产能分批上线，逐步缓解供给约束。高资本开支让自由现金流在近几个季度继续为负或接近零，股息增幅保持克制。市场情绪从动量平仓慢慢转向看基本面，股价温和修复。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 股价从当前522美元温和修复到560美元以上 | price:close | level | gte | 560 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t2 | 管理层在财报中表示新增产能上线、供给约束开始缓解，同时Azure增速达到指引 | None | level | None | None | owner | 2027-04-30 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| invalidators | i1 | 季度收入同比增速从17.75%跌破14% | quarterly_revenue_yoy_growth | level | lt | 14 | claim | 2027-07-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| invalidators | i2 | 此后出现2次及以上论点被削弱的研判 | judgement:THESIS_WEAKENED | level | gte | 2 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |


#### bull 产能放量叠加Copilot变现，Azure加速、估值重估，p=0.25

环境：hyperscalers；版本：`company-scenario-version:6d252ddb43af6fd9dfa3552339728b95`；生成：2026-10-09T00:02:28.386056+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

叙事：新增数据中心产能比预期更早上线，积压的承诺性需求快速转成收入，Azure增速超过指引，收入同比加速到20%以上。Copilot从早期试用进入规模化席位付费，微软在AI编排层的位置得到确认。资本开支开始带来收入，自由现金流转正，市场重新把它看作最稳健的AI收费者。财报后行业资金像以往一样大幅净买入，股价摆脱2008年以来最长的月度连跌。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 季度收入同比增速从17.75%加速到20%以上 | quarterly_revenue_yoy_growth | level | gt | 20 | claim | 2027-07-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t2 | 股价从522美元涨到620美元以上 | price:close | level | gt | 620 | market_price | 2027-10-31 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t3 | 此后出现2次及以上论点被加强的研判 | judgement:THESIS_STRENGTHENED | level | gte | 2 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t4 | 财报后行业资金出现明显净买入，CIO调研继续把微软列为预算份额第一 | None | level | None | None | owner | 2027-04-30 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| invalidators | i1 | 季度收入同比增速跌破15% | quarterly_revenue_yoy_growth | level | lt | 15 | claim | 2027-07-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| invalidators | i2 | 此后出现论点被削弱的研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |


#### bear 需求松动而资本开支不减，现金流持续为负，p=0.25

环境：hyperscalers；版本：`company-scenario-version:e314f4afea536c4ae764afd9b0047f77`；生成：2026-10-09T00:02:28.386056+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

叙事：Copilot在企业里推广缓慢，客户清理未使用的模块，CIO更倾向第三方数据仓库，单客户增量支出被压低。需求超过产能的说法开始松动，Azure没有加速，收入增速放缓到中低双位数。资本开支已经锁定，自由现金流继续为负，市场质疑AI基建的回报。动量平仓和仓位清理延续，股价继续走弱。

| 条件类 | ID | 条件 | 指标 | 变换 | 比较 | 阈值 | 观测者 | 截止 | 评估 |
|---|---|---|---|---|---|---|---|---|---|
| triggers | t1 | 季度收入同比增速从17.75%放缓到14%以下 | quarterly_revenue_yoy_growth | level | lt | 14 | claim | 2027-07-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t2 | 股价从522美元跌破450美元 | price:close | level | lt | 450 | market_price | 2027-06-30 | C：价格阈值可作风险／仓位监测，不能独立验证经营 thesis；目标价缺 EPS／倍数 derivation。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t3 | 此后出现论点被削弱的研判 | judgement:THESIS_WEAKENED | level | gte | 1 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| triggers | t4 | 管理层不再提需求超过产能，或者自由现金流继续为负而Azure没有加速 | None | level | None | None | owner | 2027-04-30 | C：owner 定性条件需编码、来源、时间和判定规则；不应把主观确认直接当概率更新。 先对齐公司 FCF 定义与一次性现金。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| invalidators | i1 | 季度收入同比增速加速到20%以上 | quarterly_revenue_yoy_growth | level | gt | 20 | claim | 2027-07-31 | B／C：方向可用于监测，阈值缺经验／指引依据；需对应净新增、季节性、FX／收购与分部贡献。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |
| invalidators | i2 | 此后出现2次及以上论点被加强的研判 | judgement:THESIS_STRENGTHENED | level | gte | 2 | event_judgement | 2027-06-30 | C：自身 judgement 作为 trigger 易循环确认；需外部独立事实，不是重复自己的观点。 本套部分引用 Oracle 财务模型；负 FCF 相关条件须先清源，且没有自身模型／估值 payoff。 |


### AMZN

没有场景集；不能把缺失概率当零，不能由 sell-side target 替代自身场景。

## 场景与 gate 决策记录

决策记录是人／系统何时接受某个版本的 authority，不是额外投资论据；已经通过不消除上述问题。本评估未改审批。

### company_scenario_decisions

| 环境 | 公司 | 决策版本 | 日期 | 决策定位 | 质量含义 |
|---|---|---|---|---|---|
| legacy | DXC | company-scenario-decision:2a0fa6eba4f41050f32651a0c7639356 | 2026-10-07T11:23:20.391803+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:362f1707dcb8f90cc83f948a7568790d'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | EPAM | company-scenario-decision:fa960059db0bc96496efc782f9a741c4 | 2026-10-07T11:23:22.519027+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:9c8d0ec6807ca9d1c6e7fc70667ea92c'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | CTSH | company-scenario-decision:587ecc6deba71f73ab1639ba7c2b9d58 | 2026-10-07T11:23:24.058733+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:6509211abb0e0c3f9f75f0cc634a99d9'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | ACN | company-scenario-decision:4b51c86f649719cdd39ebc347a7aa02b | 2026-10-07T11:23:27.120230+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:0fd3d022fb1a61212eccc1d92888dca3'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | IBM | company-scenario-decision:1e7da0785d604c201fd70977d3d77f94 | 2026-10-07T22:45:46.533562+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:8a768ff4971ca6fa7c2601936520fffc'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| hyperscalers | META | company-scenario-decision:ca510bc46385f6a4187ced3eeb4d19bc | 2026-10-07T22:46:03.998441+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:a5cdb497a990b09cd5d641f5d971b0f4'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| hyperscalers | GOOGL | company-scenario-decision:e5e502ef1f53f438e4895c72d066ff5f | 2026-10-09T03:13:48.174763+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:926831719ef2d50d62326b92b0e420fe'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| hyperscalers | MSFT | company-scenario-decision:796a2c3e19ee4a840f530771c3abb9c6 | 2026-10-09T03:13:50.234193+00:00 | {'decision': 'accept', 'reason': '系统记录：用户未填写补充说明', 'set_ref': 'company-scenario-set:d6477e1eb679aaad117ffe6c85676242'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |


### deep_insight_gate_decisions

| 环境 | 公司 | 决策版本 | 日期 | 决策定位 | 质量含义 |
|---|---|---|---|---|---|
| legacy | EPAM | deep-insight-gate-decision:2281cd3d3c98a3d5e3b9a349b4e0e2d1 | 2026-09-17T15:00:23.187774+00:00 | {'decision': 'approve', 'gate_version_ref': 'deep-insight-gate-version:company-sec-cik-0001352010:2', 'reason': '系统记录：用户未填写补充说明'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | ACN | deep-insight-gate-decision:5af80528b9fde3cd8d7ece4530fd5c44 | 2026-09-17T15:00:04.487522+00:00 | {'decision': 'approve', 'gate_version_ref': 'deep-insight-gate-version:company-sec-cik-0001467373:2', 'reason': '系统记录：用户未填写补充说明'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | IBM | deep-insight-gate-decision:921ed2082d1de6c932853af8de28f590 | 2026-09-17T15:00:38.874525+00:00 | {'decision': 'approve', 'gate_version_ref': 'deep-insight-gate-version:company-sec-cik-0000051143:2', 'reason': '系统记录：用户未填写补充说明'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | CTSH | deep-insight-gate-decision:2fa385a69f1b768e91d8b08eee7682ef | 2026-10-03T19:04:01.638511+00:00 | {'decision': 'approve', 'gate_version_ref': 'deep-insight-gate-version:company-sec-cik-0001058290:4', 'reason': '系统记录：用户未填写补充说明'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| legacy | DXC | deep-insight-gate-decision:5f5e266fa53e6add1eca8db6a5425f4b | 2026-10-03T19:03:54.727115+00:00 | {'decision': 'approve', 'gate_version_ref': 'deep-insight-gate-version:company-sec-cik-001688568:4', 'reason': '系统记录：用户未填写补充说明'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |
| hyperscalers | META | deep-insight-gate-decision:f6d8934ef993d417c2878e8dd6bc49db | 2026-10-07T22:46:35.168575+00:00 | {'decision': 'approve', 'gate_version_ref': 'deep-insight-gate-version:company-ticker-meta:3', 'reason': '系统记录：用户未填写补充说明'} | 审批身份／版本可追踪；需与对应语义问题一并复审，不以 passed 代研究质量。 |


## 九份 earnings preview：每段／每个听会问题

### ACN

环境：legacy；版本：`mission-deliverable-version:02b5d16b0aa7fb7c3ffb8e851daeb593`；生成：2026-09-14T17:05:36.522283+00:00；正文 refs／claim_refs 引用 1 条，其中检查时退休 0 条。

C：已发布 FY26 Q4 应先入模型；指引／共识按下一财季对齐，咨询／托管听会问题需配自身数值。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-01 #1 | Accenture 的业绩检验重点是人工智能订单能否转为净新增收入，而不是合作公告能否证明需求复苏；目前行业需求触底仍未确认。与 Amazon Web Services 的合作提供了业务拓展证据，但投资论点仍需订单兑现、可选咨询需求企稳和交付效率改善共同支持。本次研究资料没有街上的预测数字，本期公司指引也缺来源，因此无法判断我们的预测高于或低于市场或指引；实际收入相对模型假设的表现、订单转化和利润率将决定是否调整判断。日期未确认，公布时间为估计安排。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-01 #2 | 新增订单是否转向托管服务和人工智能驱动的业务重塑，可选咨询需求的下滑是否缓和？——行业需求触底尚未确认。订单结构改善只有伴随项目启动、收入兑现及托管服务续约稳定，才足以支持增长先企稳再恢复的判断。日期未确认，公布时间为估计安排。 | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |
| earnings preview 2026-10-01 #3 | 人工智能项目带来了新增客户预算，还是替代既有咨询支出？已签订单何时贡献收入，并带来后续托管服务需求？——Accenture 与 Amazon Web Services 的合作说明业务拓展方向，但缺少本期收入贡献来源。需要管理层区分新增预算与原有支出转移，并说明订单兑现情况，才能判断人工智能业务是否真正抵消可选咨询疲软。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #4 | 实际收入高于还是低于我们的模型假设，差异来自订单转化、咨询需求还是托管服务？——本次研究资料没有街上的预测数字，公司本期指引也缺来源，无法量化我们相对市场或指引的分歧。我们的明确判断是复苏仍待验证；收入偏差及其业务来源将决定是否上调对增长恢复的信心，而不能只凭总收入表现作结论。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-01 #5 | 服务成本、销售及营销费用、一般及行政费用相对我们的假设如何，人员利用率和交付效率改善是否留在利润中？——投资论点要求收入增长不以营业利润率下降为代价。需结合收入表现解释费用偏差，并核实交付节省是否被降价或重组支出抵消；业务优化成本、总营业费用及营业利润预测缺来源，目前无法据模型给出完整利润判断。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #6 | 看到这些算 thesis 被证实： | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #7 | 新增订单结构向托管服务和人工智能驱动的业务重塑改善，可选咨询需求下滑缓和，续约保持稳定，且订单开始兑现为收入。这将支持增长先企稳再恢复的条件性路径；行业触底仍需更广泛的需求证据。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #8 | 人工智能项目产生可辨认的净新增收入及后续托管服务需求，足以抵消可选咨询疲软；人员利用率和交付效率提升帮助维持营业利润率，重组节省得到实际保留。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #9 | 看到这些算 thesis 被打破： | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #10 | 新增订单继续收缩、托管服务续约恶化，且人工智能需求未形成新增预算，将打破订单结构改善推动增长企稳的路径，而不仅是延后复苏时点。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #11 | 人工智能订单持续无法兑现为收入，或主要替代既有业务，导致托管服务增量不足以抵消咨询疲软；若增长还依赖降价或持续侵蚀营业利润率，则增长与利润率兼顾的投资论点失效。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-01 #12 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-01 #13 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-01 #14 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### IBM

环境：legacy；版本：`mission-deliverable-version:f577351d1a5e389070f84323804a4041`；生成：2026-09-26T12:22:32.650830+00:00；正文 refs／claim_refs 引用 7 条，其中检查时退休 1 条。

C：硬件预算挤占为 sell-side 转述，需要具体报告期间及软件／咨询反例；不得以旧线性模型 Q4 缺季节性差作为 alpha。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-21 #1 | IBM 本季业绩日期未确认（estimated）。我们对本季收入和经营现金流的判断比较谨慎，风险主要在硬件：卖方纪要记录过一次负面业绩预告，当时各业务线全面不及预期，分析师把原因归到行业 AI 支出挤占了 IBM 硬件的资本开支预算。本季缺少来源的是市场一致预期和公司指引，所以我们和街上、和指引的差距都没法量化，只能拿实际值去对照我们自己的模型假设。能改变判断的是软件增速：软件如果在低基数下重新加速，续约定价和合同留存又守得住，偏谨慎的看法就要改。 | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |
| earnings preview 2026-10-21 #2 | 硬件收入和订单有没有继续被客户的 AI 基础设施支出挤占，管理层怎么描述供给受限和客户预算重新分配。——此前那次负面预告和全面不及预期都被归因于 AI 支出挤占 IBM 硬件预算。另一方面，BofA 以 AI 基础设施需求强劲、部分环节供给受限为由上调了 IBM 目标价。本季数据能看出这两种解读哪一种成立。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-21 #3 | 软件收入增速能否在低基数下企稳或重新加速，续约价格和维护合同留存率怎么走。——IBM 在 Q1 基数较低的情况下软件收入仍然减速，同周软件板块情绪也在走弱。续约定价和存量合同留存是我们模型里的主要驱动项。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-21 #4 | 新签软件需求和咨询服务订单转化为交付的节奏。——BofA 认为竞争对手 Infosys 的增长回升还要再等一段时间，说明 IT 服务整体需求偏慢。IBM 新签转交付的速度决定收入会不会低于我们的假设。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-21 #5 | 经营现金流和资本开支的实际值与我们的假设差多少。——自由现金流是我们估值的锚点，具体预测值见带 ref 的文档摘要。收入偏弱时，要看营运资金能不能托住现金流。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-21 #6 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-21 #7 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-21 #8 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### CTSH

环境：legacy；版本：`mission-deliverable-version:e038242f2d1664a7aa4f73701c92dad2`；生成：2026-09-28T00:10:44.460508+00:00；正文 refs／claim_refs 引用 9 条，其中检查时退休 0 条。

C：承认营业利润未建成是诚实边界；人员金字塔、重组、TriZetto 问题具体，但有共识快照却未连到本期。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-28 #1 | Cognizant（CTSH）本季业绩日期未确认，目前只有估计日期。本季的外部参照几乎为空：这个 Core 没有街上的一致预期，公司也没有针对本期的指引事件，所以我们的收入和成本预测暂时没有可对比的外部基准。本场最值得听的是 AI 带动的可选小项目回暖能否转化为收入，以及招聘扩张后的成本控制能否守住管理层对 margin 的承诺。GS US TMT desk 曾把 CTSH 列为最不受青睐的公司之一，业绩后股价也出现过较大跌幅，市场预期偏低。 | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |
| earnings preview 2026-10-28 #2 | 本季收入实际值与我们的假设差多少，以及金融服务和医疗行业里 AI 相关的可选小项目有没有继续回暖（日期未确认，estimated）。——这个 Core 没有街上的一致预期，公司也没有本期指引，收入实际值是检验我们模型的唯一硬参照。管理层在 2025 年业绩会上说可选小项目开始回来，本季能看出这个说法有没有落到收入上。 | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |
| earnings preview 2026-10-28 #3 | 收入成本（不含折旧摊销）与我们的假设差多少，金字塔底层招聘扩张有没有压低毛利。——管理层说金字塔相关招聘同比翻倍以上，2025 年在印度新增一万六千多名员工，2026 年还要继续招人。人员扩张最先反映在收入成本里，会直接影响管理层希望 2026 年延续的 margin 扩张。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #4 | 销售及管理费用、折旧摊销与我们的假设差多少，重组费用和资产处置收益有没有一次性项目。——重组费用和资产处置收益在我们的模型里缺来源，营业利润因此也没有算出来。实际披露出来以后，才能判断 EPS 增速高于收入增速是靠经营效率还是靠一次性项目。 | B／C：具体利润问题有用；先隔离证券／税／重组，普通股与股数桥不可省略。 |
| earnings preview 2026-10-28 #5 | Health Sciences 能否继续跑赢公司平均，大单结构里 AI 创新类和效率类是否仍然均衡。——管理层称 Health Sciences 增速高于公司平均，TriZetto 平台叠加 agentic 技术正在获得转型项目。大单结构也决定 AI 对 Cognizant 是扩大可触达支出还是压缩原有业务。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #6 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-28 #7 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #8 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### DXC

环境：legacy；版本：`mission-deliverable-version:e1555a0e86a5f986ec3ccf34379f8718`；生成：2026-09-29T00:27:11.307535+00:00；正文 refs／claim_refs 引用 8 条，其中检查时退休 0 条。

C：GIS／CES、退出保险合同及 Anthropic 工程认证问题合理；称所有成本／利息缺来源与最新可算模型不符，需版本对齐。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-29 #1 | 业绩日期未确认（estimated）。这个 Core 没有街上的一致预期，公司也没有给出本季的单季指引，所以我们的收入预测只能和公司的全年方向来比：公司预计全年有机收入继续下滑，下半年降幅收窄，其中 CES 预计低个位数下滑，GIS 预计中个位数下滑。本季还在上半年，我们判断收入仍会承压，主要压力来自 GIS 的短期可选基础设施项目，以及一份正在退出的保险合同。收入以外的成本、费用、利息和税项科目在模型里都缺来源，本季判断只落在收入这一项上。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-29 #2 | GIS 的短期可选项目和转售类项目有没有止跌，以及这部分收入缺口在 GIS 利润率里吃掉了多少。——GIS 大约占总收入的一半。上一季收入低于公司预期，缺口直接落到利润率上；前一季短期项目的压力还第一次蔓延到转售类的可选项目。本季收入偏离我们的假设，最可能就出在这里。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #3 | 管理层是否重申全年有机收入下滑、下半年降幅收窄的判断，CES 和 GIS 各自的下滑区间有没有调整。——本期既没有一致预期，也没有单季指引，全年分部方向是唯一能拿来对照我们收入预测的锚。区间一旦下调，我们的收入假设就要跟着下调。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #4 | CES 的订单能否在应用业务高基数之后企稳，定制应用的下滑有没有被 GrowthX、DXC Engineering 和企业应用的增长抵消。——上一季 CES 订单同比下降，但 DXC Engineering 和 GrowthX 的订单在增长，CES 收入的降幅也好于公司预期。公司自己说，这几块业务的势头能帮它对冲定制应用的行业性下滑。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #5 | 正在退出的那份保险合同对增速的拖累有多大，第四财季回升的说法是否仍然成立。——公司说这份合同在前三个财季都会拖累增速，第四财季才开始回升。本季仍在拖累期内，它决定了降幅能不能按公司说的节奏收窄。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #6 | Anthropic 合作和 agentic 相关业务有没有开始产生订单或收入。——公司按接近零的保守口径给 Anthropic 合作建模，因为工程师才刚开始认证。只要本季出现可量化的贡献，CES 就有了一块超出指引的收入来源。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #7 | 服务成本、销售及管理费用、折旧摊销、重组费用和利息支出的实际值是多少。——这些科目在我们的模型里都缺来源，本季实际值出来后才能补上利润端的假设。 | C：当前模型已有成本／D&A／restructuring／interest 预测；本前瞻缺口描述需按所绑定版本重新生成。 |
| earnings preview 2026-10-29 #8 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-29 #9 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-29 #10 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### EPAM

环境：legacy；版本：`mission-deliverable-version:5fc2cf8bccfb703ff088c12f97c8671b`；生成：2026-10-06T00:01:28.964834+00:00；正文 refs／claim_refs 引用 8 条，其中检查时退休 0 条。

B／C：增长缺口、AI 大单转化与费用问题有用；首季平增长应与官方 range／共识同口径，缺 OP→EPS。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-11-05 #1 | 我们的倾向：这一季收入落在温和区间，营业利润率由成本率决定，下行风险集中在美洲需求。依据是管理层此前说过整体需求环比走软、北美尤其疲弱、客户决策周期拉长，同时大单集中在现有客户的 AI 原生和类 BPO 交付改造上。这个 Core 没有街上的一致预期数字，公司指引事件也缺来源，所以我们的收入、成本和营业利润预测只能对照自身假设检验，具体数字见文档摘要。本场业绩日期未确认（estimated）。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-11-05 #2 | 美洲分部收入能否止跌企稳，管理层对客户决策周期的最新描述。——管理层此前点名北美表现落后、决策周期变长。美洲分部是收入的主体，它的走势决定实际收入高于还是低于我们的收入假设。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-11-05 #3 | 报告口径与固定汇率口径的收入增速差了多少，欧元、英镑、瑞郎和墨西哥比索的影响方向。——公司披露过汇率会推高报告口径增速。剔除汇率后才能看出真实需求，判断收入差异来自量还是来自汇率。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-11-05 #4 | AI 原生、类 BPO 和 agentic QA 相关的大单进展，以及新合同对毛利的影响。——管理层说大单集中在现有客户。这类交付改造可能先压价格、后提效率，直接影响成本率能否守住我们的假设。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-11-05 #5 | 交付人员数量和利用率的变化。——人力成本的金额缺来源，人员数和利用率是推算成本率的唯一抓手。利用率走低会让成本率高于假设，营业利润随之落空。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-11-05 #6 | SG&A 与折旧摊销的实际值，以及营业外项目和出售业务损益。——营业外收益、汇兑损失、税和净利润在我们的模型里缺来源，利润表下半段只能看实际披露。SG&A 和折旧摊销决定营业利润预测的剩余误差。 | B／C：具体利润问题有用；先隔离证券／税／重组，普通股与股数桥不可省略。 |
| earnings preview 2026-11-05 #7 | 估值倍数压缩是否随业绩改善而修复。——今年以来 EPAM 的 NTM P/E 压缩幅度在 Cognizant、Accenture 中最大，市场已计入较悲观的需求预期。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-11-05 #8 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-11-05 #9 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-11-05 #10 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### META

环境：hyperscalers；版本：`mission-deliverable-version:69d31acb3e52be8b0da7364d4f0446b0`；生成：2026-10-03T04:24:19.506741+00:00；正文 refs／claim_refs 引用 9 条，其中检查时退休 0 条。

C：广告变现／RL／算力 ROI 值得问；自身 Q3 收入超过官方指引上沿，应直接并列，不能只说没有 guidance。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-28 #1 | 这场业绩的公布日期尚未得到公司确认（日期未确认，estimated）。我们的判断是：本季的核心看点在于广告排序和推荐模型的改进能否继续支撑收入，同时承受研发和资本开支的加速投入；收入、各项费用和营业利润的预测由文档摘要带 ref 给出。这个 Core 没有街上的一致预期，公司也没有针对本期的指引事件（缺来源），所以只能拿实际值和我们自己的假设对比，无法与市场或指引对比。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #2 | 实际收入和我们的收入假设差多少，管理层是否继续把增长归因于转化增速快于广告展示量、以及检索和排序模型的改进。——收入假设是整个模型的起点。管理层此前把广告效果提升主要归因于排序和建模改进，如果这条驱动减弱，收入偏差会直接传到营业利润。 | C：需要把自身约 648.6 亿与公司 610–640 亿、街上预期并排；量价指标已有官方披露。 |
| earnings preview 2026-10-28 #3 | 研发费用和营业成本相对我们的假设落在哪里，前沿模型和基础设施投入有没有提前体现在费用和折旧里。——Mark Zuckerberg 表示公司正在为前沿模型和底层基础设施做大规模投入。研发是我们模型里最大的一项费用，偏差会决定营业利润落在预测上方还是下方。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #4 | 全年资本开支区间会不会再次上调，公司是否开始给出下一年资本开支的方向。——公司已经因为零部件涨价和数据中心成本上调过全年资本开支区间，而且一直没有给出下一年的具体展望。资本开支路径会决定未来几个季度营业成本里的折旧压力。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #5 | 销售、管理费用相对我们的假设如何，管理层有没有借助 AI 工具调整组织规模。——公司表示还在评估最优组织规模，并会用 AI 工具提高效率。这一点会影响销售和管理两项费用的走向。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #6 | Business Agent、WhatsApp 消息变现和 Meta AI 商家助手有没有给出可量化的进展。——卖方把这几条线看作新的变现来源，也是支撑 AI 投资回报的证据。它们一旦落到数字上，就能缓解市场对高投入的担心。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #7 | 利息及其他净收支的实际值是多少。——这一项在我们的模型里缺来源，税前利润、净利润和稀释股数因此都没有估计。实际值出来后才能补齐利润表下半段。 | B／C：具体利润问题有用；先隔离证券／税／重组，普通股与股数桥不可省略。 |
| earnings preview 2026-10-28 #8 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-28 #9 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #10 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### AMZN

环境：hyperscalers；版本：`mission-deliverable-version:5f0136f92b821db47890309259d47fe2`；生成：2026-10-07T20:54:35.695354+00:00；正文 refs／claim_refs 引用 10 条，其中检查时退休 0 条。

B／C：区分核心云迁移／伙伴集中度与零售好；没有自身模型，期间 unknown 应修正，而不是一份完整数字前瞻。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-29 #1 | 这场业绩的日期尚未经公司确认（日期未确认，estimated），对应期间同样未知。我们没有覆盖该期的预测，这个 Core 也没有街上的一致预期和公司指引，所以本次前瞻无法用数字衡量我们与市场的差距，只能做定性判断。我们当前倾向认为，AWS 加速的方向已有较充分的卖方依据；分歧在于加速的来源。如果主要来自模型伙伴和承诺容量爬坡，客户集中度风险和利润率能否持续，会比收入增速本身更关键。 | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |
| earnings preview 2026-10-29 #2 | 管理层能否拆分 AWS 增长的来源：Project Rainier 等承诺容量爬坡、模型伙伴上架，以及传统云迁移各自贡献多少？——Wells Fargo 把 AWS 加速主要归因于容量扩张，并引用管理层到 2027 年底容量翻倍的表述；另一方面，非 AI 的核心 AWS 业务在 Q2 2026 仍占收入大头，且同比加速。哪一股力量主导，决定增长是广泛的需求，还是集中在少数大客户。这是我们与卖方乐观叙事的核心分歧，也是能把这项分歧收掉的直接观测量。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #3 | AWS 利润率的变化，主要来自 Bedrock 和 Anthropic 合作带来的业务组合，还是阶段性定价？——卖方目标估值的依据之一是零售与 AWS 的利润率扩张。如果利润率优势依赖定价红利，同行以更低成本扩容后，这项优势会收窄。管理层对单位成本和定价的表述是关键观测点。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #4 | 北美零售在 Q3 2026 的增长有没有兑现 Wolfe Research 看到的上行空间？广告还在支撑零售利润率吗？——Wolfe 依据截至 8 月下旬的消费数据，认为北美零售有上修空间。卖方还认为，零售利润在 Project Leo 投入和 FBA 费用压力下保持健康，靠的是广告抵消关税和通胀的不确定性。 | C：缺自身模型是事实；对应期间未知／数据未取得不是已完成前瞻。应补读取并建基本面基准。 |
| earnings preview 2026-10-29 #5 | 管理层如何回应代理式购物对搜索广告位和客户关系控制权的影响？——如果主要由 AI 代理完成购物，第三方卖家为搜索结果显眼位置付的高额费用可能面临压力。亚马逊阻止 Muse 在站内购物，说明它在争夺客户关系的控制权。这与卖方提到的 Alexa Shopping 提升转化率是同一条主线。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-29 #6 | 资本开支节奏和融资安排：英镑债募集资金是否用于容量扩张？——公司 9 月完成了四档英镑计价固定利率优先票据发行，期限覆盖 2029 至 2045 年，但披露材料没有写明募集资金用途。如果管理层把这笔资金与容量投入挂钩，就能看出 AWS 加速对资产负债表的消耗程度。 | C：缺自身模型是事实；对应期间未知／数据未取得不是已完成前瞻。应补读取并建基本面基准。 |
| earnings preview 2026-10-29 #7 | 缺来源：这家公司还没有覆盖该期的预测模型行，preview 里没有我们自己的数字 | C：缺自身模型是事实；对应期间未知／数据未取得不是已完成前瞻。应补读取并建基本面基准。 |
| earnings preview 2026-10-29 #8 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-29 #9 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-29 #10 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### GOOGL

环境：hyperscalers；版本：`mission-deliverable-version:bf4614df01b5e65398fa31f334a0e041`；生成：2026-10-08T13:26:07.859617+00:00；正文 refs／claim_refs 引用 9 条，其中检查时退休 0 条。

B／C：Cloud、TPU、搜索、capex 的问题有用；缺数值预期差、核心 OP 与证券收益桥，已披露项目应先提取。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-28 #1 | 业绩日期未确认，目前是预估日期。本季最该看的是算力：管理层说算力紧张在未来数年都会持续，资本支出还会继续增加，客户也在签长期、照付不议的合同锁定容量。我们的收入、成本、经营现金流和资本支出都有本季预测，营业利润和净利润还缺来源。这个 Core 里没有街上的一致预期，公司也没有覆盖本季的指引，所以我们的预测暂时只能拿管理层的方向性表态和实际财报来检验，具体数值见文档摘要中的预测引用。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #2 | 资本支出是否继续上行？管理层会不会给出后续年份的投入方向，以及借款和预算追加的节奏？——管理层说算力紧张会持续数年，资本支出还会增加，也提到要靠增加借款支撑投入，并在年中追加预算。资本支出和经营现金流的差距决定自由现金流。我们对这两项的预测要靠本季实际数校准。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #3 | 云业务的积压订单和长期合同怎么样？传统非 AI 云需求有没有被 AI 数据中心建设带动？——管理层说客户锁定算力的意愿很强，打折合同里大多是长期照付不议协议。这决定扩建的算力能不能很快变成收入，也是检验我们收入预测的主要变量。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #4 | 搜索广告价格和广告加载率怎么变？AI Overviews 和 AI Mode 广告降价之后，海外广告主采用了多少？——专家访谈说谷歌在压低 AI 搜索广告价格来换取全球采用，CEO 也要求广告收入平稳增长，避免单季冲高。所以本季广告收入更可能是稳步增长，单价下降的部分要靠广告量补回来。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #5 | TPU 和 Ironwood 等自研加速器的对外供给与定价怎么样？算力紧张有没有推迟产品发布？——自研加速器能不能和英伟达 GPU 竞争、守住份额和定价，是还没有结论的争议。管理层又承认发布推迟主要受算力限制，还在借用外部算力。这两点会影响成本端和产品节奏。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #6 | Gemini 个人 Agent 嵌入 Android 和 Chrome 的进度，以及取消交易抽佣后商家入驻的情况——专家访谈提到谷歌在用现有分发渠道推个人 Agent，并取消了交易佣金来吸引商家。这关系到研发和销售费用的投入强度，也关系到新的变现路径。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #7 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-28 #8 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #9 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


### MSFT

环境：hyperscalers；版本：`mission-deliverable-version:e3ff9c83ff520b4c704e2502edb93f3d`；生成：2026-10-08T20:56:05.953961+00:00；正文 refs／claim_refs 引用 12 条，其中检查时退休 0 条。

C：应从官方 Azure／Copilot／capex 指引起建；尚无模型而且档案财务链污染，不能当已有 thesis 的例行前瞻。

| 段／条 | 内容定位 | 评估 |
|---|---|---|
| earnings preview 2026-10-28 #1 | 这份前瞻没有我们自己的预测，也没有市场一致预期和公司指引，所以对收入和利润给不出方向性判断。目前的看法集中在一点：微软正把算力从租用转向自有，同时为此保留资产负债表的灵活性。这场业绩要验证的是，这笔投入有没有和云业务、Copilot 变现同步兑现，还是在动量资金撤出时暴露成本压力。业绩日期还没有被公司确认（日期未确认，estimated），所属期间也缺来源。 | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #2 | 管理层怎么讲自有和租用算力的比例，以及数据中心租约起租的节奏。——富国银行认为微软越来越偏向自建站点，未来算力会以自有为主；年报也写明，已签约但还没起租的租约主要是数据中心。自建比例越高，资本开支和折旧的压力越早体现在报表上。目前没有一致预期，我们和市场的分歧要靠管理层对这个比例的表述来校准。 | C：缺自身模型是事实；对应期间未知／数据未取得不是已完成前瞻。应补读取并建基本面基准。 |
| earnings preview 2026-10-28 #3 | 剩余履约义务（RPO）的增长和确认节奏，管理层会不会单独讲商业部分。——年报只披露了全公司 RPO 在未来一年内的确认比例，没有单独给商业 RPO 的比例。如果管理层补充这一口径，就能判断积压订单能否覆盖新增算力的成本。 | C：缺自身模型是事实；对应期间未知／数据未取得不是已完成前瞻。应补读取并建基本面基准。 |
| earnings preview 2026-10-28 #4 | Copilot 是单独收费还是被打包进基础协议，企业客户是否绕开微软直接和大模型厂商签约。——美银的专家说，他们的合同里 Copilot Chat 含在基础协议内，Office Copilot 单独收费。另有证据显示，部分企业在采购 AI 时直接和外部前沿模型厂商签约。这两点决定了微软到底是 AI 最稳妥的收费方，还是只拿到渠道份额，也就是市场正在争的问题。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #5 | Azure 是不是仍然在拉动智能云和微软云的增长，扣除 OpenAI 投资损益后的利润怎么变。——公司把收入增长归因于微软云，智能云的增长靠 Azure；调整后利润扣除了 OpenAI 投资的损益。看清这个口径，才能把主业变现和投资波动分开。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #6 | 资本回报怎么排序：分红、回购和资本开支之间如何取舍。——分析师认为，微软这次分红上调幅度低于过去五年的平均水平，原因是要在 AI 基建投入高企时保留资产负债表的灵活性。管理层如果继续强调这种取舍，说明投入周期还会拉长。 | B／C：可用的听会／跟踪问题；需要基准、阈值、期间、来源和答复后修改哪条假设，当前不能衡量预期差。 |
| earnings preview 2026-10-28 #7 | 缺来源：这家公司还没有覆盖该期的预测模型行，preview 里没有我们自己的数字 | C：缺自身模型是事实；对应期间未知／数据未取得不是已完成前瞻。应补读取并建基本面基准。 |
| earnings preview 2026-10-28 #8 | 缺来源：consensus the consensus authority holds nothing for this period with a ref | C：当前环境有该公司共识快照；需对齐 fiscal period／引用 authority。只能说本条未接上，不是市场没有。 |
| earnings preview 2026-10-28 #9 | 缺来源：guidance the guidance profile has no event for this period | C：区别无单季指引、公开全年指引与本系统未采集；不得混称公司没提供。 |
| earnings preview 2026-10-28 #10 | （日期未确认：公布日期来自 estimated 的来源，公司尚未确认，本前瞻按预期日期写。） | B：标 estimated 比伪确定好；需公司 IR 日历确认，并展示 event date 与 earnings fiscal period。 |


## 九份当日 event note：每个子事件

NO_CHANGE 可指不修改现有假设，不等于证明事件对基本面无影响。旧事件被重新处理需原发生日期／首次判断／新增证据分离；只有文档 ID 无正文的事件仍需读取。

### IBM

环境：legacy；版本：`mission-deliverable-version:3d37fdb6e1a42de5b6abb3809db4bb3b`；生成：2026-10-09T00:01:16.636692+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 6 条 · NO_CHANGE 6 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| price_move · NO_CHANGE/no_change · research-event:05df87d399b7e60cda5131b1ffaa8239 | 判断是维持全部假设。IBM 在 2026-09-30 收于 〔数字见研判记录〕 美元，前收 〔数字见研判记录〕 美元，单日只跌了 〔数字见研判记录〕。触发事件的是相对表现：同组 4 只股票平均涨了 〔数字见研判记录〕，IBM 跑输约 〔数字见研判记录〕，这个差距低于 〔数字见研判记录〕 的阈值。这是一天的市场价格数据，不带任何经营数量，季度收入环比约 〔数字见研判记录〕、经营现金流占收入约 〔数字见研判记录〕、资本开支占收入约 〔数字见 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：9 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/research · research-event:3bf4ba0958a5c897485f5107b934e78f | 当前首选判断是：此次上涨是IBM显著相对走强的价格信号，尚未验证IT服务需求见底；维持投资论点的低置信度及全部预测，核查当日催化因素。2026年9月9日IBM收于〔数字见研判记录〕美元，上涨〔数字见研判记录〕，四家公司篮子下跌〔数字见研判记录〕，相对领先〔数字见研判记录〕个百分点，因此不宜简单归因于共同上涨；未提供SPY同期收益，篮子也不能替代大盘。事件只有市场价格证据，无法区分盈利预期上修与估值扩张，没有新增盈利兑现证据。现有投资论点 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：6 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:bf3b5784f598ed440de9ec1be20de6c6 | 维持全部预测假设。IBM 2025 年 10-K 的发行人回购表显示，2025 年 10 月、11 月、12 月三个月回购股数都是零，剩余授权约 〔数字见研判记录〕 亿美元，三个月一直没有变动。这是公司申报的一手数字，跟已核实结论一致：IBM 在 2019 年完成 Red Hat 收购时暂停了回购。四季度零回购说明这项资本配置政策没有变，现有预测本来也没有假设回购缩股，所以盈利兑现、预期和估值都不需要因此调整。 这份披露覆盖的是十个月前 | 0 | 0 | B／C：旧回购表无新增信息可维持；需当时日期、授权≠执行、当前股数预算。 C：3 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:49a6bd383bb603b3fbe39c2a56ebc173 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:04e857b4cb10dc7bece6e98edec32e14 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| price_move · NO_CHANGE/no_change · research-event:f42fa554dde78c12659875e8fe24d1f5 | 判断是维持全部预测假设。2026-10-02 IBM 收于 〔数字见研判记录〕 美元，前收 〔数字见研判记录〕 美元，单日下跌 〔数字见研判记录〕。同组 4 只股票同日平均下跌 〔数字见研判记录〕，IBM 相对同组跑赢 〔数字见研判记录〕，超过 〔数字见研判记录〕 的阈值，所以触发了这次事件。方向上看，这是板块整体下挫时 IBM 跌得更少，价格走势和现有预测并不冲突。这条数据只是一天的市场价格，没有收入、续约、定价或出货等经营数量，盈利 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：9 个数字占位，正文不可独立核验，需恢复实际值及引用。 |


### DXC

环境：legacy；版本：`mission-deliverable-version:0a77dd10b90ec139b090ab550c3c56f7`；生成：2026-10-09T00:01:16.697282+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 16 条 · NO_CHANGE 16 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| price_move · NO_CHANGE/no_change · research-event:dfea1a5fbc2440e885dc138b53f8a093 | 维持现有假设。2026-09-30 DXC 收盘 〔数字见研判记录〕 美元，前一交易日收盘 〔数字见研判记录〕 美元，单日涨 〔数字见研判记录〕。同组 4 家可比公司平均涨 〔数字见研判记录〕，DXC 跑赢 〔数字见研判记录〕 个百分点，绝对涨幅和超额涨幅都刚过 〔数字见研判记录〕 的触发线。 这条证据只有市场价格，当天没有公司申报、指引调整或客户消息。它反映估值和情绪的变化，看不出盈利兑现或市场预期有调整。现有驱动按 FY2026 申 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：12 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:731ee389ed48e290285bd5889e74a7e4 | 维持现有假设。这是一个推算值：用 FY2026 10-K 申报的全年收入 〔数字见研判记录〕 亿美元减去前九个月的 〔数字见研判记录〕 亿美元，得到第四财季收入约 〔数字见研判记录〕 亿美元。去年同期用同样方法推算为 〔数字见研判记录〕 亿美元，同比下降约 〔数字见研判记录〕，按申报精度的区间是 -〔数字见研判记录〕 到 -〔数字见研判记录〕。两个数都直接来自公司申报，属于 company_figure，只是第四季度的单季数字由推算得出 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：9 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:7df47838bcc1efe874b179d477823e91 | 维持现有假设。CEO 在 2026-01-30 的 Q3 FY2026 电话会上说，收入增长靠人手增长的时代正在结束，AI 让交付需要的增量资本和人力都更少。这是管理层对行业和公司的定性表态，没有给出具体数字。同一份纪要里，CES 收入同比下滑，公司把原因归到可自主决定的项目签约连续几个季度偏弱，并预计 FY27 改善。这两条都早于 FY2026 10-K 和 FY27 指引。公司后来给出的 FY27 指引是有机收入继续下降，调整后 E | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：2 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:0d1687720ebc3d94e2edb4a989a4cf3d | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:f1df208cd3956535ab535c6fee3e556f | 维持现有假设。这份网页里的五条已核实结论都由二手页面转述而来，最值得看的是两条。第一条说 FY27 收入指引区间包住 FactSet 一致预期，说明全年收入预期和市场基本一致。现有收入驱动按每季度环比约 -〔数字见研判记录〕 设定，和公司指引的 FY27 有机收入继续下降方向相同，因此收入假设和市场预期都没有新的偏差。 第二条说 Q2 调整后 EPS 指引低于一致预期，这是市场预期向下修正。公司此前已经指引 FY27 调整后 EBIT  | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：5 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:ef9d457480ac6adfdb35048deff941d4 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:d31e599aafa223a6f59289da89309b7f | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:8e2fc6cdd04116ca27f8c5ead653c79e | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:7268ddadda06cc37e47a96a2cb959d40 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:f924e8439ff6f1b8052bff03f9debd80 | 维持现有假设。这条回购信息来自 10-K 的一手披露，但只覆盖 2026 年 1 月到 3 月，属于半年前的资本配置记录。现有驱动全部落在收入、成本、费用和现金流占比上，没有一项涉及股本数量，所以这条信息改不动任何预测。DXC 目前也没有已建立的投资论点可以用它来验证。 这一季的回购在加速，均价在下降。2 月以 〔数字见研判记录〕 美元买入约 〔数字见研判记录〕 万股，1 月是约 〔数字见研判记录〕 万股、均价 〔数字见研判记录〕 美元 | 0 | 0 | B／C：旧回购表无新增信息可维持；需当时日期、授权≠执行、当前股数预算。 C：13 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:0ad30f88d35c4fdf5fd17a467759f59e | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:07e3143e64a332228d1baaf358764670 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| filing · NO_CHANGE/research · research-event:0d333d6a100f4334634a3a5e0b995eae | 暂时维持现有假设，先读 8-K 正文再判断。这份 8-K 是 DXC 在 10 月 8 日提交的一手文件，可信度最高，但输入里只有文件编号和报告类型，没有披露事项和数字，所以判断不了它影响的是盈利兑现、市场预期还是资本结构。 从时间上看，它不太可能是 FY27 第二季度业绩。该季度 9 月 〔数字见研判记录〕 日才结束，DXC 通常 11 月初发布季报，所以这份 8-K 更可能涉及高管变动、融资或债务安排、资产处置、重组，或者大合同和指 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：3 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/research · research-event:f45b5e5ef88a9a0bdf90178c5e235abb | 预测假设维持原状，但需要查明这次上涨的原因。2026-10-05 DXC 收盘 〔数字见研判记录〕 美元，前一交易日 〔数字见研判记录〕 美元，单日涨 〔数字见研判记录〕。同组 4 家可比公司平均跌 〔数字见研判记录〕，DXC 跑赢 〔数字见研判记录〕 个百分点。10 月 1 日和 2 日的涨跌都跟随板块，这一天的上涨只发生在 DXC 自己身上。DXC 从 9 月 〔数字见研判记录〕 日的 〔数字见研判记录〕 美元累计涨了约 〔数字见研 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：13 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/no_change · research-event:defb9114d0c13fe21c0f3029307a43de | 维持现有假设。2026-10-02 DXC 收盘 〔数字见研判记录〕 美元，前一交易日收盘 〔数字见研判记录〕 美元，单日跌 〔数字见研判记录〕。同组 4 家可比公司平均跌 〔数字见研判记录〕，DXC 只比同组多跌 〔数字见研判记录〕 个百分点。这次下跌主要来自板块整体回落，前一天板块同步上涨 〔数字见研判记录〕，今天回吐一部分，属于行业层面的估值和情绪波动。 这条证据只有市场价格，当天没有公司申报、指引调整或客户消息，看不出盈利兑现或 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：12 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/no_change · research-event:171d66b5f90988d7e6f5e8dd4bc5e746 | 维持现有假设。2026-10-01 DXC 收盘 〔数字见研判记录〕 美元，前一交易日 〔数字见研判记录〕 美元，单日涨 〔数字见研判记录〕。同组 4 家可比公司平均也涨 〔数字见研判记录〕，DXC 只跑赢 〔数字见研判记录〕 个百分点，这次上涨基本全部来自板块整体走强，属于行业层面的估值和情绪变化，DXC 自身没有新的信号。 这条证据只有市场价格，当天没有公司申报、指引调整或客户消息，看不出盈利兑现或市场预期有调整。现有驱动按 FY2 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：11 个数字占位，正文不可独立核验，需恢复实际值及引用。 |


### ACN

环境：legacy；版本：`mission-deliverable-version:f1a1b3ae9db58ab20e95e76f02251d3f`；生成：2026-10-09T00:01:16.685516+00:00；正文 refs／claim_refs 引用 2 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 19 条 · NO_CHANGE 19 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:359e57d96829df225638aade691e6bf5 | 收入预测和两条投资论点都维持原判。这条已核实结论来自公开网页的二手转述，讲的是 FY25 第三季度（截至2025年5月）的情况：收入同比增长、中东地缘紧张拖累业绩、股价当日大跌。这个季度已经过去一年多，实际数字早已在10-Q里申报，也已进入我们的收入基数，所以盈利兑现、预期修订和估值三方面都没有新信息。它也没有给出订单、可自由支配咨询需求或利润率数据，判断不了需求底部有没有到来。 同一文档里 Julie Sweet 说 AI 会成为公司 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:301b4ca311b429e453653509c34ae1b7 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| price_move · NO_CHANGE/note · research-event:a6c5cf047c78136ee3823fd2c7bb8fec | 记录最新收盘价〔数字见研判记录〕美元，后续收益空间评估应以此为市场价格基准，而不是提高估值目标。此次按绝对涨幅触发，材料未提供SPY同期收益，不能判断ACN是否跑赢市场；若需追查上涨原因，优先核对同期公司公告、盈利预期变化及行业相对表现。 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:f9feff0140121a9197ac6488200745d2 | 收入预测和两条投资论点都维持原判。这批已核实结论来自公开网页的二手转述，对应 FY25 第四季度和截至2025年8月〔数字见研判记录〕日的12个月，大约是一年前的业绩会。那一年的收入已经申报并进入我们的收入基数，所以盈利兑现、预期修订和估值三方面都没有新信息。CEO 说云、ERP、安全改造仍在进行，数据准备还处在早期；企业用 AI 和个人用 AI 差别很大。这两句是管理层一贯的定性说法，和需求底部尚未确认的判断一致，但都没有给出订单或可 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：2 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:e2ee7e0dfd136c5a6eac95d3d6626199 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:1819858c56f2045e3d0dc20fee52439e | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/research · research-event:a61124be12b09c23db695696cbdb4349 | 收入预测和两条投资论点暂时维持原判，但需要先用公司申报数据核实，再决定是否下调。最有分量的是同一文档里的一条已核实结论：Accenture 收窄了 FY2026 当地货币收入增长指引，砍掉了原区间的上限；同时新签订单同比下降，原因是客户推迟咨询决策。管理层还说，AI 在订单中的占比上升很快，但抵消不了传统数字化转型项目被推迟带来的缺口。这条信息让第一条投资论点（需求底部尚未确认）更站得住；第二条投资论点要求 AI 带来净增量收入，这条信 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：3 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/research · research-event:9f59093653fc08198d5d7ad82a461e7a | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/research · research-event:98ecbf4caa38b77f9991e4f8a94115c1 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/research · research-event:47234833488b65ed589dbfd2fa63f8ff | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:f94429dd7b4d3c98b3ade226e2a9dee2 | 收入、成本假设和两条投资论点都维持原判。这条事件说的是 2021 年 Accenture 证实遭勒索软件攻击、据报约 6TB 数据被窃。证据来自公开网页的二手转述，属于 derived 层级，性质是定性信息。事件已经过去约五年，对应年度的收入和成本早已申报，进入了我们的历史基数。现有信息里没有看到客户流失、合同索赔或订单受损的迹象，所以它对业绩兑现、预期修订和估值都没有新的含义。 同一文档里的另外两条已核实结论也逐条看过。2025 年有 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:c91cf3b43585f9908418475ef3c0e8ca | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:0d35c67ec7b12e5f1003a7f1526ff89e | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:3573c74f04be2daf307115dc2075f890 | 收入假设和两条投资论点都维持原判。管理层说，核心运营里的高级 AI 工作还不成熟，agentic AI 和 physical AI 都处在早期，回报要等技术成熟和宏观条件配合，所以兑现周期偏长。这是一条定性表态，来自二手转述，属于 derived 层级。它和第二条投资论点的前提一致：该论点本来就把 AI 抵消咨询疲软定为低置信度假设，并且以净增量收入为条件。这条表态只是重申 AI 的回报会来得晚，我们每季度约 〔数字见研判记录〕 的环比 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:dd51531ac6b682a2424dce27e3d1eb6d | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| transcript · NO_CHANGE/research · research-event:4c832ed77c3ae2739017db3abf909cb2 | 我们暂时维持收入、成本假设和两条投资论点，原因是这次事件只给了一份电话会转写稿的引用，没有附上管理层的具体表述或数字。从发生时间看，这份转写稿很可能是 FY26 第四季度业绩会。管理层直接表态是这类信息里证据等级最高的一层。第一条投资论点关心新签订单能否转成收入、可自由支配的咨询业务是否见底；第二条关心 AI 能否带来净增量收入、利润率能否守住。这份业绩会正好能回答这两个问题。 目前收入假设是每季度环比增长约 〔数字见研判记录〕，成本率 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 C：4 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/no_change · research-event:94d8dec1b2872a998a33852b0751c40e | 收入每季度环比增长〔数字见研判记录〕的假设和两条投资论点都维持原判，也不另开研究问题。10月5日 ACN 收于〔数字见研判记录〕美元，前收〔数字见研判记录〕美元，单日下跌〔数字见研判记录〕。同组4家公司同日平均上涨〔数字见研判记录〕，ACN 跑输〔数字见研判记录〕个百分点，刚过〔数字见研判记录〕的触发阈值。股价仍比9月〔数字见研判记录〕日的〔数字见研判记录〕美元高约〔数字见研判记录〕，业绩日后的涨幅保留了四成左右。 这只是市场价格证据， | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：12 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/research · research-event:4fb42432460f7ca2e61fe25e69aca6be | 收入每季度增长〔数字见研判记录〕的假设和两条投资论点保持不变，但这次回落需要和前一日的大涨放在一起查清。10月2日 ACN 收于〔数字见研判记录〕美元，前收〔数字见研判记录〕美元，单日下跌〔数字见研判记录〕。同组4家公司平均下跌〔数字见研判记录〕，ACN 跑输〔数字见研判记录〕个百分点。前一日 ACN 上涨〔数字见研判记录〕，这次跌幅回吐了其中约三分之一，收盘价仍比9月〔数字见研判记录〕日的〔数字见研判记录〕美元高约〔数字见研判记录〕。 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：14 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/research · research-event:aee0e007056e846a252f7ee5a43a8721 | 投资论点和收入每季度增长〔数字见研判记录〕的假设暂时不动，但这次波动要立刻查清原因。10月1日 ACN 收于〔数字见研判记录〕美元，前收〔数字见研判记录〕美元，单日上涨〔数字见研判记录〕。同组4家公司平均上涨〔数字见研判记录〕，ACN 跑赢〔数字见研判记录〕个百分点。之前几次波动都在〔数字见研判记录〕到〔数字见研判记录〕之间，大约一半来自板块，可以用仓位变化解释。这次幅度大了数倍，主要是公司自身原因，时间又落在截至2026-08-31的 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：10 个数字占位，正文不可独立核验，需恢复实际值及引用。 |


### EPAM

环境：legacy；版本：`mission-deliverable-version:f270edd40c4df50fca8a058d8afd0f3e`；生成：2026-10-09T00:01:16.673490+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 5 条 · NO_CHANGE 5 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:10d179941b9ca499fc022735ed4c30b9 | 首选判断是维持现有预测。两条已核实结论都来自 FY2025 10-K。第一条说汇率对 2025 年收入增速是正贡献，固定汇率增速低于报告增速，影响最大的是欧元、英镑、瑞郎和墨西哥比索。第二条说各行业垂直和三大客户地区收入都有增长。两条都只给方向，没有幅度，而且讲的都是已经入账的 2025 年。收入驱动项已经按公司申报的财报校准过，所以这里没有新的业绩兑现，也没有可以单独拿来修订预期的数字。 这两条结论的作用是提醒一个风险：2025 年报 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:1f4ec800d3c631ac83386a1e035abfdb | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| price_move · NO_CHANGE/no_change · research-event:0f4e9ab287e50dbb31b5b7e05df58149 | 首选判断是维持现有预测。10 月 2 日 EPAM 收于 〔数字见研判记录〕 美元，较前一日的 〔数字见研判记录〕 美元下跌 〔数字见研判记录〕。同组 4 家可比公司平均下跌 〔数字见研判记录〕，EPAM 只多跌 〔数字见研判记录〕 个百分点。前一日板块普涨时 EPAM 也基本随板块走，可见这两天主要是 IT 服务板块整体估值在波动。这次只有价格数据，没有公告、指引或订单信息，盈利是否兑现看不出来，也看不到市场单独下修 EPAM 的预期 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：7 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/no_change · research-event:582af8b3373d2582f6ab833293da749f | 首选判断是维持现有预测。EPAM 10 月 1 日收于 〔数字见研判记录〕 美元，较前收 〔数字见研判记录〕 美元上涨 〔数字见研判记录〕，但同组 4 家可比公司平均上涨 〔数字见研判记录〕，EPAM 跑输 〔数字见研判记录〕 个百分点。这次上涨是 IT 服务板块的整体行情，没有公司自身的新信息。本事件只有市场价格，没有公告、订单或指引数据，不涉及盈利兑现，也看不出市场对 EPAM 单独修正了预期，更像板块估值整体抬升。 现有收入假设是 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：8 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:8bac4c867a1670bf80861a05bd5210d3 | 首选判断是维持现有预测。这条已核实结论是 2026-05-19 管理层的定性表述：大单集中在存量客户，公司提到与现有客户签了十多笔交易，涉及 AI 原生、类 BPO 交付结构和智能体测试。它说明 AI 正在改变 EPAM 的交付方式，但没有给出合同金额、期限和收入确认节奏，内容也早于管理层下调 2026 年收入指引、预计第三季度收入环比下降的时间点。现有收入假设（2026-09-30 季度环比持平，之后每季度增长 〔数字见研判记录〕）已 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：2 个数字占位，正文不可独立核验，需恢复实际值及引用。 |


### CTSH

环境：legacy；版本：`mission-deliverable-version:af6e9d36997a4a329e0d739609afb6f7`；生成：2026-10-09T00:01:16.647838+00:00；正文 refs／claim_refs 引用 5 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 8 条 · NO_CHANGE 8 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| price_move · NO_CHANGE/research · research-event:5506eb59b83807d3ae7b46c30fa7a8ea | 暂不调整CTSH盈利预测，也不据此建立投资论点；当前首选解释是一次原因尚待核实的相对价格走弱，而非已经确认的经营恶化。2026年9月9日股价由〔数字见研判记录〕美元降至〔数字见研判记录〕美元，下跌〔数字见研判记录〕，四家公司篮子下跌〔数字见研判记录〕，CTSH跑输〔数字见研判记录〕个百分点。这是市场价格证据，不能直接映射为收入或利润变化。现有模型未来三个季度均假设收入环比增长〔数字见研判记录〕、营业成本占比〔数字见研判记录〕、销售及管理 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：13 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:bf2d46badda7c6f45f5167726414e9bc | 维持现有预测。这份10-K的回购表记录的是2025年11月的回购：公司在已公告的回购计划内买入〔数字见研判记录〕股，均价〔数字见研判记录〕美元，月末剩余授权额度〔数字见研判记录〕亿美元。同一份文件里，10月以〔数字见研判记录〕美元买入〔数字见研判记录〕股，12月以〔数字见研判记录〕美元买入〔数字见研判记录〕股，三个月合计约〔数字见研判记录〕亿美元。按这个节奏，剩余授权够用约〔数字见研判记录〕个月，规模约占市值〔数字见研判记录〕。这些都是 | 0 | 0 | B／C：旧回购表无新增信息可维持；需当时日期、授权≠执行、当前股数预算。 C：16 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:adb7aebc916dd6ed43cf75999aeb7cd6 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| buyback_disclosure · NO_CHANGE/no_change · research-event:717e925ff62bb4a6bb0df22e4017055c | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:866ff8db1795493a71d91f43333505d5 | 维持现有预测。这两条已核实结论都来自德意志银行2026-07-29的同一份报告，属于第三方观点，评论对象是Q2财报，到今天已经两个多月。德银给出“差行业里最好的公司”的评价，并维持买入评级，这是卖方对相对份额的判断，属于评级层面的信息，没有提供新的经营数据。另一条提到的事实来自公司自己披露的Q2数据和指引：签了7个大单，订单同比仍然收缩，部分原因是上年大单基数高；公司下调了FY26收入增速指引的上限，原因是可自由支配支出的复苏迟迟没有出 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：2 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:157b1eeb2dbca3cced6a3812dafa65e0 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| price_move · NO_CHANGE/no_change · research-event:e1ef5d96b20442e2d3abbfd9cd806474 | 维持现有预测。10月2日 CTSH 收于〔数字见研判记录〕美元，前一交易日收盘价为〔数字见研判记录〕美元，单日下跌〔数字见研判记录〕。同组4家可比公司平均下跌〔数字见研判记录〕，CTSH 比同组少跌〔数字见研判记录〕个百分点，所以这次是 IT 服务板块整体下跌，公司自身没有出现背离。这条事件只有市场价格，没有收入、订单或利润率数据，因此不涉及盈利兑现或预期修正，只反映估值和板块情绪。已核实结论里提到，2026年 CTSH 和 Accen | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：10 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| price_move · NO_CHANGE/no_change · research-event:e6496dc261650e860c02fe7fe55472b1 | 维持现有预测。CTSH在2026-10-01收于〔数字见研判记录〕美元，前收盘〔数字见研判记录〕美元，单日上涨〔数字见研判记录〕。同一天4只同业组成的篮子平均上涨〔数字见研判记录〕，CTSH反而落后篮子〔数字见研判记录〕个百分点，可见这次上涨是IT服务板块整体上涨，和Cognizant自身的消息关系不大。 这次事件只有行情数据，没有公司公告、指引变化或订单披露，收入、成本率、销售管理费用和股本这几项驱动假设都缺少新的依据。现行假设是收入 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：8 个数字占位，正文不可独立核验，需恢复实际值及引用。 |


### META

环境：hyperscalers；版本：`mission-deliverable-version:ebe65fe359b68732b9b702e125184ae6`；生成：2026-10-09T00:01:21.054955+00:00；正文 refs／claim_refs 引用 7 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 6 条 · NO_CHANGE 6 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:3753318175f705b18164b0f7a2bcce83 | 维持现有预测。这条已核实结论来自公司自己申报的财务数字，可信度高：2026 年一季度收入 〔数字见研判记录〕 亿美元，同比增长 〔数字见研判记录〕，上年同期为 〔数字见研判记录〕 亿美元。这个季度早已过去，二季度业绩会后的收入假设已经把它算进去了。现有模型从 2026-09-30 起按季度环比约 〔数字见研判记录〕 推算收入，这个增速正是以一季度及之后的实际数据为起点，所以这条结论没有带来新的盈利兑现信息，也没有改变市场预期或估值。 一 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：5 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:7fb216ead69061969dda35fd26c35855 | 维持现有预测。今天的四条输入都在讲 Meta 的开发者生态：开发者在 Meta Horizon 上做的内容可以直接发布到 Facebook 和 Instagram，Meta 也公布了围绕 Muse 的游戏计划，支持开发者给这两个平台做游戏。两位专家的访谈摘录也来自同一主题的内部资料。这些都是定性描述，属于转述层级的证据，没有收入、用户数、开发者数量或投入规模，因此没法落到季度收入环比约 〔数字见研判记录〕、研发增速或资本开支占比这些驱动 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:0b45c58075bd4f20df5325d73751c67f | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| expert_excerpt · NO_CHANGE/no_change · research-event:9a933cd1cc0a2fbc504a4f1761ae8805 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| expert_excerpt · NO_CHANGE/no_change · research-event:2c1534d68870851e55fc96d09358882f | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| price_move · NO_CHANGE/no_change · research-event:fb6d82c264e1564f39c3bd529e8a52a8 | 维持现有预测。META 在 2026-10-07 收于 〔数字见研判记录〕 美元，前一交易日收盘价为 〔数字见研判记录〕 美元，单日下跌 〔数字见研判记录〕。同组 3 家公司平均上涨 〔数字见研判记录〕，META 相对同组落后 〔数字见研判记录〕，刚好超过 〔数字见研判记录〕 的触发阈值。这次触发只有价格数据，没有公司公告、财务数字或卖方观点变化，也就看不出盈利兑现或市场预期有什么新变化。单日落后 〔数字见研判记录〕 左右，在大市值科技 | 0 | 0 | B：不因单日价格改收入是合理纪律；归因轮动／资金仍为推断，需基准和事件核验。 C：13 个数字占位，正文不可独立核验，需恢复实际值及引用。 |


### AMZN

环境：hyperscalers；版本：`mission-deliverable-version:52f429d444792891b05c1635f4119d97`；生成：2026-10-09T00:01:21.036797+00:00；正文 refs／claim_refs 引用 8 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 15 条 · NO_CHANGE 15 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| expert_excerpt · NO_CHANGE/no_change · research-event:8a3cb8ea258ceba317ef4b1cc71eb34f | 维持现状。这条事件是一份 Guidepoint 专家访谈摘录，属于专家网络层级的定性信息，归在竞争得失这一研究环节。输入里只有来源和文档引用，看不到摘录正文、具体的赢单或丢单客户，也没有份额或金额数字。因此它最多只能给已核实结论里的竞争判断补充一位从业者的观点，提供不了盈利兑现事实，也构不成预期修正。 与亚马逊竞争格局相关的已核实结论已经比较完整：巴西电商用零佣金和仓储补贴换取市场空间、Meta 尝试把算力从 AWS 和 Azure 迁 | 0 | 0 | B／C：定性单样本可以登记，不足作公司级增长事实；需样本、来源独立性与可量化指标。 |
| claim · NO_CHANGE/no_change · research-event:4a7040b1861e3cee08e3c7d2aa34f7a5 | 维持现状。今天这六条输入都是专家访谈里的定性说法，都没有经过公司披露核实。最具体的一条说 AWS 在这位专家所在经销商渠道的第三季度同比增速是双位数，并且比上季度加快。这只是一个经销商看到的局部样本，没有给出具体增速，口径和 AWS 公司报告的收入也不同，最多算第三方观察，代表不了第三季度 AWS 实际交出的业绩。 其余几条说的是 Bedrock 采纳增长快、自研芯片让 Bedrock 跑得更快更好用、Opus 输出速度领先、交易结构变 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:ceb0f72390711ff196b9fe44efeeef9f | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:3b2e0502c6ecba3857ed6829e22dcb2b | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:995d3b91e45b44dbfc1aa8e42248fe7a | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:534bb6a4cc28fa0253fc4207d87ff4e5 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:89b9ae138d1f62e00b605e2878704d58 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:b558b04b70ce926dd470a218e06b1a8e | 维持现状。今天六条输入都来自专家访谈的定性说法，属于二手推导，没有公司披露的数字。其中五条讲 Amazon 在巴西的打法：加快建物流中心、借助众包做履约、对新卖家免佣金并免除大部分仓储费，短期利润明显承压，本地化做得成熟后关税影响被对冲，以及 2021 年曾出现库存和负债问题。 巴西在 Amazon 国际零售分部里占比很小，公司也没有单独披露巴西的收入或利润，所以这些补贴和投入无法换算成对国际分部利润率的可量化影响。方向上，这些说法和现 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:31c11ec0026d8e0463b510f04028951f | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:c2570ef17a65285b8242ad83b8c98ad7 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:1690b10d4c4330ad6a43d86ce5bcfb70 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:930158b7ec6d04bacffa9147e99a98da | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:1e9b79cda5a6b129125d45cd97c13c43 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:9e6d8ffc82a93da9e97ea1300881d4b2 | 维持现状。两条输入都是专家访谈里的定性说法，属于推导层级。第一条说亚马逊因为广告是重要收入来源，率先封禁了 Meta 的个人助手 Muse。第二条说 Rufus 响应慢，而且只能在亚马逊站内使用，做不了跨平台比价。两条都没有给出公司披露的数字，也没有市场代理指标。 封禁 Muse 和现有已核实结论方向一致。摩根士丹利认为亚马逊和 Meta 谈判的核心是保护零售媒体业务，守住获客漏斗顶端的入口；亚马逊只有确信外部智能体取代不了自己时才会合 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:74e4de99295efa044d952480717eab5e | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |


### GOOGL

环境：hyperscalers；版本：`mission-deliverable-version:b8966207a9ee2be6bfb5efd742b9ef29`；生成：2026-10-09T00:01:21.045887+00:00；正文 refs／claim_refs 引用 10 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 17 条 · NO_CHANGE 17 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:4cee7d8913eb20fefdb0b54b02d67dbe | 维持现有判断。这份 Form 8-K 只是 Alphabet 美元高级票据发行交割的程序性披露，属于公司自己提交的文件，但内容只有档次结构（浮动利率加多个期限的固定利率），没有披露发行规模、票面利率和募集资金用途。现有驱动模型覆盖收入、成本、费用、经营现金流和资本开支，这次披露没有提供能修正其中任何一项的数字。 这笔发债最可能是在给高强度的 AI 和云基础设施资本开支补充资金。FY2025 10-K 里的资本开支、融资租赁还款和回购数据 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:8104a25efbba30f40c9dedf7059ceb00 | 维持现有判断，GOOGL 的驱动假设不做调整。这条信息来自销售纪要，内容是 2026-03-26 前后 Google 发布 Turboquant 压缩算法：在精度不受损的前提下，大幅降低生成 KV Cache 所需的内存。Micron 和 Sandisk 股价随即下跌。这件事属于技术层面的定性信息，市场的直接反应落在存储厂商身上，对 Alphabet 的收入、成本占比、费用增速和资本开支占比都没有给出可以量化的修正依据。 对 Googl | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| sales_note · NO_CHANGE/no_change · research-event:e975957edfa5cf033e9b56fe8dea50c0 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| sales_note · NO_CHANGE/no_change · research-event:37ec7196d5b2586a2304bdc144a5391b | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:e722383d7ccc24851eb62162f001bab8 | 维持现有判断，GOOGL 的驱动假设保持原样。这组输入都是从纪要和专家访谈整理出的定性结论，证据等级为 derived，其中没有一个可以直接写进模型的数字。管理层说算力限制会持续数年、资本开支还会逐年增加，这个方向已经包含在现有假设里：资本开支占比固定在 〔数字见研判记录〕，收入每季度增长 〔数字见研判记录〕，资本开支的绝对额会随之逐季上升。 借外部算力、增加贷款、按考核指标在年中追加预算，这三条更像是在讲融资方式和预算节奏，而且其中贷 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：4 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:14d05a17d106b132fca7fccefae924a1 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:c0d93816b35cf399e80c57119d1eb51c | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:1498118e9918b0f60d78f6681c506992 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:5d17d075e8004837f927977b604fa267 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:2c831bbb58dee5709fe6ba93a93fc854 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:357252110081c686f9d2b479d1c55a77 | 维持现有判断，GOOGL 的收入假设保持每季度增长 〔数字见研判记录〕。这条结论来自专家访谈的转述，证据等级为 derived。内容是 CEO 要求广告收入稳步增长、避免单季靠拉高广告加载率冲高，这与现有的平滑季度增速假设方向一致，也没有给出可以写进模型的数字。 同日的专家访谈原文和销售纪要属于同一批信息，其中降低 AI Overviews 广告价格、取消交易佣金、做个人 Agent 等内容都是定性描述，降价幅度和广告量的拉动效果都没有 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| expert_excerpt · NO_CHANGE/no_change · research-event:b4ad9a065a15b3bcd73ff035a02941cc | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| sales_note · NO_CHANGE/no_change · research-event:93325ef016410c7716d544a79f4cc39a | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:26fdcd4e4c3fad50a25b4f418a5e8728 | 维持现有判断，GOOGL 的资本开支占收入比例保持 〔数字见研判记录〕，收入保持每季度增长 〔数字见研判记录〕。美银分析师说 Google 正在密歇根州布局数据中心，这是卖方转述的选址信息，证据等级为 derived。它没有给出投资金额、容量和投产时间，单个园区也只是全年资本开支中的一部分，模型里没有哪个数可以据此修改。 同日另外三条输入讲的是同一件事：管理层说云业务继续扩张，AI 数据中心建设带动了传统云需求，客户急于锁定算力，折扣合 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 C：4 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| expert_excerpt · NO_CHANGE/no_change · research-event:b0de161cc60a5c83128d943b0d8addd6 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:db6b837ed1e16d0079a3312c97627768 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:6c26189cbdfc8904aa7f40d1350b0bc6 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |


### MSFT

环境：hyperscalers；版本：`mission-deliverable-version:d7454bb189ae741ac8214d95e64c066f`；生成：2026-10-09T00:01:21.062830+00:00；正文 refs／claim_refs 引用 6 条，其中检查时退休 0 条。

| 子事件 | 内容定位 | numbers 数 | gaps 数 | 评估 |
|---|---|---|---|---|
| 2026-10-08 事件研判日汇总（UTC）· 考察输入 16 条 · NO_CHANGE 16 条 | 当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；下面逐条列出输入与结论，不另发单条事件笔记。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/note · research-event:24fc251541fe9875fb77196c01e023f5 | 美银专家访谈显示微软的企业 AI 地位有两面：一面是 Azure 内置 OpenAI 和 Copilot Chat 打包进基础协议，加深了绑定；另一面是 Office Copilot 单独收费，部分客户也开始直接和前沿模型厂商签约。这组信号来自单一客户，属于定性信息，暂时只作记录。等 MSFT 建立投资论点后，应在更多大型企业专家访谈中核实「绕开微软直签」是否普遍，并对照季报中的 Azure 增速和 Copilot 席位数据。 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/note · research-event:a8d3fd00ebca4b2ad97b5f21ead72aa4 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/note · research-event:a4125be4754a98ae99872a2897cfc56a | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/note · research-event:cf13b8591ea3147b3f96e8f52f1e0015 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| expert_excerpt · NO_CHANGE/note · research-event:414b528128a11a32279ca8c4376f53b8 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| sales_note · NO_CHANGE/note · research-event:4037426291ccad43a62a1282ded209b1 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| transcript · NO_CHANGE/note · research-event:080b89e04a264649e34250132aaa6a3d | MSFT 没有已立项的投资论点和驱动模型，这份管理层电话会纪要暂时只登记、不触发修订。事件记录里没有纪要的具体内容，后续立项时应先读这份纪要，提取 Azure 增速、资本开支和商业 RPO 的管理层指引。这些数字和 FY2025 10-K 中的尚未起租租约、自有算力扩张信息一起，构成搭建驱动模型的起点。 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 |
| transcript · NO_CHANGE/no_change · research-event:2911cedbdbd790f6ddea52f53faaee4d | 当前判断：维持原状。这份电话会纪要来自管理层直接表述，是这家公司证据层级最高的一类定性材料。事件记录只给了文档编号，没有附上纪要里的数字或原话，所以看不出它对应的是业绩兑现、预期修正还是估值变化。MSFT 目前也没有已立项的投资论点和驱动模型，纪要内容即使读到，也没有可以对照修订的假设。 已有的已核实结论分成两条线。公司口径的一条来自 FY2025 10-K 和业绩材料：Microsoft Cloud 和 Azure 带动收入增长；尚未 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 |
| news · NO_CHANGE/note · research-event:88512782aafa1549f321b66a4cf3e93c | 印度媒体两篇管理层变动报道只有文档编号，看不到正文，从来源看较可能是微软印度区或营销条线的人事调整。MSFT 尚无投资论点和驱动模型，这条信息对应不到任何假设，所以维持原状。如果 SEC 出现执行官变动的 8-K，或者确认变动涉及 Azure、AI、Copilot 业务的负责人，需要重新评估。 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 |
| news · NO_CHANGE/note · research-event:2a88d1996a60f4f3b1a0b93dbea7dd11 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| transcript · NO_CHANGE/research · research-event:973ab9abeece25819cdaa856743df6cf | 当前判断：维持原状，但需要先读这份纪要正文再下结论。这份电话会纪要来自管理层直接表述，是 MSFT 证据层级最高的定性材料。事件记录只有文档编号，没有附上纪要里的数字或表述，所以分不清它对应的是业绩兑现、预期修正还是估值变化。 这家公司已经连续两次对电话会纪要给出“维持原状”的结论，依据都是看不到正文。同一缺口如果反复出现，管理层表述就会一直进不了研究。MSFT 目前还没有已立项的投资论点和驱动模型。已核实结论集中在 Azure 带动的 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| transcript · NO_CHANGE/research · research-event:f0148b58b2dc7156f8a877583646f8ec | 当前判断：维持原状，同时安排研究，先读这份纪要正文。这份电话会纪要是管理层直接表述，在 MSFT 的定性材料里证据层级最高。事件记录只给了文档编号，没有附上数字或原话，所以分不清它对应的是业绩兑现、预期修正还是估值变化。 同一类纪要已经连续多次以看不到正文为由维持原状。这个缺口不补，管理层口径就一直进不了研究。MSFT 目前没有已立项的投资论点，也没有驱动模型。已核实结论集中在 Azure 带动的云收入增长、资本开支和自有算力比例上升、 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 C：1 个数字占位，正文不可独立核验，需恢复实际值及引用。 |
| claim · NO_CHANGE/no_change · research-event:f9e680aca6da89fd18b9d3c04035b5b6 | 当前判断：维持原状。这组材料来自 Wells Fargo 的卖方解读和媒体报道，属于第三方估计。核心意思是微软长期会把更多算力放在自有站点里，到 2032 年自有算力规模达到现在的三倍以上，自有和租赁的比例转向以自有为主。这个方向和已核实结论一致：FY2025 10-K 披露了 FY2026 至 FY2031 陆续起租的数据中心租约，资本开支在加码，股息增幅放缓也被解读为给资产负债表留出余地。所以这批材料只是重复已知趋势，没有带来新的公 | 0 | 0 | B／C：维持假设需按信息新增量判断；没有经营数的单日价格不能说明盈利变化。 |
| claim · NO_CHANGE/no_change · research-event:896a3c392a483280e1ea42c14329c46c | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:c1a3f5d9c7a1cb1ee393e92989aff8b8 | 与同组主事件一并研判，结论与主事件相同。 | 0 | 0 | C：有去重意图，但必须给出主事件 ID／链接与共同依据；当前条不能独立核查。 |
| claim · NO_CHANGE/no_change · research-event:13b44c834528ad06b1f09bb015f62ea2 | 当前判断：维持原状。这条事件是公司在 FY2026（截至 2026-06-30）财报中的定性表述：收入增长，主要来自 Microsoft Cloud；Intelligent Cloud 收入增长，主要来自 Azure；调整后净利润和调整后稀释 EPS 是剔除 OpenAI 投资净损益的非 GAAP 指标。证据层级是公司口径，但这条记录没有附任何数字，只能确认增长方向和增长来源。它属于业绩兑现的定性确认，看不出预期修正或估值变化。 这些内 | 0 | 0 | C：证据不足且未读原文，正确动作是安排读取；不能计作已充分审阅且无影响。 |


## 九份催化日历：每个条目

subject 中 q3／q4 可能指发布日期所在日历季，而非报表 fiscal period。应明确展示两者，避免前瞻 period 无法解析；本报告不据标签直接断言公告日期错误。未独立逐个确认未来日历日期。

### ACN

环境：legacy；版本：`catalyst-calendar-version:c1bbb04764d7f9a10ace306371ae4f3d`；生成：2026-10-08T08:04:23.743494+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:72fb191a1081f313ba2c81cbbecd0f36 | 2026-06-18 | 2026q2 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:c5b40036a18f709087cfac16955f3a0e | 2026-10-01 | 2026q4 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:b16be7380d5effb946fb38d5f4cf2871 | 2026-10-12 | 2026q4 | ex_dividend | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |
| catalyst-entry:2214dd90aee1778e6261b019ce0e4d34 | 2026-12-17 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### CTSH

环境：legacy；版本：`catalyst-calendar-version:24a5a54ee5568d9352c18df8b52bf7e9`；生成：2026-10-02T14:16:43.796968+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:274f36beb5ab5f3c7e0b2d5f7b5d507d | 2026-07-29 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:1e0c55f8e9895d593c9c401750dab5b1 | 2026-10-28 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### EPAM

环境：legacy；版本：`catalyst-calendar-version:417f913f7bfc1c980e807095b4352e32`；生成：2026-09-14T09:17:07.256142+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:0cf463c49ed72ebd2928d16e72587ccb | 2026-08-06 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:eb220d8328ba8365d8540662c3dfd2a9 | 2026-11-05 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### DXC

环境：legacy；版本：`catalyst-calendar-version:739c1aaf827b3c6f73873032b38e1a36`；生成：2026-09-14T09:22:25.811266+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:fddad23723a359d2ae8ff94a4ca76f14 | 2026-07-30 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:117a53919b3300eb35d6f89edb72a10a | 2026-10-29 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### IBM

环境：legacy；版本：`catalyst-calendar-version:420d9deff56f10c8d03620adbe0eca7b`；生成：2026-09-14T09:27:51.368243+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:bc24da496500e3c93a52a58295e02dc9 | 2026-07-22 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 来源日期有分歧，应保留不同来源和解决规则，不掩盖冲突。 |
| catalyst-entry:014e70e5b1ac77f610a88ba5d8aadfaa | 2026-10-21 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### AMZN

环境：hyperscalers；版本：`catalyst-calendar-version:392e01ac3e53abcd3654d7b9b39f1c51`；生成：2026-09-28T10:51:42.464402+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:792feea9b2a0366ade9e987fdf20e954 | 2026-07-30 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:b0545d247deb6b4d8a8190e60c075c08 | 2026-10-29 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### GOOGL

环境：hyperscalers；版本：`catalyst-calendar-version:deb4e0efbb63797075bcf76500753641`；生成：2026-09-28T10:56:17.067847+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:42308fb887c03a1da77358b4aa23f2f2 | 2026-07-22 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:f7538e8e255ee5ffa6b903ebd5f5ee1e | 2026-10-28 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### META

环境：hyperscalers；版本：`catalyst-calendar-version:7acb586bb0ca7182b600d1160f9a7936`；生成：2026-09-28T11:01:33.517809+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:4501e371d890f1336c55337de34b71fc | 2026-07-29 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:e1a09d996ed07e9791ce6b1e41718e73 | 2026-09-20 | 2026q3 | ex_dividend | estimated | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:f1d6f18a0d6d9e1e83c2336e35d99cee | 2026-10-28 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


### MSFT

环境：hyperscalers；版本：`catalyst-calendar-version:fb2ae2dd553a27dd18b8cc5a5878e5ad`；生成：2026-09-28T11:06:30.517011+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 日期 | subject | 类型 | 置信度 | 评估 |
|---|---|---|---|---|---|
| catalyst-entry:ecc1ffda1582994a55b8e242a9346b54 | 2026-07-29 | 2026q3 | earnings | confirmed | C：日期已过，应附已发生结果／兑现记录，不作未发生催化。 |
| catalyst-entry:e44d0ed8554fbf4f2f130072d00cfbd2 | 2026-10-28 | 2026q4 | earnings | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |
| catalyst-entry:92484b1e7b23e422bf03fbfdecc5f3ed | 2026-11-18 | 2026q4 | ex_dividend | estimated | B／C：需公司 IR 一手确认，estimated 只作规划；事件→读数→假设影响需显示。 |


## 正式 thesis 与 revision 候选

当前正式 thesis 只有 industry 和 ACN。没有正式 thesis 的公司不等于没有研究，但 outcome 检查不能凭空评估“论点方向”。候选不是已采纳 authority；本报告不凭候选存在推断待审批。

### thesis_versions thesis:us-it-services:demand-bottoming

环境：legacy；版本：`thesis-version:5db84d8c54f64fc2be92dd63e0eb4d34`；生成：2026-09-14T13:46:04.369701+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 字段 | 内容 | 评估 |
|---|---|---|
| catalyst_refs | ['catalyst:quarterly-sec-filings', 'catalyst:quarterly-earnings-calls'] | C：需指向有效证据、可量化催化／证伪与期限。 |
| claim_refs | [] | C：需指向有效证据、可量化催化／证伪与期限。 |
| confidence | low | C：需指向有效证据、可量化催化／证伪与期限。 |
| mechanism | New bookings lead revenue by two to four quarters. A bookings mix shifting toward managed services and AI-led reinvention, alongside decelerating discretionary consulting declines, stabilizes aggregate growth before it recovers. | C：需指向有效证据、可量化催化／证伪与期限。 |
| statement | A US IT services demand bottom remains unconfirmed. Accenture's soft discretionary spending and roughly flat short-term project-based growth do not establish an industry recovery over the next two to four quarters; that recovery remains conditional on broader demand stabilization and demonstrated bookings conversion. | C：需指向有效证据、可量化催化／证伪与期限。 |


### thesis_versions thesis:acn:ai-reinvention-growth

环境：legacy；版本：`thesis-version:108925ad079741d38f0f912444648d2a`；生成：2026-09-14T13:46:06.691640+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 字段 | 内容 | 评估 |
|---|---|---|
| catalyst_refs | ['catalyst:acn-quarterly-results'] | C：需指向有效证据、可量化催化／证伪与期限。 |
| claim_refs | [] | C：需指向有效证据、可量化催化／证伪与期限。 |
| confidence | medium | C：需指向有效证据、可量化催化／证伪与期限。 ACN 文本低置信度与 confidence medium 应统一。 |
| mechanism | AI-led programs create consulting work and follow-on managed-services demand; bookings conversion drives revenue while utilization and delivery productivity protect operating margin. | C：需指向有效证据、可量化催化／证伪与期限。 |
| statement | AI-led modernization and integrated delivery give Accenture a credible route to offset weak discretionary consulting, but sustained mid-single-digit local-currency growth with preserved operating margin remains a low-confidence hypothesis, contingent on net incremental revenue, retained delivery savings and successful restructuring. | C：需指向有效证据、可量化催化／证伪与期限。 ACN 文本低置信度与 confidence medium 应统一。 |


### thesis_revision_candidates ACN

环境：legacy；版本：`thesis-revision-candidate:2d996c502ef7c5d68a5bb405e006eb28`；生成：2026-10-01T14:01:47.666719+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 字段 | 内容 | 评估 |
|---|---|---|
| proposed_confidence | None | C：需指向有效证据、可量化催化／证伪与期限。 |
| proposed_statement | None | C：需指向有效证据、可量化催化／证伪与期限。 |


### thesis_revision_candidates ACN

环境：legacy；版本：`thesis-revision-candidate:6cfff570ca69112df5276f37894bd9b1`；生成：2026-10-01T14:01:47.667122+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 字段 | 内容 | 评估 |
|---|---|---|
| proposed_confidence | None | C：需指向有效证据、可量化催化／证伪与期限。 |
| proposed_statement | None | C：需指向有效证据、可量化催化／证伪与期限。 |


## 五份 zero-base review：逐检查项

生成于 9/11–9/14；ACN／DXC 为 yes、EPAM unclear、IBM／CTSH no。ACN／IBM／CTSH 下一核验 9/14 已过，EPAM／DXC 是 10/11 尚未到。是否形成观点与正式 thesis 采纳并非同一状态，不能混算。

### ACN

环境：legacy；版本：`zero-base-review-version:537cfcba7cd0e1c560c245ad860cc298`；生成：2026-09-11T12:42:46.913370+00:00；正文 refs／claim_refs 引用 6 条，其中检查时退休 1 条。

form_a_view=yes；next_verification=2026-09-14

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': "I would form a low-confidence, conditional view on Accenture's ability to benefit from AI, not the existing medium-confidence view on sustained growth and margin protection. AI-led financial-services work provides evidence of demand, but soft discretionary spending, acknowledged deflation and restructuring risk leave the aggregate revenue and profit offset unproven.", 'citations': ['thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5', 'thesis-version:00482bf2e3854c158c532677eee24933', 'claim-version:7408568e8b80c33dd1929e9b374d31d007b29701239d128378c8d669c4a37347', 'claim-version:2fbb3874041694c662286f992cadfcc0868295846f004e4edfb5509fa1ccaa73', 'claim-version:ffcc89e1e63f317d8f11d3f5ec7f3dac951cfded54bc78190695a1ed99e0aeb3', 'claim-version:230d568987b87538743ed4d894f9ef739785ea4… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-09-11 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:us-it-services | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:us-it-services:14 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 93070, 'invocation_ref': 'invocation:67fa6f9652977be937e11c94b69deac5', 'route_decision_ref': 'route-decision:8525e18c112e4f498949a74f48f41d44', 'work_order_ref': 'work:cockpit-zero_base_review-8326becd8049b3b6ef86da5568150f3d'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': "会建立观点。I would form a low-confidence, conditional view on Accenture's ability to benefit from AI, not the existing medium-confidence view on sustained growth and margin protection. AI-led financial-services work provides evidence of demand, but soft discretionary spending, acknowledged deflation and restructuring risk leave the aggregate revenue and profit offset unproven.", 'heading': '一、今天第一次看，会不会建立观点'}, {'body': "- thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5（THESIS_WEAKENED）：A US IT services demand bottom remains unconfirmed. Accenture's soft discretionary spending and roughly flat short-term project-based growth do not establish an indu… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 0} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-09 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:84132f3e540e887c5a3d2aa51453c8e7 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'openai-gpt-6', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2625, 'invocation_ref': 'invocation:ab530f54041413036015f9fd037d9e59', 'route_decision_ref': 'route-decision:a1425449819b4d088cb2fa6b0b044b7f', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-aa6ab56d01a139235c1a390965b183e5'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### EPAM

环境：legacy；版本：`zero-base-review-version:76edcdb2faf3f0a99d4dbb50233cb24a`；生成：2026-09-11T12:43:24.537784+00:00；正文 refs／claim_refs 引用 5 条，其中检查时退休 0 条。

form_a_view=unclear；next_verification=2026-10-11

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': 'From a standing start, this file supports investigating EPAM, but not a directional demand-recovery view as of 2026-09-11. The only quantified revenue observation is for Q2 2025 and does not separate acquisitions, currency, or underlying engineering volume; the remaining claims show AI-related opportunity alongside selling execution problems and substitution risk.', 'citations': ['thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5', 'claim-version:2005a2babe1e23f4c1259657330151d9c3f411f192d2576b1da80ab8c1c670c0', 'claim-version:f427bcd7f3406fec4357b9c2efef275c43ec516eb0d72b28b3661a35e327e6ab', 'claim-version:fff0da032e7149642a792a341ab8445f30161f01e0df7a87efd8dd5a1e25d421', 'claim-version:fb5dbf95d8335a5b5f3b784d9a378c45d8a12df78b2b8f1167a36bb51700faf6', 'claim-version:f236ee2826… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-09-11 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:us-it-services | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:us-it-services:14 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 80580, 'invocation_ref': 'invocation:61a182d04141f4588f1826e090fed294', 'route_decision_ref': 'route-decision:956efc6c6df647cd947b835da98b7616', 'work_order_ref': 'work:cockpit-zero_base_review-cf35b0545889e1554ec8b0e1dd97f615'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '说不好。From a standing start, this file supports investigating EPAM, but not a directional demand-recovery view as of 2026-09-11. The only quantified revenue observation is for Q2 2025 and does not separate acquisitions, currency, or underlying engineering volume; the remaining claims show AI-related opportunity alongside selling execution problems and substitution risk.', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': "- thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5（THESIS_WEAKENED）：EPAM's demand bottom remains unverified. AI-led modernization is changing demand mix, but North American selling execution problems and Software/Hi-Tech weakness prevent… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 0} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-09 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:e655f792b026c7b9e78ebc2ae37301be | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'openai-gpt-6', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2359, 'invocation_ref': 'invocation:d24416c55ca72f363eb58403bb354765', 'route_decision_ref': 'route-decision:b047e2b10d004741af9d3728f65e11eb', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-811eda955a17ace93f9c263a8f2317f2'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### DXC

环境：legacy；版本：`zero-base-review-version:d55a3bc50f5889fe34366ff29eb73d28`；生成：2026-09-11T12:48:16.292381+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 1 条。

form_a_view=yes；next_verification=2026-10-11

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': "The file supports a low-confidence, company-specific view of a possible slowing in DXC's revenue decline, not an industry recovery thesis. FY27 guidance still calls for a mid-single-digit revenue decline with second-half improvement, while low-single-digit organic growth appears only as an analyst's FY29 expectation. AI partnerships provide potential upside but do not establish incremental revenue or retained productivity savings.", 'citations': ['thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5', 'claim-version:c462a7c06036f7aad98c97328ecfa3b92df0e1b1ba7a9dd6d16410b3cf6ea27d', 'claim-version:051af1f0c5cca58bdd9a660c5a676e3bd3746acd76d56c06579026ae78ef44d1', 'claim-version:6bf07eab13e5f40aeed3bfccf231d533e238d1231a92292b87f0a2ba854500be', 'claim-version:978b0ec5d0c198f95ebacfb3… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-09-11 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:us-it-services | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:us-it-services:14 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 79240, 'invocation_ref': 'invocation:e6d0c6e315d0c239e851ff3453b3db9b', 'route_decision_ref': 'route-decision:f2594bce63624c398bc6ab76ebc33dbb', 'work_order_ref': 'work:cockpit-zero_base_review-2829862d6c6656f25ba47c33c809776e'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': "会建立观点。The file supports a low-confidence, company-specific view of a possible slowing in DXC's revenue decline, not an industry recovery thesis. FY27 guidance still calls for a mid-single-digit revenue decline with second-half improvement, while low-single-digit organic growth appears only as an analyst's FY29 expectation. AI partnerships provide potential upside but do not establish incremental revenue or retained productivity savings.", 'heading': '一、今天第一次看，会不会建立观点'}, {'body': '- thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5（THESIS_WEAKENED）：DXC may experience moderating revenue declines in the second half of FY27 as legacy contract-termin… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 20} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-09 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:1b34c37075f12002fc67e61363c21162 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'openai-gpt-6', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2219, 'invocation_ref': 'invocation:d6f5df9c4dd753fec706deb355425662', 'route_decision_ref': 'route-decision:631d05e548c1413a8db2b6a464077a0d', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-274dd275a9913b97f709624d67895195'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### IBM

环境：legacy；版本：`zero-base-review-version:2a717fa4f8aae6cc07730602862a2bf4`；生成：2026-09-11T15:42:18.898207+00:00；正文 refs／claim_refs 引用 4 条，其中检查时退休 0 条。

form_a_view=no；next_verification=2026-09-14

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': 'From this file alone, I would put IBM on a research watchlist rather than form a directional view as of 2026-09-11. The reported growth observations concern consolidated revenue in 2024 and 2025, not current Consulting demand, backlog conversion or margins; enterprise AI and hybrid-cloud positioning provide a hypothesis, not evidence of an industry recovery. The GitLab and unattributed Flex claims do not establish anything about IBM Consulting.', 'citations': ['thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5', 'claim-version:44d630bbcbe019f97d3885d3255fb71f4a8cc6c439d4a1fb0504baafd93963d2', 'claim-version:ab1409765a083e91f98825bb4ec65a3f3a7d41643433c07efbe887ae6990721d', 'claim-version:f2a89011822c98c974ee1c0e7de135c88b109af0059ee76b7435643a29f6dc2b', 'claim-version:fa7dbbaa0b… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-09-11 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:us-it-services | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:us-it-services:14 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 80310, 'invocation_ref': 'invocation:e8033c2e96299b35d64f31d9d7c7bb76', 'route_decision_ref': 'route-decision:a115f7e339104a34858038f62890cae4', 'work_order_ref': 'work:cockpit-zero_base_review-c156fa7a8fa5b87ed9ccab8aefb63327'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '不会建立观点。From this file alone, I would put IBM on a research watchlist rather than form a directional view as of 2026-09-11. The reported growth observations concern consolidated revenue in 2024 and 2025, not current Consulting demand, backlog conversion or margins; enterprise AI and hybrid-cloud positioning provide a hypothesis, not evidence of an industry recovery. The GitLab and unattributed Flex claims do not establish anything about IBM Consulting.', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': "- thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5（THESIS_WEAKENED）：IBM's enterprise AI and hybrid-cloud positioning could support Consulting demand, bu… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 45} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-09 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:ead27cb72afef03975a4b4cfe61df6e9 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'openai-gpt-6', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2236, 'invocation_ref': 'invocation:f2d0d23f42444e567b88990ea0a68ae3', 'route_decision_ref': 'route-decision:0a49f50b40f14db3acc55ba28dc29b81', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-28f59e1985e0a25eec004b9115ec7268'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### CTSH

环境：legacy；版本：`zero-base-review-version:f3e4f962a909be60cc034e3948373263`；生成：2026-09-14T17:16:09.667703+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

form_a_view=no；next_verification=2026-09-14

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': '从零开始，现有材料不足以对 Cognizant 形成有方向性的投资判断。已核实结论主要涉及管理层任命、财务负责人交接及收入披露分类，缺少能够检验增长和盈利质量的实际数据；当前也没有可供改写的投资论点。三项争议分析仍涉及决定性问题：AI 生产率收益能否在续约后保留、大单能否转化为增量收入、AI 付费需求能否抵消传统业务流失；现有材料既未显示双方已形成共识，也未提供足以消除争议的数字。', 'citations': ['claim-version:4498be042ae41a9a3abcb67e340669a206fb44a3b51c05c3c6d417544210635b', 'claim-version:5451ccdbb7f808b952ae6051f94964a046842a6f4aaf36e4f15b5f9e57a65258', 'claim-version:f67c50a0236242c9739016f37b68d60403b9588a691f6b09ce8f03754eb4c539', 'debate:2c9376710faaefd342f4aa8e', 'debate:a6c9bc0b64af6f149c665fdc', 'debate:d97d2f5ac9223c0fc9566d07'], 'form_a_view': 'no', 'next_verification': {'because': '这是安排的资料核验日期，不是已确认的财报发布日期。现有收入分类提供了分析服务与合同结构的入口，但尚未回答三项核心争议；应先补齐截至今日可获得的经营证据，再决定是否形成投资论点。', 'date': '2026-09-14', 'what': '核查截至复核日已公开的最新财报及业绩说明，寻找大单签约后两至四个季度的收入兑现情况、AI 项目的增… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-09-14 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:us-it-services | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:us-it-services:18 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 57700, 'invocation_ref': 'invocation:54c91fb42fec18410ea18ed115860538', 'route_decision_ref': 'route-decision:7b65a2b555204778b1325a0859b7178f', 'work_order_ref': 'work:cockpit-zero_base_review-823bdc616ab1286862ab8ad99b829f81'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '不会建立观点。从零开始，现有材料不足以对 Cognizant 形成有方向性的投资判断。已核实结论主要涉及管理层任命、财务负责人交接及收入披露分类，缺少能够检验增长和盈利质量的实际数据；当前也没有可供改写的投资论点。三项争议分析仍涉及决定性问题：AI 生产率收益能否在续约后保留、大单能否转化为增量收入、AI 付费需求能否抵消传统业务流失；现有材料既未显示双方已形成共识，也未提供足以消除争议的数字。', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': '没有一条会被重新写。', 'heading': '二、现有 thesis 哪几条会被重新写'}, {'body': '现在开着的争论都还重要。', 'heading': '三、哪些争论已经不重要了'}, {'body': '2026-09-14：核查截至复核日已公开的最新财报及业绩说明，寻找大单签约后两至四个季度的收入兑现情况、AI 项目的增量付费收入，以及续约让利和配套技术成本对营业利润率的影响；区分已披露数据、管理层预期和仍未披露的指标。\n因为：这是安排的资料核验日期，不是已确认的财报发布日期。现有收入分类提供了分析服务与合同结构的入口，但尚未回答三项核心争议；应先补齐截至今日可获得的经营证据，再决定是否形成投资论点。', 'heading': '四、下一个验证点与日期'}], 'subject_ref': 'company:sec-cik:0001058290', 'summary': 'com… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 0} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-09 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:91915b285ef84c9188dbedff4c268231 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'openai-gpt-6', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 1563, 'invocation_ref': 'invocation:4b1da89b44418d7924793acaf5e4c622', 'route_decision_ref': 'route-decision:b5f1044f4c5f480198f3222c834fe114', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-d489e6699c501b90083a904766c33852'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### META

环境：hyperscalers；版本：`zero-base-review-version:732a9e620d48b0613f378f83dbf3a6f6`；生成：2026-10-03T04:34:11.858550+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

form_a_view=yes；next_verification=2026-10-28

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': '会形成看法。档案里的已核实结论有两条主线，足以从零起步写出投资论点。第一条：广告转化增速快于展示量增速，排序和召回模型的改进是主要驱动，管理层预计 2026 年余下时间推荐系统还会继续提升互动（claim-version:0b99589b…、claim-version:b96afdc4…）。第二条：管理层押注全栈前沿模型和基础设施，同时没有给出 2027 年资本开支指引，还在评估最优组织规模（claim-version:ecbbdc4a…、claim-version:85d4107d…、claim-version:a95779b9…）。所以今天的初始看法是：核心广告引擎有一手证据支撑，回报端证据较扎实；AI 投入规模和回报周期是主要的未知数，需要拿下一次财报检验。目前没有已存档的投资论点，因此改写列表为空。', 'citations': ['claim-version:0b99589b13e3f7f6a7744db2721b925d0fd0dae402cee19c84bcc258e871a710', 'claim-version:b96afdc4d28003fe82e767c939cee40d79360f3300b3eab5c3f4d940e49c5989', 'claim-version:ecbbdc4ae633709f36708245babe411ca577e4858d2ef6f041871fc806b179dd', 'claim-version:85d4107dc34e9e33fc60cb885a5ac5bee2624f4265f15ca25afa839243f1d7c2', 'claim-version:a95779b9d31b6e5835db321fdb46faa40bf853bf576eef006275b28735d8a5ec', 'c… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-10-03 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:ws-7d894366d1132e2930475a60 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:ws-7d894366d1132e2930475a60:8 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 262852, 'invocation_ref': 'invocation:e9c7ac1deb1ff4b732dd5c4c12aff2eb', 'route_decision_ref': 'route-decision:7db637319ba14ab79473a19f16662e4e', 'work_order_ref': 'work:cockpit-zero_base_review-c17ef0d20f68f0be7b823ed476fa077f'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '会建立观点。会形成看法。档案里的已核实结论有两条主线，足以从零起步写出投资论点。第一条：广告转化增速快于展示量增速，排序和召回模型的改进是主要驱动，管理层预计 2026 年余下时间推荐系统还会继续提升互动（claim-version:0b99589b…、claim-version:b96afdc4…）。第二条：管理层押注全栈前沿模型和基础设施，同时没有给出 2027 年资本开支指引，还在评估最优组织规模（claim-version:ecbbdc4a…、claim-version:85d4107d…、claim-version:a95779b9…）。所以今天的初始看法是：核心广告引擎有一手证据支撑，回报端证据较扎实；AI 投入规模和回报周期是主要的未知数，需要拿下一次财报检验。目前没有已存档的投资论点，因此改写列表为空。', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': '没有一条会被重新写。', 'heading': '二、现有 thesis 哪几条会被重新写'}, {'body': '现在开着的争论都还重要。', 'heading': '三、哪些争论已经不重要了'}, {'body': '2026-10-28：2026 年第三季度财报及电话会：2027 年资本开支有没有给出初步区间；广告展示量与单价的拆分，以及转化增速是否继续快于展示量；Business Agent 和 WhatsApp 新消息类别有没有给出可计量的收入。\n因为：这次财报会同… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 1} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-10 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:6ef49ccb604c769e4e8e7debc3102de1 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'anthropic-claude-5', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2064, 'invocation_ref': 'invocation:23c67269a13903210ed454347c1d70b5', 'route_decision_ref': 'route-decision:ff895eb0c5c64d4092495cc1072bc1b6', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-e0d51e3f3f2927e5c8f91393f76fa520'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### AMZN

环境：hyperscalers；版本：`zero-base-review-version:584d1b497d775c8936f3eaafcfe4255e`；生成：2026-10-07T20:54:50.616309+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

form_a_view=yes；next_verification=2026-10-29

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': '仅凭现有材料，从零开始也值得形成观点，主线是 AWS 产能扩张带来的收入加速。多份卖方报告把 Project Rainier 和管理层“2027 年底容量翻倍”的表态，视为 2026 年 AWS 增速加速和盈利预测上修的主要依据。2026 年第二季度，非 AI 的核心 AWS 业务仍占 AWS 收入的绝大部分，而且同比增速加快。零售端营收和利润率保持稳健，广告业务在支撑利润率。9 月亚马逊发行了多期英镑优先票据，说明资本开支对现金流的压力正在落到融资层面。因此，现金流与资本开支是需要重点盯的争议，但不妨碍形成观点。档案中目前没有投资论点，所以没有需要改写的条目。', 'citations': ['claim-version:06475335c7429af8ae3c5c0bd68859e9a8dca108bd2afec108f25de5368c2faf', 'claim-version:a54b1e61f8339f2af95c02c3e66f75567534e4aeb2623af5dd1ddc8efee8aedb', 'claim-version:561e51b923b6f77c16c9447d30348adf137df51c57ae4f92d4d083d48dd1967b', 'claim-version:06ad5d1b49f583804c982594b2245d191fa75f219230f78b03b8af73baa42395', 'claim-version:8bdec5f6c6d43400ed195f970231278046856e52d41a2d54b47cef6ebb9ad44e', 'claim-version:88e318704b29ef6b5fdc00dd87f7d461995dac11f2a7a4abdb89cc27ec8c1… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-10-07 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:ws-7d894366d1132e2930475a60 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:ws-7d894366d1132e2930475a60:8 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 270780, 'invocation_ref': 'invocation:05f9e32c1fa4801cbc4a0dd5eca779d3', 'route_decision_ref': 'route-decision:6d94a95fdf644c769d0368295b1753f6', 'work_order_ref': 'work:cockpit-zero_base_review-aafd4ca15ab4c9d0143692bd40c76c5b'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '会建立观点。仅凭现有材料，从零开始也值得形成观点，主线是 AWS 产能扩张带来的收入加速。多份卖方报告把 Project Rainier 和管理层“2027 年底容量翻倍”的表态，视为 2026 年 AWS 增速加速和盈利预测上修的主要依据。2026 年第二季度，非 AI 的核心 AWS 业务仍占 AWS 收入的绝大部分，而且同比增速加快。零售端营收和利润率保持稳健，广告业务在支撑利润率。9 月亚马逊发行了多期英镑优先票据，说明资本开支对现金流的压力正在落到融资层面。因此，现金流与资本开支是需要重点盯的争议，但不妨碍形成观点。档案中目前没有投资论点，所以没有需要改写的条目。', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': '没有一条会被重新写。', 'heading': '二、现有 thesis 哪几条会被重新写'}, {'body': '- debate:b947423b527e2374eb75f65a：这项争议问的是 AWS 加速主要靠模型伙伴和承诺容量，还是靠传统云迁移。数据已经给出答案：2026 年第二季度，非 AI 的核心工作负载仍占 AWS 收入的绝大部分，且增速同比加快。加速的基础较宽，由此引出的客户集中度担忧也就退居次要。\n- debate:7925bbd77f1fa99f4a12cab6：Prime Day 客单价的结构问题已经影响不了零售判断。Wolfe 的消费支出数据显示，第三季度北美零售增速有上行空间。零售营收和利润率… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 0} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-10 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:c3921453d3e58c1c9d58ef1befb1181a | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'anthropic-claude-5', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2179, 'invocation_ref': 'invocation:506a16805dc6a68c3955b911a48ceddf', 'route_decision_ref': 'route-decision:f9ab2264e804412e8a85147f711a1b4e', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-ea1c0a879f26595f20385a9ef4157bfe'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### GOOGL

环境：hyperscalers；版本：`zero-base-review-version:cc15178436459546d15116b4a18fa99b`；生成：2026-10-08T11:28:12.179047+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

form_a_view=yes；next_verification=2026-10-27

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': '从零开始看，这份档案的证据足以形成观点，核心问题也集中。FY2025 10-K 显示剩余履约义务主要来自 Google Cloud，公司预计其中略超过一半会在之后 24 个月内确认为收入。Wolfe Research 认为市场尚未计入 TPU 销售，因此其 2027 年云收入预测明显高于市场一致预期，同时提示积压订单增速可能在 2027 年前后见顶。投资论点应写成：TPU 与云积压订单转化为收入的速度，是否足以覆盖大规模资本开支和债券融资带来的现金流压力。档案目前没有投资论点，所以没有需要改写的条目。', 'citations': ['claim-version:7ab503fed17b0c2891ce7aede31a62a5a56a4410aab930517af725c7714aad3e', 'claim-version:ed2fb01170e18cf67c882ab0cd836c99bdf60543d0e7bcbd2d7ae2c2b5cbf494', 'claim-version:dc195fec26705015c07863d1be5daca79e315f33bac476ccce1da5eb5ca5100e', 'claim-version:573a2bf16f3b88821b3e1a97da3137a4e7945bd40d025de40d9c3b6e0669f74e', 'claim-version:11247b623d4a06a96d395ba348f74a096282c276fc61f6b0389c7b2a90099ee6', 'claim-version:9ecb4bbb485836cd83ecd745fa8420629292ec5ea98b7a432820051075e7d9a2', 'debate:a247b26b56bd7b08f… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-10-08 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:ws-7d894366d1132e2930475a60 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:ws-7d894366d1132e2930475a60:8 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 270317, 'invocation_ref': 'invocation:b9aff418b4397743e98be21662755c48', 'route_decision_ref': 'route-decision:c963d3c830234346a3c11cff06937754', 'work_order_ref': 'work:cockpit-zero_base_review-e0ddd564120a157b9c7a51baf67dca44'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '会建立观点。从零开始看，这份档案的证据足以形成观点，核心问题也集中。FY2025 10-K 显示剩余履约义务主要来自 Google Cloud，公司预计其中略超过一半会在之后 24 个月内确认为收入。Wolfe Research 认为市场尚未计入 TPU 销售，因此其 2027 年云收入预测明显高于市场一致预期，同时提示积压订单增速可能在 2027 年前后见顶。投资论点应写成：TPU 与云积压订单转化为收入的速度，是否足以覆盖大规模资本开支和债券融资带来的现金流压力。档案目前没有投资论点，所以没有需要改写的条目。', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': '没有一条会被重新写。', 'heading': '二、现有 thesis 哪几条会被重新写'}, {'body': '现在开着的争论都还重要。', 'heading': '三、哪些争论已经不重要了'}, {'body': '2026-10-27：Alphabet 2026 年第三季度财报：Google Cloud 收入增速、剩余履约义务的环比变化、管理层是否单独披露 TPU 对外销售或相关积压订单，以及资本开支和自由现金流的走势。\n因为：这份财报能同时检验三项争议分析：云积压订单转化为收入的节奏、TPU 能否作为对外收入来源，以及融资扩张是否压低自由现金流。日期按以往财报节奏推算，本轮没有查到公司公告，确切日期需要以 Alphabet 投资者关系页面为准。', 'heading': … | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 0} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-10 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:3e335bc438fa21fd149b747b15a75467 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'anthropic-claude-5', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2048, 'invocation_ref': 'invocation:4386b39b3acd11e3796fc8ea2dddca12', 'route_decision_ref': 'route-decision:c52f2dca2aa84cd3ba93548ffa14dcca', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-29cbac87b6d7cdea5badd0547f617625'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### MSFT

环境：hyperscalers；版本：`zero-base-review-version:1ac5107be4b7dbaead46ee7ab103b3d0`；生成：2026-10-08T20:55:37.513070+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

form_a_view=yes；next_verification=2026-10-28

| 条目 | 当前记录 | 评估 |
|---|---|---|
| answers | {'because': '深度研究档案里已经有足够的一手和卖方证据，可以从零开始形成观点。FY2025 10-K 披露了资本开支、融资租赁本金偿还和回购金额，还说明主要为数据中心的未起租租约会在 FY2026 至 FY2031 间陆续起租。Wells Fargo 和媒体报道指向同一个变化：微软正把算力布局转向自有为主，并计划到 2032 年把自有容量扩大到目前的三倍以上。变现一侧也有客户层面的证据：美银把 Copilot Chat 放进基础协议，Office Copilot 单独收费；企业也会在自己的 Azure 环境里运行 OpenAI 模型。档案里同时有一条反向证据：部分客户在 AI 采购时直接和前沿模型厂商签约。把这些放在一起，最核心的问题是重资产扩张能否换来独立的 AI 收入。目前没有存档的投资论点，所以没有需要改写的论点条目。', 'citations': ['claim-version:5d3e1935cf3bcde2ab369e024fbf6d7019b9728187456f1c2768f36114f2ed23', 'claim-version:eb02f21c63c2ceac90bd4a5e23dac54aa001b7a3ce5540f7a73d9b8f4a64da6a', 'claim-version:0bffaedfbddfe4fdd4174bf8c01c8f6e2abd635f47052162dfe5ea02ecea3783', 'claim-version:804b2533f0a36b47d3310798e7114b196f74025ee6569836e2152750dda64942', 'claim-version:67b042784904fcc506089b40a24186502b177ad6fa0427bc2341c2c14b9ec205', … | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| as_of | 2026-10-08 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| calibration_ref | None | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_ref | coverage-mission:ws-7d894366d1132e2930475a60 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| mission_version_ref | coverage-mission-version:ws-7d894366d1132e2930475a60:8 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| model | {'cost_micros': 277921, 'invocation_ref': 'invocation:096cb71375708929850c4f8be4e93be1', 'route_decision_ref': 'route-decision:94fc1d48c0f646329e60e9caed1431cc', 'work_order_ref': 'work:cockpit-zero_base_review-9cf86108cc4f36a6833c2ce5bcf97035'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| narrative | {'authority_note': '这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。', 'sections': [{'body': '会建立观点。深度研究档案里已经有足够的一手和卖方证据，可以从零开始形成观点。FY2025 10-K 披露了资本开支、融资租赁本金偿还和回购金额，还说明主要为数据中心的未起租租约会在 FY2026 至 FY2031 间陆续起租。Wells Fargo 和媒体报道指向同一个变化：微软正把算力布局转向自有为主，并计划到 2032 年把自有容量扩大到目前的三倍以上。变现一侧也有客户层面的证据：美银把 Copilot Chat 放进基础协议，Office Copilot 单独收费；企业也会在自己的 Azure 环境里运行 OpenAI 模型。档案里同时有一条反向证据：部分客户在 AI 采购时直接和前沿模型厂商签约。把这些放在一起，最核心的问题是重资产扩张能否换来独立的 AI 收入。目前没有存档的投资论点，所以没有需要改写的论点条目。', 'heading': '一、今天第一次看，会不会建立观点'}, {'body': '没有一条会被重新写。', 'heading': '二、现有 thesis 哪几条会被重新写'}, {'body': '- debate:1911bb3f9e939c7998540bc6：这个问题讨论的是传统软件厂商能否守住数据和工作流壁垒，属于行业层面的份额分配。档案里关于微软的证据集中在另一处竞争：客户直接和前沿模型厂商签约，绕过微软。真正影响微软判断的是这条直签路径，它已经由 AI 预算归属和 Azure 容量两个争议分析覆盖，这一项对本公司的判断已无实质影响。', 'h… | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| outcome_counts | {'held': 0, 'moved_right': 0, 'moved_wrong': 0, 'not_confirmed': 0, 'pending': 0, 'should_have_moved': 0, 'unavailable': 0} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| period_label | 2026-10 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| questions | ['If you were seeing this company for the first time today, would you form a view on it at all?', 'Which lines of the current thesis would you write differently?', 'Which of the open debates are no longer material?', 'What is the next verification point, and on what date?'] | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| review_ref | zero-base-review:2f6913152e7fa45c4057926d7a4743b9 | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| trigger | monthly | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |
| verification | {'findings': [], 'independence': {'predicate': 'model_family_ne', 'producer_family': 'anthropic-claude-5', 'verifier_family': 'google-gemini-3'}, 'model': {'cost_micros': 2210, 'invocation_ref': 'invocation:af0265cc02ddd07eb25901cea4e3fb08', 'route_decision_ref': 'route-decision:60c296b51da34f6d8b9b457042bc39a0', 'work_order_ref': 'work:cockpit-zero_base_review_verifier-a34456ec95268f8993a34d022ac2c3c9'}, 'status': 'verified'} | B／C：问题选择有用；应更新事实对照和核验结果，不把 review 中 yes 等同正式 thesis 已采纳。 |


### ACN 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | I would form a low-confidence, conditional view on Accenture's ability to benefit from AI, not the existing medium-confidence view on sustained growth and margin protection. AI-led financial-services work provides evidence of demand, but soft discretionary spending, acknowledged deflation and restructuring risk leave the aggregate revenue and profit offset unproven. | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | yes | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': 'This is an analyst-set schedule-verification date, not an asserted earnings date. The substantive test must distinguish gross AI activity from net growth and productivity benefits from retained profit: sector-level AI success does not settle the volume-offset debate, and prospective integration efficiencies do not settle the savings-retention debate.', 'date': '2026-09-14', 'what': 'At the next results disclosure, check converted AI revenue and follow-on managed-services revenue against legacy declines, acquisitions and FX; reconcile reported and adjusted operating margins with delivery savings, fee concessions and restructuring costs. Confirm the disclosure schedule at a 2026-0… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [{'because': 'The supplied commentary explicitly says demand has not clearly bottomed. The file provides neither an industry growth series nor bookings-conversion evidence sufficient to retain the original timing claim.', 'decision': 'THESIS_WEAKENED', 'line': "A US IT services demand bottom remains unconfirmed. Accenture's soft discretionary spending and roughly flat short-term project-based growth do not establish an industry recovery over the next two to four quarters; that recovery remains conditional on broader demand stabilization and demonstrated bookings conversion.", 'refs': ['claim-version:7408568e8b80c33dd1929e9b374d31d007b29701239d128378c8d669c4a37347', 'claim-version:2fbb3874041… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### EPAM 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | From a standing start, this file supports investigating EPAM, but not a directional demand-recovery view as of 2026-09-11. The only quantified revenue observation is for Q2 2025 and does not separate acquisitions, currency, or underlying engineering volume; the remaining claims show AI-related opportunity alongside selling execution problems and substitution risk. | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | unclear | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': 'Underlying volume is the direct test of a demand bottom; AI-related mix shifts and headline revenue growth cannot settle it. This is a proposed next monthly review date, not a confirmed earnings date, which the supplied file does not provide.', 'date': '2026-10-11', 'what': 'At a scheduled analyst checkpoint, obtain the latest available quarterly results and reconcile reported growth to acquisition- and currency-adjusted growth, then check client-funded engineering volume and new starts versus project run-off, particularly in North American Software/Hi-Tech.'} | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [{'because': 'Q2 2025 reported revenue rose 18.04% to $1.353 billion, but the file supplies no organic, constant-currency bridge or subsequent demand trajectory. The guidance-cut attribution points to company-specific execution weakness. AI substitution is a disclosed risk, not evidence of realized displacement, while the remaining-performance-obligation disclosure exemptions limit visibility into future revenue; none of these establishes the original managed-services conversion mechanism.', 'decision': 'THESIS_WEAKENED', 'line': "EPAM's demand bottom remains unverified. AI-led modernization is changing demand mix, but North American selling execution problems and Software/Hi-Tech weakness p… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### DXC 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | The file supports a low-confidence, company-specific view of a possible slowing in DXC's revenue decline, not an industry recovery thesis. FY27 guidance still calls for a mid-single-digit revenue decline with second-half improvement, while low-single-digit organic growth appears only as an analyst's FY29 expectation. AI partnerships provide potential upside but do not establish incremental revenue or retained productivity savings. | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | yes | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': 'This is a scheduled review checkpoint, not an asserted earnings date; the file provides no confirmed reporting date. These observations test the proposed second-half improvement and distinguish workload replacement from retention, and incremental AI economics from delivery efficiencies. The supplied claims do not settle any of those distinctions, so none of the four full debates can responsibly be retired.', 'date': '2026-10-11', 'what': 'At the next monthly review, check for newly reported organic revenue trends in GBS and GIS, evidence that contract-termination headwinds are diminishing, and conversion of awards into delivered revenue. Separately identify paid AI revenue and r… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [{'because': 'The concrete outlook is for continued FY27 contraction. Sustained bookings improvement is presented as an upside risk, and low-single-digit organic growth is an analyst expectation for FY29. The file supplies neither realized bookings-to-revenue conversion nor evidence for the asserted two-to-four-quarter lag, discretionary consulting deceleration, or aggregate industry inflection.', 'decision': 'THESIS_WEAKENED', 'line': 'DXC may experience moderating revenue declines in the second half of FY27 as legacy contract-termination headwinds diminish, but a return to growth remains unverified. Bookings improvement and AI-enabled offerings are potential recovery mechanisms rather than… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### IBM 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | From this file alone, I would put IBM on a research watchlist rather than form a directional view as of 2026-09-11. The reported growth observations concern consolidated revenue in 2024 and 2025, not current Consulting demand, backlog conversion or margins; enterprise AI and hybrid-cloud positioning provide a hypothesis, not evidence of an industry recovery. The GitLab and unattributed Flex claims do not establish anything about IBM Consulting. | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | no | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': 'This is an analyst-scheduled evidence check, not an assumed earnings date. The immediate constraint is stale and insufficient segment evidence: current disclosures should test whether bookings are reaching revenue and whether AI work adds demand while preserving margins before reinstating a two-to-four-quarter recovery forecast.', 'date': '2026-09-14', 'what': 'Retrieve the latest IBM quarterly disclosures available by the review date and verify Consulting constant-currency revenue growth, signings/backlog conversion, client authorization delays, segment margins, and any quantified GenAI revenue or incremental spending. Separate Consulting performance from Z-related consolidated… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [{'because': "IBM's reported consolidated revenue grew 7.65% year over year in Q2 2025, but that does not identify Consulting's contribution or establish a current industry inflection. Management's description of Z-driven revenue across multiple businesses further limits consolidated growth's usefulness as a Consulting demand proxy. The asserted bookings-to-revenue timing is not demonstrated by the supplied claims.", 'decision': 'THESIS_WEAKENED', 'line': "IBM's enterprise AI and hybrid-cloud positioning could support Consulting demand, but this file does not establish that discretionary declines are decelerating, that AI and managed-services bookings are converting, or that US IT services r… | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### CTSH 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | 从零开始，现有材料不足以对 Cognizant 形成有方向性的投资判断。已核实结论主要涉及管理层任命、财务负责人交接及收入披露分类，缺少能够检验增长和盈利质量的实际数据；当前也没有可供改写的投资论点。三项争议分析仍涉及决定性问题：AI 生产率收益能否在续约后保留、大单能否转化为增量收入、AI 付费需求能否抵消传统业务流失；现有材料既未显示双方已形成共识，也未提供足以消除争议的数字。 | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | no | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': '这是安排的资料核验日期，不是已确认的财报发布日期。现有收入分类提供了分析服务与合同结构的入口，但尚未回答三项核心争议；应先补齐截至今日可获得的经营证据，再决定是否形成投资论点。', 'date': '2026-09-14', 'what': '核查截至复核日已公开的最新财报及业绩说明，寻找大单签约后两至四个季度的收入兑现情况、AI 项目的增量付费收入，以及续约让利和配套技术成本对营业利润率的影响；区分已披露数据、管理层预期和仍未披露的指标。'} | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### META 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | 会形成看法。档案里的已核实结论有两条主线，足以从零起步写出投资论点。第一条：广告转化增速快于展示量增速，排序和召回模型的改进是主要驱动，管理层预计 2026 年余下时间推荐系统还会继续提升互动（claim-version:0b99589b…、claim-version:b96afdc4…）。第二条：管理层押注全栈前沿模型和基础设施，同时没有给出 2027 年资本开支指引，还在评估最优组织规模（claim-version:ecbbdc4a…、claim-version:85d4107d…、claim-version:a95779b9…）。所以今天的初始看法是：核心广告引擎有一手证据支撑，回报端证据较扎实；AI 投入规模和回报周期是主要的未知数，需要拿下一次财报检验。目前没有已存档的投资论点，因此改写列表为空。 | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | yes | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': '这次财报会同时回答资本开支争议和 AI 能否提升广告量价这两个最关键的问题，答案会直接决定投资论点偏向哪一边。2026-10-28 是按往年节奏估计的日期，目前还没有经公司公告核实，需以公司正式公告为准。', 'date': '2026-10-28', 'what': '2026 年第三季度财报及电话会：2027 年资本开支有没有给出初步区间；广告展示量与单价的拆分，以及转化增速是否继续快于展示量；Business Agent 和 WhatsApp 新消息类别有没有给出可计量的收入。'} | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### AMZN 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | 仅凭现有材料，从零开始也值得形成观点，主线是 AWS 产能扩张带来的收入加速。多份卖方报告把 Project Rainier 和管理层“2027 年底容量翻倍”的表态，视为 2026 年 AWS 增速加速和盈利预测上修的主要依据。2026 年第二季度，非 AI 的核心 AWS 业务仍占 AWS 收入的绝大部分，而且同比增速加快。零售端营收和利润率保持稳健，广告业务在支撑利润率。9 月亚马逊发行了多期英镑优先票据，说明资本开支对现金流的压力正在落到融资层面。因此，现金流与资本开支是需要重点盯的争议，但不妨碍形成观点。档案中目前没有投资论点，所以没有需要改写的条目。 | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | yes | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': '剩下的主要争议都集中在两件事上：一是产能能否转化为增速和份额，二是资本开支和长期采购协议会不会侵蚀自由现金流。第三季度财报会同时给出这两方面的数据。日期按亚马逊往年 10 月底发布的惯例估计，尚未查证，需以公司公告为准。', 'date': '2026-10-29', 'what': '2026 年第三季度财报：AWS 收入增速是否继续加快，核心业务与 AI 业务各自的贡献，资本开支指引，以及自由现金流变化'} | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [{'because': '这项争议问的是 AWS 加速主要靠模型伙伴和承诺容量，还是靠传统云迁移。数据已经给出答案：2026 年第二季度，非 AI 的核心工作负载仍占 AWS 收入的绝大部分，且增速同比加快。加速的基础较宽，由此引出的客户集中度担忧也就退居次要。', 'debate_ref': 'debate:b947423b527e2374eb75f65a'}, {'because': 'Prime Day 客单价的结构问题已经影响不了零售判断。Wolfe 的消费支出数据显示，第三季度北美零售增速有上行空间。零售营收和利润率在 Project Leo 投入和 FBA 费用压力下仍然稳健，利润率主要由广告业务支撑。单次促销的品类结构对整体结论影响很小。', 'debate_ref': 'debate:7925bbd77f1fa99f4a12cab6'}] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### GOOGL 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | 从零开始看，这份档案的证据足以形成观点，核心问题也集中。FY2025 10-K 显示剩余履约义务主要来自 Google Cloud，公司预计其中略超过一半会在之后 24 个月内确认为收入。Wolfe Research 认为市场尚未计入 TPU 销售，因此其 2027 年云收入预测明显高于市场一致预期，同时提示积压订单增速可能在 2027 年前后见顶。投资论点应写成：TPU 与云积压订单转化为收入的速度，是否足以覆盖大规模资本开支和债券融资带来的现金流压力。档案目前没有投资论点，所以没有需要改写的条目。 | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | yes | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': '这份财报能同时检验三项争议分析：云积压订单转化为收入的节奏、TPU 能否作为对外收入来源，以及融资扩张是否压低自由现金流。日期按以往财报节奏推算，本轮没有查到公司公告，确切日期需要以 Alphabet 投资者关系页面为准。', 'date': '2026-10-27', 'what': 'Alphabet 2026 年第三季度财报：Google Cloud 收入增速、剩余履约义务的环比变化、管理层是否单独披露 TPU 对外销售或相关积压订单，以及资本开支和自由现金流的走势。'} | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


### MSFT 零基回答明细

| 回答项 | 内容 | 评估 |
|---|---|---|
| because | 深度研究档案里已经有足够的一手和卖方证据，可以从零开始形成观点。FY2025 10-K 披露了资本开支、融资租赁本金偿还和回购金额，还说明主要为数据中心的未起租租约会在 FY2026 至 FY2031 间陆续起租。Wells Fargo 和媒体报道指向同一个变化：微软正把算力布局转向自有为主，并计划到 2032 年把自有容量扩大到目前的三倍以上。变现一侧也有客户层面的证据：美银把 Copilot Chat 放进基础协议，Office Copilot 单独收费；企业也会在自己的 Azure 环境里运行 OpenAI 模型。档案里同时有一条反向证据：部分客户在 AI 采购时直接和前沿模型厂商签约。把这些放在一起，最核心的问题是重资产扩张能否换来独立的 AI 收入。目前没有存档的投资论点，所以没有需要改写的论点条目。 | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| form_a_view | yes | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| next_verification | {'because': '容量是否持续受限、重资产投入是否压低自由现金流、AI 收入是否已经独立成项，这三个仍未结论的争议分析都要靠季度数据来判断。财报日按往年 10 月下旬的惯例估算，本次没有查证公司公告，实际日期需要以微软 IR 发布为准。', 'date': '2026-10-28', 'what': 'FY2026 第一财季财报。重点看三项：资本开支及融资租赁新增额，Azure 增速以及管理层对容量约束的表述，商业 RPO 和 Microsoft 365 Copilot 单独收费的进展。'} | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| rewritten_lines | [] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |
| stale_debates | [{'because': '这个问题讨论的是传统软件厂商能否守住数据和工作流壁垒，属于行业层面的份额分配。档案里关于微软的证据集中在另一处竞争：客户直接和前沿模型厂商签约，绕过微软。真正影响微软判断的是这条直签路径，它已经由 AI 预算归属和 Azure 容量两个争议分析覆盖，这一项对本公司的判断已无实质影响。', 'debate_ref': 'debate:1911bb3f9e939c7998540bc6'}] | B／C：有条件的复核回答可保留；下一核验需真实完成；旧式业务名如 DXC GBS／GIS 应按最新分部统一。 |


## IBM conviction proposal／decision

当前只有一条 low-confidence avoid／NO_CHANGE，风险回报不可量化；IBM Q4 收入相对街上约 −13.9% 的差异可能源于无季节性的集团模型，不能作为经营 alpha。它是低置信度候选观点，不代表九家公司已有投资建议。

环境：legacy；版本：`conviction-call-proposal:e93f130b332c7d0bb98cd9a278d2d131`；生成：2026-09-14T11:28:42.269350+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

{'call_ref': 'conviction-call:company:sec-cik:0000051143', 'change_evidence_refs': ['catalyst-entry:014e70e5b1ac77f610a88ba5d8aadfaa', 'company-dossier-version:company-sec-cik-0000051143:5', 'consensus-estimate-version:246b79b502156b9430252a9da4b7a6bb', 'forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4', 'thesis-version:b35bdc3d8aba4e4bb294c744b00f59c5'], 'change_reason': 'evidence_thicker', 'checkpoint_kind': 'conviction_call', 'company_ref': 'company:sec-cik:0000051143', 'confidence': 'low', 'consensus_gap': {'metrics': [{'consensus': '70384279280', 'gap_percent': '-3.24', 'metric': 'Revenue', 'ours': '68103116513.68325685', 'period': 'FY2026', 'refs': ['consensus-estimate-version:246b79b502156b9430252a9da4b7a6bb', 'forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4'], 'unit': 'USD'}, {'consensus': '16837324630', 'gap_percent': '3.31', 'metric': 'Revenue', 'ours': '17394323840.28796000', 'period': 'FY2026Q3', 'refs': ['consensus-estimate-version:246b79b502156b9430252a9da4b7a6bb', 'forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4'], 'unit': 'USD'}, {'consensus': '20476970520', 'gap_percent': '-13.90', 'metric': 'Revenue', 'ours': '17629792673.39529685', 'period': 'FY2026Q4', 'refs': ['consensus-estimate-version:246b79b502156b9430252a9da4b7a6bb', 'forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4'], 'unit': 'USD'}, {'consensus': '73198447680', '…

评估 C：先重建合理基准与 payoff；decision 身份可追溯，不能使缺证据的 call 自动可靠。

环境：legacy；版本：`conviction-call-decision:f365f1286a1d65766658cf9bcd4c1eb2`；生成：2026-09-17T15:00:59.383169+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

{'content_hash': 'a31b9afd840cef3ffdac62054cdb5b3814f54b7dc8cfaf2d1ba22d133b3acecf', 'created_at': '2026-09-17T15:00:59.383169+00:00', 'decision': 'defer', 'decision_number': 1, 'id': 'conviction-call-decision:f365f1286a1d65766658cf9bcd4c1eb2', 'proposal_hash': 'e4e653c110087314719613dc931028d3c9fa628c3f00011b88adeb9a7ffe719e', 'proposal_ref': 'conviction-call-proposal:e93f130b332c7d0bb98cd9a278d2d131', 'reason': '系统记录：用户未填写补充说明', 'schema_version': '0.1', 'supersedes_ref': None}

评估 C：先重建合理基准与 payoff；decision 身份可追溯，不能使缺证据的 call 自动可靠。

## 一份 weekly brief

最新 legacy v7（10/8）仍依赖固定八月 live-SEC-lane evidence pack。new=0／driver change=0 是该冻结基底的比较，不能解释成整个研究环境没有新证据。新场景／returns 通知可用，但未形成动态行业研究汇总；需把新财报／退休／关键冲突带入正文。Hyperscalers 无成功周报。

环境：legacy；版本：`weekly-brief-version:122770e47973ba08fc2f28a653958131`；生成：2026-10-08T11:02:14.765941+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 条目 | 当前内容定位 | 评估 |
|---|---|---|
| base_markdown_hash | 5acbb64ce9a2a7e0708ce1d80f7c9caf39d020cbe1632e063873b082be24df1c | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| brief_ref | weekly-brief:us-it-services | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| change_summary | {'baseline_claim_version_refs': [], 'carried_claim_version_refs': ['claim-version:1c4f31d84d431ac4aa2cfd5e10624ed9f0ddff0f17844392bc7a76a339adfa05', 'claim-version:41a602599fdf16da88b54ba8a14a20df9412fc981f3c56d7be05402bf7a7a025', 'claim-version:44332d104e23e7563bfae9dcd9f7b4740ac7c6c749915787265000db53495d67', 'claim-version:74ef310b3054cdc7022e845d22af6d6585ff757c003ab69cee70ab01f33d66a0', 'claim-version:a4d5fb26cbdf5725ff2b12e99fa09af363c9e2d975d2287d54040af485c80b14'], 'changed_driver_refs': [], 'changed_thesis_company_refs': [], 'is_baseline': False, 'new_claim_version_refs': [], 'new_gap_refs': [], 'new_question_refs': [], 'removed_claim_version_refs': [], 'resolved_gap_refs': [], 'resolved_question_refs': []} | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| company_overlay_versions | [{'company_ref': 'company:sec-cik:0001467373', 'content_hash': 'a51abc01814339f5b82c12050828d26f8557c5d9396b5af5fd9aaffac830bf4c', 'version_ref': 'company-overlay-version:acn:live-sec-lane-v2'}, {'company_ref': 'company:sec-cik:0001058290', 'content_hash': '3c8e544f6a3ba08b2e2754376d27f97d15dda9326b0b4c7e1d7878cd9239be13', 'version_ref': 'company-overlay-version:ctsh:live-sec-lane-v2'}, {'company_ref': 'company:sec-cik:0001352010', 'content_hash': '1d4b733fb96286f0957707a323e12674e1120eb46ecd615bbc78f3274035c9bd', 'version_ref': 'company-overlay-version:epam:live-sec-lane-v2'}, {'company_ref': 'company:sec-cik:0000051143', 'content_hash': '0bbf3c95312331d6223e3a1a74bdccdb17d24217cf7f3ac5b71effc649465771', 'version_ref': 'company-overlay-version:ibm:live-sec-lane-v2'}, {'company_ref': 'company:sec-cik:001688568', 'content_hash': '6f37cd2d999e54d759cf7e038915450a1b6c0ce677a20d2dd4d0597217a… | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| content_index | {'claim_version_refs': ['claim-version:1c4f31d84d431ac4aa2cfd5e10624ed9f0ddff0f17844392bc7a76a339adfa05', 'claim-version:41a602599fdf16da88b54ba8a14a20df9412fc981f3c56d7be05402bf7a7a025', 'claim-version:44332d104e23e7563bfae9dcd9f7b4740ac7c6c749915787265000db53495d67', 'claim-version:74ef310b3054cdc7022e845d22af6d6585ff757c003ab69cee70ab01f33d66a0', 'claim-version:a4d5fb26cbdf5725ff2b12e99fa09af363c9e2d975d2287d54040af485c80b14'], 'company_refs': ['company:sec-cik:0000051143', 'company:sec-cik:0001058290', 'company:sec-cik:0001352010', 'company:sec-cik:0001467373', 'company:sec-cik:001688568'], 'debate_hashes': [{'hash': '553f54ca10628ea7a53e9e22078e20440ae69d4b13f05bffc63977372926b988', 'ref': 'debate:live-sec-lane-v2:reported-revenue-growth-dispersion'}], 'driver_hashes': [{'hash': '0271631794a6e5332b8ab0fefaeb81b0f3f1519b695cacd8ced402a66063e4d6', 'ref': 'driver:revenue-growth-usd-gaa… | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| evidence_pack_version_hash | 7e23f481b61e315367a80b4d2a26e548e25dffc8a2ad90917e2d942cdc7144a2 | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| evidence_pack_version_ref | industry-evidence-pack-version:us-it-services:live-sec-lane-v2 | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| forecast_reconciliations | [] | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| industry_ref | industry:us-it-services | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| judgement_followups | [{'company_ref': 'company:sec-cik:0001467373', 'decision': 'THESIS_WEAKENED', 'hash': '21d2dd958521b2eaec16196a4f57681654b869bb6a29801466cf1b79df4841e9', 'judgement_ref': 'event-judgement:df275b8fd3e1d98888822ba96c54c6a9', 'kind': 'judgement_followup_notice', 'label': '论点已更新：这条判断针对的论点版本在它之后已由人修订，无需再裁决，仅留档', 'notice_kind': 'thesis_superseded', 'ref': 'judgement-followup-notice:da57a8400a7368947c89c89390216500'}, {'company_ref': 'company:sec-cik:0001467373', 'decision': 'THESIS_WEAKENED', 'hash': 'aeaf6c7e0ad752c6dc057c7896ea903f326d91963456e64156291531f9ebe7fb', 'judgement_ref': 'event-judgement:1eb5995ec97f35355061b42af61db9e7', 'kind': 'judgement_followup_notice', 'label': '论点已更新：这条判断针对的论点版本在它之后已由人修订，无需再裁决，仅留档', 'notice_kind': 'thesis_superseded', 'ref': 'judgement-followup-notice:3ef84cc4c151638006c599c40ba905d5'}, {'company_ref': 'company:sec-cik:0001467373', 'decision': 'THESIS_WEAKE… | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| markdown_hash | 9a3d32eef18915c7a0aa9e788e53809d4ad059b19095c715f283af1287117970 | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| period | {'end': '2026-10-08T11:00:00.000000+00:00', 'start': '2026-10-01T11:00:00.000000+00:00'} | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| preliminary_results | [] | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| return_attribution | [{'changes': [{'component': 'first', 'from': None, 'leg': 'realised_trailing_year', 'to': '-22.67'}, {'component': 'first', 'from': None, 'leg': 'expected_driver_model', 'to': '3.33'}, {'component': 'first', 'from': None, 'leg': 'expected_consensus', 'to': '12.16'}, {'component': 'first', 'from': None, 'leg': 'expected_sell_side', 'to': '29.27'}], 'company_ref': 'company:sec-cik:0000051143', 'hash': 'af90e6b44b63e768b86b272879efe0f0683df0a0b87c4e3278a8579f3b99f175', 'prior_hash': None, 'prior_ref': None, 'ref': 'holding-period-return-version:04c3643d4147e16925d15bb7cc27ba87'}, {'changes': [{'component': 'first', 'from': None, 'leg': 'realised_trailing_year', 'to': '-14.09'}, {'component': 'first', 'from': None, 'leg': 'expected_driver_model', 'to': '12.66'}, {'component': 'first', 'from': None, 'leg': 'expected_consensus', 'to': '17.75'}, {'component': 'first', 'from': None, 'leg': 'expe… | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| scenario_tracking | [{'company_ref': 'company:sec-cik:0000051143', 'moved': [{'decision': 'accept', 'hash': 'd28ca51dfd676f7a2f370001aa4cea35dcad53c230008f82c50ad96bae2920f9', 'kind': 'decision', 'ref': 'company-scenario-decision:1e7da0785d604c201fd70977d3d77f94', 'set_ref': 'company-scenario-set:8a768ff4971ca6fa7c2601936520fffc'}, {'decision': None, 'hash': '715b53805112a81d6dd73f1aebee98061b3a68ebdfcabaa6abe5ccf23508b4e5', 'kind': 'proposal', 'ref': 'company-scenario-set:8a768ff4971ca6fa7c2601936520fffc', 'set_ref': 'company-scenario-set:8a768ff4971ca6fa7c2601936520fffc'}], 'scenarios': [{'conditions': [{'condition_id': 't1', 'kind': 'trigger', 'observation_hash': 'e8c92378152d65ae8746a5921625df519c998a58aae1962efa26ae373784a759', 'observation_ref': 'company-scenario-observation:b5a5bb1a57c77246b027ef31f75b12be', 'rule': '营业收入同比增速 ≥ 2%（2027-01-31 前）', 'status': 'not_triggered'}, {'condition_id': 't2', 'ki… | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| sections | ['本期研究变化', '对现有观点的影响', '预测对账', '公司与 driver 分化', '证据缺口', '关键争议', '下期研究问题', '来源与 authority'] | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| snapshot_hash | 586c7471334b6e549b330edea8f21c42c25e8ef202f0aa4b610c822aa5cfc24d | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |
| thesis_bindings | [{'company_ref': 'company:sec-cik:0000051143', 'confidence': None, 'statement': None, 'status': 'insufficient', 'thesis_ref': None, 'thesis_version_hash': None, 'thesis_version_ref': None}, {'company_ref': 'company:sec-cik:0001058290', 'confidence': None, 'statement': None, 'status': 'insufficient', 'thesis_ref': None, 'thesis_version_hash': None, 'thesis_version_ref': None}, {'company_ref': 'company:sec-cik:0001352010', 'confidence': None, 'statement': None, 'status': 'insufficient', 'thesis_ref': None, 'thesis_version_hash': None, 'thesis_version_ref': None}, {'company_ref': 'company:sec-cik:0001467373', 'confidence': 'medium', 'statement': 'AI-led modernization and integrated delivery give Accenture a credible route to offset weak discretionary consulting, but sustained mid-single-digit local-currency growth with preserved operating margin remains a low-confidence hypothesis, continge… | C：基础包冻结范围须明示；新增版本数量不等于新投资信息，缺漏要按全研究输入核。 |


## 九份初筛质量评分：逐评分项

这是初筛 rubric 评分，不能外推为模型／深度研究整体质量。机械引用解析、模板齐全与 provenance 不评经济合理性；本次发现的 P0 是评分漏检。

### CTSH

环境：legacy；版本：`quality-score-version:f652680f99f9d7e33d211f8160dc0713`；生成：2026-09-26T12:17:27.195019+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': ['number_provenance'], 'criteria': 9, 'mean': 2.889, 'minimum': 1, 'withheld': []}；原始评分针对 `mission-deliverable-version:96f1b2c8c5f0ecaafe4481eb18eee039`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 1 | 正文出现的唯一时效性数字 USD 5413000000 与 5.83% 虽与 ee70ab01f33d66a0 一致，但 claim_refs_resolve 已 fail、9 条引用无法解析或已撤回，读者无法把该数字回指到本节可核验的 Claim。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 3 | residual_citation_artefacts 为 pass，正文无 C/N 标记或空括号，但多数句子以「因此判断」「下一步应」等无明示主语的句子开头，来源命名时有时无。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 3 | duplicate_parallel_citations 为 pass，numbers 列表只有一条 5413000000 的收入 Claim，正文同一个季度的同一个数字未出现重复并列引用。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | required_sections_present 为 pass，S1–S7 八节标题与模板逐一对齐，S6 估值节无正文但 gaps 明确写「按 Playbook 的数字纪律留空」。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 3 | S3 点名 AI 新增业务与交付效率为关键 driver，明确当前倾向「转型期的盈利韧性」而非加速，并给出「订单未转化为收入且重定价侵蚀超过新增 AI 与成本节省」的证伪条件，但主情形的收入/利润率数值边界未量化。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 3 | S4 给出「增长韧性尚在、加速受阻、利润改善更多依赖成本调整」的反向叙事，并逐条写明需要可自由支配预算持续受限、续约让利超过新增 AI 收入等成立条件，仅部分条件缺少可观察阈值。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 3 | S7 把有机固定汇率增长、续约让价、AI 可计费收入、Project Leap 净节省与利用率逐项绑定到判断，并写明「若订单强劲而有机增长放缓、指引再下调则下修」，多数指标缺具体阈值。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | S1–S7 的 gaps 均点名缺的是哪份季报/纪要/量化桥接及因此不能回答哪个问题，正文在证据尽头明确写「尚不足以确认」「仍缺来源」。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| source_discipline | 2 | 引用以 filing 与公司披露为主，但关键数字 5413000000 与 5.83% 只有 ee70ab01f33d66a0 单一来源，且其余段落依赖 Wells Fargo 调查与 Guggenheim 转述管理层观点而缺一手原文。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | fail | 9 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### DXC

环境：legacy；版本：`quality-score-version:3501b2f4514ac540eb810427a9c9ea38`；生成：2026-09-26T12:47:31.082539+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': [], 'criteria': 9, 'mean': 3.333, 'minimum': 2, 'withheld': []}；原始评分针对 `mission-deliverable-version:c49282bdff1e1d4e598c17060810935f`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 正文中出现的每个时效性数字（USD 2999000000、同比5.06%、2026-04-01至2026-06-30、USD 3194000000、同比0.96%、2025-10-01至2025-12-31）都与numbers列表中两条Claim的normalized_statement逐字对齐，且期间标注一致，未出现无来源数字。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 3 | 正文整体为完整句子，但机械检查指出存在1处引用标记剥离后的残迹（如「、、（同一季度数据重复）显示」或类似结构），构成可见的脚手架残留。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | numbers列表仅含两条Claim，分别对应不同季度（2025-10-01..2025-12-31与2026-04-01..2026-06-30）的不同数值，不存在同一subject×metric×period×unit的重复引用，正文也未重复出现同一数字。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | S1至S7八节标题与模板逐一对应，S6估值节虽无正文但写明按数字纪律留空的原因，其余各节均有实质正文，无空壳或仅换说法重讲的章节。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 3 | S3核心Thesis明确以GIS订单能否转化为收入为关键driver，当前倾向为收入收缩下的执行修复，并给出主/替代情形成立条件（订单转化与AI贡献 vs 客户拖延与短期项目疲弱）及改变判断的观察点（后续季报GIS收入、利润率与订单转化同步改善），但部分条件与影响的可检验性仍有待细化。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 3 | S4风险与Anti-thesis提出了与thesis相反的完整叙事（长期订单改善未必补上短期项目缺口，收入承压削弱成本削减效果），并分需求端、AI端、执行与估值端逐条说明成立所需条件（短期项目持续疲弱、订单未兑现、AI增量无法规模化、执行落后于计划），但个别条件的具体可跟踪阈值尚不清晰。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 3 | S7数据跟踪将GIS订单金额、订单收入比、在手订单转化、短期项目收入、利润率及AI订单收入确认等指标绑定到具体判断，并写明方向性读数（如订单转化改善支持上调、订单强劲但收入落后则挑战复苏路径），但未对每个指标给出明确阈值。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 各节gaps列表具体点名缺失的数据（如分部收入、订单金额、订单收入比、OASIS/Anthropic收入贡献、卖方共识与估值数据），正文在证据尽头明确说明不足（如S3「无法判断预期差大小或潜在股价影响」、S5「不能继续作为行业传导依据」），未超出证据作出断言。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| source_discipline | 2 | 引用以一手filing（DXC已申报收入）为基础，管理层原话与卖方观点（Guggenheim、TD Cowen、RBC）被标明为观点，但关键数字（最近报告季度收入USD 2999000000及同比5.06%）仅由单一来源承载，未出现第二个独立来源。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | fail | 1 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### ACN

环境：legacy；版本：`quality-score-version:179cea409927502bc4d143db37ca9b4f`；生成：2026-09-26T13:22:52.209202+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': [], 'criteria': 9, 'mean': 3.778, 'minimum': 3, 'withheld': []}；原始评分针对 `mission-deliverable-version:8f38f6646f7edddede1f160a785227af`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 正文中每个时效性数字（如 18718144000、18044066000、5.59%、8.31%）都逐字对应 cited evidence 中的 Claim 并标注了相同期间，无独立数字未回指。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 4 | 正文句子主语完整（如「已申报收入显示…」「管理层披露…」「公司披露…」），未见 C/N 标记、裸引用编号或剥离残迹。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | 同一 subject×metric×period 的收入 Claim 在正文各节只出现一次，无重复并列引用或「同一季度数据重复」类表述。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | 八节标题与模板对应齐备，S6 估值节虽无正文，但其 gaps 明确写明按 Playbook 数字纪律留空的原因。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 3 | S3 识别出大型转型需求与 AI 转化这一 driver，给出当前倾向（增长韧性而非加速）并写明证据边界，但主/替代情形的触发条件与影响散见于多段，未收敛为可检验的明确条件。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 4 | S4 提出「大型转型与存量合同暂撑收入，但新增需求不足将致增长放缓、成本调整侵蚀利润」的完整反向叙事，并逐条给出成立条件（支出疲弱延续、订单补充不足、联邦采购收缩或调查限制承接）及削弱该观点的读数。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 4 | S7 将每个跟踪指标绑定到具体判断并写明方向，如「咨询和托管服务订单持续改善并带动收入增长则支持需求回升，订单持续疲弱且收入进一步减速则下调增长持续性判断」。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 各节 gaps 点名缺哪个数据（最新季度咨询/托管新签订单、利用率、AI 订单收入口径、联邦收入敞口），正文在证据尽头明确说「现有材料不足以…」「仍属未决问题」。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| source_discipline | 3 | 收入数字来自已申报报表（一手 filing），但每个关键数字仅由单一 Claim 承载，没有显示两个独立来源。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### EPAM

环境：legacy；版本：`quality-score-version:b17cf2b6c576d27412f9ae6d7711734f`；生成：2026-09-26T13:53:48.723511+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': [], 'criteria': 9, 'mean': 3.889, 'minimum': 3, 'withheld': []}；原始评分针对 `mission-deliverable-version:41ccbecc88689f7afb012afd176fe48a`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 正文中每个时效性数字（1414767000、1400061000、4.53%、7.56%）都逐字对应 numbers 列表中的 Claim be05402bf7a7a025 与 b1daa73cba2451e7，期间标注也与 Claim 的 period 一致。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 4 | 正文剥离 C/N 标记后句子完整、主语明确（如「管理层表示」「已申报报表显示」），未出现裸引用或空括号残迹。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | 同一个收入事实在 numbers 列表中仅各有一条 Claim，正文引用收入数据时未在同一句并列重复同一数字。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | S1 至 S7 八节均存在，S6 估值节以正文留白加 gaps 说明按数字纪律留空，没有空壳章节。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 4 | S3 给出了具名 driver（AI 替代传统工程服务、北美销售转型迟缓）、当前偏谨慎倾向、主替代条件与可检验的证伪信号（大型 AI 项目签约并放量、北美净增长改善）。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 4 | S4 构建了与 thesis 针锋相对的反向叙事（AI 先侵蚀任务型收入而新项目填补不足），并逐条列出成立条件（传统业务缩减持续快于 AI 爬坡、北美销售转型延迟）。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 4 | S7 把前瞻指标绑定到具体判断，写明录像往哪边走支持或证伪当前判断（签约与收入确认、应收账款回款、利用率与费率方向）。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 各节 gaps 具体点名缺哪个指标、缺了它不能回答哪个问题（如缺大型 AI 项目签约金额无法判断替代业务何时填补缺口），正文在证据尽头明确说不足在哪里。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| source_discipline | 3 | 两条 Claim 均为一手申报报表收入数据，引用 basis 未在评分视野中直接展示；正文以 filing 为主并将管理层表述标为转述，但关键数字仅有单一来源，未显示第二个独立来源。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### IBM

环境：legacy；版本：`quality-score-version:ad578868e7033dfb7e8de63fdd119105`；生成：2026-09-26T14:23:58.323198+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': ['citation_dedupe', 'falsifiers_and_tracking'], 'criteria': 9, 'mean': 3.0, 'minimum': 1, 'withheld': []}；原始评分针对 `mission-deliverable-version:bce92d06c082a8d2f01c3652e276ca4d`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 正文中每个数字（如 USD 7761000000、0.577264、0.061632、20.8622、43.66 百分位）都能对应到 numbers 账本中一条 Claim 的 normalized_statement，期间标注一致（如 2026-04-01..2026-06-30），无来源处写成缺口。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 4 | 剥离 C/N 后正文没有残留标记或空括号，各句主语完整（如「已申报报表显示」「市场卖方对 IBM Consulting…」），来源在文字中被命名。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 1 | S3 与 S4 均引用同一季度净利润 USD 2165000000、摊薄 EPS USD 2.27/股，且 S4 行文出现「2026-04-01..2026-06-30 营业收入为 USD 17162000000」与 S3 同事实重复引用。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | 八节标题与模板逐一对应，Key Information、S1、S2、S7 无正文处均写明丢弃或未写原因，估值节明说只覆盖三项倍数。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 3 | S3 指出软件与现金流为关键 driver，给出「只要软件收入维持增长…下行风险相对可控」的当前倾向及「若服务成本走高且软件回落」的下修条件，但改变判断的观察点（下次季度软件收入、基础设施交易落地）未绑定阈值。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 4 | S4 提出与 thesis 针锋相对的反向叙事（AI 订单未证明咨询加速、大型机交易不落地），逐条列出三个成立条件（预算挤占、签约不转化、大额交易推迟取消），并给出可核验的证伪信号。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 1 | S7 数据跟踪整节被丢弃，S3/S4/S6 末段虽提到「下一次季度披露软件收入能否延续」等观察点，但未以独立一节将指标绑定到具体判断并写明读数方向。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 各节 gaps 点名缺哪个指标及缺了它不能回答哪个问题（如缺咨询分部利润、缺基础设施同口径季度数据），正文在证据尽头明确写「缺来源」「本批材料未提供分位构造细节」。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| source_discipline | 2 | 所引 Claim 的 basis 均为一手 10-Q/10-K filing，JPMorgan 卖方观点被标明为观点，但关键数字（收入、EPS、毛利率、现金流）在账本中只有单一申报来源，未见两个独立来源。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | fail | 1 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### META

环境：hyperscalers；版本：`quality-score-version:7a517dff4200b52c317b3d16b421a399`；生成：2026-10-03T04:23:42.818215+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': [], 'criteria': 9, 'mean': 3.889, 'minimum': 3, 'withheld': []}；原始评分针对 `mission-deliverable-version:049184711e9e0f13bf33d4e37ff1a4a7`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 每个时效性数字（如 60801000000、27.96%、49113000000、0.022092、27.237、30.88%）都能在 claim 表的 period 与 normalized_statement 中逐字对齐，缺失的指引和容量数字均写入 gaps。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 4 | 剥离 C/N 标记后正文如「管理层把增长归因于广告展示量和单条广告均价同时上升」仍有完整主语与来源命名，未见残迹。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | 同一事实（如 2026 上半年资本开支 USD 49113000000 与经营现金流 USD 64088000000）在全文多次出现但 claim 表中仅各有一条，未被并列重复引用。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | 八节标题与模板逐一对应，S6 估值节明确写出「卖方一致预期、前瞻倍数、目标价和 IRR 都缺来源」故只做相对位置判断。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 4 | S3 点名 AI 广告排序为 driver，给出「收入增速继续放缓+资本开支费用指引继续上调」的证伪条件，并列出三季度业绩、2027 年 CapEx 指引、Muse 变现三个观察点。 | C：广告增长到 capex ROI 的论证跳跃未获量化验证；评分不应奖励结论式叙述。 |
| anti_thesis | 4 | S4 给出「现金投入 AI 基础设施回报晚于定价、自由现金流/利润率/股东回报同时承压」的完整反向叙事，并逐条写明需要「2027 年资本开支继续上行且经营现金流增速跟不上」等成立条件。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 4 | S7 将「资本开支抬升而经营现金流增速跟不上」对应到证伪「CapEx 由内生现金流支撑」，并给出 27.96%、30.88%、0.022092 等具体阈值与方向。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 各节 gaps 具体点名缺哪项（如 2027 年 CapEx 指引、GW 已投运容量、Muse 收入），S6 并明确止步于「只能做相对位置判断」。 | C：需把公司未披露与公开数字未采集分开；列 gap 不证明是诚实的外部数据缺口。 |
| source_discipline | 3 | 关键数字全部取自 10-Q（accession 0001628280-26-050705 等）一手申报，但多数关键数字仅单一 filing 来源，未见第二个独立来源。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### AMZN

环境：hyperscalers；版本：`quality-score-version:d5503e517a71785b4eda544a7505fc77`；生成：2026-10-07T20:54:21.551732+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': [], 'criteria': 9, 'mean': 3.778, 'minimum': 2, 'withheld': []}；原始评分针对 `mission-deliverable-version:5aeb6842da19a20fb42f62a702a1890a`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 正文中的所有经营与财务数字（如 2026Q2 营收 USD 200606000000、AWS 营业利润 USD 16621000000 及 19.62% 同比增速）均与本节引用的 Claim 原文及期间逐字对齐，未取得的数字均明确写入 gaps。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 4 | 正文句子结构完整且主语清晰，均以「已申报报表显示」「管理层表示」「卖方研究认为」等具名引述，无任何残存的引用符号或残缺句式。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | 各章节针对同一会计期间与科目的财务数据均仅保留单一 Claim 引用，不存在同一指标的重复并列引用。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | 模板规定的八个章节均完整具备实质内容，且估值节严格遵守纪律明确了因缺乏前瞻一致预期与行情基准而留空 IRR 的原因。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 4 | 核心判断聚焦于 AWS 2027 年电力容量翻倍及大客户承诺向收入转化的关键 driver，明确了主情形与替代情形的触发机制及可检验的跟踪边界。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 4 | 风险章节构建了资本开支前置变现滞后叠加 AI 大客户多云分流的自洽反向叙事，并逐条给出了利润率与融资恶化的成立条件。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 4 | 数据跟踪一节明确绑定了 AWS 季度收入环比增量、分部利润率阈值以及经营现金流对资本开支缺口等方向性证伪读数。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 每一节文末的 gaps 均明确点出缺少具体 CapEx 绝对值与 backlog 期限结构导致无法量化预期差，结论严格止步于现有 10-Q 与纪要支持的范围。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| source_discipline | 2 | 材料严格以一手 10-Q 申报为主并区分了管理层表态与卖方研报，但关键财务指标仅依赖单一 10-Q 来源，未形成两个独立来源的交叉印证。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### GOOGL

环境：hyperscalers；版本：`quality-score-version:1595636f72269604d3d59bec3915598b`；生成：2026-10-08T11:27:52.879129+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': [], 'criteria': 9, 'mean': 3.667, 'minimum': 2, 'withheld': []}；原始评分针对 `mission-deliverable-version:82507e8891119a5047591802dc88b930`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | S1 中的 119796000000、80598000000、84859000000、34.03%、9.23/9.11 等数字均与 10-Q Claim 的申报原值逐字一致并标注了与 Claim 相同的期间，无来源的分部收入与营业外明细写成 gaps。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_hygiene | 4 | 正文无剥离残迹（mechanical check 亦通过），来源在文字中被具名（已申报报表、FY2025 10-K、Wolfe Research、公告），每句均有主语。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | 各节中同一事实（如上半年资本开支 80598000000 与经营现金流 84859000000）每次出现都只挂一条申报来源，重复引用检查通过。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | 八节齐备且与模板对应，S6 估值在缺 street 一致预期与价格序列时明说事件路径与 IRR 无法量化、缺哪些输入。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 3 | S3 给出 Cloud+TPU 第二引擎对 CapEx 挤压现金流的 driver、当前倾向、转化节奏与 CapEx 指引等改变判断的观察项，但其主张的『卖方共识未计入 TPU』预期差缺少一致预期来源（自己列为 gap），该主张目前只有 Wolfe 单一来源。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 4 | S4 逐条给出利润质量、现金流挤压、订单可见度三条反向叙事，各附成立条件（如 10-Q 附注确认一次性公允价值变动）与被证伪条件，并给出『三者中两项恶化即主导』的判定规则。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 4 | S7 的三条跟踪线（资本开支对经营现金流、backlog 环比与确认占比、营业利润率相对 34.03% 的方向）逐一绑定到 thesis/anti-thesis 条件，并写明读数方向对应的证伪含义。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 每节 gaps 具体到缺哪个指标导致哪个问题无法回答（如缺 RPO 金额致 TPU 占比不可量化、缺营业外明细致 P/E 分母失真不可剔除），正文在推断处明确标注『属于推断』『缺来源』。 | C：需把公司未披露与公开数字未采集分开；列 gap 不证明是诚实的外部数据缺口。 |
| source_discipline | 2 | 以 10-Q/10-K 申报 XBRL 为主要来源、Wolfe 观点被标为卖方观点而非事实，但关键数字（收入、资本开支、估值分位）各自只有单一来源，无两个独立来源交叉。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


### MSFT

环境：hyperscalers；版本：`quality-score-version:fdfeb9fd68221c154d9e1cb85631c898`；生成：2026-10-08T20:55:15.193017+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

评分 summary：{'below_passing': ['citation_hygiene'], 'criteria': 9, 'mean': 3.556, 'minimum': 1, 'withheld': []}；原始评分针对 `mission-deliverable-version:f8de379845eba8238f20ab7a1d9eb113`，不默认为最新初筛。

| 评分项 | 分数 | 评分理由定位 | 本次评估 |
|---|---|---|---|
| number_provenance | 4 | 每个季度增速、收入绝对值（如 USD 90007000000）及 331839000000 全年数均逐字对应 claims 表中的 normalized_statement，且 4-6 月推算值在正文两处明确标注「推算、未单独申报」，期间与 Claim 的 period 一致。 | D：得 4 分仍未检查 issuer 和 actual／estimate，Oracle 归属污染直接绕过此项。 |
| citation_hygiene | 1 | 正文句子均完整，但 gaps 列表里裸露出现「N1 与 C116 只说明 10-K 披露了这些项目」和「N1 只有说明，没有数字」这类未剥离的引用标记，对应机制检查报出的 2 处残迹。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| citation_dedupe | 4 | 六条 Claim 各承载不同季度或不同事项，13.27% 等数字在多节出现时指向同一条 Claim 而非并列重复引用，且「全公司 RPO」与「商业 RPO 确认比例」两个相近披露被分别标注缺口。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| required_sections | 4 | 八节齐备且各有实质内容，S6 估值节按 Playbook 纪律写明因估值快照车道未接入而留空，而非空壳。 | C：标题在／gaps 在不等于章节有内容；IBM 四空节、其他初筛 S6 空仍应判未完成。 |
| key_driver_thesis | 4 | S3 具名关键 driver（容量交付节奏、毛利率/FCF 代价、剔除 OpenAI 的 RPO 结构），给出当前倾向及「资料里没有卖方一致预期、只能定性判断预期差方向」的证据边界，证伪条件（容量释放后增速回落叠加毛利率下滑）与 FY2027 新分部首份财报的观察点具体。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| anti_thesis | 4 | S4 给出「用利润率和 FCF 换容量」的完整反向叙事，并逐条列出其成立的三个条件（毛利率压缩延续到 FY27 后、融资压力传导到回购、直签分流叠加用量计费爬坡偏慢），同时说明管理层 RPO 数据方向相反。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| falsifiers_and_tracking | 4 | S7 每条指标绑定具体判断并给出阈值方向，如「连续两个季度回落到 2025 年初水平即证伪收入判断」「云毛利率企稳而收入维持是对多头最有利证据」「增量重新集中到少数大模型客户则下调论点」。 | B／C：评语可检查表达纪律；应补事实核验、经济／会计与跨产物一致性，不能仅凭 3–4 分认可投资结论。 |
| gaps_honest | 4 | 各节 gaps 点名缺哪个指标及缺了它无法回答什么（如未起租租约金额是判断未来租赁负担的关键、无一致预期数值故无法量化预期差），且推算季度收入在正文明确停步于推算属性。 | C：需把公司未披露与公开数字未采集分开；列 gap 不证明是诚实的外部数据缺口。 |
| source_discipline | 3 | 一手申报（10-K、季度收入）承载关键数字并区分管理层原话与卖方观点/专家访谈为判断，但各季度收入数字各自只有单一 filing 来源，仅推算季度通过与财年、前九个月的勾稽形成内部交叉验证。 | B／C：一手来源优先合理；源标签不等于 issuer 正确，独立来源数量也不验证概念和期间。 |

| 机械检查 | 原状态 | count | 评估 |
|---|---|---|---|
| numbers_without_refs | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| claim_refs_resolve | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| residual_citation_artefacts | fail | 2 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| duplicate_parallel_citations | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |
| required_sections_present | pass | 0 | 可作低层 QA；不能由 pass 推 issuer／费用合理／报表完成。 |


## 九个按周 reflection 对象：每条候选／policy 建议

复盘只读、不写 ledger 的边界明确；问题登记候选不是已解决事项。历史大量重复版本应以反思对象／问题关闭率计量，不用版本数代表质量。

### legacy 2026-W36

环境：legacy；版本：`research-cycle-reflection-version:6ef5475fe0d56ca15935e4e4e3724d2d`；生成：2026-09-14T03:56:59.918225+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-07T04:00:00.000000+00:00', 'end_local': '2026-09-07T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W36', 'start': '2026-08-31T04:00:00.000000+00:00', 'start_local': '2026-08-31T00:00:00-04…；生成时间不等于研究窗口更新。候选 2，policy 建议 4。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': 'Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。', 'question': '这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。', 'question': '这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 1 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 质量分需要一条 lane 或一个发布前钩子才会被调用；Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #4 | 本周实际花费只占 mission 预算上限的不到 0.1%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### legacy 2026-W37

环境：legacy；版本：`research-cycle-reflection-version:500b69a6a5827eb371c206f58fc96069`；生成：2026-09-21T03:45:30.454466+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-14T04:00:00.000000+00:00', 'end_local': '2026-09-14T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W37', 'start': '2026-09-07T04:00:00.000000+00:00', 'start_local': '2026-09-07T00:00:00-04…；生成时间不等于研究窗口更新。候选 4，policy 建议 4。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '本周没有从 inquiry 开出任何 BoundedPlannerLoop。P14e 的两道闸都开着，所以要么本周没有可派发的 inquiry，要么当日的 ad-hoc 池已经用尽（lane 会报 skipped:pool_exhausted）。', 'question': '规划器这一周提出了 857 条 inquiry 而一条都没有被派发，其中哪几条值得先做成 ResearchTask？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '问题登记得比回答快三倍以上时，账本在变成愿望清单。', 'question': '未决问题这一周净增 84 条，哪一条最该先被回答？', 'refs': ['research-question:03aa1163403527f621abb4fe8862d93d', 'research-question:066df59db591e6ab379a9829c9c29e6b', 'research-question:078f52291f2e0de8f0a84c747c768740', 'research-question:08bec99609998eff44dd05d75ab792b7', 'research-question:0b6b057c1534f162bd99bd119bc3e660']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': 'Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。', 'question': '这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #4 | {'because': '五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。', 'question': '这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 2 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 质量分需要一条 lane 或一个发布前钩子才会被调用；Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #4 | 本周实际花费只占 mission 预算上限的0.4%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### legacy 2026-W38

环境：legacy；版本：`research-cycle-reflection-version:99ea6568b5dcd5c4b04063a0957b4c57`；生成：2026-09-28T03:57:29.184932+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-21T04:00:00.000000+00:00', 'end_local': '2026-09-21T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W38', 'start': '2026-09-14T04:00:00.000000+00:00', 'start_local': '2026-09-14T00:00:00-04…；生成时间不等于研究窗口更新。候选 3，policy 建议 4。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '本周没有从 inquiry 开出任何 BoundedPlannerLoop。P14e 的两道闸都开着，所以要么本周没有可派发的 inquiry，要么当日的 ad-hoc 池已经用尽（lane 会报 skipped:pool_exhausted）。', 'question': '规划器这一周提出了 485 条 inquiry 而一条都没有被派发，其中哪几条值得先做成 ResearchTask？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '问题登记得比回答快三倍以上时，账本在变成愿望清单。', 'question': '未决问题这一周净增 135 条，哪一条最该先被回答？', 'refs': ['research-question:0058567db98bad0f4e0a229579b9c41c', 'research-question:01d3b8aa5c316b12bfe0a39d03db9611', 'research-question:025330e7fd79e31ef29b21dd3308a883', 'research-question:02692f5bf972b765efe7e0b605261eb9', 'research-question:03d8990071e593dca05f8951d88622f3']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': 'Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。', 'question': '这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 1 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 质量分需要一条 lane 或一个发布前钩子才会被调用；Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #4 | 本周实际花费只占 mission 预算上限的不到 0.1%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### legacy 2026-W39

环境：legacy；版本：`research-cycle-reflection-version:0ac9bcd1c3720fb8e089c776d2d87c27`；生成：2026-10-05T01:21:45.056912+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-28T04:00:00.000000+00:00', 'end_local': '2026-09-28T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W39', 'start': '2026-09-21T04:00:00.000000+00:00', 'start_local': '2026-09-21T00:00:00-04…；生成时间不等于研究窗口更新。候选 4，policy 建议 3。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '本周没有从 inquiry 开出任何 BoundedPlannerLoop。P14e 的两道闸都开着，所以要么本周没有可派发的 inquiry，要么当日的 ad-hoc 池已经用尽（lane 会报 skipped:pool_exhausted）。', 'question': '规划器这一周提出了 185 条 inquiry 而一条都没有被派发，其中哪几条值得先做成 ResearchTask？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '问题登记得比回答快三倍以上时，账本在变成愿望清单。', 'question': '未决问题这一周净增 157 条，哪一条最该先被回答？', 'refs': ['research-question:012190e84b33af24d44fdf9f5b248488', 'research-question:034e7e96f701149d2a4d44c774dbc3b7', 'research-question:05a58ac5a2c41c29e88d1f283082ab98', 'research-question:06759ed4bc877e476df815c988b504e2', 'research-question:091a721a50267339a815e748f6d312ee']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': '人类检查点是设计里的停顿，不是设计里的死锁。等待超过两周说明要么材料没备齐，要么这个检查点没人认领。', 'question': 'industry_model（company:sec-cik:0000051143）已经在 entered 上等了 17.43 天，它在等什么？', 'refs': ['industry_model']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #4 | {'because': '五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。', 'question': '这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 3 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 本周实际花费只占 mission 预算上限的0.6%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### legacy 2026-W40

环境：legacy；版本：`research-cycle-reflection-version:201c99a1412598f4e1d8f00c5b9ea3c5`；生成：2026-10-09T15:18:25.928796+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-10-05T04:00:00.000000+00:00', 'end_local': '2026-10-05T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W40', 'start': '2026-09-28T04:00:00.000000+00:00', 'start_local': '2026-09-28T00:00:00-04…；生成时间不等于研究窗口更新。候选 4，policy 建议 1。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '专项研究已经开启，但本周没有可以安排的问题，或者当天的专项研究预算已经用完。', 'question': '系统本周提出的 198 个待核实问题都还没有安排专项研究，哪几个值得优先做？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '新问题增加的速度是回答速度的三倍以上，问题清单正在变成愿望清单。', 'question': '待研究问题本周净增 72 个，哪一个最该先回答？', 'refs': ['research-question:063c95b548aafad71795d75aa9e8028a', 'research-question:0897c206ed752d38a1131d171f271cac', 'research-question:0982044110ff487aaf8781e30af2f8b0', 'research-question:0ad6923bc249336bd0b38c248440ad8b', 'research-question:0dabb975f6412d9ed755c7d063e75745']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': '评分工具已经具备，但没有被调用时它不会产生任何结果。', 'question': '这一周没有任何研究产出被打质量分，是没有新产出，还是评分还没有接入？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #4 | {'because': '没有反馈时，无法判断研究产出是否被阅读和认可。', 'question': '这一周没有收到任何反馈，是没有意见，还是不方便反馈？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | 本周实际花费只占本研究任务预算上限的 0.4%，预算不是限制因素；研究做得少的原因在别处（选题规则、资料、研究安排），提高预算上限不会让系统多做事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### hyperscalers 2026-W37

环境：hyperscalers；版本：`research-cycle-reflection-version:d0f88fafccc6ba9c0ce9501445aecce7`；生成：2026-09-17T15:22:52.634224+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-14T04:00:00.000000+00:00', 'end_local': '2026-09-14T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W37', 'start': '2026-09-07T04:00:00.000000+00:00', 'start_local': '2026-09-07T00:00:00-04…；生成时间不等于研究窗口更新。候选 2，policy 建议 4。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': 'Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。', 'question': '这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。', 'question': '这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 0 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 质量分需要一条 lane 或一个发布前钩子才会被调用；Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #4 | 本周实际花费只占 mission 预算上限的0.0%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### hyperscalers 2026-W38

环境：hyperscalers；版本：`research-cycle-reflection-version:71991a2c32f92b8210c224dd35a37f93`；生成：2026-09-27T18:08:51.293401+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-21T04:00:00.000000+00:00', 'end_local': '2026-09-21T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W38', 'start': '2026-09-14T04:00:00.000000+00:00', 'start_local': '2026-09-14T00:00:00-04…；生成时间不等于研究窗口更新。候选 4，policy 建议 4。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '本周没有从 inquiry 开出任何 BoundedPlannerLoop。P14e 的两道闸里，还没有发布任何 ad-hoc ProbeTemplate；两者都是 owner 的版本化动作。', 'question': '规划器这一周提出了 168 条 inquiry 而一条都没有被派发，其中哪几条值得先做成 ResearchTask？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '问题登记得比回答快三倍以上时，账本在变成愿望清单。', 'question': '未决问题这一周净增 24 条，哪一条最该先被回答？', 'refs': ['research-question:063bcc8f4b861687258d06d17791cd53', 'research-question:07f2d5dd1ce1a1750ec6a8c3162081e3', 'research-question:0af0b4f3f751406bb63bf6ff8310e7dc', 'research-question:1853fe2a89a08cf1a4df7eecdbebae38', 'research-question:3a2840594c00d640d61142dac1472052']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': 'Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。', 'question': '这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #4 | {'because': '五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。', 'question': '这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 2 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 质量分需要一条 lane 或一个发布前钩子才会被调用；Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #4 | 本周实际花费只占 mission 预算上限的不到 0.1%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### hyperscalers 2026-W39

环境：hyperscalers；版本：`research-cycle-reflection-version:fe303257c9a4871c8e9db2e5ec40b29f`；生成：2026-10-04T22:06:54.846297+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-09-28T04:00:00.000000+00:00', 'end_local': '2026-09-28T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W39', 'start': '2026-09-21T04:00:00.000000+00:00', 'start_local': '2026-09-21T00:00:00-04…；生成时间不等于研究窗口更新。候选 5，policy 建议 4。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '本周没有从 inquiry 开出任何 BoundedPlannerLoop。P14e 的两道闸里，还没有发布任何 ad-hoc ProbeTemplate；两者都是 owner 的版本化动作。', 'question': '规划器这一周提出了 176 条 inquiry 而一条都没有被派发，其中哪几条值得先做成 ResearchTask？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '问题登记得比回答快三倍以上时，账本在变成愿望清单。', 'question': '未决问题这一周净增 122 条，哪一条最该先被回答？', 'refs': ['research-question:0118ebaddf6c9c9be057f4eba0e155c7', 'research-question:032aabf68db011ff00a9601c12bbceed', 'research-question:069bdc88e9f2bdfebe139c8b6bf31e36', 'research-question:0a8cd855381ecf51ecac9b951091990e', 'research-question:0e690d9b3e6ec72cc3086a234210ee7d']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': '人类检查点是设计里的停顿，不是设计里的死锁。等待超过两周说明要么材料没备齐，要么这个检查点没人认领。', 'question': 'initial_screen（company:ticker:amzn）已经在 entered 上等了 17.29 天，它在等什么？', 'refs': ['initial_screen']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #4 | {'because': 'Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。', 'question': '这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #5 | {'because': '五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。', 'question': '这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；今天有 2 个池认不到 lane。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #2 | tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、调度密度这三类问题在事后一个都答不了。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #3 | 质量分需要一条 lane 或一个发布前钩子才会被调用；Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #4 | 本周实际花费只占 mission 预算上限的0.2%。预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），提高上限不会让系统做更多事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


### hyperscalers 2026-W40

环境：hyperscalers；版本：`research-cycle-reflection-version:b7c0164d2b6e9717b8e784b8bcdef495`；生成：2026-10-09T15:01:40.914506+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

窗口：{'days': 7, 'end': '2026-10-05T04:00:00.000000+00:00', 'end_local': '2026-10-05T00:00:00-04:00', 'hours': 168.0, 'iso_week': '2026-W40', 'start': '2026-09-28T04:00:00.000000+00:00', 'start_local': '2026-09-28T00:00:00-04…；生成时间不等于研究窗口更新。候选 3，policy 建议 1。

| 条目 | 内容／原因定位 | 评估 |
|---|---|---|
| backlog_candidates #1 | {'because': '这些问题还没有安排专项研究，因为还没有设置专项研究的模板；这需要你在研究任务设置里开启。', 'question': '系统本周提出的 234 个待核实问题都还没有安排专项研究，哪几个值得优先做？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #2 | {'because': '新问题增加的速度是回答速度的三倍以上，问题清单正在变成愿望清单。', 'question': '待研究问题本周净增 44 个，哪一个最该先回答？', 'refs': ['research-question:02effc76eadbf5754102163788db97ac', 'research-question:03152a1a6dea93802878f757cb43b515', 'research-question:09d34508ba33ff51a3dfc0ffdeb10a2a', 'research-question:10710d4db1ddad353198a862f7d7619c', 'research-question:29608d132c7a610f264505585539b701']} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| backlog_candidates #3 | {'because': '没有反馈时，无法判断研究产出是否被阅读和认可。', 'question': '这一周没有收到任何反馈，是没有意见，还是不方便反馈？', 'refs': []} | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |
| policy_suggestions #1 | 本周实际花费只占本研究任务预算上限的 0.2%，预算不是限制因素；研究做得少的原因在别处（选题规则、资料、研究安排），提高预算上限不会让系统多做事。 | B／C：运营问题可登记；需优先级、负责人、期限、关闭证据与对研究结论影响，不应周周扩大未落实清单。 |


## 2,674 个不同事件 outcome checks

按 check_ref 取最新：legacy 2,615（pending 482、held 227、unavailable 1,906）；Hyperscalers 59（全部 unavailable）。逐对象状态和理由保存在私有原始对账附件中。这是全部当前检查对象的状态与方法评估，不声称对每个历史事件做了独立结果裁定。

held 是公式给定的状态而不是投资效果保证；pending 可能尚未到期，应按窗口看；unavailable 多因为没有可解析 thesis／方向。需信息可得日、正确基准、事先固定方向和阈值，再做命中／错判统计。不得把不可用算作正确。

## 缺失的闭环产物

两环境没有 model_reconciliation／forecast_reconciliation；Hyperscalers 没有正式 thesis、zero-base、weekly brief，AMZN／MSFT 没有模型。未成功登记的 IC memo／失败运行不是可评估成品。这些是覆盖缺口，不杜撰质量分。
