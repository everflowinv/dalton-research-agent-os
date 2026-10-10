# Dalton Research OS Cockpit 全工作环境所有页面检查报告 (2026-10-08)

完整深度检查报告与代码级修复方案已生成并归档于：  
👉 2026-10-08 完整页面检查报告（原件已私有归档）

---

## 一、 检查背景与覆盖范围

本次检查响应用户要求，对 **Dalton Research OS Cockpit 投研工作台的所有运行环境及所有页面** 进行拉网式系统检查。重点排查以下四类问题：
1. **内容不符合预期**（接口崩溃、白屏、逻辑断裂、占位符替代、虚假引导）
2. **格式混乱**（长数字裸露、缺乏千分符与单位换算、表格挤压、CSS 文本粘连）
3. **中文不够简洁通顺让人容易理解**（技术黑话、生硬机翻、语序倒装、中英夹杂）
4. **代码或原始标签露出**（XBRL 代码标签、SHA 哈希、系统内部 URI、代码类名与文件路径）

### 检查覆盖环境：
- **现有研究环境 (Legacy IT 服务，端口 `8793`)**：覆盖 ACN、CTSH、EPAM、IBM、DXC 5 家公司
- **新研究环境 (空白环境，端口 `8794`，`ws-e399`)**：待装载初始研究任务的纯净工作区
- **美国 Hyperscaler 研究 (端口 `8795`，`ws-7d89`)**：覆盖 AMZN、GOOGL、META、MSFT 4 家公司
- **全系统页面**：
  - 6 大主导航视图（`研究环境`、`研究目标与计划`、`策略指引`、`投研执行日志`、`投研问答`、`待办审批`）
  - 9 家公司的全部 7 个详情标签页（`概览`、`证据`、`模型`、`事件`、`情景与触发`、`回报归因`、`产物`，共 63 个子页面）
  - 5 个全局功能弹窗（`数据源与接入能力`、`模型与预算`、`运维待办`、`每周投研运行复盘`、`执行轨迹`）
  - 独立页面（`/weekly-brief` 每周简报页、`/legacy` 历史审阅页）

---

## 二、 核心高危发现摘要

### 1. 内容不符合预期 (P0 / P1)
- **空白工作区 (8794) `/legacy` 抛异常报 HTTP 503 (P0 阻断)**：由于空白工作区未初始化任务版本，调用接口触发 `DiscoveryLaunchRejected` 未捕获异常，页面顶部弹出红字崩溃告警。
- **文字表达审查门禁误杀财务报表科目标签 (P0 严重)**：在 ACN、EPAM、DXC、GOOGL、META 的「模型」标签页中，由于科目未完全收录在字典中，表格行被粗暴替换为“正文正在检查文字表达，完成后会显示。 [少数股东损益]”。
- **MSFT「模型」标签页 0 字符完全空白 (P1 显著)**：MSFT 后台未构建模型时返回 400，前端无空状态兜底，呈现为完全空白的纯白区域。
- **投研问答上下文超限提示模糊误导 (P1 显著)**：大模型提问上下文超限，回答栏显示“回答问题暂时未完成，请稍后重试”，误导分析师盲目重试。

### 2. 格式混乱 (P1 / P2)
- **财务金额大面积裸露 11~12 位长数字，缺乏千分符与单位 (P1 显著)**：在全量公司的「产物」与「证据」中，巨额数字直接渲染为 `USD 200606000000`、`17727871000`、`USD 716924 million`，缺乏三位分节符与“亿美元”换算。
- **同行对比数据挤压为单行纯文本 (P2 一般)**：多家公司的收入与增速连缀在一行纯文本内，缺少表格栅格与表头对齐。
- **数据源弹窗 (`openSources`) CSS 连写粘连 (P2 一般)**：`.lane small` 缺少块级显示，导致说明文本与连接状态紧贴挤压在一起。

### 3. 中文不够简洁通顺让人容易理解 (P1 / P2)
- **8795「每周投研复盘」卡在未校对草稿，充斥开发底层黑话 (P1 显著)**：正文出现“占 mission 这一周预算上限”、“没有已注册 lane 认领它”、“Claim 层退役 1071 条”、“记录 103 份 research plan、提出 234 条 inquiry”，完全脱离业务人员视角。
- **投研目标与公司档案中英夹杂 (P2 一般)**：出现 `US IT services`、`Initial Screen`、`Thesis`、`fiscal 2026 第二季度`（倒装）、`book-to-bill 低于一` 等生硬表达。
- **待办审批充斥内部状态机术语 (P1 显著)**：如 `admission 准入时冻结的输出上限全部花在思考上`、`放一条新 WorkOrder`、`LaneChildRejected`。

### 4. 代码或原始标签露出 (P1 / P2)
- **SEC XBRL 代码标签泄露至产物来源 (P1 显著)**：备忘录来源中普遍出现 `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`、`us-gaap:CostOfGoodsAndServicesSold` 等原始开发标签。
- **独立页 `/legacy` 暴露底层 SHA 哈希与 URI (P1 显著)**：表格大面积直接渲染 `claim-version:sha256:8f4a...`、`company:ticker:amzn · metric:revenue` 等内部数据库寻址标识。
- **每周复盘直接暴露代码类名与文件路径 (P1 显著)**：正文出现 `BoundedPlannerLoop`、`ad-hoc ProbeTemplate`、`run_once`、`run/heartbeat.json`。

---

## 三、 核心整改建议与对应文件

| 缺陷分类 | 涉及文件 | 修复重点 |
|:---|:---|:---|
| **空白环境 503 异常** | [writer_server.py](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/writer_server.py#L6138) / [agenda_control.py](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/agenda_control.py#L1267) | 增加无任务计划时的优雅空状态兜底，捕获异常并返回空列表。 |
| **报表科目长句门禁误杀** | [cockpit_plane.py](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/cockpit_plane.py#L7209-L7217) | 报表行优先返回原始科目名或标准中文，禁止替换为段落审核占位符。 |
| **MSFT 空模型白屏** | [cockpit_control.html](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/cockpit_control.html#L876) | 补充标准空状态组件：“该企业财务预测模型正在构建中”。 |
| **财务数值无千分符与单位** | [cockpit_control.html](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/cockpit_control.html#L841) | 引入 `fmtFinancial` 工具函数，大于 10 亿自动换算为“亿美元”，补充千分逗号。 |
| **SEC XBRL 标签泄露** | [cockpit_control.html](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/cockpit_control.html#L856) | 在 `sourceLabel` 中增加正则清洗，清除 `(us-gaap:...)` 并映射为官方披露文件。 |
| **复盘底层技术黑话** | [research_cycle_reflection.py](file://$HOME/Projects/dalton-research-agent-os/src/dalton_core/research_cycle_reflection.py#L910-L1016) | 全面重写模版文案，以买方研究总监视角重述，剔除代码类名与开发备忘。 |

---

详细技术分析、各公司各页面截图级问题定位与完整代码修复片段，请参见完整报告：  
👉 **2026-10-08 完整页面检查报告（原件已私有归档）**
