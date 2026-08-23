# Dalton 数据源、配额与建模顺序 v0.8

日期：2026-08-23
状态：当前近期执行顺序；替代 v0.7 的 connector / 行业研究近端顺序，不改写历史报告

## 裁决

先完成受限数据链，再补最小财务模型地基，之后才启动 US IT Services 行业研究。这里的“先建模”不是先造一个
通用 Excel 引擎，而是先让研究过程中产生的 actual、assumption、forecast、scenario 和 valuation output 有正式、
可版本化、可回指来源的位置。否则 coordinator 会把模型输入塞进 Claim、报告正文或临时文件，后面必须迁移。

近期顺序改为：

1. connector 日配额与北京时间日界线；
2. AlphaEngine 有页数、响应字节和连续 offset 约束的完整文档分页；
3. Gemini web search 与独立 web fetch；search 只发现来源，原始页面才可进入证据链；
4. Model Input Ledger v1：actual / assumption / forecast / scenario / model run / reconciliation；
5. US IT Services 行业 evidence pack，ACN 作为第一个 company overlay；
6. 模型生成行业 thesis candidate，只有人工可以准入正式 Industry Thesis / ThesisVersion。

## 配额

ConnectorStore 已有硬计数链：每次 physical call 先形成 quota reservation，完成后以 UsageEntry、CostEntry 和
QuotaSettlement 结算；同一 quota scope 和 window 内，已消费、未过期预留和 indeterminate 调用都会占用额度。
超过 calls / bytes / records / cost 任一上限会在 provider call 前阻断，结算漂移会打开 blocking incident。

本轮 owner-approved 日配额为：

- AlphaEngine `search_library`：50 次/日；
- AlphaEngine `get_document`：80 份完整文档/日；首个 page 预留并结算 1 个 document unit，续页不重复扣文档额度；
- Gemini `search_web`：1,000 次/日；
- 三者都在 `Asia/Shanghai` 当地 00:00 重置，即 UTC 16:00；
- unknown route 没有隐式 unlimited policy。

AlphaEngine 每个 page 仍形成独立 physical call，并逐页记录 calls 和 bytes。内部最多按 20 页/文档设置
1,600 calls 的日安全上限；这不是 AlphaEngine 的计费口径，文档日额度仍只看 80 个 document units。
AlphaEngine live bridge 仍是 development candidate，Gemini bridge 尚未实现，因此这些数字目前是候选治理策略，
不是已经部署到 live Core 的生产 policy。credential grant 的 `max_calls` 仍是更窄的一次性/批次权限，不能用它
替代跨 work order 的每日计数。

## OpenClaw 数据源优先级

### 当前主链

- **SEC official connector**：美国公司 actuals、filing、附件和 Item 的 canonical source。`findata-analyst` 很重要，
  但它是 edgartools/SEC 的检索与解析 adapter，不是另一份独立数据源。它的 `financials`、`smart-facts`、
  `attachments`、`read-item`、`search-text` 应映射到 SEC authority；`ratios` 属于 derived output，不能冒充原始证据。
- **AlphaEngine**：卖方研报、纪要和行业资料；用于竞争、需求、KPI 与分歧发现，关键数字仍回 filing / 公司披露。
- **Gemini web search + web fetch**：search 负责发现，fetch 保存原始公开页面。搜索摘要不能直接生成 Evidence。
- **Guidepoint**：渠道、采用、竞争、执行与监管等 practitioner 视角；按具体问题调用，不作为财务 actual 的来源。
- **财报电话会**：`roic-transcript` / 公司 IR transcript。当前 inventory 尚未把它独立成 live connector，应在
  Guidepoint 之后、社区源之前接入。
- **company-wiki / 内部文档**：适合 context、历史判断和研究连续性；若原文有正式来源，保留原始 source ref；
  内部摘要本身不升级为外部事实权威。

### 建模前必须补的缺口

- **市场与估值数据**：当前十类 inventory 没有稳定的 US price、shares outstanding、FX、rates 和 consensus
  connector。`findata-ciqticker` 只做 CIQ ticker 映射，不是行情源。没有这条 authority 前，不生成正式估值输出。
- **CN/HK 数据**：CNINFO 继续作为 A 股官方公告主链；HKEX official filing connector 仍缺。`cn-hk-findata`
  适合行情、资金流、预期与辅助检索，但底层包含东财、腾讯、新浪、同花顺、雪球等不同口径，必须保留 vendor、
  fallback 和 degraded 状态，不能替代公司公告。

### 后置来源

- X/xreach、X/x_search、Reddit/last30days、雪球用于人物动态、争议和社区情绪；
- 这些内容能提出问题或补充市场观点，不能直接写财务 actual、部署量、订单或估值输入。

## Model Input Ledger v1 最小边界

模型地基只覆盖研究必须写入的正式对象：

- actual observation：metric、公司/业务线、period、calendar、unit、currency、value、Evidence/Claim source；
- assumption version：driver、数值/公式、有效期、scenario、owner、rationale、来源或 `judgment` 标签；
- forecast line version：历史值、预测期、公式/依赖、scenario 和 prior version；
- model run version：冻结的 input refs、formula/version hash、输出、错误和运行时间；
- reconciliation：报表勾稽、单位/币种、期间、share count、actual 覆盖 forecast 和 source revision；
- valuation output：只有 price/shares/FX/rates/consensus authority 到位后才能进入正式输出。

本阶段不做任意单元格执行、VBA、复杂循环引用、自动改 thesis 或自动覆盖人工 assumption。研究先写 candidate，
通过校验和人工 gate 后再进入正式模型版本。

## 当前切片验证

- Connector rate policy 已支持 IANA timezone 的当地日历日窗口；非 UTC 只允许 86,400 秒日窗口；
- 北京时间 00:00 前额度耗尽会阻断，UTC 16:00 整点进入新窗口后可重新预留；
- AlphaEngine live 测试 profile 已绑定 50 次 search 和 80 份 document 的日限额；`get_document` 首页记 1、续页记 0；
- Gemini 1,000 次日限额已进入同一治理清单，等待 bridge 使用；
- AlphaEngine document page 现在校验 document id、content hash、offset、returned chars、next offset 和 terminal length，
  非连续分页 fail closed。
- development candidate 已新增 bounded multi-page coordinator：逐页回查 Connector/Artifact authority 和 exact raw
  JSON-RPC，命中页数/总响应字节/文档字符上限只形成 partial；只有连续终页的整文长度和 SHA-256 一致才 complete。
- page request 具有稳定 identity；crash/replay 复用已完成的 page，不再次调用上游。

本切片没有部署、没有调用 AlphaEngine/Gemini、没有写 live Core，也没有实现 Model Input Ledger。分页 coordinator
已经完成本地 authority/replay 验证，但 production ResearchPlan/Scheduler 接线和真实完整文档 canary 仍须在部署 gate
单独验收。下一开发切片是 Gemini web search discovery 与独立 web fetch。
