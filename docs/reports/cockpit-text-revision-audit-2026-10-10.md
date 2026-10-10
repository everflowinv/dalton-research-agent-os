# 文字修订报告落实情况核对（2026-10-10，只读，对照 main `cb18c6ad`）

对象：`cockpit-v2-all-pages-text-revision-for-financial-analysts-2026-10-09.md` 的全部 284 条具体改词建议（含第 10 章映射表与第 11 章实施清单）。线上 8793 当时仍运行批次 am 之前的版本，页面显示的是报告「现有」列；以下判断的是 main 的代码。

| 结果 | 条数 |
|---|---|
| 已按报告应用 | 246 |
| 有意偏离 | 18 |
| 未应用或只改了一部分 | 20 |

禁用词扫描在 main 上通过，但 20 条未应用项的旧措辞都不在 BANNED_TERMS 里，lint 拦不住；`conviction_policy.py`、`final_text_state.py`、`event_judgement_result.py` 也不在扫描名单里。

## 有意偏离（18 条，摘要）
顶栏保留「研究服务正常 · 最近一次运行」（T:77-80 记录原因：三种状态同组措辞；时间戳是服务运行时间非资料更新时间）；全站保留「研究项目」而非「研究组合」；首屏无变化时优先显示当前核心论点；证伪卡片统一用第 10 章措辞「核心假设证伪预警触发」；比较矩阵已无「关键估值倍数」列；盘中行情未写延迟分钟；第 8 章名不含「杜邦」；5.7 列名用「预警触发线」；5.9 板块名沿用 4.3；问答失败卡片保留原句、缩小范围建议放入「谁处理」行；审批按钮「核准通过」+ 拆成「退回补充资料」「否决（终止本轮）」；确认后字段不写锁定预测/更新目标价；「代码默认」采用 8.3 的「机构默认投研纪律标准」。多数偏离的原因未在代码里记录。

## 未应用 / 部分应用（20 条）
| 报告位置 | 建议 | 现状（文件:行） |
|---|---|---|
| 2.2 项目下拉框 | 研究项目 → 当前研究组合/课题 | shell.html:21；T:96 |
| 2.2 切换提示 | 切换研究覆盖组合 | shell.html:23 |
| 2.2 Tab3 | 研究问答 → 智能投研问答 | T:129；ask.js:148 |
| 2.2 Tab5 | 研究设置 → 投研规则与设置 | T:131；settings.js:108 |
| 2.3 关闭按钮 | 关闭 [Esc] | shell.html:38 只有「关闭」 |
| 3.1 首屏标题 | 研究总览 · 投研动态看板 | overview.js:167 |
| 4.2 面包屑 | 覆盖公司研究 › Accenture plc（NYSE: ACN） | companies.js:124 无交易所与全称 |
| 4.2 研究阶段 | 准入审核已通过（盈利预测进行中） | company_view.js:113 仍有「（阶段审查结果，不代表模型已完成）」 |
| 5.1 关键指标行名 | 调整后摊薄每股收益增速（Non-GAAP EPS YoY） | cockpit_plane.py:1529/1542；未映射指标显示英文 |
| 5.3 关键观测点 | 新增字段 | 无 |
| 5.6 独立核验 | 独立逻辑交叉质检：合规通过 / 存疑需复核 | cockpit_plane.py:942-944 |
| 6.1 页面主标题 | 智能投研问答（Research Q&A） | ask.js:148 |
| 6.3 依据元数据 | 公司 ｜ Form 10-Q（FY2026 Q3 官方申报） | ask.js:124-125 通用元数据 |
| 7.1 页面主标题 | 待办决策与事项中心 | tasks.js:218 |
| 8.3 规则 1 | 买入建议最低预期年化回报率（Hurdle Rate） | conviction_policy.py:99；`policy.hurdle` 未使用 |
| 8.3 规则 2 | 最低收益风险比 | 规则无此字段 |
| 8.3 规则 3 | 底稿证据充分度准入门槛 | conviction_policy.py:95；policy.js:25 |
| 10 第 13 行 | 初稿（待审核签发） | 源头 final_text_state.py:51 未改；HTML 导出（research_html_export.py:674-677）不经 scrub 仍旧词 |
| 10 第 17 行 | 订单收入比 | 源头 cockpit_plane.py:1568、research_gap_display.py:183-186、event_judgement_result.py:109 仍「订单出货比」 |
| 11 情景实时测算 | 标注「敏感性测算」 | scenario_edit.js:198 |

## 改名后留下的不一致（分析师可见）
- 待办卡片说明仍让人点「已知悉」「暂不提醒」「放弃这份资料」「停止这项研究」，页面按钮已叫「已阅知悉」「静音该提示」「忽略该缺失资料」（needs_human.py:490/569/571/634/1616/2407/2436/2465；needs_human_ack.py:161）；点完按钮的回执用旧说法（cockpit_plane.py:9034-9035）。原因：`copy()`/`scrub()` 不改写「」内文字。
- 设置页：settings.js:146 指向「投研方向指引」，该节标题是「调整研究方向」（steer.js:82/93）；settings.js:136 仍「这个研究项目还没有研究目标。」
- 旧章节名残留：templates.js:234「导出财务模型」；company_model.js:146「报告模板」；evidence_api.py:141/817/916、evidence_search_api.py:44「证据」；weekly_brief.js 仍「每周简报」而设置页改为「每周投研周报」。
- 管理员页/技术详情（按设计保留）：admin.js「规划器最近错误」「重新进行语言质检」；admin/data.py:744/801「放行一次重新进入」；needs_human.py:1106-1183 命令行说明（term-ok）；workspace_health_parity.py:524「待我处理」。

## 处理建议（并入批次 ap 的文字收尾）
1. 20 条未应用项逐条在术语注册表或源头生成器落实；源头词（final_text_state、cockpit_plane、research_gap_display、event_judgement_result、conviction_policy）改在生成处。
2. 把这些旧措辞加入 BANNED_TERMS，并把 `conviction_policy.py`、`final_text_state.py`、`event_judgement_result.py`、`research_html_export.py` 加入 lint 扫描名单；HTML 导出路径必须经 scrub 或从注册表取词。
3. 「」内引用的按钮名/章节名改为从注册表取（新增一条规则：服务端说明文本引用 UI 名称时用 `label()`，lint 检查「」内出现的已改名旧词）。
4. 对 18 条有意偏离，在 terminology.py 里逐条记录原因（现在多数未记录）。
