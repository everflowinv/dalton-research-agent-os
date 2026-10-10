// GENERATED from src/dalton_core/terminology.py -- do not edit by hand.
// Regenerate: PYTHONPATH=src python -m dalton_core.terminology --write
// tests/test_terminology.py fails when this file and the registry differ.
//
// The analyst-facing vocabulary of the cockpit (batch am, 2026-10-09):
// canonical labels, the copy table copy() applies to server text, and the
// banned terms with their replacements.  Lives beside the chart assets so
// both /v2/static/charts/ and /v2/assets/charts/ can import it.

export const LABELS = Object.freeze({
 "thinking_level": {
  "off": "关闭",
  "minimal": "最少",
  "low": "低",
  "medium": "中",
  "high": "高"
 },
 "shell": {
  "product": "Dalton 研究工作台",
  "project": "当前研究项目",
  "service_starting": "研究服务正在启动",
  "service_starting_help": "正在整理已有研究记录，完成后继续任务。",
  "service_online": "研究服务正常",
  "service_online_since": "研究服务正常 · 最近一次运行 {ago}",
  "service_online_help": "后台在按计划搜集和分析资料；每项数据自己的截至时间写在数据旁。",
  "service_offline": "后台更新已暂停（已有研究可正常查看，新资料暂停录入）",
  "service_offline_help": "已有研究可以照常查看；新资料暂停录入{since}。请联系管理员恢复服务。",
  "service_degraded": "研究服务正常，但有 {n} 项后台任务异常（点击查看）",
  "service_degraded_plain": "研究服务正常，但有 {n} 项后台任务异常",
  "service_degraded_help": "后台仍在按计划运行（最近一次运行 {ago}）；异常的任务：{lanes}。已有研究可以照常查看。",
  "service_unknown": "研究服务状态暂时读不到",
  "service_awaiting_mission": "未设定研究目标（点击前往设定）",
  "service_awaiting_mission_help": "这个项目还没有研究目标；到「投研规则与设置」写下目标即可开始。",
  "blocked": "待审阅与决策：{n} 项",
  "blocked_none": "决策事项已全部处理",
  "blocked_unknown": "待办暂时读不到（不代表没有待办）",
  "badge": "{n} 项待处理",
  "updated": "工作台界面同步于 {at}",
  "refresh": "同步最新数据",
  "refreshing": "正在同步…",
  "refreshed": "已同步最新数据",
  "fresh": "检测到新的研究数据",
  "fresh_action": "同步查看",
  "close_panel": "关闭",
  "back_to": "返回{title}",
  "read_failed": "暂时无法更新，以下为上次结果",
  "no_view": "尚未建立正式投资观点",
  "cannot_compare": "口径暂不可比",
  "back_to_company": "返回公司研究",
  "try_new": "试用新版",
  "technical": "技术信息（供管理员）",
  "retry_later": "稍后点顶部的「同步最新数据」。"
 },
 "nav": {
  "overview": "研究总览",
  "companies": "公司研究",
  "ask": "智能投研问答",
  "tasks": "待办决策",
  "settings": "投研规则与设置"
 },
 "page_title": {
  "overview": "研究总览 · 投研动态看板",
  "ask": "智能投研问答",
  "tasks": "待办决策与事项中心",
  "settings": "投研规则与设置",
  "steer": "调整研究方向",
  "templates": "财务模型导出底稿模板",
  "model_export": "导出财务分析底稿（Excel）",
  "weekly_brief": "每周投研周报"
 },
 "task_group": {
  "decision": "投研决策与审批",
  "material": "资料补充与报告校准",
  "service": "技术与连接协助",
  "processing": "后台自动运行中（无需操作，仅供备查）"
 },
 "urgency": {
  "1": "P0 · 阻断核心研判推进",
  "2": "P1 · 影响模型深度与精度",
  "3": "P2 · 个别专题数据受限",
  "4": "P3 · 例行质量维护",
  "5": "提示 · 仅供业务知悉",
  "not_started": "待开始"
 },
 "recovery_door": {
  "reentry": "恢复专项课题研究",
  "paid": "扩大资料检索范围并重新获取",
  "unproved": "扩大资料检索范围并重新获取",
  "withdraw": "暂缓当前专项课题（保留现有底稿）",
  "reentry_done": "已恢复专项课题研究",
  "paid_done": "已扩大资料检索范围并重新获取",
  "withdraw_done": "已暂缓当前专项课题（现有底稿保留）",
  "cap_label": "本次检索预算上限（美元）",
  "cap_help": "填 0.01 到 20 之间的金额，例如 2.50；达到上限即停止，不会超出。",
  "retry_round": "在当前模型设置下再试一次",
  "retry_round_done": "已安排在当前模型设置下再试一次",
  "technical_help": "交技术协助"
 },
 "door_cost": {
  "free": "不会新增模型调用费用",
  "within_budget": "可能再做一次模型调用，费用在这项研究立项时已批准的上限之内，不新增预算",
  "paid": "会再做一次模型调用，费用上限由你填写（美元）"
 },
 "hold_reason": {
  "fresh_work_recovery_exhausted": "系统自动重试的次数已用完（每个研究步骤只自动重试固定次数）",
  "fresh_work_recovery_deadline_exceeded": "自动重试的时间窗口已过（研究步骤失败后只在限定时间内自动重试）",
  "fresh_work_recovery_day_window_exceeded": "当日预算恢复后的自动重试窗口已过",
  "fresh_work_recovery_disabled": "这个研究步骤没有设置自动重试",
  "all_models_unavailable": "当时所有可用的 AI 模型都暂不可用（额度用完、冷却中或授权过期），请求没有发出、没有计费",
  "contract_failed_after_automatic_retry": "模型回复连续两次不符合输出要求（已自动重试一次）",
  "contract_failed_after_owner_authorized_retry": "模型回复在自动重试和你批准的重试后仍不符合输出要求",
  "paid_send_output_contract_failed": "模型回复不符合输出要求（旧版本记录）",
  "unproved_send_failed_after_automatic_retry": "送达未确认，自动重试一次仍失败",
  "send_state_unproved": "送达状态无法确认，系统正在自行处理",
  "provider_budget_exceeded_not_retried": "供应商用量超出本次预算；系统会按实际用量自动修正上限重试一次",
  "provider_budget_exceeded_after_corrected_retry": "供应商用量超出预算；已按实际用量自动修正上限重试，仍然超出",
  "provider_budget_exceeded_after_corrected_retry_final": "供应商用量超出预算；自动修正上限的重试已全部用完，仍然超出",
  "provider_budget_exceeded_correction_refused": "供应商用量超出预算，且无法靠调高输入上限自动修正（输出/费用超限或所需上限过大）",
  "reentry_failed_after_automatic_rebind": "研究恢复失败（系统已自动更新执行凭证并重试一次）",
  "controlled_reentry_unavailable": "研究恢复暂时不可用",
  "controlled_reentry_ticket_unavailable": "找不到这项研究上次运行的记录，无法自动恢复",
  "recovery_hint_unverifiable": "这项研究的恢复记录无法核对，系统不敢自动继续",
  "started_without_owned_live_ticket": "这项研究已开始过，但找不到正在负责它的运行记录",
  "dispatch_error": "调度这项研究时出错（原文见技术信息）",
  "budget_or_pool_refused": "研究预算或额度池拒绝了这次调用",
  "not_sent_host_preflight_refused": "核验请求两次都在发送前被宿主预检拒绝（未发送、未计费；多为提示词超过准入输入上限）",
  "empty_reply_output_cap_exhausted": "模型把准入的输出上限全用在思考上、回复为空（已计费），重试一次仍然如此",
  "empty_or_invalid_reply": "模型回复为空或无效（多为推理模型把准入的输出上限耗尽），重试一次仍然如此",
  "provider_returned_nothing": "供应商什么也没返回（多为额度/限流），重试一次仍然如此",
  "automatic_unproved_send_retry_day_cap_reached": "今天的自动重试次数已用完，系统明天自动继续",
  "route_rejected_definitely_not_sent": "当时没有可用的 AI 模型，请求没有发出、没有计费",
  "adapter_proved_definitely_not_sent": "模型服务拒绝了请求，请求没有发出、没有计费",
  "proved_zero_cost_no_send": "请求没有发出，已核实没有计费",
  "atomic_day_budget_refusal": "当日研究预算已用完，请求被拒、没有计费",
  "atomic_pool_budget_refusal": "研究额度池已用完，请求被拒、没有计费",
  "automatic_round_used_today": "今天的自动重试已用过，系统明天再自动试",
  "awaiting_model_availability": "系统会在有模型恢复可用后自动再试",
  "withdrawn_by_owner": "已由你暂缓",
  "withdrawn_after_deadline": "恢复期限已过，已自动结束",
  "no_unstarted_admission": "没有尚未启动的原文专题研究任务",
  "other": "其他原因：{code}"
 },
 "document_review_state": {
  "awaiting_human_extraction": "等待自动读取与提取"
 },
 "ack": {
  "noted": "已阅知悉",
  "snooze": "静音该提示",
  "snooze_days": "静音该提示 {n} 天",
  "abandon_document": "忽略该缺失资料（采用现有数据推导）",
  "noted_all": "全部已阅知悉",
  "approve_spend": "批准追加获取额度"
 },
 "decision_button": {
  "decision": "审阅并确认",
  "material": "补充参考材料",
  "service": "提交技术协助",
  "processing": "查看",
  "approve_entry": "审阅并确认",
  "handoff": "生成技术交办单",
  "handoff_copy": "一键复制故障信息供 IT 协助",
  "all_tasks": "进入待办决策中心处理全部 {n} 项事项 →",
  "view_change": "查看异动详情 →",
  "gate_approve": "核准通过",
  "gate_return": "退回补充资料",
  "gate_reject": "否决（终止本轮）",
  "suggest": "建议操作"
 },
 "comparison": {
  "title": "覆盖标的横向比较矩阵",
  "company": "标的名称 / 代码",
  "view": "投资观点与评级状态",
  "expected_return": "12M 预期回报率",
  "month_change": "近 30 个交易日股价变化",
  "multiple": "关键估值倍数",
  "bear": "下行风险情景（发生概率 / 潜在跌幅）",
  "catalyst": "下一关键催化事件",
  "tasks": "待办决策",
  "not_comparable": "口径暂不可比（预测周期不一致）",
  "model_pending": "模型预测待完善",
  "rule": "排序基准：按未来 12 个月基准预期回报率降序排列；统计窗口不一致的标的不计入排名",
  "bear_probability": "发生概率 {p}",
  "bear_drawdown": "潜在跌幅 {d}",
  "bear_drawdown_pending": "潜在跌幅待测算"
 },
 "company_section": {
  "view": "核心投资观点与论证",
  "valuation": "估值分析与目标价测算",
  "risks": "下行风险与关键催化剂",
  "evidence": "事实底稿与引用证据库",
  "model": "财务预测模型（三大表）",
  "events": "重大事件与研判跟踪",
  "scenarios": "情景分析与概率配置",
  "returns": "业绩拆解与回报归因",
  "reports": "投研报告与投资备忘录"
 },
 "report_kind": {
  "initial_screen": "标的初步初筛与立项评估报告",
  "investment_memo": "投资决策备忘录（IC Memo）",
  "deep_dive": "深度基本面研报",
  "dossier": "公司档案",
  "debate_map": "多空核心分歧议题",
  "industry_framework": "行业框架",
  "conviction_call": "投资判断",
  "weekly_brief": "每周投研周报"
 },
 "report_state": {
  "draft": "初稿（待审核签发）",
  "final": "正式研报（已签署归档）",
  "export": "导出公司研究全景报告（可在浏览器中打印为 PDF）",
  "export_note": "导出为网页格式的完整报告；在浏览器中选择「打印 → 存储为 PDF」即可得到 PDF。",
  "reader": "研报阅读器：{title}"
 },
 "research_source": {
  "source:sec-edgar": "SEC 财报数据",
  "source:alphaengine": "卖方研报与电话会",
  "source:company-ir": "公司官网投资者关系页面",
  "source:guidepoint": "专家访谈",
  "source:web-search": "公开网页搜索",
  "source:public-web": "公开网页",
  "source:sales-notes": "卖方销售快报",
  "source:company-wiki": "公司知识库",
  "source:prior-research": "历史研究资料",
  "source:yahoo-finance": "Yahoo Finance 行情",
  "source:xueqiu": "雪球散户讨论",
  "source:x": "X（推特）公开讨论",
  "source:blind": "匿名员工评价（Blind）"
 },
 "source_kind": {
  "filing": "SEC 法定申报",
  "management_statement": "电话会纪要（管理层表述）",
  "sell_side": "券商研报",
  "sec": "SEC 法定申报",
  "sec_filing": "SEC 法定申报",
  "10-K": "SEC 法定申报",
  "10-Q": "SEC 法定申报",
  "8-K": "SEC 法定申报",
  "transcript": "电话会纪要",
  "earnings_call": "电话会纪要",
  "earnings-call-transcript": "电话会纪要",
  "broker": "券商研报",
  "broker_research": "券商研报",
  "alphaengine": "券商研报",
  "expert": "专家访谈",
  "guidepoint": "专家访谈",
  "public_web": "公开网页资料",
  "news": "新闻",
  "other": "其他资料",
  "as_reported": "会计科目申报原名（As-Reported）",
  "as_reported_amount": "申报金额"
 },
 "stage": {
  "industry_framework": "行业框架",
  "initial_screen": "立项初筛",
  "deep_insight_gate": "深度认知审核",
  "industry_model": "行业模型",
  "company_model": "深度模型构建",
  "forecast_lines": "盈利预测",
  "investment_memo": "投资决策备忘录",
  "weekly_brief": "每周投研周报",
  "continuous_coverage": "持续覆盖",
  "active_coverage": "持续覆盖"
 },
 "scenario": {
  "bull": "乐观情景",
  "base": "基准情景",
  "bear": "悲观情景",
  "trigger": "触发信号",
  "invalidator": "预警触发线",
  "invalidated": "核心假设证伪预警触发",
  "owner_observed": "需人工核实确认",
  "rebalance": "将剩余差额一键平衡至「基准情景」",
  "deactivate": "移出本轮推演",
  "deactivate_help": "移出后该情景暂停权重加权与触发监控，历史底稿仍保留归档。",
  "edit": "自定义配置情景权重",
  "save": "应用并保存情景权重配置",
  "discard": "还原初始配置"
 },
 "attribution": {
  "title": "过去 12 个月股价总回报归因（TSR Attribution）",
  "factor": "回报驱动因子",
  "contribution": "贡献度（百分点）",
  "unit_note": "单位：百分点（pp）；柱顶数值均为对总回报的贡献。",
  "revenue": "主营业务收入增长驱动",
  "margin": "净利润率扩张 / 收缩效应",
  "shares": "股份回购与股本稀释影响",
  "multiple": "市盈率倍数重估",
  "dividend": "股息贡献",
  "residual": "其他与交叉项",
  "bridge": "营业利润同比变动桥接分析"
 },
 "attribution_component": {
  "earnings_growth": "每股收益（EPS）增长贡献",
  "share_count": "股份回购与股本稀释影响",
  "multiple_change": "市盈率倍数重估",
  "dividends": "股息贡献"
 },
 "statement_line": {
  "Revenues": "营业收入",
  "RevenueFromContractWithCustomerExcludingAssessedTax": "主营业务收入",
  "CostOfRevenue": "营业成本",
  "CostOfGoodsAndServicesSold": "营业成本",
  "GrossProfit": "毛利润",
  "OperatingIncomeLoss": "营业利润",
  "OperatingExpenses": "营业费用",
  "SellingGeneralAndAdministrativeExpense": "销售、一般及管理费用",
  "ResearchAndDevelopmentExpense": "研发费用",
  "NetIncomeLoss": "净利润",
  "EarningsPerShareDiluted": "稀释每股收益",
  "EarningsPerShareBasic": "基本每股收益",
  "NetCashProvidedByUsedInOperatingActivities": "经营活动现金流净额",
  "PaymentsToAcquirePropertyPlantAndEquipment": "资本开支",
  "IncomeTaxExpenseBenefit": "所得税费用",
  "DepreciationDepletionAndAmortization": "折旧与摊销",
  "WeightedAverageNumberOfDilutedSharesOutstanding": "稀释加权平均股数",
  "CashAndCashEquivalentsAtCarryingValue": "现金及现金等价物",
  "LongTermDebt": "长期债务",
  "StockholdersEquity": "股东权益"
 },
 "metric": {
  "core_pe": "核心经营税后市盈率（不含融资）",
  "trailing_pe": "市盈率（过去 12 个月，倍）",
  "forward_pe": "预期市盈率（倍）",
  "price_to_sales": "市销率（倍）",
  "ev_to_ebitda": "EV/EBITDA（倍）",
  "ev_to_sales": "EV/销售额（倍）",
  "fcf_yield": "自由现金流收益率（%）",
  "dividend_yield": "股息率（%）",
  "price_to_book": "市净率（倍）",
  "forward_price_to_sales": "预期市销率（倍）",
  "forward_ev_to_ebitda": "预期 EV/EBITDA（倍）",
  "ev_to_ebit": "EV/EBIT（倍）",
  "ev_to_fcf": "EV/自由现金流（倍）",
  "price_to_fcf": "股价/自由现金流（倍）",
  "peg_ratio": "PEG（倍）",
  "earnings_yield": "盈利收益率（%）",
  "dividend_payout_ratio": "股息支付率（%）",
  "shareholder_yield": "股东回报率（%）",
  "net_debt_to_ebitda": "净负债/EBITDA（倍）",
  "market_cap": "市值",
  "enterprise_value": "企业价值",
  "roe": "净资产收益率（%）",
  "roic": "投入资本回报率（%）",
  "gross_margin": "毛利率（%）",
  "operating_margin": "营业利润率（%）",
  "net_margin": "净利率（%）",
  "fcf_margin": "自由现金流利润率（%）",
  "revenue_growth": "收入增速（%）",
  "eps_growth": "每股收益增速（%）"
 },
 "glossary": {
  "EPS": "每股收益",
  "ROIC": "投入资本回报率",
  "CapEx": "资本开支",
  "10-K": "美国上市公司年度报告",
  "10-Q": "美国上市公司季度报告",
  "8-K": "美国上市公司重大事项临时报告",
  "EBITDA": "息税折旧摊销前利润",
  "EBIT": "息税前利润",
  "GAAP": "美国通用会计准则",
  "FCF": "自由现金流",
  "P/E": "市盈率",
  "TTM": "过去 12 个月合计"
 },
 "term_gloss": {
  "口径": "统计与会计度量规则：包括统计报告期（TTM / FY / FQ）、是否计入现金分红再投资、会计准则选择（GAAP 或 Non-GAAP 经调口径）等。仅同口径指标具备横向可比性。",
  "不可比较": "暂不参与横向排名：财务报表截止日不同（如非常规财年）或核心预测口径不一致时，强制排名会造成误导；具体原因见单元格内标注。",
  "口径暂不可比": "暂不参与横向排名：财务报表截止日不同（如非常规财年）或核心预测口径不一致时，强制排名会造成误导；具体原因见单元格内标注。",
  "历史分位": "当前值在过去一段时间里的位置：0% 是这段时间的最低，100% 是最高。样本天数写在说明里。",
  "基点": "基点（bps）：利率、收益率及利润率变动的度量单位。1 bps = 0.01 个百分点；100 bps = 1 个百分点。",
  "简化估算": "粗算估算（Simplified Projection）：按所列简化假设作方向性测算；该方法不采用完整三大表或 DCF 估值。",
  "粗算估算": "粗算估算（Simplified Projection）：按所列简化假设作方向性测算；该方法不采用完整三大表或 DCF 估值。",
  "悲观情景概率": "主观情景权重：研究员为悲观经营情景赋予的发生概率；它不是风险评分，也不是下行损失幅度，需结合潜在跌幅一起判断风险。",
  "下行风险情景": "悲观情景的发生概率与潜在跌幅：概率是研究员赋予的主观权重，跌幅是悲观情景目标价相对现价的变化；两者一起读，不是风险评分。",
  "百分点": "两个百分比之间的差，例如 10% 到 12% 是增加 2 个百分点（pp）。"
 },
 "section_title": {
  "business_model": "商业模式与盈利来源",
  "segments_and_mix": "业务构成",
  "demand_drivers": "需求驱动因素",
  "supply_and_cost": "供给与成本",
  "competitive_position": "竞争地位",
  "management_and_capital_allocation": "管理层与资本配置",
  "guidance_style": "业绩指引风格",
  "kpi_dictionary": "关键经营指标",
  "catalyst_calendar": "催化剂日历",
  "history_of_price_drivers": "历史股价驱动因素",
  "industry_classification": "行业归类",
  "variant_view": "与市场不同的观点"
 },
 "debate_role": {
  "多方": "看多观点",
  "空方": "看空观点",
  "市场看法": "市场观点",
  "我们的判断": "我们的判断",
  "证据": "证据",
  "验证条件": "验证条件"
 },
 "event_kind": {
  "price_move": "股价异动",
  "price_divergence": "股价与我们的判断持续背离",
  "news": "新闻",
  "filing": "公司报表",
  "transcript": "电话会纪要",
  "rating_change": "评级变化",
  "calendar": "日程",
  "reconciliation": "预测与实际对账",
  "claim": "新结论",
  "sales_note": "卖方销售简报",
  "crowd_post": "散户与市场讨论",
  "expert_excerpt": "专家访谈摘录",
  "insider_transaction": "董事与高管的买卖",
  "insider_trading_plan": "董事与高管的交易计划变更",
  "ownership_change": "大股东持股变化",
  "holdings_change": "机构持仓变化",
  "buyback_disclosure": "公司回购",
  "scenario_trigger": "情景触发条件命中",
  "earnings_release": "公司业绩稿（初步）"
 },
 "judgement_decision": {
  "NO_CHANGE": "维持基准判断",
  "THESIS_STRENGTHENED": "核心论点增强",
  "THESIS_WEAKENED": "核心论点受损",
  "THESIS_BROKEN": "核心论点被推翻",
  "NEW_THESIS": "形成新的投资论点"
 },
 "judgement_action": {
  "no_change": "不调整基准论点",
  "note": "记录研究说明",
  "research": "启动专项研究",
  "revise_forecast": "修订预测",
  "revise_thesis": "提出论点修订候选",
  "revise_dossier": "修订公司档案"
 },
 "policy": {
  "title": "投资建议准入与纪律规则",
  "default": "机构默认投研纪律标准",
  "default_version": "机构默认投研纪律标准（v{n}）",
  "hurdle": "最低预期回报率（按所选投资期限，%）",
  "upside_downside": "最低收益风险比（Upside/Downside）",
  "confidence": "底稿证据充分度准入门槛",
  "compare": "历代投研准入纪律版本对比"
 },
 "valuation_input": {
  "core_tax_expense": "明确披露的经营所得税额",
  "net_income": "净利润",
  "revenue": "营业收入",
  "operating_income": "营业利润",
  "depreciation_amortisation": "折旧与摊销",
  "operating_cash_flow": "经营活动现金流净额",
  "capital_expenditure": "资本开支",
  "total_debt": "有息债务合计",
  "cash_and_equivalents": "现金及现金等价物"
 },
 "valuation_reason": {
  "service_unconfigured": "本机尚未启用估值快照更新（由管理员在后台服务中开启）",
  "no_filing_yet": "截至 {as_of} 还没有任何已申报的财务数据（最早的申报数据日期为 {earliest}）",
  "missing_lines": "已获取的申报里缺少{lines}，无法计算",
  "not_positive": "过去 12 个月{line}不为正，{metric}没有意义",
  "percentile_sample": "该指标只有 {n} 个交易日的历史，计算历史分位至少需要 {min} 个",
  "enterprise_value_missing": "已获取的申报里缺少{lines}，暂无法得出企业价值"
 },
 "material_source": {
  "indexed_corpus": "已入库资料检索",
  "sales_notes": "卖方销售快报",
  "alphaengine": "AlphaEngine（电话会纪要与券商研报）",
  "sec_edgar": "SEC 申报（含附件与 XBRL 财务数据）",
  "guidepoint": "Guidepoint 专家访谈",
  "web": "公开网页检索",
  "consensus_vendor": "一致预期数据",
  "forecast_model_lane": "我们的预测模型",
  "market_price_lane": "行情数据"
 },
 "material_outcome": {
  "accepted": "已找到",
  "partial": "部分找到",
  "empty": "没有找到",
  "dispatched": "已发起检索，等待结果",
  "health_hold": "来源登录失效，暂缓",
  "rate_limited": "来源调用额度已满，稍后再试",
  "budget_blocked": "超出材料获取额度",
  "needs_credentials": "需要登录或授权",
  "not_connected": "本环境未接入这个来源",
  "not_applicable": "不适用于这类材料",
  "error": "检索出错",
  "waiting_on_lane": "等待对应数据更新",
  "unsupported": "无法识别的来源"
 },
 "material_need": {
  "document": "文档",
  "data": "数据项",
  "model": "模型数据",
  "answer": "研究问题"
 }
});

export const COPY_TABLE_DATA = Object.freeze([
 {
  "current": "研究环境",
  "suggested": "研究项目",
  "note": "部署隔离细节交给系统",
  "mode": "replace",
  "rules": [
   [
    "研究环境",
    "研究项目"
   ]
  ]
 },
 {
  "current": "每家公司的子任务",
  "suggested": "公司研究进展",
  "note": "首页默认更应展示公司结论",
  "mode": "replace",
  "rules": [
   [
    "每家公司的子任务",
    "公司研究进展"
   ]
  ]
 },
 {
  "current": "产物 / 交付物",
  "suggested": "研究报告 / 已完成研究",
  "note": "按对象命名",
  "mode": "replace",
  "rules": [
   [
    "研究产物",
    "研究报告"
   ],
   [
    "交付物",
    "已完成研究"
   ],
   [
    "产物",
    "研究报告"
   ]
  ]
 },
 {
  "current": "研究车道、研究线",
  "suggested": "研究流程、专项研究",
  "note": "用户不必学习调度架构",
  "mode": "replace",
  "rules": [
   [
    "研究车道",
    "研究流程"
   ],
   [
    "车道",
    "流程"
   ],
   [
    "这条研究线",
    "这项专项研究"
   ],
   [
    "研究线",
    "专项研究"
   ]
  ]
 },
 {
  "current": "资料底座齐了",
  "suggested": "所需资料已齐",
  "note": "再区分已获取、已阅读",
  "mode": "replace",
  "rules": [
   [
    "资料底座齐了",
    "所需资料已齐"
   ],
   [
    "资料底座",
    "所需资料"
   ]
  ]
 },
 {
  "current": "等着起草初步研究筛查报告",
  "suggested": "正在准备初步研究报告",
  "note": "只有真正执行时才用“正在”",
  "mode": "replace",
  "rules": [
   [
    "等着起草初步研究筛查报告",
    "正在准备初步研究报告"
   ]
  ]
 },
 {
  "current": "已进入，等待审批",
  "suggested": "等待确认初步研究结果",
  "note": "必须说清确认什么",
  "mode": "replace",
  "rules": [
   [
    "已进入，等待审批",
    "等待确认初步研究结果"
   ]
  ]
 },
 {
  "current": "历史检查已通过",
  "suggested": "上次评审通过；当前资料有缺口",
  "note": "日期和当前状态分开",
  "mode": "replace",
  "rules": [
   [
    "历史检查已通过",
    "上次评审通过"
   ]
  ]
 },
 {
  "current": "入库的结论（含重复版本）",
  "suggested": "已整理的研究结论",
  "note": "默认显示去重数，历史另看",
  "mode": "replace",
  "rules": [
   [
    "入库的结论（含重复版本）",
    "已整理的研究结论"
   ]
  ]
 },
 {
  "current": "没有基线",
  "suggested": "尚未设置更新频率",
  "note": "不把缺配置误说成缺资料",
  "mode": "replace",
  "rules": [
   [
    "没有基线",
    "尚未设置更新频率"
   ]
  ]
 },
 {
  "current": "本机连接器目录已登记",
  "suggested": "服务已配置",
  "note": "是否可用要另由连接检查决定",
  "mode": "replace",
  "rules": [
   [
    "本机连接器目录已登记",
    "服务已配置"
   ]
  ]
 },
 {
  "current": "研究目标尚未声明该来源",
  "suggested": "本项目尚未启用",
  "note": "提供真正启用入口",
  "mode": "replace",
  "rules": [
   [
    "研究目标尚未声明该来源",
    "本项目尚未启用"
   ]
  ]
 },
 {
  "current": "模型走哪条链 / 链尾",
  "suggested": "AI 服务选择顺序 / 添加备用服务",
  "note": "放管理员设置",
  "mode": "replace",
  "rules": [
   [
    "模型走哪条链",
    "AI 服务选择顺序"
   ],
   [
    {
     "re": "^链尾$",
     "flags": ""
    },
    "添加备用服务"
   ]
  ]
 },
 {
  "current": "手动指定模型 / 输出上限",
  "suggested": "高级 AI 设置",
  "note": "分析师默认不用看到",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "^手动指定模型$",
     "flags": ""
    },
    "高级 AI 设置"
   ],
   [
    {
     "re": "^输出上限$",
     "flags": ""
    },
    "高级 AI 设置"
   ]
  ]
 },
 {
  "current": "代码默认（尚未发布任何版本）",
  "suggested": "使用系统默认规则",
  "note": "版本记录二级展开",
  "mode": "replace",
  "rules": [
   [
    "代码默认（尚未发布任何版本）",
    "使用系统默认规则"
   ],
   [
    {
     "re": "^代码默认$",
     "flags": ""
    },
    "使用系统默认规则"
   ]
  ]
 },
 {
  "current": "Conviction v2",
  "suggested": "投资建议规则",
  "note": "保留真实证据/回报要求",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "Conviction v2",
     "flags": "g"
    },
    "投资建议规则"
   ]
  ]
 },
 {
  "current": "本行为收入（Revenues）科目。",
  "suggested": "营业收入",
  "note": "行名不能是解释句",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "本行为收入（[^）\\n]*）科目。",
     "flags": "g"
    },
    "营业收入"
   ],
   [
    {
     "re": "本行为([^（\\n]+?)（[^）\\n]*）科目。",
     "flags": "g"
    },
    "$1"
   ]
  ]
 },
 {
  "current": "少数股东损益 / 可赎回少数股东损益",
  "suggested": "保留准确科目",
  "note": "不因字长误删或混合",
  "mode": "keep",
  "rules": []
 },
 {
  "current": "否定：某条件",
  "suggested": "情景失效条件：某条件",
  "note": "更贴近分析工作",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "^否定：",
     "flags": ""
    },
    "情景失效条件："
   ]
  ]
 },
 {
  "current": "这一条由你判断，程序不读",
  "suggested": "需人工确认",
  "note": "必须附确认动作",
  "mode": "replace",
  "rules": [
   [
    "这一条由你判断，程序不读",
    "需人工确认"
   ],
   [
    "（由你判断，",
    "（需人工确认，"
   ],
   [
    {
     "re": "^由你观察$",
     "flags": ""
    },
    "需人工确认"
   ]
  ]
 },
 {
  "current": "等你在待办里裁决",
  "suggested": "请确认情景假设",
  "note": "同步真实导航名称",
  "mode": "replace",
  "rules": [
   [
    "等你在待办里裁决",
    "请确认情景假设"
   ]
  ]
 },
 {
  "current": "维持现有判断 → 维持现状",
  "suggested": "当前观点不变",
  "note": "重要时再说明原因",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "^维持现有判断$",
     "flags": ""
    },
    "当前观点不变"
   ],
   [
    {
     "re": "^维持现状$",
     "flags": ""
    },
    "当前观点不变"
   ]
  ]
 },
 {
  "current": "重试语言质检",
  "suggested": "重新生成定稿",
  "note": "仅在真实效果一致时使用；管理员可保留技术词",
  "mode": "page",
  "rules": [
   [
    "重试语言质检",
    "重新生成定稿"
   ]
  ]
 },
 {
  "current": "草稿，文字表达尚未校对",
  "suggested": "草稿 · 尚未审核",
  "note": "不将文字校对等同研究质量核验；不同审核分开",
  "mode": "replace",
  "rules": [
   [
    "草稿，文字表达尚未校对",
    "草稿 · 尚未审核"
   ]
  ]
 },
 {
  "current": "只读投影 / authority / admission",
  "suggested": "记录 / 审核依据 / 已获准任务",
  "note": "按具体语义翻译，不机械全局替换",
  "mode": "replace",
  "rules": [
   [
    "只读投影",
    "记录"
   ],
   [
    {
     "re": "^authority$",
     "flags": ""
    },
    "审核依据"
   ],
   [
    {
     "re": "^admission$",
     "flags": ""
    },
    "已获准任务"
   ]
  ]
 },
 {
  "current": "重新进入失败、改绑票据",
  "suggested": "研究恢复失败，需管理员处理",
  "note": "不强迫用户理解执行凭证",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "重新进入失败(（[^）]*）)?",
     "flags": "g"
    },
    "研究恢复失败，需管理员处理"
   ],
   [
    {
     "re": "[，、]?已?自动改绑票据[^，。；）]*",
     "flags": "g"
    },
    ""
   ],
   [
    {
     "re": "改绑票据",
     "flags": "g"
    },
    "研究恢复"
   ]
  ]
 },
 {
  "current": "不加 --apply 是只读预览",
  "suggested": "从分析师界面删除",
  "note": "功能由业务按钮承接",
  "mode": "remove",
  "rules": [
   [
    {
     "re": "（?不加 --apply 是只读预览[^）。]*）?",
     "flags": "g"
    },
    ""
   ]
  ]
 },
 {
  "current": "占着端口和进程，不产出任何研究",
  "suggested": "设定研究目标后即可开始",
  "note": "删除开发资源叙述",
  "mode": "replace",
  "rules": [
   [
    "占着端口和进程，不产出任何研究",
    "设定研究目标后即可开始"
   ]
  ]
 },
 {
  "current": "下载 HTML 报告",
  "suggested": "下载研究报告",
  "note": "格式放菜单中",
  "mode": "replace",
  "rules": [
   [
    "下载 HTML 报告",
    "下载研究报告"
   ]
  ]
 },
 {
  "current": "目的地",
  "suggested": "发送到",
  "note": "下拉显示频道名字",
  "mode": "replace",
  "rules": [
   [
    "目的地",
    "发送到"
   ]
  ]
 },
 {
  "current": "销售纸条",
  "suggested": "销售简报 / 交易台简报",
  "note": "按实际来源确认，避免直译 sales note",
  "mode": "replace",
  "rules": [
   [
    "销售纸条",
    "销售简报"
   ]
  ]
 },
 {
  "current": "book-to-bill 低于一 / 订单出货比",
  "suggested": "订单收入比低于 1 倍",
  "note": "IT 服务语境通常不是实物“出货”；应依据实际指标定义",
  "mode": "replace",
  "rules": [
   [
    "book-to-bill 低于一",
    "订单收入比低于 1 倍"
   ],
   [
    "订单出货比",
    "订单收入比"
   ]
  ]
 },
 {
  "current": "USD 与 million 混合 / 超长数字",
  "suggested": "表头统一“金额：百万美元”",
  "note": "不机械认定 USD + million 本身错误；真正问题是口径和尺度不一致",
  "mode": "format",
  "rules": []
 },
 {
  "current": "已绑定来源",
  "suggested": "真实文档标题与日期",
  "note": "没有元数据则“来源信息待补齐”，不能装作可核查",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "^已绑定来源( · 已绑定来源)*$",
     "flags": ""
    },
    "来源信息待补齐"
   ]
  ]
 },
 {
  "current": "同一“研究内容”标题重复",
  "suggested": "市场观点 / 我们的判断 / 证据 / 验证条件",
  "note": "标题体现该段承担的功能",
  "mode": "structure",
  "rules": []
 },
 {
  "current": "放行一次重新进入（不花钱）",
  "suggested": "恢复专项课题研究",
  "note": "财务 痛点3 / T03 / T04；am：文字评估第十章（不再声明“不产生费用”）",
  "mode": "replace",
  "rules": [
   [
    "放行一次重新进入（不花钱）",
    "恢复专项课题研究"
   ],
   [
    "放行一次重新进入",
    "恢复专项课题研究"
   ],
   [
    "放行重新进入",
    "恢复专项课题研究"
   ],
   [
    "恢复研究任务（不产生费用）",
    "恢复专项课题研究"
   ],
   [
    "恢复研究任务",
    "恢复专项课题研究"
   ]
  ]
 },
 {
  "current": "再买一次模型调用（上限由你定）",
  "suggested": "扩大资料检索范围并重新获取",
  "note": "财务 痛点3：交易软件里「再买一次」有下单含义；am：文字评估第十章",
  "mode": "replace",
  "rules": [
   [
    "再买一次模型调用（上限由你定）",
    "扩大资料检索范围并重新获取"
   ],
   [
    "再买一次模型调用",
    "扩大资料检索范围并重新获取"
   ],
   [
    "付费重试获取资料（费用上限由你定）",
    "扩大资料检索范围并重新获取"
   ],
   [
    "付费重试获取资料",
    "扩大资料检索范围并重新获取"
   ],
   [
    "再买一次",
    "重新获取"
   ],
   [
    "付费重试",
    "重新获取"
   ]
  ]
 },
 {
  "current": "撤回这项研究（规划器可按当前预算重新立项）",
  "suggested": "暂缓当前专项课题（保留现有底稿）",
  "note": "T03 / 第 13 节文案样例；am：文字评估第十章",
  "mode": "replace",
  "rules": [
   [
    "撤回这项研究（规划器可按当前预算重新立项）",
    "暂缓当前专项课题（保留现有底稿）"
   ],
   [
    "停止这项研究（已有成果保留，需要时可重新开始）",
    "暂缓当前专项课题（保留现有底稿）"
   ],
   [
    "撤回这条研究任务",
    "暂缓当前专项课题"
   ],
   [
    "撤回这项研究",
    "暂缓当前专项课题"
   ],
   [
    "停止这项研究",
    "暂缓当前专项课题"
   ],
   [
    "已由你撤回",
    "已由你暂缓"
   ],
   [
    "已由你停止",
    "已由你暂缓"
   ]
  ]
 },
 {
  "current": "停止这条研究线",
  "suggested": "暂缓当前专项课题",
  "note": "财务 4.13：车道/研究线是调度词汇",
  "mode": "replace",
  "rules": [
   [
    "停止这项专项研究",
    "暂缓当前专项课题"
   ],
   [
    "暂缓该专项课题",
    "暂缓当前专项课题"
   ]
  ]
 },
 {
  "current": "否定条件已出现",
  "suggested": "核心假设证伪预警触发",
  "note": "财务 2.3 / 5.3；MUSE 第 3 节；am：第十章",
  "mode": "replace",
  "rules": [
   [
    "否定条件已出现",
    "核心假设证伪预警触发"
   ],
   [
    "已出现的否定条件",
    "已触发的证伪预警"
   ],
   [
    "否定条件",
    "证伪条件"
   ],
   [
    {
     "re": "(^|[^假设])证伪预警触发",
     "flags": "g"
    },
    "$1核心假设证伪预警触发"
   ]
  ]
 },
 {
  "current": "由你判断，程序不读",
  "suggested": "需人工核实确认",
  "note": "C21 / 财务 5.3",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "需人工确认",
     "flags": "g"
    },
    "需人工核实确认"
   ]
  ]
 },
 {
  "current": "所需资料已齐",
  "suggested": "所需研究资料已齐备",
  "note": "财务 5.3；C03：说清是本阶段必需资料",
  "mode": "replace",
  "rules": [
   [
    "所需资料已齐",
    "所需研究资料已齐备"
   ]
  ]
 },
 {
  "current": "语言质检 / 重新进行语言质检",
  "suggested": "中文定稿校对 / 重新生成中文定稿",
  "note": "MUSE 第 2 节；财务 痛点5；am：第十章",
  "mode": "replace",
  "rules": [
   [
    "重新进行语言质检",
    "重新生成中文定稿"
   ],
   [
    "语言质检",
    "中文定稿校对"
   ],
   [
    "中文校对",
    "中文定稿校对"
   ]
  ]
 },
 {
  "current": "流水线 / 工单 / 规划器",
  "suggested": "研究流程 / 待办决策事项 / 投研系统",
  "note": "财务 5.3；G10；am：第十章",
  "mode": "replace",
  "rules": [
   [
    "流水线",
    "研究流程"
   ],
   [
    "工单",
    "待办决策事项"
   ],
   [
    "规划器",
    "投研系统"
   ],
   [
    "流程下一轮",
    "系统下一轮"
   ]
  ]
 },
 {
  "current": "按钮通过写入服务以你的身份执行…",
  "suggested": "操作会记录在项目历史中。",
  "note": "T03",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "按钮通过写入服务以你的身份执行[^。]*。",
     "flags": "g"
    },
    "操作会记录在项目历史中。"
   ],
   [
    "写入服务",
    "系统"
   ]
  ]
 },
 {
  "current": "票据 / --apply / 端口和进程",
  "suggested": "任务授权 / （删除）/ 系统资源",
  "note": "MUSE 10.1；财务 5.3",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "票据",
     "flags": "g"
    },
    "任务授权"
   ],
   [
    {
     "re": "（?先?不加 --apply[^）。；]*）?",
     "flags": "g"
    },
    ""
   ],
   [
    {
     "re": "\\s?--apply\\b",
     "flags": "g"
    },
    ""
   ],
   [
    "端口和进程",
    "系统资源"
   ]
  ]
 },
 {
  "current": "多空地图 / 初步筛查报告",
  "suggested": "核心争议 / 初步研究报告",
  "note": "第 13 节文案样例",
  "mode": "replace",
  "rules": [
   [
    "多空地图",
    "核心争议"
   ],
   [
    "初步研究筛查报告",
    "初步研究报告"
   ],
   [
    "初步筛查报告",
    "初步研究报告"
   ]
  ]
 },
 {
  "current": "挡住了整条研究链 / 有一整个环节停着 / 积压的待办",
  "suggested": "P0 · 阻断核心研判推进 / P1 · 影响模型深度与精度 / P3 · 例行质量维护",
  "note": "T02 / 第 13 节：说清优先程度；am：文字评估 7.1",
  "mode": "replace",
  "rules": [
   [
    "挡住了整条研究链",
    "P0 · 阻断核心研判推进"
   ],
   [
    "有一整个环节停着",
    "P1 · 影响模型深度与精度"
   ],
   [
    "有一类问题永远答不了",
    "P2 · 个别专题数据受限"
   ],
   [
    "积压的待办",
    "P3 · 例行质量维护"
   ],
   [
    "知会一下，不用你动手",
    "提示 · 仅供业务知悉"
   ]
  ]
 },
 {
  "current": "us-gaap:概念名",
  "suggested": "中文科目名",
  "note": "财务 5.3 / C22：未知概念保持原样，只在技术信息里出现",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "us-gaap:([A-Za-z]+)",
     "flags": "g"
    },
    {
     "template": "{1}",
     "lookup": {
      "1": "statement_line"
     }
    }
   ]
  ]
 },
 {
  "current": "草稿 · 尚未审核",
  "suggested": "初稿（待审核签发）",
  "note": "am：文字评估 5.9 / 第十章",
  "mode": "replace",
  "rules": [
   [
    "草稿 · 尚未审核",
    "初稿（待审核签发）"
   ]
  ]
 },
 {
  "current": "投资建议规则 / 使用系统默认规则",
  "suggested": "投资建议准入与纪律规则 / 机构默认投研纪律标准",
  "note": "am：文字评估 8.3 / 第十章",
  "mode": "replace",
  "rules": [
   [
    "使用系统默认规则",
    "机构默认投研纪律标准"
   ],
   [
    "投资建议规则",
    "投资建议准入与纪律规则"
   ],
   [
    "代码默认策略",
    "机构默认投研纪律标准"
   ],
   [
    "代码默认",
    "机构默认投研纪律标准"
   ]
  ]
 },
 {
  "current": "规划器 / 只读投影 / 哈希",
  "suggested": "投研系统 / 归档数据 / 数字指纹",
  "note": "am：第十章",
  "mode": "replace",
  "rules": [
   [
    "内容哈希",
    "数字指纹"
   ],
   [
    "原文哈希",
    "原文数字指纹"
   ],
   [
    "哈希",
    "数字指纹"
   ],
   [
    {
     "re": "SHA-256",
     "flags": "g"
    },
    "数字指纹"
   ]
  ]
 },
 {
  "current": "由你判断",
  "suggested": "需人工核实确认",
  "note": "am：第十章",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "（由你判断，",
     "flags": "g"
    },
    "（需人工核实确认，"
   ]
  ]
 },
 {
  "current": "停止等待",
  "suggested": "后台继续分析（离开当前等待）",
  "note": "am：文字评估 6.2",
  "mode": "page",
  "rules": [
   [
    {
     "re": "^停止等待$",
     "flags": ""
    },
    "后台继续分析（离开当前等待）"
   ]
  ]
 },
 {
  "current": "（price → xueqiu）/（transcript → alphaengine）",
  "suggested": "（删除：取数路由只在技术信息里）",
  "note": "am：问答缺口外露连接器路由",
  "mode": "remove",
  "rules": [
   [
    {
     "re": "\\s*[（(](?:技术细节：)?`?[a-z_]+ → [a-z_]+`?[）)]",
     "flags": "g"
    },
    ""
   ]
  ]
 },
 {
  "current": "NO_CHANGE / research",
  "suggested": "维持基准判断（启动专项研究）",
  "note": "am：研判代码词外露",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "(?<![A-Za-z0-9_])(NO_CHANGE|THESIS_STRENGTHENED|THESIS_WEAKENED|THESIS_BROKEN|NEW_THESIS)\\s*/\\s*(no_change|note|research|revise_forecast|revise_thesis|revise_dossier)(?![A-Za-z0-9_])",
     "flags": "g"
    },
    {
     "template": "{1}（{2}）",
     "lookup": {
      "1": "judgement_decision",
      "2": "judgement_action"
     }
    }
   ],
   [
    {
     "re": "(?<![A-Za-z0-9_])(NO_CHANGE|THESIS_STRENGTHENED|THESIS_WEAKENED|THESIS_BROKEN|NEW_THESIS)(?![A-Za-z0-9_])",
     "flags": "g"
    },
    {
     "template": "{1}",
     "lookup": {
      "1": "judgement_decision"
     }
    }
   ]
  ]
 },
 {
  "current": "price_move / earnings_release",
  "suggested": "股价异动 / 公司业绩稿（初步）",
  "note": "am：事件类型代码外露",
  "mode": "replace",
  "rules": [
   [
    {
     "re": "(?<![A-Za-z0-9_:/.-])([a-z]+(?:_[a-z]+)+)(?![A-Za-z0-9_:/.-])",
     "flags": "g"
    },
    {
     "template": "{1}",
     "lookup": {
      "1": "event_kind"
     }
    }
   ]
  ]
 }
]);

export const BANNED_TERMS = Object.freeze([
 {
  "term": "订单出货比",
  "replacement": "订单收入比",
  "note": "book-to-bill 的分母是收入"
 },
 {
  "term": "初稿 · 文字未经审校",
  "replacement": "初稿（待审核签发）",
  "note": "统一页面与导出状态"
 },
 {
  "term": "草稿，文字表达尚未校对",
  "replacement": "初稿（待审核签发）",
  "note": "统一页面与导出状态"
 },
 {
  "term": "最低证据置信度",
  "replacement": "底稿证据充分度准入门槛",
  "note": "说明规则的业务含义"
 },
 {
  "term": "旧按钮引用",
  "replacement": "使用 terminology.label 获取当前按钮名",
  "note": "引号不经 scrub，必须从源头改",
  "pattern": "「(?:已知悉|暂不提醒|放弃这份资料|停止这项研究|投研方向指引|报告模板|导出财务模型|研究设置|研究问答|待我处理|证据)」"
 },
 {
  "term": "规划器",
  "replacement": "投研系统",
  "note": "Agent 规划算法细节不外露"
 },
 {
  "term": "车道",
  "replacement": "研究流程 / 专项课题",
  "note": "调度术语"
 },
 {
  "term": "流水线",
  "replacement": "研究流程",
  "note": "调度术语"
 },
 {
  "term": "工单",
  "replacement": "待办决策事项",
  "note": "IT 工单感"
 },
 {
  "term": "票据",
  "replacement": "任务授权",
  "note": "执行凭证概念"
 },
 {
  "term": "改绑",
  "replacement": "恢复研究",
  "note": "执行凭证概念"
 },
 {
  "term": "只读投影",
  "replacement": "归档数据",
  "note": "数据库投影概念"
 },
 {
  "term": "模型网关",
  "replacement": "AI 服务（管理员设置）",
  "note": "运维参数"
 },
 {
  "term": "档位",
  "replacement": "AI 服务等级（管理员设置）",
  "note": "运维参数"
 },
 {
  "term": "链尾",
  "replacement": "备用 AI 服务",
  "note": "运维参数"
 },
 {
  "term": "SHA-256",
  "replacement": "数据版本与溯源指纹（技术信息内）",
  "note": "哈希值不出现在分析页"
 },
 {
  "term": "哈希",
  "replacement": "数字指纹（技术信息内）",
  "note": "哈希值不出现在分析页"
 },
 {
  "term": "token 上限",
  "replacement": "输出长度上限（管理员设置）",
  "note": "计费单位",
  "pattern": "(?i)token\\s*上限"
 },
 {
  "term": "再买一次",
  "replacement": "扩大资料检索范围并重新获取",
  "note": "交易软件里有下单含义（高危）"
 },
 {
  "term": "付费重试",
  "replacement": "扩大资料检索范围并重新获取",
  "note": "小账语言"
 },
 {
  "term": "放行",
  "replacement": "恢复专项课题研究 / 核准",
  "note": "调度用语"
 },
 {
  "term": "否定条件已出现",
  "replacement": "核心假设证伪预警触发",
  "note": "证伪（Falsification）"
 },
 {
  "term": "程序不读",
  "replacement": "需人工核实确认",
  "note": "机器视角",
  "pattern": "由你判断，程序不读|程序不读"
 },
 {
  "term": "公司名会写在问题开头",
  "replacement": "聚焦标的",
  "note": "实现细节自白",
  "pattern": "公司名会写在问题开头"
 },
 {
  "term": "花一次…费用",
  "replacement": "（不在分析页提示单次费用）",
  "note": "算力焦虑",
  "pattern": "花一次[^。；]{0,12}费用"
 },
 {
  "term": "不产生费用",
  "replacement": "（删除）",
  "note": "小账语言",
  "pattern": "不产生费用|不花钱"
 },
 {
  "term": "rule: …",
  "replacement": "预警触发线：<指标><方向><阈值>",
  "note": "程序规则表达式",
  "pattern": "(?:\\brule:\\s*)?\\b(?:[A-Za-z]+_[A-Za-z_]+|growth)\\s*[<>≤≥]=?\\s*-?\\d|\\brule:\\s*\\S"
 },
 {
  "term": "综合风险评分",
  "replacement": "下行风险情景（发生概率 / 潜在跌幅）",
  "note": "伪风险评分",
  "pattern": "综合风险评分|风险：悲观情景概率"
 },
 {
  "term": "维持观点：维持现状",
  "replacement": "（复读）",
  "note": "语义复读",
  "pattern": "维持观点：维持现状|维持观点：当前观点不变"
 },
 {
  "term": "本行为…科目",
  "replacement": "<科目名>",
  "note": "代码直译注水句",
  "pattern": "本行为[^。\\n]{1,40}?科目"
 },
 {
  "term": "代码默认",
  "replacement": "机构默认投研纪律标准",
  "note": "程序员俚语"
 },
 {
  "term": "Conviction v2",
  "replacement": "投资建议准入与纪律规则",
  "note": "版本号代码"
 },
 {
  "term": "--apply",
  "replacement": "（删除）",
  "note": "命令行参数"
 },
 {
  "term": "去看看",
  "replacement": "查看异动详情 →",
  "note": "口语"
 },
 {
  "term": "转管理员",
  "replacement": "提交技术协助",
  "note": "推卸运维责任"
 },
 {
  "term": "停止等待",
  "replacement": "后台继续分析（离开当前等待）",
  "note": "易误解为取消"
 },
 {
  "term": "把差额放到基准情景",
  "replacement": "将剩余差额一键平衡至「基准情景」",
  "note": "心算负担"
 },
 {
  "term": "下一市场日程",
  "replacement": "下一关键催化事件",
  "note": "催化（Catalyst）"
 },
 {
  "term": "sha256:",
  "replacement": "（内部引用不外露）",
  "note": "内部引用"
 },
 {
  "term": "guidepoint-excerpt:",
  "replacement": "新增专家访谈纪要",
  "note": "内部引用"
 },
 {
  "term": "机器精度数字",
  "replacement": "按量级取整并带单位（numeric_display.humanize_machine_numbers）",
  "note": "超精度机器数字",
  "pattern": "(?<![A-Za-z0-9_.,:/\\-])-?(?:[1-9]\\d*\\.\\d{5,}|0\\.0*[1-9]\\d{4,}|[1-9]\\d{4,}\\.\\d{3,})(?![A-Za-z0-9_])|\\d\\.\\d{4,}\\s*[（(]\\s*即"
 },
 {
  "term": "〔数字见研判记录〕",
  "replacement": "把数字与引用渲染进正文",
  "note": "间接占位符（批次 ap：需 Claim 绑定的数字）",
  "pattern": "〔[^〕]{0,16}见[^〕]{0,16}〕"
 }
]);
