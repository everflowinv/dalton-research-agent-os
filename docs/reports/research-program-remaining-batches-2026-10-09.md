# 研究能力改造：剩余批次详细计划（2026-10-09 暂停点）

历史技术评审：保留当时结论与局限；当前运行状态见 [项目状态](../PROJECT_STATUS.md)。原始操作证据私有保存。

> 截至 main `f587a46f`：批次 ai–ak 已合入（ai 已部署；aj、ak 待部署）。本文件是 owner 要求的「剩余批次需要做什么」的详细说明，供恢复时直接派工。依据：`research-capability-diagnosis-2026-10-09.md`（程序与 owner 决定）、`research-quality-review-gap-list-2026-10-09.md`（缺口矩阵与批次 an–av 规则）、各批次交付报告中留下的「未做/留给后续」条目。原则不变：**所有规则对所有公司、所有 workspace 生效；不按个例打补丁；测试用合成公司；线上只读；同一套代码；重起草按「收集一批再起草、累积到阈值再重写」。**

## 0. 恢复时先做的事（不算批次）

1. 部署 aj + ak（`zsh docs/ops/deploy.sh`），按 pending-deploy.md 的「部署后验证」逐项核对；重点：证据索引回填完成（legacy `document-index.sqlite` 待索引归零）、材料获取 lane 的 open 请求与 AlphaEngine 定向检索次数、ACN 初筛按 STRL 合约重写一次（费用上限 14 美元）、ACN 10-01 事件重判、ws-7d 的 MSFT/META/AMZN 误归属陈述退休与产物「需重写」标记、META 模型重建后 2028Q2 营业利润为正。
2. owner 待办：EPAM 两张卡各点一次「恢复专项课题研究」（若仍在）；ws-e399 决定写目标或停环境；DeepSeek 充值（可选）；飞书开通（可选）。
3. 文字核对报告（本次暂停前已派只读核对 `cockpit-v2-all-pages-text-revision` 落实情况）回来后，把「未应用」项并入批次 ap 或单独小批次。

## 1. 批次 ar：模型第二波（业务驱动与口径）

**目标**：模型从「机械外推且算得全」变成「可解释、按公司定义口径、与申报滚动」。依赖：M、al、B、B2 已合入。

规则（全部代码层，提示词只辅助）：
1. **滚动**：新申报（10-Q/10-K/8-K 初步实际）到来后，预测期起点自动前移，预测期 ≥8 季或覆盖到 FY+1 年末（取较长者）；C2 发现 ACN v9 的 8 个预测季从已报告的 2026-08 季开始，这是本条要修的根因（`model_forecast_driver.forecast_periods` 由 spec 自选且只有上限 12）。
2. **季节性**：新增同比环比 / 季节因子方法；季节性显著（按历史季度分布检验）的行禁止平均环比外推（IBM 大型机周期等）。
3. **驱动必须进公式**：spec 声明的分部、量价、合同转化、one_off 驱动若未进入公式，模型标「驱动计划未执行」并扣分/阻断发布。
4. **分部桥不变量升级**：B2 的分部桥（v10）从「只对完全闭合计数」扩展到「不闭合即 FAIL 并写明差额与缺失成员」；已披露 XBRL 分部成员按身份映射，禁止按大小推断（DXC 的 GIS/CES/Insurance）。
5. **非核心项隔离**：证券收益、权益法损益、一次性处置等不可预测时隔离在营业外，不阻断核心营业利润（GOOGL、AMZN 的 Anthropic 收益）。
6. **折旧与利息强制**：折旧用资产滚动表（`roll_forward`），利息用债务余额×利率（`rate_times_balance`）；缺基础行时标原因，不回退环比增速。
7. **capex/FCF 公司口径**：接 B 的 FCF 桥（系统/公司/一次性调整三口径），模型侧 capex 含融资租赁本金、资本化软件与合同成本（META、DXC）。
8. **年度汇总四类标注**：历史 / 混合 / 预测 / 不完整；缺季不当 0；整年历史缺失作为入库缺口单独报出（CTSH/EPAM/ACN FY2024A、DXC FY2025A、GOOGL FY2024A）。
9. **历史 EPS 读申报值**；模型输入带报表行身份；模型假设引用证据段落（`doc-passage`）或指引/一致预期记录。
10. **敏感性重做**：量、价、成本按机制联动（不再单因子历史区间）；量级合理性检查（al 已加 3 倍申报收入阻断，扩展为区间检查）；绑定当前模型版本（an 的 stale 机制已能标出旧绑定）；模型与一致预期差异先过季节性/口径/新鲜度检查再进 conviction。
11. **每股序列**：TTM EPS 用净利润÷加权股数重建；优先股/普通股分开（GOOGL）；股息缺失标「未采集」不写「无股息」；DXC 稀释股数补齐。
12. **估值双口径**：P/E 同时给 GAAP 与核心利润口径；分位展示有效样本与窗口（B 已做「样本不足」措辞）；收益桥实现腿 point-in-time、股息按实际派发。
13. **覆盖公司必须有模型或显式阻断原因**（AMZN、MSFT 现在两者皆无）；阻断原因进待办卡与公司页。
14. 长上下文路由下限扩到备忘录/档案/争议图（C2 只做了初筛与其核验）：把这些 purpose 加入 `LONG_CONTEXT_PURPOSES` 并同步提高车道单轮预算算术（备忘录 4 次起草 + 1 次核验）。

文件：`model_forecast_driver.py`、`forecast_coverage*.py`、`company_financial_statement_structure.py`、`economic_invariants.py`、`forecast_sensitivity.py`、`valuation_snapshot*.py`、`return_attribution.py`、每股序列模块、`call_budget*`、`model_router.py`。
测试：合成季节性公司拒绝平均环比；合成「集团=分部+公司层」漏成本判 FAIL；证券收益隔离后核心营业利润可算；预测期 <8 季被拒；滚动后起点前移；敏感性荒谬量级阻断。
验收：七份模型回放期数 ≥8 且起点在最新申报之后；GOOGL 核心营业利润 computed；分部桥通过；DXC 分部按身份映射；META 敏感性无千亿级负数；估值输出含有效样本数；AMZN/MSFT 显示阻断原因。

## 2. 批次 av + SV：情景条件规则与风险收益（情景估值）

**前置**：ar 的滚动与 EPS；B 的 10 年价格/EPS 历史回填完成（部署后由价格 lane 自动做）。

av（条件规则）：
1. 条件词表禁止引用系统自身 judgement（`THESIS_*`、「研判加强」）；价格阈值只做监测，价格位由 EPS×倍数推导。
2. owner 定性条件编码为 {来源, 时间, 判定规则}，不能直接改概率。
3. 同一指标三情景阈值由同一组有序切点生成（互斥穷尽），校验器拒绝重叠或空档。
4. 概率必须写依据；「0.5/0.25/0.25」模板先验显式标出。
5. 统一 12 个月评估窗口；基准绑模型版本（an 已加 `model_binding`）；现金条件用公司定义 FCF。
6. 无模型无估值不出场景；缺失显示「缺失」不当 0 概率（MSFT 倒置）。

SV（情景估值 `scenario_valuation.py`，只追加、带版本、可复算）：
- 每情景：情景 EPS（我们的模型 EPS × 情景 `driver_deltas`，由驱动模型重算，不手填）× 倍数（历史滚动 P/E 分位 P20/P50/P80，可覆盖并记理由）+ 12 个月股息 → 目标价 → 回报 = 目标价/现价 − 1，窗口「今天到 12 个月后」。
- 汇总：概率加权期望回报、偏度（上行÷|下行|）、上下行目标价；并列一致预期目标价与卖方高低。
- 实现缺失的 `scenario_version.conviction_scenarios`，conviction v2 读已计算的 payoff；比较表「预期回报」列与风险列改读它（`/v2/data/charts/risk-reward`），替换「模型预期总回报 对比期间」；`return_attribution` 的 driver leg 保留为盈利增长贡献拆分。
- 情景起草提示词加 `driver_deltas`（收入增速与利润率偏移）。
- 测试：目标价可复算；概率和为 1；窗口起点=价格日；模型无 EPS 时整份「不可估值」而非代理；分区重叠被拒；引用 THESIS_* 被拒。
- 验收：八套情景回放无重叠/空档、循环条件为 0；五家公司页头条出现「现价｜基准目标（+a%，p）｜悲观｜乐观｜加权｜上下比」或诚实的「情景尚未估值」。

## 3. 批次 HV：公司页首屏「本所观点」

后端 `/v1/cockpit/overview` `companies[].house_view = {statement, as_of, version, change_label, reasons[3]{text, claim_ref/passage_ref}, risk_reward|null, next_catalyst{date, what_to_watch}, what_changes_view[]}` 由 `cockpit_plane` 组装，不调模型；`company_view.js` 首屏只渲染这五块，其余折叠或迁到 reports/risks；连续 NO_CHANGE 合并为「近 N 天 M 次复核均维持」；过时 note（「等着起草初筛报告」）由后端按产品实际状态重算；无中文名指标不上首屏；催化剂跳过除息日等低价值事件，优先财报/投资者日并写要看的指标。
测试：首屏 DOM 只有五块；NO_CHANGE 合并；当前行业论点不进历史折叠；风险收益缺数据时诚实空状态。

## 4. 批次 TM：论点建模（问题 / 本所观点 / 反向观点 / 分歧）

- 论点内容加 `question_ref`（如「美国 IT 服务需求是否见底」）与 `stance`（house/counter）；同一 `question_ref` 只能一个 house 版本 current。
- `thesis_revision`：修订后 statement 与当前方向相反必须标 BROKEN（或另立问题下的结论），用便宜模型做方向判别作为验证项；同一问题下要发布两个相反 current 版本必须同时写 `disagreement` 记录（对象、双方、证据、截止日、裁决人），否则拒绝写入；修订候选必须给 `proposed_confidence` 且与正文措辞一致（writer 校验）；行业论点修订必须引用最新行业框架版本。
- 正式 thesis 必须有 `claim_refs`/passage refs、量化证伪条件与期限；空修订候选拒收。
- 「thesis_superseded：无需重判，仅归档」改为按 C 的阈值自动 reopen。
- `/v1/cockpit/overview` 增 `counter_views[]`；总览与公司页显示本所观点与反向观点并列、分歧记录可见。

## 5. 批次 ao：新鲜度与生命周期

催化条目过期必须转 occurred/missed/unconfirmed 并链接结果，引用 0 不发布，estimated 不驱动重判，日期冲突保留来源与取舍，财期与发布日并列；零基审查 `next_verification` 过期自动 stale 并排重审（作为重起草阈值之一）；前瞻事件后自动滚到下一财季、旧前瞻归档；事件带原始事件日与信息可得日并去重（F 已做部分）；定量段落必须用最新已申报期或标「历史对照」（档案 2023/2024 分部数据、框架 2024Q3 对照表违规）；分部命名跟随最新报告结构（DXC GBS→GIS）。
文件：`catalyst_calendar.py`、`mission_catalyst_lane.py`、`zero_base_review.py`、`mission_zero_base_lane.py`、`earnings_preview.py`、`company_dossier_cli.py`、`industry_framework.py`。
验收：两环境回放过期未标结果的催化为 0，过期零基为 0 或已排程，档案与框架无未标注旧期段落。

## 6. 批次 aq：行业原型框架与新 workspace 引导

框架为档案每方面（demand/supply_and_cost/pricing/cash）各提供 ≥1 条可计量因果链，`causal_chain_unmapped` 触发框架修复而非输出 unavailable；行业原型库 `industry_archetypes.py`（重资本算力：电力→容量→利用率→单位收入→折旧→FCF；广告：展示量×单价与版位组合；零售/履约；IT 服务：订单队列→收入转化、利用率、单价、AI 让利；消费型云：积压→确认节奏）；workspace 初始化必须按原型具体化，否则框架与档案标「未专业化」；驱动包与证据包声明冻结范围与日期，周报「新增」按全部研究输入算；档案分部覆盖与收入/营业利润权重相称（AMZN 过度 AWS）；跨公司对照同口径同期间。
验收：Hyperscalers 副本回放四家 supply_and_cost 有正文；新建空 workspace 按原型具体化后无 generic 驱动。

## 7. 批次 at：研究问题与产出生命周期 + 存量人工门清理

问题按规范键（issuer、accession/事实、指标）去重，改写只产新版本；research_question 与 backlog_question 合并为单一状态 authority；answered 必须满足回答标准且文档类型匹配期间；超范围题归档（万华 59 条）、系统状态题转运维；staged 候选 SLA 内终态（66 条）；无新材料不重扫，重复 source_unavailable 转 MaterialRequest；form type 取 SEC 元数据；别名/中文名相关性自动重跑无需 owner 授权。
B2 的人工门清单（例行项改自动）：`mission_document_research_lane.py:998` 改为自动 `admit_directed_inquiry` 并删禁词；`contract_failed_after_automatic_retry` 在日上限内增加一次有界重试；`awaiting_human_extraction` 换显示标签或 schema 迁移；deadline 超期 hold 自动撤回。保留人工：`unproved_send_failed_after_automatic_retry`（重复收费风险）、`provider_budget_exceeded_*`、新的付费检索。
验收：open 分析题降到去重后规模；绑定为 0 的分析题都挂请求或终态原因。

## 8. 批次 au：闭环——对账、结果核验与决策治理

`forecast_reconciliation.py` 扩展到预测模型：8-K 初步实际或 10-Q 到来即对每个预测单元算误差并归因到假设与方法，不需 mission 授权，重大偏差触发阈值重起草（不自动改模型）；outcome check 预登记方向/阈值/基准/信息可得日，unavailable 单独统计，核验 revise 类与 thesis 方向，无正式 thesis 的公司显示「覆盖缺口」；决策治理（候选一个终态、不同瞬间、「无需修订」与「拒绝」分开、通知去重标方向、未附理由标明）；周报必须有对账段并声明基底范围与日期，每个 workspace 可出周报（Hyperscalers 目前无）；反思清单负责人/期限/关闭证据，人工检查点超 14 天升级。
验收：两环境有对账记录；outcome 统计按「可判定/不可判定」分开。

## 9. 批次 as：研究推理规则 lint 扩展

D 已实现九条规则并进入核验与 Rubric H；G2 收窄了「共识」规则的否定句误报。剩余：把 lint 接入档案、争议图、备忘录、事件判断、问答的核验（部分已接）；对当前九份初筛与档案回放，命中清单与评审 B/C 评语的重合度作为 H 的黄金用例；规则各加一正一反合成段落（已有）并补「美元收入≠本币有机增长」「并购与汇率拆开」的数值检测。

## 10. 批次 ap 剩余项与文字核对

文字核对已完成（`cockpit-text-revision-audit-2026-10-10.md`）：284 条建议中 246 已应用、18 有意偏离、20 未应用；另有改名后「」内引用旧按钮名/章节名的不一致若干。处理建议见该文件末尾四条（未应用项逐条落实、旧词进 BANNED_TERMS 并扩大 lint 扫描名单含 HTML 导出、「」内 UI 名称改为注册表取词并加 lint、偏离原因记录）。

D 已做实质门、占位符禁止、裸数字拦截、gate 回答标准、缺口分类。剩余：管理员页汇总显示本地化 `skipped` 计数；旧 `mission_deliverable` gate 遗留件标 superseded 并指向正式 gate；文字核对报告（本次暂停前派出的只读核对）列出的「未应用」项逐条落到术语注册表或源头生成器。

## 11. 批次 H：验收

- 确定性评分表已接入初筛；扩展到档案/争议图/备忘录/事件/问答各自的 rubric。
- 黄金回放已有：ACN 10-01 事件（F）、「ACN 昨天为什么大涨」（G）、初筛 STRL 合约黄金集 7 例（D）。待加：ACN 初筛用新合约真实跑一次后与 STRL 盲评（不同模型家族，五维度：观点、量化、经济性、估值、交易），结果写成验收报告。
- 任务级验收（30 秒看懂项目、比较公司、验证结论两步到原文、阅读模型、处理待办、修改范围、修改概率、追问、导出、简报、键盘与窄屏）在隔离测试项目上执行写操作并记录。

## 11a. 批次 aw：模型调用思考强度（thinking level）显式化与按环节设定

**现状（2026-10-10 两轮核查，第二轮更正第一轮结论）**：
- Dalton 代码只在「独立核验」类调用上显式传 `thinkingLevel=low`（`openclaw_model_adapter.py` `VERIFIER_THINKING_LEVELS={"low"}`）；其余调用不传。
- OpenClaw 网关 `agents.defaults.thinkingDefault = "xhigh"` **只作用于交互式 agent**；broker 插件（`~/.openclaw/managed-plugins/openclaw-model-broker-*`）本身不设思考强度，只在 profile/请求里有 `thinkingLevel` 时透传。**网关日志实测**：Claude CLI 网关最近 400 次调用 365 次 `low`、33 次 `xhigh`（后者是交互 agent）；Antigravity 网关 400 次全部 `low`；muse 网关 240/253 次 `minimal`（OpenClaw 对 muse 把「off」映射成 minimal；muse 网关在未传 effort 时默认 high，但实际收到的是 off/minimal）。**结论：Dalton 的调用现在实际已经是 low/minimal，不是 xhigh**；但这取决于网关与 OpenClaw 的隐式行为，代码里没有声明。
- `deepseek-flash` 条目在网关参数里写死 `thinking: "max"`（模型级参数，可能覆盖请求级）。

**决定**：改在 Dalton 侧显式声明，不依赖网关隐式行为；不改网关全局默认（影响 owner 其他 agent）。

**规则**：
1. 适配器对每次模型调用都在 `controls` 里传 `thinkingLevel`；代码默认 `low`。放开 `VERIFIER_THINKING_LEVELS` 为通用允许集合 {off/minimal/low/medium/high}，核验类仍强制 `low`（provider-controlled 合同要求）。
2. 按 purpose 的覆盖放在代码默认（与批次 C2 的长上下文 purpose 下限同一处）：初筛与投资备忘录的整篇起草（STRL 合约第二步）、深度认知门、行业框架 = `medium`（这是相对现状的**提高**，理由：这些是唯一需要推理深度的环节，H 的黄金回放在 low 与 medium 下各跑一次比较后定）；其余全部 `low`；muse 路由上 `low` 与 `minimal` 的差异要在 ADR 里写明。
3. 管理员页「AI 服务」每个环节显示当前思考强度并可改，变更需理由、只追加记录、同步到所有环境。
4. `deepseek-flash` 的网关参数 `thinking: "max"`：确认 broker 请求级 `thinkingLevel` 是否优先；若模型级参数优先，建议 owner 把该条目改为不设，并在 ADR 记录。
5. 费用记录带 `thinking_level`；管理员「预算与费用」按环节显示前后对比；黄金回放在 low/medium 下的评分对比作为是否保留 medium 的依据。

**文件**：`openclaw_model_adapter.py`、`model_router.py`/`call_budget*`、`model_selection.py`、`cockpit_v2/admin/*`、`terminology.py`。
**测试**：任何 purpose 不显式声明即为 low；核验类传非 low 被拒；owner 覆盖只追加并发布到各环境；费用记录带 thinking_level；黄金回放在 low 下通过。
**验收**：部署后一轮内，网关日志里每次 broker 调用的 effort 与环节表一致；初筛整篇起草为 medium，其余 low；单次起草时长与费用记录前后数字。

## 11b. 批次 ay：发布流水线重复处理（owner 2026-10-10 在 muse 后台看到同一内容多次）

**核查结论（只读，legacy 最近 24 小时）**：
- 模型工单 1295 个，其中发布流水线四个环节占 988 个：本地化初稿 252、语言检查 208、语言修订 218、独立核验 310。
- `research-publication-work/stages/` 24 小时内 220 次运行，覆盖 192 份不同源产物；**23 份产物被重复运行，最多一份 7 次**；45% 的运行带 2–4 轮语言复审（`review_history`），三分之一带 1–2 次修复调用（`repair_brain_calls`）。每次运行约 4–5 次模型调用，同一段中文在初稿、检查、修订、核验四个提示词里各出现一遍，再乘以重复运行次数，就是 owner 看到的「同样内容好几遍」。
- muse 网关本身没有系统性重复发送：三周 253 次调用里只有 13 种相同大小的请求各出现 2 次，7 对在 5 分钟内（对应 `draft_attempts=2` 或传输重试），属正常重试。
- owner 贴出的那份产物（周度复盘，章节「时间投向」「文档深度研究」等正文等于标题）是**源产物本身空洞**（标题式章节），批次 D 的实质门只拦初筛/备忘录等合约产品，周度复盘没有过实质门，仍被整条流水线反复本地化。

**待修（未执行）**：
1. **同一源产物 24 小时内的运行次数上限与原因记录**：每次运行记录触发原因（新版本 / 语言复审未过 / 核验拒绝 / 每日重试 / rules_upgrade 重发），同一 `source_hash` 24 小时内最多 2 次，超出进 parked 并写原因；管理员「健康与故障」按产物显示运行次数与原因。
2. **复审与修复的收敛规则**：语言检查 → 修订 → 再检查最多 2 轮（现在观察到 4 轮）；核验拒绝后不重新起草整篇而是只修被拒句子（局部修复），且核验器的拒绝理由必须是确定性可复核的（与 D 的核验合约一致）。
3. **实质门覆盖全部产品**：周度复盘、事件汇总、行业框架、零基审查等非合约产品也过 `product_substance`（空节/标题式正文不进入本地化），源头修周度复盘的章节生成（标题式章节由「指标表」替代）。
4. **流水线去重**：四个环节共享同一份已本地化文本的缓存键（`source_hash` + 规则版本），检查/修订/核验只发送变化的章节而不是整篇；每份产物每个环节的提示词大小与调用次数记入费用记录。
5. **可观测性**：管理员「预算与费用」增加「每份产物的流水线调用次数」与「24 小时重复运行榜」，lint 一条：任何车道不得在同一 tick 对同一 `source_hash` 发两次同环节调用。
6. 把以上纳入 H 的验收：24 小时内每份产物的发布流水线调用 ≤ 6 次（4 个环节 + 至多 2 次修复）。

## 11c. 批次 az：研究环境的生命周期——分析师自助新建、管理员停用与删除

**现状（2026-10-10 核查）**：
- 新建只在管理员页「研究项目管理」有入口（`admin.js` `workspace_create`），走 `workspace_manager.create_managed_workspace`：校验 `config["owner_login"] == login`、分配端口、创建空白 workspace（`workspace_creation.create_blank_workspace`）、安装 launchd 服务（`workspace_process.install_workspace`）、配置 control（`workspace_control_setup`），创建请求幂等记录在 `~/.dalton/creation-requests/`。分析师页面没有入口；非 owner 登录不能创建。
- **没有任何停用/删除操作**：`workspace_fleet.py` 只有 `fleet_overview`；`agenda_control.py` 只认 `workspace_create`/`workspace_rename`。ws-e399 这种空环境只能一直空转占端口与进程。

**目标**：分析师能在自己的页面里新建研究环境并立刻开始写目标；管理员能停用、归档、删除环境；两者都走只追加的生命周期记录，同一套代码，新环境开箱即用。

**规则**：
1. **分析师自助新建**（研究设置 → 研究范围，以及顶栏项目切换器的「新建研究项目…」）：填项目名 + 初始研究目标（可选，填了就直接发布为 v1 任务书，省去再进总览写目标）；writer op 仍是 `workspace_create`（幂等 request_id）；创建中显示进度（分配端口 → 初始化 → 启动服务 → 就绪），就绪后自动切换到新项目的研究总览。权限：`allowed_tailscale_logins` 内任何登录都可新建（`create_managed_workspace` 的 `owner_login` 校验改为「登录在允许名单内」，记录创建者）；每人每日新建上限（默认 3）防误操作；未配置 `admin_logins` 时与现状一致。
2. **生命周期状态**（只追加表 `workspace_lifecycle_events` + `workspace.json` 的 `lifecycle` 字段）：`active` → `paused`（停用：launchd 服务 bootout，端口保留，状态目录原样，驾驶舱切换器标「已停用」仍可只读查看已发布研究）→ `archived`（归档：状态目录打包移到 `<host_root>/archive/<slug>-<date>/`，释放端口，launchd 配置删除，切换器隐藏但管理员页可见并可「恢复」）→ `deleted`（删除：归档包删除，不可恢复）。每一步只追加事件（谁、何时、理由）。
3. **管理员删除流程**（管理员页「研究项目管理」）：删除按钮只对 `paused` 或 `archived` 的环境可用；`active` 环境必须先停用（危险确认 + 键入项目名）；删除再次键入项目名并勾选「我知道不可恢复」；有未处理的待办决策或未发布的 owner 草案时先列出来再允许；删除后 `manager.json` 与 workspace 注册表写 `deleted` 事件（不删历史行），`~/.dalton/workspace-logs/<slug>` 一并清理或归档。
4. **空环境自动提示**：创建后 7 天仍无研究目标的环境，在待办里出一张「空环境 N 天未使用：停用 / 删除 / 保留」卡（替代现在 ws-e399 那张永远在的「还没有研究目标」）。
5. **安全边界**：legacy（主环境）不可停用/删除（标为「主环境」）；停用的环境不参与模型选择「同步到所有环境」与周报；release_switch 跳过 `paused/archived` 环境（健康检查不再把它们算作「未运行」）；parity 检查对停用环境只检查归档完整性。
6. **CLI 等价命令**保留在技术详情（`workspace_fleet pause|archive|restore|delete`），供无 UI 场景。

**文件**：`workspace_manager.py`、`workspace_fleet.py`（新增 pause/archive/restore/delete）、`workspace_process.py`（bootout/uninstall）、`agenda_control.py` 与 `cockpit_v2/admin/*`（动作与危险确认）、`cockpit_v2/pages/settings.js` + `shell.js`（分析师新建入口与进度）、`needs_human.py`（空环境卡）、`scripts/release_switch.py`（跳过停用环境）、`workspace_health_parity.py`。
**测试**：合成 host_root 上创建→停用→归档→恢复→删除全链路，每步只追加事件；非允许名单登录被拒；每日上限；主环境拒绝停用；release_switch 跳过停用环境；空环境 7 天出卡；blank canary。
**验收**：分析师在研究设置里新建一个环境并写目标，两分钟内在切换器里看到并进入；管理员把 ws-e399 停用再删除，端口释放、launchd 无残留、manager.json 有 deleted 事件；主环境删除按钮不可用。

## 12. 发布整理与推远端（全部批次部署验证后）

1. README 面向外部读者重写（项目、架构、研究流程、驾驶舱、部署与新建环境、能力与边界）；十月批次流水账移到 CHANGELOG；PROJECT_STATUS 收敛为「能力现状 + 已知缺口 + 路线图」；ADR 索引；docs/ops 待部署清单归档。
2. 内部运维与巡检记录移出公开仓库（只保留 ADR、设计文档、评审报告）。
3. 敏感与本机信息清理：旧 owner 操作手册（原件已私有归档）中的登录邮箱改占位符；`$HOME`（67 文件）、`/Volumes/<storage-volume>`（57 文件）改 `$HOME`/`<state-dir>`；`space.lumos.dalton` 保留；`scripts/rotate_dalton_logs.py` 路径不动（launchd 引用）；`docs/ops/*.sh` 参数化保留默认值。
4. 仓库结构重整与过程目录清理（docs/reports/evidence、release/r11a-candidate、external/chem-review、一次性历史脚本）；仓库外旧 release 目录与配置备份列清单请 owner 确认后删。
5. 全量测试通过、工作树干净、打标签（建议 `v0.11.0`）、推 main 与标签到 origin（公开仓库，推送前再扫一遍身份与路径）。

## 13. 已知零散问题（恢复时顺手核对）

- ACN 一致预期 FY2027 年度档在数据商缺失，导致数据商 NTM EPS 不可得（B2/C2 观察）；券商中位可用。
- 四份券商表格需模型兜底，`street_estimate` 用途未配模型。
- IBM/DXC 无 OperatingIncomeLoss 则 EBITDA 不可得；EPAM 只有非流动债务。
- 价格库无 SPY，宽基基准标「不可得」；无卖空与隐含波动率数据源。
- 文档抽取复核窗口未接 Exhibit 13（aj）。
- EPAM 分部经营利润不在报表事实里。
- 本地化空源节 `skipped` 计数未显示在管理员页汇总。
- owner 可选项：DeepSeek 充值、飞书开通脚本、IR 页面 json、HK 回购合同、SEC 计划加 10-Q、券商插件停机窗口。
