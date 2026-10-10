# Dalton Research OS Cockpit 金融分析师 UX 全量审查报告（独立审查）

- 审查对象：`src/dalton_core/cockpit_v2/`（默认 Cockpit，`/` 与 `/v2` 同体）+ 管理员页 + 图表组件
- 审查日期：2026-10-09
- 受众假设：完全不懂开发的金融行业分析师；目标是“轻松获取关键信息、执行必要操作”
- 方法：逐页源码精读（shell、全部页面 JS、admin、charts），逐按钮/逐状态梳理；抽查复核了最高优先级结论对应的代码行。本次未做浏览器实机渲染，视觉对比度、真实数据下的排版以代码与样式 token 为据（见第 7 节）。
- 说明：本报告为独立审查，与他人同题审查互不引用；文件名刻意区分，避免覆盖。

严重度定义：P0 = 分析师会卡住、误操作或不可逆风险；P1 = 高频困惑或效率损失；P2 = 打磨项。

## 总体结论（先看这 10 条 P0）

1. 问答页提问后按钮禁用约 1 分钟、无进度无取消（`pages/ask.js:72-88`，`api.job(job.job_id)` 未传 `onTick`）。分析师会以为卡死而重复提交。
2. 待我处理页的裁决按钮直接 POST、无二次确认（`pages/tasks.js:127-156`），部分决定不可逆，只能靠 receipt 文案补救。
3. 侧栏“来源与原文”关闭按钮永远写“返回公司研究”（`shell.html:33-37`），从总览/问答/待办打开时会误导。
4. 顶栏状态“后台在线 / 后台未响应 / 研究受阻 N 项”是运维语言，不说明对分析师的影响和下一步（`shell.html:25-29`，`copy.js:132-136`）。
5. 公司研究页财务模型以 `pre.v2-pre` 原文表格呈现（`pages/companies.js:498`），等于把后端文本直接摊给分析师。
6. 情景编辑要求三情景整数 % 合计 100%、停用 checkbox、600 字理由，无引导（`pages/scenario_edit.js:24-94`）。
7. 投资建议规则编辑器约 15 个阈值裸露（`pages/policy.js:10-12,166-177`），分析师无法理解、只能乱填。
8. 管理员页裸露 shell 命令预览与执行（`admin/admin.js:225-246`，`admin/data.py`），`--apply`、`launchctl`、`deploy.sh` 等直达分析师可达页面。
9. 紧急停止/全环境生效等高危操作只有 reason + confirm 防护（`admin/admin.js:392-399,659-664,686-688`）。
10. 桥接图每个柱子都是 `role=button` 可点且推算值透明度 0.55（`charts/bridge.mjs:40-48`，`charts/charts.css:159`），可点性无视觉区分，部分柱子标签被硬编码为“营业成本及费用（推算）”。

## 1. 全局外壳（顶栏 / 左导航 / 状态 / 侧栏）

文件：`shell.html`、`shell.js`、`shell.css`、`copy.js`、`pages/common.js`。

- P0（语言）：状态 pills “后台在线 / 后台未响应 / 后台状态读取中”（`shell.html:26-28`、`shell.js:839-842`、`copy.js:134-135`）。“后台”是开发概念，离线时不说明原因与影响。建议改为“研究服务正常 / 研究服务暂停·请联系管理员”，保留更新时间。
- P0（交互）：侧栏关闭按钮永远是“返回公司研究”（`shell.html:33-37`、`shell.js:632`、`copy.js:141`）。建议默认文案为“关闭面板”，仅在公司研究页透传返回标签。
- P1（语言）：“研究受阻 N 项 / 研究受阻：读取中”（`copy.js:136`、`shell.html:27`）是运维黑话且计数不可行动。建议“等你处理 N 项 / 暂无待办”，保留指向 `#/tasks` 的链接。
- P1（交互）：项目切换器用整页 `location.href` 跳到 `#/overview`（`shell.js:811-832`），丢弃公司/标签/筛选；“（暂不可用）”不给原因；空项目回退“当前研究项目”是死文案。建议保留 hash 路由、注明不可用原因、仅一个项目时隐藏切换器。
- P1（文字）：`数据更新于 YYYY-MM-DD HH:mm`（`shell.js:855-858`，`fmt.datetime`）涉及时区tooltip 才解释。建议 visible 追加“（你的本地时间）”或只显示日期。
- P1（视觉）：错误盒“暂时无法读取”配最低对比 quiet/ghost “重试”（`shell.js:522-526`；实际层级以 `cockpit_control.html:26-32` 的 primary 渐变 / ghost 描边为准）。建议重试用 primary 实心按钮。
- P2（语言）：跳过链接“跳到正文”、左下“返回旧版”（`shell.html:11,16`）。建议“跳到主要内容”、“返回旧版工作台”。
- P2（语言/视觉）：金额 `X 亿美元 / 万美元` 与 `US$1,234.56` 混用（`fmt.money` vs `fmt.usd`，`shell.js:200-202`）；徽章 `N 项` 过简（`shell.js:683`）。建议统一美元表述 + 千分位，徽章写“N 项待处理”。

## 2. 研究总览（#/overview）

文件：`pages/overview.js`、`pages/common.js`、`charts/comparison.mjs`、`pages/task_kinds.js`。

- P0（语言）：对比表题注“按预期回报…口径不足…无法比较”（`common.js` compareTable）——“口径”“无法比较”无解释。建议“按所选列从高到低排；资料不全标‘暂无可比数据’并附原因”，每列头加单位/口径小字。
- P0（语言）：指标格三列值 + “简化估算”警告 + 日期注（`common.js` metricCell/ret-risk），预测与实际分不清。建议值下三行：大数字 + 单位 / 灰色“依据…（截至…）”/“约算（供参考）”。
- P1（交互）：“待我决定”卡只有“建议：…”+ 仅前 3 项 + “查看全部 N 项并处理”，无动词按钮（`overview.js` renderDecisions）。建议每卡主按钮（去确认 / 去补充 / 转管理员），链接改为“查看并处理全部 N 项”。
- P1（语言/泄漏）：“本周复盘 / 研究轨迹 / 运行故障与失败台账（管理员页面）”“AI 调用 / 预算 / 失败台账”（`overview.js` records）。建议“本周小结 / 研究过程记录”，管理员卡片移除或折为“系统状态（管理员）”，只留业务结果表述。
- P1（语言）：“X 个情景触发条件已满足 / 已失效”+“研究受阻：label:step”（`overview.js` importantChanges）。建议“看涨 / 看跌情景的条件成立了 X 个”、“资料收集中断：…下一步…”，每项配“去看看”。
- P2（文字）：“行业投资论点”+ 置信度 tag + 各公司判断 120 字截断 because（`overview.js` conclusions）。建议论点 2 行 + 展开全文，判断卡一句话结论，because 折叠进详情。
- P2（泄漏）：“重新进行语言质检”（`task_kinds.js` publication_parked）、空态 “暂无数据 + empty_reason 原文”（charts core emptyState）。建议“重新生成中文定稿”；空态统一“这张图还没有数据，因为…（下一步…）”，屏蔽英文 reason。

## 3. 公司对比组件（comparison.mjs）

- P1（视觉 + 语言）：“不可比较”徽章 + 单字“估”+ 提示“否定条件已出现 X 个”+ 英文 `basis_label` + risk 显示为“悲观概率”（`comparison.mjs` buildComparison 约 :24-33）。建议单点修复：“估”→“约算”；“否定”→“看跌理由出现 X 条”；“不可比较”配 hover/副行写“因…暂不排名”；basis 译中文；risk 口径用术语 popover（glossary）解释。

## 4. 公司研究（#/company，含全部标签页）

文件：`pages/companies.js`、`pages/scenario_edit.js`、`pages/research_log.js`、`pages/decision_basis.js`、`copy.js`（SECTION_TITLES / DEBATE_ROLES / METRIC_LABELS）。

- P0（文字 + 交互）：财务模型以 `pre.v2-pre` 原文表格呈现（`companies.js:498`），另有“导出模板 / 默认模板 / version_ref / reason”三按钮（`:422-471`）。建议渲染中文表头表格；收敛为单个“导出财务模型（Excel）”；模板选择移到设置页。
- P0（交互）：三情景整数 % 合计 100% + 停用 checkbox + 600 字理由（`scenario_edit.js:24-49,81-94`），`copy.js` 无引导。建议滑杆 + 自动配平 + 一句话示例 + 中文就地错误提示。
- P1（语言）：未知指标回退为裸英文（`companies.js:276` `METRIC_LABELS[x.metric] || span[lang=en]x.label`，`copy.js:109-117` 仅 7 个映射）。建议补全中文标签字典 + 首见加注；兜底显示“暂无中文名”而非裸英文。
- P1（语言）：字段标签未翻译时 `span[lang=en]f.label`（`companies.js:225-229`）。建议统一中文标签字典，单位明确“倍 / % / 基点”。
- P1（泄漏）：英文 `display_statement` + “记录编号：${ref}”+ 版本/合并/canonical 筛选（`companies.js:372-375,397-401`）。建议默认中文结论 + 标题日期；编号与版本筛选项收进“核对详情”。
- P1（语言）：多空标题 `splitDebateTitle` + `DEBATE_ROLES`（多方/空方）回退“研究内容”，长英文问题裸露（`companies.js:313-315,667-676`，`copy.js:120-127`）。建议中文短问题 + “看多 / 看空 / 市场观点”分节标题。
- P1（语言）：回报归因 `legsTable` + `realised_trailing_year / expected_driver_model` + “贡献 contribution_pp / 未单独拆出”（`companies.js:246-260,632-654`）。建议四张中文卡：过去 12 个月实际 / 模型预期 / 一致预期 / 卖方预期 + “其中××贡献×个百分点”。
- P2（视觉 + 泄漏）：研究日志 KINDS（AI 调用/预算）/ DETAIL（claimed/enqueued/worker_completion）/ calls/share_of_spend（`research_log.js:14-20,86-103`），judgements/because（`companies.js:513-524`），cadence（decided_by_brain/observer_label，`:557`）。建议分析师页只留事件/判断/复盘；AI 轨迹与预算移到管理员页。

## 5. 研究问答（#/ask）

文件：`pages/ask.js`。已逐行复核提交流程（:68-88）。

- P0（交互）：提交后按钮禁用约 1 分钟、静态文案“正在查阅资料…”，无进度、无取消、无 ETA（`ask.js:72-73`；`ctx.api.job(job.job_id)` 未传 `onTick`，而 shell 的 `job()` 明明支持逐轮回调）。分析师会误判卡死而重复提交（每次产生一次 AI 调用费用）。建议进度条 + “已等待 xx 秒”+ 取消按钮 + 完成播报。
- P0（语言）：页头声明“不会自动带上上一条问题的上下文”，但“基于这条追问”按钮把 60 字旧问题塞进新问题（`ask.js:47,107-109`）。行为与声明矛盾。建议改名“就这条再问一次（开新问题）”，并携带完整问题 + 结论引用而非截断 60 字。
- P1（文字）：费用提示“回答只使用本项目已整理的资料，会产生一次 AI 调用费用，通常需要约一分钟”藏在表单注脚（`ask.js:62`）。建议提到提交按钮旁，提问前可见。

## 6. 待我处理（#/tasks，含任务录入与反馈）

文件：`pages/tasks.js`、`pages/task_entry.js`、`pages/task_kinds.js`、`pages/feedback.js`。已逐行复核裁决流程（`tasks.js:127-156`）。

- P0（交互）：裁决按钮（服务端下发标签）点击即直接 POST `/v1/cockpit/decide`，无确认对话框（`tasks.js:127-156`）；失败后才用 decision-status 查询补救（`:140-145`）。部分决定不可逆。建议提交前确认对话框：决定内容 + 后果 + 必填说明，一次确认后提交。
- P1（交互）：ack “已知悉 / 暂不提醒 / 放弃”每项要点两次（展开 + 理由表单），理由必填、snooze 加天数，无引导（`task_entry.js:48-66`）。建议合并为单个表单：单选 + 天数 + 理由 + 一次提交；“放弃”配红色后果说明。
- P1（交互）：“放行 / 再买 / 撤回”+ fee cap 裸数字（min 0.01 / max 20）+ 英文 aria，超限才报内部错误；用原生 `confirm()` 破坏 receipt 风格（`task_entry.js:75-92`）。建议“上限_美元最高 20”中文说明 + 行内中文校验；站内对话框替代原生 confirm。
- P1（语言）：“交给管理员处理 / 复制摘要”（`tasks.js:25-36`）——管理员是谁、入口在哪、复制后手动发给谁，全未说明；“放行一次重新进入 / 撤回”晦涩。建议“请管理员处理（已准备好说明）”+ 收件人与话术；动词改为“恢复研究 / 结束研究”。
- P1（文字）反馈五个按钮并排：“已阅读 / 有参考价值 / 证据不够 / 不同意 / 需要重写”（`feedback.js:9-15,50-75`），“不同意”与“需要重写”不可区分；回执“反馈不是审批…”泄气。建议每按钮一行后果说明；默认选中“证据不够”；页首先讲用途。
- P2（泄漏）：“缺少版本信息（target_hash）”“确认入口正在生成，稍后刷新本页”（`feedback.js:21-23`，`tasks.js:80`）；历史无刷新。建议“这条内容太旧，请点更新结果”+ 页内“更新结果”自动重取。

## 7. 研究设置 / 投资建议规则 / 报告模板（#/settings）

文件：`pages/settings.js`、`pages/policy.js`、`pages/steer.js`、`pages/templates.js`。

- P0（文字 + 交互）：规则编辑器 6 个整数 + 2×4 horizon % + 每日 draft 上限，约 15 个阈值裸露并交叉校验（`policy.js:10-12,166-177`）。分析师只能凭感觉填。建议预设“稳健 / 均衡 / 进取”三档；全部数字收进 `<details>` 高级区。
- P1（语言）：“今日 AI 费用…调用 N 次 / 上限 M 次”+“调整预算上限由管理员处理”无入口（`settings.js:196-201`）。建议“今日已花 $x / 上限 $y”+“申请调整”按钮直达管理员。
- P1（交互）：覆盖范围注脚“增减公司由管理员在运行控制里办理”只读 + 文字交接（`settings.js:134-142`）。建议深链到管理员运行控制页，或“一键联系管理员”。
- P2（语言）：版本对比“对比：左 / 右”+ `prior_version_ref` 冲突技术化（`policy.js:246-260`）。建议“旧版 vX vs 新版 vY 差异”，自动选中相邻版本。
- P1（泄漏）：模板 slug 小写连字符 3-48 + 报错直面分析师（`templates.js:27,76-91,182-184`）。建议隐藏 slug（自动生成、只读展示）。
- P1（交互）：三表 sheet 映射 + industries “只作说明，真正指定请管理员”，上传走入死胡同（`templates.js:149-175,229`）。建议成功页“下一步：去指定范围”跳转；行业勾选要么直通、要么明确标只读。

## 8. 管理员页（/v2/admin）

文件：`admin/admin.html`、`admin/admin.js`、`admin/admin.css`、`admin/data.py`、`admin/routes.py`。核心风险：分析师可达页面的技术泄漏与高危操作防护不足。

- P0（泄漏）：治理 approve / 飞书 enable `--apply` / `launchctl kickstart` / `deploy.sh` / `dalton-health` 以裸 shell 预览 + 执行，ports / tickets / leases / `--apply` / `--revoke` / copy 直露（`admin.js:225-246`，`data.py:181-184,470-477,726-770`）。建议“技术管理员专用”门禁横幅；执行加二次确认 + 白话后果说明。
- P0（交互）：emergency_stop / stop-topic / owner_door / model_allow 全环境即时生效，“同步到所有环境 / 重启生效”的 blast radius 仅靠 reason + confirm 防护（`admin.js:392-399,659-664,686-688`，`routes.py:84-108`）。建议分级确认（键入后果关键词）+ 影响清单；分析师视图隐藏管理员入口。
- P2（视觉）：danger zone 仅代码确认 class 存在，未做页面级对比度检查（`admin.css` 未精读）。建议单独补一轮危险按钮对比度与间距走查。

## 9. 金融图表组件（charts/）

文件：`charts.mjs`、`price.mjs`、`model.mjs`、`returns.mjs`、`scenarios.mjs`、`waterfall.mjs`、`comparison.mjs`、`bridge.mjs`、`charts.css`、`routes.py`。

- P0（视觉 + 交互）：桥接图每个柱子 `role=button` 进 statementPanel，仅 hint 说明；推算值透明度 0.55（`bridge.mjs:40-48`，`charts.css:159`）；短标签把每个推算步骤硬编码为“营业成本及费用（推算）”（`:44`）。建议仅有依据的柱子可点并给视觉 affordance；短标签用 `s.label`；提高推算透明度但保留虚线/标识区分。
- P1（视觉/单位）：price tick `toFixed(0)`（`price.mjs:24`）与 `close.toFixed(2) + 美元`（`:70`）、model `revenue en-US 亿美元`（`model.mjs:164`）、bridge divisor 轴、`returns.mjs:47` pp 并存，美元/亿美元/pp 无口径注；低价股 tick 取整失真。建议每卡一张单位行 + 量级自适应小数。
- P1（交互）：price 2×6px rects + “悬停可看”（`price.mjs:32-35,88-90`），tooltip 仅 pointermove/focus。建议增大热区 + 可见圆点 + 行内事件列表（移动端与键盘可达）。
- P1（语言）：price “指标 / 数值 / 历史分位 / 说明”+ 裸 `percentile_basis_label / basis`（`price.mjs:92-100`）。建议“历史水平（过去 N 年位置）”+ 白话注；代码类字段进脚注。
- P1（交互 + 文字）：model revenue/margin 小 multiples + 口径 select + 实线/虚线/实心/空心图例（`model.mjs:102-108,155-184`）+ 粘性 `*推导值 / 缺失` details（`:142-149`）。建议默认单 margin + 行内“虚线 = 预测”；表格收进“展开明细”。
- P1（语言）：returns SHORT（价格回报 / 总回报）+ 无单位 signed tick（`:28`）+ rounding pp（`:29`）+ “各段窗口口径不同不能比较”（`:47`）。建议轴与 tick 补 pp 后缀；total 改“含股息总回报”；residual 改“尾数差异约 X”。
- P1（文字 + 视觉）：scenarios 概率条 + 合计 ≠100% 未归一 + “触发 x/y；否定 a/b”+ 叙事藏 details（`scenarios.mjs:31-44`）；状态色用未定义的 `--dv2c-good / warning / serious / critical`（`charts.css:223-227`）。建议定义变量 + 白话合计行“三者相加 ≠100% 仅参考”+ 每行徽章。
- P2（语言/空态）：core 空态“这张图还没有数据”/ 仅 error（`core.mjs:124-134`）、`charts.mjs:29`“没有指定公司。”、asset/kind 404（`routes.py:67,93`）。建议行动空态（选择一家公司 / 换期间重试）+ “不可比较 / 估 / 风险口径”术语 popover。

## 10. 跨页共性问题（修一处、惠全站）

1. 技术概念泄漏词典：后台 / 车道 / 产物 / 资料底座 / 票据 / --apply / 端口进程 / 语言质检。`copy.js` COPY_TABLE 已覆盖 36 组替换但仅 7 个指标映射（METRIC_LABELS）；建议把第 4、6、8 节的泄漏词全部纳入 COPY_TABLE 并补全指标中文标签。
2. 金额单位三制并存：`fmt.money`（亿/万）vs `fmt.usd`（US$）vs 图表 en-US 分隔。建议全站单一美元规则 + 每表头“金额：百万美元”式口径行（`fmt.unitHeader` 已有能力，未全站使用）。
3. 确认对话框缺失是系统性问题：tasks 裁决、admin 高危操作、task_entry 放弃，都缺“后果说明 + 二次确认”。建议统一确认组件。
4. 空态/错误态无行动：大量“暂无数据 / 暂时无法读取”不给下一步。建议空态三件套：原因 + 行动按钮 + 预期（“选择一家公司”“换期间重试”“更新结果”）。
5. 术语无处可查：“口径”“不可比较”“悲观概率”“历史分位”“基点”全站裸奔。建议 GLOSSARY 首见加注 + 术语 popover（`copy.js` glossSegments 已有能力，扩大覆盖）。
6. 按钮层级：primary（渐变实心）= 提交/创建，ghost（描边）= 探索性，nav-item = 导航（`cockpit_control.html:26-44`）。建议“重试”“更新结果”等恢复类操作一律 primary；危险操作一律 danger + 间距隔离。

## 11. 修改路线图（按优先级）

- 第一批（P0，分析师可用性门槛）：问答进度/取消、裁决确认框、侧栏关闭文案、状态 pills 白话、模型表中文渲染、情景滑杆、规则预设三档、admin 门禁 + 高危分级确认、桥接图可点性修复。
- 第二批（P1，高频体验）：待决定卡动词按钮、记录页去运维化、追问语义修复、ack 表单合并、fee 上限中文校验、预算“申请调整”入口、图表单位行与热区、terminology popover。
- 第三批（P2，打磨）：skip/旧版文案、徽章措辞、论点折叠、模板 slug 隐藏、版本对比白话、danger zone 走查。

## 12. 未覆盖与未验证事项（诚实声明）

- 未做浏览器实机渲染：按钮 disabled/focus/aria 态、真实数据下的断行与溢出、颜色对比度均为代码级结论，未截图验证。
- `copy.js` SECTION_TITLES / `causal_chain:NN` 在真实服务端长文本下的中文质量未验证（仅映射逻辑）。
- `/v2/admin` 对分析师账号的实际可达性未验证（`common.js` adminHref/slot 门控未精读）；本报告按“分析师可见”评估。
- `policy` 表单各字段的服务端下发中文标签未逐条验证（仅键与 help）。
- `charts_api.py` 的单位/除数/币种 payload 未检查（空搜无命中）；单位混用结论基于 `.mjs` 渲染器。
- 本报告与同题他人审查独立完成，未互读；如有结论冲突，以代码行号为准重新核对。

