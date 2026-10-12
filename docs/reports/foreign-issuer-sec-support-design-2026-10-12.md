# 外国发行人 SEC 财报支持：独立设计，尚未实现

日期：2026-10-12。只读调查；没有生产写入、模型调用或人工派工。本设计不属于 v20 发布候选。

## 已证明的断点

NTR 的强绑定本地 SEC submissions 原件（CIK 0001725964；52,666 bytes；SHA256 `a3eda661a136d334ee071fa18097873c40448f6ca62afd4d88e7e88e647def3d`）列出 40-F 8 件、6-K 221 件、6-K/A 2 件，10-K/10-Q/8-K 均为零。原查询按国内表单过滤得到 empty，两个财报子任务分别报 no 10-K/no 10-Q；已登记财报数及当前材料请求数都为零。证据是已有收据 `/tmp/ar-v01119-agriculture-seven-current-causal-summary-ro-r01-20261012.json`，不是新查数据库。不能把这个问题归因于没有资料、权限拒绝或排队。

SEC 的 40-F 可用于符合条件的加拿大发行人年报和登记，年报包括加拿大规定的年度信息文件、经审计年度财务报表及 MD&A。6-K 可以包含多种重大信息和附件；并非每份都是季度财报，也不必都有 XBRL。其 reportDate 不足以证明财务期间。不能把 40-F 改名为 10-K，把 6-K 改名为 10-Q，或把 furnished 与 filed 的来源状态混为一谈。[SEC Form 40-F](https://www.sec.gov/files/form40-f.pdf)、[SEC Form 6-K](https://www.sec.gov/files/form6-k.pdf)。

## 现有架构与真正需要变更的位置

| 环节 | 当前行为 | 需要的通用能力 |
|---|---|---|
| `sec_plan_specs.py:46`、`mission_source_discovery.py:657` | 代码生成 10-K 与 Item 2.02 8-K 查询 | 按强 submissions/当前年度报告证明生成发行人适用表单；保留原治理预算和公司范围 |
| `sec_filings_index.py:157`、`sec_submissions_pages.py` | 枚举原表单、CIK、accession、原主文件；已有 archive 强续页 | 复用原能力；list_filings 的 form 已是 string，无需为 40-F/6-K 改这个操作的旧 schema/hash |
| `acquired_sec_filings.py:48,237` | 白名单只有国内表单；附件不能冒充 primary | 加 foreign 原件读取；新增显式附件角色和父 filing 绑定，保持 primary/附件区别 |
| `sec_financials_cli.py:101,301`、launcher `:86` | CLI/launcher 仅 10-K/Q；取 `filing.xbrl()` 和三张 statement | 原表单、具体报告包及解析能力预检；不能把无 XBRL 当空财报成功 |
| `mission_statement_lane.py:73,107,225` | 表单和定期刷新按 10-K/Q；隐含年/季节奏 | 40-F 年度与 6-K 报告包分开；6-K 以已证明财务期间更新，不按固定季度假设 |
| `coverage_mission.py:5059,5293`、schema `:644,683` | dispatch/filing 应用门及 SQL CHECK 都只 10-K/Q | 新版本、来源绑定的 foreign statement 接受与迁移；单加 Python enum 不足 |
| `statement_snapshot.py:42,336`、`research_plan.py:601`、`mission_sec_quarters.py:157` | 旧 snapshot/companyfacts/自动季度规则是国内表单及 us-gaap/USD 专用 | 保留旧规则；外国申报不能借旧规则静默变成正式季度事实 |
| material/planner/文档 inventory | 国内 form/kind、10-K typed notes 入口 | 明确 annual/interim disclosure scope 和可执行反馈；不能找到 6-K 即关闭整个季度需求 |

## 建议交付顺序

### 第一包：正确路由、可读原件及精确取证任务

新增纯 helper `sec_issuer_reporting_profile`，从已有、强 CIK/原件/查询/envelope 绑定的 submissions 派生候选报告路径。以当前年度报告的真实 40-F/20-F 为强信号，兼容发行人改变申报制度或同时存在其他表单；不能仅凭 domestic 查询 empty、ticker、注册国家或一次 6-K 认定 foreign。将证明及原件 hash 进入派生配置/查询身份，缺证据则反馈需要 profile 取证。

按公司生成 40-F annual 与 6-K disclosure 查询，支持原生 `/A`，保留 amendment accession，不合并改写旧答复。不能简单把新的 foreign specs 加进所有公司全局笛卡尔积，令美国公司也反复查空。新查询仍走既有 SEC grant、host/CIK、日次数、免费已持有原件优先、游标、每 tick 数量/时间门；不新增模型恢复或预算。

从 exact primary 或申报目录/primary 中明确列出的附件建立 `foreign_report_package`。证明至少包含 issuer CIK、company、accession、form、submissions parent、primary hash、附件文件名/URL/hash、证据定位、包的内容角色及合同版本。仅接受同 accession/归属路径的实际链接，限制附件数和 bytes；不猜 EX-99.1，也不按模糊标题抓任意外链。40-F 的年度报表可能在附件；6-K 的财报、新闻稿、股东事项分别标记，无法分类的保留 disclosure/needs_review，不判季度完成。

第一包验收目标是 NTR 可以被正确安排和读取年报/相关披露，原文、来源、取证任务进入正常 reader；**不是**保证已有完整季度模型。这一包可以不改 statement 旧 SQL CHECK 和 companyfacts 数字授权规则。

### 第二包：有源证明的正式 foreign statement 接受

在离线、完整冻结的实际 40-F 与 financial 6-K 报告包上验证现有 edgartools 能否解析 `filing.xbrl()`，包括附件 Inline XBRL/instance 和三表识别。当前代码能调用该通用入口，但本次没有运行解析或读取 NTR 附件，不能声称库已支持。若原件不含机器可读财报，使用现有定向 reader/数字核验链；不把新闻稿表格自动升格完整三表，不静默引入新的 paid parser。

新增版本化 foreign statements observation/report-package proof，明确 actual taxonomy namespace、reporting standard（有证据才填）、原币种、完整 dimensions、statement role、单位、instant/duration start/end、来源文件/contexts、fact IDs/值、审计或未经审计及 furnished/filed 状态。不能按 form 推断 IFRS，不能将 CAD 改 USD，也不能对 IFRS tag 做同名 us-gaap 字符串映射。既有泛化 statement line 结构可复用；旧 companyfacts snapshot 和旧国内自动季度授权保持原合同。

正式数据进入同一 financial consumer 需要 owner-controlled SQLite migration：现有 dispatch/filing 表有国内 CHECK 和引用/不可变触发器，需完整表重建迁移、恢复 FK/索引/guard，逐字验证旧 rows/hash/IDs 与外键；不能另一进程写 Core。备选是 additive foreign authority 表及统一强 reader，但会扩大所有直接 SQL consumer 的接线，需另行审查，不能临时只让一处读到数据。推荐先列出引用表和全部读取点，再选择迁移；本报告不声称无需迁移。

旧 connector `financial_statements` 输出只接受 10-K/Q。新增输出形状/表单必须新 schema/normalizer identity 和新正式 governance binding；保留旧操作版本/审批可读，不改旧 schema 文件/hash 使旧审批隐式扩权。list_filings 与既有 public fetch 的 schema 无需因表单名称而升级；它们现有 host/issuer 权限仍要逐次核验。

### 第三包：季度、模型与反馈闭环

从真实 duration 与 fiscal calendar 识别季度、半年、9M、全年及比较期。6-K 的申报日/reportDate 和标题不能当 quarter end。只有同币种、同概念、同完整 dimensions、同财年、可验证来源组合才允许 YTD 差分；半年只有两季总数时仍保留 H1，不能平均拆两季。全年无 Q4 单列时，沿正式版本化 FY-minus-9M proof，不能把旧 10-K rule 原封套给 40-F。重述/修订不同来源组单独完整保留，不按数值挑有利来源。

IFRS 原 concepts 首先进入 source-bound typed 目录和完整 statement。模型/公式使用明确来源与角色；没有已有等价证明的概念、NCI、shares、收入成员或比较期保留精确 gaps。数字验证及所有历史/未来 DAG、EPS/cash gates 不放宽。完成年度原件但仍缺 interim 应在 planner 保留具体缺期和已持有可读的报告包；不能继续询问不存在的 10-Q。

## 必要测试与部署验收

- 路由：国内/40-F/20-F、年度制度转换、混合/无强 profile、错 CIK、过期原件、archive 续页；同请求重放/新环境行为一致。相关现有模块 `test_sec_plan_selection`、`test_sec_submissions_archive_pages`、`test_mission_source_discovery`。
- 原件/报告包：同 accession 明确附件、只有主封面、附件同名不同 hash、越目录/外站、无 XBRL、纯非财务 6-K、年度财报在 6-K、修订与报表重述；原文完整性和预算保持。相关 `test_acquired_sec_filings`、`test_sec_financials_cli`、`test_sec_financials_normalise`。
- 正式 authority：旧国内记录 golden/hash 不变、迁移幂等与 crash rollback、全部 FK/guard、错币种/单位/期间/dims 拒、旧 governance 不获得新 schema 权限。相关 `test_sec_financials_core`、`test_mission_statement_lane`、`test_statement_lane_configured_identity`、statement/context/coverage tests。
- 财务期间：H1 不变 Q、YTD 缺前期不推、非公历财年、重复/修订不同组不拼、IFRS 与 us-gaap 同名不自动等价；相关 `test_mission_sec_quarters` 和金融结构/forecast 全链。
- 先冻结原件离线正常 publish/read 与 full validation 验收，0 provider；上线后仅观察正常调度，分别记录 source acquired / parsed /正式 numeric / spec /forecast，不把索引命中或模型返回算研究答案。

建议先实现第一包及其明确能力反馈，再以实际原件解析结果确定第二包的合同/迁移。不能为了一个 NTR 直接放宽全局 form enum、篡改 domestic rule 或做单公司路径。

## 官方依据与边界

- [SEC Form 40-F](https://www.sec.gov/files/form40-f.pdf)：年报组成与 Form 6-K 的其他重要披露。
- [SEC Form 6-K](https://www.sec.gov/files/form6-k.pdf)：用途、附件及有限条件下的 Interactive Data 要求；表单并非统一季度财报。
- [SEC IFRS taxonomy notice 33-10320](https://www.sec.gov/file/33-10320)：IFRS taxonomy 的正式支持背景；不是本软件已具有 IFRS consumer 能力的证明。

本次没有测试、网络获取公司财报、改生产配置或运行解析。公司事实来自 C 的已冻结本地证据；外网仅核 SEC 官方表单说明。下一优先项仍是 v20 自然 source-discovery 阶段日志定位。
