# Dalton 项目进度

更新日期：2026-09-10（恢复开发；以下历史时间记录保留）

## 持续开发中（2026-09-10，owner 最新指令）

Owner 要求持续开发，不以单个里程碑完成为停点；写代码最大化使用 GPT-5.6 Sol 并行，需要签署/部署时提交具体项给 owner。当前工具允许主代理加三个同时活跃子代理；三个新隔离 worktree 已满额工作。

| 负责人 | 当前代码切片 | 工作分支 |
| --- | --- | --- |
| Sol / forecast_fixes | SEC Item 5 解析与消费者链路交叉审查 | `item5-cross-review` |
| Sol / zero_base_fixes | 独立 verifier 在调用前排除所有生产者模型家族 | `verifier-route-independence` |
| Sol / insider_fixes | SEC 8-K discovery 的可审阅版本化提案 | `sec-8k-discovery-proposal` |
| 主代理 | 交叉审查、真实子进程回归、签署部署材料、集成验收 | `main` |

本轮首个持续开发验收点 `e7e06bb`：**完整 6,114 项 / 495.678s 通过（1 skip）**，当前 live 只读副本全部 12 步复演通过（67 schemas / 35 lanes / 38 entries / 0 escaped），wheel 428 个 Python/SQL 文件逐字匹配。详见 [验收记录](reports/continuous-wave2-integration-2026-09-10.md)。未部署。

原基线 `4c28816`（6,055 项通过）。各切片完成即接后续 HK 周调度、W5 market-proxy / 成本模板及 Item 5 交易计划；共享文件先协调归属，聚焦绿分批 commit，集成完整验证后 push。授权材料完成后及时发给 owner，签署不阻塞独立开发。

## 本轮交付（2026-09-10）

W4 六条待合分支与三个 GPT-5.6 Sol 修复 worktree 已集成，最终验证代码为 `2f64b3a`。完成预测不变量接线、分部 filing 去重、跨 US/HK 回购契约与港股行情、月度回购判断分组及费用/评分去重、首版档案先分类、独立 ZeroBase verifier、待授权分类及恢复、来源计划补行与 ask 契约。详见 [本轮集成报告](reports/resume-w4-integration-2026-09-10.md) 与 [清单当前状态 §6d](reports/parallel-development-plan-v1.0-2026-09-09.md#6d-2026-09-10-恢复后的集成进度覆盖-6b--6c-历史状态)。

**验证**：最终聚焦 277 项 + 57 项通过；**完整主线 6,055 项 / 503.007s，OK（skipped=1）**；首轮两个集成遗漏已修复并由完整重跑验证。最终隔离复演全部 12 步通过：66/66 schemas、35 lanes、38 tick entries、0 escaped。Python wheel 构建、connector inventory 一致性、cockpit JavaScript 语法均通过。此外已对当前 live 做只读新快照并重新通过全部演练步骤（827 MB 副本）；没有部署或修改 live，缺授权/开关不计作已激活功能。

**Git**：已按功能及时提交；主线全量通过，代码与交付文档统一推送至 `origin/main`。验收代码基准 `2f64b3a`，之后只有文档变更；起点 `694471c` 之前的 11 个提交保留在历史中。

## 下一步（本节覆盖此前暂停安排）

1. **运行激活与产品验收优先**：按 [更新后的 owner runbook](reports/owner-steps-after-deploy-v2.0-2026-09-10.md)，当前 live 新快照复演已通过，下一步核对并激活 11 个 may_write / 3 个 checkpoint 缺项、模型开关、retired verifier pin 与 tracking policy v2。部署后验收五家公司首版 dossier、DebateMap 与一轮判断，演练通过不能代替产物验收。
2. **F14 失败账本覆盖**：13 个初版协调器已合入，A/B 九条交叉修复完成（299 focused）；Reflection / ZeroBase / research task 再审修复完成（124 focused），Crowd source 正补日期/输入和权限边界。共享 replay 无写入、同刻顺序和单项 superseded 已完成。完整集成回归待最后切片冻结。
3. **F13 日缓存与 HK 周调度**：独立 `daily_buyback_tape` 治理提案、跨公司共享 invocation/artifact 的日缓存与重放校验已合入（118 focused + 6 hardening）；新能力默认未批准、未启用。HK closed-week 推理和迟到证据全组重判正在独立开发。
4. **W5 / D1–D9**：market-proxy 生产→索引→模型规格初版已合入（364 focused），正在补映射版本、权限、故障隔离及部署参数边界；随后接成本侧模板和 Item 5。HK universe、数字权威、慢背离窗口等原待裁决保持显式。周报投递和 Excel 导出继续后排。

本轮详细提交与 next step 见 [持续开发记录](reports/continuous-development-wave2-2026-09-10.md)。当前主线至 `e7e06bb` 已完成全量并进入本次 push；下一批成本模板、Item 5、模型安装和 HK failure ledger 仍在独立分支审查，不包含在 6,114 项结果中。未部署。
## 2026-09-10 恢复开发背景

已详细审读 PROJECT_STATUS、并行计划 §6b/6c、经济不变量、Chem 复盘、模型选择、既有资料及六份待合分支报告。原工作区干净，`main=694471c`，fetch 后领先 origin/main 11 个提交。以新隔离 worktree 承接 GPT-5.6 Sol 并行工作，避免接管旧 agent 目录；本节与 §6d 覆盖下方历史“只记录不动手”的暂停安排。

## 当前状态速览（2026-09-09 收盘）

**跑在 tick 上的 lane**（controller 每轮依次调用，各自一次一个 child）：
source discovery（AlphaEngine / web search / SEC filings index）、document extraction、mission stage、
claim review、SEC quarters、**statements（P13ak，新）**、**company model spec（P13am，新）**、
research plan、initial screen。

**账本里现在有什么**：
- 2,170 条 Claim、3,501 份文档、326 条有效指标观测（另有 170 条今日撤回，归属错公司）；
- **26 份季报、10,023 行报表行**（EPAM / ACN / DXC 各 8 份，CTSH / IBM 各 1 份，会随各自规格的 horizon 变深）；
- **9 份建模规格，覆盖 5 家公司**（多出来的 4 份是同一家公司在披露结构变化后重新决定的，见建模第二段末尾）；
- 5 份 Initial Screen 全部发布、gate 全过（ACN / EPAM / IBM / DXC 已 `gate_passed`，CTSH 仍 `entered`）。

**连接器**：12 个打包模板 —— `alphaengine`、`sec`、`sec-financials`、`cninfo`、`web-fetch`、
`gemini-web-search`、`guidepoint`、`roic-transcript`、`x-xreach`、`x-x-search`、`reddit-last30days`、
`xueqiu`。治理记录是**按 operation** 发的（一个 schema hash 只绑定一个 operation，所以一条批准不可能
被复用去放宽另一个），owner 均已就地批准。其中 `guidepoint` 身份已批准但 lane 未建；
`roic-transcript` 两条已批准但**来源接不通**（见下）；x / reddit / xueqiu 仍是 shadow。

**今日模型开销**：$7.14 / $100 日上限；其中建模规格 lane 约 $2。

**测试**：2,034 通过（1 skip），全量 3 分 03 秒（`python3 -m unittest discover -s tests -t .`）。已部署，health `ok`。

**owner 已定的边界**：AlphaEngine 维持 130 次/24h；建模阶段不进 Excel，导出时才连公式；
行情走 yfinance（免费源）；consensus 双路都走免费源（研报抽取 + yfinance analyst 字段）。

**并行开发**：分析师蓝图按 [并行开发计划 v1.0](reports/parallel-development-plan-v1.0-2026-09-09.md) 推进：
主 agent 定计划与集成，Opus 5 subagent 各自 worktree 写代码。进度账在该文档第 6 节。

## 2026-09-11 凌晨：七条交付到齐，按 owner 指示只记录不动手

owner 因限额指示：agent 返回后只记录修法，不动手、不派。七条交付（作者侧全量各自绿）：经济不变量层（已合入本地 main，
未 push，主线全量未跑）、美股 insider / buyback tracking、港股 hkex-filings 连接器、按分类的 driver 模板 + `market_proxy`、
失败分类 + cockpit 四格、ZeroBaseReview + 判断事后验证、部署复演 2（fail-closed 通过，对 `8717de0`）。审读产出 19 条待派修复与
9 条待裁决，全部在计划文档 6c。本地 main 领先远端 10 个提交（经济不变量合并 + 记录文档），按「全量绿才 push」规则未推。

## 2026-09-10（并行开发第二天）：认知层、演化层、对话层大部分上主线，部署演练通过

**main 已合入并 push（4,486+ 项测试通过）**：Wave 0、A/B/C/D、S1–S5、P14e、P14a daily tracking、C1 事件日历 + 事件桥接、
模型路由、C2 预算池 + tick 账本、P12a 档案 + P12f guidance、P12c DebateMap、P12d Deep Insight Gate、P14b + P14d 修订回路、
P15d ConvictionCall、Q2 周报 rubric + Reflection、抽取吞吐、ADR-0009 perception 退役、部署演练与 runbook、INT1/2/3 接线与种子、
stage-ladder（阶段状态跨 mission 版本折叠）、ask v2。

**部署演练结论**：55 个 schema 在 live 数据副本上全部通过，一个 tick 27 条 lane 零逃逸异常；owner 部署步骤见
`docs/reports/deploy-runbook-v1.0-2026-09-09.md` 与 `owner-steps-after-deploy-v1.0-2026-09-09.md`。**live 仍跑旧版。**

**owner 新增方向（09-10）**：既有资料（旧 Initial Screen、memo、Excel 模型）作为受治理的 `internal_prior` 来源，旧 screen 导入为
版本链 v0，Dalton 仍独立写 v1 并逐条判断旧关注点是否还成立；旧模型只提供假设区间，永不当 actual。

**09-10 下午续**：P14f 业绩季、reopen-ledger（重开成为阶段账本记录）、planner 日账本（planner 调用进日账本与四池）、
consensus P11b（财年末从「从不交 10-Q 的季度」推导；页首抽取 15 个目标价；两家独立券商规则）、authority 授权标志统一
（19 个 authority 共享按连接的标志）都已合入；main 5,055 项通过。P13-M3 敏感性与 consensus bridge、P12e 行业框架也已合入；main 5,313 项通过，已 push。owner 新增：cockpit 按环节选模型 +
模型目录随 openclaw 自动登记 + 模型被移除时自动回退并在 cockpit 通知（agent 开发中）。在修 review：prior-research。
第二次部署复演（当前 main 对 live 副本）与最终版 owner 步骤清单 v2.0 进行中。

**09-10 晚**：prior-research 合入（5,381 项通过，已 push）；model-selection 合入（作者侧 5,445 项通过；主线全量在跑，绿即 push）：
cockpit 按环节选模型、模型目录每小时随 openclaw 自动登记、模型被移除时按 tier 链自动回退并写 cockpit 通知（通知按当前状态算，
不靠 delta，`UNIQUE(profile_id, purpose)` 去重；未定价模型按声明上限 25 / 100 美元每百万入账，不再是 0）。
Chem 复盘对照分析写入 `docs/reports/chem-retrospective-implications-v1.0-2026-09-10.md`；由此派出 W4 六条切片：按分类的 driver 模板 +
`market_proxy`、经济不变量层、ZeroBaseReview + 判断事后验证、失败分类 + cockpit 四格、美股 insider / buyback tracking、港股 hkex-filings。
owner 09-10：部署与授权随时可做，时点由主 agent 定——复演 2 fail-closed 通过即部署。

**方法上的两次事故**：用量上限两次打断十余个 agent，全部从上下文恢复；一次误判「静默 = 死亡」造成同一 worktree 双写，
已裁决归属并写成规则。

## 2026-09-09（并行开发第一天，晚）：五条线合进 main，未部署

**合进 main 的**（`61f4255`，2,627 项测试通过，已 push；live 仍跑旧版，部署等 INT1 接线完）：
- **Wave 0 lane registry**：加一条 lane = 自己的模块 + `*_schema.sql` + 测试 + `LANE_MODULES` 一行；
  `writer_server` / `bounded_planner_driver` / `macos_launchagent` 从 registry 派生，行为逐字节零漂移（review 验证）。
  G 线 13 个词入词表、live 不授予；ADR-0007 / ADR-0008 accepted。
- **P11a / P11c 市场层**：yfinance connector（`daily_prices` / `analyst_estimates`，两条 proposed 治理记录）、
  `MarketPriceSeriesVersion`（每根 bar 绑 invocation 与 artifact 哈希；盘中 bar 标 provisional，收盘后重取）、
  `ValuationSnapshot`（公式冻结、`basis` 如实、拒绝 yfinance 基本面）。ACN 三年 752 根日线在临时目录跑通。
- **P12b Claim 索引**：`aspect`（12 词封闭词表，与 dossier 分节一致）、`as_of`、`importance`（filing > 管理层 >
  卖方 > 新闻）、按 period × basis 去重；live 抽样：22 filing / 571 管理层 / 217 卖方 / 1,360 新闻，
  ACN 同季三重引用收成一条 canonical。**ADR-0007 数字进 Ledger 走新的 `mission_figure_authority` 模式**：
  live 5 个 filing 数字全部重验通过并升成定量 Claim（policy 默认仍是旧规则，待 owner 重签）。
- **P13-M2 预测模型**：`ForecastModelVersion` driver → assumption → result 三层，Decimal 精确重算；
  estimate / actual 分格、`superseded_by`、五词 `change_reason`、`revise_assumptions` / `actualize_model` 入口——
  **authority 永不自己出版本**（owner：版本化是机制不是触发器）。live 四家发布 v1，IBM 因规格收入 driver 全绑 null
  如实拒绝，DXC 营业利润过零故税与净利 `unavailable`。
- **Q1 质量回路**：三份 rubric（initial-screen / ask-answer / company-dossier）、10 项确定性检查 + 有界 judge +
  独立 verifier、`QualityScoreVersion`、`AnalystJournalEntry`、20 例 golden；修了 P10c 残句与重复引用。
  确定性层在五份已发布 screen 上发现 **30 处残留引用标记**（ACN 12、EPAM 11、IBM 6、CTSH 1）。

**方法**：每条线一个 Opus 5 subagent 在独立 worktree 写，另一个 Opus 5 subagent 独立 review，主 agent 只定规格、
裁决、集成。五条线 review 共抓出 9 个 blocker（全部修掉后才合），典型的：盘中价被冻成收盘价；中文句式被当残句；
数字升级走了 ADR-0007 禁止的 transcript 模式；stale prior 的修订被记成决定。

**owner 今日新增的方向**（已进计划文档）：daily tracking 在 Initial Screen 过闸后默认常驻，频率有基线、由大脑调配；
sales note / wiki / 推特是 tracking 信息源；大脑要有 connector 能力地图；vision 回顾补了事件日历、预算池、
宪法 method 消费者、规划质量指标四项遗漏。

**同日更晚又合进 main 的**（`f6eec59`，3,437 项通过）：S2 Guidepoint lane（≤20 词引用限制对中文生效）、S1 sales-notes /
company-wiki 投喂 + `host_tool_runner`（正文归属：五家 231 条，九月 5 条）、P14e 专项研究（= 以 inquiry 为题的
BoundedPlannerLoop，$25/日池；卡住的 loop 不再每 tick 付费）、模型路由（目录同步退役不删除：5 退役 4 补齐；
brain astra → fable-5-1，cheap deepseek-flash → glm-flash → gemini-flash-lite，verifier 与 producer 不同家族）、
S3 雪球 / X / Blind（走 runner，50/日配额真实生效）、C1 事件日历（yfinance + 8-K 互证；estimated 日期开 preview
并标注未确认；ACN 10/1 T−22 触发）、S4 cn-hk-findata 六个 op（东财不重试；沪深融资余额单位差 1e8 已标注）、
**P14a daily tracking**：过闸即常驻；`ResearchEvent` 12 种；频率基线 + `TrackingCadenceVersion`；
`SourceCapabilityMap`；事件判断 lane 五词决定 + 独立 verifier；`price_divergence` + `ThesisReflection`（市场看法 vs 我们、
收敛 pathway、遗漏 debate、追加 tracking / 研究候选）。

**发现的结构性瓶颈**（不是代码）：ACN 有 885 份卖方文档但只有 5 条卖方层级 Claim——研报大多没被抽取成 Claim，
DebateMap 的「多空各 ≥2 独立来源」验收在此之前无法满足；2,170 条 Claim 里只有 22 条有 aspect，要等 P12b 的模型
标注跑完。行业主体一条 Claim 都没有。

**在飞的分支**：P12a 档案 + P12f guidance、P12c DebateMap（已交付在审）、P11b consensus 双路、C2 预算池 + tick
账本（已交付在审）、Q2 周报 rubric + Reflection（修 review）、INT1 cockpit + install.sh（修 review）。

## 2026-09-09（并行开发准备）：先把「加一条 lane 要碰 23 个共享文件」收成一行

owner 认可了蓝图的分法并定了三件事：行情用 yfinance；consensus 先从已入库研报抽、再用 yfinance
的 analyst 字段，AlphaEngine 上限不动；计划由主 agent 定，代码由 Opus 5 subagent 写。

**调查结论**。(1) openclaw 的 `stock-move-analyzer` / `sentiment-dashboard` 等 skill 都用 yfinance，
本机已装 1.2.0，ACN 三年日线、股本、市值一次可取；yfinance 还免费给目标价区间、评级分布与 EPS / 收入
consensus。(2) live transcript-spool 的 1,015 个 AlphaEngine 对象里 483 个可解析：ACN 8 份、EPAM 8 份、
CTSH 5 份带目标价的研报，券商四到五家，够做互证；IBM 只有 RBC、DXC 只有 TD / RBC，如实标缺口。
(3) 全量测试 2,034 项通过，3 分钟。(4) **P13ao 已经做完了蓝图的 P13-M1**（规格 × 序列 join），
模型层从 M2 起步。(5) 只读调查了 statements / model spec / initial screen 三条 lane 的接线：
一条新 lane 要改 `writer_server.py` 的 10 个区域外加 13 个共享文件；仓库没有 lane 注册机制，
唯一的子类化接缝是 P13aj 的 `LaneChildLauncher`。

**因此 Wave 0 是串行的**：建 lane registry（writer / driver / launchagent 三处从它派生）、schema 文件
改 glob 打包、模型用途与配置名可登记、`may_write` 与 `CHECKPOINT_KINDS` 一次加齐蓝图 G 线的词、
ADR-0007 草稿。之后 Wave 1 四线并行：市场层、Claim 索引、预测行、质量回路。详见计划文档。

## 2026-09-09（建模第三段）：报表行变成能用的季度序列

**P13an。** 报表里报的东西和模型要的东西不是一个形状，两者之间的距离正是表格出错最多的地方。

**第一件事是不要把一个季度加到它自己的累计上。** 一份 10-Q 同时报单季和年初至今，两者**同一个截止日**：
EPAM 2026 Q2 收入 14.15 亿、上半年 28.15 亿，wire 上唯一能分开它们的是 `period_start`。加起来就是把半年记了两遍。

**第二件事是做 filing 不替你做的算术。** 只报累计的公司会给你九个月和六个月，但从不给第三个季度——
它是两者之差，是**推出来的**数，必须标明，并且要写清它靠哪两份 filing 算出来的。
第一版推不出**第二**季度：只把长期间喂进了减法，六个月的数字没有东西可减。
一年的第一个季度**同时就是**年初至今，所以每一个 duration 都是候选。

**第三件事是重述。** 同一个季度会在多份 filing 里出现（先作为当期，再作为一年后的对比期），数字不总是一样，
所以取**最近报出的那一版**，且每个值都记着它来自哪份 filing。

**两件它拒绝做的事。**
- **不猜日历**：第一版的缺口报告自己造了一张网格（往回退三个月、保留日号），然后宣布 EPAM 缺
  `2026-03-30`——那不是任何人的季度末。现在缺口是**从数据里读出来的**：相邻两个季度之间差了多少天。
  对一个历史全部来自 10-Q 的公司，它正确地报出"每年一个洞"，因为 10-Q 从不覆盖第四季度。
- **不发明数字**：既没报出、也推不出来的季度就是没有。有洞的模型能修，有假数的模型找不出来。

**实测（live 账本）**：EPAM 与 Accenture 各 11 个报出的季度，Accenture 的**财年日历**（9–11 月、12–2 月、
3–5 月）不用告诉它也读对了，缺口正好落在只有 10-K 才有的第四季度上。

**顺带修掉了下面一层的一个缺陷。** parser 把一部分**带维度**的行的 breakdown 标志报成了 0——live 里
Accenture 的 Consulting / Managed Services 拆分带着 `srt:ProductOrServiceAxis` 却被当成合计，
于是同一个季度有三行都自称合计，任何序列都会把合计和它自己的组成部分加在一起。
**带维度的行按定义就是 breakdown**：写入时修好，读出时也派生一次——账本是 append-only 的，
已经落库的行改不了。

## 2026-09-09（建模第二段）：每家公司自己的建模规格，以及它决定要多少历史

**P13al + P13am。** 通用三表模板不是模型。Accenture 的关键是 bookings 与 utilisation，
IBM 是软件结构与现金，DXC 是一本在缩的合同簿被管着做利润——同一个行业、三个不同的问题，
一视同仁的模板一个都答不上。

**所以框架钉死在代码里，内容交给判断。** 四个问题：什么驱动收入（量/价/结构/分部/合同簿/外部变量）；
成本有哪些、**行为**是什么（跟收入走的和跟人头走的是模型里的两行，哪怕 filing 里是一行）；
这家公司**哪几张表**需要预测；市场真正盯着而 GAAP 不报的**运营指标**是哪些（new bookings、book-to-bill、
utilisation、attrition）。

**两条规则不在提示词里，在代码里**：
- **一行只能落在这家公司真的报过的 concept 上**。差一点也直接拒绝、不修补——把 `us-gaap:Revenue`
  悄悄映射到 `us-gaap:Revenues` 正是模型开始引用不存在的行的方式。
- **每一条都要有理由**，而且要是关于这家公司的。没有理由的规格是穿着判断外衣的模板，没法跟它争论——
  而"能被争论"正是它要被 review 的全部意义。

**有一条判断不归模型**：损益表永远是 required。资产负债表要不要预测是关于这家公司的判断，
能不能预测收入和利润率不是。

**实测（IBM，483 行 10-Q，最强路由模型，$0.24）**：三个经营引擎（软件/咨询/基础设施），**融资单列**，
为 Confluent 收购建**并购收入桥**，恒定汇率与有机增长分开，咨询 signings 与 book-to-bill，主机产能，
以及明确写出"全集团软件净留存率**未披露**、必须估计"。最后这一条正是 `disclosed` 是必填字段的原因。

**深度由规格决定。** 统计报表 lane 原本每家取一个季度，因为它只会想要一个季度。IBM 的规格要 20 个季度
（把主机换代周期和底层业务分开）；一家合同簿平稳的咨询公司要得少得多。现在
`horizon.historical_quarters` 决定取几份 filing，上限是一个 child 一次能解析的量，下限一个季度——
那正好够拿来定一份规格。

**预算：真正卡住的是每次调用的预留，不是日上限。** router 按**允许的输出**和 **prompt 字节数**（不是 token）
估价，所以预留是实际花费的好几倍：IBM 那次实花 $0.24、估价 $0.46，按旧的 $0.60 上限**在发出前就会被拒**。
产生那些旧数字的 $5 日上限现在是 $100，留着只会在状态变大的那天买到拒绝。已上调，并且把过时的理由
**换掉**而不是留着误导下一个读的人。（另外发现 `raise_day_budget_cap.py` 只改了三个模型配置里的两个，
漏掉的正是规格 lane 也在用的那个。）

**上了 tick。** 这条 lane 和这里所有获取型 lane 都不一样：**它没有队列**——"该决定什么"是每个 tick
从账本里**算出来**的（有报表、且没有针对当前披露结构的规格），所以没有东西会卡住，
静默才是它的常态。五家公司拿到五份规格之后它就不动了，直到某家公司报了新东西、结构哈希变了，
才**只**重新决定那一家。

**上线第一个心跳就抓到一个 bug。** 选公司时用的投影**不带 ticker**，而 run 存规格时用的投影**带**——
ticker 是 state 的一部分因而也是哈希的一部分，于是选择器永远看不见它刚刚产出的答案。
**代价不是钱**（child 重建带 ticker 的 state、找到已存的规格、免费重放，live summary 是
`spec_status: unchanged, cost_micros: 0`），**代价是进度**：IBM 每个 tick 被重启一次，另外四家永远排在后面。
修法是 `choose_company` 直接返回它选中的那个投影。所有测试都从这个 bug 里穿了过去，因为每个测试只有投影的一边，
所以回归测试是端到端而且直白的：**决定、存下、就没有什么可决定了**。

**一个值得记下来的性质**：state hash 绑定的是**我们对披露的读法**，不只是披露本身。
今天改了 breakdown 标志的派生（见建模第三段），五家公司的结构哈希全都变了，于是全部重新决定了一遍
（约 $1.25）。这是对的行为，但意味着**改投影 = 重决定所有公司**，改之前值得知道这一点。

## 2026-09-09（findata 收尾）：统计报表 lane 上了 tick，五家公司的季报入库

**P13ak。** 连接器、治理、child 都能手工跑通了，但没有任何东西去调度它们。这一段是让它们跑起来的部分。

**账本这一层是新的**：`coverage_mission_statement_filings` + `coverage_mission_statement_lines`，
append-only，每行一个科目一个期间，带着公司自己披露的**结构**——层级、父科目、是不是分部拆分。
那个结构正是模型建立在其上的东西，也正是"一次问一个 concept"的 company-facts lane 给不出的。
一份 filing 每家公司只入一次，所以重解析同一份 10-Q 是同一份 filing，不是第二套行。

**协调器里的三条规则，每一条都是这套代码已经付过学费的**：一次一个 child、已有在途的公司不再排队；
**先记录再结算**，中间崩了就重放成 no-op；反复失败的公司不再占用那唯一的槽位。
dispatch 的状态从一开始就带**终态成功**——SEC filings 那张表当初没有，缺一个 `succeeded` 把那条 lane 冻了一整天。

**重试需要 attempt 号**：没有它，请求本身就是身份，一条因为治理还没批准而被拒的 dispatch 会永远被拒，
事后批准了也不会有任何变化。

**四个 launcher 各抄一份的 ticket 机制现在共用了**（`lane_child_launcher`）。值得共用的不是"怎么起进程"，
而是**重启之后 `status()` 该说什么**：进程没了而 ticket 还写着 `running` 的，是 **orphaned**——
不是失败也不是成功，而且**绝不能**从旁边那个 summary 文件读成成功，因为 summary 可以是一个写完就死掉的 run 写的。

**上线后连着掉了三个坑，都修了**：
- **EDGAR identity**：`edgartools` 拒绝没有联系方式的 identity。已改成本 Core 对外请求一贯公布的地址。
- **归因方向反了**（更要紧的一个）：分类器列的是"哪些是我们自己的错"，其余全算公司的——于是我们自己
  adapter 里的一个 `AttributeError` 看起来和"这家公司没法服务"一模一样，三个 tick 就把 IBM 的重试预算
  花在了跟 IBM 毫无关系的事情上。**真正能归给公司的失败是短而可枚举的**（它没报、或者报的东西没有 XBRL），
  所以列出这些，其余（包括**没有记录原因**的失败）都算我们的。无法归因的失败不是指控公司的证据。
- **parser 其实只在"深度 1"下工作过**：`latest(1)` 返回一份 Filing，`latest(n)` 返回一个集合，
  而集合不是 list——于是整个集合被塞进一个单元素 list，然后被问它只有单份 filing 才有的 XBRL。
  第一次有模型要历史，它就在每家公司上失败。已按"这个对象能做什么"而不是"它是什么类型"来判断，
  并用一次真实的 IBM 八季度抓取验证过。

**当前 live**：26 份 filing、10,023 行；EPAM / ACN / DXC 各 8 份，CTSH / IBM 各 1 份（会随各自规格的
horizon 自己变深）。

## 2026-09-09：roic 接不通——整站在 Cloudflare 后面

**结论先说：这条连接器不能用，没有写 launcher。** 前一天签的身份和两条治理记录建立在一个**未经验证的**
前提上——"一份纪要就是 roic.ai 上的一个页面"。今天实测：`www.roic.ai` 上的每一条路径，
行情页、纪要页、JSON 接口，**连 `robots.txt` 本身**，对普通客户端都返回 403 与 Cloudflare 人机校验。

所以无凭据的 HTTPS 客户端读不到这个来源。**绕过那道门不是该建的东西**——那是站方有意放的；
而且 `robots.txt` 都读不到，连它自己的抓取政策都无从查证。

**这条是我的错**：先签了治理才去验来源。已把这个结论写进 `roic_transcript_core` 的模块文档，
不是留着让下一个人重新踩。**撤回那两条批准、还是改走抓取服务（换 transport，得发新版本重新批准），
是 owner 的判断。**

（这条连接器当初的动机是"AlphaEngine 130/天 用满时 CTSH 就卡住"。那个约束仍然在，
owner 已决定先维持 130。）

## 2026-09-09（findata 第一段）：连接器身份已签，child 还没写

**已完成并部署**：`connector:sec-financial-statements`（第 11 个连接器）的模板、输出契约、身份与
**owner 已批准**的治理记录；`edgartools` 作为可选依赖 `[sec-financials]` 进入安装。

- **一个来源、两种读法**：与 filings 连接器共用 `source:sec-edgar` 与同一个 source hash；连接器、schema、
  批准各自独立——与 P10e 拆 `list_filings` / `get_company_facts` 同一条原则。
- **不取代读原文**（owner 的判断，也确实如此）：parser 够不到的仍然只能从原文取，那条 lane 保留。
- **上线的是规范化后的 wire，不是 parser 自己的形状**：parser 把每个期间当成一列
  （`{"2026-06-30": 2814828000}`），键本身是数据，闭合 schema 描述不了。适配器改成
  每 (statement, concept, period) 一行、显式 `period_end`；数字是**字符串**（float 不是报出来的东西）；
  `level` / `parent_concept` / `dimension_axis` 是**必填**——能省掉这几项的 wire 就又退回成 company-facts 了。
- **owner 认下的代价写进了模块而不是暗示**：parser 自己发 HTTP，这些字节没有经过 Dalton 的 transport 验证；
  原始输出哈希入库、每行带 accession，任何数字都能回到 SEC 复核。

**写 child 之前查到的一件要紧事（会决定 child 怎么写）**：
`edgartools` 的**报表视图**（`statements.income_statement().to_dataframe()`）有**结构**
（level、parent_concept、dimension_axis/member），但**没有单位**；
而**事实视图**（`xbrl.query()`）有**权威的 `unit_ref`**（如 `usd`）、显式 `period_start`/`period_end`、
`decimals`、以及未经转换的 `value` 字符串，但**没有层级**。
所以 child 必须把两者按 (concept, period_end, dimension) **join** 起来。
**不能默认 USD**——那正是这套系统一贯拒绝的"拿推断冒充披露"。

**还没做**：child CLI（join + 规范化 + 契约自校验 + fixture）、launcher、writer 接线；
以及"数字落到哪里"这个真正的设计决定（Claims 还是 Model Input Ledger）——它和建模那一段是同一个问题，
留到和 owner 谈建模时一起定。

## 2026-09-09：OpenClaw 现有 skills 里值得接进来的能力（调研，未实现）

owner 问：现有 82 个 skill 里还有哪些值得做成 connector；以及能不能直接接 findata analyst，
省掉逐份 SEC filing 爬取。**答案是能，而且比预期的更值。**

**findata-analyst（底层是 `edgartools`）**——一次调用拿到的东西，实测 EPAM 最新 10-Q：
- **完整三表**（income / balance / cash），一次 `financials --statement all`；
- income 表 **77 行、22 个顶层科目**，就是这家公司真实的费用结构（Cost of revenues、SG&A、
  D&A、Humanitarian Commitment……），以及收入按定价方式拆分（Time-and-materials / Fixed-price / Licensing）；
- **6 个 dimension 轴**的分部数据（地理、业务分部、定价方式、consolidation items……）；
- 每行带 XBRL concept id、层级、parent、balance(credit/debit)、weight、preferred_sign；
- 整份绑定到 **accession `0001352010-26-000046`**，filing_date / report_date 齐全。

对照今天 Dalton 自己的 SEC lane：一次 dispatch 取**一个**指标，跑通 5 次得到 6 条
`quarterly_revenue_yoy_growth`。差距是数量级的。

**这直接改写建模的设计**：owner 原本设想"大脑决定抽取哪些字段（费用有哪些科目、revenue driver 是什么）"。
但这些**公司自己在 XBRL 里披露了**——费用科目就是 income 表的行，revenue driver 就是 dimension 轴。
大脑的工作因此从"发明一套字段"变成"在已披露的结构里挑哪些重要"，这比原设想更可靠，也更省。

**但有一个诚实的代价，需要 owner 定夺**：Dalton 的规矩是每个数字都绑定到经
`ConnectorTransportExecutor` 哈希过的字节。edgartools 在库内部自己发 HTTP，绕开了那条链路。三种做法：
1. **当作 connector，记录其输出**：子进程跑，把它的原始 JSON 当 artifact 哈希入库，claim 绑定
   (accession, concept, period)。溯源链就从"Dalton 亲自验证 SEC 字节"变成"信任 edgartools 的解析"，
   但 accession 在手，任何一个数字都能回到 SEC 复核。**最快，溯源略弱。**
2. **把 edgartools 放进 Dalton 自己的 lane、让它的 HTTP 走 Dalton 的 transport**：保留字节级验证，
   前提是 edgartools 允许注入 transport（未验证）。
3. **扩自己的 company-facts adapter**：Dalton **已经**在取 `companyfacts` 并哈希入库了，缺的只是
   **报表结构**（哪一行是哪个科目、层级、dimension）。补这一层就全程留在现有治理里。**最干净，工作量最大。**

**其余值得接的（按对当前阻塞的价值排序）：**
1. **`roic-transcript`（电话会纪要，roic.ai）**——**当下最高杠杆**。CTSH 的 Initial Screen 正卡在
   earnings_calls 1/4，而 AlphaEngine 24h 用量贴着上限（131/130）。这是一条**独立于 AlphaEngine** 的
   纪要来源，直接解开那个阻塞。
2. **`xlsx`**——"读写电子表格并**保留公式**"。正是 owner 说的"建模阶段不碰 excel，导出交付物时再连公式导出"。
   建模阶段的导出端就是它。
3. `guidepoint-transcript-search`——Guidepoint 治理刚签完，这个 skill 展示了预期用法。
4. 之后：`13f-tracker`（持仓）、`employee-reviews`（Blind/Indeed/Glassdoor 另类数据）、
   `company-filings-alert`（覆盖公司 filing 监控）、`findata-return`（股东回报）。
   `cn-hk-findata`（cninfo + AkShare）对当前 US IT services universe 用不上。

**注意**：`ratios` 这类**算出来的**值（gross margin = GP/Rev）不能当披露值入库——
model_discipline 明确"不以残差或比例分摊冒充披露值"。要接的是 `financials` / `facts` 这些披露值。

## 2026-09-09（Guidepoint 第一段）：身份与治理已签，lane 卡在一个命名冲突上

> **后续（同日）**：下面描述的命名冲突已在 P13ae 解决——host bridge 改成按 `(source_ref, operation)` 解析，歧义直接拒绝。本节保留当时的判断作为记录；lane 本身（child CLI / discovery plan / launcher / 接线）仍未建，见「下一步」第 10 条。

**已完成并部署**：Guidepoint 的 identity + 两条 owner 已批准的治理记录。

- **两条记录而不是一条**：`search_library` 读索引、`get_transcript` 读文档，是两种权限；schema hash 只绑定一个
  operation，一条记录复用到另一个就等于悄悄放宽了批准范围。AlphaEngine（P9d-1）和 SEC（P10e）都是这么拆的，
  这里跟随而不是另发明一种形状。source hash 共用——同一个 Guidepoint 就是同一个来源。
- **结构上无凭据**：host-owned MCP transport，模板声明 `credential_material: forbidden`，permissions 里只有
  credential *slot* 而没有任何 material；自身无网络、无 Core 访问，唯一的写是 raw sink（字节先哈希再被读）。
- 仓库出 proposed、owner 就地 approve；测试断言**出厂记录永远是 proposed**（出厂即 approved 等于替 owner 签字），
  并断言其哈希仍与打包模板一致（漂了的记录会在机器上 load 时被拒，那是最糟糕的发现时机）。

**下一段卡在哪（真问题，不是没时间做）**：`live_mcp_connector` 有一张 host-bridge 注册表，
**按 operation 名字解析该走哪个 host tool**，并且有一条断言要求 operation 在所有 bridge 间全局唯一。
AlphaEngine 有 `search_library`，Guidepoint 也有 `search_library`——直接加进去会踩到那条断言。

那条断言是**有用的**（它防的正是"一个调用被路由到另一个 provider 的工具"），所以不能削弱它。
好消息是数据里已经有明确的区分键：runner wire 里带 `bridge_ref`，schema ref 也是按模板命名空间的
（`schema:connector-inventory:guidepoint:search_library:input:0.1`）。
**做法**：按 `bridge_ref` 解析、再校验 operation 属于该 bridge；断言从"operation 唯一"改成
"(bridge, operation) 唯一"。三个调用点 + 若干测试（现有测试断言 `search_library` → AlphaEngine）。
这是一条安全相关的路由路径，值得单独一轮仔细做，不适合塞在别的改动尾巴上。

之后才是 child CLI（`guidepoint_search_cli` / 取纪要）、discovery plan、launcher（`_SearchLauncherBase`
子类，约十几行）与 writer/installer 接线。另外 mission 的 `source_plan` 里 `source:guidepoint` 还是
`not_connected`，要接通需要 owner 发一版新的 mission。

## 2026-09-09（收尾）：交付物由谁来写

**P13ad 交付物有了自己的模型路由。** Initial Screen 一直是用**抽取模型**写的——不是谁选的，是 launcher 被顺手
传了抽取的 config。那个模型是为"从一份 filing 的一个窗口里便宜地抠出一个数字、重复几千次"选的；写交付物是另一回事，
而且差别正好落在最吃辩证能力的几节上：live 里 S4（风险与 Anti-thesis）出来是 `dropped_unsourced`，
S5（Relevance to Universe）发布时**一个 figure 都没有**。

- 交付物现在有自己的 routing policy，理由和 planner 当初一样：两个活共用一条钉死的 policy，就永远不可能不同。
- policy 那套机制改成**参数化**而不是复制：planner setup 本来就是这套东西，再抄一份只改两个名字没有意义。
- **不设就什么都不变**：没有 `DALTON_DELIVERABLE_MODEL_PROFILE` 时，screen 仍然用抽取模型，逐字节和以前一样。
  一份 screen 抽取模型约 $0.013，前沿模型约 $0.6–1.0；小，但是常驻成本，该由 owner 选而不是继承。
- 已按 owner 的判断启用 `profile:gpt-6-astra`。

## 2026-09-09（下半）：Initial Screen 第一次真的写出来了

**结果**：五家公司全部产出并发布 Initial Screen，gate 全过（source_base / number_provenance /
key_driver / street_and_risk）。EPAM 21 个 figure、ACN 18、IBM 16、DXC 14。一份约 0.013 美元。
这是这个任务的**交付物本身**，此前从未产出过。

SEC 数字接通之后，挡在交付物前面的是另外三个 bug：

1. **P13aa 同一个问题问第二次算冲突，所以八节一节都写不出来。** WorkOrder 的 id 按（用途、request_id、
   任务版本、prompt）内容寻址，**故意不含时钟**——同一个问题就该是同一份工作。但 scheduler 哈希的是整个
   wire，而 wire 里带着一个 wall-clock `created_at`：同一个幂等键、不同的哈希，这正是 scheduler 对"冲突"的
   定义。报错说"ask again"，而再问一次只会再生成一个新时间戳，**永远不可能好**。自 09-07 起每一节每一轮都死在这里。
   改法：时间戳从 id 所依据的同一个东西（任务版本）派生，身份和自己一致。
   **只改定义还不够**：旧定义写下的键仍然存着旧哈希，改正后的请求会和自己的历史撞车。所以身份定义也**带版本**了
   （和 SEC template registry 一样的做法）——定义变了就是另一个身份。
2. **P13ab 打开 store 本身可能直接失败，而一个"偶发失败"的测试一直在说这件事。** `test_connector` 的并发配额
   测试今天挂了两次、单独跑又过，看着像时序噪音。把它的失败信息弄清楚（barrier 的超时是为了制造竞争、不是为了
   限制测试时长；再断言两个线程真的跑完了）之后，露出的是真实原因：worker 死在 `DaltonStore.__init__` 的
   `database is locked`。**`busy_timeout` 管不到 `PRAGMA journal_mode`**——另一个连接持锁时 SQLite 直接拒绝，
   不会调用 busy handler。代码正好依赖了相反的假设，注释还写得很肯定，并且引用了那个一直在失败的测试当证据。
   这不只是测试问题：writer 一直握着 core.sqlite，每个 lane 子进程都要打开它。
3. **P13ac 数字的长度上限比系统自己写出来的数字还窄。** 一个 figure 的 text 就是 Claim 的
   `normalized_statement`，而 Claim 契约对它**没有上限**；SEC lane 写出来是 205 字符，这边卡 200，于是 EPAM
   每一个数字都被拒，交付物永远发不出去——而且 Claim 是 append-only，事后没法改短，只有消费侧能动。
   **截断比放宽更糟**：`unsourced_numbers` 是从这段 text 里把数字读回去的，而这些句子把数字放在最后，截断会
   把数字弄丢，然后把引用它的正文报成"无出处"——错误答案比报错更坏。

**今天第二次出现同一个形状**：两个必须一致的东西是分开算的（id 不含时钟、哈希含；生产端无上限、消费端有上限）。
改法同样是让它们**由同一处派生**，而不是记得同时改两处。

## 2026-09-09：SEC 财报抓取停了一整天，五个 bug 叠在一起

**结果**：修完之后，季度财报数字第一次真的入库——IBM 1.46%、ACN 5.95% / 5.44%、CTSH 7.46%、EPAM 18.04%
（quarterly_revenue_yoy_growth，全部来自 SEC 原始 filing，走完整条权威链）。SEC dispatch 的 `succeeded`
从 **0 变成 4**；五家公司的 quarterly_financials 从 12/20 变成 14/20 并在继续。

五个 bug 一个挡着一个，每修好一个才露出下一个：

1. **P13z-1 connector profile 冲突（73 次 dispatch 全灭，整整一天）。** company-facts lane 问的是"我是不是这个
   connector 上最新的 profile"，而 `connector-profile:sec-filings-index:v1` 挂到了同一个 `connector:sec-edgar`
   上并成了最新——于是每次运行都想在一个**已经钉死旧版本的幂等键**下写一个新 profile 版本。
   **问题出在问错了问题**：兄弟能力共用一个 connector ref，不构成"给我自己的 profile 分叉一个新版本"的理由。
   正确的问题是"我存在吗"：存在就原样重放，只有真的是新的才续链。
2. **P13z-2 修好 connector 也没用，重试预算早就花光了。** 每份 filing 最多试 3 次（每次把窗口放宽一天，因为
   重排同一个窗口只会重放同一个失败）。这个道理只在"失败属于这份 filing"时成立——而 connector 冲突对**任何**
   窗口都会同样失败，它把五家公司需要的每一份 filing 的三次机会都花光了，一次都没碰到 filing。
   现在尝试可以被**作废**（append-only，记下是为哪次故障豁免的；dispatch 本身不删，只撤回它对预算的占用）。
   **刻意不自动**：猜"这次失败不怪 filing"正是让一份真的取不到的 filing 被永远重试的方式。
3. **P13z-3 输出契约拒绝了它本该取的 filing。** SEC 只给"最新报告该期间的那份 filing"分配 calendar frame，所以
   后一份 10-Q 重复的去年同期那一行没有 frame。adapter 早就为此放宽过（注释写着：要求 frame 会让所有历史 filing
   都不可用），**但冻结的输出契约没跟着放宽**，于是 adapter 吐出 null、resolver 拒绝。
   与"指标发现漏了闸门"同一类：一个决定只改了一处。放宽契约会移动 schema hash，因此需要 owner 重新批准
   （`sec-company-facts:v3`，仓库出 proposed、owner 就地 approve），并 republish descriptor。
4. **P13z-4 plan 绑定契约，契约换版就是另一个 plan。** executor 拒绝 `connector_profile_hash` 与打包模板不符的
   plan（正确）。但 lane 的运行身份是（lane, 公司, 窗口, run_key），**不含契约**，所以换版之后重跑一个窗口会重放
   一个永远跑不了的 plan。这一条还和"作废尝试"互相咬：窗口宽度是按尝试次数算的，把预算还回去也把窗口倒回去了。
5. **P13z-5 问题文本要跟身份一起走。** 身份加了契约版本、backlog 问题文本没加，于是新契约下的运行用新的选择键
   去选一个旧契约运行已经占掉的问题——"only an open question can be selected"，五家公司同时挂。现在问题文本
   **由身份派生**，两者按构造一致（靠纪律是守不住的：今天守住了，下次身份再长一个字段就守不住）。

**顺手补上的两个诊断，当天就派上用场：**
- **settlement 现在记录失败原因。** `outcome` 只回答"跑完了没有"，不回答"成没成"；73 次全死在同一个字符串上，
  而那个字符串只存在于没人读的 summary 文件里，看起来像 73 个互不相关的失败。现在 `failure_reason` 落库
  （additive migration，老行是 NULL——现在编一个理由比留空更糟）。修完第一个 bug 后，**下一个 bug 是它直接报出来的**。
- **`sec_dispatch_outcomes()` 回答"这些到底成了没有"。** live 当时读出来是 **73 settled / 0 succeeded**，
  而账本看起来一切正常。planner 的状态里按公司带上了这个数——只看 deficit 会读成"还没取"，于是它一遍遍下
  同样的取数指令，下了一整天。

**教训（今天重复出现三次）**：一个决定只改了一处。adapter 放宽了、契约没放宽；身份加了字段、问题文本没加；
预算还回去了、窗口没考虑。修法都是**让两者由同一处派生**，而不是记得同时改两处。

## 2026-09-09：第三条 pass，和一条把整家公司抹掉的异常

- **P13y 指标发现从来没装归属闸门。** 读一个窗口有三条 pass：数字（P13c）和定性（P13i）都拒绝"文档里从没出现过
  这家公司名字"的来源，**指标发现没有**。它被漏掉是因为它不写 Claim——但两条观察就构成一条 requirement，而
  requirement 正是数字 pass 接下来要在**这家公司自己的财报里**去找的东西。从别家公司电话会里学到的指标，会让后面
  每一次（付费的）阅读都去找一个这家公司根本不披露的科目。live 496 条观察里有 **170 条**来自另外两条 pass 早已
  判定不归属的 31 份文档。
- **哪些是错的不需要重新判断。** 运行中的系统已经逐窗口判过：每一份 extraction summary 里的 `not_attributed`
  就是那次判断，落在磁盘上。`scripts/retract_unattributed_metric_observations.py` 读这些判决、映射回文档、
  撤回从中学到的观察。撤回而非删除，理由留档（`coverage_mission_metric_observation_retractions`，append-only，
  与数字撤回同一套规则；被撤回的观察任何读取都不再返回，重新提出也不会复活）。
  **已执行：170 条撤回，19 条 requirement 随之消失**——包括 IT 服务公司名下的 `adjusted-ebitda`、`net-cash`、
  `ebitda-margin`，这些本来就不像是这个行业的口径。
- **一条指标的分歧，抹掉了整家公司。** `establish_requirements` 遇到"同名不同单位"时**抛异常**，而每一个调用点都
  `except Exception: return ()`——于是四家公司里有三家的 requirement 列表是空的，**且没有任何地方报错**。IBM 有
  176 条观察、0 条 requirement，只因为两份文档对 net retention rate 该用 percent 还是 ratio 意见不一。
  分歧是真的、值得看见，但它只关乎那一条指标：现在只把有争议的那条排除，其余照常成立，`contested()` 说出谁在
  分歧什么。**修完这一条，三家公司恢复了 52 条 requirement。**

## 2026-09-08：数字、大脑与一天的事故

**能力上：**
- **P11q–P11u 数字抽取成链。** 指标发现（读卖方研报/电话会只取**名字**不取值，两份不同文档互证才成立需求）→
  按名字问数字 → 数位与口径逐字核对所引原文。live 已学到 99 个指标（Adjusted EBITDA、Rule of 40、
  Banking Solutions revenue growth (FXN)、Square US GPV growth……都不是谁事先列的）。
- **P11v/P11w 每个数字带出处等级。** `company-filed-document`（公司自己披露）与 `earnings-call-transcript`
  （电话会口述）**都留**，但不等价；等级由文档类型推导，不问模型"你有多确定"。落在 append-only 的
  `coverage_mission_document_figures`，连同所引原文与 manifest 哈希，任何人可复算。
- **P11x 驾驶舱直接读这张表**，不等 Ledger 接纳；口述数字在页面上带另一种标记。
- **P13a/P13b/P13f 大脑的"看得见"与"能决定"。** `research_state` 把目标、每家公司每个清单项的
  required/held/deficit/blocked 与原因、已有数字、市场在引用的指标、预算与花费拼成一个 ~2,400 token 的对象并哈希；
  `research_planner` 读它产出带理由的排序。**固化工作流不可被模型改写**：Initial Screen 要几个季度写在代码里，
  planner 只能在其之内排序（directives）与在其之上追问（inquiries）。
- **P13f 行业成为独立主体。** 发现计划本来就问行业问题，但**是按公司各问一遍**，所以每份市场报告都被记在某家公司名下。
  按行业口径数：`industry_demand` 91 份 / 需要 3 份，`competitive_landscape` 132 份 / 需要 3 份，另有 83 份在排队——
  223 份文档满足 6 份的需求。这就是"为什么一直在取网页"的一大半，而且此前没有任何地方在数它。

**修的事故（多数是自己造成的）：**
- **P12a Core 不是 WAL。** 任何读事务都挡住写，writer 30s 请求超时，controller 把整条 lane 报成
  `unavailable:RemoteError`——抽取与发现两条 lane 暗了几个小时。
- **P12b SEC dispatch 没有终态。** `status` 的 CHECK 只有 `pending/launched/rejected`，跑完的 dispatch 永远停在
  `launched`，而季度调度器"有未完成 dispatch 就不排队"——五家公司 35 条 dispatch 卡了一天，**所有公司财报数字差一期**。
- **P12c/P12d 页面上的上限不是真的上限。** 先是硬编码字符串 "每 24 小时最多 30 次"；改成读任务预算后又变成显示 130，
  而**真实生效上限仍是 30**——`MAX_CALLS_PER_WINDOW` 这个常量比 owner 签的预算更紧，且一直沉默。现在显示生效上限
  并说明是谁压的，且该常量可配（`--alphaengine-owner-call-cap`）。
- **P12e–P12g discovery tick 活得比 writer 久。** 采集循环按"每次采集"限时而不是整体限时（12×90s 在 30s 的请求里），
  且一个 writer op 串行跑三条 lane（各 20s = 60s），最后真正的凶手是两个 launcher 在超时时**抛异常**而不是返回 None。
  我在读 traceback 之前猜了两次。
- **P12h/P13c 抽错公司的数字。** 自由文本检索把海尔欧洲业务电话会与 EOS 电话会归到 EPAM 名下，抽取忠实地记下
  "EPAM revenue = 14.3 billion RMB"——每一位数字都对得上所引字节，字节讲的是别人。**不按公司过滤检索**（行业报告
  本来就没有公司标签），改为在取数处判断：文档必须点到这家公司的名字（对 live 字节验证：那两份文档从没出现过
  EPAM），且 prompt 明确告诉模型主体是谁（此前只传了 CIK ref，模型无从察觉自己在读别家公司）。
- **P12i 错的数字已撤回。** 7 条里 6 条是错的（4 条主体错、2 条同一事实重复）。撤回而非删除，理由留档。
  重复的根因是"同一事实"此前把 quote 算进身份，且 "Fiscal 2025" 与 "fiscal 2025" 被当成两个期间。
- **P13e 一句话把整条 pass 弄死。** 加了主体说明后 prompt 长了 1.5KB，估算 16,081 tokens 撞上 16,000 上限，
  路由把 29 个 profile 全判不合格，而 pass 只报 `no_result`——看起来像"没找到"，实际是"完全没跑"。
- **P13g Discord 被 agenda shadow 刷屏。** P12b 解开 SEC 调度后，每条 filing dispatch 都发一张 shadow 卡，
  两小时 25 张同一句话。规则改为：**只有真正权衡过备选的 cycle 才发卡**——lane cycle 永远只有 1 个候选，
  没有选择可言（live 数据：lane cycle 63 个都是 1 个候选，研究 cycle 是 5–6 个）。

**我造成的一次停机**：`alphaengine_owner_call_cap` 加进了配置读取但没加进允许键集合，安装脚本写出了一份配置加载器
自己会拒绝的文件，而安装在**卸载服务之后**中止——writer 与 controller 都下线。现在有测试断言"安装脚本能写的每个键
都是配置能接受的键"。

- **P10c：任务开始写自己的 Initial Screen（已部署；等 owner 发布 mission v9 后开始起草）。** 新增
  `mission_deliverable_versions`：append-only、带指针与版本链，每一版按哈希绑定所依据的任务版本与 Playbook 版本。
  **三条规则由权威强制**：① 正文里的每个数字必须出现在该节 `numbers` 并绑定一条定量 Claim，期间标签（年/季/财年）
  不算数字，出现无来源数字直接拒绝发布（提示要求写"缺来源"）；② 引用的 Claim 必须还活着，P10b 退役的不能引用；
  ③ 文档绑定写作时的权威。起草子进程按任务优先级一次写一家、每节一次受预算约束可重放的模型调用；**估值一节永远不
  起草**（五类市场数据 authority 未接入，无来源数字不如留缺口）。**出口门由结构检查判定**：资料底座来自 P10a 清单、
  数字溯源来自发布时校验、关键 driver 与 street/风险来自"哪几节真写出来了、引用了多少条结论"，结果写进阶段账本
  （gate_passed/gate_failed），不问模型自己写得好不好。驾驶舱公司卡可点开读全文（分节正文、缺口、依据的结论、
  出口门判定）。**部署时发现两件事**：① 重启遗留的票据卡在 `running` 会把整条 lane 永久锁死（抽取 lane 早有认领
  机制，这条漏了，已补）；② 治理拒绝时退出码 1 让票据记成 failed，`held` 不是失败，已改。live 实跑为
  `held`（v8 未授予 `deliverable`），零写入零调用。12 项新测试；全套 1238 项通过（除两项已知 macOS 路径失败）。
  **预期**：第一批文档会写出来但出口门会判 `gate_failed`——没有一家公司的资料底座齐备（缺 10-K 正文、每家只有
  1 个季度的财报数字），这正好指出下一片要建的东西。
  见 [P10c 报告](reports/p10c-initial-screen-v0.1-2026-09-07.md)。
- **P10b：错的结论被挑战并退役，账本不改（已部署；等 owner 发布 mission v8 后自动退役）。** 读一遍 live 的 224 条定性
  Claim 发现两类出处无可挑剔但内容错误的记录：50 条关于 LED 照明/电动车充电/道路产品的陈述记在 EPAM 名下（AlphaEngine
  搜索返回了别家公司的文档），3 条 J.P. Morgan 免责声明。Ledger 是 append-only、ClaimVersion 契约冻结，因此**不编辑不删除**：
  新增两条 append-only 记录——挑战（哪条 claim 版本、哪个确定性检测器、针对哪份精确原文）与决定（退役/保留）；读取路径
  跳过已退役版本，历史哈希全部仍可验证。检测器 `subject_absent_from_source`（公司自己的名字在所引原文里一次都没出现，
  名字取自发现计划的搜索词并剔除行业词）与 `boilerplate_disclaimer`（起草过滤器回溯应用），**由权威在写入时重跑**，
  不采信调用方。写入受 `claim_challenge` 任务授权约束：没有授权时如实报告发现、零写入（live 现状：scanned 246 /
  detected 53 / unreadable 41 / held）。人可挑战与退役任何 Claim，自动化只能依据确定性检测器且不能替人说"保留"。
  驾驶舱"待你审批"逐条可退役/保留。链脚本新增 `--add-write-scope`（只发任务版本，不做 policy 级联）。
  **部署时发现两件事**：① Core 里早有同名 `claim_challenges` 表（旧的数值冲突记录，语义不同），`IF NOT EXISTS` 撞名被
  静默跳过——新表改名 `claim_retirement_*`，旧机制未动；② `pyproject.toml` 的 package-data 逐项列举，新 `.sql` 没进包，
  装到 venv 的包有模块没有建表脚本。11 项新测试；全套 1226 项通过（除两项已知 macOS 路径失败）。
  见 [P10b 报告](reports/p10b-claim-retirement-v0.1-2026-09-07.md)。
- **P10a：任务按研究手册的阶段走，获取与阅读跟着缺口走（已部署，live 已验证）。** live 阶段账本首次非空：
  五家公司各写入一条 `initial_screen entered`，actor 是任务自己的 automation principal（mission v7 早已授予
  `stage_record`）。Playbook 的 Initial Screen 必读清单翻译成四项计数（季度财报数字 / 电话会纪要 / 年报正文 /
  券商观点），一份文档算进哪一项由**找到它的发现规格**决定（发现记录里已存 spec_ref，规格自带 document_type），
  不读正文猜类型、不新增权威。`next_discovered_document` 新增 `preferred_needs`，抽取子进程改按
  (公司优先级, 原文类型, 时间) 读——修的是一个真实缺陷：15 份纪要全归 EPAM、18 份研报全归 CTSH，而 P0 的 ACN
  两样都没有，每天 30 次受治理调用被先发现的公司占满。驾驶舱子任务改为"公司 × 阶段 + 清单"。
  **部署时发现两件事**：① 排序被 `self.load_plan()`（那是 launcher 的方法）静默关掉，`except: return []` 让
  "没有缺口"与"算缺口时崩了"无法区分——现在把原因写进 tick 报告，live 一跳即定位，修好后 8 项缺口、第一项
  ACN 电话会纪要；② 查 AlphaEngine 文档类型目录后确认它**没有年报/10-K**，该项来源改为 `source:sec-edgar`
  并如实写明"还没有获取 10-K 正文的通道"，这是后续切片要建的 lane。另记一次部署竞态导致的 scheduler
  `database is locked`（此前从未出现、之后未复现，未调整全仓统一的 5 秒 busy timeout）。
  15 项新测试；全套 1214 项通过（除两项已知 macOS 路径失败）。
  见 [P10a 报告](reports/p10a-mission-stage-driver-v0.1-2026-09-07.md)。
- **裁决 v1.1：下一阶段 = Phase 10「按研究手册的阶段执行任务」（2026-09-07，当前执行顺序基线）。** owner 在新 cockpit 上
  指出子任务与其理解不一致：目标是"建立 US IT services 首次覆盖"，子任务应是"建立 ACN 的 Initial Screen 和财务预测"之类，
  而不是资料计数或常驻研究问题。盘点后确认：Playbook 六阶段与 mission 七项交付物早已是合同，`stage_record` 也已授予
  自动化，但 live 阶段账本 0 行——P9d 只把"搜→取→读→入库 Claim"做成全自动，没有任何 lane 把工作组织成"公司 × 阶段"，
  没有交付物 authority，没有出口门自评。常驻研究问题只由 owner 改，不是子任务。**Phase 10 切片**：P10a 阶段账本启动
  与资料底座清单核对（抽取按公司优先级、发现按缺项驱动；cockpit 子任务改为公司 × 阶段）→ P10b Claim 挑战/退役与按
  公司索引（清掉约 50 条误归属 Claim）→ P10c `MissionDeliverableVersion` authority + Initial Screen 自动起草（数字必须
  回指 quantitative Claim 否则标"缺来源"）+ 出口门四问自评过门 → P10d Deep Insight Gate 12 问草稿与人审 → P10e 行业
  框架与行业模型缺口清单 → P10f 公司模型与预测线（M2）→ P10g Investment Memo。owner 只需发布一次 mission v8
  （`may_write` 增加 `claim_challenge`、`deliverable`）。退出门槛：三家 `initial_screen gate_passed`、ACN 深度认知门
  经 owner 裁决一次、五份 Initial Screen 数字零无源；止损 2026-09-28。
  见 [愿景复盘与下一阶段裁决 v1.1](reports/vision-and-next-phase-v1.1-2026-09-07.md)。
- **P9d-18 / ADR-0006：cockpit 按 owner 的五件事重做（已部署，live 已验证）。** owner 说旧 cockpit 太反人类：他要的是
  一处输入总研究目标并看到当前目标、系统拆出的子任务与进展；一处 steer；一页研究日志（系统在做什么）；一页临时问答；
  一页只放需要人审批的事项——清晰、fancy、不要机器语言。**改动**：同一 Tailscale/session/CSRF shell 下新页面 `/`
  五个视图（研究目标 / 调整方向 / 研究日志 / 随时提问 / 待你审批），旧审阅页与全部旧路由保留在 `/legacy`；新
  `cockpit_plane`（`/v1/cockpit/*`）只读 Core、读 lane ticket、heartbeat 与日预算账本，写入一律经 writer 以 owner 的
  human principal 执行。目标=active CoverageMission（标题/目标/问题/交付/来源），子任务=每家公司一条线（搜集→阅读→持续
  跟踪）与每个来源一条 lane，进展全部是计数；日志由 ticket、心跳、新 Claim、关闭的审阅与 owner 自己的操作拼成一句句人话；
  审批聚合未决 thesis admission、capability promotion、活 loop 的 planner proposal、forecast overturn 与 owner 自己的
  草稿。**模型调用**（`cockpit_model`）走抽取 routing policy + 同一 broker + 日账本按 mission 上限准入 + scheduler
  WorkOrder replay，不写 Core：问答只从正式 Claim 作答并列依据、把握与缺口；输入目标→起草标题/目标/问题/子任务，确认后
  才发布新 mission 版本（覆盖名单、bindings、autonomy、预算不变，草稿绑定所基于的 mission 版本）；steer→增删研究问题
  （必要时改写目标），做不到的明说。mission 的目标与问题进入抽取上下文与 prompt，新版本重键全部窗口，steer 因此真正改变
  提炼方向。`cockpit_setup` 写 `control.config.cockpit`，installer 调用。**验证**：8 项测试；live 部署后以 owner 身份读
  到 mission v7、五家公司计数、两条 lane 运行中、当日 161 次模型调用 $0.14；一次真实提问经 broker 回答（$0.0013），Ledger
  无相关 Claim 时如实说没有。**不做**：改覆盖名单、接来源、提预算、加工具不是 cockpit 的杠杆（steer 草稿会明说），各需
  自己的 authority 切片。见 [P9d-18 报告](reports/p9d18-owner-cockpit-v0.1-2026-09-07.md)、
  [ADR-0006](adr/0006-owner-cockpit.md)。
- **P9d-17c：网页经同一条链自动准入为 Claim（已过专项，全仓中，待部署）。** owner 已跑第二次 chain（policy-7 /
  constitution v5 / mission v7：列出文档定性规则、`max_daily_paid_calls` 提到 1000）。**live 首批自动 Claim 已产生**：
  一篇文档一 tick 内准入 9 条定性 Claim、审阅自动关闭，Ledger 由 6 条到 15 条；4 条同引文同 aspect 的建议因候选身份未含
  陈述而撞车，已改为以建议 id 键定候选对。网页不是逐字稿：原文是精确字节的确定性渲染，链上处处默认"原文=原始字节"。
  两条路：并行再建一条链，或一条链两种来源——选后者：①`TranscriptCorrectionAuthority._source` 按 manifest id 分派，
  `public-web-fetch-manifest:` 走 `verified_public_web_source` 得渲染文本，`source_content_hash` 即渲染 hash，渲染器变或
  字节漂移即 fail closed；correction set 的 `document_ref` 记页面 record ref（契约放宽到 `public-web-document:`）。
  ②resolver/binder/builders/staging 函数增加 `source_kind`：web 走 `source:public-web`+`fetch_get`、单条 record、
  `public_web_core_authority` 溯源模式与自己的 verifier 身份、显式 envelope；evidence 标为 `public_web`（契约对称扩展）。
  ③staging store 与 Ledger writer 接受 web 种类，writer 新增 `public_web_binding`（envelope 恰好命名该页、raw artifact
  即抓取体；渲染 hash 由 correction authority 在准入时对字节核验，与 AlphaEngine 信任文档摘要同理）。④policy 规则接受
  两种来源。⑤子进程只在 policy/validator 拒绝建议本身时算"判定"，冲突或意外错误一律 hold 审阅不 dismiss（构建中发现
  一次基础设施冲突把审阅误 dismiss）。**验证**：真实抓取子进程（假页面、connected+grant）+ 真实抽取子进程（hermetic、
  带 staging）端到端——网页起草物成为 1 Evidence + 1 Claim（`public_web`/`source:public-web`、两条 artifact ref），
  correction set 为自动化 scope、页面 ref、抓取 manifest，审阅关闭；既有 transcript/qualitative/review/admission 与
  对抗测试全部通过。人工 `stage` 对网页仍拒绝（自动化路径才是 ADR-0005 要的）。见
  [P9d-17c 报告](reports/p9d17c-public-web-admission-v0.1-2026-09-07.md)。
- **P9d-17b：起草物由 policy 自动准入为正式 Claim（已部署；owner 已发布规则，live 已产出自动 Claim）。**
  P9d-17a 上线经过：live writer 从未装抽取模型配置 → 装上；首个子进程因"read-only WAL 无 sidecar"失败 → 子进程持有
  预算账本与 router 的写句柄；34 条 acquired 行没有 ticket_ref（9/4 获取的 33 条 AlphaEngine + 1 条 already-held web）→
  两个 launcher 新增 `locate_completed_manifest(document_ref)`，审阅面按 document ref 从票据目录找 manifest，这也意味着
  那 33 篇 AlphaEngine 文档此前在 cockpit 里从未可读；62 条审阅全被"mission constitution does not bind current governance
  policy"拒绝——constitution v2 绑 policy-3 而 policy-4 已激活，且 policy-4 与 mandate 都没有 ADR-0004 §7 要求的闭合
  `research_budget`，**live 上付费抽取从来就不可能**。`scripts/publish_extraction_authority_chain.py` 依次重发 policy-5 /
  mandate p8a:2 / constitution v3 / mission v5，各自只重绑必须变的字段，research_budget 等于 mission 已发布的预算（不扩）；
  副本排练通过后由 owner 亲自执行（权限闸正确地拦下了我）。发布后再遇 carry-forward 在 v2 与 v4 同有一篇文档时撞
  UNIQUE 把整个发现 tick 打死 → 每篇只从最新版本搬一次、维护步骤异常只报告不抛。随后 **真实起草跑通**：deepseek-v4-flash
  经 broker 调用，每窗约 0.001 USD，按 mission 日预算结算，起草物在 cockpit 可读。
  **P9d-17b**：①`TranscriptCorrectionAuthority` 新增 `automation_verified_raw_span`——人版 `verified_raw_span` 的镜像：
  span 同样逐字节核验，actor 必须是 `automation:`（人不能发它、自动化不能发人版），rationale 记模型 invocation 与 route；
  引文绑定与 resolve 对两者一视同仁；契约 enum 与 actor pattern 同步。②policy 规则
  `research-auto-commit:mission-document-qualitative:v1`：仅当 producer 与 evidence actor 为同一 `automation:` 身份、
  不断言任何数字（value/unit/scale/currency 为 null，陈述无数字/%/$）、evidence 为 `source:alphaengine` 的
  authenticated transcript、引文绑定持久且 claim-eligible、其 correction set 为同一身份的自动化 scope、SourceEnvelope
  为精确的 get_document envelope 时准入；修订/链式候选仍 escalate；共享 Ledger writer 只在该规则下接受 policy 路径的
  定性候选，ADR-0003 B 的人工路径不动。③`resolve_document_review` 接受该审阅所属 mission 的 principal：全部窗口起草完且
  有准入 → `extraction_staged` 绑第一条候选并记准入/拒绝数；无可准入建议 → `dismissed` 记原因；被闸（缺 grant/缺规则/
  web 来源）→ 保持 awaiting。④子进程起草后对"全窗已起草"的审阅做准入：发布或复用 correction set → 绑引文 → staging →
  `commit_policy_candidate`；每步幂等，重跑只报 duplicate；`formal_authority_writes` 如实计数；`--candidate-staging`
  由 launcher 从 writer 配置传入。⑤每 tick 4 窗。**验证**：自动化 scope 的双向拒绝；hermetic 子进程端到端——无规则时
  窗口被 hold、审阅仍开；有规则时 fixture 起草物成为 1 条 Evidence + 1 条 Claim（qualitative、无数值、正式 actor 为
  policy reviewer、候选 producer 为 mission 自动化）、审阅自动关闭并绑候选、重跑零扫描零写入、对已关闭审阅准入被拒；
  多页获取的 partial page envelope 可准入（精确绑定由 Ledger writer 把关，不看 envelope status）。
  **待 owner**：policy 需列出该规则，且 policy 变更经 hash 级联到 constitution 与 mission，同一脚本
  `--add-auto-commit-rule research-auto-commit:mission-document-qualitative:v1` 发布 policy-6 / constitution v4 /
  mission v6（mandate 已有预算不动）；副本排练通过。未跑之前所有准入以"policy 未列规则"hold，不 staging。
  见 [P9d-17b 报告](reports/p9d17b-autonomous-document-admission-v0.1-2026-09-07.md)。
- **ADR-0005：文档抽取由自动化完成；P9d-17a 起草已改为 mission 自动化（已部署，live 真实起草中）。** owner 看到 cockpit 里一整页
  「待抽取文档」和「未安装已批准的抽取模型」后明确：人不批文档——只设目标、掌舵、问答；系统变更（如写新工具）才批。
  据此立 [ADR-0005](adr/0005-autonomous-document-extraction.md)：自动化身份在 mission 授权（live v4 已授 `evidence/claim/
  stage_record`）与日预算内完成起草→staging→policy 准入整条链；人类检查点收敛为 mission 发布/改版、问答、系统变更；
  ADR-0003 B 的「只经人工 accept」在 mission 文档上让位于 policy 准入（人工路径保留为纠错通道）；ADR-0001 thesis 人工准入
  暂不动，另立 ADR。**live 缺的两件事**：writer 从未装抽取模型配置（`document_extraction_model_config_path` 缺失，人按
  generate 也被挡）；起草只能走 writer 单线程 30s 执行器内的人工 op，真实模型调用会卡住 cockpit 与 tick。
  **P9d-17a**：①`dalton_core.document_extraction_setup` 由 install.sh 幂等执行——追加路由 policy
  `model-routing-policy:dalton-openclaw-extraction`（按 profile id `profile:deepseek-v4-flash`，日刷新不失效）、写闭合模型配置、
  把 service.json 指向它；复用 planner 的 broker、thesis-impact 的日预算账本与 policy、model router；不读凭据。
  ②起草改为子进程 `document_extraction_cli`：每次最多 `--max-windows` 个窗口，按各自 mission 的 grant（自动化身份须等于
  principal、来源 `connected`）与预算（mission 日调用/费用，共享账本）起草；完全复用 `DocumentExtractionService`
  （同一 context/prompt/schema/预算准入/可 replay 的持久结果）；已有结果的窗口跳过；`formal_authority_writes` 恒 0。
  ③`DocumentExtractionLauncher/Coordinator` 与 fetch lane 同形：单槽、`extractions/` 票据、重启后采纳子进程 summary、
  新 core op `dispatch_document_extraction` 由 planner driver 每 tick 调用；子进程"无可起草"或被闸时 hold 一小时（队列变化
  即解除）。④`_source_context` 接受 `automation:` actor，授权仍由 mission grant 决定。⑤顺手修：carry-forward 复制
  `acquired` 行时丢了 `ticket_ref`（审阅面靠它找 manifest）。**验证**：setup 幂等三态；真实子进程 hermetic 模式对已获取
  AlphaEngine 文档——无 grant 时不起草并说明原因、v2 授权+carry-forward 后起草两窗（第二窗为终态无效输出、只记一次账）、
  结果在自动化与人两种 actor 下可读回、不写正式记录、重跑只 replay、陌生自动化身份被拒；coordinator 的 launched/busy/
  settle/held/resume/retry 全覆盖。**未做**：staging 与准入（P9d-17b AlphaEngine 链、P9d-17c web citation authority）。
  见 [P9d-17a 报告](reports/p9d17a-autonomous-document-drafting-v0.1-2026-09-07.md)。
- **下一步裁决与实施：web 页面开放模型起草（P9d-15，已部署）。** 复盘 v0.1 愿景、v0.9 复盘与 ADR-0004 后的
  判断：价值只看固定成本下人工接受的 Claim 数量；web lane 自 P9d-7 起搜索→抓取→核验原文→审阅队列已全自主，但到队列就
  停了——网页只能人读、翻页、驳回，模型起草被 `public_web_extraction_drafting_not_supported` 挡住；AlphaEngine 文档早有
  预算内、人触发的起草（P9d-3b）。上限提到 1000 后每天十几页进队，"每页都要人读"正是愿景说不该存在的瓶颈。路线图上的
  Guidepoint 是往一个排不完的队列再加来源，08-26 复盘冻结新 bridge 直到发动机出活——这一片是发动机。
  **改动**：`view`/`generate` 不再对 `source:web-search` 特判，网页审阅与 AlphaEngine 同一道闸（已批准的抽取模型配置 +
  日预算）；上下文本就是核验过的确定性渲染原文，prompt、输出 schema、五条上限、拒绝数字陈述、untrusted 框定、task hash
  全部不变，起草物是"渲染原文的精确引文 + 定性陈述"，永远不是 Claim，replay 不二次调用。**staging 仍拒绝**（新原因
  `public_web_candidate_staging_not_supported`）：候选链发布 transcript correction set、经 transcript correction authority
  绑定引文、经 `stage_transcript_qualitative_candidate` 带 AlphaEngine 文档血统入库；网页来源需要自己的 citation authority
  （fetch manifest、body hash、渲染 hash 与 renderer 身份、span、人工复核）和把该血统带进 Evidence 的 staging 路径——即
  **P9d-16**，并需 ADR-0003 B 补记（网页语义候选同样只经人工 accept）。
  **验证**：writer-ops harness 里用 hermetic fixture worker 对真实抓取页起草——引文即渲染原文、绑定 context 内容 hash、
  `pending_human_citation_admission`、replay 不二次调用、Claim/Evidence 计数不变；对起草物 staging 以新原因拒绝。未做任何
  live 模型调用：起草由人触发，第一次 live 起草由 owner 在 cockpit 上按。见
  [P9d-15 报告](reports/p9d15-public-web-extraction-drafting-v0.1-2026-09-07.md)。
- **web lane 的队列不再漏（P9d-11/12/13/14，已全部部署）。** 按 owner 指令修掉此前发现的四件事，
  并顺手抓出 live 反馈的三件新事。
  ①**P9d-11 部署孤儿**：根因是 Dalton 自己——writer 停机时各 launcher 的 `close()` 会 terminate 在飞子进程，下个 tick
  记 `orphaned`，company/spec 停 park 一天。不改 launcher 语义（SEC lane 明文测试"死 pid 不能凭 summary 升为成功"），
  改 `install.sh`：新增只读 `dalton_core.launch_drain`，先停 thesis-impact/control/**controller**（每 tick 发子进程的是它），
  再等在飞子进程（`DRAIN_TIMEOUT` 默认 600s，超时如实说明后继续），最后才停 writer。
  ②**P9d-12 两条进不了人工队列的路**：(a) 发布新 mission 版本会孤立上一版本未完成的文档（v3→v4 孤立 10 条，6 条再未被引用）。
  tick 现在先 `carry_forward_superseded_documents`：在**当前版本自己的 grant 下**（公司仍在 universe、来源仍 `connected`）
  把未完成行复制到当前版本，保留 document_ref / discovery_ref / host / 原时间戳；grant 拒绝的如实报告不搬；幂等。live 首 tick
  搬回 6 条。(b) 搜索时字节已在 authority 的文档记 `already_in_authority` 后永不入队（那份手工抓的 Accenture 业绩 PDF）。
  tick 现在复核 authority 后把它们结为 `acquired` 并登记审阅，不花抓取。live 首 tick 该 PDF 入队。AlphaEngine 的旧测试
  编码的是"已持有=永不入队"这条错误规则，已改。
  ③**P9d-13 队列有序、账本知主机**：`coverage_mission_discovered_documents` 增 nullable `host` 列（旧库 additive ALTER），
  搜索子进程发现时写入，旧行由 coordinator 每 tick 25 条从 exact discovery envelope 经只读 spool 回填。DiscoveryPlan **0.3**
  （仅 web-search）加闭合 `acquisition` 策略：`preferred_hosts` 先抓（组内仍最旧优先），`skip_hosts` 永不抓也不重试，
  idle tick 报告被 skip 挡住的行数；两表不得重叠、主机名校验、各 ≤50。人写、hash 绑定，所以是**新计划版本**
  `discovery-plan:us-it-services:web-search:3`（hash `519e702d…`），companies/specs/budget 与 v2 逐字节相同。skip 只列今天
  验证过对本 lane UA 全路径回 403 的两台（`news.alphastreet.com`、`www.spglobal.com`）；preferred 列四家公司第一手 IR/newsroom
  与 SEC。**不从失败"学"skip 表**——那是自动化写策略；每主机失败原因在账本里给 owner 看。
  ④**live 回敬的三件事**（见报告末三节）：(i) P9d-10 的回归：两次发现的 envelope 记录时含 grounding 转链 ref，新归一化把它们
  丢掉后 ref 比对失败，**16 条真实 URL 变得不可抓**。修法：envelope 是"引用了什么"的 authority，按其所属时代的归一化复核
  （旧归一化留在 `drop_redirect_proxies=False` 后面），策略只作用于产出；两个时代都拒绝未引用的 ref。(ii) drain 在 controller 已停的情况下仍等满 600s，
  等的是一个"写完 summary 后 12 分钟仍在"的抓取子进程；先误判为子进程不退出并加了 `os._exit`（第三轮部署），新代码子进程
  照样如此——`ps` 给出答案：状态 **Z**。子进程早已退出，只是 writer 要到下个 tick 轮询句柄时才 reap，controller 停了就
  永远不 reap；`kill(pid,0)` 对僵尸成功，所以 drain 把它当活的。**误诊的 `os._exit` 已回退**；drain 现在把僵尸视为已退出
  （Linux 读 `/proc`，macOS 用 `ps`），并有测试真的造一个僵尸来验证。
  (iii) **票据目录普查**：13 张票（discoveries 6 / fetches 5 / acquisitions 2）子进程**已完成并写了 summary** 却被记
  `orphaned`——结算要等下个 tick（最多 5 分钟），其间 writer 重启就丢了句柄。三个 mission launcher 现在在"pid 已死且
  summary 有终态"时采纳子进程自己的记录并标 `adopted_from_summary: true`；结算侧仍复核 authority（summary 说成功但字节
  不在 Core 仍记失败）；无 summary 或无终态仍 `orphaned`；SEC lane launcher 刻意不动。另：两条转链"文档"本身回填不出主机、
  每 tick 报错，现改经 `cited_url_hosts`（含转链）回填真实主机，coordinator 把转链主机与计划 skip 表一并 hold（物理事实，
  非策略：transport 拒绝跟随转链出站）。
  ⑤**live 验证**：v3 计划生效；6 条 v3 文档以 `discovered` 回到 v4；已回填 42 条主机；PDF 审阅入队；
  Evidence 6 / Claim 6 / Thesis 2 不变；第三轮首 tick 已见 `adopted_from_summary: true` 把一个本会被记孤儿的成功抓取结为 `acquired` 并入队。第四轮（僵尸判定、回退误诊）部署时 drain 首次轮询 26ms 即收敛。全仓 **1183/1185**（2 例既有 macOS 路径断言）。见
  [P9d-11..14 报告](reports/p9d11-13-web-lane-queue-integrity-v0.1-2026-09-07.md)。
- **搜索预算提到 1000/24h，抓取失败终于说得出原因，grounding 转链不再当来源（P9d-8/9/10，均已部署）。**
  ①**P9d-8 预算**：owner 指定把 web search 日上限由 40 提到 1000（provider 是便宜的 Gemini 2.5 Flash）。
  计划文件是 hash 绑定的，契约写明"改条款/窗口/节奏就是新文件新 hash"，所以发**新版本**而不是原地改：
  `discovery-plan:us-it-services:web-search:2`（hash `7cc168b4…`），companies 与 specs 与 v1 逐字节相同，
  重发现节奏不断档；v1 保留在树内，因为 live 记录引用它的 hash。1000 正好等于 `gemini-web-search`
  connector 早已持有的 governed 日配额，计划不再低估它所花的 connector。**注意**：`web-fetch` 自己的
  200/24h governed 配额仍独立约束抓取；且每 tick 只发一次调用，真正的限速器是 tick 频率不是预算。
  ②**P9d-9 失败诊断**：live 上同一台主机连续 5 次抓取失败，ledger 里只有 `acquisition ended failed
  (exit 1)`——与网络抖动读起来完全一样。手工探测发现 `news.alphastreet.com` 对本 lane 的 user agent
  一律回 **HTTP 403**。信息其实一直都在：adapter 观测到了 403 并写进 `ResultEnvelope` 的闭合
  `{code, message, retryable}`，是 fetch receipt 把它丢了。`ConnectorRunnerResponse` 是闭合形状不带
  error，但它按 hash 绑定了 `ResultEnvelope`，所以让这一个事实多走三层：receipt 按绑定 hash 读取
  （envelope 漂移则 fail closed）→ CLI 组成具体 `failure_reason` → `settle_documents` 优先采用它，
  与 search 侧 `settle_dispatches` 早就一致（这个不对称正是 bug）。未加宽任何契约。**06:45 live 验证**：
  `fetch outcome failed; public web fetch returned HTTP 403; not retryable`。
  ③**P9d-10 转链不是来源**：把 spool 里的发现 URL 还原后按主机分组，发现两条"文档"根本不是文章，而是
  `vertexaisearch.cloud.google.com/grounding-api-redirect/…`。收录它四重错误：不指名出版方，人无法核验；
  token 会过期，"持久可复核来源"的保证断裂；同一文章经两个 token 得到两个 document ref，去重静默失效；
  而且它永远取不到字节——fetch profile 把 `allowed_hosts` 钉死为 URL 自身主机，transport 拒绝跟随转链
  出站。归一化现在在 `max_records` 截断**之前**丢弃它们，使转链不会挤掉真实引文的名额。live ledger 里
  已有的两条不动：账本只增不改，删除不是自动化该做的事。
  ④**backlog 形状**（供后续接手）：103 条发现文档散在约 50 台主机，多数每台 1–5 条，所以"主机级封禁记忆"
  的价值低于失败模式初看的样子。已确认封禁：`seekingalpha.com`、`news.alphastreet.com`、`www.spglobal.com`、
  `stockanalysis.com`、`www.reddit.com`；已成功：`quartr.com`、`www.tikr.com`、`news.futunn.com`。更有价值的
  缺口是**排序**：第一手 IR 主机（`newsroom.accenture.com`、`investor.accenture.com`、`investors.epam.com`）
  正躺在 `discovered` 里排队，而 coordinator 每 tick 只取一条，没有任何东西优先它们。
  ⑤**一个运维后果**：每次部署 `bootout` writer 都会孤立在飞的 discovery/fetch 子进程，该失败又让那对
  company/spec 按"失败后 1 天重试"停park一天；今天两次部署各烧掉一个槽位。bootout 前先排空在飞子进程可修。
  全仓 **1169/1171**（2 例既有 macOS `/var` 与 `/private/var` 路径断言失败，与本次无关，baseline 可复现）。见
  [P9d-9/10 报告](reports/p9d9-fetch-failure-diagnosis-v0.1-2026-09-07.md)。
- **PDF 抽取来源已上线，web search/fetch 已开闸自主运行（P9d-7）。**
  ①**PDF**：`application/pdf` 经 **pypdf**（可选 extra `pdf`，核心依赖面仍为零；`install.sh` 改装
  `[deploy,pdf]`）渲染为可核验抽取来源；extractor 缺失/加密/畸形/超 400 页/无文本一律带原因拒绝，
  渲染器身份带抽取器版本（`pdf-pypdf-6.17.0:0.1`）使升级让旧 context 失效而非悄悄改变引文文本。
  对 live 里那份 **14 页 Accenture 业绩发布 PDF 抽出 35,471 字**、两次逐字一致。全仓 **1166/1168**。
  ②**开闸**：重装（含 pypdf）+ 健康检查 ok + 备份 `pre-web-connected-20260907` 后，发布 mission
  **v4**（hash `ef242f1a…`）把 `source:web-search` 由 `probe_only` 改为 **`connected`**。
  ③**live 自主闭环已跑通**：约 20 分钟内 **4 次真实搜索、3 次真实抓取**（24h 合计 7/40），文档
  `discovered→acquisition_launched→acquired`，**1 条审阅入队**，人工审阅面成功渲染自动抓取的
  `quartr.com` 页面（9,579 字、8 段引文、renderer `html-visible-blocks:0.1`）；模型起草仍 gated、
  候选 staging 仍拒绝；**Evidence 6 / Claim 6 / Thesis 2 全程不变**，integrity ok。
  ④**两个既有设计后果（未修，待 owner 定）**：发布新 mission 版本会孤立上一版本发现的文档
  （v3 下人工发现的 10 个 URL 现为孤儿，自动化不会去取；自动化会在 v4 下重新发现同样 URL）；
  搜索时已在 authority 的文档记为 `already_in_authority`，永不进人工队列（我手工抓的那份 PDF 即如此）。
  ⑤用量：当时 40 次/24h 由搜索与抓取共用（**已由 P9d-8 提到 1000**）；抓取走无凭据公网
  HTTPS，**transport 不读 robots.txt**。见
  [P9d-7 报告](reports/p9d7-pdf-rendering-and-autonomous-web-research-2026-09-07.md)。
- **live 首次真实页面抓取完成（human-only），并暴露两件事；其中一件已修（P9d-6）。**
  `acquire_public_web_document`（`human:lumos`）对发现结果里第一手的 `newsroom.accenture.com` 抓了一次：
  transport `public-https`、1 次抓取、**190,517 字节 `application/pdf`**（`%PDF-1.4`）进入 Core connector
  authority；live 现有 1 条 `fetch_get` invocation，`formal_authority_writes=0`，Evidence 6 / Claim 6 /
  Thesis 2 不变。
  ①**最有价值的第一手来源常是 PDF，而抽取来源渲染不了 PDF**，审阅面如实拒绝（字节可核验可重放，但人还读不了）。
  **是否支持 PDF 是 owner 的依赖决策**：本仓库运行时依赖几乎为零，本机无任何 PDF 库；引入 `pypdf` 会扩大依赖面，
  自研抽取工作量与出错面都不小。在决定前 PDF 一律如实拒绝，绝不猜着读。
  ②**human 抓取不推进 mission 账本**（与既有 AlphaEngine human op 同构），文档行仍 `discovered`，在
  `connected` 下协调器会为已持有的字节**再付一次抓取**。已修：协调器启动抓取前先判断字节是否已在本来源
  authority，是则经新的 `settle_document_already_held`（只允许 `discovered → acquired`）直接结算并登记人工
  审阅，返回 `already_in_authority`，不花第二次抓取；新增回归测试覆盖该路径。全仓 **1163/1165**（两条既有
  环境路径断言）。见 [P9d-5/6 记录](reports/p9d5-web-chain-deployment-and-live-rehearsal-2026-09-07.md)。
- **web 链已部署 live，并完成 live probe_only 人工排练（P9d-5）。**
  **先纠正一处过时记述**：比对 live 已安装包与仓库 HEAD 后确认，thesis-impact 换版修复、P9d-3a、P9d-3b
  早已随 2026-09-06 08:22 的安装上线（live thesis-impact 退出码 3 / `blocked_pending_human` 正是修复后的
  停泊行为，不是失败循环）；本次部署的差集**只有** web 链的 7 个新模块。
  部署前在 live Core 只读副本上以新代码跑既有 AlphaEngine canary：真实子进程完成 human 发现、写入正常、
  integrity ok（该 canary 报 `ok=false` 是其预期过时——写它时 mission 还是 v1 `probe_only`——以及
  AlphaEngine 24h 预算已满 31/30，非回归）。备份快照 `pre-p9d4-web-chain-20260907`（core 与 scheduler 带
  sha256）。LaunchAgent 的 web search broker socket/key **由 planner 已配置的 broker 路径派生**且仅在文件存在
  时传入，未新增配置项。`install.sh` 重装并重启四个服务后 `dalton-health` `ok: true`，controller tick 同时驱动
  AlphaEngine 与 web search 两条 lane。
  随后发布 live mission **v3**（hash `caae3a13…`）：只把 `source:web-search` 由 `not_connected` 改为
  `probe_only`，其余原样继承。以 `human:lumos` 身份在 live 上跑通**首次真实 Gemini 搜索**：transport
  `openclaw-search-broker`、1 次 provider 调用、**10 个去重 URL ref**（spglobal/morningstar/staffingindustry/
  tikr/grounding 重定向/investor.accenture.com/newsroom.accenture.com/alphastreet/livemint/quartr），
  live 新增 1 条 dispatch、1 条 discovery（`requested_by: human:lumos`）、10 行 `discovered` 文档；
  `formal_authority_writes=0`，**Evidence 6 / Claim 6 / Thesis 2 不变**，integrity ok，web fetch 调用 **0**。
  部署后 fresh tick 确认稳定状态：web discovery 与 web acquisition 均 `not_authorized`
  （"probe_only; automation discovery requires connected"），即**只对人开放、自动化被合同拒绝、页面一次都没抓**。
  本轮共花 5 次真实 Gemini 搜索（4 次在副本，1 次在 live）。**下一步由 owner 决定**：改 `connected` 会让
  automation 每 tick 自动搜索**并自动抓取**那 10 个 URL；真实页面抓取至今一次未跑，建议先用 human-only
  `acquire_public_web_document` 对单个 URL 小步验证。见
  [P9d-5 部署与排练记录](reports/p9d5-web-chain-deployment-and-live-rehearsal-2026-09-07.md)。
- **web search 已在 OpenClaw 侧真实激活，并完成首次真实 Gemini 搜索（P9d-4e）；Dalton 代码栈仍未部署、live mission 未改、live Core 未写入。**
  owner 批准治理记录后：`openclaw config patch`（先 dry-run）装入并配置 broker 插件（备份 openclaw.json，
  模型 broker 条目原样保留），重启 gateway，插件启动、owner-only socket 与 key 就位、doctor 与 health 通过。
  **真实调用推翻了两个推断**：①`api.runtime.webSearch.search` 返回 `{provider, result}` 包装而非 payload 本身
  （broker 已改为校验包装并只转发内层）；②内层 payload 正是 2026-08-14 冻结的形状（含 `model`）——我早期误取
  OpenClaw **agent 工具**表面（返回 `kind`）并据此"纠正"契约，该纠正已完全回退，并加测试确认 agent 形状在这条
  路径上被拒。**真实调用还暴露三个缺陷并已修**：broker 未拆包装；Gemini grounding 单次返回 13 条引用而页面上限
  为 10，旧代码当硬错误拒绝真实答案（该来源 completeness 本就是 `ranked`，改为取排名前 N 并与 URL authority 重建
  共享同一常量，原始 artifact 保留全部引用）；复制来的 broker journal 把记录键钉死为 `^invocation:`，导致重启后
  插件因 `journal invocation id is invalid` 启动失败、socket 变陈旧（已改为有界命名空间 ref，并补上"写入→重启→
  重放 duplicate 且宿主零调用"的回归测试，正是这条测试此前缺失才漏掉该缺陷）。
  **首次真实搜索**在 live Core 只读副本上 `ok=true`（条件具名全真）：真实 launcher→子进程→broker→Gemini，
  1 次 provider 调用，返回 5 个去重 URL ref（alphastreet/morningstar/koalagains/seekingalpha/moomoo），
  发现记录 5 条，URL authority 由 4090 字节原始 artifact 逐字重建，合成答案只留在原始 artifact 内，
  `formal_authority_writes=0`，Claim/Evidence/Thesis 不变，integrity ok，live Core 只读、未写。
  本轮共花 4 次真实搜索（3 次失败诊断 + 1 次成功），在计划 24h 上限 40 之内。插件 16/16、模型 broker 25/25、
  web 专项 56/56、全仓 **1162/1164**。**仍需 owner 决定**：部署 Dalton 代码栈（一次会上线五片并重启四个
  LaunchAgent）、把 broker socket/key 传给 writer、发布 mission 新版本把 `source:web-search` 改为
  `probe_only` 再 `connected`；真实 fetch（抓取第三方原文）尚未跑过，应单独授权。见
  [P9d-4e 报告](reports/p9d4e-live-web-search-activation-v0.1-2026-09-06.md)。
- **P9d-4d「host-owned OpenClaw web search broker」development candidate 已完成；插件未安装、未部署、0 次真实搜索。**
  调查确认 P9d-4a 的真实 transport 此前**根本不存在**：本机 OpenClaw 只在 MCP 上暴露 guidepoint(8943)、
  alphaengine(8950) 与远端 firecrawl，web search 是 agent 工具与插件运行时能力。本片按模型 broker 的既有边界
  补上：新增 Dalton 自有 OpenClaw 插件 `integrations/openclaw-web-search-broker`，宿主保管 provider 与凭据，
  经 owner-only Unix socket 只提供一次有界搜索（HMAC-SHA-256 + 时间偏移 + nonce 重放保护，key 0600 且不进
  应答/参数/日志），闭合请求只含 query/count/可选日期窗/timeout——客户端不能传 key、provider、model、endpoint
  或 header。broker 调用 `api.runtime.webSearch.search` 后核对宿主实际使用的 provider，与配置不符即
  `PROVIDER_CONTRACT_DRIFT` 而不是交出另一种 payload 形状；payload 逐字透传进 tool-result 信封，因此**存进
  Core 的原始 artifact 就是 broker 应答帧**，P9d-4b 的 URL authority 重建无需改动即可解析（已专项验证）。
  幂等以 Dalton 的 credential-use ref 为键：崩溃留下的 pending 报 `IDEMPOTENCY_INDETERMINATE`，绝不自动重搜。
  Python 侧 `WebSearchBrokerHandle` 实现与 loopback MCP handle 相同的协议，其上 adapter/transport plan/authority
  链逐字未变；`WebSearchLauncher` 网络模式改为仅在缺 socket 或 key 时拒绝。插件 Node 专项 15/15、模型 broker
  25/25 原样通过、Python 专项 8/8，含**跨语言回环**（Python 签名 → 真实 Node broker → fresh/duplicate/conflict），
  并已验证两侧规范化 JSON 对非 ASCII 逐字节一致；全仓 **1159/1161**。本机事实：`tools.web.search` 已启用且
  provider 为 `gemini`，`models.providers.google.apiKey` 已配置。**唯一未经真实验证的契约**是
  `api.runtime.webSearch.search` 的返回形状；若不是 provider payload 本身，broker 会拒绝而非交出错误形状。
  启用需 owner：装插件并配置 → 把 socket/key 传给 writer → 发布 mission 新版本改 `source:web-search` 状态。见
  [P9d-4d 报告](reports/p9d4d-openclaw-web-search-broker-v0.1-2026-09-06.md)。
- **owner 已批准两条 connector 治理记录（2026-09-06）。** `connector-governance:gemini-web-search:v1`
  （hash `927c25ed…`）与 `connector-governance:web-fetch:v1`（hash `87c094ca…`）已按既有流程 seed 到 live state
  `connector-governance/` 并由 `human:lumos` 原地 approve，文件权限 0600；仓库 deploy 模板仍为 `proposed`。
  **批准本身没有激活任何东西**：live mission v2 的 `source:web-search` 仍是 `not_connected`，P9d-4a/4b/4c 的代码
  未部署，真实 gateway `web_search` handle 未接线，因此 live 上没有搜索、没有获取、没有网页。启用仍需 owner
  发布 mission 新版本改 `source:web-search` 状态（先 `probe_only` 排练，再 `connected`）。
- **P9d-4c「已获取网页作为可核验的抽取来源」development candidate 已完成，未部署、0 网络调用。**
  P9d-4b 收进 authority 的网页现在人能真正读到：新 `public_web_extraction_source` 从 fetch manifest 逐跳核验
  Core 回执（manifest→invocation→call spec/profile，manifest→source envelope→raw artifact，spool 字节必须 hash 成
  record 自己命名的 body hash，另反查恰好一条 `succeeded` attempt 与 `consumed` settlement），再确定性渲染成文本
  供既有窗口/引文机制使用。只渲染 UTF-8 的 HTML/纯文本，PDF、图片、非 UTF-8 一律拒绝；script/style/noscript 等
  整体丢弃；零宽字符删除、Unicode 空白折叠，避免隐形内容藏进引文；超过 60 万字符按控制面上限截断并声明。
  AlphaEngine 路径逐字未动（既有 17/17 通过），网页 context 只**追加** `canonical_url/host/raw_media_type/
  body_sha256/source_renderer/source_truncated`，其 `source_content_hash` 是渲染文本的 sha256。
  **网页只读**：模型起草在任何 route/预留前 gated（不花预算），候选 staging 明确拒绝——那条链绑定 transcript
  修正权威与 AlphaEngine 谱系，需另立一片并复核 ADR-0003。Cockpit 队列把网页项显示为「公开网页 <短 hash>」。
  新增专项 10/10，fetch lane writer 用例改为真实端到端（socket 上取回可核验窗口、起草 gated、staging 拒绝），
  邻接 107/107，全仓 **1151/1153**（两条为既有环境路径断言）；wheel/sdist 与干净安装后 installed 专项 36/36。
  live Core 只读副本 canary `ok=true`（条件具名全真）：真实 search + fetch child 收进一页并登记
  `awaiting_human_extraction`，两次渲染逐字一致、脚本样式被排除、引文 hash 绑定精确文本、篡改 host 的 manifest
  被拒；Claim/Evidence/Thesis 与 AlphaEngine 行数不变，integrity ok。见
  [P9d-4c 报告](reports/p9d4c-public-web-extraction-source-v0.1-2026-09-06.md)。
- **P9d-4b「public-web fetch lane」development candidate 已完成，未部署、0 网络调用。**
  web search 发现的 `public-web-url:sha256:` ref 现在能像 AlphaEngine 文档一样被协调器按预算获取：新
  `capability:dalton:connector:web-fetch`（proposed 记录已入 deploy，配额 200 页/日）、Core-hosted
  `PublicWebCoreFetch`（通用 runner gate、wire 0.1、**每个 host 一个 operation-scoped profile**，因 adapter 要求
  `allowed_hosts` 恰等于 authority host）、单槽 `PublicWebFetchLauncher` + `public_web_fetch_cli` 子进程、闭合
  fetch manifest。子进程只接受 mission 账本已发现的 ref，并从 exact 原始搜索字节重建 URL authority，搜索未引用的
  ref 在取任何字节前被拒；页面**原始字节**进 connector authority（`fetch_get` envelope），文档 `acquired` 后按 P9d-2
  进入人工抽取队列。搜索与获取共用 web 计划 `max_calls_24h`（deploy 计划 20→40，hash 变化）。本片不渲染页面、不
  抽取、不生成候选；Cockpit 对 web 页面的证据视图仍拒绝并指向 P9d-4c。新增专项 9/9，邻接 108/108，全仓
  **1141/1143**（两条失败仍是既有 `test_document_extraction_preflight` 的 `/private/var` 路径断言，与本片无关）；wheel/sdist 与干净安装后 installed 专项 27/27。live Core 只读副本 canary `ok=true`（条件具名）：
  live v2 拒绝；副本 v3 `probe_only` 下 owner 排练搜索+获取走真实子进程（fake page，1 次调用，0 正式写入），自动化
  获取与登记均如实拒绝；副本 v4 `connected` 下 15/15 搜索、新 URL 自动 `acquired`→`awaiting_human_extraction`、
  已持有 URL 记 `already_in_authority`，到 idle；Claim/Evidence/Thesis 与 AlphaEngine 行数不变，integrity ok。
  激活仍需 owner 批准两条治理记录并发布 mission 新版本；真实 gateway `web_search` handle（P9d-4a 遗留）与页面
  渲染/抽取来源（P9d-4c）未做。见 [P9d-4b 报告](reports/p9d4b-public-web-fetch-lane-v0.1-2026-09-06.md)。
- **P9d-4a「web search 作为 mission 第二个发现来源」development candidate 已完成（shadow），未部署、无真实 host 调用。**
  live MCP 通道由单一 AlphaEngine 桥改为冻结的两条目 host bridge 注册表（AlphaEngine 的 id/hash/错误路径逐字节不变），
  新增 Core-hosted Gemini `search_web` 治理能力 `capability:dalton:connector:gemini-web-search`（proposed 记录已入
  deploy）、单槽子进程 `public_web_search_cli`、DiscoveryPlan 0.2（`source:web-search` spec 无 document_type，
  计划级 `budget.max_calls_24h` 作为硬上限，因 mission body 无 web 预算字段且 Gemini 由 host 计费）、
  `coverage_mission.DISCOVERY_SOURCES` 来源表（表外来源不能做发现）、按来源过滤的协调器（AlphaEngine 协调器不结算
  web 票据、不把 URL ref 交给 AlphaEngine 获取；web 协调器不接受 acquisition launcher，URL 停在 `discovered`）。
  搜索结果只是发现：Gemini 综合与 snippet 留在原始 artifact，向后只暴露 `public-web-url:sha256:` ref，页面原始字节
  须经 P9d-4b 的 public-web `fetch_get` lane 才进 authority。授权规则不变：`not_connected` 拒绝所有人，`probe_only`
  只允许 human 排练，自动化需 `connected`+`source_discovery`；live mission v2 的 web-search 仍 `not_connected`。
  本片没有真实 transport：`WebSearchLauncher` 在网络模式下 spawn 前拒绝，子进程 `--allow-network` 也在触碰 Core 前
  写固定失败 summary。新增专项 18/18，邻接（live MCP、AlphaEngine 搜索/发现、writer ops、治理、inventory、配额、
  service、contracts）原样通过，全仓 **1133/1135**（两条失败为既有 `test_document_extraction_preflight` 路径断言，
  基线 `df7a8b0` 原样复现，与本片无关）；wheel/sdist 与干净 Python 3.14 安装后 installed 专项 18/18。live Core
  只读副本 canary `ok=true`：v2 下自动化与 owner 均被 `not_connected` 拒绝；副本 v3 `probe_only` 下 owner 排练走
  真实子进程（fake citations，2 个 URL ref、1 次调用、0 正式写入）；副本 v4 `connected` 下自动化 **15/15
  dispatch succeeded** 到 idle；Claim/Evidence/Thesis 与 AlphaEngine 发现行数不变，integrity ok，0 网络、0 付费、
  0 live 写入。激活需 owner ①批准 `gemini-web-search-v1.json`，②发布 mission 新版本改 web-search 状态；真实
  OpenClaw gateway `web_search` handle 与 fetch lane 留待 P9d-4b。见
  [P9d-4a 报告](reports/p9d4a-web-search-mission-discovery-v0.1-2026-09-06.md)。
- **live 缺陷修复：thesis-impact 定时任务的治理政策换版死循环已在隔离副本修好，未部署。**
  live LaunchAgent 自 2026-09-02 起连续 1225 次 exit 2，日志只有 `{"error_type": "RemoteError"}`。根因是
  唯一一条 pass verification 绑定 `policy-3`，而 `governance_policy_pointer` 在 2026-09-02T07:01:12Z
  （P9b-1 上线）换到 `policy-4`，`eligible_assessment` 因此永久拒绝，runner 每 5 分钟重放一次同样的失败。
  本轮不放宽 gate：新增 `ThesisImpactVerificationPolicySuperseded` 与只读 `superseded_verification`，
  控制面在入队前就把该绑定停泊为 `verification_policy_superseded`，runner 整趟返回 `blocked_pending_human`
  （退出码 3，区别于故障 2 与成功 0），失败输出改为带 writer 映射 code 与固定文本、不泄露内容。
  全仓 **1117/1117**、专项 91/91、wheel 与干净安装通过；live 只读副本端到端复跑返回停泊状态且 0 写入、
  0 provider 调用。**在 `policy-4` 下重跑评估/验证会产生付费调用与新 authority 记录，保留 owner gate。** 见
  [政策换版停泊报告](reports/thesis-impact-policy-rollover-park-v0.1-2026-09-06.md)。
- **P9d-3b 只读访问修复与启用预检已在隔离副本完成本地验收；未启用真实模型、未部署。**
  基线 `f0ab1cb` 的三个页面读取入口仍会调用可写 router/budget 构造器，本轮改为
  `read_only=True`（`mode=ro` + `query_only`），不 chmod、创建文件或迁移 schema；执行链仍可写。
  新增认证 human-only `document_extraction_preflight`，只在内存 backup 中复用 canonical route/admit，
  检查 exact source/mission/constitution/mandate/governance、单模型 policy/profile、共享预算及外层 cap；
  拒绝过期绑定、换版 policy、超额/跨日未结算与 overrun，不建立持久 route/reservation 或调用。
  返回明确标注 preview_only、非预留、快照会过期、执行仍须重验；不验证 broker 身份、来源送模许可或真账单。
  最终全仓 **1110/1110**、专项邻接 **174/174**、broker **25/25**；wheel/sdist、干净安装及 installed
  预检/合成 socket/HTTP staging/replay canary 通过，218 个 runtime 文件与 wheel 逐字节一致。
  WAL 缺既有 sidecar 时 fail closed；SQLite SHM 读锁标记可能变化，不把它说成物理逐字节不变。
  仅在 workspace 内 clone 开发、提交和导出 patch；没有写原仓或 live，没有网络模型调用、部署、push、merge。
  详见 [本轮预检报告](reports/p9d3b-readonly-preflight-v0.2-2026-09-06.md)。
- **P9d-3b 预算准入、真实 broker 接线与人工 citation/staging 已实现并通过本地自动验收；真实模型 canary 未执行，未部署。**
  不再仅允许 Hermetic adapter：复用原 Scheduler / ModelRouter / OpenClawModelAdapter / 付费账本，原子检查 owner 与 mission 预算，
  调用前预留、Core 记账后结算；未知费用、断连和跨日未结算预留不释放，超预留停止后续准入。
  父审发现 `a3cb1bd` 未显式约束 mandate/governance 研究预算，已补强：缺外层 cap 或 mission 声明超上限即拒绝，
  exact 版本/hash 进入准入，外层调用数/费用在共享账本跨 mission 原子累计；没有修改 live authority。
  Cockpit 可校订语义陈述、缩小 exact 原文 span，并由认证 human 明确确认 citation 后写 CandidateStaging；
  新增有界 `verified_raw_span` 人工原文确认，既有 ASR correction 约束保留，不能绕过未决重叠或过期版本。
  qualitative 的 value/unit/scale=null；数字仍走 SEC；不自动关闭 review、不 accept Claim/Thesis、不伪装 human。
  补丁后全仓 **1080/1080**、专项邻接 **133/133**、真实本地 socket、wheel/sdist 和干净安装通过；
  broker **25/25**、1280/390 人工交互沿用未变组件此前通过的证据，不算新模型调用。
  本地 socket 使用合成 provider，真实模型调用 **0**：尚未核验已批准 extraction 接线、mandate/governance 显式研究预算及共享 live 余额，不擅用 planner 权限。
  人工看图仍待父处理权限及独立审阅；没有 live 写、部署、push、merge 或远端 CI。见
  [P9d-3b 接续报告](reports/p9d3b-budgeted-broker-and-human-staging-v0.1-2026-09-06.md)；
  [上一轮离线阶段记录](reports/p9d3b-document-evidence-and-extraction-scaffold-v0.1-2026-09-05.md)保留供追溯。
- **P9d-3a「Cockpit 文档抽取审阅入口与历史队列恢复」开发完成，尚未部署 live。**
  Cockpit 待审页新增文档队列、同公司同文档的候选选择、抽取完成登记及带理由忽略；登记不接受 Claim。
  writer 核对 exact candidate、原文 citation 和公司，拒绝过期页面与内容改变的重复提交；忽略必须有理由。
  **修正 9/4 部署记录的验收结论：9/5 本轮只读检查发现 live 已有 7 份 acquired 文档，但 review 表为 0 行，
  不能把代码已部署等同于队列已生效。** 新增 controller 每 tick 最多补 100 条历史遗漏，逐条重验 active mission
  授权；不重新获取、不重开已处理记录。live Core + staging 只读副本恢复 7/7，fresh/duplicate、状态过期、
  内容漂移、automation 拒绝均通过；正式 Claim/Evidence/Thesis 与连接器调用数不变、integrity ok。
  全仓 **1039/1039**，broker 25/25；桌面/手机页面及提交交互通过。见
  [P9d-3a 报告](reports/p9d3a-cockpit-document-review-v0.1-2026-09-05.md)。下一片为 LLM 起草抽取建议；本片不做自动抽取。
- **P9d-2「获取文档的人工语义抽取队列」已部署 live（2026-09-04 晚）。** discovery 循环每个
  `acquired` 文档自动登记 `coverage_mission_document_reviews`（状态 awaiting_human_extraction，
  每 mission 版本+文档幂等一条；复用 `source_discovery` 既有授权，无新词表）；人工裁决
  `extraction_staged`（绑定 staged candidate，writer 先核 CandidateStaging）/`dismissed`（必带
  rationale），automation 裁决被合同拒绝；`mission_document_reviews` 读 op + 进度投影新增每公司
  awaiting 计数。ADR-0003 B 人工链完整保留——automation 只排队、不抽取、不 accept。live 只读副本
  canary（真实已获取文档 register→replay→dismiss→automation 拒绝，integrity ok，Claim/Evidence
  不变）+ 全仓 **1035/1035**（+2）。同日：**万华 Agenda Shadow 经 owner 指令退役**
  （`agenda-control-version:wanhua-shadow-retired:1` paused；心跳转干净的 paused，不再有每日调用；
  为修截断发布的 policy v5 随之失去服务对象）；9/4 discovery 全天 126+ 文档发现、7 个完整获取，
  AE 计数 31/30（多页文档页=调用可越过整数上限，launch 检查先于调用记录——软上限边界已记录）。
  见 [P9d-2 报告](reports/p9d2-document-extraction-review-queue-v0.1-2026-09-04.md)。
- **P9c + P9d-1 已部署 live 并激活（2026-09-04，owner 常设授权）：AE search 驱动的来源发现首次真实跑通，三个 live 缺陷当日修复。**
  ①owner 批准 `alphaengine-search-library:v1` 治理记录（hash `f6dff246…`）并发布 **CoverageMission v2**
  （`coverage-mission-version:us-it-services:2`，hash `36877f2c…`：may_write +`source_discovery`
  +`forecast_reconciliation`，`source:alphaengine` → **connected**，预算不变）。②首次真实 `search_library`
  烧掉 3 次调用后定位根因：带 cursor 的搜索观察冻结为 `("partial","ranked")`，而 envelope 校验硬性要求
  `partial↔partial`（fake transport 无 cursor 未覆盖）；修复配对规则后每个 dispatch 成功 settle、单次最多命中
  20 文档。③P9c 新「预测对账」节未按 issue 冻结 `sections` 门控，9/3 旧 issue 重渲染长出新节 → body hash 漂移 →
  每 tick outbox 幂等 conflict + KeyError；修复后 9/3 issue 重渲染与投递 artifact **逐字节一致**
  （`4c8266c6…`），weekly_brief 心跳恢复 ready。④`acquisition_failed` 文档原永久卡死（两次部署重启把在飞
  child 杀成 orphan 暴露）；新增 1 天有界重试（`retryable_failed_document` + 状态迁移），新文档永远优先。
  live 结果：**92 个文档 discovered、1 个 acquired（CTSH `alphaengine-doc:320000610044534` 完整入
  authority）、2 个部署孤儿待自动重试**；当日 AE 共享预算 18/30，失败 cadence 与超限拒绝全部如实入账。
  全仓 1033/1033（+3 回归）。另：9/3 首个自动 weekly brief 窗口已确认完整投递（Discord
  `1545026666730889226` + DeliveryReceipt）；万华 Agenda 9/3 起失败根因为输出 token 随输入增长被 v4 的
  2000 上限截断，已发布 policy v5（4000）。见
  [P9c/P9d-1 激活与首次发现报告](reports/p9d1-live-activation-and-first-discovery-2026-09-04.md)。
- **P9d-1「AlphaEngine search 驱动的 mission 来源发现」development candidate 已完成，未部署。**
  新增独立治理的 `search_library` capability、hash-bound DiscoveryPlan（US IT Services 五家公司 × 财报电话会/卖方报告
  两类搜索）、CoverageMission 的发现/dispatch/待获取文档账本，以及 controller tick 协调器。每 tick 最多启动一条
  search 和一条 get_document；先补已发现文档，再搜新文档；两类调用共用 mission 与 owner 的 24h 上限。自动化必须同时
  满足 active mission、`source:alphaengine=connected`、`may_write` 含 `source_discovery` 与 `observation`；human 可在
  `probe_only` 下排练。搜索结果和随后取得的原文只进入 connector authority，语义 Claim 仍走人工 accept，ADR-0003 B
  不变。live Core 只读副本 canary `ok=true`：mission v1 全拒且 0 写入；human probe 成功；副本 mission v2 自动跑完
  10/10 个 company/spec dispatch 并全部 settled，新增文档经既有 acquisition 进入 authority；Claim/Evidence/Thesis 数量
  不变、integrity ok、0 网络 / 0 付费 / 0 live 写入。canary 同时复现 shared catalog 的 `StaleCatalog` 隐患，launcher
  已改为 AlphaEngine search/get_document 各自独立 catalog。全仓 unittest 1030/1030，wheel/sdist 与干净安装通过。见
  [P9d-1 报告](reports/p9d1-alphaengine-search-driven-source-discovery-v0.1-2026-09-02.md)。
- **P9c「Forecast reconciliation——第一条 Outcome 对象」development candidate 已完成，未部署。**
  新 authority `ForecastReconciliation`（`outcome:forecast-reconciliation:1` 冻结合同）把 exact ForecastLine 版本与同公司、
  同 metric、同财季的 exact 正式 ClaimVersion 绑在一起，实际数从 Claim 自带的冻结 SEC 语句反解并逐字节重渲染核对，
  偏差按 <1% / 1–3% / ≥3% 分为 within_tolerance / notable / overturn_candidate；≥3% 只登记 `forecast_overturn` 人工检查点，
  预测不自动改，人用 `decide_forecast_overturn` 裁决。接入点：SEC lane 提交后立即对账、controller tick 每轮兜底、周报新增
  「预测对账」节、thesis-impact assessment 带对账数据块、CompanyResearchView 新增字段。mission 词表追加
  `forecast_reconciliation`；live mission v1 未授予，automation 路径会如实 skipped，激活需 owner 发布 mission v2
  （`scripts/build_mission_v2_params.py` 生成参数）。live Core 只读副本 canary：mission v1 下 lane 提交 Claim 6→7 且对账
  skipped、0 行写入；副本发布 v2 后 tick 对账 2 条（+1.7125% notable、+4.7396% overturn_candidate）、人工裁决
  keep_forecast、integrity ok、0 网络 / 0 付费 / 0 live 写入。见
  [P9c 报告](reports/p9c-forecast-reconciliation-v0.1-2026-09-02.md)。
- **P9b-2「CoverageMission observation → SEC lane → Claim 阶段绑定」development candidate 已完成，未部署。**
  新 accession observation 先写持久 dispatch queue；SEC lane 忙时保持 pending，controller 每 tick 重试。writer 与 child
  双重检查 active mission、company/ticker、source_plan=connected、may_write、exact form/window/accession 和零付费预算 receipt；
  正式 policy Claim/Evidence 落库后，append-only `coverage_mission_stage_claims` 把 exact ref/hash 绑定当前 playbook stage，
  不自动通过任何 gate。live Core 只读副本 + 本地 ACN companyfacts canary 通过：policy-4、历史 5 条 plan 重验、10-K Claim
  6→7、stage-claim 0→1、source/numeric/integrity 全 pass，0 网络、0 付费、0 live 写入。见
  [P9b-2 报告](reports/p9b2-mission-observation-sec-auto-lane-v0.1-2026-09-02.md)。
- **P9b-1「SEC company-facts 读 10-K」已于 2026-09-02 07:01Z 激活 live。** 核对 live
  companyfacts 后确认：ACN 的 10-K 本身带 Q4 单季值与上年同期值（同一 accession、frame 齐全），CTSH / EPAM /
  IBM / DXC 的 10-K 只有全年值。因此本片让 `10-K` 走既有「同一 filing 的季度对比」规则（覆盖 10/1 之后 ACN Q4
  FY2026 的 10-K），FY − 9M 跨 accession 派生留作后续冻结规则。追加式改动：company-facts 表单注册表
  `10-Q|10-K`、annual 专用 policy rule（auto-start / auto-commit 各一条，只列 10-Q 规则的 policy 继续拒绝 10-K）、
  SEC 模板注册表 `SEC_TEMPLATE_REGISTRY`（v1 旧 hash、v2 当前 hash，历史 plan 按注册版本重验）、connector profile /
  price / rate policy 的 template 版本维度、lane / CLI / launcher / writer op 的 `form` 参数，并修掉一个潜在
  阻断：同一 issuer 的第二个窗口会撞上「only an open question can be selected」，现在每次运行问窗口专属的问题。
  live 只读副本排练 `scripts/run_p9b_annual_lane_canary.py` 用真实 ACN companyfacts 通过：5 条历史 plan 重验、
  候选 policy-4 装入、10-K lane committed（Q4 FY2025 USD 17,596.26M，同比 +7.26%，source / numeric 均 pass）。
  owner `human:lumos` 已批准 live connector governance v2（hash `f781c156…`），发布 active `policy-4`
  （hash `39dd5b7a…`），并随 `b2f34c8` 重装 wheel、重启四服务；health 全绿，激活过程没有触发 lane 或新增 Claim。见
  [P9b-1 报告](reports/p9b1-sec-company-facts-annual-form-v0.1-2026-09-02.md)。
- **P9a 已于 2026-09-02 05:05Z 发布到 live（owner go）。** 重装 wheel 后 writer 自建 playbook / mission 表，
  `human:lumos` 发布 `research-playbook-version:team-analyst-manual:1`（hash `7fe802df…`）与
  `coverage-mission-version:us-it-services:1`（hash `b63e1652…`，绑 constitution v2 / p8a mandate / playbook v1）。
  注意：版本 hash 含 `created_at`，live hash 与 canary 副本不同，mission 绑定必须用 live 实际 hash。阶段账本仍为
  0 条；没有激活任何自动化写入。
- **当前阶段：Phase 9「任务驱动的自主研究（Coverage Mission）」已开工。** owner 2026-09-02 定下目标形态：人下达
  任务（如「建立对 US IT services 行业的首次覆盖」），OS 7×24 自主从 SEC / AlphaEngine / Guidepoint / web search
  找资料，建立行业认知、公司财务模型和预测，人只在检查点看结果。裁决、切片顺序与退出门槛见
  [Phase 9 v1.0](reports/phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)，自动化写入范围见
  [ADR-0004](adr/0004-mission-driven-autonomy-and-automation-write-scope.md)。Phase 8 的 9/3 首个自动 weekly brief
  窗口照常值守；Phase 8 控制面（Bounded Planner、LLM planner、M1、AE probe）成为 mission 下的执行层。
- **P9a ResearchPlaybook + CoverageMission development candidate 已完成（2026-09-02）。** chem agent 的团队分析师
  手册（三段式流程、Deep Insight Gate 12 问、memo 12 个 key questions、analyst level 标尺、tracker 清单、模型与
  证据纪律）转写为 human-only、append-only 的 `ResearchPlaybookVersion 0.1`（`research_playbook.py` + packaged
  schema）：六阶段顺序、Deep Insight Gate / Investment Memo 的人类检查点、五词决定词汇、数字溯源规则由 validator
  冻结。新增 `CoverageMissionVersion 0.1`（`coverage_mission.py`）：行业、universe+tier、研究问题、交付物、如实标注
  connected / probe_only / not_connected 的来源计划、exact 绑定 playbook / constitution / mandate、autonomy
  （`may_write` 冻结词表，thesis 永远不在其中；human_checkpoints 只能加不能删）、budget；阶段账本强制「进入第 k 阶段
  先 gate_passed 第 k−1」「gate_passed 必带 evidence」「人类检查点只接受 human actor」「automation 必须是 mission
  声明的 principal」。writer 新增 10 个 ops、3 份 JSON 合同、`deploy/phase9/` 两份 manifest（playbook v1、US IT
  Services mission v1：ACN/CTSH/EPAM/IBM/DXC）。隔离 canary `scripts/run_p9a_playbook_mission_canary.py` 在
  in-memory 与 **live Core 只读副本**（绑定 live 的 constitution v2 与 p8a mandate）均 `ok=true`：playbook / mission
  fresh→duplicate、ACN 阶段演练（automation 过 initial_screen、被拒绝通过 deep_insight_gate、human 通过）、
  integrity ok、0 付费调用、0 网络、0 live 写入。**未部署、未激活任何自动化写入；发布 playbook v1 与 mission v1 到
  live 保留 owner gate。**见 [P9a 报告](reports/p9a-research-playbook-and-coverage-mission-v0.1-2026-09-02.md)。
- **Phase 8 收口状态（2026-08-27 记录，仍有效）：Phase 7 已收口（第 5 家 policy 自动提交 SEC Claim 达成）；P8a Research
  Constitution 与初始 Thesis、S7f Weekly Brief coordinator 均已于 2026-08-27 经 owner 批准部署并激活 live。**
  裁决见 [愿景复盘与下一阶段 v0.9](reports/vision-review-and-next-phase-v0.9-2026-08-26.md)。live Core 已有 5 条正式 Claim / 5 条
  Evidence：4 条 policy 自动提交 SEC quantitative + 1 条 owner 人工接受 transcript qualitative；driver pack、industry evidence pack、
  四家公司 overlay 和可重放 Markdown 均已在 live。原止损条件「2026-09-09 live 仍无正式 Claim」已解除。若继续保留
  「≥5 条 policy 自动提交 SEC Claim」的严格退出门槛，还需增加第 5 家 issuer。S7e 已把 issue、delivery 和内容反馈接进正式 authority，
  S7f Weekly Brief coordinator development candidate 已完成并通过全量验收，但尚未部署或激活自动发布。Phase 8 的裁决、切片和四周退出门槛见
  [单主题自主认知闭环 v1.0](reports/phase8-single-topic-autonomous-cognition-loop-v1.0-2026-08-27.md)。
- **P8b CompanyResearchView 与结构化知识查询 development candidate 已完成（2026-08-27）。** 新增
  `company_research_view.py`：纯投影（无新事实 authority、无新表），`build_company_research_view` 从
  Ledger snapshot（含 ClaimIndex 派生状态）、thesis admission（当前 Thesis + template/driver_refs）、
  research backlog 开放问题、thesis-impact assessment/verification、覆盖该公司的最新 weekly issue 与
  最近研究停点装配闭合自 hash 的 `CompanyResearchView 0.1`；`built_as_of` 取输入 authority 最大时间戳，
  同状态逐字节重建一致。`query_company_research` 按 company / aspect / period / status 过滤，行携带
  immutable claim ref/hash 与 evidence freshness。writer 新增只读 ops `company_research_view` /
  `company_research_query`（并补齐 `_error_code` 漏掉的 `ResearchConstitutionValidationError`）。视图 claim
  ref/hash 可直接交给 ContextMaterializer 生成 token-bounded ContextPack。隔离 live-copy canary：5 家公司
  全部正确（ACN current thesis + 2 claims + verified impact + open question + w35；DXC 无 issue 停在
  claim_committed）、ACN ContextPack 2 claims / 443 tokens 预算内、integrity ok、0 付费调用。专项/邻接
  106/106、全仓 949/949、broker check 通过。**未部署（只读投影随下次 install.sh 上线）。**见
  [P8b 报告](reports/p8b-company-research-view-v0.1-2026-08-27.md)。
- **P8c-1 常驻问题与 Tier 1 Bounded Planner Loop 准入已完成并进入 live（2026-08-27）。** writer 新增
  human-governed ops `record_backlog_question` / `publish_probe_template` / `create_bounded_planner_loop`
  与读 ops `bounded_probe_template` / `bounded_planner_loop`。live（`human:lumos`）已准入：常驻研究问题
  `research-question:8359…52d9`（"Has US IT services demand bottomed?"，主体 industry，绑 P8a mandate）、
  SEC revenue-growth ProbeTemplate `probe-template-version:3c374282…`（read-only、source-level coverage
  合同）、Bounded Planner Loop v1 `bounded-planner-loop-version:ae3363ca…`（5 个 coverage item、
  预算 6 rounds/6 units/900s）——**循环停泊待 P8c-2 controller 驱动**。隔离 canary（live Core+Scheduler
  副本）：确定性 planner 完整跑 5 轮（proposal→准入→WorkOrder→stub 结果→observed outcome）至终态
  `evidence_observed_for_review`，重放 duplicate，integrity ok，0 付费调用。邻接 76/76、全仓 950/950。见
  [P8c-1 报告](reports/p8c1-standing-question-and-loop-admission-v0.1-2026-08-27.md)。
- **P8c-2 Bounded Planner controller 驱动已部署 live 并完成首次全自主循环（2026-08-27 17:06–17:2x UTC）。**
  `daltond` 新增 `bounded_planner` 服务块（300s 唤醒，core-principal RPC）：列出未终态循环 → 确定性 planner
  提案 → Core 准入（Scheduler WorkOrder）→ controller 经公共 SEC transport 真实执行 probe（每 tick 至多
  1 次、8 MiB 上限、data.sec.gov 白名单）→ 源级 ResearchOutcome → 终态。新增 writer 驱动 ops（propose /
  admit / record_outcome / active_loops）、`bounded_probe_executor.py`（10-Q accession 源级覆盖，
  失败→source_unavailable、未命中→not_found_in_scope）与 `bounded_planner_driver.py`。**首次自主循环**：
  ACN/CTSH/IBM observed（ACN 选中比 lane 更新的 10-Q `…000032`）、EPAM not_found_in_scope（不报告
  us-gaap:Revenues，如实记录）、DXC source_unavailable（SEC 当天移除了该 companyfacts key，已实测复现）；
  planner 自主 terminate，终态 `evidence_observed_for_review`；此后 driver idle。全程 0 付费调用。
  新测试 executor 4 / driver 2 / service 2，邻接 32/32，全仓 957/957。见
  [P8c-2 报告](reports/p8c2-controller-driven-loop-v0.1-2026-08-27.md)。
- **P8c-3 概念回退与新观察→研究注意力已部署 live（2026-08-27 晚）。** executor 支持收入概念候选有序回退
  （与 lane 冻结 allowlist 同序），关闭 v1 循环的 EPAM `not_found_in_scope` 缺口；控制面新增
  `record_observation_followup`：round 的 matched source location 与同 coverage item 既往 outcome 对比，
  新 accession 时以 `automation:bounded-planner` 在 backlog 登记开放注意问题（幂等；unchanged/not_observed
  不提问），进入 CompanyResearchView 与 brief 的 open questions；writer core-only op
  `bounded_planner_record_observation`，driver 在 observed 后调用（失败不中断 tick）。loop v2
  （`bounded-planner-loop-version:52f3636c…`，query_terms 带三概念候选）已准入并自主推进：
  ACN observed 且 observation unchanged（diff 正确）、EPAM 经回退 observed 并**自动登记首条观察问题**。
  全仓 958/958。见
  [P8c-3 报告](reports/p8c3-concept-fallback-and-observation-attention-v0.1-2026-08-27.md)。
- **P8c-4a Doctrine ops、Constitution v2 与 9/3 投递演练已完成（2026-09-01）。** writer 新增
  human-governed `publish_doctrine_pack` / `get_doctrine_pack`，错误映射补齐 `ResearchDoctrine*`
  （并修复 P8a 遗漏：`_error_code` 的 conflict/not_found 从未含
  `ResearchConstitutionConflict/NotFound`）。live 发布 **DoctrinePack v1**
  （`doctrine-pack-version:13a018582c…`，需求拐点透镜）与 **ResearchConstitution v2**
  （`constitution-version:us-it-services:2`，绑 mandate p8a + thesis pack v2 + policy-3 +
  doctrine v1 + plan v3 hash——P8a 的 doctrine null 空缺关闭）。9/3 自动投递的最大未验证环节
  （bridge `openclaw message send --media`）已真实演练：0600 附件 + 精确 bridge 参数形态，
  messageId `1544275074763329619`，消息标注 `[DRILL]` 不复现。全仓 959/959。9/3 窗口全链条
  （admission/issue/outbox canary、--media 投递、DeliveryReceipt）均已各自验证。见
  [P8c-4a 报告](reports/p8c4a-doctrine-ops-constitution-v2-and-delivery-drill-v0.1-2026-09-01.md)。
- **P8c-4b Doctrine ContextPack 接入驱动循环已部署 live（2026-09-01）。** writer 新增 CORE ops
  `materialize_bounded_planner_context` / `bounded_planner_propose_next_with_context`；driver 新增
  doctrine 模式（config `doctrine_pack_version_ref/hash` 成对，materialize 冲突如实 skip 不回退）。
  live service config 绑定 doctrine pack v1，loop v3
  （`bounded-planner-loop-version:1db56b1f…`，prior=v2）准入；首轮验证：提案携带
  `planner_context_pack_ref: planner-context-pack-version:be6eb4d3…`（exact 冻结透镜），outcome
  observed、observation unchanged。当前 lens priority_topics 与 coverage ref 不重合故重排惰性（后续
  loop 版本可对齐命名）；LLM planner 接入留待下片。全仓 960/960。见
  [P8c-4b 报告](reports/p8c4b-doctrine-context-driver-v0.1-2026-09-01.md)。
- **P8c-4c LLM Planner 已接入 live（2026-09-01 晚）。** writer 新增 `llm_planner_prepare` /
  `llm_planner_advance` / `llm_planner_execute`（模型调用在 writer 进程内执行并记账，driver 每 tick
  先试一次 LLM、失败回退确定性 doctrine planner）；production policy
  `model-routing-policy-version:dalton-openclaw-planner:1` pin `profile:deepseek-v4-flash`。三处真实
  缺口修复：①LLM planner provenance 链把 Scheduler 表当 Core 表读（分文件部署必败），现接受显式
  Scheduler 连接；②writer adapter 缺 dedicated agent id（复用 thesis-impact 教训补
  `planner_expected_agent_id` 链）；③LaunchAgent 从 service config 派生全部 planner flag。live loop v5
  已有 **2 条 `planner:llm-research-planner:0.1` 撰写的 probe 提案**被 Core 绑定并执行（V4 Flash 给出
  真实 rationale）。全仓 961/961。见
  [P8c-4c 报告](reports/p8c4c-llm-planner-driver-v0.1-2026-09-01.md)。
- **M1 财务建模引擎与 AlphaEngine Tier 1 probe 已部署 live 并完成首次真实运行（2026-09-01，owner
  裁决双线并行，AE 上限 24h/30 次）。** ①**M1**：新增版本化 ForecastLine authority
  （`model_forecast.py`，value_kind 用 SPEC 冻结词表；derived 行只能绑冻结公式
  `formula:quarterly-growth-extend:1` + exact 输入 hash，非 derived 行 human-only）与确定性
  `extend_growth`（经 record_model_run 写 model run，全链幂等）；writer 新增
  `publish_forecast_line` / `get_forecast_line` / `extend_growth_forecast`。**首个 live 财务模型**：
  ACN base scenario + Q3 FY2026 收入 actual USD 18,718.144M（绑 live SEC claim evidence）+ 增速
  1.15%/季（从 ACN Thesis implied_expectation 换算）→ 4 条 derived_deterministic 预测线
  （Q4 FY26 18,933M → Q3 FY27 19,595M）+ model run v1。估值仍 fail closed（M2 需市场数据）。
  ②**AE probe**：launcher 新增 automation 路径（永不伪装 human）；探测先数 trailing 24h
  invocations，超 30 拒绝且零花费；文档已在 authority 直接命中。AE ProbeTemplate + 循环 v6
  （5 SEC + `coverage:transcript:acn`）准入并全自主跑完：5 轮 SEC + **round 6 AE observed**
  （0 调用），终态 evidence_observed_for_review；当日 AE 计数 0/30。顺带修复 driver 对
  `core_action` 结果的忽略（v5 曾因此卡 40+ 悬空 terminate 提案）。新测试 3+5，全仓
  969/969×3。见 [M1/AE 报告](reports/m1-model-engine-and-ae-probe-v0.1-2026-09-01.md)。
- **2026-08-27 owner 批准全部保留 gate 后已在 live 执行（详见
  [live 激活报告](reports/p8a-s7f-live-activation-v0.1-2026-08-27.md)）**：
  1. **DXC 第 5 家 SEC issuer 完成**：catalog 加入 DXC（CIK 001688568，agenda binding v2→v3），live lane 取
     10-Q `0001688568-26-000069`（fiscal Q1 FY2027），Revenues USD 2,999M 同比 **-5.06%**，`policy-2` 自动提交
     `claim-version:a4d5fb26…80b14`——**live 6 Claim / 6 Evidence（5 条 policy SEC），Phase 7 严格退出门槛达成**。
  2. **lane-only brief v2**：evidence pack v2（5 binding 含 DXC、debate 增加 against 立场）+ 4 家 overlay v2 + DXC overlay v1，
     副本渲染 11,049 bytes 逐字节一致；manifest `deploy/coverage/us-it-services-industry-evidence-v5.json`。
  3. **P8a live 激活**：thesis driver pack v2（lane v1 超集，pointer v2）、P8a mandate、ResearchConstitution v1
     （绑 mandate / pack v2 / policy-2 / plan v3 hash；doctrine 绑定 null 待后续 ops）、行业 Thesis
     `thesis:us-it-services:demand-bottoming`（low）与 ACN Thesis `thesis:acn:ai-reinvention-growth`（medium）
     均经人工准入并带 current pointer。
  4. **S7f coordinator 激活**：schedule plan v3（hash `75153819…e9c8a1`，绑 pack v2 + 5 overlay + ACN 映射）、
     service config 附件目录与 `weekly_brief` block、`policy-3`（保留全部既有规则 + `weekly_brief_auto_publish`
     exact plan v3 hash；`effective_from`=激活时刻避免 S7a 式指针事故）、live 副本故障演练 `ok=true`。
     **首个自动窗口 2026-09-03 07:00 America/New_York**，心跳 `weekly_brief: waiting`。
  5. **thesis-impact 首条真实链已闭环**：ACN mapping 激活后 assessment（gpt-5-6-sol，真实付费）裁决
     **insufficient**（单一 SEC 收入 Claim 不能证明 AI-reinvention 机制——正确的认识论行为），独立 verifier
     （gemini-3-7-flash，provider controls + thinking low）**pass / 0 findings**，
     `thesis-impact-verification:379796f9…` 入库，runner `completed / eligible`。当日三次 live 事故修复并部署：
     ①配置类控制面失败（`broker_auth_key` 指向缺失文件）原有界 **re-drive**；②Gemini host 路径故障 root cause 为
     broker 把 `thinkingLevel` spread 进 `providerControls`（8/23 host 补丁严格化后首个真实调用暴露；另修 proof
     形状校验），broker 侧两处修复后端到端验证通过；③`INVALID_HOST_RESULT` 纳入 re-drive、上限提到 5、writer
     `_error_message` 补齐错误类。host 补丁链未改动。
- **P8a Research Constitution 与初始 Thesis development candidate 已完成（2026-08-27）。** 新增 human-only、
  append-only 的版本化 ResearchConstitution authority（`research_constitution.py` + 新 packaged SQL schema）：
  publish 时以 exact `ref+hash` 绑定 MandateVersion、Driver Pack、active GovernancePolicyVersion、可选
  DoctrinePackVersion（须为该链最新）与可选 Weekly Brief schedule plan（file-contract hash），并只补研究方法：
  问题准入与信息增益、US IT Services 需求因果链、来源等级与冲突裁决（`minimum_independent_sources=1` 与 live
  现行强制一致）、必做 falsifier 搜索与替代解释、量级→盈利→估值→市场预期映射、continue/refresh/stop/escalate
  条件、好坏产物冻结样本与 rubric（good sample 指向首期 live issue，bad sample 指向 owner 的 `revise` 反馈）。
  writer 新增 `publish_research_constitution`（human-governed）与 `get_research_constitution` /
  `get_active_research_constitution` / `research_constitution_report` 读 ops。行业 Thesis
  `thesis:us-it-services:demand-bottoming` 以 `company_ref == industry_ref` 走既有 propose/decide 人工准入
  （driver pack 追加 v2 与行业模板 `template:industry-demand-bottoming`），ACN Thesis 绑定 pack v2 准入；
  `deploy/phase1/weekly-brief-schedule-us-it-services-v2.json`（hash `da9406d4…a4e6dc`）把 `company_thesis_refs`
  从 `{}` 变为 ACN 单条映射，v1 原样保留。隔离 in-memory canary（`scripts/run_p8a_constitution_bootstrap_canary.py`
  + `deploy/phase8/p8a-us-it-services-bootstrap-v1.json`）：constitution fresh→duplicate、2 条 `human_admission`
  ThesisVersion、映射经 weekly brief production 读路径 `_thesis_bindings` 解析为 `current`、未映射公司
  `insufficient`、integrity ok、0 付费调用。专项/邻接 62/62、全仓 940/940、compileall、`git diff --check`、
  sdist/wheel 与 wheel-only 安装均通过。**未写 live Core；live 激活（P8a mandate、driver pack v2、constitution、
  两条 Thesis、schedule v2 是否替换 v1）与 DXC lane、coordinator activation 各自保留 owner gate。**见
  [P8a 报告](reports/p8a-research-constitution-and-initial-thesis-v0.1-2026-08-27.md)。
- **S7f Weekly Brief coordinator development candidate 已完成代码和隔离 live-copy canary。** 新链由现有 `daltond` 唤醒，
  exact plan hash + active governance policy 准入后先写 append-only CycleAdmission，再生成 issue、把 exact Markdown 放入现有
  outbox，并由 OpenClaw bridge 以附件投递和写 DeliveryReceipt。同窗口重放不重复 issue、outbox 或 Discord 消息；issue 后、
  outbox 前故障可从冻结 admission 恢复。候选 plan hash 为 `dde12a2f…846fc`，隔离 canary 为 `ok=true`，live Core 复核仍是
  `policy-2` / 1 issue / 1 delivery，**代码尚未部署，live 自动发布尚未激活**。见
  [S7f 报告](reports/s7f-weekly-brief-coordinator-v0.1-2026-08-27.md)。下一步先跑全仓与打包验收并提交；DXC live lane 和
  automatic delivery activation 继续保留各自的 exact owner gate。当前候选专项回归 49/49、全仓 926/926，sdist / wheel、
  wheel 内容检查和 wheel-only import 均通过。
- live 万华 Agenda Shadow 自 08-25 起连续 `PROVIDER_BUDGET_EXCEEDED`：Dalton 冻结 tokenizer 把整段中文数成 1 个 token，
  DeepSeek 实际计数是它的 3.4 倍，policy 8,000 按后者事后执行。S7a development candidate 已按 provider 单位 bounding
  perception snapshot 并在付费前预检，见
  [S7a 报告](reports/agenda-provider-token-budget-s7a-2026-08-26.md)。owner 已于 2026-08-26 用 `dalton-gov`
  发布 `agenda-policy-version:phase1-shadow-v3`（`max_input_tokens` 16,000，`effective_from` 2026-08-27T00:00Z），
  8/27 起的 cycle 用新预算。**S7a 代码已于 2026-08-26 17:18 UTC 随 `dc747de` 部署 live**
- **live 事故（2026-08-26 17:01–）**：v3 发布时 `activate` 默认 true，pointer 立刻指向一个 `effective_from` 在未来的版本，
  `AgendaStore.active_policy()` 不回退 prior，于是每个 Agenda tick 报 `requested object was not found`。已发布 v4
  （内容同 v3，`effective_from` 17:21:42Z，content_hash `28679036…326c11`）把 pointer 修正。之后到 8/27 00:00 UTC 前
  Agenda 仍每小时报 `conflict`：v4 触发当日第二个 cycle，S7a bounding 后的 snapshot 内容变了但 `snapshot_id` 仍按日期生成，
  与 00:57 UTC 已登记版本冲突；发生在模型调用之前，不花钱。S7a + v4 的首次真实验证是 8/27 00:xx UTC 的 cycle。
  两处待改：`active_policy()` 回退 prior chain / 未来生效默认不 activate；snapshot_id 带内容 hash。见
  [S7c-3 报告](reports/s7c3-live-deploy-candidate-staging-wiring-v0.1-2026-08-26.md)
- S7c-1 development candidate：writer 新增 human governance op `acquire_alphaengine_document` /
  `alphaengine_acquisition_status`，以子进程（不是线程，`SIGALRM` watchdog 只能在主线程）跑 S6b 的 Core-hosted 获取；
  owner 用 `dalton-connector-governance approve` 批准治理记录，launcher 对 `proposed` 记录 fail closed。见
  [S7c-1 报告](reports/s7c-writer-hosted-alphaengine-acquisition-v0.1-2026-08-26.md)。仓库里的
  `deploy/connector-governance/alphaengine-get-document-v1.json` 已由 `human:lumos` 于 2026-08-26 批准
  （`approved`，content_hash `2f6ad555…997c49`）；2026-08-26 部署时 install.sh 已把它复制到
  `state/dalton-core/connector-governance/`，live writer 以 `--connector-governance` 启动。尚未调过真实 AlphaEngine
- S7c-2 development candidate：writer 新增 human-only op `stage_transcript_candidate` /
  `transcript_candidate_status` 和 `--candidate-staging` 参数，把 Core-held AlphaEngine 获取结果经 S7b 入口写进
  Cockpit 共用的 candidate-staging 文件（verification mode 固定 `transcript_core_authority`），
  `HumanReviewAuthority.candidate_status` 只读回读。专项 6/6，合并后关联回归 93/93。见
  [S7c-2 报告](reports/s7c2-stage-transcript-candidate-op-v0.1-2026-08-26.md)
- S7c-3 已部署：`macos_launchagent.render` 从 `control.config.research_review.candidate_staging_path` 推导 writer 的
  `--candidate-staging`，writer 与 Cockpit 写同一个 `candidate-staging.sqlite`。live 探针 `transcript_candidate_status`
  对未知 ref 返回 `not_found`（未配置时是 `rejected`）。见
  [S7c-3 报告](reports/s7c3-live-deploy-candidate-staging-wiring-v0.1-2026-08-26.md)。
- **S7c-4 已在 live 执行（2026-08-26 17:38 UTC）**：live writer 以 `human:lumos` 真实调用 AlphaEngine 一次
  （ticket `alphaengine-acquisition:75a314bc4dac29482e5dbccb`，2 页、1 document unit），装配 digest 与 8/25 owner 确认的
  correction set / citation 绑定的 `a8a9fbff…bd96bd` 一致，probe ok；随后 `stage_transcript_candidate` 把 ACN Q3 FY2026
  「新签订单本币口径同比下降」写成 qualitative 候选 `candidate-claim-version:3fafc07d…e9a87d`，30 项 source verification 全 pass，
  `review_state=staged`。**owner 于 17:42:45 UTC 在 Cockpit accept，review control 随即 `commit_reviewed_candidate`：live Core
  现有正式 `claim-version:e93760a1…df7a9`（qualitative，value null）+ `evidence-version:954b29af…c58fb1`（authenticated_transcript）
  + 1 条 supports relation，`review_state=committed`——live Core 第一条正式 Evidence / Claim。**
  执行前修了一个路径 bug（`0efe8f5`，已部署）：获取子进程原来写 `<state>/connector-spool`，writer 的 stage 校验却从
  `--transcript-spool-dir` 读，live 上 `raw_artifact_bytes` 必 fail；现在 CLI `--spool-dir` 由 writer 传自己的 transcript spool。见
  [S7c-4 报告](reports/s7c4-live-acn-acquisition-and-candidate-staging-v0.1-2026-08-26.md)
- **S7c-5 brief v3**：隔离 canary（`scripts/run_isolated_us_it_services_brief_v3_canary.py` + manifest
  `deploy/coverage/us-it-services-industry-evidence-v3.json`）在一个临时 Core 上重走 ADR-0003 B 全链（fake-handle 获取 →
  correction set / citation → stage → accept → commit），得到 1 条正式 qualitative transcript Claim，再叠上 v2 的 21 条 SEC
  Claim：driver pack v4（+ semantic aspect `aspect:new-bookings-direction-local-currency`）、pack v3（22 binding）、四份 overlay，
  brief 22 Claim / 6 来源 / 20 行 KPI / 80 单元格，replay 一致。用 8/24 真实 ACN 原文跑，citation span 与 live 一致。
  专项 1/1，关联 29/29。**live 上暂时做不出 brief v3**：`register_evidence_pack` 要求每个 driver 至少一条正式 Claim、
  overlay 每个 driver view 至少引用一条本公司 Claim，而 live 只有 ACN 这一条；v2 的 SEC Claim 只在隔离 canary。
  owner 于 2026-08-26 18:03 UTC 选 A：先做 S7d（见下一条），再出 live brief v3。
  见 [S7c-5 报告](reports/s7c5-brief-v3-isolated-canary-and-live-brief-gate-v0.1-2026-08-26.md)
- **S7d 已在 live 执行（2026-08-26 19:02–20:05 UTC）**：US IT Services SEC company-facts lane（S7d-1 `sec_company_facts_lane` +
  `dalton-sec-lane`；S7d-2 通用 `connector_governance`，SEC 记录 `connector-governance:sec-company-facts:v1` 由 `human:lumos` 批准；
  S7d-3 writer human-only op `run_sec_company_facts_lane` / `sec_lane_status`，子进程 + ticket）。live governance policy `policy-2`
  自动提交 ACN（+5.59%）、EPAM（+4.53%）、CTSH（+5.83%，Q1，API 未收录 Q2）三条 quantitative Claim，0 人工 gate；**live Core 现有
  4 Claim / 4 Evidence**。IBM 被 5 MiB 响应上限挡住，等 owner 决定是否提到 8 MiB；Phase 7 门槛「≥5 条 policy 自动提交 SEC Claim」未达（3）。
  上 live 暴露两处性能根因并已修（`ea160d6`）：writer LaunchAgent `ProcessType: Background` 让 CPU 工作慢 6 倍且子进程无法自行解除
  （EPAM 步骤 6 分钟以上、CTSH 391 秒）→ writer 改 `Standard`；`ResearchPlanAuthority.plans()` 每个 SEC plan 重新加载校验 packaged
  inventory 24 次，`thesis_impact_targets` 超 30 秒、thesis-impact worker 自 19:07 UTC 起每次失败 → inventory 每进程缓存，0.25 秒，worker 恢复 idle。
  见 [S7d 报告](reports/s7d-live-sec-lane-rollout-v0.1-2026-08-26.md)、[S7d-1](reports/s7d1-sec-company-facts-lane-v0.1-2026-08-26.md)、
  [S7d-2](reports/s7d2-connector-governance-generalization-v0.1-2026-08-26.md)
- **S7d-4 已部署（2026-08-26 20:24 UTC，`8357465`）**：owner 19:21–19:22 UTC 在 Cockpit 对已被 `policy-2` 自动入库的 ACN、EPAM SEC 候选点了
  accept——Cockpit 只看 staging 库的人工决定，不知道 Core `reviewed_candidate_commits` 已有 policy 收据，所以仍给按钮；之后 reconcile 每 60 秒
  重试 `commit_reviewed_candidate`，Core 每次 `conflict`，一小时累积 110 条 commit event。Core 无脏数据。修法：Core 新增只读 `candidate_promotions`，
  writer 对 review principal 开放；Cockpit `view()` 每次渲染读 Core 收据并标「已由 policy 自动入库」、不渲染按钮，`record()` 对已提升候选和
  writer 不可读一律拒绝；`pending_commits()` 把 `failed / conflict` 当终态不再重试。部署后事件停在 110 条，`pending_commits` 0。
  owner 的两条 accept 决定保留为不可变记录，页面注明「人工接受未另行写入」。见
  [S7d-4 报告](reports/s7d4-cockpit-promotion-visibility-and-terminal-conflict-v0.1-2026-08-26.md)
- **S7d-5 / S7d-7 已部署并完成 live brief（2026-08-27 06:31–06:35 UTC）**：SEC response budget 采用追加式版本，
  v1 5 MiB 继续重验历史 plan，v2 8 MiB 供新 plan 使用；旧/新 profile、price、rate-policy、runner environment 均为独立不可变 authority。
  部署前用新代码完整重验 live ACN / EPAM / CTSH 三条 5 MiB plan；`ab894ee` 部署后 health 为 `running`，Agenda 正常交付。
  IBM ticket `sec-lane-run:2a6c518b28cdf11987ba1629` 取回 10-Q `0000051143-26-000078`，Q2 2026 Revenues
  17,162.0M 美元、同比 +1.09%，source / numeric verification 均 pass，由 `policy-2` 自动提交正式 Claim。
  owner 裁决的 lane-only brief 已按 `2cdcb9e` manifest 发布：唯一 driver 为 `revenue-growth-usd-gaap`，只绑定 ACN / CTSH / EPAM / IBM
  四条 `quarterly_revenue_yoy_growth` SEC Claim，不含 transcript 或手工 8-K exhibit KPI。live driver pack v1、evidence pack v1、4 个 overlay v1
  全部注册；Markdown 9,279 bytes，连续渲染逐字节一致，render hash `c37a8482…13c1714`，integrity ok、0 issues。
  **live Core 现有 5 Claim / 5 Evidence：4 条 policy 自动提交 SEC quantitative + 1 条人工接受 transcript qualitative；严格的
  「≥5 条 policy 自动提交 SEC Claim」门槛仍差 1 条，不能用 transcript 充数。**见
  [S7d-5 报告](reports/s7d5-sec-response-budget-v2-8mib-v0.1-2026-08-27.md)、
  [S7d-6 manifest 报告](reports/s7d6-brief-v4-lane-only-manifest-v0.1-2026-08-27.md)、
  [S7d-7 live 报告](reports/s7d7-live-ibm-and-lane-only-brief-v1-2026-08-27.md)
- **S7e 已部署 live（2026-08-27 08:05–08:09 UTC，`2cecd12`）**：新增 append-only `WeeklyBriefIssueVersion`、`WeeklyBriefDelivery` 和
  `WeeklyBriefFeedback`。brief 不是开发周报；固定写本期研究变化、对现有观点的影响、公司与 driver 分化、证据缺口、关键争议、
  下期研究问题和来源 authority。首期只建立 baseline，4 条既有 SEC Claim 不算“本周新增”；第二期才和 prior issue 做 exact delta。
  没有正式当前 ThesisVersion 的公司一律写 `insufficient`，不能由 Claim 自动补投资结论。writer 增加 human-governed publish / delivery /
  feedback ops，Cockpit 只可替 exact Tailscale human subject 写内容反馈。专项与 writer 回归 27/27，industry 邻接回归 13/13，packaging 1/1；
  临时 state 双 bootstrap 和 live Core 一致性副本均通过。live 首期
  `weekly-brief-version:us-it-services:2026-w35` 为 4 条 baseline Claim、0 条 new Claim、4 家 Thesis `insufficient`；
  Markdown 5,180 bytes，SHA-256 `50d24d68…54d9`，已投递本 Discord channel（message `1542445868618223636`）并写入 exact
  delivery receipt。owner 关于“brief 是每周研究变化、不是开发周报”的意见已作为 `revise` feedback 写入同一 issue。
  live integrity 为 1 issue / 1 delivery / 1 feedback、0 问题；尚未创建 weekly cron。见
  [S7e 报告](reports/s7e-weekly-brief-authority-v0.1-2026-08-27.md)
- S7b development candidate：ADR-0003 裁决为 B（Accepted，owner 可否决）。transcript 候选以 `claim_kind = qualitative`
  进 CandidateStaging，数值字段全为 null，只收带 exact citation binding 的 transcript evidence，policy 路径一律拒绝，
  只经 explicit human review 入库；新增闭合 verification mode `transcript_core_authority` 和
  `stage_transcript_qualitative_candidate` 入口（S7c writer op 直接调用）。隔离端到端：ACN 语义候选 stage → accept →
  commit 写出 1 条 EvidenceVersion + 1 条 qualitative ClaimVersion 0.2。见
  [S7b 报告](reports/s7b-qualitative-transcript-candidate-staging-v0.1-2026-08-26.md)
- live deployed source：`2cecd12`（2026-08-27 08:05 UTC，S7e weekly brief authority）；之前 `ab894ee`（06:31 UTC，含 S7d-5 SEC response budget v2；live brief exact manifest 为 `2cdcb9e`）、`8357465` 2026-08-26 20:24 UTC S7d-4 Cockpit 提升状态回读、`ea160d6` 20:04 UTC S7d 性能修复、`abff89f` 19:45 UTC、`326a62f` 19:00 UTC S7d 首版、`0efe8f5` 17:36 UTC S7c-4 spool 接线修复；上一版 `dc747de` 17:18 UTC，含 S7a / S7b /
  S7c-1 / S7c-2 / S7c-3；再上一版 `3fe746e`）；
  thesis-impact production runner：`9c295ca`；OpenClaw host patch chain：`6f93b9b14`；claude-cli-gateway 心跳补丁：
  workspace `935a751be`
- live 已启用独立的 thesis-impact 短任务，每 300 秒运行一次；writer 持有 Core/Scheduler，worker 只能通过
  scoped RPC 提交受限操作，不直接打开 live Core SQLite
- assessment policy 固定 `profile:gpt-5-6-sol`；verifier policy 固定
  `profile:gemini-3-7-flash`，并要求 provider-controlled `thinking=low`
- production day-budget policy 为 USD 25；当前 `company_thesis_refs={}`，live Core 也没有 ThesisVersion，
  因此短任务稳定返回 idle、provider call 为 0，也不会修改 Thesis current pointer
- development candidate 已将首个覆盖切换为 US IT Services / ACN：ordinal ThesisVersion v0.2、versioned Driver Pack、
  human-only admission candidate/decision 和唯一 ACN mapping fixture 已在 in-memory Core 跑通；尚未写 live Core，
  尚未激活 production mapping，也没有产生付费模型调用
- development candidate 已增加 AlphaEngine live `search_library → get_document` bridge：每次调用绑定 exact
  CompiledConnectorPlan、credential use receipt、quota、raw Artifact 和 SourceEnvelope；真实只读 canary 已取回
  10 条 ACN 检索结果及首个 30k 字符文档分片，续页游标为 `30000`。bounded multi-page coordinator 已在本地实现：
  每页回查 immutable authority/raw JSON-RPC，只有连续终页的整文长度和 SHA-256 一致才 complete；真实完整文档
  canary 和 production ResearchPlan/Scheduler 接线尚未执行
- development candidate 已把 connector quota window 扩展到 IANA 当地日历日，并按 owner 指令冻结候选日配额：
  AlphaEngine `search_library` 50 次、`get_document` 80 份完整文档，Gemini `search_web` 1,000 次；三者均在
  `Asia/Shanghai` 00:00（UTC 16:00）重置。AlphaEngine 文档只在首个 page 消耗 1 个 document unit，续页只记录
  physical calls/bytes；内部 20 页/文档的 calls 上限只是安全阀，不是供应商计费口径。Gemini 1,000 次仍是
  development governance policy，尚未部署。分页 coordinator 的测试已确认首页记 1、续页记 0，
  并覆盖页数/总响应字节/文档字符上限和 crash replay
- development candidate 已新增单问题内的 Bounded Planner Loop v1：human-only ProbeTemplate、exact
  ResearchQuestion/模板/checklist/预算绑定、下一轮生效的 ResearchDirective、PlannerProposal/Core decision、不可变
  PlanRound、机器派生 CoverageManifest 和来源级 ResearchOutcome 已落地。每轮继续使用现有 Scheduler、WorkOrder、
  WorkflowRunVersion 与 WorkOrderLink，没有第二套 queue/DAG；三轮来源级 miss 只能形成
  `coverage_complete_unobservable_candidate`，不会自动生成负面 Claim。专项 8/8、contracts/Scheduler/
  Observability/ResearchPlan 关联回归 59/59 通过；未接真实 LLM/connector，未部署 live
- development candidate 已新增 Doctrine 与 Planner ContextPack v1：human-only、append-only DoctrinePack 定义
  research lens，限时 override 绑定 exact pack/loop/lens；Core 每轮冻结 exact question、Doctrine、可选 Driver Pack/
  Thesis、Outcome 历史、directive、剩余预算和 ProbeTemplate catalog。doctrine-aware deterministic planner 只能在
  已批准 coverage item 内重排；ContextPack stale、catalog/参数/权限/预算漂移或试图降低负面 Claim gate都会 fail
  closed。PlannerProposal 0.2 绑定 exact ContextPack，旧 0.1 保持兼容；最终专项 9/9、关联 95/95、sdist/wheel
  与 wheel-only import 通过；exact revalidation 补强前全仓 732/732，最终全仓矩阵等待同提交独立 CI。未接真实
  LLM/connector，未部署 live
- development candidate 已把真实 LLM 接入 Bounded Planner Loop：模型只提交 `LLMPlannerCandidate 0.1`，Core
  重新验证 exact ContextPack、catalog、coverage、预算和 terminal prerequisites 后才生成 `PlannerProposalVersion
  0.3`；模型不能输出模板、参数、权限、预算、source 或 Claim。15-case frozen corpus 含 10 个安全 case，Qwen 3.8
  Max、GPT-5.6 Terra/Sol、Claude Opus/Fable 均首轮 15/15，Gemini 3.1 Pro Preview 因两次 Markdown-fenced JSON
  得 13/15、safety 9/10 并淘汰。Qwen 与 Terra 复测都再次 15/15；development-only planner policy 固定
  `profile:qwen3-8-max`，Terra 记为首选替代候选。全仓 746/746、sdist/wheel 与 wheel 内容检查通过；尚未部署 live
  worker 或 production routing。详见
  [LLM Research Planner 与模型选择 v0.1](reports/llm-research-planner-and-model-selection-v0.1-2026-08-23.md)
- development candidate 已按 owner 最新要求把私有操作面收敛为单一 Dalton Cockpit：现有 `:8793` 服务将共用
  Tailscale identity、session、CSRF 与一个 HTML shell，Agenda、候选 Claim 审阅和 transcript correction/citation
  审阅仍分别走 `dashboard-control`、`agenda-timeout`、`research-review-control` 与临时 `human:*` writer principal；
  Cockpit 不持有 Core DB 路径。独立 `dalton-review` CLI、HTML 和部署入口已移除。真实 ACN Q3 FY2026 packet 可按
  exact packet/manifest/raw-object hash 写入 owner-only review inbox，GET 只读 correction/citation 状态，人工确认后才发布
  correction set 并绑定 citation；packet/hash 漂移、source lineage 漂移和 unresolved overlap 均 fail closed。伪造
  AlphaEngine `source_record_refs` 的敌对测试也已补齐。S2 又增加 `/v1/research-trajectory` 和 Cockpit「轨迹」页：
  投影从已验证的 packet、manifest、correction/citation state 和 candidate staging state 即时重建，不建新 authority，
  也没有 POST 接口；每个节点绑定 exact ref/hash，packet fragment 与正式 authority 分开标记。真实 ACN Q3 FY2026
  packet 已渲染为 11 个节点、2 页、51,034 字；acquire-only canary 缺少的 Agenda、PlanRound、WorkOrder 和
  WorkflowRun 明确显示为 `unrecorded`，系统没有补造上游轨迹。当前状态仍为 `awaiting_transcript_review`。S3A 又在
  同一 Cockpit 增加 candidate-only 自然语言 composer：服务端冻结 verbatim `HumanUtteranceVersion`、exact
  `IntentContextPack`、独立 interpreter WorkOrder/provenance 和 closed `IntentCandidateVersion`；question、directive、
  priority、context-bound approval 与 meta 先形成 typed candidate。S3B 新增同源 human 二次确认：`/v1/intent/confirm`
  复用 Cockpit session/CSRF，Core 用最新 context 逐字段复核 exact binding，再按 effect 交给原 writer principal。
  candidate 继续保持 `candidate_only=true / executable=false`；append-only confirmation/dispatch receipt 另记确认和每次
  writer attempt。question writer 可从 active mandate、Agenda decision、open loop 或 coverage item 解析 exact
  MandateVersion/company 后进入 ResearchQuestion backlog；directive、priority、Agenda/research/transcript approval
  分别复用 Bounded Planner、Agenda 和原 review authority。16-case
  冻结语料在 exact GPT-5.6 Terra profile 上完成 16/16、safety 9/9，30,295 tokens、provider cost USD 0.13069000；
  interpreter/corpus hash 未变，S3B 没有重新调用模型；S3B 与关联 authority 回归 172/172。S4 又在同一
  Cockpit 增加只读「问答」页：Core 只允许与已入库、已回答 ResearchQuestion 完全一致的问题进入
  `answer_direct`，并冻结正式 Claim/Evidence 及关系、当前 Thesis、Driver/Overlay、open questions、Evidence
  时效和 exact ref/hash；其他问题只返回 `recommend_agenda_item`，不创建 Agenda item 或任何正式 authority。
  human-only `AnswerSufficiencyPolicyVersion` 固定最低 driver coverage、各 source type 最大 age、允许的争议/open
  questions 和最低正式 Claim/Evidence 数；策略 pointer 换版会让旧 subject binding 失效。refresh 与 ad-hoc
  research 的 policy 在 S4 强制关闭且预算为 0。Cockpit 复用原 Tailscale session/CSRF，`dashboard-control` 只增加
  两个只读 RPC；策略发布仍走临时认证 `human:*` governance principal。S4 与 Agenda/Backlog/Industry/Bounded
  Planner 等邻接回归 122/122。S4.1 又在单个 in-memory Core 中回放仓库已有的 ACN SEC authority：精确问题从
  2 条 answer binding 读取 USD 19.32 billion new bookings 和同比 -3% local-currency bookings growth，返回
  `answer_direct`；改写问题和证据超过 30 天分别以 `question_not_admitted`、`stale_evidence` 回退到 Agenda 建议。
  route 前后表计数、SQLite `total_changes` 和完整 authority 指纹不变，policy 换版后旧 subject binding 失效；
  网络、付费模型、成本记录和 live 写入均为 0。canary 同时修正 router 对 ThesisVersion 的 hash 口径：现在重验
  v0.1/v0.2 闭合 wire、exact version/thesis/authority binding，并只对 thesis 正文复算保存的 hash。S4.1 与关联
  authority 回归 153/153。S5A 现只打开 stale-only 的 `answer_after_refresh` development route：exact answered
  question 必须唯一绑定 human-created、未启动的单轮 Bounded Planner Loop 和 human-admitted read-only
  ProbeTemplate；独立 policy 日预算先写 append-only reservation，再复用原 Scheduler/WorkOrder。重复 dispatch 与
  reservation 后崩溃重试不会重复计费或排队；无命中只形成 `coverage_complete_unobservable_candidate`，有命中必须
  绑定本次 ResultEnvelope、SourceEnvelope 和 CandidateStaging stage receipt，不能直接写正式 authority。S5A 关联
  authority 矩阵 153/153；最后
  两项 crash-hardening 写入后，Answer Routing/Contracts/Packaging 最终超集 24/24，Cockpit JavaScript、compileall、
  JSON schema 解析和 diff check 通过。S5B 又补了 observed refresh 的 connector → CandidateStaging 隔离 canary：进程内
  合成 SEC 响应走完整 Connector/Artifact/SourceEnvelope/resolver/verifier/staging production code path，1 次 physical
  attempt 形成 1 条 CandidateEvidence 和 1 条 CandidateClaim，再以 `observed / evidence_observed_for_review` 关闭 bounded
  refresh。finalize 现在必须从只读 connector receipt authority 重读 exact SourceEnvelope 和 raw ArtifactVersion；只给
  caller ref/hash 或缺 reader 会在 ResearchOutcome 前拒绝，同一候选命中多条 stage receipt 也 fail closed。重复 finalize
  只返回原 receipt；CandidateClaim 仍是 `semantic_verification_status=unverified`，正式 Evidence/Claim/Thesis 增量为 0。
  canary 没有外网、付费模型、live DB 或部署。S5C 现已给 development Cockpit 增加显式 human dispatch：浏览器只提交
  exact subject/question/RouteDecision ref/hash/as-of，服务端从 Tailscale login 派生稳定 human actor，再用临时认证
  `human:*` writer principal 调用原 `AnswerRefreshControlPlane`；`dashboard-control` 继续只有两个只读 answer RPC。Core
  会按同一 as-of 重算 route，换日、ref/hash/context 漂移都拒绝；UI 不能创建 ProbeTemplate、Bounded Loop、connector
  plan 或改预算。writer 的 Core 与 Scheduler 仍是两个 SQLite，本切片把 Bounded Planner 的 WorkOrder 复核改为从 exact
  Scheduler authority 读取，并验证 enqueue 后崩溃重放只产生一个 WorkOrder。ad-hoc research 继续关闭；本切片未部署
  live `:8793`，也未打开 production pointer。S5C 的 Answer Routing 与相邻 Scheduler/Cockpit/writer/Doctrine/
  StatementSnapshot/TranscriptPolish/ResearchPlan/Backlog/Industry/CandidateStaging/Observability 回归 197/197 通过；
  Cockpit JavaScript、compileall、全部 JSON contract 与 diff check 也通过。
  实现记录见
  [有限回答刷新 S5A v0.2](reports/answer-after-refresh-s5a-v0.2-2026-08-25.md) 和
  [S5B connector → CandidateStaging 隔离 canary](reports/answer-refresh-connector-canary-s5b-v0.3-2026-08-25.md) 和
  [S5C Cockpit human dispatch](reports/answer-refresh-cockpit-human-dispatch-s5c-v0.4-2026-08-25.md)。live `:8793` 仍是旧 Agenda，
  production pointer 关闭，正式
  Evidence/Claim/Thesis 写入仍为 0。此前 S1/S2 关联回归 80/80、Cockpit JavaScript
  语法、compileall、真实 ACN projection 和 diff check 通过；全仓 `unittest discover` 在无失败输出的情况下运行
  40 分钟后仍停在既有
  `test_routed_worker_retries_contract_then_verifies_independently` 的 connector inventory `canonical_json`
  热点，已人工中断，因此不能记为全仓绿色。当前本机 Python 3.13/3.14 都没有 `build` 模块，因此本轮没有生成
  sdist/wheel，只验证了 packaging manifest test。架构裁决见
  [Dalton Cockpit 与自然语言方向控制](reports/dalton-cockpit-natural-language-control-architecture-review-2026-08-24.md)
  、[ACN 研究轨迹只读投影 v0.1](reports/acn-research-trajectory-read-projection-v0.1-2026-08-24.md)
  、[自然语言 Intent Composer v0.1](reports/natural-language-intent-composer-v0.1-2026-08-24.md)
  、[Intent 二次确认与 writer dispatch v0.2](reports/natural-language-intent-confirmation-dispatch-v0.2-2026-08-25.md)
  及 [Ad-hoc 回答路由 v0.1](reports/ad-hoc-answer-routing-v0.1-2026-08-25.md)
- S6 于 2026-08-25 完成 live Cockpit `research_review` 部署与真实 Tailscale transcript gate（1 条
  `TranscriptCorrectionSetVersion`、1 条 `TranscriptClaimCitationBinding`，`claim_eligible = true`），轨迹停在
  `awaiting_candidate_staging`。2026-08-26 复核发现两道结构性的门：live Core 从未打开 connector authority
  schema（`connector_source_envelopes` / `connector_invocations` 不存在，`observability_artifact_versions_v2` 为 0），
  `_commit_authorized_candidate` 无法核对任何候选 SourceEnvelope；CandidateStaging 0.1 只收能从同一份 material
  的 normalized payload 复算的数值候选，AlphaEngine `get_document` 的 payload 抽不出逐字稿里的 -3%。S6b 开发候选
  已关闭第一道门：Core gate 对缺表明确 `GateRejected`；writer 启动时打开 `ConnectorStore` schema；新增
  `alphaengine_core_acquisition`，用 owner 审批、hash 绑定的 `StaticConnectorGovernance` 充当 catalog 的
  approval / policy resolver，把 AlphaEngine `get_document` 通过既有 `LiveMcpRunnerAdmissionGate +
  ConnectorTransportExecutor` 写进 Core 自己的 connector / artifact authority，再由既有 coordinator 从 Core 回读
  receipt 拼接文档。hermetic 9/9：两页文档入 Core、journal 重放零 provider call、篡改 schema hash 被 catalog 拒绝、
  `proposed` 治理记录 fail closed，且 **Core-held authority 已能让 transcript 候选经 `commit_reviewed_candidate`
  写入正式 Evidence / Claim**（数值 spec 仍为占位）。用 8/24 真实 ACN 原文做的无网络演练两页入库，assembled digest
  与用户已确认的 `a8a9fbff…bd96bd` 一致。未部署 live、未调用真实 AlphaEngine、未接 writer RPC；
  `deploy/connector-governance/alphaengine-get-document-v1.json` 为 `proposed`，需 owner 批准。第二道门写成
  ADR-0003（草案推荐 A 双 material 候选；2026-08-26 裁决为 B 语义候选，见 S7b）。详见
  [S6 正式晋级前置缺口与 Core-hosted AlphaEngine 获取 v0.1](reports/s6-formal-promotion-authority-gaps-and-core-acquisition-v0.1-2026-08-26.md)
- development Planner 又扩展校准了 Qwen DeepSeek V4 Flash/Pro、Grok 4.6、Gemini 3.7 Flash、OpenRouter Ox
  Alpha、ZAI GLM 5.3 和 GPT-5.6 Luna。V4 Flash、V4 Pro、Gemini 3.7 与 Ox Alpha 均连续两轮
  30/30、safety 20/20；V4 Flash 以两轮 USD 0.00960668 和约 2.0s 单 case 中位延迟取代 Qwen 3.8 Max，
  成为 immutable development policy v2 的首选。Luna 完整复测为 14/15、safety 10/10、USD 0.0058576、
  中位 3.191s，不替代 Flash；Grok 有 5 次超过 frozen 800-token WorkOrder 上限并 fail closed。GLM 已证明
  OpenClaw 新路由可发现、可做 calibration-only 准入，但复查确认旧宿主静默忽略 broker 的
  profile-level `thinkingLevel`。host runtime 已补 exact forwarding/capability，broker 也会在宿主不支持时启动失败；
  fake provider 与 Node 25/25 已通过。safe restart 已排队，GLM 完整重跑仍待新进程。详见
  [模型扩展校准 v0.2](reports/llm-research-planner-model-expansion-v0.2-2026-08-23.md)及
  [GLM / Luna follow-up v0.3](reports/llm-research-planner-glm-luna-follow-up-v0.3-2026-08-23.md)。仍未部署
  production Planner routing
- development candidate 已实现 StatementSnapshot v1：human-only、append-only concept set 绑定 exact
  `us-gaap / USD` XBRL concept allowlist 与 Decimal equation；本地 worker 只消费已有 SEC Company Facts
  ArtifactVersion，重验 artifact ref/record hash/raw hash/size、CIK、accession、form、period 和 concept-set ref/hash，
  不再发 HTTP。首个资产负债表 slice 生成扁平 `Assets / Liabilities / StockholdersEquity` fact rows，并以
  `Decimal` 验证 `Assets = Liabilities + Equity`；同 accession 歧义、勾稽失败或 fuzzy label 偷渡都在写入前拒绝。
  隔离测试已通过 ProbeTemplate → 原 Scheduler WorkOrder → local worker → ResultEnvelope → Bounded Planner
  observed Outcome，未增加 queue/DAG，也未自动写 Evidence、Claim 或 Model Input。专项 4/4；真实 SEC connector
  canary 与 live worker 尚未执行。详见
  [StatementSnapshot v1](reports/statement-snapshot-v1-2026-08-23.md)
- development candidate 已把 TranscriptPolishWorker 升为 source-lineage v0.2。原始 ASR 只是不改写的捕获记录，
  不再被误称为唯一语义 authority。新增 human-only、append-only `TranscriptCorrectionSetVersion`：专名与术语修正
  必须绑定 exact primary reference、音频或官方逐字稿；数字、否定词、语义和 speaker 修正只能由音频 span 或官方逐字稿
  支持，不能拿 filing 的“看起来一致”替说话人改口。polished artifact 同时绑定 raw manifest、correction set、resolved
  source hash 和 span mappings，固定 `citation_authority=source_lineage_only`；polished 文本仍不构成证据。正式 Claim 要生成
  `TranscriptClaimCitationBinding`，引用 raw span 与相交的 admitted corrections；若相交位置仍有 unresolved correction，
  Core 把 `claim_eligible` 置为 false。专项 6/6、相关 33/33 通过；目前尚未接通通用 Claim admission、真实 routed model、
  AlphaEngine canary 或 live deployment。详见
  [Transcript Correction Authority v0.2](reports/transcript-correction-authority-v0.2-2026-08-23.md)
- development candidate 已把 `TranscriptClaimCitationBinding` 接进通用 Claim admission。新增明确的
  `authenticated_transcript` Evidence 类型；CandidateEvidence 与 EvidenceVersion 必须同时绑定 exact raw
  ArtifactVersion 和持久化、append-only 的 citation binding。staging 只检查受限 wire shape，正式 promotion 时
  Core 会重读 binding、correction set、accepted/unresolved span overlap、raw Artifact hash 与 SourceEnvelope raw hash；
  unresolved overlap、binding 缺失、hash 漂移或来源不一致都会在写正式 Evidence/Claim 前拒绝。polished artifact
  仍只供阅读和模型上下文，不是第二份来源。相关超集 47/47、compileall、diff check、wheel 安装包 SQL 资源检查
  通过；尚未接真实 transcript routed model worker、AlphaEngine/audio canary 或 live deployment。详见
  [Transcript Claim Admission Gate v0.3](reports/transcript-claim-admission-gate-v0.3-2026-08-24.md)
- development candidate 已接 routed TranscriptPolish model worker。Core 从 exact raw manifest 与可选 correction set
  重建 resolved source，预切不超过 2,000 字符的 span 并计算 hash；模型只回抄 span identity 和 `polished_text`，
  不负责计算 hash，也不能发布 correction、Evidence 或 Claim。模型 WorkOrder 走现有 Scheduler、exact-one-profile
  ModelRouter policy、OpenClaw adapter 和 usage/cost accounting；strict JSON、数字、专名、否定词、不确定性限定词或
  span conservation 任一失败都会进入 bounded retry。只有本地 verifier 已生成 source-lineage-only artifact，model
  Result 才能成功，随后 routed coordinator 关闭原 probe；crash replay 复用同一 route/result 与幂等 artifact。
  相关超集 55/55、pyflakes、compileall、schema、diff check 与 wheel 隔离导入通过；尚未跑真实模型、独立 transcript frozen corpus 或
  live deployment。详见
  [Routed TranscriptPolish Worker v0.4](reports/routed-transcript-polish-worker-v0.4-2026-08-24.md)
- development candidate 已用 transcript-polish 专用 10-case corpus 横评 Dalton broker 当前准入的 25 个 exact
  profile、24 条 distinct model route。首轮 8 个模型达到 10/10、safety 9/9；对低延迟的四个 finalist 再跑一轮，
  Gemini 3.7 Flash `low`、GLM 5.2、Qwen 3.8 Max 和 GPT-5.6 Terra 均再次全过。人工复核发现 GLM 5.2 在 unresolved
  ASR case 删除了通用 `Speaker:` 标签，而 v0.1 corpus 尚未把该标签列为 protected term。Gemini 3.7 Flash 两轮中位
  延迟为 2.027/1.885 秒，均保留 speaker 结构，成本也低于 Qwen 与 Terra，因此成为横评算法首选。Owner 随后明确指定
  GPT-5.6 Terra 为 TranscriptPolish 首选；Core 已把 Gemini v1 原样保留，并新增 exact Terra development policy v2，
  production pointer 未启用。Corpus v0.2 现有 12 case、11 个 safety-critical，显式保护 speaker，并增加 unresolved
  proper-name/numeric ASR 错误；Terra `xhigh` 在 clean commit 上取得 12/12、safety 11/11，中位延迟 4.142 秒，成本
  USD 0.037506。Planner development policy 继续使用 Qwen DeepSeek V4 Flash，不受逐字稿选择影响。AlphaEngine
  登录恢复后，真实 17,703 字逐字稿 canary 已完成 acquisition → Terra → Core artifact gate；最终 17,885 字 artifact
  通过，自动标记的 unresolved 术语保留，正式 Evidence/Claim/Thesis 写入均为 0。事后权限审计确认，自动挑词并沿用
  owner 执行身份不能算人工 correction review；此前生成的 Claim binding 只保留为技术记录，不具备 admission authority。
  canary runner 现只支持未审阅 shadow，不提供可代填 `human:` actor 的 correction 或 Claim binding 模式。canary
  同时补上 late lease 的 broker replay 恢复，并把句点粘连假专名从保护规则中排除。第二份独立 shadow 改用 42,279 字、
  16 个 speaker label 的 `Nebius Q2 2026`；两页采集、Terra 和 Core artifact gate 通过，最终产物 42,632 字，比例
  1.008349，实际成本 USD 0.203092。该样本按未人工审阅模式运行，因此没有 correction set，Claim binding 按设计阻断，
  正式 Evidence/Claim/Thesis 仍为 0。本轮还修复 AlphaEngine 尾页 `complete=false`、broker 600 秒 timeout 前置校验、
  segment 首尾空白丢失和 `CPU-heavy` 连字符假专名。当前继续 shadow，production pointer 仍未启用，需人工明确完成
  correction review 后再审。详见
  [TranscriptPolish 模型校准基础 v0.5](reports/transcript-polish-calibration-foundation-v0.5-2026-08-24.md)
  、[TranscriptPolish 模型初轮校准 v0.6](reports/transcript-polish-model-calibration-v0.6-2026-08-24.md)
  、[TranscriptPolish 全模型横评 v0.7](reports/transcript-polish-model-matrix-v0.7-2026-08-24.md)
  、[TranscriptPolish Terra policy 与 corpus v0.2](reports/transcript-polish-terra-policy-and-corpus-v0.8-2026-08-24.md)
  、[AlphaEngine TranscriptPolish 真实 canary v0.9](reports/alphaengine-transcript-polish-live-canary-v0.9-2026-08-24.md)
  和 [AlphaEngine TranscriptPolish 第二份 shadow canary v1.0](reports/alphaengine-transcript-polish-second-shadow-v1.0-2026-08-24.md)
- development candidate 已增加 Gemini `web_search` discovery bridge 和独立 public-web fetch adapter。冻结 inventory
  已按真实 OpenClaw 合同修正为无 cursor，`freshness` 与显式日期窗互斥；search raw response 完整保存，向后只暴露
  由引用 URL 推导的 opaque authority ref，不把 Gemini synthesis、snippet 或 title 当作网页正文。系统只有从 exact
  search raw artifact 重建 URL authority，并经现有 public-only HTTPS transport 独立取回原始 bytes 后，才允许
  `fetch_get` 形成 public-web authority material；search 和 HEAD 在 source-material gate 与 candidate-evidence gate
  都会 fail closed。本地全量回归 694/694 通过；最终 citation 上限硬化后的关联超集 53/53 通过，compileall、
  inventory 确定性重建和 Python 3.13 sdist/wheel 构建也通过。具体 OpenClaw host runner、production
  Catalog/profile/grant 和真实 canary 尚未接线或执行
- development candidate 已完成 Model Input Ledger v1：actual、assumption、forecast line、scenario、model run 和
  reconciliation 都使用不可变版本与 exact authority；研究 worker 只能提交 candidate，正式 input 由认证人类准入；
  valuation 缺 price/shares/FX/rates/consensus 任一正式 authority 时 fail closed。随后新增通用
  `IndustryResearchAuthority`：Industry Evidence Pack 只能绑定正式 Evidence/Claim/Relation，Company Overlay 只能复用
  pack driver、公司 Claim 和同源的 human-admitted actual Model Input。第二版隔离 US IT Services canary 已用 ACN、
  CTSH、EPAM、IBM 五个 exact SEC accession 形成 5 条 Evidence、21 条 Claim、17 条 Model Input、4 份当前 overlay；
  每家公司必须对 19 个 driver/KPI 关联逐项声明 observed、已审来源未找到、不可比或不适用，缺失值不能补零或偷换口径。
  确定性 industry brief snapshot 已形成 4 个 driver scoreboard 和 76 个公司单元格，且要求四份 overlay 全部绑定同一
  pack exact ref/hash。ThesisVersion 与 paid model call 均为 0，尚未写 live Core，也未生成自动投资结论
- 修正版 3×30 canary 的三个 run identity 各自独立，90/90 fresh execution，三轮均 30/30、0 FP、0 high miss，
  总成本 USD 0.12906825；随后真实 isolated shadow 固定 GPT-5.6 Sol → Gemini 3.7 Flash low，quality gate 为
  `eligible`，成本 USD 0.010518
- activation 前已创建并验证 `pre-thesis-impact-production-20260822-v1` 回滚快照；OpenClaw host 本轮没有配置
  或 patch 变化，沿用已通过 canary 的现有 gateway 进程，没有为重启而重启

本文是当前进度的权威入口。`docs/reports/` 下的实施报告记录各次交付当时的状态，后续实现不会
反向改写历史结论。这里的“完成”只表示代码、测试和当前部署已经验收，不表示已达到多租户或
hostile-code 生产安全等级。

当前 connector / 建模 / 行业研究的近期顺序见
[direction-and-source-modeling-order-v0.8-2026-08-23.md](reports/direction-and-source-modeling-order-v0.8-2026-08-23.md)。
v0.7 及更早报告保留为各切片启动时的历史基线，不再作为当前近期执行顺序。

## 当前判断

Dalton 已经完成独立 Core、Research Ledger 核心版本链与 gate、单写者、Scheduler、模型路由、模型用量/成本、
Capability Registry/Catalog、常驻控制服务、Agenda Shadow、durable outbox、人工反馈和备份恢复。
开发候选现在另有单问题内的 Bounded Planner Loop：Agenda 继续负责跨问题排序，inner loop 只根据上一轮
source-level Outcome 从 human-admitted ProbeTemplate 中提出下一 probe。Core 掌握 scope、权限、预算、coverage
和终态，且复用现有 Scheduler/Workflow authority。Doctrine 与 Planner ContextPack v1 已让同一问题在不同
human-admitted lens 下选择不同的已批准 probe；真实 LLM 现在可以读取 exact ContextPack，但只能提交严格的弱候选，
Core 才能签发 Proposal 0.3，deterministic planner 保留为 fallback。Qwen DeepSeek V4 Flash 0731 已按两轮
30/30 的固定 corpus 结果写入 development-only policy v2；live production 尚未启用 Planner worker 或该 policy。
StatementSnapshot v1 与 TranscriptPolish source-lineage v0.2 均已作为受限 probe 完成隔离接线，
`TranscriptClaimCitationBinding` 已接入通用 Claim admission，routed transcript model worker 也已复用现有
Scheduler/Router/adapter/accounting 跑通隔离链。25 个 exact profile 的横评和四个 finalist 的第二轮复测已经完成；
Owner 选择的 GPT-5.6 Terra 已进入 immutable development policy v2，并在 corpus v0.2 上通过 12/12、safety 11/11。
首份真实 AlphaEngine 完整 transcript canary 也已通过；当前决定继续 shadow，等第二份结构不同的真实逐字稿通过后再审
production policy。
Planner 的 DeepSeek V4 Flash 只负责 Planner，不承担逐字稿润色。
live 部署现在能自主生成并选择研究问题，也已加载 phase-pinned thesis-impact production lane；由于 live Core
尚无 ThesisVersion 和 company mapping，这条 lane 当前只做无模型调用的 idle 检查，不提交 assessment、verification
或 ThesisVersion。仓库 fixture 仍可按一次性 connector plan 执行 CNINFO、SEC、AlphaEngine 三源离线流程并从
checkpoint 恢复；当前开发候选已能重放 fixture，也能从完整 Connector authority 解析真实 SEC public
响应，并把 source/numeric verifier 通过的 CandidateEvidence/CandidateClaim 写入独立 staging。这条链已由
ResearchPlanExecutor 接通，并在隔离临时 authority 中跑完一份真实 SEC public 四步 plan。Owner 已接受该 plan
的 exact candidate；隔离 Core 已生成 1 条正式 EvidenceVersion 0.2、1 条 ClaimVersion 0.2、1 条 supports
relation 和 1 条 Backlog answer binding。当前开发候选已经增加明确人工审阅入口、无损正式 promotion 和
authority-bound ResearchPlan closure，但尚未部署。

2026-08-20 新隔离 canary 已从 `data.sec.gov` 真实跑通 policy-authorized 主链：versioned policy 自动授权 1 份
SEC public 10-Q plan，四个节点全部成功，系统自动提交 1 条 Evidence、1 条 Claim、1 条 supports relation，并生成
1 条 Backlog answer binding。`research_plan_approvals=0`、`human_review_decisions=0`；Core、candidate staging、
coordinator、capability 四个 SQLite integrity check 均为 `ok`。这证明低风险主链不需要逐 plan、逐 Claim 找 owner
审批，但仍只是 `filing_count` 机制样本，不代表已经达到自主研究分析师的最终质量。

同日第二条无凭据 canary 已把 SEC Company Concept 接入完整自动主链。系统从 Microsoft 同一份 10-Q accession
`0001193125-26-191507` 中选出 2026-01-01..2026-03-31 与同比期间收入：USD 82,886,000,000 对
USD 70,066,000,000，独立 numeric verifier 复算同比增长 18.3%。`get_company_facts → authority resolver →
source/numeric verifier → candidate staging → policy commit → formal Evidence/Claim → Backlog answer` 全部完成；正式
Claim 为 `claim-version:56531bfe0721396625896d5bef8b2584e3c0fd5388aadf7e996bc6cde6c7e179`，
`research_plan_approvals=0`、`human_review_decisions=0`，四个 SQLite integrity check 均为 `ok`。选择规则拒绝跨
filing 拼接、年度/累计期间、单位漂移、模糊 comparative context、非整数 USD 事实和 taxonomy/concept 猜测。
这证明系统不需要 owner 逐条审阅，也已把正式产物从 filing metadata 推进到研究可用的财务事实；它仍未更新
thesis/model，不能把单一收入增速自动解释成投资结论。

同日多样本校准暴露并修复了一个真实错误：Walmart 已弃用的 `SalesRevenueNet` 只有 2018 年数据，旧实现仍把它
作为“最新”事实自动提交。`get_company_facts` 现在必须冻结最长 400 天的 `filed_from..filed_to`，adapter、raw
authority replay 和 policy commit 都绑定同一窗口；旧 concept 在窗口内没有 10-Q 时会在 connector 阶段失败，
不会生成 Evidence 或 Claim。窗口化实跑中，Apple 的同季度收入同比为 16.36%，NVIDIA 的 `Revenues` 为 85.23%，
Walmart ASC 606 revenue concept 为 7.14%；三个 plan 都是零人工审批并完成正式 closure，Walmart 旧 concept 则按
预期产生 0 条 Evidence、0 条 Claim。专项测试同时覆盖 `10-Q/A` 排除、缺失同比、模糊 context 和超宽窗口。

随后开发候选已去掉 company-specific concept 输入。plan 只冻结统一的有序 allowlist：`Revenues →
RevenueFromContractWithCustomerExcludingAssessedTax → SalesRevenueNet`；adapter 从 SEC Company Facts 原始响应找出
窗口内最新 10-Q accession，只在该 accession 上按顺序选第一个能形成 same-filing 同比的 concept，并把全部可用
concept 一并写入 authority。当前代码实跑 Apple 时自动 fallback 到 ASC 606 concept，NVIDIA 自动选 `Revenues`
并得到 85.23%，Walmart 自动选更完整的 `Revenues` 并得到 7.33%；此前 7.14% 的口径不含部分会员费等收入。
三家公司均完成零人工审批的正式 closure。

开发候选现已完成 **正式财务 Claim → 既有 driver/thesis 影响判断** 的第一版 authority：producer 只能读取
exact formal ClaimVersion、当前 ThesisVersion 和两者 hash，输出 `supports / weakens / no_change / insufficient`；
另一个不同 model family 必须从 Scheduler 的 immutable ResultEnvelope 独立复核。通过后只形成 append-only
pre-commit 判断，不会直接改 thesis，也不增加逐条 owner 审批。没有既有 thesis/driver 时，系统保留 Claim 并
在 ResearchQuestionBacklog 自动生成一条可重放的 follow-up question。

2026-08-21 开发候选已把 ResearchPlan closure 的 exact formal Claim 接到上述路由。新 coordinator 重读
ResearchPlan、Backlog start/answer、正式 Claim 和当前 Thesis 的 exact ref/hash，再依次生成受预算约束的
assessment WorkOrder 与独立 verifier WorkOrder；verifier 同时读取 assessment、Claim 和 Thesis，不能只复核摘要。
隔离 recorded-result 端到端 canary 已覆盖 `supports`、没有 thesis 自动建题、`insufficient` 自动建题和 crash/replay
去重；两条模型 WorkOrder 都无 side effect，整个流程不增加逐条人工审批，也不改 thesis。截至这个 recorded 阶段，
真实付费模型尚未调用；后续 Gate 2 结果见下文。

同日后续开发已把这两个 WorkOrder 接入现有 `ModelRouter → OpenClawModelAdapter → Core/Scheduler` 执行边界。
worker 只能执行 coordinator 生成且已在 Scheduler authority 中冻结的 exact WorkOrder；assessment 失败后按 bounded
attempt 重新路由，verifier 的 producer family 从已持久化 assessment invocation 自动读取，caller 不能选择或绕过
family independence。每次 broker 调用都会先写 ModelInvocation、usage 和 cost authority，模型输出通过 closed contract
及 exact ref/hash 检查后才允许成为 Scheduler formal success。错误 JSON 会进入有界 retry，最后一次失败会形成正式
failed result，不留下 exhausted-but-unreadable 状态。

隔离 recorded broker canary 已验证 `错误 assessment JSON → retry → valid assessment → independent verifier pass → replay`：
三次调用分别形成三条 invocation、usage 和 cost，错误输出没有生成 assessment，verifier 自动切换到另一个 model
family，重放没有第四次 broker 请求，人工 review decision 仍为 0，Thesis current pointer 不变。该 canary 没有调用
真实模型，不能用于判断模型输出质量。

同日继续补齐了付费调用前的崩溃边界。如果模型已经返回并写入 invocation/usage/cost、但进程在
`Scheduler.complete` 前崩溃，lease 过期后的新 attempt 会复用原 route 和 invocation，只能向 broker 发
authenticated `replayOnly` 请求。broker durable journal 命中时返回原 completion；miss 时直接返回
`IDEMPOTENCY_MISS`，禁止调用 host 模型。只有没有被 Scheduler 接受过的 route 才进入这条恢复路径；已经形成
retryable ResultEnvelope 的 attempt 仍会按正常 retry 新建 route。隔离 E2E 注入了 accounting 后崩溃，最终 2 次
socket 请求只产生 1 次 provider call、1 条 invocation、1 条 usage 和 1 条 cost，formal success 落在 Scheduler
attempt 2，三套 SQLite integrity check 均为 `ok`。

2026-08-21 方向复审维持 Conditional Go，但改变了下一阶段顺序。Fable 的 review evidence 注入脚本失败，且
`3fd630e..b81d1cb` 的 10 个提交在独立复核完成前已累计增加 8,527 行。仓库虽已有 Python 3.11/3.13 全量测试、
build 和 broker check 的 GitHub Actions，Apple/NVIDIA/Walmart 也跑过同指标自动 closure，但最新 HEAD 的 CI 在
复审时尚未全部结束，三家公司结果也只有状态文档记录，没有可提交的 replay bundle。当前顺序改为：先完成最新
HEAD 独立 CI、hermetic replay canary 和 fail-closed review evidence collector；再在同一 commit 上复现 5 家公司并
产出 verified revenue-growth brief；之后才取得单独付费调用授权，跑一条真实 thesis-impact canary。此前不增加
thesis updater、并发 worker、自动 thesis revision 或 fleet control。真实 canary 仍不得直接修改 thesis；
`insufficient` 只生成后续问题，`supports / weakens / no_change` 只形成可供未来 thesis updater 消费的已验证判断。

Gate 0 候选现已增加显式 hermetic replay CI step 和 fail-closed review evidence collector。前者单独覆盖
recorded SEC Company Facts → formal Claim closure → recorded thesis assessment/verifier → replay；后者从 closed
manifest 收集非空文档、实现和命令证据，只接受 argv 数组，任一文件缺失/为空、命令失败/超时、路径逃逸或陈旧
输出都会停止且不发布半份 artifact。GitHub Actions 在 Python 3.11、3.13 两个 runner 都跑 canary，3.13 runner
另上传证据包。该候选本机 Python 全量 581/581、broker 16/16、canary 1/1、collector 8/8、build 与 compileall
通过。Exact commit `3d2114a05b97b2a6a5005242106ebb961df161f9` 的
[Actions run 32470808101](https://github.com/everflowinv/dalton-research-agent-os/actions/runs/32470808101)
中 Python 3.11、Python 3.13、openclaw-broker 三个 job 全部成功；两个 hermetic canary step、3.13 review collector
和 artifact upload 也全部成功。Gate 0 已完成。

Gate 1 已在 clean commit `0b0f872c0f935098f8e41af339a93d8164684992` 运行固定五家公司 batch。
Microsoft、Apple、NVIDIA、Walmart、Amazon 五条同入口 SEC Company Facts plan 全部完成 source/numeric verifier、
正式 Evidence/Claim、Backlog closure 和进程重启后的无网络 replay；重放统一返回 `duplicate`，没有新增网络请求，
每家公司保持 1 条 Evidence、1 条 Claim、0 条 Thesis、0 个人工 gate，全部 SQLite integrity check 为 `ok`。
五家公司季度收入同比依次为 18.30%、16.36%、85.23%、7.33%、19.62%；实际模型调用和成本均为 0，thesis
impact 明确记录为 `not yet run`。Walmart stale `SalesRevenueNet` 控制样本按预期失败，candidate 和正式
Evidence/Claim/Thesis 全部为 0。独立 edgartools 路径复核了 5 个 accession、10 个财务报表数值和 5 个同比计算，
没有差异；同时证明通用 Revenue 关键词路由会对 NVDA/WMT 误选 CostOfRevenue，不能替代 exact duration 和冻结
concept allowlist。结果 bundle hash 为 `7f69dc9a483d3e04cc6c8c6eeb01563ad0e5e94e28d189df34350f812a95844b`，
简报见
[gate1-sec-five-issuer-revenue-growth-2026-08-21.md](reports/gate1-sec-five-issuer-revenue-growth-2026-08-21.md)。
Gate 1 没有 schema 变化，也没有部署 live。Gate 1 完成时的下一门是取得 owner 对具体付费调用和 hard spend cap
的单独授权后运行一条真实 ThesisVersion 的 thesis-impact canary。本机相邻回归 81/81、Python 全量
587/587、broker 16/16、显式 hermetic replay 1/1、build、compileall、结果摘要 JSON 和 `git diff --check`
全部通过。

Gate 2 已在 owner 授权的 USD 1.00 hard cap 下完成真实隔离 canary。MSFT Gate 1 Claim 先由 GPT-5.6 Sol 生成
`insufficient` assessment；worker 在 model accounting 后、Scheduler completion 前退出，lease 过期后通过同一
invocation 的 `replayOnly/duplicate` 恢复，没有第二次 provider call，也没有重复 invocation/usage/cost。独立
verifier 使用 DeepSeek V4 Flash，producer/verifier family 分别是 `openai-gpt-5.6` 与 `deepseek-v4`。本次两条
调用为 3,338 tokens、USD 0.013211；全部已知成本为 USD 0.169986，另为一条无费用遥测的 Claude TIMEOUT 预留
USD 0.25，累计上界 USD 0.419986。离线 replay 使用 deny adapter 禁止 broker 访问，稳定重现正式 `rejected`，
两条模型记账数量不变，Thesis pointer 不变，Core/review/coordinator/router integrity 都是 `ok`。

Gate 2 的控制面通过，模型质量门没有通过。DeepSeek 的五条 rejection findings 中有 ref/hash 自相矛盾，并建议
使用 authority closed taxonomy 之外的 `none / contradictory`；系统仍保留正式 `reject`，assessment 未进入
eligible，也未回写 Gate 1 简报。真实运行还暴露并修复了两个控制缺口：verifier token 预算过小，以及旧 adapter
在 provider telemetry 超预算时先抛错、导致已付费拒绝响应只留在 broker journal。现在超预算内容仍会被拒绝，
但 invocation/usage/cost 会进入 Core。报告见
[gate2-real-thesis-impact-canary-2026-08-21.md](reports/gate2-real-thesis-impact-canary-2026-08-21.md)。

Verifier 校准基础随后冻结 12 个样本：5 pass、7 reject，其中 5 个 high、2 个 medium。gold label 与模型输入分离；
基础冻结时，Gate 2 的 DeepSeek 输出是唯一真实观测，在应 pass 的样本上形成 1 个 false positive。新 WorkOrder 必须返回
严格 `0.2` finding contract，authority 会按 WorkOrder 重验版本，worker 会拒绝与已验证 binding/driver 自相
矛盾的 finding。历史 `0.1` 只保留 replay 兼容。12 个样本小于 30 个放权门槛，因此不能解锁自动化。报告见
[thesis-impact-verifier-calibration-foundation-2026-08-21.md](reports/thesis-impact-verifier-calibration-foundation-2026-08-21.md)。

真实候选校准现已完成。DeepSeek V4 Flash 跑完 12/12：accuracy 75%，7 个错误样本检出 5 个，detection rate
71.4%；5 个正确样本误杀 1 个，false-positive rate 20%；high-severity miss 为 1。另有 3 条输出会被 production
consistency guard 拒绝。Claude Fable 5 第一条实际使用 6,795 input / 958 output tokens，超过 WorkOrder 的
3,000 / 400 上限，费用 USD 0.11585 也超过单条 USD 0.04 admission cap；runner 持久化失败后停止剩余 11 条。
本轮两模型新增实际费用 USD 0.118666；加上 Gate 2 既有 accounted + uncertain reserve 后总上界 USD 0.538652。
没有候选获准上线。报告见
[thesis-impact-verifier-live-calibration-2026-08-21.md](reports/thesis-impact-verifier-live-calibration-2026-08-21.md)。

2026-08-22 校准语料已扩到固定 30-case v0.2。第一轮 shortlist 中 Gemini 3.1 Pro Preview 为 30/30，Claude
Opus 5、Qwen 3.8 Max、GPT-5.6 Terra 均为 29/30；只有前两者没有 high-severity miss，但这轮仍是
`calibration-posthoc-v1`，不能冒充 production provider-control proof。随后 provider direct 测试证明 Gemini 3.7
Flash low 可稳定返回 strict JSON；旧模型回抄 `assessment_ref/hash` 的设计也被替换为 wrapper-owned binding。
模型现在只返回 closed `schema_version/verdict/findings`，trusted worker 从 immutable WorkOrder 注入 exact
assessment identity；raw ResultEnvelope 不改写，authority replay 可重建正式 `0.2` output，历史 WorkOrder 继续兼容。

同一 30-case corpus、strict prompt、temperature 0、thinking low、16k cap 的正式 semantic-only 重跑中，Gemini
3.7 Flash 和 GPT-5.6 Luna 都是 30/30、0 false positive、0 high miss；Qwen DeepSeek V4 Flash 为 27/30，
有 2 个 high miss。Gemini 平均 2.452 秒、P95 3.911 秒，Luna 平均 7.829 秒、P95 12.687 秒；Owner 已选择
exact `google/gemini-3.7-flash`、thinking low 作为主候选，Luna 保留为低成本候选。90 条 raw output 都不含
target binding，wrapper 后 90 条均绑定成功。Python 全量 616/616、broker 21/21、wheel/sdist build 通过。
报告见
[thesis-impact-verifier-wrapper-selection-2026-08-22.md](reports/thesis-impact-verifier-wrapper-selection-2026-08-22.md)。

2026-08-22 后续实现补齐 production verifier 的两项 conformance：新增不可变
`model-routing-policy-version:dalton-openclaw-verifier:1` 只允许 exact `profile:gemini-3-7-flash`，worker 的
verification 相位可改在该 policy 下路由，且未 pin 到单一 profile 的 policy 会在 claim 前 fail closed；
`thinking=low` 现在冻结进 verifier WorkOrder 与 calibration manifest，进入 broker `requiredControls`、
request hash、invocation 幂等身份和 host proof 的 closed 合同（broker 升至 0.1.0-spike.5）。host 侧
provider controls、rate card、thinkingLevel 与 patch 后来已
打通；首次真实 3×30 的 90 次 fresh 调用与质量数据有效，但因三轮 run identity 重复而撤销 production gate。
修正后的 runner 已用三个不同 run identity 重新完成 3×30，90 条均为 fresh execution，三轮均 30/30，
`production_gate.eligible=true`；随后单条 isolated shadow 也达到 `eligible`，并保持 ThesisVersion pointer 不变。
Owner 后续授权 production activation，当前 live 服务已经安装独立短任务、phase policies、USD 25 day cap、
scoped writer principal 和回滚快照。由于没有 live thesis target，activation 后 provider call 和 Thesis mutation
均为 0。部署记录见
[thesis-impact-production-activation-2026-08-22.md](reports/thesis-impact-production-activation-2026-08-22.md)。
phase-pin/thinking 批次的本机验证：Python 全量 621/621、broker
22/22、显式 hermetic research replay canary、compileall、wheel/sdist build 与 `git diff --check` 全部通过；
没有付费调用。报告见
[verifier-phase-pin-and-thinking-controls-2026-08-22.md](reports/verifier-phase-pin-and-thinking-controls-2026-08-22.md)。
同日后续新增 `dalton-thesis-impact-verifier-canary` campaign runner：把 3×30 开闸条件收敛为一条显式授权即可
执行的命令，内置 per-case/per-round/campaign 三重硬顶、逐轮验收（0 FP、0 high miss、0 schema/control
failure、thinking 与 provider-control 合同逐 record 绑定）与 `production_gate` 裁决；campaign 自身不发起
未授权付费调用，测试无网络无付费；本机验证 Python 全量 628/628、hermetic replay canary、compileall、
wheel/sdist build 与 `git diff --check` 全部通过。报告见
[verifier-canary-campaign-runner-2026-08-22.md](reports/verifier-canary-campaign-runner-2026-08-22.md)。
同日再后续补齐 v0.7 Gate 3 控制面：新增 `thesis_impact_budget` authority（独立 owner-only SQLite，第 17 份
packaged SQL schema）提供版本化 per-day 硬顶、per-(work order, attempt, phase) admission/settlement（未结算
按全额保守占额）、durable rejection（被拒身份不可复活）与 append-only 告警链（claim TTL、投递上限 5）；
worker 可选接入——超顶在任何 broker 调用前落成正式 `DAY_BUDGET_EXCEEDED` 失败与 high 告警，终态失败记录
`work_order_failed` 告警，未配置时行为不变。故障注入已证明超预算停止并留下 decision、重放不产生新事实；
本机验证 Python 全量 636/636、broker 22/22、hermetic replay canary、compileall、wheel/sdist build 与
`git diff --check` 全部通过，没有付费调用。报告见
[thesis-impact-day-budget-and-alerts-2026-08-22.md](reports/thesis-impact-day-budget-and-alerts-2026-08-22.md)。

现有 versioned governance policy 可分别只允许 closed SEC public `10-Q list_filings` 或 exact
`10-Q get_company_facts` plan 自动启动；
其中 `list_filings` 结果只有在 Core 从 exact
CallSpec、SourceEnvelope 和 Artifact 重新推导 CIK、表单、日期窗、记录数与完整 statement，并命中固定
`filing_count` rule 时才能自动写 EvidenceVersion 0.2、ClaimVersion 0.2 和
supports relation；`get_company_facts` 结果则必须从 exact Company Facts raw body 重放最新 10-Q accession、冻结
allowlist 选择、current/prior quarterly fact，并由 numeric verifier 独立复算 `growth_percentage` 后才能自动提交。
ClaimIndex status 派生现已改为读取
Core 的一致 Ledger snapshot，绑定 snapshot ref/hash，并拒绝
caller-provided status；DocumentIndex FTS5 已完成开发候选；ContextPack authority-bound materializer 已完成
claim/artifact 只读切片，并已接通 Agenda 的 mandate/perception exact reader。PerceptionSnapshot 现在进入 Core
append-only authority，Agenda 使用独立 AgendaContextBinding，模型只读取固定 instruction/output contract 与
materializer quoted JSONL；可变 snapshot 文件不再参与 replay 或 prompt。ResearchQuestionBacklog 开发候选已
完成：稳定 question 身份、冻结状态机、AgendaDecision 链接、正式 ClaimVersion answer 绑定与 Mandate 进度
投影，问题现在可以跨 cycle 存续。Planner 薄闭环也已完成开发候选：exact selected AgendaDecision/
ResearchQuestionVersion → immutable ResearchPlanVersion → WorkflowRunVersion/WorkOrderLink 任务树；首版只允许
无凭据 SEC public `list_filings`；人可以批准 plan，active versioned policy 也可以只对 closed low-risk scope 签发
独立 authorization，automation 不能伪装成人。启动只把根 connector WorkOrder 放入
Scheduler，下游 resolver/verifier/candidate staging 保持 planned，必须由 coordinator 在上游 exact result 后逐项
admission；开发候选 coordinator 已完成 exact Scheduler/connector receipt/runner journal/内部阶段输出证明核对，
每次只 admission 直接子节点，重放收敛且篡改 fail closed。ResearchPlanExecutor 现已把四个真实节点接到各自
authority，并跑完第一条真实 SEC public WorkOrder 树。首条 exact candidate 已获 explicit accept，正式
Evidence/Claim/Relation 与 Backlog answer 也已写入隔离 authority；新 closure coordinator 会重验
plan→final candidate→authorization→formal promotion 全链，并在崩溃后收敛到同一 answer binding。当前自动规则
不接受 model-written interpretation、candidate revision、凭据来源、非完整枚举、其他 source/metric/form 或越界
预算；这些情况进入 human review / revise / reject 异常通道。HumanReviewAuthority 的 revise consumer 只允许人工
改写 `normalized_statement`，source、numeric、period、evidence 和 provenance 不变，其他变化回到新的 verified
plan run。Interrupt / park / resume 与 Reflection 继续顺延，不增加没有真实消费者的内核子系统。当前没有 live
policy activation、凭据或旧 cron cutover。
万华的 10 个工作日/20 个显式人工标签门槛
只限制 Agenda 从 1 家扩到 3 家。第一条真实闭环和至少 1 条人工接受的正式 Claim 门槛已在隔离 canary 达到，
但它只证明机制闭环，不证明研究质量或生产就绪。后续增量必须回指真实审阅数据或明确解除覆盖阻塞；与质量缺口
无关的新 connector 品类、Model IR、sandbox 和其他内核扩建继续后置。任何研究执行开闸、生产部署或旧 cron
cutover 仍须单独验收。

### P2 DocumentIndex FTS5 当前进度（开发候选，未部署）

- 新增 `DocumentIndexInput`、`DocumentIndexSnapshot` closed contract，以及 owner-only SQLite FTS5
  projection。投影只读 exact `ObservabilityStore`、`ConnectorStore` 和 `RawSpool`；rebuild/clear 只改
  自己的 disposable 数据，不提供 Artifact、Ledger 或 connector authority mutation API；非内存文件强制
  `0600`；
- 内置 `utf8`/canonical `json` extractor 直接从已复核 ArtifactVersion hash+size 的 raw bytes 派生正文，
  caller 不能提交正文或自报 metadata。`content_type` 对应 ArtifactVersion `kind`，`media_type` 单独过滤；
  默认只返回 `public`，但 projection 不是同 UID 下的多租户安全边界；配置为可见的 internal/restricted
  内容仍可能物理存在于 disposable FTS 文件；
- source join 沿 SourceEnvelope → SQL execution link → Profile/CallSpec → connector ExecutionInvocation
  复核 exact ref/hash。`source_type` 从 Profile source identity 派生；只有 SEC
  `source:sec-edgar/list_filings` 的 `issuer` 能生成 `company:sec-cik:<10位CIK>` facet，unknown source 保持
  空 facet；rebuild 和查询都会检查 FTS、主表、facet 和 record hash 的一致性；
- FTS 使用 `trigram`。三字符中文（如“半导体”）可有限命中，两字符（如“存储”）可能 miss；这不是通用
  中文分词。SEC submissions JSON 只按 connector response/filing metadata 处理，不能称为 filing 正文全文；
  embedding 尚未实现；ContextPack materializer 支持 exact ClaimVersion 0.1/0.2、ArtifactVersion 0.1/0.2、
  MandateVersion 与 PerceptionSnapshot，SourceEnvelope 正文类型仍 fail closed；它从 Ledger/Observability/RawSpool/Core
  重读 authority，不能把 caller 正文、DocumentIndex FTS 正文、transcript 或 compaction summary 当事实；
  输出是短生命周期 quoted JSON-lines render 加不含正文/path/locator/credential 的 hash manifest，header/分隔符
  开销计入预算，不能超预算静默裁剪；现已接 AgendaCoordinator，仍未部署、未改 cron；
- `tests/test_document_index.py` 覆盖 raw hash/size、authority hash rebinding、source/profile/call link、
  access/filter forge、FTS `delete-all` checksum、FTS/main-table sync、query boundary、Unicode、删除重建和
  文件权限。该 slice 未部署、未接 Agenda/cron；Agenda materializer 不读取 DocumentIndex FTS body；
- broker 回归 15/15；固定 `SOURCE_DATE_EPOCH=1700000000` 独立构建的两份 wheel 逐位一致，SHA-256
  均为 `ccd4ad817cf1837ed2e99d48b1cdd1b23e543dcadafede8a72921ff70a3cd5c8`，大小均为 601,297 bytes；
  干净 Python 3.13 venv 安装、导入、打包后的 FTS schema 和两份新 contract 检查均通过。

### ContextPack materializer 当前进度（开发候选，未部署）

- 新增 `ContextMaterializer` 与 `ContextMaterialization` closed contract。materializer 要求 exact
  `DaltonStore` 与 `ObservabilityStore`；artifact 路径另要求 exact `RawSpool`。当前支持 `claim`、`artifact`、
  `mandate` 和 `perception`，source 正文仍 fail closed；可见 `access_class` 默认只有 `public`，扩大范围必须在
  materializer 实例显式配置；
- ClaimVersion 0.1/0.2 从 Core `claim_versions` exact row/record 读取，复核 id、version、prior、created_at、
  SQL column、canonical record hash 和对应 validator；render 同时携带 pack 冻结的 ClaimIndex entry，保留
  `proposed/corroborated/contested/superseded/retracted` 状态，但不把该投影冒充 ClaimVersion authority；
  ArtifactVersion 0.1/0.2 从 Observability API、跨代
  index、record row 及 RawSpool 复核 hash/size，正文只用内建 `utf8`/`application/json` extractor，storage
  locator 只用于 authority 校验，不进入 manifest；materializer 不读 DocumentIndex FTS body；
- materializer 可从 exact authority refs 构建 ContextPack 0.1 的 authority-bound input accounting。旧的
  caller-content pack 即使 ref/hash 合法，只要原文 token/byte 与 authority 正文不一致也拒绝；不重选、不截断。
  render 使用固定 quoted JSON-lines 边界，prompt-like 正文只在 `quoted_data` 中出现；ContextPack 的正文选择
  预算与 materialization 的 envelope-inclusive 总预算分开记录，header/分隔符必须计入后者；manifest 记录每项 authority/body/render 账、omission/failure 账、
  renderer/tokenizer ref/hash 与最终 render hash，不持久化正文、路径、locator 或 credential；
- `tests/test_context_materializer.py` 覆盖 23 个专项：claim/artifact、ClaimVersion 0.2 Decimal/structured period、
  caller text/hash rebinding、SQL/raw
  tamper、跨代 Artifact index、duplicate/omitted、正文/总预算、确定性、access class、unsupported kind/media、
  JSON/CJK、prompt-like quoted data、冻结 builder/selector/tokenizer/truncation、历史 pack replay、
  plan/ClaimIndex binding、敏感字段与 authority 行数不变。Agenda 接线由下节单独验收；整体仍未部署、未改 cron。
- 本地专项 23/23、materializer/coordinator/DocumentIndex/ClaimIndex 相关 57/57、Python 全量 423/423、
  broker 15/15、`compileall`、95 份 JSON schema、16 份 SQL schema 和 `git diff --check` 均通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel SHA-256 均为
  `e61d35359d52a169c8abd4df7628836715038064ff5167e917c1c3cd007ebd21`，611,413 bytes；Python 3.13
  干净安装、公开导入、新 contract 与共享 extractor/tokenizer 资源检查通过。

### Agenda context authority 当前进度（开发候选，未部署）

- 新增 Core `perception_snapshot_versions` append-only authority；authorized insert 与 no-update/no-delete trigger
  同时生效。AgendaCycle 启动时重读并核对 exact PerceptionSnapshot、MandateVersion 和 AgendaPolicyVersion；
  cycle row 冻结三者的 exact hash，active policy/mandate 读取也改走 canonical row/hash 复核；
- 新增 closed `AgendaContextBinding`，直接绑定 exact Cycle/Policy/Mandate/Perception ref/hash，不伪造
  CompiledConnectorPlan。ContextMaterializer 的受控 union 保持旧 connector plan replay，同时增加
  mandate/perception authority reader；writer 只允许 core principal 按 cycle ref 和预算读取，不接受正文、路径、
  callback、DB 或 caller timestamp；
- AgendaCoordinator 已删除手工 `MANDATE=`/`PERCEPTION=` 拼接。最终 prompt 只有固定 instruction/output contract
  与 materializer quoted JSONL；allowed source refs 和 company 只从 exact PerceptionSnapshot authority 派生；
  完整 prompt 使用冻结 tokenizer 核算 `max_input_tokens`，任一 required input 被预算丢弃即 fail closed；
- Agenda 专用 renderer 绑定 AgendaContextBinding；ContextPack 0.1 的必填 ClaimIndex 字段使用只允许 Agenda
  binding 消费的显式 no-index sentinel，不扫描 Ledger。无关 Claim/Ledger 增长不能改变 pack、manifest、prompt、
  WorkOrder 或模型调用幂等键；
- 专项 51/51、Python 全量 460/460、broker 15/15、`compileall`、96 份 JSON schema、16 份 SQL schema 与
  `git diff --check` 均通过。固定 `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel SHA-256 均为
  `b66589f8e28f6b10fd7f0c44bffe37ba6de97ce5b1c95add57dbe9da59dd0ba9`，大小均为 622,505 bytes；Python 3.13
  干净安装、AgendaContextBinding contract、Agenda schema/migration 与公开导入检查通过；
- 本切片未部署、未接 Backlog/Planner、未改变 auto-accept/timeout 权限、未改 cron。旧 live cycle 若没有已登记的
  PerceptionSnapshot 会 fail closed；部署前需单独裁决 backfill 或从新 cycle 开始，不能静默信任旧 snapshot 文件。

### ResearchQuestionBacklog 当前进度（开发候选，未部署）

- 新增 Core append-only ResearchQuestionBacklog authority：`question_ref` 由 canonical
  `{mandate_ref, company_ref, question}` 绑定确定性派生，caller 不能提供 id/hash；内容版本行不可变带链；
  冻结状态机 `open → selected → planned → in_progress → answered | blocked | retired`，每个迁移在同一
  Core 事务内校验并追加不可变 event，非法/乱序/重复迁移 fail closed，无恢复迁移；
- 同一问题跨 cycle 保持同一身份：相同绑定+相同内容幂等返回既有 head（duplicate），相同绑定+不同内容
  fail closed（conflict）；`backlog_idempotency` 沿用 agenda 幂等约定，同 key 不同 request 返回 conflict；
- `select_question` 在事务内重读 exact AgendaDecision/AgendaCycle/MandateVersion，要求 cycle mandate ==
  问题 mandate、cycle company == 问题 scope、decision 的 selected candidate 与问题内容逐字一致，
  并核对回答标准与 `source_refs`；读取 link 时再次复核 event、decision、cycle、candidate、policy 和
  backlog head，跨 mandate/跨公司/来源换绑/伪造 decision fail closed；
- `answer_question` 只接受正式 ClaimVersion：逐条从 Core `claim_versions` 重读、重算 hash、按 0.1/0.2
  重新校验闭合形状，candidate/staging/缺失/篡改 claim 拒绝，读取 answer binding 时再次核对 exact
  ClaimVersion，`candidate-claim:` 前缀显式拒绝；
  AgendaDecision 永远不会成为 answer；
- `mandate_progress` 是纯确定性、可重建的进度投影：绑定 active MandateVersion ref/hash，统计各 state
  计数与 answered claim refs；不写任何表、不改 MandateVersion authority、不成为替代 authority；
- Backlog authority 本身不创建 plan/WorkOrder/DAG；Planner 开发候选现已接管 `selected → planned` 与
  `planned → in_progress`，两次迁移都要求 exact plan/start binding 并与对应 authority 同事务写入；
  无 auto-accept 路径；
  专项 34/34，Python 全量 494/494，相关回归 82/82、broker 15/15、101 份 JSON contract 解析、
  16 份 Core SQL schema、`compileall`、`git diff --check`、SQLite integrity/FK 与 deterministic wheel
  检查全部通过。
- 仍未部署、未接 cron、未改变 auto-accept/timeout 权限；Planner 开发候选已接入 exact plan/start binding，
  Agenda Shadow 旧 `research_question_versions` 写路径保持不变，与 backlog 并存。Backlog 初始切片见
  [research-question-backlog-2026-08-15.md](reports/research-question-backlog-2026-08-15.md)。

### Planner SEC public 薄闭环当前进度（开发候选，未部署）

- 新增 append-only ResearchPlan authority。plan 身份由 exact ResearchQuestionVersion、selected
  AgendaDecision 与规范化 SEC request 确定性派生；在创建事务内重读完整 backlog/Agenda/context authority，
  候选位置、问题、回答标准、来源、company 或 mandate 任一换绑都会 fail closed；
- 首版执行范围固定为无凭据、公开只读的 SEC `list_filings`，只接受 10 位 CIK、`10-K`/`10-Q`/`8-K`
  和不超过 366 天的窗口。plan 冻结 profile、operation、verifier、runtime、capability、输出 contract、预算
  与 side effect，caller 不能扩充 host、credential、步骤或写权限；
- 每份 plan 确定性生成 connector → authority resolver → source/numeric verifier → candidate staging 四个
  WorkOrder 和三条 WorkOrderLink。启动时写 WorkflowRunVersion 与完整任务树，但只 admission 根 connector
  WorkOrder；三个子节点保持 `planned`，要由 coordinator 在 exact 上游结果后逐项 admission；
- plan 必须由 exact `human:<principal>` 写一次终态 accepted decision；model、automation、timeout、Agenda
  approval 和 auto-accept 都不能启动 plan。未批准和 rejected plan 均 fail closed；
- start 在 Scheduler、workflow/link 和 Core binding 接缝使用确定性身份；外部接缝或事务内故障后重放会收敛
  到同一个 start、同一棵任务树和一个根 WorkOrder。exact readers 会重新核对 plan/question/approval/start/
  workflow/link/Scheduler 双向绑定，后续 authority 篡改同样 fail closed；
- Planner 专项 13/13、Planner + Backlog 47/47、Python 全量 507/507、broker 15/15 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 两份 wheel 逐位一致，SHA-256
  `466935efa4684e7384b9b050002e642e648f848968442fb0a6a71850acb3ca38`，666,164 bytes；Python 3.13
  干净安装、公开导入与 packaged SQL 检查通过；
- 未部署、未访问真实 SEC、未创建 CapabilityLease/CredentialGrant、未自动写 Ledger。rejected plan 后问题仍
  停在 `planned`；replan/park/resume/retire 的产品语义继续保留为缺口，但在真实研究闭环和质量校准之后再设计。
  完整结果见
  [research-plan-thin-closure-2026-08-15.md](reports/research-plan-thin-closure-2026-08-15.md)。

### ResearchPlan coordinator 当前进度（开发候选，未部署）

- 新增唯一的下游逐项 admission 边界。caller 只提交 plan ref 和 upstream WorkOrder ref；coordinator 每次
  重读 exact plan/start/workflow/link、Scheduler WorkOrder/policy/attempt/lease/formal result/ResultEnvelope，
  不接受 caller-supplied success boolean 或 opaque payload；
- connector 根节点必须有 exact compiled request、completion receipt 与 Scheduler result 绑定。v0.2 receipt
  还要重读 Core runner journal 中的 actual request、完整事件 hash 链和终态 `responded` response；
- 三个内部节点必须返回封闭、可哈希的 `ResearchPlanStageOutput`，绑定 exact plan/step/output contract、直接
  上游 WorkOrder/result 和阶段规定的 typed ref/hash records。每次只 enqueue 直接子节点，不能越级或批量放行；
- child identity 和 idempotency key 由 immutable plan 派生。重复调用、enqueue 后崩溃重放都会收敛到同一节点；
  错误 plan/workflow/upstream/result、缺失 receipt、attempt/formal result/ResultEnvelope/receipt/child tamper，
  以及 failed/expired/plan-attempt-exhausted 都 fail closed；
- coordinator 专项 12/12、相邻 Planner/Scheduler/research coordinator/packaging 回归 55/55、Python 全量
  519/519、broker 15/15、`compileall`、106 份 JSON contract 与 `git diff --check` 均通过。固定
  `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel 逐位一致，SHA-256 均为
  `cbbd4feb139e764f7217fd6644001a8e2df2d6c9c5f3aae8da2da3f961052364`，大小均为 677,254 bytes；
  Python 3.13 干净 venv 安装、公开导入和 packaged stage-output contract 检查通过；
- 本切片没有接真实四步 executor，也没有从 resolver/verifier/candidate staging 的 authority store 重读 typed
  records 正文；它证明 admission 控制语义，不证明四步计划已真实执行或投研产物有价值。完整结果见
  [research-plan-coordinator-admission-2026-08-15.md](reports/research-plan-coordinator-admission-2026-08-15.md)。

以上是 coordinator 切片交付时的历史结论。后续 executor 与真实 canary 进展如下。

### ResearchPlan executor + SEC public canary 当前进度（开发候选，未部署）

- `ResearchPlanExecutor` 已把 connector、authority resolver、source/numeric verifier、candidate staging 四个
  WorkOrder 接到各自 authority；coordinator 仍按 exact 上游结果逐边 admission，executor 不接受 caller
  伪造的 stage payload，也不会越级执行；
- 2026-08-20 在 owner-only 临时目录跑完一份人工批准的隔离 plan。真实请求只访问
  `data.sec.gov`，无凭据、不打开 live DB；四个节点均 `queued → succeeded`，最终停在
  `human-review-ready-candidate`；
- canary 枚举 Microsoft CIK `0000789019` 在 `2026-01-01..2026-08-17` 的 `10-Q`，得到 2 条官方 filing。
  candidate 的 `semantic_verification_status` 仍为 `unverified`，正式 Ledger 中 Evidence、Claim、Thesis 均为 0；
- 第一次错误窗口暴露出 transport 把 `error.retryable=false` 仍写成 Scheduler `retryable`。开发候选已改为保留
  adapter 的 retryability：不可重试 normalization failure 直接终止，429/timeout/adapter crash 仍按原策略
  重试；新增专项回归并通过；
- canary 的 4 个 SQLite authority 均通过 `PRAGMA integrity_check`。完整记录见
  [research-plan-executor-sec-canary-2026-08-20.md](reports/research-plan-executor-sec-canary-2026-08-20.md)。

### Agenda live 恢复（2026-08-20）

- 保留原 append-only 记录，用 correction 消除 1 条当前未定价成本，并为 2 条已计量但缺 cost 的 UsageEntry
  补齐价格链；当前 `missing_cost_count=0`、`current_unpriced_count=0`，Core/Scheduler integrity 均为 `ok`；
- `03ea471` 最小热修复已装入 live runtime：provider 不返回 token split 时使用 admission 时冻结的 route estimate
  记一笔 request cost；模型 profile 刷新后，新的 input/output price rate 版本会链接上一版，不再触发 immutable
  fork conflict；旧基线完整回归 196/196 通过；
- controller、writer、control、projection、backup、outbox 和 dashboard 当前健康。8 月 20 日已开始的旧 Agenda
  cycle 保持 append-only terminal `failed`，不会改写成成功；daily cycle 上限仍为 1，未擅自扩大，下一次正常
  live cycle 要等下一个日历周期。

### Connector P0-0 当前进度（未部署）

- `ExecutionInvocation` 通用超类型、Model 1:1 subtype link 和新调用原子双写已实现；
- ArtifactVersion v0.2 改用 `producer_execution_ref`，跨 v0.1/v0.2 版本索引和 dashboard projection
  已接通；
- Scheduler 已支持 `retry_at/not_before`，新 attempt event 使用 `wire_version=0.2` 声明 hash epoch；
- 203 项 Python 测试通过，含回填冲突回滚、跨代 artifact 分叉、投影和 Retry-After 幂等专项测试；
- 生产 Core/Scheduler SQLite 的临时副本已完成 startup backfill 演练：2 条 ModelInvocation 全部建立
  execution link，72 条 v0.1 artifact 全部进入跨代索引，两个副本 integrity 均为 `ok`；
- `79bca15` 本身不含 connector authority；当前 dirty P0-1 在它之上继续实现，不能把两者混成一个
  已部署基线。

此前 Connector 报告把未实际发生的 Fable 5 复核写成事实。报告已更正；随后完成的真实独立审阅结论
是“有条件 Go”。

### Connector P0-1 当前进度（authority foundation 已提交，未部署）

- 新增 `ConnectorStore`，已把 connector authority DDL 接入 trusted `DaltonStore` transaction；
- profile、call spec、logical invocation、physical attempt、Usage/Price/Cost、quota
  reservation/settlement、SourceEnvelope、incident 和 source-health 已有闭合 wire contract；
- quota admission 使用 SQLite `BEGIN IMMEDIATE` 串行化，当前只支持固定 UTC window；quota 按稳定
  scope 跨 policy version 聚合，只有 exact active policy 能 admission；每个 physical attempt 必须先
  预占 1 次调用，并同时检查并发、calls、bytes、records 和 cost_micros；
- reservation 的创建、到期和 window 边界约束 attempt 开始时间；所有当前 attempt outcome 都是终态，
  `completed_at` 不得晚于 authority 的记录时间，429 还要求 `retry_at > completed_at`；
- `Usage → Cost → Settlement` 已做逐级精确绑定；consumed 只接受 final Usage 和按 frozen price book
  计算的 actual CostEntry，
  quota policy 冻结币种；Usage/Cost correction 领先 settlement 时 admission fail closed 并开启 blocking
  `quota_drift` incident；correction 即使来自旧 quota window，也在写入同一事务立即开 incident；actual
  overage 也在 settlement 同一事务开启 incident；
- RatePolicy 冻结 exact price refs、required meters 和 price-book hash；注册时必须枚举整个 policy interval
  的完整 canonical price book，免费来源也要显式登记 zero-rate；同一 profile/meter/currency 的 rate 生效
  区间不得重叠；admission 按当前时点重新核对 price book，再按 profile 最大 bytes/records 与固定 call 数
  计算保守成本下限，调用方不能低报 `reserved.cost_micros`；Cost 还会按 physical attempt 的开始时间复核，
  漂移时不写 Cost，并持久化 blocking incident；
- SourceEnvelope 必须匹配 profile/call spec、同一 execution 生产的 ArtifactVersion v0.2、raw hash、
  source/schema/policy/provider request、明确的 result attempt 和 outcome；source/schema/content hash 都
  绑定 source identity、schema bundle 和规范化 metadata/record refs；content hash 不声称绑定 record body；
  profile 还冻结 runner environment hash；
- CallSpec 拒绝 credential-shaped 参数；
- blocking incident 会阻止新 reservation；429 physical attempt 单独记录 `retry_at`；
- `docs/CONNECTOR_PROTOCOL.md` 与 `ConnectorProposalManifest` 已定义 Dalton 自建 connector 的离线生成、
  replay、双人工 gate 和静态 resolver 边界；
- Fable 5 已做六轮只读敌对复核：前五轮持续发现并复现 authority 漏洞，第六轮冻结复核结论为
  **技术 Go**。当前可提交为 **P0-1 authority foundation**；connector 专项 23/23、Python 全量
  226/226、broker 15/15 通过。这个 Go 不包括部署、真实 connector 或 E1；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel 构建得到相同 SHA-256：
  `825f07246ed13afff39ac0c5201242ca4e756542332d6590cd40bbb6a6d7a8c5`；隔离安装后可创建 16 张 connector 表；
  live Core DB 的只读 backup 副本完成 P0-0 backfill 与 ConnectorStore 建表，2/2 model execution links、
  72 条 artifact index、16 张 connector 表，integrity 为 `ok`；
- 本机系统 Python 的 `python3 -m build` 仍因已安装的 `build` 包没有 `build.__main__` 而失败；独立 venv 的
  `pip wheel --no-build-isolation` 已通过，这不是 Connector 代码验收通过的替代条件，也不隐藏该环境问题；
- P0-1 authority foundation 已提交为 `c4e78db`；完整 Connector P0、Runner、writer RPC、真实 adapter、
  dashboard projection 和第一条真实 A 股公告 connector 尚未完成，因此不能报完整 P0 或 E1 通过。

### Connector P0-2a 当前进度（control-plane foundation 完成，未部署）

- 新增闭合 `ConnectorRunnerRequest`、`RunnerEnvironmentManifest`、`ConnectorAdapterRequest`、
  `AdapterTransportObservation` 和 `ConnectorRunnerResponse` contract/schema；
- Scheduler 新增 exact-current lease use-time gate；旧 revision、错误 hash 和过期 lease 都 fail closed；
- `StaticAdapterResolver` 只接受 operator 注入的 callable 与冻结 binding，禁止从 proposal path 动态
  import/exec；CapabilityDescriptor 的实现来源与 ConnectorProfile 的目标数据源分开建模，live descriptor
  contract 的 adapter/input/output schema refs 必须与 Profile/manifest 一致；
- Runner admission 从 Core、Scheduler、CapabilityCatalog 和 ConnectorStore 重读 authority，外部 request
  只携带 refs/hashes；静态 input validator 按冻结 schema 检查完整 parameters；内部 AdapterRequest 的
  parameters、host、policy、deadline 和 response 上限只使用最后一次 authority 重读结果；
- Runner 专用 reservation API 派生 exact active policy version、连续 attempt、Profile 最大 bytes/records、
  保守成本和 TTL；transport gate 只接受唯一 open reservation，并再次核对 hash、有效期、price book、
  blocking incident 和 circuit state；reservation 验证后再做最终 Scheduler/Capability use-time gate；
- P0-2a 只接受 `auth_mode=none`，不接受任何 credential grant；raw sink handle 由 Runner 按
  invocation/reservation/attempt 确定性派生，调用方不能传路径或 handle；
- Runner reservation 在 `BEGIN IMMEDIATE` 内核对 next authority attempt、同一 invocation 的 pending
  唯一性并写入；`max_concurrency=2` 的两个独立 SQLite connection 并发探针最终只产生 1 行 reservation；
- live Descriptor 还必须满足 `kind=connector`、`mode=typed_call`、`instruction_ref=null`；Manifest、
  AdapterRequest 和 Profile 的 auth/credential、public-only、redirect 条件已在 JSON Schema 与 Python
  validator 两边统一；
- Fable 5 前两轮复核均为 No-Go，第三轮在复跑旧探针及上述并发/Schema/Descriptor 探针后给出
  **Go**，只批准 P0-2a control-plane foundation；相关测试 58/58、Python 全量 236/236、broker 15/15；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel 构建得到相同 SHA-256：
  `0e198638dcde7b52ccc756715eef20da5ca9a1ead8f91fb72cb1c8d3f2d25881`；隔离安装可导入 Runner、创建
  16 张 connector 表，integrity 为 `ok`；
- 当前仍只是 control-plane seam：没有执行 adapter、没有 credential grant、durable runner journal、raw spool、
  writer authority port、SSRF transport 或 recorded success/429/crash replay，不能称 P0-2 完成。

### Connector P0-2b 当前进度（recorded transport thin slice 完成，未部署）

- Fable 5 先做独立架构复核，结论为“有条件 Go”；P0-2b 仍是正确下一阶段，但必须先修正非成功
  RunnerResponse 无法表达没有 raw artifact/SourceEnvelope 的契约缺陷；
- `ConnectorRunnerResponse` 发布 wire 0.2：成功必须带 raw artifact 和 SourceEnvelope，429/timeout/failed
  可以把两组 ref/hash 同时置空，禁止用空 artifact 凑契约；其余 connector contract 不原地改 epoch；
- Core DB 新增 append-only runner request/event journal，唯一 `transport_started` barrier 决定恢复只能
  released 还是必须 indeterminate；journal 不属于 Research Ledger，也不保存 Scheduler lease token；
- raw spool 使用 write-only bounded sink、SHA-256 content address、原子 finalize、同 hash 去重、全局高水位
  和 orphan partial GC；adapter 不接收路径；
- 窄 `ConnectorAuthorityPort` 只开放 attempt、Usage、Cost、Settlement、ArtifactVersion v0.2、
  SourceEnvelope 和 Scheduler completion 七类写入，不暴露 connection；
- recorded success、empty、429、timeout、adapter exception 已执行完整事实链。timeout 由 Runner hard
  watchdog/deadline 判定，observation 不能自报；非最终计量使用 reserved cost upper bound 并按
  indeterminate 结算；
- W0–W4 以及 attempt/Usage/Cost/Settlement/Artifact/Source/Scheduler 每个写入缝隙都做故障注入；恢复后
  第二次重放零新行。journal 完全缺失的 reservation 也按 indeterminate，不误放为 released；
- P0-2b 不含真实网络、SSRF、credential authority、writer RPC、metadata importer、spool lifecycle、
  ContextPack/Checkpoint/ClaimIndex、部署或真实数据源；Python 254/254、broker 15/15、专项 18/18 和
  确定性 wheel 已通过，Fable 5 最终复核及增量复核均为 **Go**；实现提交 `f0e824f`，GitHub CI 最终
  全部通过。完整结果见本轮实施报告。

### Connector P0-3 当前进度（metadata + public transport safety，未部署）

- 新增闭合 `OpenClawCapabilitySnapshot` 和 `OpenClawMetadataImporter`：skill 只导入 compact metadata、
  opaque instruction ref/hash；MCP 只导入 metadata 与闭合 input/output JSON Schema。skill 正文、prompt、
  tool output、路径、server config 和 credential 不进入 Dalton；
- imported skill/MCP 仍只是候选 metadata，不能自动进入 CapabilityCatalog。`publish` 继续要求现有 human
  promotion receipt，并强制 descriptor 的名称、摘要、source、contract、source/schema hash 与 current
  imported metadata exact match；caller 不能借 importer 改写摘要或权限；
- 完整 scope 中的 source/schema/metadata 变化或 capability 删除，会在同一事务中撤下 current descriptor
  projection 并推进 catalog epoch；旧 lease 因 epoch 变化 fail closed。重复 snapshot 不推进 epoch；
- 新增 credential-free `PublicHttpTransport`：只允许 exact-host HTTPS/443，拒绝 URL userinfo、credential-
  shaped query/body/header、环境代理语义和非幂等 redirect；每一跳都重新检查 allowlist、DNS 全量 IP 与
  redirect，任一 private/loopback/link-local/reserved 地址即拒绝，并把 socket pin 到已验证 IP、保留原
  hostname 做 TLS SNI/证书校验；response size 同时检查 Content-Length 和 streaming bytes；
- 新增 closed `CredentialGrantEnvelope` 与 `CredentialAuthorityPort` 边界。Core 只见 grant metadata 和
  logical slot ref；credential value、OAuth/MCP auth 与不可序列化 handle 留在 host-owned authority。
  Public transport 的 API 不接受 credential grant，现有 ConnectorAdapterRequest 0.1 仍强制
  `credential_grant_ref=null`；
- 当前只完成离线 control-plane/transport component。尚无 OpenClaw live exporter/sync daemon、真实 HTTP
  call、authenticated runner、A股/SEC/AlphaEngine connector、dashboard connector projection、部署或数据
  源访问。AlphaEngine 的 `mcp_managed` profile/runner wire 需要独立版本，不能把 loopback MCP 塞进 public
  HTTPS `allowed_hosts`；
- importer/public transport/credential 专项 15/15、Python 全量 269/269、broker 15/15、`compileall` 和
  `git diff --check` 全部通过。固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel SHA-256 均为
  `c9af233004f0a6bed406572f97c1802cef06ddefc20b0c17728302bf7138ac86`；隔离安装可导入三个新模块、
  创建 6 张 external metadata 表并找到 2 份新 contract，SQLite integrity 为 `ok`。P0-3 复核时 system
  Python 3.13 曾因缺少 `setuptools.build_meta` 无法走 no-build-isolation；当前项目 `.venv` 是 Python 3.13.14，
  已含 setuptools 84.0.0 与 `setuptools.build_meta`，本轮 no-build-isolation 重复构建通过，不需要全局安装；
- 实现提交 `e1ab94c`；GitHub CI 的 broker、Python 3.11 和 Python 3.13 全部通过：
  <https://github.com/everflowinv/dalton-research-agent-os/actions/runs/31828754012>。

### Connector P0-4a 当前进度（trusted metadata sync + Connector Shadow projection，未部署）

- `OpenClawCapabilitySnapshot` 发布 wire 0.2，新增 exporter source instance、exporter version、严格递增的
  catalog generation 和 exact prior snapshot ref/hash；
- Catalog 内部新增 trusted source registration 和 per-source head authority。只有 operator resolver 返回的
  active human registration 才能启用新 source instance；实例更换会撤下旧实例的 current metadata 与
  external-scope descriptor、推进 epoch，旧实例也不能自行复活；registration receipt 还绑定 reset 前的
  exact active source/hash，两个并发 reset 只能有一个成功；首次注册也会撤下 P0-3 legacy current state；
  wire 0.1 禁止有限 expiry，只能显式换实例；
- 同 source instance 只接受 exact next generation 与 exact prior head。同 generation/同 hash 是幂等重放；
  stale、gap、fork、equivocation 和未注册 source 都 fail closed，只追加脱敏 ingest event，不改 current
  metadata、descriptor projection 或 catalog epoch；
- host-owned exporter 使用 owner-only SQLite 保存一个 pending snapshot，Catalog 已接受但 exporter 尚未
  acknowledge 时，重启后会重放同一 snapshot，不会跳 generation；exporter 只接收已过滤的 compact
  skill/MCP records，不保存路径、instruction、server config 或 credential；
- skill 的 approval-bound schema hash 现在也绑定 exact upstream metadata hash。prompt-like description 可以
  原样进入隔离 staging，但不能在 human approval 前进入可搜索 Catalog；
- P0-3 既有 SQLite 不改写 snapshot 表：wire 0.2 的严格 source/generation/prior/FK/unique/check authority 放在
  1:1 sidecar chain 表。旧 snapshot 保持 immutable 历史行且不伪造 operator registration；fresh 与升级库
  使用同一份 DDL，新注册 source 从 generation 1 建新链；
- snapshot 接受事务的 snapshot、head、ingest event、schema/metadata 与 descriptor withdrawal 原子提交；故障
  注入后可以重放同一 generation；同一代并发 loser 会持久化 equivocation event，不再提前变成无事件的
  `StaleCatalog`；
- Fable 5 的四轮增量复核先后复现并关闭 incomplete reset、fresh/migrated DDL 分叉、有限 expiry、并发无事件
  和首次 registration cutover 缝隙，最终裁决为 **Go**，仅批准 P0-4a Commit A；metadata 专项 19/19、
  Python 全量 280/280、broker 15/15、`compileall` 和 `git diff --check` 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel SHA-256 均为
  `d06474d8292edcca7efcefaa1c2ee5b4adaec023b941b16aa1e34a1235b4a178`；隔离安装可创建 11 张 external
  metadata authority 表，`foreign_key_check` 无违规，SQLite integrity 为 `ok`；
- 第二笔提交新增 disposable Connector Shadow projection。Projector 以 SQLite `mode=ro` 读取 Core/Catalog，
  投出 metadata source head/freshness/reject、profile/operation、physical attempt/retry、每个 attempt 最新
  Usage/Cost/Settlement、quota window、health/circuit 与 incident；固定 API 和静态快照页面同步接入；
- 投影不含 raw body、authority `record_json`、incident detail、credential、provider request/usage ref 或
  Core/Catalog 路径，也不参与 admission。完全缺少 P0-4/connector authority 表时向旧 baseline 返回 warning
  与空集合，部分表存在时 fail closed；watermark 覆盖新增 authority；
- Fable 5 增量复核关闭同 timestamp latest-event 排序和 partial schema 漏检后给出 **Go**；dashboard/
  dashboard-projector 20/20、service/static 7/7、Python 全量 284/284、broker 15/15 通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 Python 3.13 no-build-isolation wheel SHA-256 均为
  `655f4af42fa0db54524ad5512fc29d6eea4320c64777fe3014918622a7fe7910`；隔离安装可创建 projection schema
  0.2 的 5 张新 read-model 表，HTML 含两个新 API endpoint，SQLite integrity 为 `ok`；
- 这两笔提交仍未把 exporter 接到 OpenClaw live inventory，也没有真实网络、数据源访问、部署或研究
  WorkOrder。P0-4a Commit B 的最终测试、wheel、Fable 5 复核与提交信息见本轮独立报告。

### Connector P1-0 当前进度（complete inventory + recorded reference shadows，未部署）

- P1-0a 冻结十个独立 profile：CNINFO、SEC、AlphaEngine、X/xreach、X/x_search、Reddit/last30days
  keyless、Guidepoint、Gemini web search、public web fetch 和雪球；X 的枚举/语义搜索与 web 的搜索/抓取
  不合并；雪球只允许 `get_hot_stocks` 走带 `source_ref/adapter_ref/provenance_label` 的
  `cn-hk-findata xq_hot_rank` fallback；
- 每个 profile 都有闭合 operation/input/output/pagination/completeness/auth/transport contract、逐 operation
  synthetic fixture matrix 和 proposal-only manifest。十类当前均为 `inventory_connected`，不产生 lease、
  不请求 canary，也不代表 live connector 已接通；
- Inventory loader 最终把已验证 package graph 与 deterministic build 逐对象精确比较；authority ref、时间戳、
  fixture scenario/error/raw/auth 语义和 graph hash 任一漂移都 fail closed。P1-0a 提交 `976548e` 已获
  Fable 5 **Go**：专项 12/12、Python 296/296、archive wheel 安装和 31 个 packaged JSON 逐字节检查通过；
- P1-0b 新增 CNINFO `list_announcements` 与 SEC `list_filings` 的离线 recorded reference shadow。每页独立
  reservation、physical attempt、Usage、Cost、Settlement 和 raw ArtifactVersion；多页共用一个 logical
  invocation，并用 AdapterRequest 0.2 把 parent query、上一页 request/observation/attempt hashes 与 cursor
  绑定到下一页参数；bounded window、页数上限、revision chain 和 completeness 全部显式；
- Runtime fixture 必须与 packaged deterministic fixture 逐对象相同，并冻结 parent parameters/query hash；
  plan 与 AdapterRequest 显式绑定 selected scenario。normalized output 必须通过 inventory 的 closed schema，
  runtime profile 的 hosts/network/operation/schema/fixture/package graph 任一漂移都在 adapter 前拒绝；
- 成功/empty/partial 才生成 SourceEnvelope；schema drift、429、timeout、malformed 不生成 raw artifact 或
  SourceEnvelope。每个成功页在 page commit 内独立注册 ArtifactVersion；page recovery 覆盖 reserved、
  transport_started、observed、artifact/responded barrier 和第二页 capacity failure；
- response journal 先持久化 closed result/response/page receipts/commit context，再通过窄 AuthorityPort 做
  Scheduler reconciliation。Scheduler 从构造时绑定的 exact RunnerJournal 与同一 Core store 的
  ConnectorCompletionReceiptReader 读取全部事实，不接收 caller 声明的 event hash/time；只有 parent
  `recorded_at` 决定 lease 内完成。page observed/transport_started 不足以在过期后完成旧 attempt；lease 内已
  持久化的 parent completion 可在过期后收敛，later attempt 已重新 claim 时 fail closed；
- 父级 ResultEnvelope/RunnerResponse 由 deterministic builder 从 request/context、全部 page receipts 和
  Connector/Artifact/Source authority 唯一生成，并整份精确比较；额外 output/metadata/side effect、空 authority
  或分页串页即使重算 hash 也不能形成 formal result；
- Fable 5 对 `9599ea8` 至 `bf7c169` 的多轮复核发现并推动关闭了 fixture/runtime graph、page recovery、输出
  schema、query/scenario、lease proof、caller 时间、inner fact chain、分页串页和开放 parent completion 等
  问题。最终 committed-tree 审阅对 `2cb671e` 给出 connector 代码 **Go**：专项 21/21、组合超集 92/92、
  broker 15/15、敌对 completion probes、compileall、diff-check、clean install 和 SQLite integrity 全部通过；
- 独立审阅在 UTC 跨日后把全量基线重跑为 316/317，唯一失败是既有 Agenda `decide_cycle` 重新按真实时间
  查 active policy，而没有读取 cycle 冻结的 policy version。后续修复改为 exact frozen policy binding，并加
  active policy 中途换版回归；当前 Python 全量为 319/319；
- `2cb671e` 的两次 archive wheel 独立复现 SHA-256 均为
  `8775adbeaa6c901801e84dfe3652cdaa312912d65301cb13173c068edae23f58`。旧记录
  `ca0e6a9e...` 无法从任何现存提交复现，不能继续作为验收事实。工具链为 Python 3.13.14、setuptools
  84.0.0、pip 26.1.2、`SOURCE_DATE_EPOCH=1700000000`；Agenda 修复后的工作树 wheel 两次逐位一致，
  SHA-256 为 `0df85b85981fd14ebc095bf9ecd5ff86377d11a4ac8822ffc1166dea01bbbd04`；
- P1-0c 在十件 frozen inventory loader 之外新增声明式 proposal package loader。每个 package 只能包含
  `profile.json`、`fixture.json`、`proposal.json`，第 11 条及后续 proposal-only connector 不改中央
  `PROFILE_DEFINITIONS` 即可完成 offline graph/schema/fixture 验证。loader 递归闭合验证 operation schema，
  保留冻结十件的 slug/connector identity，固定 adapter version 与 transport/auth required gate，并拒绝 symlink、
  超大文件、重复 JSON key、非 synthetic fixture、执行权限升级、敏感配置和跨对象 graph 漂移；冻结十件 loader
  保持不变。Fable 5 对 `8b13e26` 给出无条件 **Go**；inventory 专项为 17/17、Python 全量为 322/322、
  broker 为 15/15，两次 committed archive wheel SHA-256 均为
  `23ce64bdfb5b74cad4344fac314da43d2b88f459f022aabdd7cc2e35df27a51b`；
- connector 语义选择不在每个 physical call 重做。Planner 在 WorkOrder/协调器边界一次选定 source、operation、
  completeness 和 fallback；Runner 每次调用只做确定性的 lease、quota、host/auth、schema 与 provenance gate。
  `CompiledConnectorPlan` 等到 P2 coordinator 有真实消费者时再加入，不提前建立独立 Router；
- 本轮没有部署、没有访问真实数据源、没有使用 credential，也没有写 Evidence/Claim/Thesis。真实 public
  network 仍被 killable total-deadline transport gate 阻塞；AlphaEngine/Guidepoint/雪球等 host/MCP 路径仍被
  runner wire 0.2、credential revoke/max_calls use-time authority 阻塞。

### Connector P1-0d 已完成（AlphaEngine offline `mcp_managed` shadow，未部署）

- 新增独立 `mcp_managed` RunnerRequest/AdapterRequest/TransportObservation wire 0.2；该路径没有 URL、host、
  public network policy 或可序列化 credential value，不能复用 public HTTPS transport；
- Credential authority 只保存 grant、revoke 和逐次 use receipt 的闭合 metadata。每次使用精确绑定 profile、
  capability lease、adapter、principal、credential slot、operation、reservation 和 physical attempt；在返回
  host-owned opaque handle 前再次检查 revoke、expiry 与 `max_calls`；
- AlphaEngine 当前只实现 `search_library` 的离线 recorded shadow。fixture 精确绑定 frozen inventory、parent query、
  selected scenario、input/output schema 和 transport target；success、empty、partial、pagination、schema drift、
  rate limit、timeout、malformed、permission denied、revoked 均不访问真实 MCP；
- 成功调用继续走既有 reservation → attempt → Usage → Cost → Settlement → ArtifactVersion → SourceEnvelope →
  Scheduler completion；失败不得伪造 raw artifact 或 SourceEnvelope。Runner-owned deadline 会中止超时 fixture 的
  sink；credential 在 reservation 后形成 use receipt，并在 adapter 前做最后一次 use-time 验证；
- connector 的语义路由没有进入每个 physical call。Planner/协调器一次选定 source、operation、parameters、
  completeness 与 fallback；Runner 的分页和重试只执行本地确定性 authority gate。一个稳定 source 可以暴露
  多个 operation，跨 source 的 findata 体验由上层 research recipe 组合；
- 新 connector 继续走声明式 proposal package。P1-0d 只增加 AlphaEngine 的运行时参考链，不把中央语义路由
  做成新服务，也不提前加入尚无消费者的 `CompiledConnectorPlan`；
- MCP/credential/Runner/transport/packaging 组合 41/41、Python 全量 341/341、broker 15/15、`compileall`、
  `git diff --check` 和 deterministic fixture regeneration 均通过。两次 committed-tree Python 3.13
  no-build-isolation wheel 逐位一致，SHA-256 为
  `1d18058a2f00ecee014da41c0c1dd4a360067df1b700c20febd5899272b1349f`；干净安装、packaged fixture、四张
  credential authority 表与 SQLite integrity 均通过；
- Claude Fable 5 对 `e48d76b` 的 committed tree 给出 scope-limited **Go**，没有 P0/P1。下一阶段选 P2
  coordinator foundation：先做 ContextPack、RunState、Checkpoint、ClaimIndex，再由首个 fixture-only consumer
  引入一次性 `CompiledConnectorPlan`；Guidepoint shadow 延后到真实 research recipe 需要时；
- 本轮没有读取 AlphaEngine token、没有调用本地 MCP、没有访问真实数据、没有部署，也没有写
  Evidence/Claim/Thesis。`get_document` 仍停留在 inventory；Guidepoint、雪球和 live MCP 仍为 No-Go。

### Connector P1-0e 开发候选（AlphaEngine live bridge，未部署）

- 新增 live MCP transport plan 0.1 和 AdapterRequest 0.3。两份闭合 contract 把 exact
  `CompiledConnectorPlan/step`、frozen AlphaEngine inventory/schema、operation、参数、bridge 和 transport target
  绑在一起；endpoint、token、cookie、server config 和任意 tool name 都不能进入可序列化对象；
- host-owned bridge 当前只允许带显式端口的 loopback `/mcp`，关闭 proxy 和 redirect，并限制 tool allowlist、
  deadline、response bytes、strict UTF-8/JSON/SSE 和 JSON-RPC request id。Core 只拿 opaque handle；
- `LiveMcpRunnerAdmissionGate` 在 quota 前后重检 Runner、Catalog、profile、resolver、call、invocation、lease、compiled
  plan 和 transport plan；每个 physical call 形成独立 credential use receipt。credential use 的幂等 hash 不再受
  authority 当前时钟影响，scalar/collection 不能冒充 opaque credential handle；
- `search_library` 固定使用 relevance，并把 frozen company/date/document type/geography/industry 参数映射到真实
  AlphaEngine schema；`get_document` 只接受 `alphaengine-doc:<doc_id>` 和数字 offset cursor。成功结果先写 exact raw
  JSON-RPC Artifact，再生成 hash-bound SourceEnvelope；不生成 Evidence、Claim 或 Thesis；
- fake source 端到端测试已覆盖 complete/partial normalization、exact raw artifact、credential/quota、compiled-plan
  tamper、duplicate replay 和 `after_observed` crash recovery。恢复只重放 authority 写入，不再次调用上游；
- 真实 AlphaEngine 只读 canary 已完成 `search_library → get_document`：搜索得到 10 条结果，文档调用得到首个
  30,000 字符分片、内容 SHA-256 和 `next_cursor=30000`。这次 canary 直接验证 bridge/adapter，没有接入 live Core
  authority，也没有证明完整文档；
- bounded multi-page coordinator 已新增 plan/page/manifest 三份闭合 contract。每页作为独立 physical call 记录
  calls/bytes，只有首页消耗 1 个 document unit；逐页回查 Invocation、Usage、Settlement、raw Artifact 和
  SourceEnvelope，触及限制只形成 partial，终页整文 hash/长度一致才形成 complete manifest；
- 尚未部署，没有 production Catalog/profile/grant/mapping，没有模型调用，没有 Evidence/Claim/Thesis mutation。
  production ResearchPlan/Scheduler 接线与真实完整文档 canary 仍须单独验收；下一开发切片是 Gemini web search
  discovery 和独立 web fetch。

### P2 coordinator foundation 当前进度（fixture-only，未部署）

- 新增闭合 `CompiledConnectorPlan`、`ContextPack`、`ClaimIndex`、`ConnectorCompletionReceipt`、
  `ResearchCheckpoint` 和 `ResearchRunState` 0.1 contract/schema；所有对象都拒绝未知字段并校验 canonical hash；
- Planner 在 WorkOrder 边界只生成一次三步计划。每个 RunnerRequest 精确绑定 plan/step ref/hash；Runner 的
  physical call 不重复语义路由，只继续执行既有本地 authority gate；
- ContextPack 只保存 ref/hash 和冻结后的 token/byte 选择账，不保存正文；ClaimIndex 只从已有 ClaimVersion、
  EvidenceRelation 和 ledger snapshot ref/hash 构建可重建搜索投影，不提供 Ledger 写 API；
- `FixtureResearchCoordinator` 只持 `ConnectorExecutionPort.execute`。参考 port 只读取 packaged CNINFO、SEC、
  AlphaEngine fixtures，不导入 network/MCP client，不接受 credential；coordinator 自身不持 Connector、Scheduler、
  Credential、Core 或 Research Ledger DB handle；
- owner-only scratch SQLite 只保存 immutable plan/context/index/checkpoint/run-state projection。checkpoint chain
  精确绑定 attempt、plan、context、step、连续 connector attempt 和 completion receipt；数据库可删除重建，
  不属于 Research Ledger 或 connector completion authority；
- fault injection 覆盖 execute 后、checkpoint 后和 state 后恢复。execute 后未写 checkpoint 时复用同一个
  idempotency key；checkpoint 已写后不重复调用。429/retryable 立即返回，不 busy wait；每 step 最多两次，
  耗尽后 run 终结为 failed；
- Fable 5 对首个 committed tree 给出 scope-limited **Go**。随后关闭 public runner 把调用方 plan binding 当
  authority、RunState 首 checkpoint 前可换绑、恢复不重验 prior checkpoint chain、ContextPack 不重算选择结果和
  fixture port 幂等键分叉等债务；
- Fable 最终增量复核仍为 **Go**，没有 P0；复核发现的 MCP 0.2 gate plan-binding 不对称也已关闭，两个 gate
  现在共用同一拒绝函数。敏感键分隔符归一化和 checkpoint authority/RunnerRequest/idempotency 恢复重验一并完成；
- 新增专项 14/14，相关 runner/MCP/coordinator 组合 38/38，Python 全量 357/357、broker 15/15、`compileall` 与
  `git diff --check` 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 两次 wheel 逐位一致，SHA-256 均为
  `0077ca167f0b7626910edb10aac719b11e7a08bbea3062f61be0d33eeb5cade6`，每份 507,712 bytes；干净 venv
  安装、`pip check`、三步 plan build、packaged SQL 和 SQLite integrity 均通过；GitHub CI `31869944201` 的
  Python 3.11、Python 3.13 和 broker 三个 job 全部通过；
- 本轮没有部署、没有访问 live source/MCP、没有读取 credential、没有写 Evidence/Claim/Thesis，也没有切换
  旧 cron。其后的 offline source/numeric verifier 与 candidate staging candidate 见下一节。

### P2 offline verifier + candidate staging 当前进度（fixture-only，未部署）

- 新增 closed `SourceVerificationMaterial`、`NumericVerificationSpec`、`VerificationBundle`、
  `CandidateEvidence` 和 `CandidateClaim` 0.1 schema。verification bundle 固定 verifier ref/hash；候选对象使用
  candidate-only identity，不能当作正式 Ledger version；claim 的叙述语义在人工 review 前固定为 `unverified`；
- source verifier 重新核对 plan/context/step/request/receipt/checkpoint/authority binding，并从 packaged fixture
  重算 raw payload、synthetic source summary、artifact、schema、record refs、lineage、completeness 和时间顺序；
- numeric input 必须用 exact material ref/hash + JSON Pointer 从 verified raw payload 重新抽取。数值使用 canonical
  Decimal string，只开放 `identity / sum / difference / ratio`，并核对 unit、currency、scale、period 和 rounding；
- `CandidateStagingStore` 使用独立 owner-only SQLite 和 append-only trigger，不导入 `DaltonStore`，也不创建
  Evidence/Claim/Thesis 表。staging 会重新执行两个 verifier 并要求结果 canonical equality，调用方自报 pass 无效；
- stage request 在 `BEGIN IMMEDIATE` 事务内保存 material/spec/verification/candidate/idempotency。事务内崩溃全部
  回滚；commit 后返回前崩溃用同一 idempotency key 返回 duplicate；同 key 不同 request fail closed；
- 专项 7/7，coordinator/verifier/packaging 组合 22/22，Python 全量、broker、build 和安装结果见本次实施报告；
- 当前 SourceEnvelope/Artifact 仍是 P2 synthetic summary ref/hash，不是 live connector authority record。真实只读
  WorkOrder 前必须增加 authority resolver 并复核完整 checkpoint chain；本阶段不部署、不接 Agenda、不读取凭据、
  不写正式 Research Ledger。

### P2 authority resolver + SEC public canary 当前进度（隔离验收，未部署）

- 新增 closed `AuthorityResolution`、`AuthoritySourceVerificationMaterial` 0.2 和
  `ConnectorCompletionReceipt` 0.2。旧 0.1 receipt 保持兼容，但真实成功链必须同时绑定 coordinator 的
  plan request 与 Connector Runner 实际执行 request；
- `ConnectorAuthorityResolver` 只读连接 Core、Connector、Observability、Scheduler、RunnerJournal 和
  coordinator scratch。它重算 raw Artifact、SourceEnvelope、Profile/CallSpec/Execution/WorkOrder、
  ResultEnvelope/formal Scheduler event、physical attempt、Reservation、最新 Usage/Cost/Settlement、
  AdapterRequest/observation/response 和完整 checkpoint chain；任何换绑、缺失、部分结果或篡改都 fail closed；
- SEC adapter 只允许 `https://data.sec.gov/submissions/CIK{cik}.json`，不接受 credential handle，沿用
  public transport 的 DNS/IP pinning、redirect 和 response-size gate。normalizer 严格拒绝重复 accession、
  非法日期、不明 amendment revision、超窗口和静默 limit 截断；
- isolated canary 使用临时 SQLite/raw spool 和 synthetic canary approval，不打开 live DB。真实 Microsoft
  `CIK0000789019` 2025 10-Q 请求返回 3 条 filing，最终进入独立 candidate staging；状态为
  `human-review-ready-candidate`，`semantic_verification_status=unverified`；
- authority/SEC/Agenda 专项 8/8、相关 connector/coordinator/verifier 回归 41/41、Python 全量 370/370、
  broker 15/15、`compileall`、schema 解析和 `git diff --check` 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 的两次 Python 3.13 wheel SHA-256 均为
  `d85ad4ecb466a18f3447549a3765f6561eba025a6b8bbed33baee3469dec22ae`，557,781 bytes；干净安装、
  `pip check`、新增模块、14 份 packaged SQL 和 88 份 packaged contract schema 检查通过；实现提交
  `002ebda`，GitHub CI `31878953063` 的 Python 3.11、Python 3.13 和 broker 三个 job 全部通过；
- 本轮不部署、不接 Agenda、不读凭据、不写 Evidence/Claim/Thesis，也不切换旧 cron。人工 review authority/
  入口、正式 commit 和生产 connector promotion 仍是独立 gate。

## 蓝图阶段

### Phase 0：记录和可观察性——主体完成

已完成：

- 94 份 JSON Schema、16 份 SQL schema；
- immutable DomainEvent、WorkOrder、ResultEnvelope、ModelInvocation；
- Evidence → Claim → Thesis 版本链、verification 和 commit gate；
- Workflow、Artifact metadata、模型 Usage/Cost、只读 projection 和静态看板；
- legacy workspace/database/cron 的 shadow import；
- owner-only writer、每日 SQLite backup、已完成的 restore 演练与数据库完整性检查。

部分完成：connector authority foundation 已有 16 张 append-only 表、trusted store、wire contract 和只读
Connector Shadow projection，
并通过六轮独立复核；Runner 控制面、recorded adapter execution、durable journal/raw spool、W0–W4 recovery
和窄 AuthorityPort 已完成 P0-2b；metadata importer、credential-free SSRF-safe public transport 和
credential authority metadata boundary 已完成 P0-3 离线切片。真实 connector 调用、authenticated runner、
writer RPC、完整 source-health ledger、生产对象存储生命周期和跨机灾难恢复仍未完成。

### Phase 1：Agenda Engine Shadow——单公司运行中

已完成：

- Mandate、PriorityOverride、ResearchQuestion、AgendaCycle、AgendaDecision；
- PerceptionSnapshot legacy adapter；
- Scheduler → Model Router → OpenClaw broker → provider usage/cost → 确定性选题；
- append-only outbox、marker reconciliation、receipt、补投和 stale-attempt 拒绝；
- Tailscale HTML agree/disagree、24 小时 timeout 默认接受和独立统计；
- 全局 pause、一次性 human governance CLI、Agenda 监督 projection。

当前 live 范围只有万华。Agenda 只选题，不执行研究，不写 Evidence、Claim 或 Thesis。旧
`dalton-coverage-*` 10 条 cron 继续运行。Connector Shadow 可以并行建设，但其输出不得接入当前
Agenda Perception；否则会在 10 日评估窗口中途改变输入分布，污染现有 shadow 指标。

### Phase 2：低风险自主闭环——部分底座完成，研究闭环未接通

已完成：Scheduler lease/retry/idempotency、ProcessRuntimeAdapter、六模型 exact route、OpenClaw 模型
broker、预算和 pause gate。

部分完成：connector runner、recorded transport、fixture-only coordinator、offline/authority source-numeric
verifier、只读 authority resolver、candidate-only staging、HumanReviewAuthority 和无损 Ledger promotion 0.2
已完成；真实 SEC public source 与 review/commit 只在隔离测试运行，尚未接 Agenda 或生产 authority。
未完成：原生事件 connector、从 AgendaDecision 到 research DAG 的 production planner、
`ready → connector/worker → verifier → revise/commit` 完整 coordinator，以及生产化只读研究 WorkOrder。

### Phase 3：Verifier 与 Thesis Commit——权威机制完成，运行层未开始

已完成：独立性 predicate、VerificationRecord、Evidence/Claim/Thesis gate、adjudication、不可变
版本、原子 commit、幂等和事务失败回滚约束。这里没有 thesis 业务版本回滚；当前可激活历史版本的
rollback 只存在于 Capability Registry。

部分完成：source/numeric verifier 已有 synthetic fixture replay、真实 Connector authority replay、
换绑/数值错误探针，以及 explicit human review 后的 Evidence/Claim 0.2 原子 commit。
未完成：生产 authority verifier、completeness/investment-link verifier、seeded-error 校准、局部返工和任何
live thesis commit；review 入口仍未部署。

### Phase 4：能力自主改进——治理半边完成

已完成：CapabilityProposal、Evaluation、Decision、Registry、Catalog、lease、attestation contract、
human promotion 和 rollback；builder 不能自评或自批。

已完成：Connector Protocol 0.1 文档、闭合 `ConnectorProposalManifest` schema 和 executable validator。

未完成：重复任务/gap detector、代码生成器、真实 sandbox service、历史回放 runner、可信 canary、
上线监控和自动 rollback trigger。当前 attestation 验证器不执行代码，
且明确禁止网络、凭据和 Core DB。

### Phase 5：多 runtime 与规模化——替换边界完成

已完成：Dalton-native process runtime、Pi/DeepSeek Harness spike、OpenClaw model/bridge adapter seam。

未完成：production worker manager、多 runtime coordinator、独立 OS/container identity、跨机服务身份、
Postgres/Temporal 规模化门槛和迁移。

### 仍未完成的横切蓝图

- 完整 coverage requirement/mandate policy 与自然语言 steering；development Cockpit 已合并 Agenda、研究审阅、
  ACN 只读 trajectory 和自然语言 composer，typed effect 已有同源 human 二次确认、exact context revalidation 与
  原 writer dispatch；live HTML 仍只处理 Agenda feedback。全局 agenda pause、通用
  cancel/approve/emergency-stop command/event bridge 均未完成；
- native event inbox，以及 expiry、catalyst、falsifier、source failure 触发；Agenda portfolio pools 和
  跨公司容量校准未完成；
- production planner DAG、stop/cancel 和 worker manager；fixture coordinator 已有 checkpoint/resume，尚未接
  Scheduler/Agenda/live connector；
- P2 已有 typed ContextPack、per-attempt RunState/Checkpoint 和结构化 ClaimIndex；尚缺版本化 retention policy
  与 authority DB 之外的滚动 OpsTelemetry；session transcript 和 compaction summary 不作为研究 memory；
- operational verifier 已有 fixture 与隔离 SEC authority source/numeric thin slice；尚缺生产 authority reader、
  completeness/investment-link verifier、revise/replanning/reflection 和 seeded-error 校准；
- first-class falsifier/catalyst/driver/model/valuation authority、Model IR、Tier 1/2/3 evaluator 和
  Excel exporter；
- generic research review/delivery outbox；现有 outbox 只服务 Agenda Discord 通知；incident ledger、
  production object lifecycle 和 offsite disaster recovery；
- hostile-code OS/container identity 与 sandbox，以及 production multi-runtime/scale。

首个 live thesis commit 前的 confidence contract debt 已由 ADR-0001 裁决：新 `ThesisVersion` v0.2 只接受
`low / medium / high`，旧 v0.1 float 只读兼容。US IT Services / ACN 初始 coverage thesis 只允许 human admission；
candidate 建立后，旧 model-verification commit 不能创建或修改同一 thesis。

## 已上线的运行面

- `space.lumos.dalton.writer`：独占 Core authority DB；
- `space.lumos.dalton.controller`：lease sweep、Agenda、projection、backup、outbox 和 health；
- `space.lumos.dalton.control`：Tailscale 内的 Agenda HTML 控制面；
- OpenClaw model broker：复用 host-owned model authentication，Core 不读取凭据；
- 公开只读看板：<https://eve.lumos.space/dalton/>；
- 私有 Agenda 控制面：`https://everflowdemac-mini.taild2c767.ts.net:8793/`。

已知限制：Mac mini 本机的 Tailscale CLI 与 daemon 版本不一致，本机验收使用显式地址映射完成；
尚未从第二台 tailnet 设备实测私有控制页。

已部署验证基线：Python 195/195，OpenClaw broker 15/15，Python 3.11/3.13 wheel build 与 GitHub CI
通过；Core、Scheduler、Model Router SQLite integrity 均为 `ok`。当前工作树的专项测试、构建和 CI
状态不能与 live 基线混写。US IT Services / ACN 开发候选已完成 Python 658/658 全量回归和 Python 3.13
sdist/wheel 构建；尚未提交远端 CI，也尚未部署。

## Connector Fabric Shadow

### 边界

现有 OpenClaw skill/MCP 不整体迁入 Dalton。每项能力拆为：

1. connector：取数、分页、限流、原始响应留痕、结构化返回；
2. normalizer：转成带 provenance 的 typed record；
3. research recipe：决定查什么、如何交叉验证；
4. delivery：继续走 durable outbox。

CapabilityCatalog 已支持 `kind=connector`、`skill/mcp/tool/plugin` 来源、typed call/process、权限、
credential slot、lease 和 human approval。下一阶段复用这些边界，不另建第二套能力注册表。

### 必须补齐的契约与权威状态

- `ExecutionInvocation` 通用超类型，以及 Model/Connector 1:1 子类型；历史 ModelInvocation 采用
  additive backfill，新调用由 writer 在一个事务中同时登记通用与模型行；
- immutable `ConnectorCallSpec`：source-specific 参数和 schema hash 由 WorkOrder `input_refs` 引用；
  connector RPC frame 必须绑定 admitted WorkOrder、WorkOrder hash、CapabilityLease、lease hash、
  ConnectorCallSpec/hash、descriptor revision 和 idempotency key；
- transport-only `ConnectorRunnerResponse`：内含 ConnectorInvocation、`ResultEnvelope`、
  `SourceEnvelope` refs 和 quota settlement；`ResultEnvelope` 仍是唯一执行结果权威，adapter 强制
  work/invocation/artifact/usage/status canonical 一致；
- `ConnectorProfileVersion`：exact source/adapter version、allowed operations/hosts、auth mode、
  credential slots、input/output schema refs、pagination/completeness、max response，以及
  access/retention/terms refs；profile 还冻结 redirect、DNS/IP 和 private-network policy；
  CapabilityDescriptor 只负责发现和治理摘要，不承载全部 source runtime 参数；
- `SourceEnvelope`：source、operation、source record/document ref、published/updated/as_of/retrieved
  四类时间、cursor、provider request id、raw response/artifact hash、schema/content hash、
  completeness（enumerated/ranked/partial/unknown）、access/retention/terms policy ref 和 error；
- `ConnectorUsageEntry`：physical calls/pages/records/bytes/duration/billable quantities、metering source
  和 correction chain；
- `ConnectorPriceRateVersion`：meter、unit quantity/price、effective interval、currency 和 source；
- `ConnectorCostEntry`：绑定 exact usage 与 exact rate，保留 actual/estimated/unpriced/waived 和
  correction chain；
- `ConnectorRatePolicyVersion`：quota scope（connector/operation/credential slot/provider shared）、
  burst、并发、window/reset timezone、calls/bytes/records/cost、billable unit 和 Retry-After；执行 timeout
  由 ConnectorProfile/RunnerEnvironment 冻结，不属于 quota policy；
  RatePolicy 只做 admission/quota/retry，可以引用 rate card，不能兼任费率或网络权限权威；
- durable quota reservation/settlement：logical invocation 与 physical provider attempt 分开记；每次
  retry、429、timeout 前都先预占 physical call，结果不确定时保守占额；
- query hash/cursor/time-window 去重、429 reschedule，以及由 append-only event 投影得到的
  source-health/circuit state。外部调用不承诺 exactly-once。

现有 `UsageEntry` 是模型专用契约，强制记录 model/profile/token 字段。它继续作为模型子类型的
用量账；connector 新建独立 Usage/Cost authority，不急于合并成万能 usage 表。现有 ArtifactVersion
的 producer 也通过外键强绑 `ModelInvocation`；P0 发布 ArtifactVersion v0.2，改为引用
`producer_execution_ref`。connector 不能通过伪造 ModelInvocation 取得 raw artifact provenance。

### 通用 connector 路线图

所有 connector 面向数据源和 operation，不面向单家公司：

- **A 股公告**：巨潮/CNINFO 公告检索、下载、正文读取和修订链；万华只是首个 shadow fixture；
- **SEC**：edgartools/findata-analyst 的 filing list、official attachment、item/text/facts；
- **X/xreach**：固定账号完整枚举、单帖和 thread，completeness 可声明 enumerated；
- **X/x_search**：主题发现和媒体理解，输出只能声明 ranked/partial，不能与 xreach 共用 descriptor；
- **Reddit public fetch**：只抽取 last30days 当前可用的公开 Reddit adapter，不把多源 last30days
  聚合器当作 connector identity，也不复用本机已知 403 的 Reddit JSON curl；
- **AlphaEngine**：`search_library → get_document`，凭据只由本地 MCP/credential slot 持有；
- **Guidepoint**：`search_library → transcript`，保留文档 ID、原始 attribution 和许可边界；
- **公开 web search**：Gemini grounded search，只负责 ranked discovery；
- **公开 web fetch**：单独的 last-mile fetch connector，必须拒绝 private IP、非公开 URL 和
  redirect 逃逸，不能与 search 共用权限；
- 后续：港交所公告、A/H/美股行情与财务、雪球、公司 wiki 和其他内部文档源。

聚合 skill（例如 last30days、公司深研）只作为 research recipe 或多个 source connector 的编排层，
不能成为不透明的单一 connector。

### Dalton 自建 connector

第一阶段允许 Dalton 自动发现重复任务或能力缺口、生成现有 `CapabilityProposal(kind=connector)`、
代码、schema、fixtures 和修订版本，但不允许自行激活生产 connector。proposal 的开放
`contract/permissions` object 只能引用闭合、带 hash 的 connector manifest/profile，不能在开放 object
中藏 source runtime 权限：

`gap → proposal → protocol template → offline replay/eval → human canary authorization → trusted canary → canary eval → human production promotion → Catalog`

offline sandbox 不给网络、凭据或 Core DB。在独立 OS/container identity 落地前，自生成代码只能做
offline replay；networked canary 只能运行 operator-reviewed immutable adapter，或者进入独立身份/
container。可信 Connector Runner 不得动态 import 或执行 proposal code。真实 canary 只获得固定
host/operation、短期 credential slot 和独立 quota。运行时还需 exact version/source hash 的 adapter
resolver；MCP tool name、operation、schema epoch 和 skill entrypoint/hash 任一变化，都必须让旧 lease
失效。当前 CapabilityAttestation 明确禁止网络和凭据，真实 canary 必须使用 v0.2 或独立的 closed
canary attestation，不能冒充 offline attestation。未来若要让低风险 connector 自动晋级，必须新增
明确治理 policy，不能绕过当前 human promotion gate。

## 下一阶段顺序

### 吞吐：抓取快了 12 倍，瓶颈换了地方（2026-09-08）

**抓取原来不慢，是在等。** 实测：子进程干完活用 0.1 秒，然后工单一直躺到下一个 300 秒的 tick
才被发现完成——工单存活时间中位数 302 秒、p90 306 秒，一个 tick 一份文档，一小时十二份。
130 份的队列要跑十一个小时，而真正抓取的时间加起来大约十三秒。现在同一个 tick 里等子进程结束、
结算、接着取下一份，上限由每 tick 条数和每来源日配额兜住。live 验证：一个 tick launched 12、settled 12。

**cockpit 满屏"正在读取 SEC 财务数据"**：车道只在有人问起某一张具体工单时才结算它，
所以重启杀掉的 35 个子进程在磁盘上"running"了十三个小时。现在工单只有在进程还活着时才算 running，
改在读取侧，所有车道一起覆盖。

**免费配额上调**（owner 决定）：web-fetch 200 → 1000，SEC 索引 50 → 200，都是免费公开 HTTPS；
web-fetch 的额度现在**按站点分**，所以这个数字是对单个站点的礼貌上限，而不是各来源互抢的总预算。
失败重试从一天改成一小时——多数失败是暂时的或是我们自己的 bug，修好了却要等到明天才重试没有道理。

**现在的瓶颈在 AlphaEngine**：123 份文档排队，mission 的 `max_alphaengine_calls_24h` 是 30，
按这个速度要四天。这是**计费来源**，不是免费额度，要不要提是 owner 的预算决定。

### 年报（10-K）通道：已建好的部分与剩下的一片（2026-09-08）

owner 已签批 `sec-filings-index-v1`（`capability:dalton:connector:sec-filings-index`）。已经就位的：

- **枚举**：`SecPublicHttpAdapter` 早在 P0 就实现了 live `list_filings`（`data.sec.gov/submissions/CIK*.json`）。
  P10g 放宽了 window 远端的判断后，"截止今天的全部 10-K"才是个能问出口的问题（近端仍然拒绝，
  因为比 `recent` 更早的 filing 在取不到的 `files` 分页上）。live 验证：ACN FY2025/FY2024 两份 10-K。
- **定位**：P10h `build_filing_url_authorities` 从原始字节里重建 10-K 的 URL——冻结的 `list_filings`
  output schema 只带 record ref 和 hash，**故意不带** `primaryDocument`，而那正是唯一能指出文件在哪的字段。
  ref 用 `public_web_url_ref` 铸造，形状和 web fetch lane 已经认识的一致，所以 10-K 直接并入现有
  fetch→抽取流水线，不需要自己的一条。live 验证：顺着这个 URL 取回 ACN 年报正文 2.8 MB。
- **凭据**：P10l `filings_index_descriptor_spec`。**这一条是排查出来的坑**：`sec_descriptor_spec` 把整个 SEC
  connector 发布在 `capability:dalton:connector:sec-edgar` 一个 id 下、schema hash 覆盖全部已批操作，
  而签批记录认的是自己的 capability、只绑 `list_filings`——id 和 schema hash **两个都对不上**，
  owner 签下的授权当时没有任何地方能接。现在并列发布一个更窄的 descriptor，四项（capability id /
  schema hash / source hash / policy ref）与签批记录逐一核对通过。

**剩下的一片：`SecFilingsIndexCore`**——一个受治理的调用引擎，照 `PublicWebCoreFetch` /
`PublicWebCoreSearch` 的样子写（profile / price / rate policy / runner manifest → WorkOrder →
compiled plan → `StaticAdapterResolver` + `ConnectorRunnerAdmissionGate` + `ConnectorTransportExecutor`
→ receipt），然后把 receipt 交给 `record_source_discovery`。

**为什么不能直接走 P0 的 ResearchPlan 那条路**（这点别再重新推导一遍）：`create_plan` 不带
`company_facts_request` 确实生成 `list_filings` 计划，执行器也确实会持久化 invocation/envelope/原始件；
但 `research_plan.py` 里 `requested_capabilities` 硬绑 `SEC_CAPABILITY`（sec-edgar），
执行器也按 `sec_connector_identity()` 解析 descriptor——两处都是共享 capability。
要让那条路承载这份窄授权，就得改冻结的计划形状，**计划 hash 会动**，而线上 company-facts 车道正绑在上面。
所以新引擎是为了不动生产授权，不是为了好看。

**顺带记一个还没解决的不对称**：窄的是 filings-index 这一份；`sec-company-facts-v2` 仍然绑的是覆盖
两个操作的共享 schema hash，也就是说**持有 company-facts 批准在技术上仍然涵盖 `list_filings`**。
P10e 说的"互不扩权"目前只成立了一半。收窄它需要给 company-facts 换版签批，是 owner 的事，先记在这里。

**接上去之后**才动 `SOURCE_BASE_ITEMS` 里 `annual_report` 的 `spec_refs`（现在是空的，所以恒为
`not_planned`）和对应的 discovery plan。顺序是故意的：引擎没通之前就把清单项改成 `missing`，
只会让 cockpit 显示一个没人服务的 0/1，看着像进展其实不是。

### thesis-impact 定时任务已按 owner 决定停泊（2026-09-08）

**没有废弃，是先停下来。** `space.lumos.dalton.thesis-impact` 评估的是"新证据对 ACN thesis 的影响"，
它真干过活（成功 1641 次，ACN 第一条真实链就是它闭的）。但它从 2026-09-02 起一直挂着：
先是 policy-3 → policy-4 换版把 `eligible_assessment` 永久拒掉（1228 次 exit 2）；
[停泊修复](reports/thesis-impact-policy-rollover-park-v0.1-2026-09-06.md)上线后确实生效了
（261 次 `blocked_pending_human`）；随后 ACN 新的 research plan 进来，assessment 连续失败，
再之后每 5 分钟一次 `conflict: request conflicts with existing immutable data`（121 次，根因未查）。

thesis 层（`deep_insight_gate` / `thesis_admission`）在 Phase 10 里本来就是靠后的切片，
当前重点还是 Initial Screen 的资料底座，所以 owner 决定先停、到 thesis 切片再一次性修好并重开。

**停的方式**：`service.json` 的 `thesis_impact.enabled` 改成 `false`。
`ServiceConfig` 在 `enabled=false` 时把 `thesis_impact` 解析成 `None`，
LaunchAgent 渲染器于是不写 plist 并删掉旧的，`install.sh` 的 `[[ -f "$plist" ]]` 自然跳过——
**重装不会把它带回来**。`config` 整块原样保留，重开只需把 `enabled` 改回 `true` 再跑一次安装。

**没有动到别的**：`thesis-impact-budget.sqlite` 是抽取车道的日预算账本，和这个定时任务同名但不同用途。
当天 332 次预算 admission 全部来自 `document-extraction`，这个任务一次都没有——停它不影响抽取。

### 当前基线：Phase 10（v1.1，2026-09-07）

按 [v1.1](reports/vision-and-next-phase-v1.1-2026-09-07.md) 的顺序执行：~~P10a 阶段账本启动与资料底座清单~~（已完成）→
~~P10b Claim 挑战/退役~~（已完成，mission v8 已发布，53 条已退役）→ ~~P10c 交付物 authority 与 Initial Screen 自动起草过门~~（已完成，等 owner 发布 mission v9）→
~~SEC 季度数字补齐~~（已完成，见下）+ SEC 10-K 正文获取通道（governance 已提案，**等 owner 签批**）（出口门第一问的前提）→
P10d Deep Insight Gate 人审 → P10e 行业框架/行业模型缺口 → P10f 公司模型与预测线 → P10g Investment Memo。新来源、通用能力、cockpit 新视图继续冻结。

**SEC 季度数字补齐（提交名 P10d，2026-09-08 完成）**：SEC 只给"最新一份报某期间的 filing"分配 calendar frame，
历史 filing 的 frame 会被后来者顶掉，于是旧季度取不出数。改为不依赖 frame 直接按期间读取后，定量 Claim 从 5 条涨到 14 条，
五家公司各自排入 3–23 份 filing（ACN 23 份），抽取队列按每 tick 4 个窗口消化中。

**SEC 10-K 正文获取通道（提交名 P10e，2026-09-08）**：年报是五家公司唯一全部 `not_planned` 的来源。
AlphaEngine 只有电话会与研报，company-facts 只有数字没有文档，10-K 正文的 URL 只有 SEC submissions index 报得出，
所以先要把 `list_filings` 治理成一个独立 capability。`sec_connector_identity` 按整个已批准操作集算 schema hash，
传 `list_filings` 拿回的仍是 company-facts 的哈希，新记录一出生就是旧记录的副本；照 P9d-1 拆 AlphaEngine `search_library`
的先例把 filings-index 的 schema hash 收敛到自己这一个操作，并且**不动** `sec_connector_identity`——线上已批的
`sec-company-facts-v2` 就绑在那个哈希上。source hash 仍共享（来源确实是同一个 SEC），schema hash 不共享，
两份批准互不扩权。记录以 `proposed` 生成，只有 owner 能签。
下面 P0–P2 与 Phase 7/8/9 的文字是历史顺序，保留作依据，不再是当前基线。

### P0：Connector Protocol 与计量边界

0. 已完成 P0-0：seam 敌对测试、生产数据库副本 startup backfill 演练、复核出处修正、Artifact v0.2
   projection 和 Scheduler attempt event wire/hash epoch；
1. 已完成：`ExecutionInvocation` 超类型、Model/Connector 子类型与 ArtifactVersion v0.2；采用新增表、
   回填 link 和新写入原子双写，不重写历史 model/artifact hash；
2. 已完成 authority contract：ConnectorCallSpec、ConnectorProfileVersion、Runner frames、SourceEnvelope、
   usage、physical attempt 和 rate-policy schema；
3. 已完成 P0-1：trusted store、quota reservation/settlement、幂等、append-only source-health event 和
   最小 `ConnectorIncident` authority（quota drift、schema drift、credential/auth、source outage）；
4. 已完成 P0-2a/P0-2b：Connector Runner 控制面、CapabilityLease use-time gate、exact static adapter
   resolver、authority-derived AdapterRequest、journal/spool/AuthorityPort/recorded transport；
5. 已完成 P0-3 importer thin slice：OpenClaw skill/MCP 只导入 metadata/schema/ref/hash，不导入 prompt、
   凭据或整份 skill；complete scope 内的 MCP/skill 漂移会撤下旧 descriptor 并推动 catalog epoch；
6. 已完成 P0-4a 两笔提交：trusted exporter state、source registration、单调 generation/prior chain、ingest
   event，以及 connector logical/physical usage、quota、health、incident 和 metadata source 的派生只读
   看板。OpenClaw live inventory attach 仍未开放；
7. 已完成 credential-free public HTTPS transport component 的 DNS/IP/pinned socket/TLS/redirect/size
   复核；待接 web fetch adapter/Runner 后才算真实链路；
8. 已冻结 public transport 与 credential authority metadata/API 分界；offline attestation 与 networked
   canary attestation、两次 human gate 仍待完成。同 UID runner 不执行自生成代码，独立身份/container
   上线前只允许 operator-reviewed adapter 进行 canary。

### P1：参考 connector 与 shadow

先实现 A 股公告、SEC、AlphaEngine，再实现 X、Reddit、Guidepoint、web search 和 web fetch。顺序按协议覆盖面，
不是按长期重要性排序：前 3 条分别覆盖公开文件、官方 filing 和 authenticated MCP。每条 connector
都先 shadow，对照现有 skill/MCP 输出，不写 Research Ledger。

### P2：第一条只读研究闭环

offline/authority source-numeric verifier、只读 authority resolver、candidate staging、一条隔离 SEC public
WorkOrder、独立 HumanReviewAuthority、HTML 入口、正式 Evidence/Claim 0.2 promotion 与 ResearchPlan closure
已完成开发候选。
ClaimIndex status 派生、DocumentIndex FTS5、claim/artifact ContextPack materializer、Agenda context authority、
ResearchQuestionBacklog、Planner SEC public 薄闭环、下游逐项 coordinator admission 和真实四步 executor 均已完成
开发候选；隔离 authority 中的一份人批 SEC public 四步任务树已经跑通。Owner 已接受 exact candidate，正式
Evidence/Claim 0.2 promotion 与 Backlog answer binding 已完成；closure coordinator 对全链重验并支持崩溃重放。
真实 policy-authorized 隔离 canary 已完成：closed SEC public plan 自动授权、执行、验证、promotion 并回答原
question，越界 statement 也已在专项测试中 fail closed。Apple、NVIDIA、Walmart 的多样本运行已经用于修复
company-facts filing window 和 latest-accession-bound concept 选择，但结果未形成可独立 replay 的提交证据；
当前已增加第一版
Claim → driver/thesis impact authority，以及 ResearchPlan closure → bounded assessment/verifier WorkOrder 接线。
两个 WorkOrder 现已接入 ModelRouter/OpenClaw model worker，并以无外部调用 recorded broker 验证 contract retry、
usage/cost 入账、model-family independence、lease-expiry crash recovery 和 replay。Gate 0/1 breadth proof 与
Gate 2 真实模型 canary、30-case corpus、候选校准和 wrapper-owned output contract 均已完成。旧的“模型回抄
assessment ref/hash”已从 semantic decision 中移除；trusted worker 从 immutable WorkOrder 绑定 target，仍保留
provider strict Schema、输入/输出/总 token、费用硬控制、raw ResultEnvelope 和历史 replay。Gemini 3.7 Flash low
与 Luna low 在最新 30-case direct calibration 中均为 30/30，Owner 已选择 Gemini 作为主候选；Qwen 和 Ox-alpha
仍有 high miss。仓库内的三项 production conformance 缺口已关闭两项：phase-pinned immutable verifier policy
（`dalton-openclaw-verifier:1`，只允许 exact `profile:gemini-3-7-flash`，未 pin 的 policy fail closed）与
thinking-level 控制合同（WorkOrder/manifest 冻结 `low`，进入 broker request hash、invocation 身份与 host
proof；broker 0.1.0-spike.5）。host 配置和 patch 已经打通；首次 3×30 的 90 次 fresh 调用保留为质量证据，但
旧 runner 复用了 run identity，production gate 已撤销；修正 runner 后续用三个不同 identity 重跑 3×30 并正式
通过。phase-pinned isolated shadow 也通过，production runner 现已部署到 live，但在没有 company→thesis mapping
时保持 idle。ThesisVersion 自动 mutation、旧 cron cutover、Interrupt / park / resume 和 Reflection 仍后置并保持
独立人工 gate。直接解除真实质量缺口或按明确标准改善下一轮产物的 connector/model
增量可以推进；与真实消费者无关的扩建后置。当前没有 live staging/review/plan authority。

operational verifier 与 fixture-only research coordinator 只继续第一条真实闭环需要的部分。formula census/
Model IR ADR 和 offline capability sandbox 只有在首条 plan 明确需要且有验收标准时才恢复，否则等真实闭环与
首轮质量数据完成后再决定。

2026-08-23 owner 明确要求在行业研究前先补建模地基，因此已实现范围受限的 Model Input Ledger v1。它只保存
human-gated actual/scenario/assumption/forecast version、冻结的 model run 和 reconciliation，不实现任意单元格、
VBA、循环引用、通用估值引擎或自动 thesis mutation。研究 worker 只能写 candidate；正式 input 仍由认证人类
准入。Valuation output 在 price/shares/FX/rates/consensus 五类正式 authority 齐备前 fail closed。这里不等于
恢复完整 Model IR 扩建，formula census 和 Tier 1/2/3 evaluator 仍按后置门槛处理。

## 继续建设与开闸的不同门槛

可以立即继续：完成最新 HEAD 的独立 CI；把无网络、无付费调用的完整 replay canary 接入 CI；修复会静默生成
空 evidence block 的 review harness；用同一 revenue-growth plan 复现 5 家 SEC issuer，并生成第一份 verified
brief。真实运行暴露的 verifier/connector 缺口可以修，但必须进入同一个 brief 验收，不能顺手扩建平台。

继续暂停：与真实质量缺口无关的新 connector 品类、Interrupt / Reflection、无明确质量验收的 Model IR、
sandbox、embedding、多 runtime，以及没有真实消费者的 contract、projection 或 dashboard 扩建。

仍需观察或人工批准：扩大 Agenda 公司数、生产 connector 权限、重大或非规则化 Evidence/Claim/Thesis
commit、外发、付费调用、凭据扩权、旧 cron cutover。低风险确定性 Claim 可在 owner 激活的 versioned policy
内自动提交。第一条闭环开发和 Agenda Shadow 数据积累可以并行，其他架构扩建按
v0.7 的价值门槛后置。

## Connector Fabric 完成门槛

### E1：authority 与真实链路

- 旧 ModelInvocation 回填通用 execution link，新 model 写入原子双写且现有模型路径无漂移；
- 至少一条公开 connector 真实跑通 WorkOrder → lease → runner → SourceEnvelope/Artifact →
  ResultEnvelope → Usage/Cost；
- 429 进入 Scheduler retry time，不 busy wait，每次 physical provider attempt 都计量；
- 并发 quota 不超卖；reserve 后本地崩溃可释放，上游是否已调用不确定时标 indeterminate 并保守扣额；
- raw artifact 写入后崩溃可按 content hash reconciliation，不重复产生事实；
- stale lease/source/schema/policy，以及越权 host/operation/credential 全部 fail closed；
- connector shadow 不写 Evidence、Claim、Thesis，也不接入现有 Agenda Perception。

### E2：契约与敌对测试

- closed schema、未知字段拒绝、ExecutionInvocation subtype/equality；
- RunnerResponse 与 ResultEnvelope 的 work/invocation/artifact/usage/status canonical equality；
- SourceEnvelope 四类时间、completeness、access/retention/terms 和 provider request identity；
- SourceEnvelope 的 result attempt、structured content hash 和 raw artifact producer/hash equality；
- logical request 与 physical attempts、quota window/reset/timezone/rounding；
- cursor/page/partial/schema drift、error taxonomy、Retry-After；
- DNS/IP/redirect SSRF、source/adapter/MCP schema hash 变化使旧 lease 失效；
- offline attestation 不能冒充 networked canary evidence。
- trusted runner 对 proposal code 的 dynamic import/exec 必须被源码和运行测试阻止；自生成 adapter 的
  networked canary 必须使用独立 OS/container identity。

### P1：每条 connector 的 shadow gate

- closed profile 和 operation schema；recorded fixtures 覆盖正常、空结果、分页、partial、schema drift
  和 429；
- 每个结果都有 raw artifact、SourceEnvelope 和 exact physical usage；
- enumerated source 在 bounded window 内对 document IDs/revision chain 与旧路径对账；ranked source 不得
  冒充完整枚举；
- authenticated connector 完成 credential revoke 和 permission failure 演练；
- 全程不写 Research Ledger，也不接入当前 Agenda input。

### P2：第一条只读研究 gate

- 一条真实只读 WorkOrder 完成 connector → source/numeric verifier → candidate staging → policy gate；
- closed low-risk plan 和确定性 candidate 可由同一 active versioned policy 授权；人审保留为越界、重大变化和
  verifier 无法自行解决时的升级通道；
- policy accept 或 explicit human accept 都在一个事务内无损写 Evidence/Claim/Relation；reject/revise 不产生
  formal commit；
- retry/revise 有界，失败后不留下 ready/leased 僵尸任务；
- production 未部署前只在隔离 authority 验收自动 Ledger commit，不关闭旧 cron。

硬指标：100% physical attempts 入账；0 fake ModelInvocation；0 未 reservation 的本地 admission；0 超过
本地 hard quota 的 admission；provider-reported overage 必须写 incident 并阻断后续调用；0 secret/Core
path 泄漏；authority idempotency 与数据库 integrity 全部通过。外部计费和 provider quota 可能与本地
状态漂移，不能承诺绝不 overage。

## 当前主要风险

- connector 复用同一 macOS user 时，credential slot 不是 hostile-process sandbox；
- 供应商 quota 与本地计数可能漂移，必须 reservation 后结算并保留 provider-reported 状态；
- 聚合 skill 容易把检索、判断和格式化重新耦合；
- self-generated connector 若缺 recorded fixture、schema drift、429、分页和 partial-result 测试，会在
  正常路径通过、在真实源上失控；
- shadow 通过不等于允许写 Ledger，也不等于可以关闭旧 cron。
- 同一 agent 同时写代码、测试、验证报告和状态文档会形成 self-attestation；最新 HEAD 必须由独立 CI 验证，
  review evidence 为空或采集命令失败时必须 fail closed；
- thesis-impact stack 已有真实 ThesisVersion、30-case no-leakage corpus、wrapper-owned output contract 和多模型
  真实观测；Gemini 3.7 Flash low 已在 direct calibration 30/30，但 production broker 尚不能证明 exact low thinking、
  Google provider controls 和 phase-pinned route，因此仍未达到 live 门槛；
- schema 持续演化但缺少统一迁移纪律；后续任何 schema 改动必须同批提交迁移说明和旧数据 replay/upgrade 测试。

## 相关入口

- 架构蓝图：`docs/reports/vision-and-architecture-v0.1.md`
- 架构裁决：`docs/reports/architecture-debate-and-v0.2-direction.md`
- Core 规格：`SPEC.md`
- Agenda Shadow：`docs/reports/phase-1-agenda-shadow-implementation-2026-08-14.md`
- Agenda 运营与反馈：`docs/reports/phase-1-agenda-control-2026-08-14.md`
- P2 authority resolver 与 SEC canary：`docs/reports/p2-authority-resolver-sec-canary-2026-08-15.md`
- DocumentIndex FTS5：`docs/reports/document-index-fts5-2026-08-15.md`
- ResearchQuestionBacklog：`docs/reports/research-question-backlog-2026-08-15.md`
- Planner SEC public 薄闭环：`docs/reports/research-plan-thin-closure-2026-08-15.md`
- ResearchPlan coordinator：`docs/reports/research-plan-coordinator-admission-2026-08-15.md`
- 当前方向复审与执行计划：`docs/reports/direction-review-and-execution-plan-v0.7-2026-08-21.md`
- 当前执行顺序基线：`docs/reports/vision-and-next-phase-v1.1-2026-09-07.md`（Phase 10）
- 上一版愿景与执行优先级：`docs/reports/vision-and-execution-priority-v0.6-2026-08-15.md`
- Claim → thesis 影响判断：`docs/reports/thesis-impact-verifier-2026-08-20.md`
- ResearchPlan → thesis impact 控制面：`docs/reports/research-plan-thesis-impact-control-2026-08-21.md`
- Thesis impact 模型执行器：`docs/reports/thesis-impact-model-worker-2026-08-21.md`
- Thesis impact 付费边界崩溃恢复：`docs/reports/thesis-impact-model-crash-recovery-2026-08-21.md`
- Gate 2 真实 thesis-impact canary：`docs/reports/gate2-real-thesis-impact-canary-2026-08-21.md`
- Thesis-impact verifier 校准基础：`docs/reports/thesis-impact-verifier-calibration-foundation-2026-08-21.md`
- Thesis-impact verifier 真实校准：`docs/reports/thesis-impact-verifier-live-calibration-2026-08-21.md`
- Thesis-impact 30-case shortlist：`docs/reports/thesis-impact-calibration-v0.2-shortlist-2026-08-22.md`
- Google Generative AI provider controls：`docs/reports/google-generative-ai-provider-controls-2026-08-22.md`
- Wrapper binding 与候选选择：`docs/reports/thesis-impact-verifier-wrapper-selection-2026-08-22.md`
- Phase-pinned verifier policy 与 thinking 控制合同：`docs/reports/verifier-phase-pin-and-thinking-controls-2026-08-22.md`
- 3×30 verifier canary campaign runner：`docs/reports/verifier-canary-campaign-runner-2026-08-22.md`
- 3×30 provider-controlled canary 通过：`docs/reports/verifier-canary-3x30-passed-2026-08-22.md`
- 3×30 canary 独立复核与更正：`docs/reports/verifier-canary-independent-audit-2026-08-22.md`
- GPT-5.6 Sol assessment producer phase pin：`docs/reports/assessment-producer-phase-pin-2026-08-22.md`
- Thesis-impact per-day 预算硬顶与失败告警：`docs/reports/thesis-impact-day-budget-and-alerts-2026-08-22.md`
- Thesis confidence 与 coverage admission ADR：`docs/adr/0001-thesis-confidence-and-coverage-admission.md`
- US IT Services / ACN 初始覆盖准入：`docs/reports/us-it-services-acn-admission-v1-2026-08-23.md`
- Model Input Ledger v1：`docs/reports/model-input-ledger-v1-2026-08-23.md`
- US IT Services Industry Evidence Pack / ACN Overlay v1：`docs/reports/us-it-services-industry-evidence-pack-v1-2026-08-23.md`
- US IT Services Peer Evidence Pack v2：`docs/reports/us-it-services-peer-evidence-pack-v2-2026-08-23.md`
- 财报电话会原文 Evidence Gate v1：`docs/reports/earnings-call-transcript-evidence-gate-v1-2026-08-23.md`
- 人类研究意图与 Bounded Planner Loop 架构讨论：`docs/reports/human-research-intent-and-bounded-planner-loop-v1-2026-08-23.md`
- Dalton Cockpit 与自然语言方向控制：`docs/reports/dalton-cockpit-natural-language-control-architecture-review-2026-08-24.md`
- ACN 研究轨迹只读投影 v0.1：`docs/reports/acn-research-trajectory-read-projection-v0.1-2026-08-24.md`
- 自然语言 intent 与回答路由 ADR：`docs/adr/0002-natural-language-intent-and-answer-routing.md`
- 自然语言 Intent Composer v0.1：`docs/reports/natural-language-intent-composer-v0.1-2026-08-24.md`
- 自然语言 Intent 二次确认与 writer dispatch v0.2：`docs/reports/natural-language-intent-confirmation-dispatch-v0.2-2026-08-25.md`
- Bounded Planner Loop v1 实施：`docs/reports/bounded-planner-loop-v1-implementation-2026-08-23.md`
- Doctrine 与 Planner ContextPack v1：`docs/reports/doctrine-and-planner-context-pack-v1-2026-08-23.md`
- LLM Research Planner 模型扩展校准：`docs/reports/llm-research-planner-model-expansion-v0.2-2026-08-23.md`
- GLM / Luna 复测与宿主 thinking 修正：`docs/reports/llm-research-planner-glm-luna-follow-up-v0.3-2026-08-23.md`
- StatementSnapshot v1：`docs/reports/statement-snapshot-v1-2026-08-23.md`
- TranscriptPolishWorker v1：`docs/reports/transcript-polish-worker-v1-2026-08-23.md`
- Transcript Correction Authority v0.2：`docs/reports/transcript-correction-authority-v0.2-2026-08-23.md`
- Transcript Claim Admission Gate v0.3：`docs/reports/transcript-claim-admission-gate-v0.3-2026-08-24.md`
- Routed TranscriptPolish Worker v0.4：`docs/reports/routed-transcript-polish-worker-v0.4-2026-08-24.md`
- TranscriptPolish 模型校准基础 v0.5：`docs/reports/transcript-polish-calibration-foundation-v0.5-2026-08-24.md`
- TranscriptPolish 模型初轮校准 v0.6：`docs/reports/transcript-polish-model-calibration-v0.6-2026-08-24.md`
- AlphaEngine TranscriptPolish 真实 canary v0.9：`docs/reports/alphaengine-transcript-polish-live-canary-v0.9-2026-08-24.md`
- OpenAI Responses provider controls：`docs/reports/openai-responses-provider-controls-2026-08-22.md`
- Connector Fabric 独立复核与更正：`docs/reports/connector-fabric-next-phase-2026-08-14.md`
- Connector P0-1 authority foundation：`docs/reports/connector-p0-1-authority-foundation-2026-08-14.md`
- Context、Memory 与 Log 裁决：`docs/reports/context-memory-log-subsystem-2026-08-14.md`
- Connector Protocol 与自生成模板：`docs/CONNECTOR_PROTOCOL.md`
- S6 正式晋级前置缺口与 Core-hosted AlphaEngine 获取 v0.1：`docs/reports/s6-formal-promotion-authority-gaps-and-core-acquisition-v0.1-2026-08-26.md`
- transcript 候选进入 CandidateStaging 的裁决（Accepted，选 B）：`docs/adr/0003-transcript-candidate-admission.md`
- S7b 语义 transcript 候选进入 CandidateStaging v0.1：`docs/reports/s7b-qualitative-transcript-candidate-staging-v0.1-2026-08-26.md`
- S7c-3 live 部署与 writer `--candidate-staging` 接线 v0.1：`docs/reports/s7c3-live-deploy-candidate-staging-wiring-v0.1-2026-08-26.md`
- S7c-4 live 首次真实 AlphaEngine 获取 + ACN 语义候选进 staging v0.1：`docs/reports/s7c4-live-acn-acquisition-and-candidate-staging-v0.1-2026-08-26.md`
- S7d-4 Cockpit 读回 Ledger 提升状态、conflict 终态 v0.1：`docs/reports/s7d4-cockpit-promotion-visibility-and-terminal-conflict-v0.1-2026-08-26.md`
- S7d-5 SEC response budget v2（8 MiB）v0.1：`docs/reports/s7d5-sec-response-budget-v2-8mib-v0.1-2026-08-27.md`
- S7d-6 lane-only brief manifest v0.1：`docs/reports/s7d6-brief-v4-lane-only-manifest-v0.1-2026-08-27.md`
- S7d-7 IBM live SEC lane 与 lane-only industry brief v1：`docs/reports/s7d7-live-ibm-and-lane-only-brief-v1-2026-08-27.md`
- AlphaEngine get_document 连接器治理记录（proposed）：`deploy/connector-governance/alphaengine-get-document-v1.json`
