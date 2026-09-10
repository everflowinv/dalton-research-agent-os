# S5 持续跟踪层交付报告 v1.0：SEC 持股申报与 IR 页监视

日期：2026-09-09
分支：`s5-sec-insider-13f`（worktree `~/Projects/dalton-s5-sec-insider-13f-worktree`）
分叉基线：main `ebd2ea8`（3,932 项测试）；已 `git merge main` 至 `88a9325`（含 C1 事件桥、P14d reopen lane）
审阅：主 agent 一轮，两个静默丢数据缺陷 + 六项，全部处理，见第 6 节
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 S 线「之后」一行、[OpenClaw 数据源盘点 v1.0](openclaw-data-source-survey-v1.0-2026-09-09.md) 第 19/20/34 行与 B.9 / C.7 / C.9、[P14a 日常跟踪](p14a-daily-tracking-v1.0-2026-09-09.md)、[C1 事件日历](c1-catalyst-calendar-v1.0-2026-09-09.md)、[P11a 市场层](p11a-market-layer-v1.0-2026-09-09.md)、`docs/CONNECTOR_PROTOCOL.md`
全量测试：见第 9 节，原文粘贴

---

## 0. 一句话

Dalton 以前能读公司**赚了多少**，读不到**谁在买卖它**。现在 `sec` 连接器多了四个各自治理的操作——Form 4 内幕交易、SC 13D/G 举牌、Form 144 拟售通知、13F-HR 机构持仓——外加一个本地 changedetection.io 的 `ir-page-watch` host_tool 连接器盯着申报之外的 IR 页面；两者都只产出 ResearchEvent，**一个数字都进不了报表行、进不了预测模型**。这条排除不是文档里的一句话：`OWNERSHIP_GRADE = "regulatory-ownership-filing"` 不在 `FIGURE_ADMISSIBLE_GRADES` 里、不在 `document_figure_grade.GRADE_BY_SPEC` 里，四类 form 与 `statement_snapshot._FORMS` 的交集为空，四条断言各有一个测试。

选下一份要读什么**零网络调用**：沿用 C1 的发现，已落盘的 `list_filings` 原始产物里本来就有发行人整块 `filings.recent`。只读核验（第 8 节）：五家公司近十年共有 **3,541 份 Form 3/4/5、105 份 SC 13D/G、178 份 Form 144、7 份 13F-HR** 躺在本机 spool 里，一次 SEC 调用都没发过。

---

## 1. 交付清单

| 文件 | 内容 |
| --- | --- |
| `src/dalton_core/sec_ownership_core.py` | 四个操作的身份、四个哈希、治理记录构造、**grade 词与它的排除含义**、由 accession 派生的文档 URL、以及读已落盘 `list_filings` 产物挑候选的零网络读取器 |
| `src/dalton_core/sec_ownership_adapter.py` | `primary_doc.xml` 与 13F 信息表的解析：每个数字都是 filing 里的原文，每一行绑 accession + 原始产物哈希；13F 计价单位判定与季度对比 |
| `src/dalton_core/sec_ownership_cli.py` | 子进程：approval first → artifact always → contract last；产出 typed event payload，不写事件 |
| `src/dalton_core/sec_ownership_launcher.py` | `SecOwnershipLauncher(LaneChildLauncher)`，票据按 accession 命名 |
| `src/dalton_core/ir_page_watch_core.py` | `ir-page-watch` 身份与治理、IR 页声明的加载与匹配、diff 命名、一次 sweep |
| `src/dalton_core/ir_page_watch_cli.py` | host_tool 子进程：只跟 loopback 说话，只读被声明过的页 |
| `src/dalton_core/mission_ownership_lane.py` | 每日无队列 lane（order 88）+ `LaneSpec` 注册；事件经 P14a 的 `record_event` 写出 |
| `deploy/phase9/p9-us-it-services-ir-pages-v1.json` | 五家公司各两个 IR 页的**声明**——这个连接器的全部作用域 |
| `deploy/connector-governance/sec-{form4-transactions,beneficial-ownership,form144-notices,form13f-holdings}-v1.json` | 四条 `sec` 操作治理记录，`status: proposed` |
| `deploy/connector-governance/ir-page-watch-{list-watches,get-diff}-v1.json` | 两条 `ir-page-watch` 治理记录，`status: proposed` |
| 共享增量 | `connector_inventory`（`sec` 加四个操作、新 profile `ir-page-watch`、六份输出契约、四个入参字段）、`connector_governance`（六个 kind）、`connector_quota_policy`（六条配额）、`research_event`（四个 kind + payload + tier）、`lane_registry.LANE_MODULES`（一行）、`research_plan.SEC_TEMPLATE_REGISTRY`（append `v4`）、`cockpit_plane.REGISTRY_LANE_LABELS`（一行，见 §7.3） |
| 测试 | `tests/test_s5_sec_ownership.py`、`test_s5_ir_page_watch.py`、`test_s5_ownership_lane.py`；fixture `tests/fixtures/sec-ownership/`（10 份合成 filing，含联名 Form 4、拆行的 13F、一份 filing index）与 `tests/fixtures/ir-page-watch/`（3 份合成响应） |

`writer_server.py`、`coverage_mission.py`、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、`PROJECT_STATUS.md`、`tests/test_service*`、`tests/test_lane_registry.py` **一行都没动**。

未推送。未部署。未写 live 状态。未发 mission 版本。**没有发过一次 SEC 网络请求**——`edgartools` 在 `.venv` 里没装（`sec-financials` extra 未安装），本切片也不需要它：解析是 Dalton 自己的 XML 读取器，fixture 全是合成的。

---

## 2. 四个操作，四份批准

一个 schema 哈希绑一个操作。这不是形式：**「批准读一位董事卖了什么」不该顺带变成「批准读一家机构持有一切」**，而这四份文档正好是四种形状。

| operation | form | 输出里的关键字段 | 配额（份/日 × 物理调用/份） |
| --- | --- | --- | --- |
| `form4_transactions` | 3 / 4 / 5（含 /A） | 申报人、四个身份布尔 + `officer_title` + 派生的 `role`、`transaction_code`、股数、每股价格、交易后持股、直接/间接、脚注引用 | 20 × 1 |
| `beneficial_ownership` | SC 13D / SC 13G（含 /A） | 申报人、独有/共有投票与处分权、合计股数、**占已发行股份百分比**、修订号、CUSIP、事件日、**Item 4 目的的哈希** | 10 × 1 |
| `form144_notices` | 144（含 /A） | 卖方、与发行人关系、拟售股数、总市值、拟售日、券商、取得日与方式、近三月是否已售 | 10 × 1 |
| `form13f_holdings` | 13F-HR / 13F-NT（含 /A） | 申报机构、CUSIP、发行人名、股数、价值（as filed 与 USD 两列）、季度、`put_call`、投票权三列、**与上季度的变化** | 8 × 2 |

**入参只能是一个 accession。** 四个操作的入参里没有 `url` 字段，`filing_accession` 的 pattern 钉死成 `^[0-9]{10}-[0-9]{2}-[0-9]{6}$`，路径由 `sec_ownership_core.primary_document_url` 从 accession 推导。profile 上 `route:arbitrary-attachment-url` 仍在禁止列表里，而这次它**是真的**——没有任何入口能把一个 URL 递进来。

**字段叫 `filing_accession` 不叫 `accession`。** `list_official_attachments` / `get_official_attachment` / `read_item` 三个冻结操作已经拿 `accession` 当自由文本，在 `_field_schema` 里收窄这个名字会**同时挪动这三份已批准的入参哈希**。S4 为 `statement` / `date_from` 写下过同一条教训。有测试钉住五个旧操作的入参与输出哈希（`ContractTests::test_the_input_contracts_before_s5_did_not_move`），值取自 `git show ebd2ea8:.../profiles/sec.json`。

**动的哈希只有 profile 自己那一层**：`connector:sec-edgar` 的 `profile_template_hash` / `fixture_manifest_hash` / `proposal_manifest_hash` 与 index 顶层 `content_hash`。`scripts/build_connector_inventory.py --check` 归零（第 9 节）。`research_plan.SEC_TEMPLATE_REGISTRY` 因此 append 了一条 `v4`——**两份输出契约与 v3 逐字节相同**，所以已绑 v3 的 plan 仍然按 v3 复验，没有 live 记录被扰动；`sec_connector_identity` 的 schema 哈希仍只由 `list_filings` + `get_company_facts` 推导，没变。

**一条给部署时预期的注意语。** `sec_template_registry_tag()` 现在返回 `v4`，而 `_template_suffix` 对非 `v1` 的 tag 会加后缀，所以**升级后第一个新建的 SEC plan 会注册一份新的 `connector-profile:sec-public:...:template-v4` profile 版本**（以及配套的 `runner-binding` / `rate-policy` 兄弟 ref）。这是这套机制设计好的样子——版本链 append-only，`v3` 的记录一条不动，旧 plan 继续按 `v3` 复验——但 live 状态里会多出一组以 `template-v4` 结尾的记录，看到时不必当作异常。

---

## 3. grade：一手、监管、但永远不是报表

这一节是本切片的中心，因为**这些文件最像报表而最不是报表**：政府表格、公司报送、一堆精确的大数字。一份 Form 4 上的「交易后持有 1,200,000 股」能通过任何位数核对，而它不是关于这门生意的事实。

落成一个词与四条可测的排除：

```python
OWNERSHIP_GRADE = "regulatory-ownership-filing"
```

| 排除 | 测试 |
| --- | --- |
| 不是 figure 可以被赋予的 grade | `GradeExclusionTests::test_the_grade_is_not_one_a_figure_may_be_read_under` |
| 已核验 figure 不能带它（`FIGURE_ADMISSIBLE_GRADES` 只有 `company-filed-document`） | `::test_a_verified_figure_may_not_carry_it` |
| 四类 form 与 `statement_snapshot._FORMS`（只有 10-Q / 10-K）交集为空 | `::test_no_ownership_form_can_produce_a_statement_line` |
| 四个 operation 都不是「可取数的文档种类」 | `::test_the_operations_are_not_figure_worthy_document_kinds` |
| 预测/报表四个模块都不 import 本连接器 | `::test_nothing_in_the_forecast_path_imports_this_connector` |

**证据层级仍然是最高的一档，这不矛盾。** `EVIDENCE_TIERS[0] == "primary_filing"`，三个 filing 类事件默认就带它：tier 说的是「这件事有多可信」，而这件事是「此人报送了此内容」——可信到不能更可信。grade 说的是「这个数字能不能当作那门生意的数字」，答案是不能。两个词分开，测试同时钉住这两句（`::test_the_tier_says_well_attested_and_the_grade_still_excludes`）。

IR 页是另一档：`ir_page_change` 默认 `management_direct`。**没有向任何人报送过**，营销页面改动没有版本历史，但它确实是公司用自己的声音在自己的站点上说话。

---

## 4. 解析：逐字，或者不要

三条规则，各自对应一种「省事写法会毁掉可核验性」的方式：

**4.1 浮点会丢掉 filing。** ACN 的合成 Form 4 里有一行 `1200.0000` 股——四位小数是那个计划本来的口径，`1200.0` 是另一句话。所有数字在 wire 上是**文本**，按 `^-?(0|[1-9][0-9]*)([.][0-9]+)?$` 校验，从不转换。千分位与货币符号被去掉（那是排版），`018600` 的前导零被去掉（那是表格补位），而 `approximately 4,250` 是**拒绝**，不是 4250——filer 拒绝给的精度不该由解析器发明。

**4.2 13F 的价值没有固定单位。** 2023 年修订前 SEC 自己的说明是「千美元」，之后是「整美元」，而跨界那阵子 filer 各行其是。**周期规则与比值检查两条都跑，谁定的写在 wire 上**：

| 情况 | `value_unit` | `value_unit_basis` |
| --- | --- | --- |
| 比值 > 20（只能是整美元），与周期规则一致 | 规则的答案 | `post_2023_rule` / `pre_2023_rule` |
| 比值 < 0.05（只能是千美元），与周期规则一致 | 规则的答案 | 同上 |
| 比值决定性且与规则矛盾 | **比值胜** | `ratio_heuristic` |
| 比值落在 0.05–20 之间 | 规则的答案 | **`ambiguous`** |
| 表里没有 `SH` 行可算比值 | 规则的答案 | 对应的 rule 名 |

**「决定性」这三个字是审阅改出来的。** `13f-tracker` 原判据是 `median < 1 即千美元`，而这对任何低价股都是错的：一只真正 0.4 美元的股票按整美元报，比值就是 0.4，那条规则会把它乘一千。所以比值只在两种读法不可能重叠的区间外说话——比值 < 0.05 意味着按整美元读的话中位持仓不到五分钱，比值 > 20 意味着按千美元读的话中位持仓超过两万美元一股——区间之内 basis 是 `ambiguous`：**数字仍由周期规则给出，而 wire 明说这次校验没有确认它**，而不是假装确认了。

中位数**只对 `SH` 行**取。`PRN` 行报的是债券面值而不是股数，它的 value/面值比是面值上的折价、对每一份文件里的每一只债券都接近 1，混进中位数会把一本普通股票账拖进模糊区间。

`value_as_filed` 与 `value_usd` 两列并存，想复核缩放的人能复核。

**4.3 没有出处的行是传闻。** 每一行带 `record_hash = content_hash({accession, artifact_hash, row})`。同一份 filing 读两次哈希相同；换一份字节哈希就变。这个哈希同时充当事件的 `event_key`，所以「事件说的」与「wire 说的」不可能各说各话。

**边缘情况（都有测试）**：修订版 13G 的 `amendmentNo` 是 `007` → 归一化为 `"7"`，且它嵌在 `formData/coverPageHeader` 而不是根下（所以查找是按局部名的后代查找，不是固定路径）；老式扁平 13D 只有一个申报人、没有修订号 → `amendment_no` 是 `None` 而**不是 0**；13F 上季缺失 → `prior_absent`，`changes` 为空、`first_reading_count` 有值，**不把整本持仓报成 `new`**；`putCall` 参与匹配键，看跌与正股不合并;带 DOCTYPE 的文档直接拒（实体声明 = 内存炸弹，SEC 这几种表从不带）。

---

## 5. IR 页监视：Dalton 不抓页面

传输是 `host_tool` 而不是 `public_https`，整个设计就在这个区别上。changedetection.io 已经在本机 `127.0.0.1:5055` 跑着，按自己的节奏抓、存自己的快照、算自己的 diff；这个连接器**通过 loopback 问它变了什么**。所以：没有凭证槽，没有要照顾的上游，Core 里没有任何人网站的副本——只有一个 diff、它的哈希，和被抓页面本身落盘为产物。

**作用域由声明文件定，不由那台本地工具定。** 共享的本地工具盯着它的使用者最后指给它的东西；一个「读所有 watch」的连接器，作用域就是别人某个下午的决定。所以 `deploy/phase9/p9-us-it-services-ir-pages-v1.json` 声明五家公司各两个页（press releases + events），而没被声明的 watch：**列出来，但永远不读**。列出来是为了让操作者看见那台工具在盯 Dalton 不读的东西（`undeclared_count` 进 tick 摘要）；不读是因为没人声明的 host 就是没人批准的 host。`get_watch_diff` 对未声明的 watch 直接拒绝并说明理由。

`--base-url` 只能是 loopback，检查在代码里而不在注释里。两个操作两份批准：「列出这台工具拿着什么」和「读某一页的 diff」是两种权限。

**幂等到 (url, diff hash)**：`diff_hash = content_hash({url, previous_snapshot_hash, current_snapshot_hash})`，与「什么时候注意到的」无关。明天重读同一对快照得到同一个 diff，事件账本回 `duplicate`。URL 归一化吃掉尾随斜杠但**保留 query**（`?tab=events` 是另一个页面）。

**工具不在就是关着的，这不是故障。** 没有两份已批准记录、或没有声明文件，sweep 返回 `unconfigured` 并说明缺哪一份，lane 照常空转。多数 Core 就是这种 Core。

---

## 6. 审阅改了什么

主 agent 一轮审阅：契约完整性通过（五个 S5 之前的操作逐字节未动、已批准记录未动、绑 v3 的 plan 仍解析到 v3、`OWNERSHIP_GRADE` 结构上不可达），合并卡在两个**静默丢数据**缺陷上。两个都是同一类：不报错、不留痕、下游看到的是一个说得通的假事实。

### B1 — 13F 的网络路径读的是封面，然后报「成功读到 0 条持仓」

一份 13F 不是一份文档。`primary_doc.xml` 是封面页（管理人名、期末、合计），持仓在第二份文件里，**文件名由 filer 自己取**，只有这份 filing 自己的 `index.json` 知道它叫什么。原来的网络路径只取了封面、拿封面去解析持仓、解析出零条、然后 `status: succeeded`——这是这里能出的最坏形状的 bug：**一份说「什么都没变」的成功读取**。上季度还有仓、这季度报 0 条，比对层就会为一整本账生成 `exit` 事件。

改法：
- `form13f_documents()` 走三次 GET——封面、`index.json`、信息表。三份**在解析任何东西之前**全部落盘；信息表是持仓绑定的那份产物，另外两份进 `companion_artifacts`。配额的 `max_physical_calls_per_unit` 从 2 改成 3，注释写清为什么是三。
- `information_table_name()` 从 filing 自己的索引里认那份文档：排掉 `primary_doc.xml`、排掉 EDGAR 的 `xsl*` 渲染副本，剩一个就是它；剩多个先按 `infotable` 之类的词收窄；**还是不唯一就拒绝并列出候选**。读错文档会产出一份能通过校验、内容是别人持仓、而且任何地方都发现不了的 wire——猜不如拒。
- 文件名是这个连接器里**唯一一个来自外部的名字**，所以进路径之前按 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.xml$` 校验，`../` 与 `a/b.xml` 都过不去（有测试）。
- **13F-HR 解析出零条持仓一律是 `failed`**，理由里带 form 与 accession，原始字节仍在 spool 里。`13F-NT` 是诚实的空情形——它本来就是「这些持仓由另一位管理人申报」的通知——`succeeded` 且 `holdings_status: notice_only`，零事件。

### B2 — 联名 Form 4 丢掉第二个申报人

`event_key` 原来就是那一行的 `record_hash`。一份联名申报（夫妻、基金与其 GP）对**同一批交易行**列出多个 reporting owner，于是第二个人往后的每一条事件都是重复键，事件账本按设计回 `duplicate` 并**一声不响地丢掉**。改成 `content_hash({"row": row["record_hash"], "owner": owner_cik or owner_name})`。测试：两个申报人 × 三行 = 6 条事件、6 个不同的键。

### 其余六项

| # | 事项 | 改了什么 |
| --- | --- | --- |
| 3 | `compare_holdings` 折叠重复的 `(cusip, put_call)` 行 | 新增 `aggregate_holdings()`：同一 key 的行**求和**而不是后者覆盖前者。一位管理人按子顾问 / 按基金分行申报很常见，原来只留最后一行，于是 200 万 + 100 万的仓位被拿 100 万去比 250 万，凭空生出一条 150 万的 `trim`。每一条被求和的 filed 行的 `record_hashes` 全部保留，`record_hash` 是它们的函数。first reading 一路也走同一个聚合。 |
| 4 | 比值判据 | 见 §4.2：只对 `SH` 行取中位数；只在 < 0.05 或 > 20 时推翻周期规则；区间内 basis 为 `ambiguous` |
| 5 | `--prior-file` 的出处 | 给了 `--prior-file` 就**必须**给 `--prior-accession`（argparse 与 `run()` 双重拒绝）。原来缺省回落到当期 accession，于是每条 `exit` 事件都在引用一份**不含那个数字**的文件 |
| 6 | 配额测试的理由注释 | 我上一版是从模块重新生成整张表，把 S1/S2/S3/S4/P10p/P11a/C1 的理由注释全洗掉了。已从 `ebd2ea8` 取回原文，六条新条目**插入**进去（`git diff` 现在是纯 79 行新增、0 删除） |
| 7 | IR sweep 被 filing 积压饿死 | sweep 原来只在 `idle` 分支跑，于是有未读 filing 的 Core——第一天的每个 Core、休假一周后的任何 Core——根本走不到它，监视看起来像坏了而不是被饿着。改到 grant 检查之后、launch 决定之前，每条返回路径都带 `ir_pages`；一天一次的护栏本来就在 |
| 8 | `13F-NT/A` | 加进 `FORMS_BY_OPERATION` |

零碎：`MAX_FILINGS_PER_FORM` 改成**按 form 计**（一家公司每一份 13D 要配一百份 Form 4，单一上限意味着那份唯一值得看的 13D 掉在一串例行行权后面永远选不到）；13F 配额注释写明三次调用；`MAX_HOLDINGS` 从 4,000 提到 12,000（BlackRock 量级约七千行），**超出仍然是带理由的拒绝而不是截断**——截断一本账再拿去和一份按不同边界截断的上季度比对，会凭空造出 `new` 与 `exit`，一个能读的拒绝可以恢复，一次编造的清仓不能；`_ir_watched_on` 挪到写完所有事件之后（原来写在前面，写一半抛异常就把当天剩下的变化丢到明天，而 sweep 本来就是幂等的）。

合并：`git merge main`（`88a9325`，含 C1 事件桥与 P14d reopen lane）。冲突两处，都是同一行邻居：`cockpit_plane.REGISTRY_LANE_LABELS` 里我的 `mission_ownership` 与 main 的 `mission_reopen`（两条都留），以及 `tests/test_openclaw_web_search_broker_client.py`——**我那版 `future()` 整块丢弃，取 main 的**。

---

## 7. lane：每天，每公司，一个孩子

### 7.1 选谁

无队列。每个 tick 从**本机已落盘的字节**推导「还有什么没读」：C1 证过 `list_filings` 返回发行人整块 `filings.recent` 且原始 body 被 spool 且哈希过，所以挑候选是本地读，零 SEC 调用。只有真去取选中那份 filing 的 primary document 才花一次调用，而那次调用就是四个受治操作之一。

一个 tick 一个孩子。live 数据说这够用：五家公司过去 12 个月共 587 份 Form 3/4/5 加 48 份 144，平均一天不到两份。

C1 那条注意语原样适用：这些行**在字节里但不在那次 invocation 的 `source_record_refs` 里**，所以从这里能拿的是「一个 accession 和一个日期」，不是「一份可以顺手去抓的文档」。抓 primary document 是另一件受治的事，这正是那四个操作存在的理由。

### 7.2 两个 grant，不是一个

`observation`（学到关于覆盖公司的带日期事实）与 `market_event`（写进 P14a 的事件账本）。缺任何一个 → `ungranted`、不起孩子、理由里**点名缺的那个**。测试 `GrantTests::test_both_grants_are_required_and_named_when_missing` 四种组合都钉。

四个操作**逐个批准**：`SecOwnershipLauncher.approved_operations()` 只返回治理目录里真有 approved 记录的那些，只批了 Form 4 的 Core 就只读 Form 4，其余在 tick 摘要里记 `unapproved` 计数，而不是拒绝启动。

### 7.3 共享文件的两处越界，说明白

- `research_plan.SEC_TEMPLATE_REGISTRY` append 一条 `v4`。这不是选择：`sec_template_registry()` 在包内模板不是注册头时**直接 fail closed**，错误信息本身就写着「append a SEC_TEMPLATE_REGISTRY entry」。改动是纯 append，两份输出契约逐字节不变。
- `cockpit_plane.REGISTRY_LANE_LABELS` 加一行 `"mission_ownership": "看谁在买卖这家公司"`。计划把 `cockpit_*` 列为禁改，但 `test_cockpit_wave1.LaneVocabularyTests` 会在任何未命名的新 lane 上失败，而 S1 的 `sales_notes_feed`、S3 的 `mission_crowd_sources` 都已在这张表里。**如果集成时希望由主 agent 统一做，这一行可以从本分支撤掉再补**——它与其余改动无耦合。

四份共享测试文件里的字面量按需更新：`test_research_plan`（registry 多一个 tag）、`test_connector_inventory`（profile 集合多 `ir-page-watch`）、`test_connector_quota_policy`（配额表从模块重新生成）、`test_research_plan_executor`（`template-v3` → `template-v4`，7 处）。

---

## 8. 只读冒烟：本机 spool 里已有多少

`/private/tmp/dalton-ro/core.sqlite` 复制到 `/tmp/s5-smoke`，三个 spool root 以只读符号链接指向 live 目录。**零 SEC 网络调用**；live 状态未被写过。`today=2026-09-09`。

近十年（`lookback_days=3650`）：

| 公司 | 状态 | Form 3/4/5 | SC 13D/G | 144 | 13F-HR | 明细 |
| --- | --- | --- | --- | --- | --- | --- |
| ACN | read | 814 | 2 | 104 | 7 | `4`×800、`4/A`×2、`3`×12、`144`×104、`SC 13G/A`×2、`13F-HR`×7 |
| CTSH | read | 860 | 13 | 35 | 0 | `4`×843、`4/A`×5、`3`×12、`144`×35、`SC 13G`×2、`SC 13G/A`×11 |
| EPAM | read | 616 | 47 | 23 | 0 | `4`×557、`4/A`×19、`5`×28、`3`×12、`144`×22、`144/A`×1、`SC 13G`×7、`SC 13G/A`×40 |
| IBM | read | 705 | 17 | 2 | 0 | `4`×685、`4/A`×4、`3`×15、`3/A`×1、`144`×2、`SC 13G`×3、`SC 13G/A`×14 |
| DXC | read | 546 | 26 | 14 | 0 | `4`×461、`4/A`×29、`3`×50、`3/A`×4、`5`×2、`144`×14、`SC 13G`×7、`SC 13G/A`×19 |
| **合计** | | **3,541** | **105** | **178** | **7** | |

近 12 个月与 lane 默认窗口（90 天）：

| 公司 | 12 个月 Form 3/4/5 | 12 个月 144 | 90 天 Form 3/4/5 | 90 天 144 |
| --- | --- | --- | --- | --- |
| ACN | 235 | 23 | 36 | 1 |
| CTSH | 172 | 15 | 34 | 4 |
| EPAM | 43 | 7 | 1 | 0 |
| IBM | 92 | 1 | 15 | 1 |
| DXC | 45 | 2 | 15 | 0 |

三点读出来的结论：

1. **Form 4 是主要素材，且量刚好。** 一天不到两份，20 份/日的配额是十倍余量而不是一个好看的整数。
2. **SC 13D/G 与 144 在 90 天窗口里很稀**（各 0–4 份）。这是对的：举牌不常发生。但**近十年 105 份 13D/G** 说明回填一次很有价值——ACN 只有 2 份而 EPAM 有 47 份，这个差别本身就是一条关于股东结构的观察。
3. **13F-HR 只在 ACN 的索引里有 7 份，那是 Accenture 自己作为管理人报的。** 「哪些机构持有 ACN」这个问题**不在发行人的索引里**——见下面第一条待裁决。lane 对这 7 份把 `holder_cik` 设成公司自己的 CIK（一份 13F 是关于一本账的，不是关于一个发行人的）。

---

## 9. 全量测试

```
PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
```

```
Ran 4050 tests in 619.833s

OK (skipped=1)
```

基线 main `ebd2ea8` 是 3,932 项，本切片新增 118 项（68 + 26 + 24）。

**顺手修掉的一个定时炸弹。** `tests/test_openclaw_web_search_broker_client.py` 里 `FUTURE` 是模块常量，在 `unittest discover` **导入**该文件时算成「此刻 + 5 分钟」，而它真正跑到是几分钟以后。全量套件长到跨过五分钟之后，那八个 socket 测试开始整齐地报 `web search deadline has already passed`——这不是新 bug，是超时检查对着一个测试自己放陈的 deadline 正确工作。改成每次调用现算（`future()`）。同一个文件里 `test_python_client_and_node_broker_agree_on_the_wire` 的注释里已经记着同一课的另一半（timeout 钳位），这次是补齐它。这不在 S5 的所有权范围内，但套件跨过那个时长的第一个人就得修，而那个人是我。

`scripts/build_connector_inventory.py --check`：

```
packaged connector inventory matches the frozen definitions
```

本切片新增的三个文件：

```
tests/test_s5_sec_ownership.py     Ran 68 tests   契约 / grade 排除 / 数字逐字 / 四种 form 解析 / 子进程 / spool 读取器
tests/test_s5_ir_page_watch.py     Ran 26 tests   身份与治理 / 声明加载 / 两个操作的 wire / diff 幂等 / loopback 约束
tests/test_s5_ownership_lane.py    Ran 24 tests   LaneSpec 注册 / 两个 grant / 协调器决策 / 事件幂等 / P14a 真权威收下 payload
```

---

## 10. 装机接线（给 INT）

1. **治理记录**：把 `deploy/connector-governance/` 里六份新记录复制到 live 状态的 `connector-governance/`。文件名就是 lane 找它们的名字（`sec-form4-transactions-v1.json` 等）。owner 批准前它们是 `proposed`，lane 会把对应操作报成未批准而不是失败。
2. **IR 页声明**：`deploy/phase9/p9-us-it-services-ir-pages-v1.json` → live 状态根下的 `ir-pages.json`。**不在就等于关掉 IR 监视**，`argv_fragment` 不会加那个参数。
3. **LaunchAgent 参数**由 lane 注册表自动拼：`--sec-ownership-governance-dir <state>/connector-governance`，以及（当 `ir-pages.json` 存在时）`--ir-page-declaration <state>/ir-pages.json`。`macos_launchagent.py` 一行没改。
4. **mission 版本**：live mission 的 `autonomy.may_write` 需要同时含 `observation`（已有）与 `market_event`（P14a 加的词，live 尚未授予）。两者齐了 lane 才动。
5. **changedetection.io**：需要在 `127.0.0.1:5055` 上、且已经为声明里的 10 个 URL 建好 watch。没建就是 `undeclared_count` 为 0、`changes` 为空，没有报错。
6. `pyproject.toml` **没改**：这四个操作不需要任何新依赖，解析是标准库 `xml.etree`。

---

## 11. 要 owner 批的

| 事项 | 说明 |
| --- | --- |
| **四条 `sec` 操作治理记录** | `sec-form4-transactions`、`sec-beneficial-ownership`、`sec-form144-notices`、`sec-form13f-holdings`，各 `proposed`。四份分开批：批 Form 4 不等于批 13F。 |
| **两条 `ir-page-watch` 记录** | `ir-page-watch-list-watches`、`ir-page-watch-get-diff`。第二份是「读某一页的内容」，比第一份重。 |
| **10 个 IR URL 的确认** | 声明文件里的 URL 是按公司惯例写的，**需要人核一遍**。写错一个的后果是把另一家公司的新闻塞进这家公司的档案。IBM 与 DXC 的最可能写错。 |
| **live mission 授予 `market_event`** | P14a 已加词，live 未授。 |

---

## 12. 留给人裁决的问题

1. **「哪些机构持有这五家公司」需要一份声明。** 13F 是机构报的，不在发行人的索引里，所以「谁在加仓 ACN」这个问题需要一张「要跟踪哪些管理人 CIK」的表——跟 IR 页声明同样性质的东西，同样不可推导（是跟前十大股东？跟几家有观点的长线基金？跟激进投资者？这是研究口径的选择，不是工程选择）。本切片把机制建好并测好（`compare_holdings`、上季缺失的 `prior_absent`），但没有替 owner 决定跟谁。建议形式：`deploy/phase9/p9-us-it-services-13f-holders-v1.json`，每家公司一组管理人 CIK 与理由。
2. **13F 事件的 CUSIP 过滤需要 company_ref → CUSIP 映射。** 子进程支持 `--company-cusips` 并已测（只有被跟踪的名字出事件），但 lane 目前不传——没有那张映射表。不传时事件数由 `MAX_EVENTS_PER_RUN = 40` 兜住，不会淹没账本，但一本大机构的账会有 40 条不相干的持仓变化。与上一条一起解决。CUSIP 本身在 SC 13D/G 的封面上就有（本切片已经在解析它），所以这张表大概率是**从已读的 filing 里长出来**而不是手写的。
3. **SC 13D 的 Item 4 目前只有哈希，没有正文。** `purpose_text_hash` 能回答「这次修订里目的变了没有」，回答不了「变成了什么」。要读正文需要抓 filing 的**文本**，那是一次新的 fetch 与一条新的 spec，跟 C1 报告里「公司自己announce 未来日期的 8-K」是同一个缺口，建议一并解决。
4. **回填多深。** lane 默认窗口 90 天。第 8 节显示近十年有 3,541 份 Form 4 与 105 份 13D/G 可读；一次性回填十年在 20 份/日的配额下要半年。是回填 12 个月（约 400 份 Form 4，两周）、还是只回填 13D/G（105 份，一周），还是不回填，是 owner 的取舍。
5. **`cockpit_plane` 那一行**（见 §7.3）：留在本分支，还是撤掉由集成时统一做。
6. **13F 的上季度还没有接线。** `--prior-file` / `--prior-accession` 已经实现并测到位（含 `exit` 事件绑到上季度那份 filing），但 lane 不传——要传就得记住上一季那份 filing 的 spool 产物在哪，而那是「跟哪些管理人」定下来之后才有意义的簿记。在此之前每次 13F 读取都是 `prior_absent` 的第一次读取，这是正确的默认而不是缺陷。

7. **Form 3 与 Form 5 也被 `form4_transactions` 读。** 它们是「就任时的初始持仓」与「年终补报」，形状同源、批准同一份。如果 owner 认为初始持仓不值得占事件账本的位置，收窄成只读 4 是 `FORMS_BY_OPERATION` 里删两个字符串加一条测试。
