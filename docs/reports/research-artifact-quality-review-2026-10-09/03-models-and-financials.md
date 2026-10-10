# 模型与财务派生产物逐项评估

历史技术评审：保留当时结论与局限；当前运行状态见 [项目状态](../../PROJECT_STATUS.md)。原始操作证据私有保存。

[返回主报告](../research-artifact-quality-review-2026-10-09.md)

本附件保留每类实际执行假设，不以 model spec 的文字计划代替公式。每一期 assumption 的完整 ref、value、reason 与评语见 CSV（历史证据原件已私有归档）；全部 860 个预测单元见 CSV（历史证据原件已私有归档）。金额原值为美元，ratio 原值保留用于复算。

假设输入引用多以 accession／concept／period_end 定位，input-cell ref 常为空；应补 statement-line 身份以便回看确切来源。这是追溯能力不足，不代表原始报表不存在。

## 七家公司：spec 与实际模型

### IBM

环境：legacy；版本：`forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4`；生成：2026-09-11T15:30:30.218642+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

D：旧 schema 0.2，十二季单一集团收入轨道，无季节性、软件／咨询／大型机驱动；OP 直接作为税基且遗漏营业外。NI 数值可算不代表会计成立。

**Model spec** `company-model-spec:e0df50a51e4d8168982dd97854eca770`：Model IBM as a hybrid of renewable software and maintenance contracts, labor-delivered services, infrastructure product sales and financing, not as one homogeneous contract compounder. Begin arithmetic at consolidated filed revenue; reconcile product/service partitions without adding overlapping Software segment or geographic disclosures. Infer growth from retained book, new work, realized pricing, delivery capacity and product mix; isolate acquisition perimeter changes. No amounts, contract KPIs or market proxies are supplied, so pricing, retention and unit economics remain assumptions rather than measured drivers. Retention cannot be empirically covered from this input; deferred income is a timing check, not a renewal rate or full backlog. The key uncertainty is how much growth reflects renewable demand versus project conversion, product refreshes and acquisitions. Evidence that transactional sales or acquisition additions dominate would require a more product-cycle or acquisition-cohort-led structure. Forecast all three statements: financing receivables, debt, pensions, software investment and acquisitions make earnings alone insufficient.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Realized renewal prices and service billing rates | {'basis_concept': None, 'because': 'Pricing slot: Software, Services and Maintenance revenue categories support separate price assumptions. Realized price changes and pricing power are not supplied; this is an analytical inference, not evidence that IBM can raise prices uniformly.', 'kind': 'price', 'label': 'Realized renewal prices and service billing rates', 'ref': 'realized_pricing', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Retained software and maintenance contract book | {'basis_concept': None, 'because': 'Retention slot: Software and Maintenance revenue plus deferred income support a renewable-book hypothesis. Renewal rates, duration and a usable market proxy are absent, so retention cannot be empirically covered. Estimate opening book times retention; do not equate deferred income with backlog.', 'kind': 'contract_book', 'label': 'Retained software and maintenance contract book', 'ref': 'ret… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| New software demand and services bookings converted to delivery | {'basis_concept': None, 'because': 'Software and Services revenue require additions beyond the retained book. Infer new business and expansion with separate recognition lags; the input supplies neither bookings nor conversion schedules. Keep these additions separate from price and renewal effects.', 'kind': 'volume', 'label': 'New software demand and services bookings converted to delivery', 'ref': 'new_work_conversion', 'unit… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Services headcount, utilization and billable output | {'basis_concept': None, 'because': 'Services revenue and cost, together with Consulting goodwill, justify a labor-capacity model as an analytical inference. Use headcount times available hours times utilization as a delivery constraint on converted work, not an additional revenue stream.', 'kind': 'volume', 'label': 'Services headcount, utilization and billable output', 'ref': 'billable_delivery_capacity', 'unit': 'billable ho… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Server and storage shipment volume and configuration mix | {'basis_concept': None, 'because': 'Servers and Storage are explicit Infrastructure revenue breakdowns; finished goods and work-in-process inventory support product economics. Infer shipment and configuration-mix growth separately from recurring Maintenance rather than impose contract retention on all Infrastructure sales.', 'kind': 'mix', 'label': 'Server and storage shipment volume and configuration mix', 'ref': 'infrastruct… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Average financing receivables, realized yield and equipment disposals | {'basis_concept': 'ibm:NotesReceivableAndNetInvestmentInLeaseNoncurrent', 'because': 'Financing and used-equipment sales are explicit revenue categories, with current and long-term financing receivables on the balance sheet. Infer interest/lease revenue from the average earning book and yield; forecast used-equipment disposals separately within this financing schedule.', 'kind': 'contract_book', 'label': 'Average financing rec… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Acquired and divested revenue contribution | {'basis_concept': None, 'because': 'Goodwill identifies HashiCorp, Confluent, Apptio and StreamSets/webMethods, while cash flows identify acquisitions and a QRadar SaaS disposal. Separate perimeter effects from organic contract growth; no acquired revenue amounts or recognition dates are provided.', 'kind': 'segment', 'label': 'Acquired and divested revenue contribution', 'ref': 'acquisition_perimeter', 'unit': 'USD per quarte… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Geographic revenue translation | {'basis_concept': None, 'because': 'Americas, Asia Pacific and Europe/Middle East/Africa revenue breakdowns support a translation overlay, not extra revenue partitions. Currency exposures and exchange-rate proxies are absent, so geography-weighted FX effects are analytical assumptions rather than measured IBM realizations.', 'kind': 'external', 'label': 'Geographic revenue translation', 'ref': 'currency_translation', 'unit': '… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| billable_delivery_capacity | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Split the filed Services cost category into payroll capacity and purchased delivery. Consulting goodwill and compensation liabilities support a labor-based inference. Payroll follows staffing and wage assumptions even when utilization falls; the labor share must be estimated.', 'behaviour': 'variable_with_headcount', 'cost_driver_slot': 'revenue_per_employee', 'driver_ref'… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| new_work_conversion | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Analytically split Services cost to distinguish flexible purchased delivery from employee capacity. Estimate this component against recognized work, not bookings; the filing structure supplies the parent cost but no subcontractor allocation.', 'behaviour': 'variable_with_revenue', 'cost_driver_slot': 'delivery_cost', 'driver_ref': 'new_work_conversion', 'label': 'Subcontra… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| retained_contract_book | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': "Software cost and Maintenance revenue warrant support economics separate from product manufacturing and project labor. Infer a base support team plus scale-related service costs; estimate Maintenance's cost allocation and reconcile all delivery splits to consolidated cost.", 'behaviour': 'semi_variable', 'cost_driver_slot': 'delivery_cost', 'driver_ref': 'retained_contract… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| infrastructure_shipments_mix | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Sales cost, server/storage revenue and raw-material/work-in-process inventory support a materials-plus-capacity split. Model variable product costs with shipments and a fixed manufacturing base; use mix-specific margins rather than a consolidated revenue percentage.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'delivery_cost', 'driver_ref': 'infrastructure_shipments… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| financing_book_yield | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Financing has its own filed cost breakdown and receivables carry allowances. Infer book-linked funding and credit costs; separate equipment disposal costs. Preserve the filed boundary with corporate interest so funding costs are not charged twice.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'delivery_cost', 'driver_ref': 'financing_book_yield', 'label': 'Financing … | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| new_work_conversion | {'basis_concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'because': 'Split SG&A analytically into selling costs and corporate payroll. Contract-compounder unit economics require an estimated acquisition-cost component tied to new work, with deferred contract costs constraining timing; IBM supplies no commission or acquisition-cost breakdown here.', 'behaviour': 'variable_with_revenue', 'cost_driver_slot': 'delivery_… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'because': 'The residual SG&A component should follow corporate staffing and facilities rather than mechanically track revenue. Compensation liabilities and operating leases support this inference. Separate payroll from a fixed occupancy base and exclude separately scheduled noncash allocations.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'revenue_per_… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:ResearchAndDevelopmentExpense', 'because': 'IBM separately files R&D alongside Software and Infrastructure activities. Infer technical staffing and compensation as the main driver, with program costs added explicitly; do not flex the whole line with quarterly shipments or expense capitalized software investment again.', 'behaviour': 'variable_with_headcount', 'cost_driver_slot': 'revenue_per_employee… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:AmortizationOfIntangibleAssets', 'because': 'Cash flow separately reports amortization and depreciation, and the balance sheet identifies acquired technology and capitalized software. Forecast asset-cohort charges, then allocate them into filed expense lines; remove these amounts from cash-cost splits to avoid double counting.', 'behaviour': 'fixed', 'cost_driver_slot': None, 'cost_driver_unbound_rea… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:InterestExpense', 'because': 'IBM files interest within expense and other income and reports short- and long-term debt plus issuance and repayment cash flows. Forecast average debt times effective rates, respecting the separate Financing cost allocation rather than applying a revenue ratio.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Corporate debt funding… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'ibm:IntellectualPropertyAndCustomDevelopmentIncome', 'because': 'The disclosed parent is total expense and other income, not revenue. Forecast a separately signed income offset using an estimated recurring base and deal timing; do not treat it as software top-line growth or net it invisibly against R&D.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'IBM files this as… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'ibm:OtherExpenseAndIncome', 'because': 'Other income/expense includes hedge reclassifications, while cash flow identifies pension settlements and disposal gains. Separate recurring components from explicitly discrete overlays. Reconcile settlements to their actual income-statement location; do not assume the cash-flow addback is a second expense.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cos… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:IncomeTaxExpenseBenefit', 'because': 'The provision is separately filed, with deferred-tax assets, deferred-tax cash-flow adjustments and cash taxes paid. Forecast tax on pretax income plus discrete items and reconcile cash taxes separately; international revenue alone does not reveal taxable-profit allocation.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'T… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Gross renewal retention and contract duration | {'because': 'Not disclosed in the supplied blocks; false does not establish absence from other IBM reporting. Estimate these inputs for Software and Maintenance renewal cohorts. Deferred income, including Extended Warranty, cannot identify churn or total contract duration.', 'disclosed': False, 'label': 'Gross renewal retention and contract duration', 'periodicity': 'annual', 'ref': 'renewal_retention', 'unit': 'percent and mo… | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Realized renewal price and billing-rate change | {'because': 'The supplied revenue breakdowns contain no realized prices or rate cards, and there are no market proxies. Estimate price separately from volume and mix to address the pricing slot; do not interpret total Software or Services growth as pricing.', 'disclosed': False, 'label': 'Realized renewal price and billing-rate change', 'periodicity': 'annual', 'ref': 'realized_price_change', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| New services bookings and book-to-bill | {'because': 'Services revenue and Consulting goodwill justify this analytical demand indicator, but no bookings or backlog KPI is supplied. Estimate recognition lags and bookings only as uncertain model inputs; book-to-bill is not mechanically recoverable from revenue or deferred income.', 'disclosed': False, 'label': 'New services bookings and book-to-bill', 'periodicity': 'quarterly', 'ref': 'bookings_book_to_bill', 'unit': … | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Recurring software contract run rate | {'because': 'Software has a distinct revenue and cost view, but its recurring share and annualized contract base are absent. Estimate a recurring/nonrecurring split to support retained-book arithmetic; never annualize all Software revenue and label it company-disclosed recurring revenue.', 'disclosed': False, 'label': 'Recurring software contract run rate', 'periodicity': 'quarterly', 'ref': 'software_recurring_base', 'unit': … | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Delivery headcount and revenue per employee | {'because': 'Services cost and compensation liabilities support the revenue_per_employee cost slot, but headcount is absent. Estimate delivery staffing and productivity jointly; use this as a capacity and margin consistency check, not an independent extra growth contribution.', 'disclosed': False, 'label': 'Delivery headcount and revenue per employee', 'periodicity': 'annual', 'ref': 'delivery_headcount', 'unit': 'headcount an… | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Billable utilization | {'because': 'The Services revenue/cost structure supports an inferred distinction between paid labor and billable output. No utilization measure is supplied, so estimate it to constrain delivery and explain payroll operating leverage rather than claim observed IBM efficiency.', 'disclosed': False, 'label': 'Billable utilization', 'periodicity': 'quarterly', 'ref': 'utilization', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Contract contribution margin and acquisition-cost payback | {'because': 'Unit_economics slot: filed gross profit, activity costs, SG&A and deferred contract costs provide reconciliation anchors, not customer-cohort economics. Estimate contribution and acquisition-cost allocation separately for software and services; aggregate gross margin cannot establish stable reinvestment returns.', 'disclosed': False, 'label': 'Contract contribution margin and acquisition-cost payback', 'periodicit… | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Server/storage shipment and configuration mix index | {'because': 'Servers and Storage revenue plus inventories justify a product-volume check distinct from contract KPIs. No shipment or configuration metric is supplied; estimate an index and keep its uncertainty separate from realized prices and Maintenance renewals.', 'disclosed': False, 'label': 'Server/storage shipment and configuration mix index', 'periodicity': 'quarterly', 'ref': 'infrastructure_volume_mix', 'unit': 'index… | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'Distinct Software, Services, Sales and Financing costs require gross-margin forecasts by economic activity. Reconcile SG&A, R&D, IP income, interest, taxes and any discontinued operations to net income; use share counts for EPS without summing overlapping breakdowns.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'IBM has financing receivables, deferred income and contract costs, inventory, acquisition goodwill/intangibles, debt and pension balances. These determine earning assets, revenue/cash timing and financing needs; this is not merely a working-capital support schedule.', 'importance': 'required', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'Software investment, acquisitions, dividends and debt repayments compete for operating cash. Pension settlements, noncash amortization and financing-receivable changes separate earnings from available cash. Forecast these explicitly and define any free-cash-flow adjustment rather than assuming net income converts directly.', 'importance': 'required', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': "Three years is appropriate to distinguish IBM's infrastructure seasonality, contract conversion and acquisition effects while linking software investment, debt and cash returns. This is a desired modelling window, not supplied history: the blocks contain structure, no quarterly amounts, and fewer than 12 continuous quarters.", 'forecast_quarters': 12, 'historical_quarters': 12}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfRevenue | revenue_share | 42.0348% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (42.03%), carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:IncomeTaxExpenseBenefit | operating_income_share | 3.9333% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average share of operating income over the 4 quarters filed between 2025-09-30 and 2026-06-30 (3.93%), carried forward unchanged | D／P1：税额除营业利润得 3.9333% 再对 OP 征税，税基与 pretax 不同；缺营业外／递延／一次性桥。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | revenue_share | 21.7503% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (21.75%), carried forward unchanged | C／P1：固定 CFO／收入比没有 NI、D&A、SBC、营运资金与一次性现金桥；利润崩塌也保持现金率，不足为现金预测。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | revenue_share | 1.5735% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (1.57%), carried forward unchanged | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:ResearchAndDevelopmentExpense | revenue_share | 12.7440% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (12.74%), carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:Revenues | quarterly_growth | 1.3537% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average of the 4 quarter-on-quarter changes filed between 2025-06-30 and 2026-06-30 (1.35%), carried forward unchanged; an average of quarters carries no seasonality, so a quarter unlike the ones averaged will be wrong by however much it is unlike them | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 5.53%，须用客户需求／指引校验。 |
| concept:us-gaap:SellingGeneralAndAdministrativeExpense | revenue_share | 29.4537% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30,2028-09-30,2028-12-31,2029-03-31,2029-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (29.45%), carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 12/12 | 17394323840.28796000 | 20167113474.15073849 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost_of_revenue[k] = revenue[k] * share[k] | 12/12 | 7311660748.28282451 | 8477195972.03224934 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:gross_profit | gross_profit[k] = revenue[k] - cost_of_revenue[k] | 12/12 | 10082663092.00513549 | 11689917502.11848915 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:us-gaap:ResearchAndDevelopmentExpense | ResearchAndDevelopmentExpense[k] = revenue[k] * share[k] | 12/12 | 2216734566.55982185 | 2570099186.16900917 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:us-gaap:SellingGeneralAndAdministrativeExpense | SellingGeneralAndAdministrativeExpense[k] = revenue[k] * share[k] | 12/12 | 5123266580.53464460 | 5939954863.24439624 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_income | operating_income[k] = gross_profit[k] - sum(operating_expense[k]) | 12/12 | 2742661944.91066904 | 3179863452.70508374 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:income_tax_expense | income_tax_expense[k] = operating_income[k] * share[k] | 12/12 | 107876158.30754958 | 125072451.54910175 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:net_income | net_income[k] = operating_income[k] - income_tax[k] | 12/12 | 2634785786.60311946 | 3054791001.15598199 | D：数学一致，税基／营业外会计口径有误，不能用于 EPS／回报。 |
| result:operating_cash_flow | operating_cash_flow[k] = revenue[k] * share[k] | 12/12 | 3783320393.56809818 | 4386410899.71183133 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 12/12 | 273691739.63888595 | 317320317.87582018 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 12/12 | 3509628653.92921223 | 4069090581.83601115 | C：CFO−PP&E 算术可成立，尚非公司定义／正常化 FCF；缺租赁、资本化及利润现金桥。 |


独立复算：{'pass': 132}；未计算项不判算术失败，亦不判研究完成。

### CTSH

环境：legacy；版本：`forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6`；生成：2026-09-14T11:37:33.509381+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

C：仅四季，22 条结果行仅六条可算；OP／NI／EPS／CFO／FCF 无法闭环。缩股与 SG&A 连续下降没有预算支持。

**Model spec** `company-model-spec:6e5841d801f23e2b35151fcb4d92cd92`：Model Cognizant provisionally as a people-led technology-services business: delivered work, realised pricing and client/service mix drive sales, while delivery staffing and utilisation drive margins. This is an analytical inference from company identity, service costs, employee restructuring and industry-segment disclosures, not a supplied industry classification. The generic template is only a fallback. Volume and price cannot be calibrated separately from this context; no market proxies are supplied. Acquisitions make organic versus acquired growth a material uncertainty. Contract-level evidence showing predominantly outcome-based or recurring platform economics would require replacing the labour-volume frame. Keep consolidated revenue growth as a forecasting expedient, not an economic driver. The exact supported earnings bridge is pretax income less tax plus equity-method income. Operating income remains filed because complete component evidence is missing for some periods. No EPS numerator note or four-quarter annual share tie is available; EPS forecasting remains unavailable. Truncated history and ambiguous segment values are not filled or used as formula evidence.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Volume of client technology-services work delivered | {'basis_concept': None, 'because': "Analytical inference from Cognizant's identity, service-cost presentation and employee severance disclosures. Covers the volume slot conceptually; no delivered hours or comparable company quantity is supplied, so quantitative coverage is unavailable.", 'kind': 'volume', 'label': 'Volume of client technology-services work delivered', 'ref': 'delivered-work', 'unit': 'billable hours'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Realised fee per unit of delivered work | {'basis_concept': None, 'because': 'Analytical inference paired with delivered work, not a disclosed billing model. The price slot cannot be quantitatively covered: consolidated revenue does not separate rates from hours, and no market proxy is provided.', 'kind': 'price', 'label': 'Realised fee per unit of delivered work', 'ref': 'realised-price', 'unit': 'usd per billable hour'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Client-industry and service mix | {'basis_concept': None, 'because': 'Goodwill is disclosed across Financial Services, Health Sciences, Products and Resources, and Communications, Media and Technology. Different demand and service mixes are an inferred driver; goodwill weights cannot substitute for absent segment sales.', 'kind': 'mix', 'label': 'Client-industry and service mix', 'ref': 'client-service-mix', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Acquired client relationships and delivery capacity | {'basis_concept': None, 'because': 'Business-combination outflows of $1.334 billion in the first half of 2026 and the Belcan acquisition breakdown support separating acquired from organic activity. Revenue contribution is not supplied and cannot be inferred from purchase price.', 'kind': 'volume', 'label': 'Acquired client relationships and delivery capacity', 'ref': 'acquired-capacity', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| None | {'basis_concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'because': 'Analytical split of service costs: employee severance and India contribution disclosures support a staffing exposure that persists between assignments. The allocation is not disclosed; the income structure retains the unsplit filed total.', 'behaviour': 'variable_with_headcount', 'cost_driver_slot': 'fixed_cost', 'driver… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| delivered-work | {'basis_concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'because': 'Analytical variable portion of the same service-cost line, not an additional filed expense. Cost of revenues is $3.652 billion against $5.481 billion sales in Q2 2026; no subcontractor or other flexible-input allocation is provided.', 'behaviour': 'variable_with_revenue', 'cost_driver_slot': 'variable_cost', 'driver_ref'… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'because': 'SG&A falls from $791 million to $728 million while quarterly sales rise, arguing against a purely proportional revenue expense. Retain a separate overhead run rate; the filing does not quantify a headcount versus selling-cost split.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'fixed_cost', 'driver_ref': None, 'label': 'Corporate and adminis… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:DepreciationAndAmortization', 'because': 'D&A is explicitly excluded from service costs and shown separately. $981 million of property and equipment and $1.675 billion of intangibles support asset-driven rather than immediate sales-driven expense; no allocation or amortization schedule is supplied.', 'behaviour': 'fixed', 'cost_driver_slot': 'fixed_cost', 'driver_ref': None, 'label': 'Depreciation an… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:RestructuringCharges', 'because': 'The statement separates restructuring and its employee, facility and program dimensions. Q2 2026 includes $84 million; timing cannot be extrapolated as a sales share or assumed to cease.', 'behaviour': 'one_off', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Program-specific severance and facility exits do not represent normal fixed or variable delivery c… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:InterestExpenseNonoperating', 'because': 'Long-term debt rises from $535 million to $1.527 billion between March and June 2026, with $1 billion of first-half credit-facility proceeds. A revenue-linked interest forecast would miss this financing change.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Financing cost follows borrowing balances and rates, not the … | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| New bookings and book-to-bill | {'because': 'Candidate market metric for the inferred client-services model, not established market coverage in these blocks. No bookings disclosure is supplied; estimates would be directional demand evidence, not a filed revenue bridge or deferred-revenue substitute.', 'disclosed': False, 'label': 'New bookings and book-to-bill', 'periodicity': 'quarterly', 'ref': 'bookings', 'unit': 'usd and ratio'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Delivery staff utilisation | {'because': 'The inferred staffing-led cost structure makes billable deployment a useful margin indicator. No company utilisation disclosure is available here; estimates must remain sensitivity inputs rather than calibrated revenue quantities.', 'disclosed': False, 'label': 'Delivery staff utilisation', 'periodicity': 'quarterly', 'ref': 'utilisation', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Delivery headcount | {'because': 'Employee severance and India contribution disclosures support labour exposure but provide no workforce count. Headcount must be estimated in this bounded model; it cannot be recovered from aggregate service expense.', 'disclosed': False, 'label': 'Delivery headcount', 'periodicity': 'quarterly', 'ref': 'headcount', 'unit': 'headcount'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Voluntary employee attrition | {'because': 'An inferred staffing model makes retention relevant to hiring cost and available delivery capacity. The provided evidence does not establish company disclosure or market tracking; any estimate is a supplementary sensitivity only.', 'disclosed': False, 'label': 'Voluntary employee attrition', 'periodicity': 'quarterly', 'ref': 'attrition', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Organic constant-currency sales growth | {'because': 'Acquisition payments and foreign-currency statement items make reported growth insufficient to isolate underlying client demand. No company organic or constant-currency reconciliation is supplied, so estimates cannot be treated as actuals.', 'disclosed': False, 'label': 'Organic constant-currency sales growth', 'periodicity': 'quarterly', 'ref': 'organic-growth', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'Separate delivery costs, overhead, D&A and restructuring are necessary to distinguish recurring service margins from program charges; equity-method income also enters the filed final earnings bridge.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'June 2026 receivables are $4.780 billion, goodwill $8.083 billion and long-term debt $1.527 billion. Acquisition funding, collections and repurchases make this more than a simple working-capital-only balance sheet.', 'importance': 'required', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'First-half 2026 operating cash of $832 million compares with $1.334 billion of acquisitions and $1.607 billion of repurchases. Liquidity and capital allocation cannot be assessed from net income alone.', 'importance': 'required', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': 'Eight quarters are the desired basis for distinguishing acquisition effects, restructuring and cash seasonality. The bounded ledger does not supply that complete history; a four-quarter forecast limits extrapolation without fabricating omitted observations.', 'forecast_quarters': 4, 'historical_quarters': 8}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization | share_of_line | 66.5591% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30 | the trailing filed history through 2026-06-30 for structure line cost-of-revenue is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:DepreciationAndAmortization | quarterly_growth | 0.7397% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30 | the trailing filed history through 2026-06-30 for structure line da is carried forward unchanged | C：D&A 增减应来自资产 vintage、寿命、摊销和处置；历史 QoQ 无法表达新增 capex 滞后。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | share_of_line | 1.4729% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (1.47%), carried forward against revenue by the cash-flow companion | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:Revenues | quarterly_growth | 1.1208% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30 | the trailing filed history through 2026-06-30 for structure line revenue is carried forward unchanged | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 4.56%，须用客户需求／指引校验。 |
| concept:us-gaap:SellingGeneralAndAdministrativeExpense | quarterly_growth | -2.5569% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30 | the trailing filed history through 2026-06-30 for structure line sga is carried forward unchanged | C：SG&A 自动下降需工资、招聘、重组与销售投资支持；改善不能无期限复利。 |
| concept:us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding | quarterly_growth | -2.3061% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30 | the trailing filed history through 2026-06-30 for structure line diluted-shares is carried forward unchanged | C／P1：每季 −2.3061%（约 −8.9% 年化）需回购美元、价格与 SBC 发行桥；不能当免费 EPS 增长。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 4/4 | 5542431030.57042000 | 5730886189.21042292 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost-of-revenue[k] = revenue[k] * share[k] | 4/4 | 3688993706.63480569 | 3814427814.94779350 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:sga | sga[k] = sga[k-1] * (1 + growth[k]) | 4/4 | 709386069.57545600 | 656351505.08057419 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:da | da[k] = da[k-1] * (1 + growth[k]) | 4/4 | 144057746.43717600 | 147278161.47924066 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:restructuring | restructuring[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_operating_income_expense | property-sale-gain[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_income | operating-income[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:interest_income | interest-income[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:interest_expense:interest-expense | interest-expense[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:interest_expense:legacy-interest-expense | legacy-interest-expense[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_nonoperating_income_expense:fx | fx[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_nonoperating_income_expense:other-net | other-net[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:nonoperating_income_expense | nonoperating-total[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:pretax_income | pretax-income[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:income_tax_expense | income-tax[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:company_presented_component | equity-method-income[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"pretax-income"},{"coefficient":"-1","line_ref":"income-tax"},{"coefficient":"1","line_ref":"equity-method-income"}]} | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:diluted_weighted_average_shares | diluted-shares[k] = diluted-shares[k-1] * (1 + growth[k]) | 4/4 | 455253668.76296400 | 424478860.98764422 | C：股数复利缩减缺回购现金／价格与 SBC 约束。 |
| result:diluted_eps | diluted-eps[k] unavailable by company structure | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_cash_flow | operating_cash_flow[k] = None[k] * share[k] | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 4/4 | 81631849.30272670 | 84407516.33864810 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 0/4 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |


独立复算：{'pass': 24}；未计算项不判算术失败，亦不判研究完成。

### EPAM

环境：legacy；版本：`forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9`；生成：2026-09-29T07:05:10.162553+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

C：首季持平较贴近指引，之后机械 +1.12%；16 行八行可算，OP 可算但 NI／EPS 缺。AI 净增量未进入模型。

**Model spec** `company-model-spec:492b980e22b7f043b6735853809de0d9`：EPAM is a labour-arbitrage engineering-services firm. In Q2 2026 time-and-materials work was about $1.10bn of $1.41bn revenue, so revenue is billable headcount × utilisation × rate. The turnaround template fits only partly. Cash runway does not constrain the company: cash is $789m against $25m long-term debt at 2026-06-30. The self-help question is margin: Q2 2026 operating margin was about 10.8%, with cost of revenues at 69.6% and SG&A at 17.3% of revenue. The cash signal to watch is H1 2026 operating cash flow of -$38.8m, while $425m of financing outflows included buybacks. The key uncertainty is AI-driven deflation in rates and demand versus headcount utilisation. Headcount and utilisation are not in the provided data, so the drivers are analytical inferences. If a disclosed headcount series showed revenue per head diverging from utilisation, the model should split rate from volume explicitly. Q2 2026 operating income ties exactly without the loss-on-sale line, which was zero in 2024 and 2025. A non-zero divestiture loss would require adding it to the bridge.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Billable delivery headcount | {'basis_concept': None, 'because': 'About 78% of Q2 2026 revenue ($1.10bn of $1.41bn) is time-and-materials, so revenue moves with engineers deployed. Headcount is not in the provided data; this is an analytical inference.', 'kind': 'volume', 'label': 'Billable delivery headcount', 'ref': 'billable-headcount', 'unit': 'headcount'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Blended realised rate per billable engineer | {'basis_concept': None, 'because': 'With T&M dominant, pricing and delivery-location mix set revenue per head. This is inferred; no rate data is filed in context.', 'kind': 'price', 'label': 'Blended realised rate per billable engineer', 'ref': 'revenue-per-head', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Americas segment revenue | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': 'Americas was $792m of Q2 2026 revenue and Europe $623m. The US alone was $715m, so US client spend is the single largest swing.', 'kind': 'segment', 'label': 'Americas segment revenue', 'ref': 'americas-segment', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Europe segment revenue | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': 'Europe is $623m of Q2 2026 revenue, with UK and Switzerland the largest countries, and is exposed to FX and European discretionary demand.', 'kind': 'segment', 'label': 'Europe segment revenue', 'ref': 'europe-segment', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Fixed-price contract mix | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': 'Fixed-price work was about $305m in Q2 2026. Moving toward fixed-price or AI-enabled outcome pricing changes revenue per engineer.', 'kind': 'mix', 'label': 'Fixed-price contract mix', 'ref': 'fixed-price-mix', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| billable-headcount | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': '$985m in Q2 2026, 69.6% of revenue. This is mostly engineer compensation, so it follows headcount and wage inflation rather than billed rates.', 'behaviour': 'variable_with_headcount', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Delivery payroll scales with headcount; it is not a removable fixed cost.', 'driver_ref': 'billable-headcount', 'label': 'Cost of rev… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'because': '$245m in Q2 2026, 17.3% of revenue. It includes non-cash stock compensation (H1 2026 $96m) and humanitarian costs; this is the lever for margin self-help.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'fixed_cost_removal', 'driver_ref': None, 'label': 'SG&A overhead', 'ref': 'sga-overhead'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:DepreciationAndAmortization', 'because': 'About $32m per quarter, driven by acquired intangibles ($373m, including NEORIS and First Derivative) and office PP&E.', 'behaviour': 'fixed', 'cost_driver_slot': 'fixed_cost_removal', 'driver_ref': None, 'label': 'Depreciation and amortization', 'ref': 'd-and-a'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:GainLossOnSaleOfBusiness', 'because': 'A $25.9m loss in 2023 from the Russia exit; zero in 2024 and 2025. It stays unforecast.', 'behaviour': 'one_off', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'A divestiture event, not a recurring fixed cost.', 'driver_ref': None, 'label': 'Loss on sale of business', 'ref': 'business-disposal'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Delivery / total headcount | {'because': 'This is the core volume driver. It is not in the provided data and must be estimated here, even though the company may state it outside the statements.', 'disclosed': False, 'label': 'Delivery / total headcount', 'periodicity': 'quarterly', 'ref': 'headcount', 'unit': 'headcount'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Billable utilisation | {'because': 'It bridges headcount to revenue and delivery margin. Not in context; estimated.', 'disclosed': False, 'label': 'Billable utilisation', 'periodicity': 'quarterly', 'ref': 'utilisation', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Voluntary attrition | {'because': 'It drives wage pressure inside delivery cost. Not in context; estimated.', 'disclosed': False, 'label': 'Voluntary attrition', 'periodicity': 'quarterly', 'ref': 'attrition', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Top-20 client revenue growth | {'because': 'Large-account spend cuts or AI-driven scope compression would appear here first. Not in context.', 'disclosed': False, 'label': 'Top-20 client revenue growth', 'periodicity': 'annual', 'ref': 'top-client-concentration', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'The margin recovery thesis shows up here, in delivery cost and SG&A ratios against revenue.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'The balance sheet is mostly receivables ($1.27bn) and accrued compensation ($496m), plus a net cash position. Working capital matters; leverage does not.', 'importance': 'supporting', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'H1 2026 operating cash flow was -$38.8m while buybacks continued. Cash conversion and buyback capacity drive per-share value, but runway is not binding.', 'importance': 'supporting', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': 'Eight quarters cover the post-NEORIS base since late 2024 and the 2026 margin trajectory. Two years is the realistic horizon to judge a margin recovery.', 'forecast_quarters': 8, 'historical_quarters': 8}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfRevenue | share_of_line | 70.5916% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line cost-of-revenue is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:DepreciationAndAmortization | quarterly_growth | 0.6674% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line d-and-a is carried forward unchanged | C：D&A 增减应来自资产 vintage、寿命、摊销和处置；历史 QoQ 无法表达新增 capex 滞后。 |
| concept:us-gaap:IncomeTaxExpenseBenefit | share_of_line | 26.9810% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line income-tax is carried forward unchanged | C：历史平均税率仅是先验，需 pretax 正常化、地区结构、一次性税和公司税率指引；关联税前行 unavailable 时本假设并未产生税额。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | share_of_line | 9.6159% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (9.62%), carried forward against revenue by the cash-flow companion | C／P1：固定 CFO／收入比没有 NI、D&A、SBC、营运资金与一次性现金桥；利润崩塌也保持现金率，不足为现金预测。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | share_of_line | 0.9991% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (1.00%), carried forward against revenue by the cash-flow companion | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | quarterly_growth | 0.0000% | 2026-09-30 | 管理层表示全年指引中值要求公司基本持平，下半年增速会放缓。大单尚未签约，要到 2027 年初才会贡献收入，所以第三季度收入环比增长按 0 假设。这个数字依据的是公司口头指引，公司没有给出具体的季度数字。 | B／C：首预测季 0% 是指引 midpoint 下的保守覆盖；需同期间核 guide 范围和有机／收购／FX，不保证未来各季。 |
| concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | quarterly_growth | 1.1219% | 2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line revenue is carried forward unchanged | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 4.56%，须用客户需求／指引校验。 |
| concept:us-gaap:SellingGeneralAndAdministrativeExpense | share_of_line | 17.1452% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line sga is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 8/8 | 1414767000.00000000 | 1529679743.08444295 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost-of-revenue[k] = revenue[k] * share[k] | 8/8 | 998706167.77021492 | 1079824871.60895818 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:sga | sga[k] = revenue[k] * share[k] | 8/8 | 242564309.96315467 | 262266303.45908098 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:d-and-a | d-and-a[k] = d-and-a[k-1] * (1 + growth[k]) | 8/8 | 32315226.09832743 | 33855377.42056474 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:other_operating_income_expense | loss-on-sale[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"revenue"},{"coefficient":"-1","line_ref":"cost-of-revenue"},{"coefficient":"-1","line_ref":"sga"},{"coefficient":"-1","line_ref":"d-and-a"}]} | 8/8 | 141181296.16830298 | 153733190.59583905 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:nonoperating_income_expense | interest-other[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_nonoperating_income_expense | fx-loss[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:pretax_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"operating-income"},{"coefficient":"1","line_ref":"fx-loss"},{"coefficient":"1","line_ref":"interest-other"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:income_tax_expense | income-tax[k] = pretax-income[k] * share[k] | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"pretax-income"},{"coefficient":"-1","line_ref":"income-tax"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:diluted_weighted_average_shares | diluted-shares[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:diluted_eps | diluted-eps[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_cash_flow | operating_cash_flow[k] = revenue[k] * share[k] | 8/8 | 136042246.93382540 | 147092114.34700158 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 8/8 | 14134699.94385209 | 15282773.89753038 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 8/8 | 121907546.98997331 | 131809340.44947120 | C：CFO−PP&E 算术可成立，尚非公司定义／正常化 FCF；缺租赁、资本化及利润现金桥。 |


独立复算：{'pass': 64}；未计算项不判算术失败，亦不判研究完成。

### ACN

环境：legacy；版本：`forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8`；生成：2026-10-07T23:43:02.732095+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

C：十九行七行可算，无完整 OP／NI／普通股 NI／EPS；咨询／托管差异与重组未执行；财务锚仍截至 FY26 Q3。

**Model spec** `company-model-spec:c9cb679d74ff3962437a3e82d87069dd`：Accenture's revenue comes from two books that behave differently. Consulting (9.33bn USD in Q3 FY26) is short-cycle and depends on discretionary demand. Managed Services (9.39bn USD) is multi-year contracted run work, where renewal and retention set growth. Pricing is set per contract and is not filed. Margin depends on delivery cost, almost all of it people, filed only as Cost of services (about 67% of revenue). The model must get right each book's growth, the cost-of-services ratio, and the timing of business optimization charges (307.5m USD in 9M FY26, zero in Q3). About 60% of revenue is earned outside the Americas segment; FXE and UUP show only the direction of translation, not its size. Retention, bookings, headcount and utilisation are not in this context and must be estimated. A persistent divergence between Managed Services and Consulting growth, or a recurring optimization charge (then modelled as a run-rate cost), would require a different structure. The diluted EPS numerator is not disclosed, so EPS is left out of the bridge.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Managed Services contracted run revenue | {'basis_concept': 'us-gaap:Revenues', 'because': 'Filed as a ProductOrServiceAxis member (9.39bn USD in Q3 FY26), about half of revenue. Its growth depends on renewals and how much multi-year scope is kept (the retention slot). No retention figure is filed, so retention is an analytical inference.', 'kind': 'contract_book', 'label': 'Managed Services contracted run revenue', 'ref': 'managed-services-book', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Consulting project demand | {'basis_concept': 'us-gaap:Revenues', 'because': 'Filed Consulting member, 9.33bn USD in Q3 FY26. These are shorter, discretionary projects, so this book reacts to client budgets faster than Managed Services.', 'kind': 'segment', 'label': 'Consulting project demand', 'ref': 'consulting-demand', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Contract pricing and rate realisation | {'basis_concept': 'us-gaap:Revenues', 'because': 'This is the pricing slot. No rate or price data is filed, so it is inferred from revenue growth against estimated headcount. It is an analytical estimate.', 'kind': 'price', 'label': 'Contract pricing and rate realisation', 'ref': 'pricing-realisation', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Americas / EMEA / Asia Pacific segment mix | {'basis_concept': 'us-gaap:Revenues', 'because': 'Segment revenue is filed (Americas 9.14bn, EMEA 6.87bn, APAC 2.71bn USD in Q3 FY26). Segment operating margins differ, so mix moves operating income.', 'kind': 'mix', 'label': 'Americas / EMEA / Asia Pacific segment mix', 'ref': 'geographic-mix', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Currency translation of non-USD revenue | {'basis_concept': None, 'because': "EMEA and Asia Pacific make up about half of revenue. The FXE/UUP proxies show only the direction: FXE excludes GBP and hedging, and UUP's currency weights differ from the company's revenue basket, so neither gives the size of the effect.", 'kind': 'external', 'label': 'Currency translation of non-USD revenue', 'ref': 'fx-translation', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| managed-services-book | {'basis_concept': 'us-gaap:CostOfGoodsAndServicesSold', 'because': 'Cost of services is 12.58bn of 18.72bn USD revenue in Q3 FY26. It is mostly billable staff, so it follows headcount and utilisation rather than invoicing.', 'behaviour': 'variable_with_headcount', 'driver_ref': 'managed-services-book', 'label': 'Cost of services (delivery workforce)', 'ref': 'delivery-labour'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| consulting-demand | {'basis_concept': 'us-gaap:SellingAndMarketingExpense', 'because': '1.81bn USD in Q3 FY26 (about 9.7% of revenue). This spend wins new bookings and follows the pipeline, with some lag.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Business-development spend serves neither the delivery-cost slot nor the revenue-per-employee slot.', 'driver_ref': 'consulting-demand', 'label': 'Sales and… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:GeneralAndAdministrativeExpense', 'because': '1.15bn USD in Q3 FY26 (about 6.1% of revenue). This is corporate overhead and scales more slowly than revenue.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'revenue_per_employee', 'driver_ref': None, 'label': 'General and administrative', 'ref': 'g-and-a'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:RestructuringSettlementAndImpairmentProvisions', 'because': '307.5m USD in 9M FY26 and zero in Q3 FY26. These are programme-specific severance charges and do not recur.', 'behaviour': 'one_off', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'A discretionary programme charge, not a cost of serving contracts.', 'driver_ref': None, 'label': 'Business optimization costs', 'ref': 'business-optim… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| New bookings and book-to-bill | {'because': 'Bookings show where Managed Services revenue is heading. They are not in this context, so they must be estimated or sourced from earnings releases.', 'disclosed': False, 'label': 'New bookings and book-to-bill', 'periodicity': 'quarterly', 'ref': 'new-bookings', 'unit': 'USD'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Employee headcount | {'because': 'Headcount drives cost of services and revenue per employee. It is not filed in these statements.', 'disclosed': False, 'label': 'Employee headcount', 'periodicity': 'quarterly', 'ref': 'headcount', 'unit': 'headcount'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Voluntary attrition | {'because': 'Attrition drives wage inflation and delivery cost. It is not in this context.', 'disclosed': False, 'label': 'Voluntary attrition', 'periodicity': 'quarterly', 'ref': 'attrition', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Billable utilisation | {'because': 'Utilisation links headcount to billed revenue. It is not filed, so it must be estimated.', 'disclosed': False, 'label': 'Billable utilisation', 'periodicity': 'quarterly', 'ref': 'utilisation', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'Earnings and margins depend on the cost-of-services ratio and restructuring timing.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'Assets are mostly receivables (16.0bn USD), payroll accruals (8.7bn) and goodwill (25.3bn) from acquisitions. Working capital matters; the asset base does not.', 'importance': 'supporting', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'Operating cash flow was 9.27bn USD in 9M FY26 against capex of only 0.49bn. That cash pays for buybacks (5.19bn), dividends and acquisitions (3.0bn), so the per-share outcome depends on cash generation.', 'importance': 'required', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': 'The eight available filings cover FY24 Q3 through FY26 Q3. They include both optimization programmes and the change from Outsourcing to Managed Services naming, which is enough to set the cost ratios.', 'forecast_quarters': 8, 'historical_quarters': 8}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfGoodsAndServicesSold | share_of_line | 68.0050% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line cost-of-services is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:GeneralAndAdministrativeExpense | quarterly_growth | 1.6434% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line g-and-a is carried forward unchanged | C：管理费季度增长应核一次性与人员预算，不能仅用同比／QoQ 历史均值。 |
| concept:us-gaap:IncomeTaxExpenseBenefit | share_of_line | 25.7739% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line income-tax is carried forward unchanged | C：历史平均税率仅是先验，需 pretax 正常化、地区结构、一次性税和公司税率指引；关联税前行 unavailable 时本假设并未产生税额。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | share_of_line | 18.1270% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the average share of revenue over the 4 quarters filed between 2025-08-31 and 2026-05-31 (18.13%), carried forward against revenue by the cash-flow companion | C／P1：固定 CFO／收入比没有 NI、D&A、SBC、营运资金与一次性现金桥；利润崩塌也保持现金率，不足为现金预测。 |
| concept:us-gaap:NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsNonredeemable | share_of_line | 1.8353% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line nci-other is carried forward unchanged | C：NCI 比率需对应子公司利润及权益结构；母公司利润链未完成时，本参数不能补普通股 NI。 |
| concept:us-gaap:NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsRedeemable | share_of_line | 0.0922% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line nci-canada is carried forward unchanged | C：NCI 比率需对应子公司利润及权益结构；母公司利润链未完成时，本参数不能补普通股 NI。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | share_of_line | 0.8183% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the average share of revenue over the 4 quarters filed between 2025-08-31 and 2026-05-31 (0.82%), carried forward against revenue by the cash-flow companion | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:Revenues | quarterly_growth | 1.4452% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line revenue is carried forward unchanged | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 5.91%，须用客户需求／指引校验。 |
| concept:us-gaap:SellingAndMarketingExpense | share_of_line | 9.8904% | 2026-08-31,2026-11-30,2027-02-28,2027-05-31,2027-08-31,2027-11-30,2028-02-29,2028-05-31 | the trailing filed history through 2026-05-31 for structure line sales-marketing is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 8/8 | 18988657262.99874490 | 20994936132.22815436 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost-of-services[k] = revenue[k] * share[k] | 8/8 | 12913231010.53974812 | 14277600389.11646348 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:sales-marketing | sales-marketing[k] = revenue[k] * share[k] | 8/8 | 1878048024.18558146 | 2076476381.39563407 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:g-and-a | g-and-a[k] = g-and-a[k-1] * (1 + growth[k]) | 8/8 | 1166875898.21713938 | 1307917369.00607692 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:business-optimization | business-optimization[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:company_presented_component | total-operating-expenses[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"revenue"},{"coefficient":"-1","line_ref":"total-operating-expenses"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:interest_income | interest-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:interest_expense | interest-expense[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_nonoperating_income_expense | other-nonoperating[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:pretax_income | pretax-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:income_tax_expense | income-tax[k] = pretax-income[k] * share[k] | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"pretax-income"},{"coefficient":"-1","line_ref":"income-tax"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:noncontrolling_interest:nci-other | nci-other[k] = net-income[k] * share[k] | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:noncontrolling_interest:nci-canada | nci-canada[k] = net-income[k] * share[k] | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:parent_net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"net-income"},{"coefficient":"-1","line_ref":"nci-other"},{"coefficient":"-1","line_ref":"nci-canada"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_cash_flow | operating_cash_flow[k] = revenue[k] * share[k] | 8/8 | 3442069777.17675390 | 3805747511.98001647 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 8/8 | 155383046.99433500 | 171800307.01980683 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 8/8 | 3286686730.18241890 | 3633947204.96020964 | C：CFO−PP&E 算术可成立，尚非公司定义／正常化 FCF；缺租赁、资本化及利润现金桥。 |


独立复算：{'pass': 56}；未计算项不判算术失败，亦不判研究完成。

### DXC

环境：legacy；版本：`forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9`；生成：2026-10-07T23:48:04.501756+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

C：十八行九行可算，收入下降方向有依据但成本节省机械；税前／净利未完成，FCF 漏合同／软件资本化及正常化。

**Model spec** `company-model-spec:eed542572b3ebbf4f42cad953652ac44`：DXC is an IT services turnaround with shrinking revenue. FY26 (year to 2026-03-31) revenue was $12,644m against $12,326m of costs and expenses, leaving $318m of pretax income; Q1 FY27 revenue was $2,999m. Small changes in revenue run-off or the cost base swing earnings sharply, and FY26 tax of $290m on $318m pretax means tax also decides EPS. Segment values (GIS, CES, Insurance) are ambiguous and cannot drive formulas, so revenue is forecast at consolidated level with segment and geographic mix as overlays. Other Europe, UK and Australia are most of revenue, so FX translation matters and only proxies exist. Cash is the other half of the thesis: Q1 FY27 operating cash flow was $418m against $59m of capex, and debt fell after the 2025 note repayments. The key uncertainty is whether GIS shrinkage is slowing; if segment values become unambiguous or bookings show a lasting book-to-bill above 1, rebuild revenue as a segment or contract-book model. Interest income, other income and disposal gains stay unavailable, so forecast pretax income is incomplete rather than treating them as zero.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| GIS infrastructure contract run-off and renewals | {'basis_concept': None, 'because': 'GIS is the largest operating-segment revenue member: the $6,342m FY26 value is read as GIS, but that is an analytical inference because the segment values are ambiguous. Renewals of long infrastructure contracts that come in smaller than the expiring ones set the rate at which revenue shrinks.', 'kind': 'segment', 'label': 'GIS infrastructure contract run-off and renewals', 'ref': 'gis-runof… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 DXC 分部名称／数值已正式披露，不应按大小推定。 |
| CES consulting and engineering project demand | {'basis_concept': None, 'because': 'CES is the only segment named in the filed structure with its own revenue, cost and SG&A lines. Project demand there moves faster than the infrastructure contracts, so CES shows first whether the turnaround is working.', 'kind': 'segment', 'label': 'CES consulting and engineering project demand', 'ref': 'ces-demand', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 DXC 分部名称／数值已正式披露，不应按大小推定。 |
| Insurance software and BPS volume | {'basis_concept': None, 'because': 'Insurance is the smallest segment (about $1,279m in FY26, by inference). It carries $149m of goodwill and is the most annuity-like part of the business, which steadies the revenue mix.', 'kind': 'segment', 'label': 'Insurance software and BPS volume', 'ref': 'insurance-platform', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 DXC 分部名称／数值已正式披露，不应按大小推定。 |
| Translation of European and Australian revenue | {'basis_concept': None, 'because': "In FY26, Other Europe ($4,249m), UK ($1,862m) and Australia ($1,093m) exceeded half of revenue. FXE and UUP can show the direction of translation only: they leave out GBP and AUD weights and the company's hedges, so they bound the effect without measuring its size.", 'kind': 'external', 'label': 'Translation of European and Australian revenue', 'ref': 'fx-translation', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 DXC 分部名称／数值已正式披露，不应按大小推定。 |
| Contract book renewal and new bookings | {'basis_concept': None, 'because': 'Bookings are not in the filed data. Bookings and book-to-bill lead revenue by several quarters in multi-year outsourcing contracts, so they have to be estimated or taken from earnings releases.', 'kind': 'contract_book', 'label': 'Contract book renewal and new bookings', 'ref': 'bookings-book', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 DXC 分部名称／数值已正式披露，不应按大小推定。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| gis-runoff | {'basis_concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'because': 'This was $9,613m, or 76% of revenue, in FY26. Delivery staff and subcontractors scale with the contracts being run, so the line follows the revenue base, mostly GIS.', 'behaviour': 'variable_with_revenue', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Delivery cost follows contract volume. It is not a fixed co… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'because': 'This was $1,402m in FY26 and $328m in Q1 FY27. It mixes sales cost, which follows bookings, with corporate overhead that the restructuring plans target. Modelled here as a share of revenue.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'fixed_cost_removal', 'driver_ref': None, 'label': 'SG&A (excluding D&A and restructuring)', 'ref': 'sga'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:DepreciationAndAmortization', 'because': 'This was $1,160m in FY26. It follows the past base of equipment, software and transition and transformation contract costs rather than current revenue.', 'behaviour': 'fixed', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Follows past capital spending, not restructuring.', 'driver_ref': None, 'label': 'Depreciation and amortization', 'ref': 'd-and-… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:RestructuringCharges', 'because': "This was $115m in FY26 and $26m in Q1 FY27. Charges come from successive plans by fiscal year (FY24 to FY27), and the FY27 plan's $17m of Q1 charges are mostly workforce cuts.", 'behaviour': 'one_off', 'cost_driver_slot': 'fixed_cost_removal', 'driver_ref': None, 'label': 'Restructuring costs', 'ref': 'restructuring'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:InterestExpense', 'because': "This was $216m in FY26. It depends on the stock of notes, which fell after €650m and $300m of repayments in Q3 FY26. IEF shows only the direction of base rates and excludes DXC's credit spread.", 'behaviour': 'fixed', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Financing cost depends on debt, not on fixed-cost removal.', 'driver_ref': None, 'label': 'Interes… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:InterestIncomeOperating', 'because': 'This was $89m in Q1 FY27 against $181m for all of FY26, which is volatile. It depends on cash balances and rates, and the data here cannot support a forecast.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Treasury income, not an operating cost.', 'driver_ref': None, 'label': 'Interest income', 'ref': 'interest-income'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:OtherOperatingIncomeExpenseNet', 'because': 'This swung from -$1m in FY26 to +$217m in Q1 FY27. Pension and FX items are likely contributors, but there is no note evidence, so the line stays unavailable.', 'behaviour': 'one_off', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Non-operating items with no restructuring link.', 'driver_ref': None, 'label': 'Other income, net', 'ref': 'other-in… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Total bookings | {'because': 'Leads revenue in a multi-year contract business. Reported in earnings materials, not in the XBRL statements available here.', 'disclosed': True, 'label': 'Total bookings', 'periodicity': 'quarterly', 'ref': 'bookings', 'unit': 'USD'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Book-to-bill (trailing 12 months) | {'because': 'A lasting ratio above 1 is the observation that would show GIS shrinkage has stopped.', 'disclosed': True, 'label': 'Book-to-bill (trailing 12 months)', 'periodicity': 'quarterly', 'ref': 'book-to-bill', 'unit': 'ratio'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Organic revenue growth | {'because': 'Removes the FX translation that drives large swings given Europe and Australia exposure.', 'disclosed': True, 'label': 'Organic revenue growth', 'periodicity': 'quarterly', 'ref': 'organic-growth', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Employees | {'because': 'Annual only. Quarterly figures must be estimated to track how far the workforce cuts have gone.', 'disclosed': True, 'label': 'Employees', 'periodicity': 'annual', 'ref': 'headcount', 'unit': 'headcount'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Adjusted free cash flow | {'because': "The company's own definition includes software and transition contract cost outflows. These differ from the property and equipment purchases used in the companion.", 'disclosed': True, 'label': 'Adjusted free cash flow', 'periodicity': 'quarterly', 'ref': 'adj-fcf', 'unit': 'USD'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'Pretax income is a thin residual ($318m on $12.6bn of revenue in FY26). Delivery cost ratio, restructuring and tax decide EPS.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'Debt ($3,003m long-term plus $501m current) and $1,957m of cash determine whether the company can keep paying for restructuring and buybacks. The rest of the balance sheet is working capital and intangible assets.', 'importance': 'supporting', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'The thesis rests on free cash flow funding debt reduction and buybacks ($71m in Q1 FY27). Operating cash flow ($418m in Q1) diverges sharply from net income ($126m).', 'importance': 'required', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': "Eight quarters cover the FY26 and FY27 restructuring plans and the 2025 debt refinancing. An eight-quarter forecast reaches the point where the FY27 plan's savings should show.", 'forecast_quarters': 8, 'historical_quarters': 8}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization | share_of_line | 77.0379% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line cost-of-services is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:DepreciationAndAmortization | quarterly_growth | -3.1880% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line d-and-a is carried forward unchanged | C：D&A 增减应来自资产 vintage、寿命、摊销和处置；历史 QoQ 无法表达新增 capex 滞后。 |
| concept:us-gaap:InterestExpense | quarterly_growth | 0.4717% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line interest-expense is carried forward unchanged | C：利息季度延长缺平均债务、利率、偿还／发行和租赁桥；应据融资计划预测。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | share_of_line | 11.8686% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (11.87%), carried forward against revenue by the cash-flow companion | C／P1：固定 CFO／收入比没有 NI、D&A、SBC、营运资金与一次性现金桥；利润崩塌也保持现金率，不足为现金预测。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | share_of_line | 1.8294% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (1.83%), carried forward against revenue by the cash-flow companion | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:RestructuringCharges | quarterly_growth | -5.0548% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line restructuring is carried forward unchanged | C：重组季季下降需执行计划、费用／现金支付日期；可节省不等于实际已兑现。 |
| concept:us-gaap:Revenues | quarterly_growth | -1.2704% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line revenue is carried forward unchanged | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 -4.99%，须用客户需求／指引校验。 |
| concept:us-gaap:SellingGeneralAndAdministrativeExpense | share_of_line | 10.7072% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line sga is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 8/8 | 2960899384.75189600 | 2707409432.42083940 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost-of-services[k] = revenue[k] * share[k] | 8/8 | 2281015669.69936637 | 2085732251.30420327 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:sga | sga[k] = revenue[k] * share[k] | 8/8 | 317030606.77480928 | 289888828.90397281 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:d-and-a | d-and-a[k] = d-and-a[k-1] * (1 + growth[k]) | 8/8 | 258488078.77454100 | 206037048.32726765 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:restructuring | restructuring[k] = restructuring[k-1] * (1 + growth[k]) | 8/8 | 24685760.44989600 | 17169539.47342912 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:interest_expense | interest-expense[k] = interest-expense[k-1] * (1 + growth[k]) | 8/8 | 55259433.96226000 | 57110061.72762813 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:interest_income | interest-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_nonoperating_income_expense:other-income | other-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:other_nonoperating_income_expense:gain-on-disposition | gain-on-disposition[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:company_presented_subtotal | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"cost-of-services"},{"coefficient":"1","line_ref":"sga"},{"coefficient":"1","line_ref":"d-and-a"},{"coefficient":"1","line_ref":"restructuring"},{"coefficient":"1","line_ref":"interest-expense"},{"coefficient":"-1","line_ref":"interest-income"},{"coefficient":"-1","line_ref":"other-income"},{"coefficient":"-1","line_ref":"gain-on-disposition"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:pretax_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"revenue"},{"coefficient":"-1","line_ref":"total-costs"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:income_tax_expense | income-tax[k] = pretax-income[k] * share[k] | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"pretax-income"},{"coefficient":"-1","line_ref":"income-tax"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:noncontrolling_interest | nci[k] = net-income[k] * share[k] | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:parent_net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"net-income"},{"coefficient":"-1","line_ref":"nci"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_cash_flow | operating_cash_flow[k] = revenue[k] * share[k] | 8/8 | 351418109.94759824 | 321332332.49847661 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 8/8 | 54167340.62982659 | 49529940.02619027 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 8/8 | 297250769.31777165 | 271802392.47228634 | C：CFO−PP&E 算术可成立，尚非公司定义／正常化 FCF；缺租赁、资本化及利润现金桥。 |


独立复算：{'pass': 72}；未计算项不判算术失败，亦不判研究完成。

### META

环境：hyperscalers；版本：`forecast-model-version:56e60bcab451be700f45c44b1929362a:3`；生成：2026-10-07T23:01:45.959751+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

D：异常 G&A／R&D 复利到营业巨亏；CFO／capex 固定收入比带来正 FCF；模型与收益桥互相矛盾。

**Model spec** `company-model-spec:42bbce0b4719679f3db70e9e9e76de40`：Meta is an advertising business run through Family of Apps. In Q2 2026, advertising was 59.363bn of 60.801bn revenue, and Reality Labs revenue was 431m against a 4.619bn operating loss. The model has to get two things right. First, ad revenue growth, which comes from impression volume times price per ad; Meta does not file either figure here, so both are analytical inferences. Second, the cost of AI infrastructure. H1 2026 capex was 49.1bn against 64.1bn of operating cash flow, and depreciation is building up inside cost of revenue and R&D. R&D (21.7bn in Q2) is the largest cost line and follows headcount and compute, not revenue. Below operating income, the tax line has swung a lot (a 5.0bn benefit in Q1 2026), and non-operating items are not modelled. Pretax income therefore stays a filed tie and is not forecast, and EPS is not forecast because no diluted numerator is filed. Two observations would change this structure: a material disclosed AI or other revenue stream outside advertising, or Reality Labs revenue that scales.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Ad impression volume across Family of Apps | {'basis_concept': None, 'because': 'Advertising is about 98% of Q2 2026 revenue. Volume comes from users and engagement across the apps. Meta does not file this in these statements, so it is an inference that has to be estimated (penetration slot).', 'kind': 'volume', 'label': 'Ad impression volume across Family of Apps', 'ref': 'ad-impressions', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Average price per ad | {'basis_concept': None, 'because': 'The other half of ad revenue. AI-driven ad ranking and conversion lift price. This is an inference and is not filed here (adoption slot). The UUP proxy shows only the direction of currency translation, not its size: about 72% of Q2 revenue was earned outside US & Canada.', 'kind': 'price', 'label': 'Average price per ad', 'ref': 'ad-price', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Geographic revenue mix | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': 'Meta files geographic revenue. In Q2 2026, US&Canada was 23.9bn, Europe 14.0bn and APAC 16.1bn. These regions earn very different revenue per user, so a shift toward APAC or Rest of World pulls the blended price down.', 'kind': 'mix', 'label': 'Geographic revenue mix', 'ref': 'geo-mix', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Reality Labs hardware revenue | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': "Reality Labs revenue was 431m in Q2 against a 4.6bn segment loss, so it is immaterial to revenue but material to margin. The TAM slot is not covered: no third-party TAM is supplied. SMH is far from Meta's realised revenue.", 'kind': 'segment', 'label': 'Reality Labs hardware revenue', 'ref': 'reality-labs', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| ad-impressions | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Cost of revenue was 11.3bn in Q2. It scales with serving load, but data-centre depreciation from capex of about 49bn in H1 makes part of it fixed and rising (unit_cost_curve slot).', 'behaviour': 'semi_variable', 'cost_driver_slot': 'unit_cost_curve', 'driver_ref': 'ad-impressions', 'label': 'Cost of revenue (infrastructure depreciation, data centre)', 'ref': 'cost-of-reve… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:ResearchAndDevelopmentExpense', 'because': 'R&D was 21.7bn in Q2, up from 17.7bn in Q1. It follows engineer headcount, share-based compensation (13.7bn in H1) and AI compute, not revenue.', 'behaviour': 'variable_with_headcount', 'cost_driver_slot': None, 'cost_driver_unbound_reason': "This is an investment choice driven by Meta's own decisions on AI and Reality Labs, so it does not fit the unit-cost… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| ad-price | {'basis_concept': 'us-gaap:SellingAndMarketingExpense', 'because': 'Marketing and sales was 3.4bn in Q2. It is small and moves partly with advertiser sales effort and brand spend.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Sales effort is company-specific and does not scale with unit adoption cost.', 'driver_ref': 'ad-price', 'label': 'Marketing and sales', 'ref': 'sales-marketing'… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:GeneralAndAdministrativeExpense', 'because': 'G&A jumped to 5.6bn in Q2 from 2.6bn in Q1. That points to lumpy legal or regulatory charges on top of a fixed base. This reading is an inference.', 'behaviour': 'one_off', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Legal and regulatory accruals are company-specific events.', 'driver_ref': None, 'label': 'General and administrative (incl. le… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Family daily active people | {'because': 'This is the volume base for impressions. Meta reports it in earnings materials, not in these statements.', 'disclosed': True, 'label': 'Family daily active people', 'periodicity': 'quarterly', 'ref': 'dap', 'unit': 'people'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Ad impression growth | {'because': 'This is the volume driver. It is disclosed in earnings releases, not in GAAP statements.', 'disclosed': True, 'label': 'Ad impression growth', 'periodicity': 'quarterly', 'ref': 'impression-growth', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Average price per ad growth | {'because': 'This is the price driver and the market watches it as evidence of AI monetisation.', 'disclosed': True, 'label': 'Average price per ad growth', 'periodicity': 'quarterly', 'ref': 'price-per-ad', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Employees | {'because': 'It drives R&D and share-based compensation.', 'disclosed': True, 'label': 'Employees', 'periodicity': 'quarterly', 'ref': 'headcount', 'unit': 'headcount'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Capex guidance | {'because': 'It sets the future depreciation run-rate and free cash flow.', 'disclosed': True, 'label': 'Capex guidance', 'periodicity': 'annual', 'ref': 'capex-guide', 'unit': 'USD'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'Revenue and the operating cost lines decide the earnings path.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'PP&E rose from 194.8bn to 225.7bn in one quarter and long-term debt to 83.7bn. Depreciation and leverage come from this, but working capital is minor.', 'importance': 'supporting', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'H1 capex of 49.1bn against 64.1bn of operating cash flow decides free cash flow, buybacks (zero in 2026) and debt issuance (24.9bn).', 'importance': 'required', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': 'Twelve quarters of history cover the 2024 to 2026 AI capex ramp and the change in depreciation life estimate. Eight forecast quarters cover the depreciation catch-up from current capex.', 'forecast_quarters': 8, 'historical_quarters': 12}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfRevenue | share_of_line | 18.2388% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line cost-of-revenue is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:GeneralAndAdministrativeExpense | quarterly_growth | 30.6076% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line g-and-a is carried forward unchanged | D／P0：一次性法律与裁员抬高基期，不应将季度 +30.6076% 永久复利；spec 已标 one-off 却未执行正常化。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | share_of_line | 57.1602% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (57.16%), carried forward against revenue by the cash-flow companion | C／P1：固定 CFO／收入比没有 NI、D&A、SBC、营运资金与一次性现金桥；利润崩塌也保持现金率，不足为现金预测。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | share_of_line | 38.9288% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (38.93%), carried forward against revenue by the cash-flow companion | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:ResearchAndDevelopmentExpense | quarterly_growth | 13.9525% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line rnd is carried forward unchanged | D／P0：季度 +13.95% 对应约 68.6% 年化；需人员／算力预算与规模约束，不能连续八季机械延长。 |
| concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | quarterly_growth | 6.6793% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line revenue is carried forward unchanged | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 29.52%，须用客户需求／指引校验。 首季约 648.6 亿已高于官方 640 亿上沿。 |
| concept:us-gaap:SellingAndMarketingExpense | share_of_line | 5.5132% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line sales-marketing is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 8/8 | 64862068165.83854100 | 101988518420.97406014 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost-of-revenue[k] = revenue[k] * share[k] | 8/8 | 11830044756.11549371 | 18601453386.36434971 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:rnd | rnd[k] = rnd[k-1] * (1 + growth[k]) | 8/8 | 24677564050.13927200 | 61570205474.45870365 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:sales-marketing | sales-marketing[k] = revenue[k] * share[k] | 8/8 | 3575967386.42742134 | 5622818173.65082415 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:g-and-a | g-and-a[k] = g-and-a[k-1] * (1 + growth[k]) | 8/8 | 7325779328.28809900 | 47493325424.58127848 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"revenue"},{"coefficient":"-1","line_ref":"cost-of-revenue"},{"coefficient":"-1","line_ref":"rnd"},{"coefficient":"-1","line_ref":"sales-marketing"},{"coefficient":"-1","line_ref":"g-and-a"}]} | 8/8 | 17452712644.86825495 | -31299284038.08109584 | D：费用异常复利导致末季巨亏；现金与下游回报未反映，需重做基期及预算。 |
| result:nonoperating_income_expense | nonoperating[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:pretax_income | pretax-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:income_tax_expense | income-tax[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"pretax-income"},{"coefficient":"-1","line_ref":"income-tax"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:diluted_weighted_average_shares | diluted-shares[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_cash_flow | operating_cash_flow[k] = revenue[k] * share[k] | 8/8 | 37075258129.92083618 | 58296794315.56120244 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 8/8 | 25250023135.76031920 | 39702903754.58335501 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 8/8 | 11825234994.16051698 | 18593890560.97784743 | C：CFO−PP&E 算术可成立，尚非公司定义／正常化 FCF；缺租赁、资本化及利润现金桥。 |


独立复算：{'pass': 72}；未计算项不判算术失败，亦不判研究完成。

### GOOGL

环境：hyperscalers；版本：`forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3`；生成：2026-10-08T11:37:11.075592+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

C／D：十三行八行可算，主费用能预测但 OP 仍不可用；证券收益不可预测应隔离，不阻断 core OP；TTM margin 不能补 NI。

**Model spec** `company-model-spec:718fa0df3922f58d18994123ea73e07e`：Alphabet has two engines. The first is a mature, cash-rich advertising franchise (Search & other, YouTube ads, Network). The second is a fast-growing Google Cloud segment that depends on AI capacity. Cloud revenue was 12.26bn in Q1 2025 against Search & other at 50.70bn, so the mix is shifting. The model has to get three things right: advertising growth in Search, Cloud growth, and the cost of revenue and depreciation that follow capex. Capex was 80.6bn of property and equipment purchases in H1 2026, against 84.9bn of operating cash flow. Below the operating line, the numbers are dominated by securities gains: other income was 98.0bn in Q2 2026, against 40.8bn of operating income. Net income therefore cannot be forecast from operations, and that gap is left unavailable. The main open question on drivers is whether AI answers reduce Search monetisation or extend it. If Search & other growth fell below Cloud's absolute dollar additions for several quarters, the model would need a segment-led revenue build in place of a consolidated anchor. The TAM slot rests only on proxies. SMH and UUP measure sentiment and currency direction, not Alphabet's own revenue.

**revenue_drivers**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Google Search & other advertising | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': 'This is the largest product line: 50.70bn of 90.23bn in Q1 2025. Query volume times monetisation sets the consolidated trend. Penetration slot: search is near saturation, so growth comes from price and ad load, not new users (analytical inference).', 'kind': 'segment', 'label': 'Google Search & other advertising', 'ref': 'search-ads', … | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| YouTube ads | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': '8.93bn in Q1 2025. It is driven by watch-time and the shift of brand budgets to connected TV, and it is more cyclical than Search.', 'kind': 'segment', 'label': 'YouTube ads', 'ref': 'youtube-ads', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Google Cloud segment revenue | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': "This is the adoption slot. It grew from 11.35bn in Q3 2024 to 12.26bn in Q1 2025 and is capacity-constrained. TAM rests only on the SMH proxy, which tracks chip-equity expectations, not Alphabet's cloud revenue or backlog. The Wiz goodwill of 22.7bn adds security revenue.", 'kind': 'segment', 'label': 'Google Cloud segment revenue', 'r… | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Subscriptions, platforms and devices | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': '10.38bn in Q1 2025. Subscriber counts (YouTube, Google One/AI tiers) times price, plus seasonal device sales, make this a separate driver from ads.', 'kind': 'segment', 'label': 'Subscriptions, platforms and devices', 'ref': 'subs-devices', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| Google Network | {'basis_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'because': 'It fell from 7.55bn in Q3 2024 to 7.26bn in Q1 2025. A structurally declining third-party inventory line has to be modelled apart from owned-and-operated growth.', 'kind': 'segment', 'label': 'Google Network', 'ref': 'network', 'unit': 'USD'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |
| USD translation | {'basis_concept': None, 'because': "Revenue outside the US was more than half of Q1 2025 (EMEA 25.9bn, APAC 14.9bn). UUP shows the direction of translation only; Alphabet's own currency basket and hedges (FX hedge reclass of 97m in Q2 2026) set the size.", 'kind': 'external', 'label': 'USD translation', 'ref': 'fx', 'unit': 'percent'} | B／C：业务维度方向合适；实算以集团收入为锚，未实现该项量价／续约／分部数学。 |


**expense_lines**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| search-ads | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Accrued revenue share of 10.6bn on the balance sheet shows that partner payments scale with ad revenue. This is the variable part of a cost of revenue that totalled 45.9bn in Q2 2026.', 'behaviour': 'variable_with_revenue', 'cost_driver_slot': 'unit_cost_curve', 'driver_ref': 'search-ads', 'label': 'Revenue share / TAC and content costs', 'ref': 'tac-content'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| cloud | {'basis_concept': 'us-gaap:CostOfRevenue', 'because': 'Depreciation was 13.6bn in H1 2026 and is rising behind 80.6bn of half-year capex. It follows installed capacity, not current revenue, so cost of revenue needs a separate capex-lagged component.', 'behaviour': 'semi_variable', 'cost_driver_slot': 'unit_cost_curve', 'driver_ref': 'cloud', 'label': 'Infrastructure depreciation and data-center ops', 'ref': 'infra-depreciation… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:ResearchAndDevelopmentExpense', 'because': '18.2bn in Q2 2026. Engineering headcount, stock-based compensation (14.7bn in H1) and AI compute for training drive it, not current-period revenue.', 'behaviour': 'variable_with_headcount', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'This is a company-specific discretionary investment line; the unit-cost slot covers delivery costs, not research… | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| cloud | {'basis_concept': 'us-gaap:SellingAndMarketingExpense', 'because': '8.4bn in Q2 2026. Enterprise sales hiring for Cloud plus brand spend for devices and AI products.', 'behaviour': 'semi_variable', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Go-to-market spend is a management choice, not a unit-cost curve.', 'driver_ref': 'cloud', 'label': 'Sales and marketing', 'ref': 'sales-marketing'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |
| None | {'basis_concept': 'us-gaap:GeneralAndAdministrativeExpense', 'because': '6.46bn in Q2 2026 includes lumpy fines and settlements. A 3.5bn antitrust charge was tagged on 2025-09-05.', 'behaviour': 'one_off', 'cost_driver_slot': None, 'cost_driver_unbound_reason': 'Regulatory charges are event-driven and specific to Alphabet.', 'driver_ref': None, 'label': 'General and administrative incl. legal/antitrust', 'ref': 'g-and-a'} | B／C：应按本项实际成本属性／一次性拆分；实际外推见下表，不把描述当预算。 |


**operating_metrics**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| Google Cloud backlog / RPO | {'because': 'This is the contract-book signal for Cloud adoption. It is disclosed in the filing narrative but is not in this ledger.', 'disclosed': True, 'label': 'Google Cloud backlog / RPO', 'periodicity': 'quarterly', 'ref': 'cloud-backlog', 'unit': 'USD'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Full-year capex guidance | {'because': 'It drives the depreciation and free-cash-flow lines. Management gives it on calls.', 'disclosed': True, 'label': 'Full-year capex guidance', 'periodicity': 'annual', 'ref': 'capex-guide', 'unit': 'USD'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Paid clicks and cost-per-click growth | {'because': 'Search volume against price split. It is no longer reported in this context, so it has to be estimated.', 'disclosed': False, 'label': 'Paid clicks and cost-per-click growth', 'periodicity': 'quarterly', 'ref': 'paid-clicks', 'unit': 'percent'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Paid subscriptions count | {'because': 'The volume driver for the subscriptions line. It is disclosed only occasionally.', 'disclosed': True, 'label': 'Paid subscriptions count', 'periodicity': 'annual', 'ref': 'subscriptions', 'unit': 'count'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| Employee headcount | {'because': 'Drives R&D and S&M behaviour.', 'disclosed': True, 'label': 'Employee headcount', 'periodicity': 'quarterly', 'ref': 'headcount', 'unit': 'headcount'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |
| AI Overviews / Gemini usage | {'because': 'Signals whether Search monetisation is being disrupted or extended. Management gives it only as anecdotes, so it must be estimated.', 'disclosed': False, 'label': 'AI Overviews / Gemini usage', 'periodicity': 'quarterly', 'ref': 'ai-query-share', 'unit': 'users'} | B／C：指标适合作监测，尚需数值基准、单位、频率、source 与模型驱动绑定。 |


**forecast_statements**

| 每条 spec | 内容／假设定位 | 评估 |
|---|---|---|
| income | {'because': 'Operating income, Search and Cloud margins, and depreciation drag are the core of how the market values Alphabet.', 'importance': 'required', 'statement': 'income'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| balance | {'because': 'Net PP&E (321bn) and debt (98bn long-term, rising with 56bn of H1 issuance) matter for depreciation and interest. Working capital is a minor factor.', 'importance': 'supporting', 'statement': 'balance'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |
| cash | {'because': 'Capex (80.6bn in H1 2026) now nearly equals operating cash flow (84.9bn). Free cash flow, and with it buyback and dividend capacity, depends on the capex path.', 'importance': 'required', 'statement': 'cash'} | C：required 是计划状态；无完整 BS、利润链或现金桥，不能称已执行三表预测。 |


**期间**：{'because': 'The filings run from Q2 2024 to Q2 2026, which covers the AI capex step-up. Eight quarters forward captures the lag between capex and depreciation.', 'forecast_quarters': 8, 'historical_quarters': 9}；评估：历史窗口较短；四／八／十二季长度不等于季节性、周期和长期折旧已经覆盖。

**逐类假设（相同值按期间合并）**

| driver | 公式口径 | 值 | 适用期间 | 理由 | 合理性 |
|---|---|---|---|---|---|
| concept:us-gaap:CostOfRevenue | share_of_line | 39.1333% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line cost-of-revenue is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |
| concept:us-gaap:GeneralAndAdministrativeExpense | quarterly_growth | 11.2708% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line g-and-a is carried forward unchanged | C／P1：季度 +11.27% 可能混一次性费用；需基期正常化和法律／人力驱动，不应永久加速。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | share_of_line | 41.9049% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (41.90%), carried forward against revenue by the cash-flow companion | C／P1：固定 CFO／收入比没有 NI、D&A、SBC、营运资金与一次性现金桥；利润崩塌也保持现金率，不足为现金预测。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | share_of_line | 29.4583% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the average share of revenue over the 4 quarters filed between 2025-09-30 and 2026-06-30 (29.46%), carried forward against revenue by the cash-flow companion | C：PP&E／收入比不是 capex 计划，需容量／服务器／租赁现金和资本化软件／合同范围；与公司定义 FCF 对齐。 |
| concept:us-gaap:ResearchAndDevelopmentExpense | quarterly_growth | 7.7457% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line rnd is carried forward unchanged | C：季度 +7.75% 要核工资、人员、算力和折旧；当前是历史变化外推而非执行预算。 |
| concept:us-gaap:Revenues | quarterly_growth | 5.7276% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line revenue is carried forward unchanged | C：历史集团收入 QoQ 均值延长，无季节性／分部／量价／订单转换；年化隐含增速 24.95%，须用客户需求／指引校验。 |
| concept:us-gaap:SellingAndMarketingExpense | share_of_line | 7.0481% | 2026-09-30,2026-12-31,2027-03-31,2027-06-30,2027-09-30,2027-12-31,2028-03-31,2028-06-30 | the trailing filed history through 2026-06-30 for structure line sales-marketing is carried forward unchanged | B／C：历史费用率可作基准情景，需 mix／工资／利用率／AI 让利敏感性；当前并非自下而上的业务 driver。 |


**每条结果行**

| 结果行 | 公式 | 可算／总季数 | 首季值 | 末季值 | 评估 |
|---|---|---|---|---|---|
| result:revenue | revenue[k] = revenue[k-1] * (1 + growth[k]) | 8/8 | 126657417664.78526400 | 187046167349.85256788 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:cost_of_revenue | cost-of-revenue[k] = revenue[k] * share[k] | 8/8 | 49565214013.47831384 | 73197318293.92844617 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:rnd | rnd[k] = rnd[k-1] * (1 + growth[k]) | 8/8 | 19630188863.60680200 | 33092080575.81220413 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:sales-marketing | sales-marketing[k] = revenue[k] * share[k] | 8/8 | 8926917573.80891659 | 13183165654.36533586 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_expense:g-and-a | g-and-a[k] = g-and-a[k-1] * (1 + growth[k]) | 8/8 | 7189205912.05043500 | 15182706440.20306028 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:operating_income | operating-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:nonoperating_income_expense | other-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:pretax_income | pretax-income[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:income_tax_expense | income-tax[k] unavailable by company structure | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:net_income | {"operator":"sum","terms":[{"coefficient":"1","line_ref":"pretax-income"},{"coefficient":"-1","line_ref":"income-tax"}]} | 0/8 | None | None | C：明确 unavailable 是诚实边界；该链尚未完成，不能在估值／收益桥用未经正常化的 TTM margin 代替。 |
| result:operating_cash_flow | operating_cash_flow[k] = revenue[k] * share[k] | 8/8 | 53075655154.82554228 | 78381496001.81487968 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:capital_expenditure | capital_expenditure[k] = revenue[k] * share[k] | 8/8 | 37311173927.44512802 | 55100697701.91370190 | B／C：算术复算通过；数值可靠性受对应历史 carry-forward 假设、映射与季节性限制。 |
| result:free_cash_flow | free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k] | 8/8 | 15764481227.38041426 | 23280798299.90117778 | C：CFO−PP&E 算术可成立，尚非公司定义／正常化 FCF；缺租赁、资本化及利润现金桥。 |


独立复算：{'pass': 64}；未计算项不判算术失败，亦不判研究完成。

### AMZN、MSFT

未登记成功的公司 model spec／预测模型；AMZN 自有收益腿不可用，MSFT 模型、估值和收益腿均缺。不能用其他公司或统一 AI 模型填空。

## 六份当前年度投影：逐年／逐行

年度汇总继承季度模型的假设与 unavailable，不是独立年度经营预测。

### CTSH

环境：legacy；版本：`forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6`；生成：2026-09-14T11:37:33.509381+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

绑定模型：`forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6`。评估 C：年度同比／年化需区分全年历史加未来、纯未来、部分年度。缺失季不得当零。

| 年度／类型 | 结果行 | 状态 | 值 | 原因 | 评估 |
|---|---|---|---|---|---|
| FY2024A historical | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:diluted_eps | unavailable | None | the structure does not define diluted EPS authority | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:diluted_weighted_average_shares | unavailable | None | this line requires one directly filed annual value with an exact calendar and definition | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:interest_expense:interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:interest_expense:legacy-interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:da | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:restructuring | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:sga | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:other_nonoperating_income_expense:fx | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:other_nonoperating_income_expense:other-net | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:capital_expenditure | computed | 288000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:company_presented_component | computed | 9000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:cost_of_revenue | computed | 13991000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:diluted_eps | unavailable | None | the structure does not define diluted EPS authority | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:diluted_weighted_average_shares | computed | 489000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:free_cash_flow | computed | 2595000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:income_tax_expense | computed | 1258000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_expense:interest-expense | computed | 37000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_expense:legacy-interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_income | computed | 105000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:net_income | computed | 2230000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:nonoperating_income_expense | computed | 90000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_cash_flow | computed | 2883000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:da | computed | 550000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:restructuring | computed | 0 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:sga | computed | 3240000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_income | computed | 3389000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_nonoperating_income_expense:fx | computed | 18000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_nonoperating_income_expense:other-net | computed | 4000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_operating_income_expense | computed | 62000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:pretax_income | computed | 3479000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:revenue | computed | 21108000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:capital_expenditure | computed | 339178628.11284908 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:cost_of_revenue | computed | 14709333643.00257430 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:diluted_eps | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:diluted_weighted_average_shares | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:interest_expense:interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:interest_expense:legacy-interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:da | computed | 573181063.28023680 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:restructuring | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:sga | computed | 2919634140.60253526 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:other_nonoperating_income_expense:fx | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:other_nonoperating_income_expense:other-net | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:revenue | computed | 22040981610.50654259 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:diluted_eps | unavailable | None | annual diluted EPS authority is unavailable | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:diluted_weighted_average_shares | unavailable | None | this line requires one directly filed annual value with an exact calendar and definition | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:interest_expense:interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:interest_expense:legacy-interest-expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:operating_expense:da | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:operating_expense:restructuring | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:operating_expense:sga | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:other_nonoperating_income_expense:fx | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:other_nonoperating_income_expense:other-net | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E (partial) partial | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |


### EPAM

环境：legacy；版本：`forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9`；生成：2026-09-29T07:05:10.162553+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

绑定模型：`forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9`。评估 C：年度同比／年化需区分全年历史加未来、纯未来、部分年度。缺失季不得当零。

| 年度／类型 | 结果行 | 状态 | 值 | 原因 | 评估 |
|---|---|---|---|---|---|
| FY2024A historical | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:diluted_eps | unavailable | None | the structure does not define diluted EPS authority | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:diluted_weighted_average_shares | unavailable | None | this line requires one directly filed annual value with an exact calendar and definition | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:d-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:sga | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:capital_expenditure | computed | 42243000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:cost_of_revenue | computed | 3883535000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:diluted_eps | unavailable | None | the structure does not define diluted EPS authority | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:diluted_weighted_average_shares | computed | 56233000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:free_cash_flow | computed | 612691000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:income_tax_expense | computed | 127946000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:net_income | computed | 377678000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:nonoperating_income_expense | computed | 11546000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_cash_flow | computed | 654934000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:d-and-a | computed | 124811000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:sga | computed | 928707000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_income | computed | 520003000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_nonoperating_income_expense | computed | -25925000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:pretax_income | computed | 505624000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:revenue | computed | 5457056000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:capital_expenditure | computed | 61498972.60540582 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:cost_of_revenue | computed | 4005867503.42781328 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:diluted_eps | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:diluted_weighted_average_shares | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:free_cash_flow | computed | 173327736.10261849 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_cash_flow | computed | 234826708.70802431 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:d-and-a | computed | 128486107.93334884 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:sga | computed | 972796872.02229099 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_income | computed | 553083339.08152732 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:revenue | computed | 5660233822.46498043 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:capital_expenditure | computed | 58794698.07682293 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:cost_of_revenue | computed | 4154218188.90824187 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:diluted_eps | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:diluted_weighted_average_shares | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:free_cash_flow | computed | 507086634.10142699 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_cash_flow | computed | 565881332.17824992 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:d-and-a | computed | 132309012.73872443 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:sga | computed | 1008970506.98975973 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_income | computed | 589367123.61832094 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:revenue | computed | 5884864832.25504695 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:diluted_eps | unavailable | None | annual diluted EPS authority is unavailable | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:diluted_weighted_average_shares | unavailable | None | this line requires one directly filed annual value with an exact calendar and definition | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:d-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:sga | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:other_operating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |


### ACN

环境：legacy；版本：`forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8`；生成：2026-10-07T23:43:02.732095+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

绑定模型：`forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8`。评估 C：年度同比／年化需区分全年历史加未来、纯未来、部分年度。缺失季不得当零。

| 年度／类型 | 结果行 | 状态 | 值 | 原因 | 评估 |
|---|---|---|---|---|---|
| FY2024A historical | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:interest_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:noncontrolling_interest:nci-canada | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:noncontrolling_interest:nci-other | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:business-optimization | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:g-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:sales-marketing | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:capital_expenditure | computed | 600039000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:company_presented_component | computed | 59447313000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:cost_of_revenue | computed | 47437576000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:free_cash_flow | computed | 10874360000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:income_tax_expense | computed | 2437993000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_expense | computed | 228555000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_income | computed | 336324000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:net_income | computed | 7832400000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:noncontrolling_interest:nci-canada | computed | 7240000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:noncontrolling_interest:nci-other | computed | 146727000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_cash_flow | computed | 11474399000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:business-optimization | computed | 615324000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:g-and-a | computed | 4350968000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:sales-marketing | computed | 7043445000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_income | computed | 10225664000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_nonoperating_income_expense | computed | -63040000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:parent_net_income | computed | 7678433000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:pretax_income | computed | 10270393000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:revenue | computed | 69672977000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:capital_expenditure | computed | 647874046.99433500 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:cost_of_revenue | computed | 50626752010.53974812 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:free_cash_flow | computed | 12062142730.18241890 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:interest_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:noncontrolling_interest:nci-canada | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:noncontrolling_interest:nci-other | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_cash_flow | computed | 12710016777.17675390 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:business-optimization | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:g-and-a | computed | 4672743898.21713938 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:sales-marketing | computed | 7312910024.18558146 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:revenue | computed | 74492992262.99874490 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:capital_expenditure | computed | 644314915.60063101 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:cost_of_revenue | computed | 53546300639.80344337 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:free_cash_flow | computed | 13628650770.63655085 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:interest_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:noncontrolling_interest:nci-canada | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:noncontrolling_interest:nci-other | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_cash_flow | computed | 14272965686.23718186 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:business-optimization | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:g-and-a | computed | 4862450858.11034627 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:sales-marketing | computed | 7787557121.60272688 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:revenue | computed | 78738802838.79618096 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:company_presented_component | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:interest_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:noncontrolling_interest:nci-canada | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:noncontrolling_interest:nci-other | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:business-optimization | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:g-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:sales-marketing | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:other_nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |


### DXC

环境：legacy；版本：`forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9`；生成：2026-10-07T23:48:04.501756+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

绑定模型：`forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9`。评估 C：年度同比／年化需区分全年历史加未来、纯未来、部分年度。缺失季不得当零。

| 年度／类型 | 结果行 | 状态 | 值 | 原因 | 评估 |
|---|---|---|---|---|---|
| FY2025A historical | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:company_presented_subtotal | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:noncontrolling_interest | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:d-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:restructuring | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:sga | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_nonoperating_income_expense:gain-on-disposition | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:other_nonoperating_income_expense:other-income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:capital_expenditure | computed | 212000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:company_presented_subtotal | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:cost_of_revenue | computed | 9613000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:free_cash_flow | computed | 1036000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:income_tax_expense | computed | 290000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:interest_expense | computed | 216000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:interest_income | computed | 181000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:noncontrolling_interest | computed | 10000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:operating_cash_flow | computed | 1248000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:operating_expense:d-and-a | computed | 1160000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:operating_expense:restructuring | computed | 115000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:operating_expense:sga | computed | 1402000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:other_nonoperating_income_expense:gain-on-disposition | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:other_nonoperating_income_expense:other-income | computed | -1000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A historical | result:revenue | computed | 12644000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:capital_expenditure | computed | 219446267.47947727 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:company_presented_subtotal | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:cost_of_revenue | computed | 9144478091.96563813 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:free_cash_flow | computed | 1239471071.45921321 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:interest_expense | computed | 221561504.52636249 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:noncontrolling_interest | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:operating_cash_flow | computed | 1458917338.93869048 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:operating_expense:d-and-a | computed | 1018005257.01787333 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:operating_expense:restructuring | computed | 96376931.74330649 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:operating_expense:sga | computed | 1267059901.08296140 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:other_nonoperating_income_expense:gain-on-disposition | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:other_nonoperating_income_expense:other-income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027A/E mixed | result:revenue | computed | 11769326346.87133786 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:capital_expenditure | computed | 204575775.33379202 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:company_presented_subtotal | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:cost_of_revenue | computed | 8614795257.64879060 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:free_cash_flow | computed | 1122637845.88060132 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:interest_expense | computed | 225771581.73795768 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:noncontrolling_interest | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:operating_cash_flow | computed | 1327213621.21439334 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:operating_expense:d-and-a | computed | 894266680.83030666 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:operating_expense:restructuring | computed | 78318749.62823221 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:operating_expense:sga | computed | 1197341081.01638187 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:other_nonoperating_income_expense:gain-on-disposition | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:other_nonoperating_income_expense:other-income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E forecast | result:revenue | computed | 11182536936.05607807 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:company_presented_subtotal | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:interest_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:interest_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:noncontrolling_interest | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:d-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:restructuring | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:sga | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:other_nonoperating_income_expense:gain-on-disposition | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:other_nonoperating_income_expense:other-income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:parent_net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |


### META

环境：hyperscalers；版本：`forecast-model-version:56e60bcab451be700f45c44b1929362a:3`；生成：2026-10-07T23:01:45.959751+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

绑定模型：`forecast-model-version:56e60bcab451be700f45c44b1929362a:3`。评估 C：年度同比／年化需区分全年历史加未来、纯未来、部分年度。缺失季不得当零。

| 年度／类型 | 结果行 | 状态 | 值 | 原因 | 评估 |
|---|---|---|---|---|---|
| FY2023A historical | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:diluted_weighted_average_shares | unavailable | None | this line requires one directly filed annual value with an exact calendar and definition | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:operating_expense:g-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:operating_expense:rnd | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:operating_expense:sales-marketing | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2023A historical | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:capital_expenditure | computed | 37256000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:cost_of_revenue | computed | 30162000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:diluted_weighted_average_shares | computed | 2614000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:free_cash_flow | computed | 54072000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:income_tax_expense | computed | 8303000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:net_income | computed | 62360000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:nonoperating_income_expense | computed | 1284000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_cash_flow | computed | 91328000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:g-and-a | computed | 9740000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:rnd | computed | 43872000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:sales-marketing | computed | 11347000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_income | computed | 69379000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:pretax_income | computed | 70663000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:revenue | computed | 164500000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:capital_expenditure | computed | 69691000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:cost_of_revenue | computed | 36175000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:diluted_weighted_average_shares | computed | 2574000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:free_cash_flow | computed | 46109000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:income_tax_expense | computed | 25475000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:net_income | computed | 60458000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:nonoperating_income_expense | computed | 2657000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_cash_flow | computed | 115800000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:g-and-a | computed | 12152000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:rnd | computed | 57371000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:sales-marketing | computed | 11991000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_income | computed | 83276000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:pretax_income | computed | 85933000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:revenue | computed | 200965000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:capital_expenditure | computed | 101299565656.78277035 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:cost_of_revenue | computed | 45998251156.93265020 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:diluted_weighted_average_shares | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:free_cash_flow | computed | 39415310375.62297276 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_cash_flow | computed | 140714876032.40574311 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:g-and-a | computed | 25116802642.02909222 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:rnd | computed | 92153277360.47941745 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:sales-marketing | computed | 13729783596.31329318 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_income | computed | 74170339797.64161947 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:revenue | computed | 251168454553.39607252 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:capital_expenditure | computed | 126980219800.11529397 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:cost_of_revenue | computed | 59492289385.24351135 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:diluted_weighted_average_shares | unavailable | None | the structure does not define annual diluted EPS inputs | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:free_cash_flow | computed | 59468101501.25839884 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_cash_flow | computed | 186448321301.37369281 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:g-and-a | computed | 77976791691.05674990 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:rnd | computed | 157585341694.09825506 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:sales-marketing | computed | 17983235986.96080111 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_income | computed | 13148168008.23004596 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:revenue | computed | 326185826765.58936338 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:diluted_weighted_average_shares | unavailable | None | this line requires one directly filed annual value with an exact calendar and definition | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:g-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:rnd | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:sales-marketing | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |


### GOOGL

环境：hyperscalers；版本：`forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3`；生成：2026-10-08T11:37:11.075592+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

绑定模型：`forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3`。评估 C：年度同比／年化需区分全年历史加未来、纯未来、部分年度。缺失季不得当零。

| 年度／类型 | 结果行 | 状态 | 值 | 原因 | 评估 |
|---|---|---|---|---|---|
| FY2024A historical | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:g-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:rnd | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_expense:sales-marketing | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2024A historical | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:capital_expenditure | computed | 91447000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:cost_of_revenue | computed | 162536000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:free_cash_flow | computed | 73266000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:income_tax_expense | computed | 26656000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:net_income | computed | 132170000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:nonoperating_income_expense | computed | 29787000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_cash_flow | computed | 164713000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:g-and-a | computed | 21482000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:rnd | computed | 61087000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_expense:sales-marketing | computed | 28693000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:operating_income | computed | 129039000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:pretax_income | computed | 158826000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2025A historical | result:revenue | computed | 402837000000 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:capital_expenditure | computed | 157357377036.82994833 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:cost_of_revenue | computed | 189183317764.43485913 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:free_cash_flow | computed | 36692886508.73361271 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_cash_flow | computed | 194050263545.56356104 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:g-and-a | computed | 25940692314.44339692 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:rnd | computed | 76031873029.63526177 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_expense:sales-marketing | computed | 34374131934.93138714 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2026A/E mixed | result:revenue | computed | 490261246519.77089155 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:capital_expenditure | computed | 181718698308.49663974 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:cost_of_revenue | computed | 241400235474.39431402 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:free_cash_flow | computed | 76778635100.54559947 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_cash_flow | computed | 258497333409.04223921 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:g-and-a | computed | 42088738423.98149565 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:rnd | computed | 102304278791.18705141 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_expense:sales-marketing | computed | 43477266209.15993642 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2027E forecast | result:revenue | computed | 616866708989.56573902 | None | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:capital_expenditure | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:cost_of_revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:free_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:income_tax_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:net_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:nonoperating_income_expense | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_cash_flow | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:g-and-a | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:rnd | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_expense:sales-marketing | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:operating_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:pretax_income | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |
| FY2028E (partial) partial | result:revenue | unavailable | None | a fiscal-year amount needs four contiguous quarters with one calendar, definition, and unit | C：继承季度模型假设；partial 年度不够四连续季时不可用是合理纪律。 |


## 64 条当前 forecast line（包括旧 ACN 手工链）

每条 line 是派生展示，需正确期间／单位并绑定所展示的模型；绑定旧版本不自动表示数值错，表示不能当最新模型结果。

### legacy

| 公司 | line 版本 | 指标 | 期间末 | 值 | 模型绑定 | 评估 |
|---|---|---|---|---|---|---|
| ACN | forecast-line-version:acn:revenue:q1:1 | metric:revenue-usd | 2026-08-31 | 18933.40265600 | input-version:acn:scenario:base:1 | C：旧人工 quarterly-growth 链，不能与当前公司模型混成一个版本；本币 annual→USD QoQ 缺 FX／季节性。 |
| ACN | forecast-line-version:acn:revenue:q2:1 | metric:revenue-usd | 2026-11-30 | 19151.13678654 | input-version:acn:scenario:base:1 | C：旧人工 quarterly-growth 链，不能与当前公司模型混成一个版本；本币 annual→USD QoQ 缺 FX／季节性。 |
| ACN | forecast-line-version:acn:revenue:q3:1 | metric:revenue-usd | 2027-02-28 | 19371.37485959 | input-version:acn:scenario:base:1 | C：旧人工 quarterly-growth 链，不能与当前公司模型混成一个版本；本币 annual→USD QoQ 缺 FX／季节性。 |
| ACN | forecast-line-version:acn:revenue:q4:1 | metric:revenue-usd | 2027-05-31 | 19594.14567047 | input-version:acn:scenario:base:1 | C：旧人工 quarterly-growth 链，不能与当前公司模型混成一个版本；本币 annual→USD QoQ 缺 FX／季节性。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2026-09-30:2 | metric:revenue-usd | 2026-09-30 | 17394323840.28796000 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2026-12-31:2 | metric:revenue-usd | 2026-12-31 | 17629792673.39529685 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2027-03-31:2 | metric:revenue-usd | 2027-03-31 | 17868449073.42814479 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2027-06-30:2 | metric:revenue-usd | 2027-06-30 | 18110336190.82289291 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2027-09-30:2 | metric:revenue-usd | 2027-09-30 | 18355497760.14802982 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2027-12-31:2 | metric:revenue-usd | 2027-12-31 | 18603978108.01160274 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2028-03-31:2 | metric:revenue-usd | 2028-03-31 | 18855822161.07572067 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2028-06-30:2 | metric:revenue-usd | 2028-06-30 | 19111075454.17955079 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2028-09-30:2 | metric:revenue-usd | 2028-09-30 | 19369784138.57227673 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2028-12-31:2 | metric:revenue-usd | 2028-12-31 | 19631994990.25750727 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2029-03-31:2 | metric:revenue-usd | 2029-03-31 | 19897755418.45064420 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| IBM | forecast-line-version:0000051143:revenue-usd:2029-06-30:2 | metric:revenue-usd | 2029-06-30 | 20167113474.15073849 | forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3 | C：绑定 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:3，不是当前 forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2026-09-30:3 | metric:revenue-usd | 2026-09-30 | 5542431030.57042000 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2026-12-31:3 | metric:revenue-usd | 2026-12-31 | 5604550579.93612259 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2027-03-31:3 | metric:revenue-usd | 2027-03-31 | 5667366365.01357581 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2027-06-30:3 | metric:revenue-usd | 2027-06-30 | 5730886189.21042292 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:4，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2027-09-30:2 | metric:revenue-usd | 2027-09-30 | 5795117943.39487527 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2027-12-31:2 | metric:revenue-usd | 2027-12-31 | 5860069606.87596997 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2028-03-31:2 | metric:revenue-usd | 2028-03-31 | 5925749248.39481449 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| CTSH | forecast-line-version:0001058290:revenue-usd:2028-06-30:2 | metric:revenue-usd | 2028-06-30 | 5992165027.12694096 | forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2 | C：绑定 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:2，不是当前 forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2026-09-30:3 | metric:revenue-usd | 2026-09-30 | 1430638822.46498043 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2026-12-31:3 | metric:revenue-usd | 2026-12-31 | 1446688705.87452618 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2027-03-31:3 | metric:revenue-usd | 2027-03-31 | 1462918647.83792564 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2027-06-30:3 | metric:revenue-usd | 2027-06-30 | 1479330668.37501251 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2027-09-30:3 | metric:revenue-usd | 2027-09-30 | 1495926810.16758262 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2027-12-31:3 | metric:revenue-usd | 2027-12-31 | 1512709138.81363130 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2028-03-31:3 | metric:revenue-usd | 2028-03-31 | 1529679743.08444295 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| EPAM | forecast-line-version:0001352010:revenue-usd:2028-06-30:3 | metric:revenue-usd | 2028-06-30 | 1546840735.18456484 | forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4 | C：绑定 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:4，不是当前 forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2026-08-31:3 | metric:revenue-usd | 2026-08-31 | 18988657262.99874490 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2026-11-30:3 | metric:revenue-usd | 2026-11-30 | 19263079964.10514769 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2027-02-28:3 | metric:revenue-usd | 2027-02-28 | 19541468602.23592761 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2027-05-31:3 | metric:revenue-usd | 2027-05-31 | 19823880492.82606108 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:5，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2027-08-31:3 | metric:revenue-usd | 2027-08-31 | 20110373779.62904458 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2027-11-30:3 | metric:revenue-usd | 2027-11-30 | 20401007446.68769394 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2028-02-29:3 | metric:revenue-usd | 2028-02-29 | 20695841330.47794479 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| ACN | forecast-line-version:0001467373:revenue-usd:2028-05-31:3 | metric:revenue-usd | 2028-05-31 | 20994936132.22815436 | forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7 | C：绑定 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:7，不是当前 forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2026-09-30:3 | metric:revenue-usd | 2026-09-30 | 2960899384.75189600 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2026-12-31:3 | metric:revenue-usd | 2026-12-31 | 2923282816.48021216 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2027-03-31:3 | metric:revenue-usd | 2027-03-31 | 2886144145.63922970 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2027-06-30:3 | metric:revenue-usd | 2027-06-30 | 2849477300.80976384 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2027-09-30:3 | metric:revenue-usd | 2027-09-30 | 2813276287.70660988 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2027-12-31:3 | metric:revenue-usd | 2027-12-31 | 2777535188.19859925 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2028-03-31:3 | metric:revenue-usd | 2028-03-31 | 2742248159.34110510 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| DXC | forecast-line-version:001688568:revenue-usd:2028-06-30:3 | metric:revenue-usd | 2028-06-30 | 2707409432.42083940 | forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6 | C：绑定 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:6，不是当前 forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |


### hyperscalers

| 公司 | line 版本 | 指标 | 期间末 | 值 | 模型绑定 | 评估 |
|---|---|---|---|---|---|---|
| META | forecast-line-version:meta:revenue-usd:2026-09-30:1 | metric:revenue-usd | 2026-09-30 | 64862068165.83854100 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2026-12-31:1 | metric:revenue-usd | 2026-12-31 | 69194386387.55753152 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2027-03-31:1 | metric:revenue-usd | 2027-03-31 | 73816072212.02162872 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2027-06-30:1 | metric:revenue-usd | 2027-06-30 | 78746453307.52137329 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2027-09-30:1 | metric:revenue-usd | 2027-09-30 | 84006148291.15430916 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2027-12-31:1 | metric:revenue-usd | 2027-12-31 | 89617152954.89205221 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2028-03-31:1 | metric:revenue-usd | 2028-03-31 | 95602932250.92658210 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| META | forecast-line-version:meta:revenue-usd:2028-06-30:1 | metric:revenue-usd | 2028-06-30 | 101988518420.97406014 | forecast-model-version:56e60bcab451be700f45c44b1929362a:1 | C：绑定 forecast-model-version:56e60bcab451be700f45c44b1929362a:1，不是当前 forecast-model-version:56e60bcab451be700f45c44b1929362a:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2026-09-30:1 | metric:revenue-usd | 2026-09-30 | 126657417664.78526400 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2026-12-31:1 | metric:revenue-usd | 2026-12-31 | 133911828854.98562755 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2027-03-31:1 | metric:revenue-usd | 2027-03-31 | 141581742608.61095241 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2027-06-30:1 | metric:revenue-usd | 2027-06-30 | 149690957187.94418285 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2027-09-30:1 | metric:revenue-usd | 2027-09-30 | 158264633920.95676176 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2027-12-31:1 | metric:revenue-usd | 2027-12-31 | 167329375272.05384200 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2028-03-31:1 | metric:revenue-usd | 2028-03-31 | 176913307384.38774941 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |
| GOOGL | forecast-line-version:googl:revenue-usd:2028-06-30:1 | metric:revenue-usd | 2028-06-30 | 187046167349.85256788 | forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1 | C：绑定 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:1，不是当前 forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3；可能值未变化，仍需明确旧版本／更新绑定。 金额 unit=one 与 USD 展示需统一。 |


## 八份当前估值：每个指标与输入口径

所有历史分位均需展示有效样本而非仅价格 bars 数，当前股数套历史不等于实际历史 market cap。

### ACN

环境：legacy；版本：`valuation-snapshot-version:5a753205c98a2abb85a83fc5797b1754`；生成：2026-10-09T01:11:44.247229+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': '2d7c5ac5a4c210f85d760804bc7245b216e7329a4b7f0372f2170465d8b64608', 'bar_date': '2026-10-08', 'close': '208.36', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:f25c84dffe0dc904d759fcab122bc244', 'version_hash': '88e8b03c8a16a535fe257df94e3f89e3f8a58a170f32b3da0cf939a536401c53', 'version_ref': 'market-price-series-version:0c054089d32d4971f7d3e5f55ac7c5a6'}；**shares**：{'artifact_hash': '2d7c5ac5a4c210f85d760804bc7245b216e7329a4b7f0372f2170465d8b64608', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:f25c84dffe0dc904d759fcab122bc244', 'shares_outstanding': '611942109', 'version_hash': '88e8b03c8a16a535fe257df94e3f89e3f8a58a170f32b3da0cf939a536401c53', 'version_ref': 'market-price-series-version:0c054089d32d4971f7d3e5f55ac7c5a6'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 16.3682 | 60 | 250 | 4 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 |
| price_to_sales | available | 1.7442 | 59.6 | 250 | 4 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.098677 | 44.4 | 250 | 4 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### CTSH

环境：legacy；版本：`valuation-snapshot-version:1aa50a4af0b19d37bcd4f9543a11eca1`；生成：2026-10-09T14:17:02.592434+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': '1d910e92dbe90a6988ef53f5817ca56d95e84bc4076d8aed18018e14d463fadf', 'bar_date': '2026-10-08', 'close': '60.01', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:abce9fd9d96553cb61762346ee21d85b', 'version_hash': '8be583338e9bb5a318dfcbbfc3b8de376a78db04c19098290628bf62f78f9605', 'version_ref': 'market-price-series-version:9552ccd614bbafeba3824663f86d670f'}；**shares**：{'artifact_hash': 'ee51a7bca51fdd3e2fcf0c2f039a78d3684fb6a3cabc16b51b2f50500eca1f48', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:db75fdf814ae80dfe00189094f879940', 'shares_outstanding': '450444209', 'version_hash': '8be583338e9bb5a318dfcbbfc3b8de376a78db04c19098290628bf62f78f9605', 'version_ref': 'market-price-series-version:9552ccd614bbafeba3824663f86d670f'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 12.1762 | 64.85 | 165 | 3 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 |
| price_to_sales | available | 1.249 | 60.61 | 165 | 3 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.096111 | 38.18 | 165 | 3 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### EPAM

环境：legacy；版本：`valuation-snapshot-version:fca4abda78b70af7aed6a03d301ff6e2`；生成：2026-10-09T14:26:58.292499+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': '61a86232d7e7b37ac0f13e3911b0b5cd1587fca27a1172a2b7708eedca512238', 'bar_date': '2026-10-08', 'close': '113.86', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:4c01505949b1efe31f48ce793a7b61ac', 'version_hash': 'e58372147d91edbdd09272802130379de69ed01ac71f842a3ed76ec3340f9e68', 'version_ref': 'market-price-series-version:96d39c538a61d659513c23066c52dd1a'}；**shares**：{'artifact_hash': '72895106d42d9e5371e19b279b0d22c4ed926a2234571aaa39a658f516768f3a', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:84a08e571313b56d7723c414453aa8ab', 'shares_outstanding': '51586981', 'version_hash': 'e58372147d91edbdd09272802130379de69ed01ac71f842a3ed76ec3340f9e68', 'version_ref': 'market-price-series-version:96d39c538a61d659513c23066c52dd1a'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 14.6232 | 57.69 | 156 | 3 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 |
| price_to_sales | available | 1.0457 | 60.26 | 156 | 3 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.082169 | 12.82 | 156 | 3 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### IBM

环境：legacy；版本：`valuation-snapshot-version:b4f3012b11028e70c1cebd92d9b2a0b2`；生成：2026-10-09T14:52:12.000668+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': 'bb3df85561613eb8fefe20362a39539357c0448f402e4ab508c0f828208b3804', 'bar_date': '2026-10-08', 'close': '226.61', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:02877e37ee67a58f7e8e58473cea59cb', 'version_hash': 'e9b16658857a19804c633fd578940bed0d347a991b8544b2ca1b59067490d221', 'version_ref': 'market-price-series-version:b201a9eeef16809640d7444414948e63'}；**shares**：{'artifact_hash': 'bed43e1455a789c6806d92d61ce8dc01ef1fe5afa4928624d8ab4c2b8071793c', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:7bc6dd6b9b1248fdfd767f3937cc01d3', 'shares_outstanding': '942134390', 'version_hash': 'e9b16658857a19804c633fd578940bed0d347a991b8544b2ca1b59067490d221', 'version_ref': 'market-price-series-version:b201a9eeef16809640d7444414948e63'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 19.9065 | 21.52 | 158 | 3 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 |
| price_to_sales | available | 3.0899 | 20.89 | 158 | 3 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.064591 | 87.97 | 158 | 3 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### DXC

环境：legacy；版本：`valuation-snapshot-version:35c8839ba03166ea7d8679fcce0d90cb`；生成：2026-10-09T14:37:07.080327+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': '03bc2de47e67037bcfbc4d358da87c4b626b759b602af7ca864809e9233e0fc2', 'bar_date': '2026-10-08', 'close': '12.37', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:4c7926a70287dcfd6027be40f3225f3e', 'version_hash': '87297f261d65bcd4bc3de4e51508854025201aed9a72de7eafd6dec6fd20d4cd', 'version_ref': 'market-price-series-version:55f7f038d1964b057877e18701282521'}；**shares**：{'artifact_hash': 'ffdb6ededab283564a5ac732e09f72878936667c61fd0aa10fc6194bfa86ef2b', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:319fd7a9ca098a2065bdba180c7e1ac7', 'shares_outstanding': '159778689', 'version_hash': '87297f261d65bcd4bc3de4e51508854025201aed9a72de7eafd6dec6fd20d4cd', 'version_ref': 'market-price-series-version:55f7f038d1964b057877e18701282521'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 15.9392 | 46.23 | 106 | 2 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 |
| price_to_sales | available | 0.1583 | 100 | 106 | 2 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.633455 | 5.66 | 106 | 2 | D：63.35% 混定义与诉讼现金；公司定义约 47.1%、剔诉讼示例约 36.2%，不能据此直接认便宜。 |


### AMZN

环境：hyperscalers；版本：`valuation-snapshot-version:dd03aaee464a7b8da813988d9724acea`；生成：2026-10-09T14:06:04.274101+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': '3d85de1b2c169d8ad8666c8018375258b4b6ec9f9c2ed4265ddda55335ccde09', 'bar_date': '2026-10-08', 'close': '254.06', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:45af03f119bf61e7b15fffd8dc90582e', 'version_hash': '6fa831e20becf61da72649ae51f251b2a3484d0ccdca192ca3d581b35a577428', 'version_ref': 'market-price-series-version:1c263ec886d9c4aa29073818a587e8d8'}；**shares**：{'artifact_hash': 'd5f999da3feeb9b8a4fce9597fa24c44c4593827d50cd7486d6bffb61a17b2c7', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:f7433a8fd023c33e24b17ae13cd924ee', 'shares_outstanding': '10786313572', 'version_hash': '6fa831e20becf61da72649ae51f251b2a3484d0ccdca192ca3d581b35a577428', 'version_ref': 'market-price-series-version:1c263ec886d9c4aa29073818a587e8d8'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 20.2569 | 10.06 | 169 | 3 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 当前投资收益很大，低 PE 不意味着核心收益便宜。 |
| price_to_sales | available | 3.5329 | 46.75 | 169 | 3 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | -0.004242 | 10.06 | 169 | 3 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### GOOGL

环境：hyperscalers；版本：`valuation-snapshot-version:4f3d279bee9ee7ddffd74a99e73fbc72`；生成：2026-10-09T14:11:11.028731+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': 'dbcf395e5649fa52861f4c08da6c7e97480fbb6ca7b358882834c00fc16dad31', 'bar_date': '2026-10-08', 'close': '348.29', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:4a55747f6e9594fd126961b185d7a48f', 'version_hash': '00a25d677dcc619b7154a0a2f23d5c668901d609802c58ae9e36ee64e870cb3f', 'version_ref': 'market-price-series-version:86dac9ffa947449336c39d7dfc231a19'}；**shares**：{'artifact_hash': 'd7e7e353e551eb389477725b49876965820caf76921d23054b9d9552e0d71db9', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:50c4a4219f0f49c71a7d75fb7123e562', 'shares_outstanding': '12229934831', 'version_hash': '00a25d677dcc619b7154a0a2f23d5c668901d609802c58ae9e36ee64e870cb3f', 'version_ref': 'market-price-series-version:86dac9ffa947449336c39d7dfc231a19'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 17.4426 | 25.29 | 170 | 3 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 当前投资收益很大，低 PE 不意味着核心收益便宜。 |
| price_to_sales | available | 9.5534 | 38.39 | 112 | 2 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.012507 | 7.65 | 170 | 3 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### META

环境：hyperscalers；版本：`valuation-snapshot-version:e410fb2b62eba5d0d596c09600411ede`；生成：2026-10-09T14:21:11.525448+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

**price**：{'artifact_hash': '1f9e1efcf5b4244f823565a7798fb215d7f4b46c1d52e7ad41a80140f93e0fc8', 'bar_date': '2026-10-08', 'close': '720.89', 'currency': 'USD', 'invocation_ref': 'connector-invocation:yfinance:7496de40485b680a8e975a40037fa09c', 'version_hash': 'c40683ceefe4b31133db26ee3f7118482bf9dae564ce36275c49e3c2cd4951e5', 'version_ref': 'market-price-series-version:598f32046555a42c9dbfa9f5568d8dce'}；**shares**：{'artifact_hash': '5b54f4d1b726bc8e70d6225dc492bb669d6e8c4203016f04dfa0352cdd809baa', 'as_of': '2026-10-09', 'invocation_ref': 'connector-invocation:yfinance:5cc798ed37578dc042a8880f00b09aba', 'shares_outstanding': '2547506225', 'version_hash': 'c40683ceefe4b31133db26ee3f7118482bf9dae564ce36275c49e3c2cd4951e5', 'version_ref': 'market-price-series-version:598f32046555a42c9dbfa9f5568d8dce'}；评估：当前股数取值需可追溯；历史 share observation count 为 0，分位样本不足长期周期。

| 指标 | 状态 | 值 | 分位% | 样本 | 财务窗口 | 评估 |
|---|---|---|---|---|---|---|
| trailing_pe | available | 26.9681 | 72 | 175 | 3 | C：reported NI 分母要调整投资收益／税项；当前股数与 TTM weighted shares 不同，需说明与 EPS PE 的差别。 |
| price_to_sales | available | 8.046 | 68.57 | 175 | 3 | B／C：可作粗估值对照，但利润结构／资本强度不同，不能跨公司直接等价。 |
| ev_to_ebitda | unavailable | None | None | None | None | C：未取得 debt／D&A 映射，诚实 unavailable；补期末债务、lease 与现金，不能说公司没披露。 |
| fcf_yield | available | 0.022312 | 7.43 | 175 | 3 | C：CFO−PP&E 定义需与公司 FCF 桥接，营运资金／一次性／租赁／软件资本化不能忽略。 |


### MSFT 估值

无估值快照。不可用是输入缺口，不是 P/E／FCF 为零。

## 九份 holding-period return bridge：每条腿

固定 PE／倍数是情景假设，不是市场合理估值结论。realised leg 的 EPS 按期间而非信息可得日，是事后分解，不可作为当时可交易归因。总回报还用 trailing dividend yield 近似，不等于窗口实际分红再投资 TSR。

### IBM

环境：legacy；版本：`holding-period-return-version:959411c5809bd68950cefb865cf00273`；生成：2026-10-09T15:07:21.506466+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | -19.39 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | 3.25 | market_implied | {'end': '2027-06-30', 'start': '2026-07-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；模型没有稀释股数预测：股数按最近一期持平 | C：固定现 PE、缺股数则平；NI 缺失用历史净利率代理，不能称完整自有 EPS／风险回报。 |
| expected_consensus | available | 9.14 | consensus | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | 25.79 | sell_side_note | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### CTSH

环境：legacy；版本：`holding-period-return-version:4f45b24bc4b84daf49f36691d725b414`；生成：2026-10-09T14:22:00.908232+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | -10.37 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | 12.55 | market_implied | {'end': '2027-06-30', 'start': '2026-07-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；代理口径：驱动模型没有算出净利润，用模型收入 × 过去四季净利率（10.26%） | C：固定现 PE、缺股数则平；NI 缺失用历史净利率代理，不能称完整自有 EPS／风险回报。 |
| expected_consensus | available | 11.98 | consensus | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | 5.00 | sell_side_note | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### EPAM

环境：legacy；版本：`holding-period-return-version:27f1fb621ac5f3e18d807cf73ad19aac`；生成：2026-10-09T14:52:11.800788+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | -24.63 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | 2.46 | market_implied | {'end': '2027-06-30', 'start': '2026-07-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；代理口径：驱动模型没有算出净利润，用模型收入 × 过去四季净利率（7.15%）；模型没有稀释股数预测：股数按最近一期持平 | C：固定现 PE、缺股数则平；NI 缺失用历史净利率代理，不能称完整自有 EPS／风险回报。 |
| expected_consensus | available | 7.87 | consensus | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | 7.68 | sell_side_note | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### ACN

环境：legacy；版本：`holding-period-return-version:021373e0fe0cc4b83d0a5b848f9bfbd4`；生成：2026-10-09T14:06:42.320173+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | -15.12 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | 9.24 | market_implied | {'end': '2027-05-31', 'start': '2026-06-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；代理口径：驱动模型没有算出净利润，用模型收入 × 过去四季净利率（10.66%）；模型没有稀释股数预测：股数按最近一期持平 | C：固定现 PE、缺股数则平；NI 缺失用历史净利率代理，不能称完整自有 EPS／风险回报。 |
| expected_consensus | available | 10.40 | consensus | {'end': '2027-08-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | 5.00 | sell_side_note | {'end': '2027-08-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### DXC

环境：legacy；版本：`holding-period-return-version:365618bfd0d5391f8f51f5aae6d195c5`；生成：2026-10-09T15:02:24.653158+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | -7.27 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数；缺稀释股数行：回购效应并入盈利增长，未单独拆出 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | -6.92 | market_implied | {'end': '2027-06-30', 'start': '2026-07-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；代理口径：驱动模型没有算出净利润，用模型收入 × 过去四季净利率（0.99%）；缺稀释股数行：回购效应无法拆出 | C：固定现 PE、缺股数则平；NI 缺失用历史净利率代理，不能称完整自有 EPS／风险回报。 |
| expected_consensus | available | -9.34 | consensus | {'end': '2027-03-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | -15.12 | sell_side_note | {'end': '2027-03-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### AMZN

环境：hyperscalers；版本：`holding-period-return-version:e8c9bab056a6ddb183991d094699d9ac`；生成：2026-10-09T14:11:10.922312+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | 12.81 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | unavailable | None | None | None |  | C：诚实不可用；缺模型／估值／EPS，不能与可用腿平均或当零回报。 |
| expected_consensus | available | 30.49 | consensus | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | 38.00 | sell_side_note | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### GOOGL

环境：hyperscalers；版本：`holding-period-return-version:a9caf0c77421119b9476095c08ce5fdc`；生成：2026-10-09T14:16:08.431536+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | 42.38 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数；缺稀释股数行：回购效应并入盈利增长，未单独拆出；缺股息数据：股息贡献未计 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | 23.77 | market_implied | {'end': '2027-06-30', 'start': '2026-07-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；代理口径：驱动模型没有算出净利润，用模型收入 × 过去四季净利率（54.75%）；缺稀释股数行：回购效应无法拆出 | D／P0：NI unavailable 却用受一次性污染 TTM margin 补利润；把证券收益隐含的 54.75% 净利率延长。 |
| expected_consensus | available | 23.31 | consensus | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | -9.93 | sell_side_note | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### META

环境：hyperscalers；版本：`holding-period-return-version:6c4309fd9a8c3509b6e661e63573c2de`；生成：2026-10-09T14:21:12.121680+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | available | 0.72 | None | {'end': '2026-10-08', 'start': '2025-10-08'} | 窗口两端的 EPS 按财务期间对齐（期末 ≤ 窗口端点的最近一期），不是当时已知的数 | C：EPS 有 look-ahead，股数和倍数对数归因可作事后描述；股息用 TTM yield 近似。 |
| expected_driver_model | available | 25.87 | market_implied | {'end': '2027-06-30', 'start': '2026-07-01'} | 估值倍数按当前市盈率延续（市场价格隐含），不假设重估；代理口径：驱动模型没有算出净利润，用模型收入 × 过去四季净利率（29.84%）；模型没有稀释股数预测：股数按最近一期持平 | D／P0：NI unavailable 却用受一次性污染 TTM margin 补利润；忽略费用快速增长下的 OP 路径；同四季 OP 合计约 514.7 亿，代理 NI 约 855.1 亿，两年末 OP 为亏损。 |
| expected_consensus | available | 11.18 | consensus | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：街上 EPS adjusted／GAAP 未完全对齐，期间可能为财年／季度而非恒定未来十二月；需同口径。 |
| expected_sell_side | available | 9.01 | sell_side_note | {'end': '2026-12-31', 'start': '2026-10-08'} | 一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算；股数变化已包含在一致预期 EPS 增长里，不单独拆出 | C：目标价是另一种预期来源，不是自身模型；需日期、期限、分散度与实际 dividend 假设。 |


### MSFT

环境：hyperscalers；版本：`holding-period-return-version:f182fc0b3e30ce6eef821d2004a16ee5`；生成：2026-10-09T14:06:05.030788+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 腿 | 状态 | 回报% | 预期来源 | 窗口 | 关键 notes／假设 | 评估 |
|---|---|---|---|---|---|---|
| realised_trailing_year | unavailable | None | None | None |  | C：诚实不可用；缺模型／估值／EPS，不能与可用腿平均或当零回报。 |
| expected_driver_model | unavailable | None | None | None |  | C：诚实不可用；缺模型／估值／EPS，不能与可用腿平均或当零回报。 |
| expected_consensus | unavailable | None | None | None |  | C：诚实不可用；缺模型／估值／EPS，不能与可用腿平均或当零回报。 |
| expected_sell_side | unavailable | None | None | None |  | C：诚实不可用；缺模型／估值／EPS，不能与可用腿平均或当零回报。 |


## 七份 sensitivity：逐 driver 和每条共识差

单因素历史 band 扰动可测公式敏感性；不是情景概率／风险分布，无法证明选取的 driver 有经济意义。

### CTSH

环境：legacy；版本：`sensitivity-projection:3177dd3315252bf70be00d4894d32ffb:62`；生成：2026-10-09T14:01:39.713139+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6`；当前 `forecast-model-version:3177dd3315252bf70be00d4894d32ffb:6`。B／C：版本匹配，仍受底模假设限制。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:Revenues | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001058290-26-000016', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001058290-26-000031', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.012562349898'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001058290-25-000341', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001058290-26-000016', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2025-03-31', 'ref': None}, {'accession': '0001058290-26-000031', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001058290-26-000031', 'concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001058290-26-000031', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.666301769750'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001058290-25-000341', 'concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaa… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:DepreciationAndAmortization | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001058290-26-000016', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001058290-26-000031', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.014184397163'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001058290-25-000341', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001058290-26-000016', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001058290-26-000016', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001058290-26-000031', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.018062397373'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001058290-25-000267', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:SellingGeneralAndAdministrativeExpense | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001058290-26-000016', 'concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001058290-26-000031', 'concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '-0.079646017699'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001058290-25-000341', 'concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001058290-25-000341', 'concept': 'us-gaap:SellingGeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001058290-26-000016', 'concept': 'u… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2026 Revenues | 22040981610.50654259 | 22194878740 | -0.69 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q3 Revenues | 5542431030.57042000 | 5645162420 | -1.82 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q4 Revenues | 5604550579.93612259 | 5647730630 | -0.76 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


### EPAM

环境：legacy；版本：`sensitivity-projection:f8921ad873856ba15d281cb7c75b08fd:59`；生成：2026-10-09T14:06:42.325239+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9`；当前 `forecast-model-version:f8921ad873856ba15d281cb7c75b08fd:9`。B／C：版本匹配，仍受底模假设限制。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001352010-26-000029', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001352010-26-000046', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '-0.001713356334'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001352010-25-000029', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001352010-25-000056', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '00… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001352010-26-000029', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001352010-26-000046', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.010503828047'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001352010-25-000056', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001352010-25-000056', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001352010-26-000029', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001352010-26-000046', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.010753714216'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001352010-25-000029', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001352010-25-000056', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '000… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfRevenue | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001352010-26-000046', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001352010-26-000046', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.696368377266'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001352010-25-000056', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001352010-25-000056', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001352010-25-000056', 'concept': 'us-gaap:CostOfRevenue', 'k… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:DepreciationAndAmortization | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001352010-26-000029', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001352010-26-000046', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.017819207965'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001352010-25-000056', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001352010-25-000056', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001352010-26-000029', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2026 Revenues | 5660233822.46498043 | 5656945700 | 0.06 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q3 Revenues | 1414767000.00000000 | 1419251920 | -0.32 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q4 Revenues | 1430638822.46498043 | 1423375440 | 0.51 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027 Revenues | 5884864832.25504695 | 5858678280 | 0.45 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


### ACN

环境：legacy；版本：`sensitivity-projection:ff98fb9b6accc2576b2bfe8fa89a38d5:67`；生成：2026-10-09T13:56:35.172933+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8`；当前 `forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:8`。B／C：版本匹配，仍受底模假设限制。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | {'band': {'count': 8, 'first_period': '2024-08-31', 'last_period': '2026-05-31', 'latest': {'period_end': '2026-05-31', 'refs': [{'accession': '0001467373-26-000014', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}, {'accession': '0001467373-26-000032', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}], 'value': '0.202274969142'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001467373-25-000169', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000217', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000222', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:Revenues | {'band': {'count': 7, 'first_period': '2024-11-30', 'last_period': '2026-05-31', 'latest': {'period_end': '2026-05-31', 'refs': [{'accession': '0001467373-26-000014', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-02-28', 'ref': None}, {'accession': '0001467373-26-000032', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}], 'value': '0.037357322901'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001467373-25-000217', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000222', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-11-30', 'ref': None}, {'accession': '0001467373-26-000014', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2025-02-28', 'ref': None}, {'accession': '0001467373-26-000032', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | {'band': {'count': 8, 'first_period': '2024-08-31', 'last_period': '2026-05-31', 'latest': {'period_end': '2026-05-31', 'refs': [{'accession': '0001467373-26-000014', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}, {'accession': '0001467373-26-000032', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}], 'value': '0.009948849630'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001467373-25-000169', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000217', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000222', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfGoodsAndServicesSold | {'band': {'count': 8, 'first_period': '2024-08-31', 'last_period': '2026-05-31', 'latest': {'period_end': '2026-05-31', 'refs': [{'accession': '0001467373-26-000032', 'concept': 'us-gaap:CostOfGoodsAndServicesSold', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}, {'accession': '0001467373-26-000032', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}], 'value': '0.672278672501'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001467373-25-000217', 'concept': 'us-gaap:CostOfGoodsAndServicesSold', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000217', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000222', 'concept': 'us-gaap:CostOfGoodsAndServicesSold', 'kind': 'input_cell', 'period_end': '2024-11-30',… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:GeneralAndAdministrativeExpense | {'band': {'count': 7, 'first_period': '2024-11-30', 'last_period': '2026-05-31', 'latest': {'period_end': '2026-05-31', 'refs': [{'accession': '0001467373-26-000014', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-02-28', 'ref': None}, {'accession': '0001467373-26-000032', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-05-31', 'ref': None}], 'value': '-0.056621185427'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001467373-25-000169', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2024-08-31', 'ref': None}, {'accession': '0001467373-25-000222', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2024-11-30', 'ref': None}, {'accession': '0001467373-26-000014', 'concept': 'us-gaap:GeneralAndAdministrat… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2026 Revenues | 74492992262.99874490 | 73566672090 | 1.26 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q4 Revenues | 18988657262.99874490 | 19411241500 | -2.18 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027 Revenues | 78738802838.79618096 | 81240639950 | -3.08 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027Q1 Revenues | 19263079964.10514769 | 18828149490 | 2.31 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


### DXC

环境：legacy；版本：`sensitivity-projection:c1328e1764845e6ef93ea4ef086bccb5:52`；生成：2026-10-09T14:11:47.839392+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9`；当前 `forecast-model-version:c1328e1764845e6ef93ea4ef086bccb5:9`。B／C：版本匹配，仍受底模假设限制。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001688568-26-000069', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001688568-26-000069', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.139379793264'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001688568-25-000072', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-25-000098', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-25-000098', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:Revenues | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001688568-26-000005', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001688568-26-000069', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '-0.041853035144'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001688568-25-000098', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-26-000005', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001688568-26-000005', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2025-03-31', 'ref': None}, {'accession': '0001688568-26-000069', … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001688568-26-000069', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001688568-26-000069', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.019673224408'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001688568-25-000072', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-25-000098', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-25-000098', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001688568-26-000069', 'concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001688568-26-000069', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.796265421807'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001688568-25-000098', 'concept': 'us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-25-000098', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-26-000005', 'concept': 'us-gaa… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:DepreciationAndAmortization | {'band': {'count': 7, 'first_period': '2024-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001688568-26-000005', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001688568-26-000069', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '-0.039568345324'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001688568-25-000098', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001688568-26-000005', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001688568-26-000005', 'concept': 'us-gaap:DepreciationAndAmortization', 'kind':… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2027 Revenues | 11769326346.87133786 | 12161147500 | -3.22 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027Q2 Revenues | 2960899384.75189600 | 2986066670 | -0.84 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027Q3 Revenues | 2923282816.48021216 | 3082100000 | -5.15 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2028 Revenues | 11182536936.05607807 | 11988076540 | -6.72 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


### IBM

环境：legacy；版本：`sensitivity-projection:e958ceeed41ec924ffc2342fe2109df4:47`；生成：2026-10-09T14:17:02.551215+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4`；当前 `forecast-model-version:e958ceeed41ec924ffc2342fe2109df4:4`。B／C：版本匹配，仍受底模假设限制。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:Revenues | {'band': {'count': 11, 'first_period': '2023-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0000051143-26-000038', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0000051143-26-000078', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.078218257209'}, 'mean': {'period_end': None, 'refs': [{'accession': '0000051143-24-000049', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2023-12-31', 'ref': None}, {'accession': '0000051143-25-000032', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-03-31', 'ref': None}, {'accession': '0000051143-25-000052', … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0000051143-26-000038', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0000051143-26-000078', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.151322689663'}, 'mean': {'period_end': None, 'refs': [{'accession': '0000051143-24-000039', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0000051143-26-000038', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0000051143-26-000078', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.013343433166'}, 'mean': {'period_end': None, 'refs': [{'accession': '0000051143-24-000039', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfRevenue | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0000051143-26-000078', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0000051143-26-000078', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.422736277823'}, 'mean': {'period_end': None, 'refs': [{'accession': '0000051143-24-000049', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2023-12-31', 'ref': None}, {'accession': '00000511… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:ResearchAndDevelopmentExpense | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0000051143-26-000078', 'concept': 'us-gaap:ResearchAndDevelopmentExpense', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0000051143-26-000078', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.134657965272'}, 'mean': {'period_end': None, 'refs': [{'accession': '0000051143-24-000049', 'concept': 'us-gaap:ResearchAndDevelopmentExpense', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0000051143-24-000049', 'concept': 'us-gaap:ResearchAndDevelopmentExpense', 'kind': 'input_cell', 'period_end': '20… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2026 Revenue | 68103116513.68325685 | 70394870180 | -3.26 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q3 Revenue | 17394323840.28796000 | 16842602400 | 3.28 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q4 Revenue | 17629792673.39529685 | 20485595520 | -13.94 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027 Revenue | 72938261132.41067026 | 73120277120 | -0.25 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


### META

环境：hyperscalers；版本：`sensitivity-projection:56e60bcab451be700f45c44b1929362a:16`；生成：2026-10-07T22:41:38.567283+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:56e60bcab451be700f45c44b1929362a:2`；当前 `forecast-model-version:56e60bcab451be700f45c44b1929362a:3`。C：投影仍绑定旧模型，不能称当前 sensitivity。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:GeneralAndAdministrativeExpense | {'band': {'count': 11, 'first_period': '2023-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001628280-26-028526', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001628280-26-050705', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '1.145753634277'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001326801-24-000081', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2023-12-31', 'ref': None}, {'accession': '0001326801-25-000054', 'concept': 'us-gaap:GeneralAndAdministrat… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax | {'band': {'count': 11, 'first_period': '2023-12-31', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001628280-26-028526', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001628280-26-050705', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.079735753228'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001326801-24-000081', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2023-12-31', 'ref': None},… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:ResearchAndDevelopmentExpense | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001628280-26-050705', 'concept': 'us-gaap:ResearchAndDevelopmentExpense', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001628280-26-050705', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.356178352330'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001326801-24-000081', 'concept': 'us-gaap:ResearchAndDevelopmentExpense', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'conc… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:SellingAndMarketingExpense | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001628280-26-050705', 'concept': 'us-gaap:SellingAndMarketingExpense', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001628280-26-050705', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.056429992928'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001326801-24-000081', 'concept': 'us-gaap:SellingAndMarketingExpense', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': … | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfRevenue | {'band': {'count': 12, 'first_period': '2023-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001628280-26-050705', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001628280-26-050705', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.186345619315'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001326801-24-000081', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'kind': 'input_cell', 'period_end': '2023-09-30', 'ref': None}, {'accession': '0001326801-24-000081', 'concept': 'us-gaap:CostOfRevenue', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2026 Revenue | 251168454553.39607252 | 254164485210 | -1.18 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q3 Revenue | 64862068165.83854100 | 63329245590 | 2.42 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q4 Revenue | 69194386387.55753152 | 73704896660 | -6.12 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027 Revenue | 326185826765.58936338 | 306488920930 | 6.43 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


### GOOGL

环境：hyperscalers；版本：`sensitivity-projection:4f24149a9da4cfc4e3393070ad7df433:23`；生成：2026-10-09T14:00:55.746772+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

模型绑定 `forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3`；当前 `forecast-model-version:4f24149a9da4cfc4e3393070ad7df433:3`。B／C：版本匹配，仍受底模假设限制。

| driver | band／基准与影响定位 | 评估 |
|---|---|---|
| concept:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | {'band': {'count': 9, 'first_period': '2024-06-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001652044-26-000048', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001652044-26-000071', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.375004173762'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001652044-25-000043', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000062', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000062', 'concept': 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:NetCashProvidedByUsedInOperatingActivities | {'band': {'count': 9, 'first_period': '2024-06-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001652044-26-000048', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001652044-26-000071', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.326129420014'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001652044-25-000043', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000062', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000062', 'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:Revenues | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001652044-26-000048', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001652044-26-000071', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.090085171435'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001652044-25-000062', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000091', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001652044-25-000091', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-12-31', 'ref': None}, {'accession': '0001652044-26-000048', '… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:CostOfRevenue | {'band': {'count': 9, 'first_period': '2024-06-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001652044-26-000071', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}, {'accession': '0001652044-26-000071', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.383510300845'}, 'mean': {'period_end': None, 'refs': [{'accession': '0001652044-25-000062', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000062', 'concept': 'us-gaap:Revenues', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000091', 'concept': 'us-gaap:CostOfRevenue', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '000165204… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |
| concept:us-gaap:GeneralAndAdministrativeExpense | {'band': {'count': 8, 'first_period': '2024-09-30', 'last_period': '2026-06-30', 'latest': {'period_end': '2026-06-30', 'refs': [{'accession': '0001652044-26-000048', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-03-31', 'ref': None}, {'accession': '0001652044-26-000071', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2026-06-30', 'ref': None}], 'value': '0.505709624796'}, 'mean': {'period_end': None, 'refs': {'accession': '0001652044-25-000062', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2024-06-30', 'ref': None}, {'accession': '0001652044-25-000091', 'concept': 'us-gaap:GeneralAndAdministrativeExpense', 'kind': 'input_cell', 'period_end': '2024-09-30', 'ref': None}, {'accession': '0001652044-25-000091', 'concept': 'us-gaap:GeneralAndAdministrati… | C：历史 min/max 或 band 不等于未来合理分布；需价格／量／成本相关性、不同执行预算及业务机制。 |

| 对照条目 | ours | consensus | gap% | 评估 |
|---|---|---|---|---|
| FY2026 Revenues | 490261246519.77089155 | 498741458330 | -1.70 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q3 Revenues | 126657417664.78526400 | 127400614950 | -0.58 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2026Q4 Revenues | 133911828854.98562755 | 141592714250 | -5.42 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |
| FY2027 Revenues | 616866708989.56573902 | 614952947060 | 0.31 | C：同期间数值差是描述；缺季节性／GAAP 对齐／来源时效，不能直接称 alpha 或据此 avoid。 |


## 193 份利润桥（各自不同期间）

完整逐期间表原件已私有归档。complete 指结构和残差在系统容差内，不等于特殊费用、税项、普通股归属、源概念已人工审计。下表展示公司分布与最新期间。

| 公司 | 不同期间数 | 状态分布 | 最新期末 | 最新状态 | 评估 |
|---|---|---|---|---|---|
| IBM | 21 | {'missing_rows': 21} | 2026-06-30 | missing_rows | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| CTSH | 21 | {'complete': 21} | 2026-06-30 | complete | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| EPAM | 21 | {'complete': 21} | 2026-06-30 | complete | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| ACN | 24 | {'complete': 22, 'missing_rows': 2} | 2026-05-31 | complete | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| DXC | 21 | {'missing_rows': 21} | 2026-06-30 | missing_rows | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| AMZN | 21 | {'complete': 21} | 2026-06-30 | complete | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| GOOGL | 21 | {'complete': 21} | 2026-06-30 | complete | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| META | 21 | {'complete': 21} | 2026-06-30 | complete | 历史会计桥有用，先处理缺锚／一次性／普通股归属。 |
| MSFT | 22 | {'complete': 22} | 2026-06-30 | complete | C：MSFT 最新桥止于 2026/3，已发布 FY26 Q4 尚未进入该链。 |


## 九份每股序列：每个期间记录

每期 EPS／股数／DPS 为历史观察，不是预测股数。annual＋YTD−prior YTD 的每股 EPS 加减仅为近似；股数变动时应按净利及期间 weighted shares 重建 TTM。

### IBM

环境：legacy；版本：`per-share-series-version:a63278a064abc9d1dd030d5e76b518e4`；生成：2026-10-07T22:27:25.675244+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=21；缺口：[]

### CTSH

环境：legacy；版本：`per-share-series-version:9c74b80f0969c2cf67968ba812eb4d13`；生成：2026-10-07T22:32:27.365938+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=21；缺口：[]

### EPAM

环境：legacy；版本：`per-share-series-version:5055b41e882cf3639d4ceabff021ea48`；生成：2026-10-07T22:37:16.987520+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=21；缺口：[]

### ACN

环境：legacy；版本：`per-share-series-version:7839e5aba1cd84c960fdb4912ecb9bce`；生成：2026-10-07T22:42:24.057067+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=24；缺口：[]

### DXC

环境：legacy；版本：`per-share-series-version:daf4f10516b0fe092966b0a3a56815cb`；生成：2026-10-07T22:51:05.748446+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。 diluted shares 缺失，不能量化缩股对 EPS 的贡献。
原记录 periods=21；缺口：[{'end': '2022-12-31', 'field': 'basic_shares', 'fiscal_label': 'FY2023 前9个月', 'label': '基本加权股数', 'start': '2022-04-01'}, {'end': '2022-12-31', 'field': 'diluted_shares', 'fiscal_label': 'FY2023 前9个月', 'label': '稀释加权股数', 'start': '2022-04-01'}, {'end': '2022-12-31', 'field': 'basic_shares', 'fiscal_label': 'FY2023 Q3', 'label': '基本加权股数', 'start': '2022-10-01'}, {'end': '2022-12-31', 'field': 'dilu…

### AMZN

环境：hyperscalers；版本：`per-share-series-version:93e6e45ab3ac4cb05efac496741188b7`；生成：2026-10-07T22:26:26.030689+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=21；缺口：[]

### GOOGL

环境：hyperscalers；版本：`per-share-series-version:1a98aef09b4680faec9a6cf77fa51fff`；生成：2026-10-07T22:31:24.748204+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。 股数／股息映射缺口应补，普通股与 preferred 分配需核；公司付息不能因采集缺数标无股息。
原记录 periods=21；缺口：[{'end': '2023-03-31', 'field': 'basic_shares', 'fiscal_label': 'FY2023 Q1', 'label': '基本加权股数', 'start': '2023-01-01'}, {'end': '2023-03-31', 'field': 'diluted_shares', 'fiscal_label': 'FY2023 Q1', 'label': '稀释加权股数', 'start': '2023-01-01'}, {'end': '2023-06-30', 'field': 'basic_shares', 'fiscal_label': 'FY2023 前6个月', 'label': '基本加权股数', 'start': '2023-01-01'}, {'end': '2023-06-30', 'field': 'dilute…

### META

环境：hyperscalers；版本：`per-share-series-version:eb430513ac2fb8d8f3931433c2b4cce8`；生成：2026-10-07T22:36:38.937586+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=21；缺口：[]

### MSFT

环境：hyperscalers；版本：`per-share-series-version:9b949241da693f16d3e9ead69e55af26`；生成：2026-10-07T22:41:38.308447+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

B／C：历史 filed 每股信息可用；期间加减生成 TTM EPS 不必等于 TTM 净利／TTM weighted shares。
原记录 periods=22；缺口：[]

### IBM TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#385'], 'value': '8.23'}, 'components': [{'end': '2023-12-31', 'role': 'annual', 'start': '2023-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#386'], 'value': '8.14'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | 截至 2024-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#385', 'statement-line:statement-ingest:cbb58cee6db37e1d395f83a08326730a#309', 'statement-line:statement-ingest:cbb58cee6db37e1d395f83a08326730a#257'], 'value': '8.96'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#385', 'statement-line:statement-ingest:34440f1021fa7a53e5e0bcedd8cdf3bc#375', 'statement-line:statement-ingest:34440f1021fa7a53e5e0bcedd8cdf3bc#271'], 'value': '9.21'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#385', 'statement-line:statement-ingest:d2680262c21c2bd4d248735862dd017d#385', 'statement-line:statement-ingest:d2680262c21c2bd4d248735862dd017d#281'], 'value': '6.98'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#462'], 'value': '6.53'}, 'components': [{'end': '2024-12-31', 'role': 'annual', 'start': '2024-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#463'], 'value': '6.43'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#462', 'statement-line:statement-ingest:2f734b56b95a8c4e7f9a03bda635df1e#334', 'statement-line:statement-ingest:cbb58cee6db37e1d395f83a08326730a#309'], 'value': '5.92'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#462', 'statement-line:statement-ingest:88c08a8ae6dff1a0cb8f11bdfda115fc#415', 'statement-line:statement-ingest:34440f1021fa7a53e5e0bcedd8cdf3bc#375'], 'value': '6.29'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#462', 'statement-line:statement-ingest:6a03ff326d26b6944171498fff0e00a2#419', 'statement-line:statement-ingest:d2680262c21c2bd4d248735862dd017d#385'], 'value': '8.51'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#539'], 'value': '11.36'}, 'components': [{'end': '2025-12-31', 'role': 'annual', 'start': '2025-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#540'], 'value': '11.17'}, 'diluted_shares': {'basis': 'annual_w… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#539', 'statement-line:statement-ingest:4659516ef446c3a62d5433c68fa165a8#333', 'statement-line:statement-ingest:2f734b56b95a8c4e7f9a03bda635df1e#334'], 'value': '11.52'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:4a4ad277e993d5a59ab57b464a2f0a0b#539', 'statement-line:statement-ingest:90cc3acdb23db80ba243ee2d9cc4e099#408', 'statement-line:statement-ingest:88c08a8ae6dff1a0cb8f11bdfda115fc#415'], 'value': '11.46'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### CTSH TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#207'], 'value': '4.21'}, 'components': [{'end': '2023-12-31', 'role': 'annual', 'start': '2023-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#208'], 'value': '4.21'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | 截至 2024-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#207', 'statement-line:statement-ingest:20ca9b6623683e4cfd20d9a0edb401e5#151', 'statement-line:statement-ingest:20bc947551b2a3abac98e5c5ffd8a37d#153'], 'value': '4.17'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#207', 'statement-line:statement-ingest:68b10974bc8aebe8950bf2ce32ca2666#171', 'statement-line:statement-ingest:82d94e3061bde19143e291f7a886b90b#169'], 'value': '4.4'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {'… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#207', 'statement-line:statement-ingest:b958aeea5b3008a18921191b880fa254#186', 'statement-line:statement-ingest:30b38df008aeb97af4746e4208b70b5e#187'], 'value': '4.52'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#238'], 'value': '4.52'}, 'components': [{'end': '2024-12-31', 'role': 'annual', 'start': '2024-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#239'], 'value': '4.51'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#238', 'statement-line:statement-ingest:20ca9b6623683e4cfd20d9a0edb401e5#182', 'statement-line:statement-ingest:20ca9b6623683e4cfd20d9a0edb401e5#151'], 'value': '4.76'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#238', 'statement-line:statement-ingest:68b10974bc8aebe8950bf2ce32ca2666#234', 'statement-line:statement-ingest:68b10974bc8aebe8950bf2ce32ca2666#171'], 'value': '4.93'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#238', 'statement-line:statement-ingest:b958aeea5b3008a18921191b880fa254#250', 'statement-line:statement-ingest:b958aeea5b3008a18921191b880fa254#186'], 'value': '4.33'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#269'], 'value': '4.57'}, 'components': [{'end': '2025-12-31', 'role': 'annual', 'start': '2025-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#270'], 'value': '4.56'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#269', 'statement-line:statement-ingest:36457e97b27148f7ea33a6603fc520f6#175', 'statement-line:statement-ingest:20ca9b6623683e4cfd20d9a0edb401e5#182'], 'value': '4.62'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:7e5945b811292c1651b239d922e5c324#269', 'statement-line:statement-ingest:6f75596ec91f72a957af32688b2aca1f#237', 'statement-line:statement-ingest:68b10974bc8aebe8950bf2ce32ca2666#234'], 'value': '4.68'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### EPAM TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#240'], 'value': '7.21'}, 'components': [{'end': '2023-12-31', 'role': 'annual', 'start': '2023-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#241'], 'value': '7.06'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | 截至 2024-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#240', 'statement-line:statement-ingest:03bc2f0fe65616a1009aa1efdd2d7996#288', 'statement-line:statement-ingest:03bc2f0fe65616a1009aa1efdd2d7996#196'], 'value': '7.45'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#240', 'statement-line:statement-ingest:b5ddb46e7337c53526fa491d1795be40#405', 'statement-line:statement-ingest:b5ddb46e7337c53526fa491d1795be40#220'], 'value': '7.09'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#240', 'statement-line:statement-ingest:417549655a145a08f9decce701d9703d#430', 'statement-line:statement-ingest:417549655a145a08f9decce701d9703d#240'], 'value': '7.8'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {'… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#337'], 'value': '7.93'}, 'components': [{'end': '2024-12-31', 'role': 'annual', 'start': '2024-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#338'], 'value': '7.84'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#337', 'statement-line:statement-ingest:cda4a9846494c2112ffebc85527387eb#276', 'statement-line:statement-ingest:03bc2f0fe65616a1009aa1efdd2d7996#288'], 'value': '7.21'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#337', 'statement-line:statement-ingest:2c2a6851010aaf01835cdb6ac4acb98c#381', 'statement-line:statement-ingest:b5ddb46e7337c53526fa491d1795be40#405'], 'value': '7.07'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#337', 'statement-line:statement-ingest:0d1028e453215cdd6be9d6294040e970#391', 'statement-line:statement-ingest:417549655a145a08f9decce701d9703d#430'], 'value': '6.59'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#412'], 'value': '6.76'}, 'components': [{'end': '2025-12-31', 'role': 'annual', 'start': '2025-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#413'], 'value': '6.72'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#412', 'statement-line:statement-ingest:91437add36fc814a9035efb90da832e4#265', 'statement-line:statement-ingest:cda4a9846494c2112ffebc85527387eb#276'], 'value': '7'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, {'en… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:d03b4c31faec5098306c71a7e7439f0d#412', 'statement-line:statement-ingest:499bac0c1f285f02cb0f818fca47d8a3#358', 'statement-line:statement-ingest:2c2a6851010aaf01835cdb6ac4acb98c#381'], 'value': '7.4'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, {'… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### ACN TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-02-28 | None | None | {'components': [], 'end': '2023-02-28', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-05-31 | None | None | {'components': [], 'end': '2023-05-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-08-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#264'], 'value': '10.9'}, 'components': [{'end': '2023-08-31', 'role': 'annual', 'start': '2022-09-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#265'], 'value': '10.77'}, 'diluted_shares': {'basis': 'annual_we… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-11-30 | None | None | {'components': [], 'end': '2023-11-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-02-29 | 截至 2024-02-29 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#264', 'statement-line:statement-ingest:a9da29c0de6325ff55bfc4293991e198#304', 'statement-line:statement-ingest:a9da29c0de6325ff55bfc4293991e198#221'], 'value': '11.17'}, 'components': [{'end': '2023-08-31', 'role': 'prior_fiscal_year', 'start': '2022-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-05-31 | 截至 2024-05-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#264', 'statement-line:statement-ingest:244b32d87f9e079104de11ac8bc0254a#318', 'statement-line:statement-ingest:244b32d87f9e079104de11ac8bc0254a#234'], 'value': '11.06'}, 'components': [{'end': '2023-08-31', 'role': 'prior_fiscal_year', 'start': '2022-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-08-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#334'], 'value': '11.57'}, 'components': [{'end': '2024-08-31', 'role': 'annual', 'start': '2023-09-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#335'], 'value': '11.44'}, 'diluted_shares': {'basis': 'quarter_… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-11-30 | 截至 2024-11-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#334', 'statement-line:statement-ingest:ca2f7c13d6ae3495a7b4340c85a72ab7#246', 'statement-line:statement-ingest:ca2f7c13d6ae3495a7b4340c85a72ab7#208'], 'value': '12.07'}, 'components': [{'end': '2024-08-31', 'role': 'prior_fiscal_year', 'start': '2023-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-02-28 | 截至 2025-02-28 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#334', 'statement-line:statement-ingest:e228663cd2d55a63270e43a7cabcc10b#309', 'statement-line:statement-ingest:a9da29c0de6325ff55bfc4293991e198#304'], 'value': '12.26'}, 'components': [{'end': '2024-08-31', 'role': 'prior_fiscal_year', 'start': '2023-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-05-31 | 截至 2025-05-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#334', 'statement-line:statement-ingest:dd3fb3d5bc8ceca4318ac47242e1ee03#312', 'statement-line:statement-ingest:244b32d87f9e079104de11ac8bc0254a#318'], 'value': '12.7'}, 'components': [{'end': '2024-08-31', 'role': 'prior_fiscal_year', 'start': '2023-09-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-08-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#419'], 'value': '12.29'}, 'components': [{'end': '2025-08-31', 'role': 'annual', 'start': '2024-09-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#420'], 'value': '12.15'}, 'diluted_shares': {'basis': 'quarter_… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-11-30 | 截至 2025-11-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#419', 'statement-line:statement-ingest:1c8e68f88ac190c02eb06285025e6b52#259', 'statement-line:statement-ingest:ca2f7c13d6ae3495a7b4340c85a72ab7#246'], 'value': '12.22'}, 'components': [{'end': '2025-08-31', 'role': 'prior_fiscal_year', 'start': '2024-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-02-28 | 截至 2026-02-28 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#419', 'statement-line:statement-ingest:ff83ba26d7c801face11b9d3ad950c99#351', 'statement-line:statement-ingest:e228663cd2d55a63270e43a7cabcc10b#309'], 'value': '12.33'}, 'components': [{'end': '2025-08-31', 'role': 'prior_fiscal_year', 'start': '2024-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-05-31 | 截至 2026-05-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:900a7a4b24ca636855551c525cbc610e#419', 'statement-line:statement-ingest:8820722186cfd558af7dda797cc18fbe#312', 'statement-line:statement-ingest:dd3fb3d5bc8ceca4318ac47242e1ee03#312'], 'value': '12.63'}, 'components': [{'end': '2025-08-31', 'role': 'prior_fiscal_year', 'start': '2024-09-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### DXC TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2022-12-31 | None | None | {'components': [], 'end': '2022-12-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | None | None | {'components': [], 'end': '2023-12-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#278'], 'value': '0.46'}, 'components': [{'end': '2024-03-31', 'role': 'annual', 'start': '2023-04-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#279'], 'value': '0.46'}, 'diluted_shares': None, 'dps': {'refs':… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#278', 'statement-line:statement-ingest:ce63ce622a2771a850b9fb37cf71c283#230', 'statement-line:statement-ingest:ce63ce622a2771a850b9fb37cf71c283#201'], 'value': '0.43'}, 'components': [{'end': '2024-03-31', 'role': 'prior_fiscal_year', 'start': '2023-04-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#278', 'statement-line:statement-ingest:f777a44468c01a447480fb3f057d3e7f#278', 'statement-line:statement-ingest:f777a44468c01a447480fb3f057d3e7f#221'], 'value': '0.18'}, 'components': [{'end': '2024-03-31', 'role': 'prior_fiscal_year', 'start': '2023-04-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | 截至 2024-12-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#278', 'statement-line:statement-ingest:5eac4d2d5311a167f9ef97e259cb6f2c#274', 'statement-line:statement-ingest:ec5f9c629cdb5e2493fa543a8fee5ccf#283'], 'value': '-0.3'}, 'components': [{'end': '2024-03-31', 'role': 'prior_fiscal_year', 'start': '2023-04-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#316'], 'value': '2.15'}, 'components': [{'end': '2025-03-31', 'role': 'annual', 'start': '2024-04-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#317'], 'value': '2.1'}, 'diluted_shares': None, 'dps': {'refs': … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#316', 'statement-line:statement-ingest:d5f092b68cd02df5507188b9c74213c3#244', 'statement-line:statement-ingest:ce63ce622a2771a850b9fb37cf71c283#230'], 'value': '2.1'}, 'components': [{'end': '2025-03-31', 'role': 'prior_fiscal_year', 'start': '2024-04-01'}, {'… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#316', 'statement-line:statement-ingest:cee282accf15ec80c9ca660987a7e7c8#298', 'statement-line:statement-ingest:f777a44468c01a447480fb3f057d3e7f#278'], 'value': '2.06'}, 'components': [{'end': '2025-03-31', 'role': 'prior_fiscal_year', 'start': '2024-04-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | 截至 2025-12-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#316', 'statement-line:statement-ingest:db3ea6482a63a796af5524784d3cd7af#306', 'statement-line:statement-ingest:5eac4d2d5311a167f9ef97e259cb6f2c#274'], 'value': '2.36'}, 'components': [{'end': '2025-03-31', 'role': 'prior_fiscal_year', 'start': '2024-04-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | FY2026 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#354'], 'value': '0.1'}, 'components': [{'end': '2026-03-31', 'role': 'annual', 'start': '2025-04-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#355'], 'value': '0.1'}, 'diluted_shares': None, 'dps': {'refs': [… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:9f8f4c93f964ba692007d9f458851ada#354', 'statement-line:statement-ingest:993651551053a8a364f3db37d11c2acb#229', 'statement-line:statement-ingest:d5f092b68cd02df5507188b9c74213c3#244'], 'value': '0.76'}, 'components': [{'end': '2026-03-31', 'role': 'prior_fiscal_year', 'start': '2025-04-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### AMZN TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#214'], 'value': '2.95'}, 'components': [{'end': '2023-12-31', 'role': 'annual', 'start': '2023-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#215'], 'value': '2.9'}, 'diluted_shares': {'basis': 'annual_weig… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | 截至 2024-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#214', 'statement-line:statement-ingest:bdb159ccccdd3b2d8b9695b11a7852bc#243', 'statement-line:statement-ingest:bdb159ccccdd3b2d8b9695b11a7852bc#204'], 'value': '3.64'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#214', 'statement-line:statement-ingest:98741dd9303dbbc0396299b1048c21f6#350', 'statement-line:statement-ingest:98741dd9303dbbc0396299b1048c21f6#272'], 'value': '4.28'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#214', 'statement-line:statement-ingest:5a0447f4394bb02806e5c87dd66aaec6#350', 'statement-line:statement-ingest:5a0447f4394bb02806e5c87dd66aaec6#272'], 'value': '4.78'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#258'], 'value': '5.66'}, 'components': [{'end': '2024-12-31', 'role': 'annual', 'start': '2024-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#259'], 'value': '5.53'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#258', 'statement-line:statement-ingest:28d193a038720ef921f9ebc462170941#250', 'statement-line:statement-ingest:bdb159ccccdd3b2d8b9695b11a7852bc#243'], 'value': '6.28'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#258', 'statement-line:statement-ingest:9d4e2168a62e264d08cea6efbbcaa446#360', 'statement-line:statement-ingest:98741dd9303dbbc0396299b1048c21f6#350'], 'value': '6.68'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#258', 'statement-line:statement-ingest:706165785e13a6ab4f938577b25513ff#360', 'statement-line:statement-ingest:5a0447f4394bb02806e5c87dd66aaec6#350'], 'value': '7.21'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#302'], 'value': '7.29'}, 'components': [{'end': '2025-12-31', 'role': 'annual', 'start': '2025-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#303'], 'value': '7.17'}, 'diluted_shares': {'basis': 'annual_wei… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#302', 'statement-line:statement-ingest:0c18b76560f9a0a3ce10491243518ca9#264', 'statement-line:statement-ingest:28d193a038720ef921f9ebc462170941#250'], 'value': '8.49'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:bdc242a9759c0f5557f235ea2372656c#302', 'statement-line:statement-ingest:c305da030ea7a50fe24a0806c382b7c9#380', 'statement-line:statement-ingest:9d4e2168a62e264d08cea6efbbcaa446#360'], 'value': '12.61'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### GOOGL TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#197'], 'value': '5.84'}, 'components': [{'end': '2023-12-31', 'role': 'annual', 'start': '2023-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#198'], 'value': '5.8'}, 'diluted_shares': None, 'dps': None, 'en… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | 截至 2024-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#197', 'statement-line:statement-ingest:584073cd3f98ec3aabcfa56b4199e5b4#230', 'statement-line:statement-ingest:584073cd3f98ec3aabcfa56b4199e5b4#186'], 'value': '6.57'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#197', 'statement-line:statement-ingest:4f49384089b6b89d0a819e3908bd841b#284', 'statement-line:statement-ingest:4f49384089b6b89d0a819e3908bd841b#196'], 'value': '7.03'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#197', 'statement-line:statement-ingest:ef94bfe845a693c1deb64e49cb4d665f#285', 'statement-line:statement-ingest:ef94bfe845a693c1deb64e49cb4d665f#197'], 'value': '7.61'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#231'], 'value': '8.13'}, 'components': [{'end': '2024-12-31', 'role': 'annual', 'start': '2024-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#232'], 'value': '8.04'}, 'diluted_shares': None, 'dps': None, 'e… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#231', 'statement-line:statement-ingest:6350f1b7ee320b45a693da1fd115adf1#230', 'statement-line:statement-ingest:584073cd3f98ec3aabcfa56b4199e5b4#230'], 'value': '9.06'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#231', 'statement-line:statement-ingest:1e5a3f503e871301fc3181a6de1da279#187', 'statement-line:statement-ingest:4f49384089b6b89d0a819e3908bd841b#284'], 'value': '9.47'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#231', 'statement-line:statement-ingest:ec7ceb61c2c16729676952ae61106ace#239', 'statement-line:statement-ingest:ef94bfe845a693c1deb64e49cb4d665f#285'], 'value': '10.23'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#265'], 'value': '10.91'}, 'components': [{'end': '2025-12-31', 'role': 'annual', 'start': '2025-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#266'], 'value': '10.81'}, 'diluted_shares': None, 'dps': None, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#265', 'statement-line:statement-ingest:78db75354548863cd2732b553617077e#194', 'statement-line:statement-ingest:6350f1b7ee320b45a693da1fd115adf1#230'], 'value': '13.24'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:141a5e2d50f9b58d4d7ff4de519448b0#265', 'statement-line:statement-ingest:1e5a3f503e871301fc3181a6de1da279#277', 'statement-line:statement-ingest:1e5a3f503e871301fc3181a6de1da279#187'], 'value': '20.16'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### META TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-06-30 | None | None | {'components': [], 'end': '2023-06-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | FY2023 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#205'], 'value': '15.19'}, 'components': [{'end': '2023-12-31', 'role': 'annual', 'start': '2023-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#206'], 'value': '14.87'}, 'diluted_shares': {'basis': 'annual_w… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | 截至 2024-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#205', 'statement-line:statement-ingest:156b50f12bc30640d1c99c9ea71241b6#189', 'statement-line:statement-ingest:156b50f12bc30640d1c99c9ea71241b6#161'], 'value': '17.84'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | 截至 2024-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#205', 'statement-line:statement-ingest:509088a108726100ac6ee026efae4965#231', 'statement-line:statement-ingest:509088a108726100ac6ee026efae4965#175'], 'value': '20.12'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#205', 'statement-line:statement-ingest:5f8ebfbe3ffb50f774c7b98f29401f6a#231', 'statement-line:statement-ingest:5f8ebfbe3ffb50f774c7b98f29401f6a#175'], 'value': '21.83'}, 'components': [{'end': '2023-12-31', 'role': 'prior_fiscal_year', 'start': '2023-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#234'], 'value': '24.61'}, 'components': [{'end': '2024-12-31', 'role': 'annual', 'start': '2024-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#235'], 'value': '23.86'}, 'diluted_shares': {'basis': 'annual_w… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#234', 'statement-line:statement-ingest:7de90f80ea21b4163ebd111709443b11#175', 'statement-line:statement-ingest:156b50f12bc30640d1c99c9ea71241b6#189'], 'value': '26.34'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | 截至 2025-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#234', 'statement-line:statement-ingest:54b1beef43d505188e2751975d545c3e#226', 'statement-line:statement-ingest:509088a108726100ac6ee026efae4965#231'], 'value': '28.31'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#234', 'statement-line:statement-ingest:f0307cf9708713422610407c9ae65ea7#228', 'statement-line:statement-ingest:5f8ebfbe3ffb50f774c7b98f29401f6a#231'], 'value': '23.2'}, 'components': [{'end': '2024-12-31', 'role': 'prior_fiscal_year', 'start': '2024-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#263'], 'value': '23.98'}, 'components': [{'end': '2025-12-31', 'role': 'annual', 'start': '2025-01-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#264'], 'value': '23.49'}, 'diluted_shares': {'basis': 'annual_w… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#263', 'statement-line:statement-ingest:7175bae1dddec99097abb39148a8f2d4#178', 'statement-line:statement-ingest:7de90f80ea21b4163ebd111709443b11#175'], 'value': '27.96'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | 截至 2026-06-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:56ce110ae5e9c1cb5d84d4488aa9cfed#263', 'statement-line:statement-ingest:4834daae61250277379519b9d8deb8cd#224', 'statement-line:statement-ingest:54b1beef43d505188e2751975d545c3e#226'], 'value': '26.9'}, 'components': [{'end': '2025-12-31', 'role': 'prior_fiscal_year', 'start': '2025-01-01'}, {… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |


### MSFT TTM 派生窗口

| 期末 | 财年标签 | 规则 | 组成／状态定位 | 评估 |
|---|---|---|---|---|
| 2022-12-31 | None | None | {'components': [], 'end': '2022-12-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-03-31 | None | None | {'components': [], 'end': '2023-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-09-30 | None | None | {'components': [], 'end': '2023-09-30', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2023-12-31 | None | None | {'components': [], 'end': '2023-12-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-03-31 | None | None | {'components': [], 'end': '2024-03-31', 'fiscal_label': None, 'reason': '缺上一完整财年或上年同期累计，按规则不能组成过去四个季度', 'rule': None} | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-06-30 | FY2024 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#290'], 'value': '11.86'}, 'components': [{'end': '2024-06-30', 'role': 'annual', 'start': '2023-07-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#291'], 'value': '11.8'}, 'diluted_shares': {'basis': 'annual_we… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-09-30 | 截至 2024-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#290', 'statement-line:statement-ingest:457f73efe30de50cf68f10cea544a810#291', 'statement-line:statement-ingest:457f73efe30de50cf68f10cea544a810#252'], 'value': '12.18'}, 'components': [{'end': '2024-06-30', 'role': 'prior_fiscal_year', 'start': '2023-07-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2024-12-31 | 截至 2024-12-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#290', 'statement-line:statement-ingest:700c35dde8b35f8d8ec0bed8feb17d14#407', 'statement-line:statement-ingest:c6b349bcb8c9c4cdeb27259adbe31e70#409'], 'value': '12.48'}, 'components': [{'end': '2024-06-30', 'role': 'prior_fiscal_year', 'start': '2023-07-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-03-31 | 截至 2025-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#290', 'statement-line:statement-ingest:bb6368a0a051ea748e56565a2ff3fb5e#409', 'statement-line:statement-ingest:ca39492bebd7288e065338f544d787cb#410'], 'value': '12.99'}, 'components': [{'end': '2024-06-30', 'role': 'prior_fiscal_year', 'start': '2023-07-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-06-30 | FY2025 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#334'], 'value': '13.7'}, 'components': [{'end': '2025-06-30', 'role': 'annual', 'start': '2024-07-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#336'], 'value': '13.64'}, 'diluted_shares': {'basis': 'annual_we… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-09-30 | 截至 2025-09-30 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#334', 'statement-line:statement-ingest:d67c8fc617d16b194e8556a9f1081692#295', 'statement-line:statement-ingest:457f73efe30de50cf68f10cea544a810#291'], 'value': '14.11'}, 'components': [{'end': '2025-06-30', 'role': 'prior_fiscal_year', 'start': '2024-07-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2025-12-31 | 截至 2025-12-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#334', 'statement-line:statement-ingest:f5f5f105169f7401f8047406c7974a45#411', 'statement-line:statement-ingest:700c35dde8b35f8d8ec0bed8feb17d14#407'], 'value': '16.05'}, 'components': [{'end': '2025-06-30', 'role': 'prior_fiscal_year', 'start': '2024-07-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-03-31 | 截至 2026-03-31 的过去四个季度 | annual_plus_ytd_minus_prior_ytd | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#334', 'statement-line:statement-ingest:7073f9023ebec94d1f5b7139edd757c5#414', 'statement-line:statement-ingest:bb6368a0a051ea748e56565a2ff3fb5e#409'], 'value': '16.86'}, 'components': [{'end': '2025-06-30', 'role': 'prior_fiscal_year', 'start': '2024-07-01'}, … | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |
| 2026-06-30 | FY2026 | annual | {'basic_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#378'], 'value': '18'}, 'components': [{'end': '2026-06-30', 'role': 'annual', 'start': '2025-07-01'}], 'diluted_eps': {'refs': ['statement-line:statement-ingest:111f9b8eaa889393d4538a52599906a4#380'], 'value': '17.95'}, 'diluted_shares': {'basis': 'annual_weig… | B／C：期间与组成可追溯；每股加减对股数变化仅近似，需按 NI／weighted shares 与实际披露 EPS 交叉。 |

逐期字段／评语见 每股 CSV（历史证据原件已私有归档）。

## 九份共识：逐 revenue／EPS／目标价

vendor observation 比单一券商更适合作市场基准，但不是确定未来；analysis count、期间、adjusted／GAAP、币种和时间戳需一致。前瞻未接到对应期间，不等于这里没数据。

### ACN

环境：legacy；版本：`consensus-estimate-version:028f4962da295fdad7c0465d6470e727`；生成：2026-10-09T13:51:46.090205+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q4 | 2026-08-31 | 3.94689 | 3.82 | 4.04 | 21 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-08-31 | None | None | None | None | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027Q1 | 2026-11-30 | 3.15056 | 3.04 | 3.49 | 21 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-08-31 | 15.90406 | 15.03 | 16.61 | 28 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-08-31 | 19411241500 | 19217000000 | 19632000000 | 19 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-08-31 | 73566672090 | 73320000000 | 73996000000 | 26 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027Q1 | 2026-11-30 | 18828149490 | 18522700000 | 19068900000 | 18 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-08-31 | 81240639950 | 78471000000 | 83677000000 | 25 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 223.65459 | None | None | 24 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### CTSH

环境：legacy；版本：`consensus-estimate-version:93810ab0655a9a3f08f0f68f41570e5e`；生成：2026-10-09T13:56:36.875557+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q3 | 2026-09-30 | 1.46383 | 1.42 | 1.5 | 23 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026Q4 | 2026-12-31 | 1.52494 | 1.45 | 1.59 | 22 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-12-31 | 5.75512 | 5.68 | 5.81 | 27 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-12-31 | 6.33896 | 6.2 | 6.54 | 27 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q3 | 2026-09-30 | 5645162420 | 5630000000 | 5690000000 | 22 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-12-31 | 5647730630 | 5590000000 | 5673657000 | 20 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-12-31 | 22194878740 | 22124000000 | 22320000000 | 29 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-12-31 | 23244835200 | 22821000000 | 23673000000 | 29 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 65.904 | None | None | 25 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### EPAM

环境：legacy；版本：`consensus-estimate-version:5414a7045c29a49224fe061082892228`；生成：2026-10-09T14:01:41.618083+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q3 | 2026-09-30 | 3.40436 | 3.26 | 3.44 | 17 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026Q4 | 2026-12-31 | 3.51264 | 3.46 | 3.62 | 17 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-12-31 | 13.15114 | 13.08 | 13.3 | 19 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-12-31 | 14.05986 | 13.27 | 14.69 | 19 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q3 | 2026-09-30 | 1419251920 | 1410436000 | 1429000000 | 16 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-12-31 | 1423375440 | 1400510000 | 1450000000 | 16 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-12-31 | 5656945700 | 5633400000 | 5694000000 | 19 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-12-31 | 5858678280 | 5700300000 | 5985344000 | 19 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 122.82353 | None | None | 17 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### DXC

环境：legacy；版本：`consensus-estimate-version:0ee79ef4ac8dd240ac6b364e34f674e7`；生成：2026-10-09T14:06:44.180225+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2027Q2 | 2026-09-30 | 0.55046 | 0.54 | 0.56 | 8 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027Q3 | 2026-12-31 | 0.80756 | 0.75 | 0.93 | 8 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-03-31 | 2.59189 | 2.47 | 2.74 | 8 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2028 | 2028-03-31 | 2.95755 | 2.64283 | 3.32 | 7 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027Q2 | 2026-09-30 | 2986066670 | 2974000000 | 2998100000 | 6 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027Q3 | 2026-12-31 | 3082100000 | 3034000000 | 3131700000 | 5 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-03-31 | 12161147500 | 12105580000 | 12200000000 | 8 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2028 | 2028-03-31 | 11988076540 | 11886000000 | 12226635800 | 7 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 11.21429 | None | None | 7 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### IBM

环境：legacy；版本：`consensus-estimate-version:930bbbfa79ae16fc288ef03f5c0ee70f`；生成：2026-10-09T14:11:49.515564+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q3 | 2026-09-30 | 2.86433 | 2.75 | 3.12 | 19 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026Q4 | 2026-12-31 | 4.62401 | 4.41 | 4.768 | 18 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-12-31 | 12.3121 | 12.08 | 12.63 | 23 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-12-31 | 13.15121 | 12.30183 | 13.86 | 23 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q3 | 2026-09-30 | 16842602400 | 16669833600 | 17050000000 | 18 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-12-31 | 20485595520 | 20120884580 | 20713000000 | 16 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-12-31 | 70394870180 | 69869318180 | 70930000000 | 22 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-12-31 | 73120277120 | 71325270790 | 74486000000 | 22 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 240.59 | None | None | 22 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### AMZN

环境：hyperscalers；版本：`consensus-estimate-version:1499d5034670d7c3c4f3cf26614f7e77`；生成：2026-10-09T13:51:18.374627+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q3 | 2026-09-30 | 1.95204 | 1.72 | 2.23 | 45 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026Q4 | 2026-12-31 | 2.44288 | 2 | 2.75 | 42 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-12-31 | 12.89234 | 12.16 | 13.51 | 51 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-12-31 | 10.5269 | 8.73 | 15.0435 | 53 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q3 | 2026-09-30 | 202022776300 | 199369000000 | 205272000000 | 47 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-12-31 | 244153134570 | 234241000000 | 255934000000 | 46 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-12-31 | 828433394500 | 815866000000 | 841977000000 | 56 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-12-31 | 948442130010 | 898023000000 | 992145100000 | 58 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 331.53 | None | None | 58 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### GOOGL

环境：hyperscalers；版本：`consensus-estimate-version:202e9f258d3838441c84319d07fb1c95`；生成：2026-10-09T13:55:58.854749+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q3 | 2026-09-30 | 3.0235 | 2.65 | 3.68 | 43 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026Q4 | 2026-12-31 | 3.3456 | 3.03 | 4.09232 | 39 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-12-31 | 20.62887 | 19.89 | 21.92 | 53 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-12-31 | 14.97242 | 13.67 | 17.21393 | 53 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q3 | 2026-09-30 | 127400614950 | 123409000000 | 132554000000 | 39 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-12-31 | 141592714250 | 135389700000 | 158818481540 | 35 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-12-31 | 498741458330 | 489824800000 | 519817939360 | 51 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-12-31 | 614952947060 | 545052000000 | 674430000000 | 50 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 429.47406 | None | None | 54 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### META

环境：hyperscalers；版本：`consensus-estimate-version:897e8458aa8e11bd4ab9dc258a8c22ed`；生成：2026-10-09T14:00:55.914890+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2026Q3 | 2026-09-30 | 6.25261 | 2.88 | 7.64 | 44 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026Q4 | 2026-12-31 | 8.26407 | 6.84 | 9.66 | 43 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2026 | 2026-12-31 | 31.04359 | 27.7 | 33.65 | 55 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-12-31 | 34.07467 | 28.77952 | 41.5 | 54 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q3 | 2026-09-30 | 63347373250 | 61702000000 | 65278000000 | 47 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026Q4 | 2026-12-31 | 73704766220 | 71681000000 | 75897000000 | 46 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2026 | 2026-12-31 | 254116759170 | 251029000000 | 258738000000 | 60 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-12-31 | 306527238830 | 288178000000 | 353304000000 | 60 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 799.34 | None | None | 58 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


### MSFT

环境：hyperscalers；版本：`consensus-estimate-version:ce555667fc1aac93b901ff6ffcf991f6`；生成：2026-10-09T14:06:01.610429+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

| 类型 | 期间 | 期末／as of | 平均 | 低 | 高 | 分析师数 | 评估 |
|---|---|---|---|---|---|---|---|
| eps_estimates | FY2027Q1 | 2026-09-30 | 4.72336 | 4.43 | 4.83363 | 27 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027Q2 | 2026-12-31 | 4.82904 | 4.72 | 5.02 | 27 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2027 | 2027-06-30 | 19.76867 | 19.21 | 20.7 | 34 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| eps_estimates | FY2028 | 2028-06-30 | 23.67732 | 22.08 | 26 | 33 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027Q1 | 2026-09-30 | 90685460400 | 89640000000 | 92655000000 | 39 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027Q2 | 2026-12-31 | 95590068270 | 93527000000 | 98410000000 | 38 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2027 | 2027-06-30 | 391270574280 | 380000000000 | 402719127430 | 50 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| revenue_estimates | FY2028 | 2028-06-30 | 467954706440 | 433188000000 | 503000000000 | 51 | B／C：保留范围与样本；EPS basis 未明确时不能直接比较 GAAP 模型；财年与恒定十二月不同。 |
| price_target | 目标价 | 2026-10-09 | 587.6338 | None | None | 53 | B／C：均值不是合理价值结论；需 high/low、期限、旧目标更新分布与 dividend。 |


## 旧人工 ACN inputs／run

旧链仍有三份 input 与一份 run；不能与当前公司模型合并解释。1.15% QoQ 来源于约 4.7% 本币年增换算，美元收入延长未显式处理 FX、季度季节性、收购。作为演示基线 C，作为自有预测不足。

环境：legacy；版本：`input-version:acn:scenario:base:1`；生成：2026-09-01T17:05:20.315590+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

{'actor_ref': 'human:lumos', 'admission_decision_ref': 'decision:input:acn:scenario:1', 'content_hash': '092d72933bda4b404b356b68d56078455d53ebe200512532430941b46e87829b', 'created_at': '2026-09-01T17:05:20.315590+00:00', 'id': 'input-version:acn:scenario:base:1', 'input_kind': 'scenario', 'model_input_ref': 'input:acn:scenario:base', 'payload': {'base_scenario_version_hash': None, 'base_scenario_version_ref': None, 'description': 'ACN base scenario bound to the FY2026 Q3 revenue actual.', 'label': 'Base', 'owner_ref': 'human:lumos', 'scenario_ref': 'input:acn:scenario:base', 'schema_version': '0.1'}, 'prior_version_ref': None, 'schema_version': '0.1', 'version': 1}

评估 C：仅作为历史人工假设记录／结果，需独立基本面与口径校准。

环境：legacy；版本：`input-version:acn:revenue:1`；生成：2026-09-01T17:05:20.420327+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

{'actor_ref': 'human:lumos', 'admission_decision_ref': 'decision:input:acn:actual:1', 'content_hash': '128ecce5f3197bb8f0b3236f4e348a3e6a4d9da55a2d732c5b470c9f00c0cc80', 'created_at': '2026-09-01T17:05:20.420327+00:00', 'id': 'input-version:acn:revenue:1', 'input_kind': 'actual', 'model_input_ref': 'input:acn:revenue', 'payload': {'business_line_ref': None, 'currency': 'USD', 'metric_ref': 'metric:revenue-usd', 'period': {'calendar': 'company:fiscal', 'end': '2026-05-31', 'kind': 'quarter', 'start': '2026-03-01'}, 'schema_version': '0.1', 'source_authorities': [{'authority_kind': 'evidence_version', 'content_hash': 'b9378b95e280c943185640abd9f879c05333788de2ed015e900f246bd3362bb3', 'version_ref': 'evidence-version:6dc635ffcf08ea088636fc626ee6a83650b5e0e9e902a81209d354c78d215d06'}], 'subject_ref': 'company:sec-cik:0001467373', 'unit': 'million', 'value': '18718.144'}, 'prior_version_ref': None, 'schema_version': '0.1', 'version': 1}

评估 C：仅作为历史人工假设记录／结果，需独立基本面与口径校准。

环境：legacy；版本：`input-version:acn:revenue-growth:1`；生成：2026-09-01T17:05:20.526092+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

{'actor_ref': 'human:lumos', 'admission_decision_ref': 'decision:input:acn:growth:1', 'content_hash': '6f65b4cd79b122b1eb579a6a40d00c15973a458eeac02d7b08fc29423d0221e1', 'created_at': '2026-09-01T17:05:20.526092+00:00', 'id': 'input-version:acn:revenue-growth:1', 'input_kind': 'assumption', 'model_input_ref': 'input:acn:revenue-growth', 'payload': {'currency': 'USD', 'dependency_bindings': [], 'driver_ref': 'driver:bookings-mix-and-conversion', 'effective_period': {'calendar': 'company:fiscal', 'end': '2027-08-31', 'kind': 'forecast_period', 'start': '2026-06-01'}, 'formula': None, 'owner_ref': 'human:lumos', 'provenance': 'judgment', 'rationale': 'Thesis implied_expectation is mid-single-digit local-currency annual growth (~4.7%); compounded to ~1.15% per fiscal quarter.', 'scenario_version_hash': '092d72933bda4b404b356b68d56078455d53ebe200512532430941b46e87829b', 'scenario_version_ref': 'input-version:acn:scenario:base:1', 'schema_version': '0.1', 'source_authorities': [], 'subject_r…

评估 C：仅作为历史人工假设记录／结果，需独立基本面与口径校准。

环境：legacy；版本：`model-run-version:acn-revenue-growth-extend:1`；生成：2026-09-01T17:05:26.944330+00:00；正文 refs／claim_refs 引用 0 条，其中检查时退休 0 条。

{'actor_ref': 'automation:forecast-extender', 'completed_at': '2026-09-01T17:05:26.943490+00:00', 'content_hash': '467dfdfa16f7d9754201bd1ef684088b9f2a2714d7a0fe11d4a8b46e5f54bee1', 'created_at': '2026-09-01T17:05:26.944330+00:00', 'errors': [], 'formula_version_hash': '219f2ade5284fa9048aee285702e85fa1c3d8e76e0e9bdc285720c2ee8bbe5d8', 'formula_version_ref': 'formula:quarterly-growth-extend:1', 'id': 'model-run-version:acn-revenue-growth-extend:1', 'input_bindings': [{'binding_ref': 'base-actual', 'role': 'base', 'version_hash': '128ecce5f3197bb8f0b3236f4e348a3e6a4d9da55a2d732c5b470c9f00c0cc80', 'version_ref': 'input-version:acn:revenue:1'}, {'binding_ref': 'growth-assumption', 'role': 'growth', 'version_hash': '6f65b4cd79b122b1eb579a6a40d00c15973a458eeac02d7b08fc29423d0221e1', 'version_ref': 'input-version:acn:revenue-growth:1'}], 'model_run_ref': 'model-run:acn-revenue-growth-extend', 'outputs': [{'authority_bindings': [], 'currency': 'USD', 'metric_ref': 'metric:revenue-usd', 'outpu…

评估 C：仅作为历史人工假设记录／结果，需独立基本面与口径校准。
