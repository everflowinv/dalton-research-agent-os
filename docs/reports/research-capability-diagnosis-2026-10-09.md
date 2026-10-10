# 研究能力诊断（2026-10-09）：为什么框架有了、研究做不出价值

> owner 10-09 的判断：「框架有了，但研究能力不行，这个应该不是模型能力的问题」；「材料缺少的时候，分析师应该主动去找；不仅写报告，搭模型、回答问题时也应该能找，至少 web search 总能做到」。本文是两份只读诊断（Opus 子代理，代码 + legacy 线上数据）的汇总，基准是人工分析师的 `STRL_Initial Screen_20260612.docx`。数据与代码路径见文末。

## 一句话结论

不是模型能力问题。三层管线把分析师的手脚都绑住了：**材料层**——所有起草环节只能读从文档抽出的「单句陈述（Claim）」，合约规定 Claim 不许带数字，文档原文从不进入任何提示词，ACN 约 390 份文档、510 万字符的原文，初筛起草者只看到 120 句无数字陈述（约 0.5%）；**合约层**——提示词明令「不写目标价、前瞻倍数、trade call」，数字只能逐字抄自 4 个标签，材料不够就写「缺什么」，上一版未决问题必须原样保留；**闭环层**——产物写下的「材料缺少」是自由文本，没有任何消费者，初筛过门后不再起草，事件判断的「去核实」绑定不到检索模板（ACN 84 条停在 `no_bindable_template`）。

## 与 STRL 的差距

| STRL 有的 | 我们的 ACN 初筛 | 原因 |
|---|---|---|
| 明确观点（consensus 明显偏低） | 「现有证据更支持…仍待验证」 | 合约禁止观点；Claim 无数字 |
| 自有预测 vs consensus（27E EPS $32，高 30–40%） | 无 | 初筛不读 consensus（61 版已入库）；模型无 EPS 行；72 个假设全是机械外推 |
| 估值与观察价（20x 27E P/E → $650 vs 现价 $800） | S6 为空 | 合约禁目标价；起草时估值快照尚未生成，之后不重写 |
| 业务经济性（占 capex 1–2%、OPM ~30%）、竞争格局、带数字的公司历史 | 无数字 | 抽取合约「Never write a number」；一份 5 万字纪要只产出 20–25 句 |
| 交易角度（隐含波动、momentum、回撤） | 无 | 合约「This is analysis, not a trade call」；无 ADV/52 周/做空数据 |
| 「我们不知道什么」 | 28 处「缺少/待验证」 | 唯一允许的输出，被放大成主旋律 |

## 逐环节断点

| 环节 | 实际输入 | 遇到缺口时 | 断点 |
|---|---|---|---|
| 初筛 | 最新 120 条 Claim（按时间不按相关性）+ 4 个数 | 写「现有材料缺少」 | gap 无消费者；gate_passed 后只有人批 reopen 才重写，reopen 只看清单项翻转 |
| 档案 | 每单元 40 条 Claim | repair_targets 进规划器 | 绑定时返回 None 变 capability_gap；Claim 旧到新排序、预测格取最远季度（bug） |
| 争议图 | 40 条 Claim | 无 | 4 条争议 our_position 全是 none_yet |
| 事件判断（ACN Q4 8-K） | 收入表 11 个数 + 2 论点 + 12 条 Claim，无任何正文 | action=research | 无同事件聚合（8-K 附件 99.1、Q4 纪要、BofA sales note、GS 预览都在库里）；有判断就不重判；research 无可绑定模板 |
| 备忘录 | 72KB，consensus 一行且不能进正文 | 写进正文后被拒 | 标题要求「30 日 ADV + 建议」与材料/禁令冲突，ACN 7 次全被拒 |
| 模型规格/驱动 | — | 只报 series_gaps，未知值一律 trailing carry-forward | 不找指引、consensus、KPI；方法词表只有 4 种；小计规则断链；schema 只允许利润表 |
| 情景 | — | 无 | 无目标价/EPS/估值；conviction 读的 `scenario_version` 模块不存在 |
| 问答 | 公司 + 最新 271 条 Claim + 12 事件（无标题）+ 我们自己的判断，预算 90K 用满 | refresh 一次只补标题 | 无主题检索；FTS5 全文索引写了从未部署；事件 payload 渲染丢失 |
| 行业框架 | — | 单元缺材料不起草 | 行业 inquiry 被拒 |
| 抽取 | — | — | ACN 556 条 awaiting_human_extraction |

## 四个产品逻辑问题

1. **总览两条矛盾论点**：同一论点 v1（08-27，已作废）与 v2（09-14，当前）并列；总览页没按 `current` 过滤，「最新判断」在全是 NO_CHANGE 时回退取 theses[0]=作废版（已修，批次 ai）。模型层：v2 是 v1 反命题却只标 WEAKENED；仅凭 ACN 一家证据改行业论点，EPAM/DXC/IBM 的修订被拒；无「反向观点/分歧」实体。
2. **「模型预期总回报 对比期间」**：= 未来四季模型净利润 ÷ 过去四季 + 股息率，倍数固定，窗口是预测期不是今天起；5 家 4 家因无净利润用代理口径。情景无目标价；conviction 情景回报由模型自填。已有未用：一致预期目标价 223.65（24 家，高 275 低 163）、FY27 EPS、卖方目标价、3 年日线与滚动 P/E 分位。
3. **模型行数少**：历史几乎全满；预测方法词表窄、已申报小计不可预测而结构写了「营业利润 = 收入 − 经营费用合计」、税前利润直接 unavailable、schema 只允许利润表、假设生成器跳过符号不一致的行。
4. **投资观点节**：当前观点 → 基准情景 → 700 字 NO_CHANGE 草稿 → 摘要 → 反馈 → 混有当前论点的历史折叠 → 过时状态行 → 无名关键数字 → 图表。

## 附带发现的独立缺陷

consensus 季度标签错位（标 FY26Q4 的行实为 FY27Q1，因 last_reported 停在 2026-05-31）；事件判断运行目录不存 prompt；SEC 8-K 发现晚 7 天；初筛 8 节各自调用共用同一上下文致事实重复。

## 重构方案（批次）

- **A 证据底座**：部署并扩展 `document_index.py` FTS5，覆盖 connector/transcript spool 全部正文（8-K 附件、10-K/Q、纪要、研报、sales notes、Guidepoint、web），约 1.5K 字符分段带元数据，获取即入索引；新引用类型 `doc-passage:<document_ref>:<offset>:<hash>`，数字校验改为「逐字出现在所引段落」；抽取合约允许带数字，awaiting_human_extraction 不再在关键路径。
- **B 数字接线**：consensus 渲染器（修财期映射）；street_estimates 抽券商 EPS/收入表；8-K 展望段解析（bookings、book-to-bill、本币增速、指引）；模型加 EPS 行、驱动优先级 指引 > consensus > 分析判断 > trailing、「我们 vs consensus」差异表；前瞻 P/E、总债务与 EV、同业倍数；市场数据（ADV、52 周、YTD、相对 basket）。
- **C 共享材料包** `material_pack.build(subject, product, as_of)`：初筛配方 150–250K 字符（最近 4 季纪要、近 6 个月 top-N 研报多空两派的摘要/估值段/预测表、近 90 天 sales notes、consensus/street/我们的模型与差异表、行情与估值、10-K 业务段、Guidepoint、我们的历史产品标注为「我们的看法」）。
- **D 产品合约重写（仿 STRL）**：结论与观点（立场 + 观察价）/ 商业模式与经济性 / 行业结构与竞争 / 盈利预测 vs consensus / 估值与观察价 / 交易角度 / 风险与证伪 / 跟踪指标 / 附录我们不知道什么。必须表态、必须给数字；允许三类数字（逐字引用、声明算式确定性复核、标注「我们的估计」挂模型假设）；删除禁目标价/倍数/trade call 与「未决问题必须保留」；「材料缺少」必须附已用尽的 MaterialRequest；两步起草（结构化底稿 → 整篇）；verifier 改为核对引用与算式并检查观点/预测差/估值在场。
- **E 材料获取服务（跨所有环节）**：MaterialRequest{requester, subject, need, period, source_priority, acceptance, budget, deadline} → 来源阶梯（已入库主题检索 → sales notes → AlphaEngine → SEC 含附件/XBRL → Guidepoint → web）→ MaterialResult{status, items, attempts} → 回调触发者重起草/重判/重算/续答；digest 幂等、负缓存、`material_acquisition` 预算池；规划器 inquiries、档案 repair_targets、research_task 的 no_bindable_template 全改走本服务；owner door 只剩来源用尽/预算超限/需凭证；起草前材料充分性检查；gate_passed 不再终态，必需材料版本变化即自动 reopen。
- **F 事件判断**：同事件包（±3 天 8-K 附件/纪要/券商快评/sales notes/新闻）；预期基准（会前 consensus、券商预览、上次指引）；合约「实际 vs 预期表 → 市场反应 → 论点影响」；+72 小时内新文档入库即重判；文档事件必须带正文段。
- **G 问答**：相对日期解析、why/move 类型；公司 + 问题词 + 日期窗口跨文档类型 FTS + rerank；我们的判断降权；事件 payload 修复；回答结构「发生了什么（一手）→ 市场预期 → 我们的看法 → 不确定点（已尝试）」；材料不足发 MaterialRequest 续答。
- **模型（来自第二份诊断）**：≥8 季历史的行必须有方法；新增 carry_forward_mean / zero_one_off；小计自动生成组成项求和推导行；税前利润强制公式；开放 cash/balance；表上每行标方法。
- **风险收益**：`scenario_valuation`（情景 EPS × 历史 P/E 分位 P20/P50/P80 → 目标价 → 12 个月回报，概率加权与上下比），比较表与 conviction 改读它；实现缺失的 `scenario_version.conviction_scenarios`。
- **公司页首屏** `house_view`：本所观点 / 三条理由 / 风险收益 / 下一个催化剂 / 什么会改变观点；其余折叠。
- **论点建模**：question_ref + stance（house/counter）；同一问题只能一个 house 版本 current；方向相反必须 BROKEN 或分歧记录；修订必须给置信度并与正文一致；行业论点修订必须引用最新行业框架。
- **H 验收**：确定性 rubric（立场 + 观察价；≥15 个带出处量化陈述；我们 vs consensus 差异；估值六要素；≥4 种来源；引用最新纪要与发布稿；「材料缺少」≤2 处且挂 request；同一事实 ≤2 节）；黄金回放（ACN 10-01 事件必须引 bookings 222 亿、本币 +7% 超指引上沿、FY27 3–6%、EPS 14.39–14.81 并与 GS 预览对比；问答「ACN 昨天为什么大涨」必须解析 10-08 +5.96% 并覆盖 10-01 财报）；不同模型家族盲评五维度。

**建议顺序**：A → B → C → D（此时 ACN 初筛可见质变）→ 模型方法覆盖 → F → G → E 闭环 → 风险收益 → 公司页首屏 → 论点建模；H 贯穿。只改提示词不做 A/B 不会有实质变化。

## 证据路径

代码：`initial_screen.py` 38/44/66/99/421/499–524；`initial_screen_cli.py` 84–104/121–182/524–543；`document_extraction.py` 877/886–888；`document_research_qualitative.py` 96–99；`research_task.py` 789–870/985–1001；`event_judgement.py` 395–533/2754–2810；`event_judgement_cli.py` 319–347/401–445/543–620；`earnings_release_intake.py` 722–737；`ask_context.py` 58/737–741/1096–1098；`ask_refresh.py` 505–560；`document_index.py`；`mission_stage.py` 225–245/833/895；`company_dossier_cli.py` 346–351/518–520；`investment_memo_draft.py` 115–135；`return_attribution.py` 991–1067；`company_financial_statement_structure.py` 58/134/145–152/588–594；`model_forecast_driver.py` 850–860/1794–1797；`conviction_call_cli.py` 362–386；`thesis_revision.py` 171–196；`cockpit_v2/pages/overview.js` 70–74/123–129；`company_view.js` 57–124。
数据（legacy）：`initial-screens/97246c71ac96f8fecec2a041/summary.json`；`event-judgement-runs/b14cb6614e551bea96cbbe6b/`；`acquisitions/a520606071f4a87f825b9d8f/manifest.json`（Q4 纪要）；`feed-acquisitions-sales-notes/d5e3da79ca192239f1b14596/manifest.json`（BofA 10-01）；core.sqlite：claim_versions、consensus_estimate_versions、street_estimates、forecast_model_versions、valuation_snapshot_versions、gate_reopen_*、coverage_mission_document_reviews、event_judgements、thesis_versions、holding_period_return_bridges；cockpit/journal.sqlite `cockpit-job:49b8ad5585075d6ec2f79b3e`。

## owner 决定（2026-10-09）

1. 允许产品写观点、观察价和交易角度（反转「系统不持有目标价」规则；观察价作为合约字段留痕）。
2. 取消常规提取的人工门；人工只留给真正有歧义的情况。
3. 接受起草成本上升（材料包从约 2.5 万字符到 15–25 万字符）。
4. **重起草节奏按分析师工作流**：「收集一批信息再起草，之后累积了一些新信息再重新起草」。单条材料到达只登记为「待消化」（`material_arrivals`），重起草由阈值触发：必需材料版本变化（新季度纪要/发布稿）、consensus 变化超阈值、累计 ≥N 份新研报/sales notes、或距上次起草超过 X 天且有新材料；不得每取到一条就全文重写。

执行顺序（第一波并行）：A 证据底座、B 数字接线、M 模型覆盖；随后 C 材料包 + D 合约（含重起草阈值）；再 F 事件判断、G 问答、E 材料获取服务；之后风险收益、公司页首屏、论点建模；H 验收贯穿。另有并行修复：ai（总览作废论点）、aj（数字提取为零与纪要读不完）。
