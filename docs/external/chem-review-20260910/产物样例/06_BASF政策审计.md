# BASF：Citi 欧盟化工保护政策情景审计

> **任务：** `sell_side_policy_catalyst_review`（task_id=68；event_id=54）  
> **证据截止：** 2026-09-03  
> **卖方原文：** Citi，2026-09-03，AlphaEngine `320000610261263`  
> **模型：** `models/basf-model-v0.4-citi-eu-policy-2026-09-03.xlsx`；旧版 `models/basf-model-v0.3-citi-2026-08-14.xlsx` 保留

## Direct answer

**决定：`NO_CHANGE / WATCHLIST`，BASF-T2/T3 与 Dalton base case、估值均不变。** 欧盟的政策方向和若干产品级反倾销措施已经落地，但截至证据截止日，没有找到覆盖 BASF 约 10Mt 欧洲上游产能、可统一带来 €100/吨净价改善的已生效规则；10月8日第二次 Critical Chemicals Alliance 大会只有利益相关方来源确认，Citi 所称9月重要会议未获独立核验。[A1][P1][P2]

Citi 的价格桥可复算，但利用率桥存在口径断裂：其“4–6个百分点 × €200m/点”应为 €800–1,200m，而不是报告所写 €400–600m；即使按欧洲占集团销售40%折算，也只有 €320–480m。其最高约 €1bn 是政策情景毛收益，不是当前盈利预测，且未扣中国反制、实施时滞、产品错配和欧洲成本劣势。[A1]

## 1. 9—10月政策催化：方向成立，日期与措施强度未完全核验

### 已核验

- 欧委会《欧洲化工行动计划》明确把高能源成本、不公平全球竞争和弱需求列为行业问题，建立 Critical Chemicals Alliance（CCA），并要求用贸易防御工具维护公平竞争。[P1]
- 欧委会 CCA 页面显示，联盟设有贸易工作组，目标包括识别关键化学品、加强进口监测并协调支持；官网已发布2026年7月和8月进口监测报告。8月报告的数据只更新到2026年5月，属于预警工具，不等于已经实施的行业保护措施。[P2]
- Renewable Carbon Initiative 9月1日称，第二次 CCA General Assembly 将于 **2026年10月8日**在法国 Creutzwald 举行，议题包括未来授权和优先事项。[P3] 这是联盟成员/利益相关方来源；欧委会 CCA 公共页面截至9月3日仍只列出2026年1月13日首次大会，未列10月8日。

### 未核验

- Citi 只写“9月和10月将有数场重要政策会议”，没有给出主办机构、具体日期、议程或可能通过的法律文本。[A1]
- 公开官方渠道未找到9月的特定化工贸易保护会议，也未找到已经启动的全行业 safeguard 调查、统一关税/配额或“€100/吨”净价规则。
- 因此，Citi 的90天 Upside STV更接近**政策消息催化交易**，不是确定的政策生效时间表。Citi自己的披露也说明 STV 代表中等而非最高置信度，并可能在90天内到期。[A1]

## 2. 已落地措施：能证明产品级保护有效，不能外推到10Mt组合

| 措施 | 已生效内容 | BASF映射 | 当前可入模程度 |
|---|---|---|---|
| BDO反倾销 | 对中国进口征收105.6%–113.7%，对沙特52.4%，对美国135.7%–142.5%；2024年被调查三国对欧进口额€140m。[P4] | BASF在Ludwigshafen生产BDO，并已宣布逐步提高产量，同时供应THF、PolyTHF和NMP衍生链。[B1] | **方向正面、不可量化。** 公司未披露增量吨位、实现价、负荷或EBITDA。 |
| 己二酸反倾销 | 对中国进口征收29.1%–42.3%；中国占欧盟域外进口€160m中的€130m。[P5] | BASF已在2025年关闭Ludwigshafen剩余己二酸产能，但仍在韩国Onsan及法国Chalampé合资企业生产。[B2] | **受益范围有限且口径不明。** 不能把已关闭产能计入欧洲价格/负荷弹性。 |
| 中国聚酰胺纱线反倾销 | 税率60%–67.6%，欧盟市场规模€400m。[P6] | 这是下游纱线产品；本次未验证到BASF直接纱线产能。 | **不入模。** 不能把下游保护直接映射为BASF聚酰胺原料利润。 |

这三项说明欧盟愿意采取贸易防御，但仍是逐产品、逐原产国调查。BDO是最直接的BASF正面案例；己二酸和聚酰胺纱线都不能支撑“BASF欧洲全部上游产能统一提价”。前BASF管理人员在2026年5月的Guidepoint访谈中也判断，已落地措施集中于较小产品链，尚未改变大型大宗化学品的整体定价，且产品组越大，反制风险越高。[G1]

## 3. Citi €100/吨与利用率情景：价格桥成立，利用率桥不闭合

### 3.1 价格情景

Citi假设：BASF上游产能超过20Mt，其中约10Mt位于欧洲；Chemicals销售的75%–80%为外部销售；欧洲利用率75%；广泛保护使净价差改善€100/吨。[A1]

可复算为：

- 低端：10Mt × 75%利用率 × 75%外销量 × €100/吨 = **€562.5m EBITDA**；
- 高端：10Mt × 75%利用率 × 80%外销量 × €100/吨 = **€600.0m EBITDA**。

因此，Citi所写约€500–600m在四舍五入口径下成立。但10Mt、外销量占比、利用率和€100/吨均为卖方假设；BASF没有披露同口径的产品—地区—外售产能表，无法判断哪些产品已被贸易措施覆盖，也无法扣除合同滞后、需求弹性和客户议价。

### 3.2 市占与利用率情景

Citi假设BASF欧洲上游市占约25%–30%，若恢复2–3个百分点，以25%为基数对应欧洲销量增长8%–12%；欧洲占集团销售约40%，因此集团销量增加约3%–5%。这两步可复算：

- 2–3个百分点 ÷ 25% = **8%–12%**欧洲销量增长；
- 8%–12% × 40% = **3.2%–4.8%**集团销量增长。

但后续有两处断裂：

1. 若74%欧洲利用率承接8%–12%销量增长，机械终值应为 **79.9%–82.9%**，即增加 **5.9–8.9个百分点**；报告写的是78%–81%、增加4–6个百分点。
2. Citi称管理层口径约€300m/利用率点、自己的模型更接近€200m/点，却又把4–6点折算为€400–600m。按原文应为 **€800–1,200m**；若再乘40%欧洲销售权重则为 **€320–480m**。报告所写€400–600m实际隐含 **€100m/欧洲利用率点**。

所以，利用率部分不能作为可审计的模型输入。价格端€500–600m加利用率端€400–600m形成的区间本应是€900–1,200m；“最高约€1bn”是概略情景，不是严格上限。[A1][M1]

## 4. 与8月14日Citi预测、现有模型和估值对桥

Citi上调目标价的同时，下调了近三年经营预测；这说明€60目标价主要来自更高的中期政策/复苏置信度，而不是2026–2028显性盈利上修。[A1][A2]

| 指标 | 2026E 新/旧 | 变化 | 2027E 新/旧 | 变化 | 2028E 新/旧 | 变化 |
|---|---:|---:|---:|---:|---:|---:|
| Sales (€bn) | 64.108 / 64.482 | -€0.374bn / -0.6% | 66.026 / 66.504 | -€0.477bn / -0.7% | 69.450 / 69.945 | -€0.495bn / -0.7% |
| Adjusted EBITDA / EBITDA bsi (€bn) | 7.578 / 7.608 | -€30m / -0.4% | 7.652 / 7.721 | -€69m / -0.9% | 8.488 / 8.561 | -€73m / -0.9% |
| Adjusted EPS (€) | 2.78 / 2.80 | -€0.02 / -0.7% | 2.58 / 2.63 | -€0.05 / -1.9% | 3.50 / 3.56 | -€0.06 / -1.7% |
| FCF (€bn，Figure 3口径) | 1.470 / 1.230 | +€0.240bn / +19.5% | 3.427 / 3.551 | -€0.124bn / -3.5% | 未披露同口径旧值 | — |

[A1][A2][M1]

- Citi把目标价由€58上调至€60；以9月2日€53.38参考价计算，价格上行12.4%，加4.2%股息后总回报16.6%。DCF假设为WACC 7.4%、长期增长2%、IROIC 7.4%；Citi认为回到约€9.15bn中周期EBITDA可支持约8.7x公允倍数。[A1]
- 把完整€1bn毛收益叠加到Citi 2027E €7.652bn，只得到 **€8.652bn**，仍比€9.15bn中周期低 **€498m/5.4%**。€1bn相对2027E仅为 **13.1%**；只有相对2025A €6.554bn才是 **15.3%**，所以“约15%集团盈利”依赖未说明的分母。[M1]
- 新模型只新增政策情景和算术审计，**base-case uplift保持€0**。没有把政策毛收益分摊到Chemicals、Materials或地区利润，也没有调整Dalton估值。LibreOffice重算后共229个公式、0个公式错误，Checks全部`OK`。[M1]

## 5. 中国反制与净收益

中国商务部5月21日明确表示，如果欧盟以“产能过剩”为由推出针对中国的歧视性限制措施，中方将采取坚决反制。[P7] 这只是官方风险警告，不代表反制已经发生；但它足以否定把欧洲毛收益直接当净收益。

BASF 2025年Greater China约占集团销售14%，Zhanjiang总投资约€8.7bn，且公司明确采用local-for-local策略。[B3] 潜在抵消项包括中国对欧化工品或相关行业的反制、Zhanjiang本地经营/审批风险、贸易流转移，以及欧洲客户因下游成本上升减少采购。没有产品清单、税率、时点和公司地区利润表，无法合理量化净额。

## 6. Thesis、反方证据与下一项验证

### BASF-T2：轻微支持，不升级

- **支持：** BDO措施和Ludwigshafen增产证明“欧洲保护—本地负荷改善”路径在单一产品上可以成立；若扩大到大宗上游，BASF闲置欧洲资产确有经营杠杆。
- **反方：** 已生效措施仍局限于产品链；Citi的利用率桥不闭合；Ludwigshafen结构成本和天然气劣势没有因关税消失。
- **未知：** 10Mt具体产品表、当前负荷、外销量、产品级税则覆盖、实现价与增量contribution margin。
- **Falsifier：** 若广泛措施落地后两个季度，受覆盖产品的欧洲销量、实现价和贡献利润仍不改善，则政策杠杆假设失效。
- **下一项验证：** 10月8日CCA大会后核对正式建议、产品清单、法律工具和生效时点；2026Q3/Q4逐产品核验BASF欧洲volume-price-margin。

### BASF-T3：不变

- **支持：** 中国新建大装置和间接贸易转移仍可绕开单一产品措施；中国反制可能抵消欧洲收益。[G1][P7]
- **反方：** 若欧盟从逐产品反倾销转向广泛、可执行且持续的保护，欧洲价格中枢和负荷可能上移。
- **未知：** 政策能否覆盖大型大宗产品组、WTO约束、下游承受能力及第三国转口。
- **Falsifier：** 若正常化后连续四季BASF欧洲core margin与ROCE上升，且不依赖供应扰动，BASF-T3应下调置信度。
- **下一项验证：** 把政策覆盖产品与BASF欧洲实际产能逐项匹配；没有这张表前，不使用集团€1bn情景。

### 估值与仓位

`NO_CHANGE / WATCHLIST`。Citi的目标价机械上行只有€2，而近三年EPS均下调；12.4%的价格上行不足以替代underlying FCF、Zhanjiang ROCE和欧洲产品级利润兑现。政策消息可能推动90天交易，但不是当前建立长期多头的证据。

## Sources

- **[A1]** Citi, “BASF SE (BASFn.DE): Protecting Europe: The Next Leg Higher?”, 2026-09-03，AlphaEngine `320000610261263`，pp.1–7。
- **[A2]** Citi, “BASF SE (BASFn.DE): Less risky than Air Liquide?”, 2026-08-14，AlphaEngine `320000610125576`；现有桥见 `2026-08-14-citi-consensus-volume-bridge.md`。
- **[P1]** European Commission, [Plan for stronger EU chemical industry](https://commission.europa.eu/news-and-media/news/plan-stronger-eu-chemical-industry-2025-07-08_en), 2025-07-08。
- **[P2]** European Commission, [Critical Chemicals Alliance](https://single-market-economy.ec.europa.eu/sectors/chemicals/critical-chemicals-alliance_en)，accessed 2026-09-03；August 2026 Import Trends Monitoring Tool report，数据更新至2026-05。
- **[P3]** Renewable Carbon Initiative, [Monthly news, August 2026](https://renewable-carbon.eu/news/monthly-news-from-the-renewable-carbon-initiative-rci-august-2026/), published 2026-09-01。
- **[P4]** European Commission, [BDO definitive anti-dumping duties](https://policy.trade.ec.europa.eu/news/commission-imposes-anti-dumping-duties-imports-bdo-three-countries-2026-06-24_en), 2026-06-24。
- **[P5]** European Commission, [Adipic acid definitive anti-dumping duties](https://policy.trade.ec.europa.eu/news/commission-acts-against-unfairly-traded-imports-adipic-acid-2026-05-05_en), 2026-05-05。
- **[P6]** European Commission, [Polyamide yarns definitive anti-dumping duties](https://policy.trade.ec.europa.eu/news/commission-acts-against-dumped-imports-polyamide-yarns-china-2026-07-28_en), 2026-07-28。
- **[P7]** MOFCOM, [Regular Press Conference response on EU “overcapacity” tool](https://english.mofcom.gov.cn/News/PressConference/art/2026/art_fe232668f62042d28245dd87ea2bfaba.html), 2026-05-21。
- **[B1]** BASF, [BDO production increase in Ludwigshafen](https://www.basf.com/global/en/media/news-releases/2026/02/p-26-023), 2026-02。
- **[B2]** BASF, [Ludwigshafen adipic-acid closure](https://www.basf.com/global/en/media/news-releases/2024/08/p-24-269), 2024-08-29；执行期2025。
- **[B3]** BASF, [Our engagement in China](https://www.basf.com/global/en/who-we-are/organization/locations/asia-pacific/our-engagement-in-china)，accessed 2026-09-03。
- **[G1]** Guidepoint Transcript, [former BASF Managing Director on EU anti-dumping and Chinese imports](https://platform.guidepoint.com/transcript?id=D34B1F3D-D1AF-4B5E-8B73-1857E370841F&utm_source=external-mcp&utm_medium=Q%26A+Pairs), interview 2026-05-12。
- **[M1]** `models/basf-model-v0.4-citi-eu-policy-2026-09-03.xlsx`，LibreOffice recalc / Checks verified 2026-09-03；旧版v0.3保留。
