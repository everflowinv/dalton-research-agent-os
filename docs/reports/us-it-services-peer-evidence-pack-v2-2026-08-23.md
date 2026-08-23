# US IT Services Peer Evidence Pack v2

## 当前结论

Dalton 现在可以在隔离环境中做有来源约束的真实行业研究，但还没有进入 live 自主研究阶段。v2 已把 ACN、CTSH、EPAM、
IBM 四家公司放进同一份 Evidence Pack，生成四份 Company Overlay 和一份确定性的行业 brief snapshot；它仍不写 live
Core、不建立 ThesisVersion，也不调用付费模型。

这轮验证的重点不是给四家公司排投资顺序，而是确认同一行业框架能保留各公司不同的披露口径。系统不得把缺失数字补成
零，也不得把 reported、constant currency、organic constant currency、公司整体和业务分部指标混在一起。

## 一手证据

本轮按 SEC filing-first 流程锁定五个 exact accession：

- ACN Q3 FY2026 earnings exhibit：`0001467373-26-000031`；
  https://www.sec.gov/Archives/edgar/data/1467373/000146737326000031/q3fy26earnings8-kexhibit.htm
- CTSH Q2 CY2026 earnings exhibit：`0001058290-26-000030`；
  https://www.sec.gov/Archives/edgar/data/1058290/000105829026000030/exhibit9916302026.htm
- EPAM Q2 CY2026 earnings exhibit：`0001352010-26-000043`；
  https://www.sec.gov/Archives/edgar/data/1352010/000135201026000043/exhibit99_q2x2026.htm
- IBM Q2 CY2026 earnings exhibit：`0000051143-26-000077`；
  https://www.sec.gov/Archives/edgar/data/51143/000005114326000077/ibm-20260722xex991.htm
- IBM Q2 CY2026 10-Q：`0000051143-26-000078`；
  https://www.sec.gov/Archives/edgar/data/51143/000005114326000078/ibm-20260630.htm

ACN 的报告期截至 2026-05-31，另外三家截至 2026-06-30。行业输出保留这个错月，不能把四家公司当作完全同步的季度。

## 可比结果与限制

- 收入增速：ACN 本地货币 3.0%，CTSH constant currency 4.1%，EPAM organic constant currency 3.4%，IBM Consulting
  constant currency 1.0%。EPAM 剔除并购，IBM 只是 Consulting 分部，所以这四个数字只能并列展示，不能机械排名。
- 订单：ACN 披露季度本地货币新签订单同比 -3%；CTSH 披露季度 bookings 同比 -6%，但没有把该增速标成 constant
  currency，同时披露 TTM bookings USD 29.1 billion。EPAM 和 IBM 没有同口径公司整体订单指标进入本版 pack。
- AI：四家公司都有管理层定性表述，均只记为 `metric:ai-demand-signal`，不改写成 AI bookings、AI revenue 或市场规模。
- 盈利：ACN、CTSH、EPAM 的 GAAP operating margin 分别为 17.0%、15.9%、10.8%。IBM 只用 10-Q 的 GAAP revenue
  USD 17.162 billion 和 gross profit USD 9.907 billion 推导 consolidated gross margin 57.7%，并标记为不可与前三家的
  operating margin 排名。
- 现金与回购：ACN 和 IBM 分别披露季度 free cash flow USD 3.60 billion、USD 2.50 billion；CTSH 和 EPAM 分别披露
  季度回购 USD 1.153 billion、USD 85 million。不同字段不会拼成一个统一的“现金能力”排名。

## 新增合同

每份 Company Overlay 现在必须覆盖 Driver Pack 中的每一个 KPI，并给出以下状态之一：

- `observed`：必须绑定同一 metric 的正式 ClaimVersion；
- `not_found_in_reviewed_sources`：已审来源没有形成这个字段；
- `not_comparable`：有相关披露，但不能当作这个标准字段；
- `not_applicable`：该字段不适用于公司或业务边界。

非 `observed` 状态不能夹带 Claim。每条 Claim 也必须出现在对应 KPI 的 `observed` 记录中。这样可以明确区分“没有数字”、
“没有找到”和“数字不具备可比性”。

`industry_brief_snapshot` 只接收与同一个 Evidence Pack exact ref/hash 绑定的 Overlay，并要求覆盖池中每家公司恰好一份。
本轮 snapshot 形成 4 个 driver scoreboard、19 行 KPI 矩阵和 76 个公司单元格；缺一家公司或混入旧 pack 的 overlay 都会
fail closed。

## 隔离 canary

- Driver Pack：v1 → v2 → v3，4 个 driver、18 个唯一 metric；
- formal Evidence：5 条；Claim / Relation：各 21 条；
- human-admitted actual Model Input：17 条；
- Industry Evidence Pack：v1 → v2；当前 Company Overlay：ACN v2、CTSH v1、EPAM v1、IBM v1；
- 行业 brief：4 个 scoreboard、19 行 KPI、76 个单元格；
- ThesisVersion：0；paid model call：0；
- IndustryResearch 与 ModelInput integrity report 均通过。

## 尚未完成

- 未把 pack 写入 live Core，未启用 production mapping；
- 未自动生成可发布的行业文字结论或投资判断；
- 尚未用 earnings call、AlphaEngine 或 Guidepoint 检查订单定义、AI 转化和交付效率；
- 尚未准入任何公司 thesis，也没有估值输入和正式 valuation run。

因此可以开始受控研究和积累证据，但仍应把当前版本看作开发验收环境。下一 gate 是把 earnings-call evidence 接入同一
pack，并让报告生成器只从 exact snapshot 出稿。
