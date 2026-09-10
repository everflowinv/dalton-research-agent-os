# W4 香港披露连接器交付报告 v1.0

日期：2026-09-10
分支：`w4-hkex-filings`（worktree `~/Projects/dalton-w4-hkex-filings-worktree`）
分叉基线：main `ba99ef9`
未推送。未部署。未写 live 状态。未发布 mission 版本。未改 `writer_server.py` / `coverage_mission.py` / `bounded_planner_driver.py` / `macos_launchagent.py` / `PROJECT_STATUS.md`。
按规则 9 触碰的共享文件：`lane_registry.LANE_MODULES`（一行）、`cockpit_plane.REGISTRY_LANE_LABELS`（一条中文名）、`scripts/rehearse_deploy.INSTALL_SEEDS`（四条）、`deploy/macos/install.sh`（一个播种块）。**没有新 schema，所以 `bootstrap.SCHEMA_DATABASES` 与 `rehearse_deploy.CORE_MIGRATIONS` 各零行**——本片与 S5 一样不建权威表，事件走 P14a 的 `record_event`。

---

## 0. 一句话

Dalton 现在能读香港了：一条 `hkex-filings` 连接器，四个操作、四个 schema 哈希、四条 `proposed` 治理记录、三个真实主机、一个「审批优先—产物必留—重放对题—契约最后」的子进程 CLI、一条注册齐全的 lane，和 2026-09-10 当天一次性抓下来的八份真实文件（腾讯 00700）。全球唯一的**每日**回购披露现在是一条可治理的管道；mission universe 仍是美股，所以这条 lane 在 live 上会报 `idle` 并说明理由——那是对的行为，不是缺口。

---

## 1. 提交清单

| commit | 内容 |
| --- | --- |
| `0b58000` | 连接器身份、四个操作契约与冻结输出 schema、四条治理记录与 kind 注册、配额、`SourceCapabilityMap` 条目、`buyback_disclosure` 事件类型与 `notes_text_hash` |
| `dbb6758` | 适配器（四个 reader + 派生上下文 + 事件构造）、子进程 CLI、launcher、lane 与四处登记、`[hk-filings]` extra、抓取脚本、八份真实 fixture、三个测试文件、共享测试加项 |
| （本文件） | 报告 v1.0 |

---

## 2. 端点：真的能到什么，以及有多脆

**全部结论来自 2026-09-10 从 owner 主机上的实际探测，不是文档。** 每一条都写进了 `hkex_filings_core.ENDPOINT_FRAGILITY`，理由和调用放在一起，而不是放在这份报告里等人来找。

### 2.1 `next_day_disclosure_returns` — 交易所自己的每日回购汇总

```
https://www3.hkexnews.hk/reports/sharerepur/documents/SRRPT{YYYYMMDD}.xls
```
200，`application/vnd.ms-excel`，约 45 KB，131 行 × 14 列，**每一格都是字符串**（交易所把数字格式化好再写进去）。

脆在哪：

- `www.hkexnews.hk` 与 `www1.hkexnews.hk` 的同名路径都 302 到一个小写路径然后 404；`www3.hkexnews.hk/reports/sharerepur/sbn.htm` 也 404。**只有 www3 的 `/reports/sharerepur/documents/` 这一种写法能拿到文件。**
- `.htm` 与 `.csv` 两种渲染都探过，都 404。**这份报表只有 workbook 一种形态**——这就是仓库多一个 `xlrd` 可选 extra 的全部原因。
- 只在交易日发布；周日返回 404，那是答案不是故障。
- 报表打印日 D 承载的是 **D-1** 的成交（这就是「翌日披露」）。适配器从表内的 `Date Printed` 读打印日、从行内读交易日；若拿请求日当交易日，每一笔回购都会晚记一天。**有测试钉住。**

为什么用它而不是发行人自己的 PDF：`Next Day Disclosure Return` 公告只有 PDF，而这份 workbook 是交易所对当天全部翌日披露报表的汇总，字段一一对应（已用腾讯 2026-09-09 的 PDF 逐格核对：交易日 09-09 的 231,000 股、HKD 438.2/432.8、HKD 100,431,292.5、授权以来 44,613,700 股 / 0.48929%，与 SRRPT20260910 应当一致；我们抓的 SRRPT20260909 载的是 09-08 的 230,000 股）。用结构化文本而不是版面 PDF，是「每个数字可核对」与「文本抽取猜出来的数字」的区别。

### 2.2 `announcements_index` / `monthly_returns` — HKEXnews 标题检索

```
https://www1.hkexnews.hk/search/prefix.do?callback=callback&lang=EN&type=A&name=00700&market=SEHK
https://www1.hkexnews.hk/search/titleSearchServlet.do?...&stockId=7609&t1code=-2&t2Gcode=-2&t2code=-2&from=20260801&to=20260910&...
```

**本连接器里最脆的一条，而且它是静默失败。** 用「看起来最像『不限分类』」的 `t1code=-1`：HTTP 200，`recordCnt: 0`，而同一窗口该发行人实际发了 24 份公告。真正表示「全部分类」的是 **`-2`**，`t1code` / `t2Gcode` / `t2code` 三个都要。第一版就是这样白跑了半小时。

防线是结构性的而不是事后检查：`title_search_url()` 只接受 `TIER_ONE_CODES`（`-2` / `10000` / `50000` / `51500`），`-1` 根本composed 不出来；profile 里 `route:hkexnews-title-search-tier-one-minus-one` 是 forbidden route。wire 上 `record_count` 与 `row_count` 并列，短列表和空列表因此可分辨。

其它脆点：

- 检索按内部 `stockId` 而不是股票代码，`prefix.do` 是唯一的公开转换路径，而且返回 **JSONP**，要先剥回调壳。前缀匹配到多于一个证券就拒绝，不取第一条。
- 人用的 `titlesearch.xhtml` 页面同样能用（同样要 `-2`），**故意不用**：它返回 HTML，带 JSF `ViewState` 和会话 cookie，比 JSON servlet 多三样会变的东西。记在 fragility 表里作为 servlet 被撤时的退路。
- 旧的 `www3.hkexnews.hk/listedco/listconews/advancedsearch/` 已经 302 回首页，没了。
- servlet 每页 100 行，`hasNextRow` 会说还有；本连接器不翻页，所以这两个操作的完备度是 `partial` 而不是 `enumerated`，并把 `has_next_row` 放在 wire 上。

### 2.3 `disclosure_of_interests` — 证监会 Part XV，走 HKEX 的 DI 数据库

```
https://di.hkex.com.hk/di/NSSrchCorpList.aspx?sa1=cl&scsd=01/01/2026&sced=10/09/2026&sc=00700&src=MAIN&lang=EN
https://di.hkex.com.hk/di/NSAllFormList.aspx?sa2=an&sid=6893&corpn=...&sd=...&ed=...
https://di.hkex.com.hk/di/NSForm3A.aspx?fn=DA20260819E00389&...
```

脆在哪，而且这一条比 `-1` 更危险：

- 同样按内部 `sid` 检索，**而 `NSAllFormList` 完全无视旁边的 `sc=00700`**：`sid=6` 会返回一整页格式完好的「电能实业」董事名单。一个猜错的 `sid` 给出的是别人公司的、下游任何一环都发现不了的答案。所以 `sid` 只从公司列表页自己的链接里读（00700 → 6893，腾讯），并且 `sid` 必须是正整数，字符串和布尔都拒。
- 日期是 `dd/mm/yyyy`，而 HKEXnews 是 `yyyymmdd`。同一个日期两种写法，是那种「12 号之前一直没事」的东西；两边各有测试。
- 没有 JSON，没有分页参数，列表页 100 行封顶；适配器再封 40 条，所以完备度 `partial`。
- 列表页给披露原因代码、涉及股数、均价、事件后持股与百分比；**事件前的持股只在该通知自己的表格页上**。所以每条通知多一次 GET，配额按「40 + 2」钉死。

---

## 3. 逐字与口径：两处必须说清楚的事

### 3.1 `cumulative_shares_ytd` 在港股行**恒为 null**，而这是刻意的

交易所那一列的准确名字是「本次回购授权（股东大会决议）以来在交易所购回的股份数」，以及「占决议当日已发行股份（不含库存股）的百分比」。腾讯这一轮的授权是 2026-05-13 通过的——**不是自然年迄今**。

`buyback_disclosure` 的字段表是与美股切片逐字商定的封闭集，`cumulative_shares_ytd` 这个名字在下游会被当成日历年读。所以：

- `cumulative_shares_ytd` = `None`，`remaining_authorisation` = `None`（授权上限只在发行人自己的 PDF 里，交易所汇总没有）。
- `pct_of_issued` **照登**交易所那一列——这个字段名声称的是分母不是期间。
- 数字一个都没丢：wire 上 `mandate_to_date_shares` / `mandate_to_date_pct_of_issued` 逐字保留，派生上下文里 `cumulative.basis` 写明「since the resolution granting the current repurchase mandate」，每行 `caliber_note` 也写了。
- **给美股切片 / owner 的提案**：如果两边都要在事件层带累计数，正确做法是把字段改名或加一个 `cumulative_basis`（`fiscal_ytd` / `since_mandate` / `since_program`）。这是跨切片的合同变更，本片不擅自做。

### 3.2 `average_price` 是算出来的，所以事件里是 null

交易所给最高价和最低价，不给均价。`aggregate_price_paid / shares_repurchased` = 436.708100（腾讯 2026-09-08）落在派生上下文里，公式写在旁边；事件 payload 的 `average_price` 留空。DI 通知那边相反——「每股平均价」是申报原文里就有的（`HKD 503.0000`），所以 `insider_transaction.price_per_share` 是逐字的，币种拆到 wire 上。

---

## 4. 事件与派生上下文

| 操作 | 事件 | 说明 |
| --- | --- | --- |
| `next_day_disclosure_returns` | `buyback_disclosure` | 每行一条；`market: "HK"`，`form: "Next Day Disclosure Return"`，`accession_or_ref: hkex:share-repurchase-report:{打印日}:{代码}:{交易日}` |
| `disclosure_of_interests` | `insider_transaction`（Form 3A/3B，董事与最高行政人员）/ `ownership_change`（Form 1/2/3，主要股东） | 分开而不是合并：owner 点名要的就是这个区分 |
| `monthly_returns` / `announcements_index` | 无 | 它们告诉 filings index「有这么一份文件」，不声称读过它 |

**HK 权益披露 → 既有 payload 字段的映射**（`insider_transaction`）：

| payload 字段 | 来源 |
| --- | --- |
| `accession` | 表格编号 `DA20260819E00389` |
| `form` | `Form 3A` / `Form 3B`（从表格页标题读） |
| `owner_cik` | **null**：香港没有 CIK，DI 数据库不发布任何个人标识符 |
| `transaction_code` / `transaction_meaning` | 证监会披露原因代码 + 已公布对照表；表外的代码原样保留、meaning 为 null（fixture 里有一个 `11031`） |
| `price_per_share` | 逐字均价（币种另立字段进 wire） |
| `acquired_disposed` | **推**出来的：事件前后持股比大小；相等即 null（1316「权益性质改变」正是这种） |
| `shares_owned_following` | 事件后长仓总数 |
| `direct_or_indirect` | 2101（实益拥有人）→ `D`，其余 capacity → `I` |
| `notes_text_hash` | 装不下的东西：capacity 代码与释义、股份类别、该类已发行股数、事件前后百分比、短仓、position marker。全文在 wire 的 `notes_text` 上，事件里只放哈希 |

**派生上下文（可重放，全部是同一份 wire 加 lane 递进来的历史的函数）**：

- 回购：`average_price_paid`（公式冻结）、`cluster_key`（ISO 周，coordinator 要的按周归并）、`pace`（对前 20 个已披露交易日的均值，本日排除）、`cumulative`（带 basis 与口径注）、`price_vs_current`。
- DI：`size_vs_holdings`（涉及股数 / 事件前持股、/ 事件后持股）、`trailing_90d`（通知数、涉及人、累计涉及股数、增减持笔数）。

**`cluster_key` 没有进事件 payload**，因为那个字段集是与美股切片逐字商定的封闭集，单方面加字段就不再是「商定」了。它在 `context` 里，lane 与判断层都拿得到。如果两边都要它进 payload，请一起加。

**香港价格：`unavailable`，理由已核。** yfinance 上游本身是支持 `.HK` 的（`0700.HK`），但 **Dalton 的价格权威拿不住**：`market_price._TICKER_RE` 是 `^[A-Z][A-Z0-9.\-]{0,15}$`，而港股符号以数字开头。所以「均价 vs 现价」这一格报 `unavailable` 并带理由，而不是拿一条不存在的序列去比。有一条测试断言这个正则现在仍然拒绝 `0700.HK`——**它开始失败的那天就是可以打开这个比较的那天**。CLI 留了 `--current-price` 入口，价格从外面递进来就立刻可比（有测试）。

---

## 5. 做了 / 没做

**做了**：

- `hkex-filings` 连接器：identity / 四个冻结输出契约 / 四个 schema 哈希 / adapter 哈希（绑本仓自己的 reader 与该操作允许的主机）/ 四条 `proposed` 治理记录 / `GOVERNANCE_KIND_REGISTRY` 注册 / 配额四条 / `SourceCapabilityMap` 一条。
- `scripts/build_connector_inventory.py --check` 归零；`index.json` 全程由脚本重生成。
- 适配器四个 reader、派生上下文、事件构造；子进程 CLI（审批优先 / 产物必留 / 时钟不入哈希 / 重放对题 / 契约最后 / 主机白名单在 fetch 处再查一遍）；launcher；lane 与四处登记；`install.sh` 四条一起播种。
- `buyback_disclosure` 事件类型（字段与美股切片逐字一致）；`notes_text_hash` 加到 `insider_transaction` 与 `ownership_change`。
- `company:hk-secucode:<code>.HK` ref scheme，附一条测试断言仓库里没有任何 mission 用了它。
- 八份 2026-09-10 真实 fixture + 抓取脚本（脚本里印出每一个 URL，每份 capture 记 URL / sha256 / 字节数）。
- 153 项新测试，全部离线。

**没做，以及为什么**：

1. **月报表的数字。** HKEXnews 只以 PDF 发布月报表，本操作只做索引（期间、提交时间、文件位置），契约里 `figures_available` 冻结为 `false`，所以将来真去读 PDF 的版本不会被误认成这一版。理由写在 `MONTHLY_RETURN_LIMIT_NOTE`：版面 PDF 用文本抽取读出来的数字，行列没人能核。
2. **发行人自己的 Next Day Disclosure Return PDF。** 同上；交易所的结构化汇总覆盖了同样的字段，且是交易所自己发布的。若要拿 `remaining_authorisation`（授权可购回总数），只有 PDF 里有——那是下一个操作、另一个 schema 哈希、另一条批准。
3. **翻页。** servlet 与 DI 列表都在 100 行分页，本片不跟；完备度诚实写 `partial`，`has_next_row` 上 wire。
4. **权威表。** 和 S5 一样不建：事件走 `record_event`，没有 `*_schema.sql`。
5. **mission universe 不动。** 见第 6 节。
6. **不用第三方转载。** 多家聚合站在几分钟内转载同一份日回购数据且更好读；`route:third-party-buyback-transcription` 是 forbidden route。owner 现有的 skill 已经有这条规则（最终来源只能是 hkexnews.hk 或公司 IR）。
7. **owner skill 的 web-search 回退没有搬过来。** `company-filings-alert` 在拿不到 HKEX 时会发一条 `site:hkexnews.hk ...` 的检索请求。这一片证明了官方端点从这台主机上是通的，所以把「检索到官方 URL 再抓官方文件」做成本连接器的退路会引入一条模型选路的路径，正是 CONNECTOR_PROTOCOL 禁止的。**建议**：这条退路留在 skill 侧，或者作为 `web-search` + `web-fetch` 两条已批连接器的普通用法，由判断层显式发起，而不是 `hkex-filings` 内部的静默 fallback。本连接器的失败是**带理由的拒绝**（`allowed_hosts`、HTTP 状态、`record_count`），不是换个地方问。

---

## 6. 集成时要接的线

1. **批准治理记录。** 四份在 `deploy/connector-governance/`，`status: proposed`，`approved_by: human:lumos`。四个 schema 哈希互不相同，批一个不会顺带批另一个。`install.sh` 已按 INT1 的「一条 lane 全批或全不批」把四份一起播种——播种本身是安全的：mission universe 全是 `company:sec-cik:*`，四条全批之后 lane 也只会报 `idle` 并说明理由。
   ```
   hkex-filings-next-day-disclosure-returns-v1.json
   hkex-filings-monthly-returns-v1.json
   hkex-filings-disclosure-of-interests-v1.json
   hkex-filings-announcements-index-v1.json
   ```
2. **mission universe 要不要收港股名字（owner 裁决）。** 这条 lane 只看 `company:hk-secucode:<code>.HK`。要真跑起来需要：(a) owner 决定把某个港股名字放进 coverage universe；(b) 该 mission 授 `observation` + `market_event` 两个 scope（两个都要，缺一报 `ungranted`）；(c) `coverage_mission` 的 universe 条目允许 `company:hk-secucode:` 前缀——本片没有碰 `coverage_mission.py`，请集成时确认它对 company_ref 的形状有没有约束。
3. **价格权威的 ticker 形状（集成裁决）。** 想要「回购均价 vs 现价」，得把 `market_price._TICKER_RE` 放宽到允许数字开头（`0700.HK`），并确认 `MarketPriceSeriesVersion` 的 currency 走 HKD、`mission_market_price_lane` 从 universe 取 `ticker` 时港股名字带的是 Yahoo 写法。`hkex_filings_core.yahoo_ticker()` 已经把 `00700` → `0700.HK` 的换算写好了。放宽之前，比较一律 `unavailable`。
4. **`[hk-filings]` extra。** `pip install -e '.[hk-filings]'`（xlrd）。**不装也不会崩**：CLI 在读 workbook 时带理由拒绝，测试全程不 import 它（fixture 存的是 canonical 文本网格；唯一打开 workbook 的那条测试在缺 xlrd 时 skip）。抓 fixture 时我在 `.venv` 里用到了 xlrd——它本来就在（akshare 的传递依赖），我没有安装或卸载任何东西。
5. **cockpit。** lane 中文名已加（`mission_hkex_filings` → 「看港股公司每天回购了多少、董事有没有增减持」），`EVENT_KIND_LABELS` 也补了四个此前缺名的事件类型（含 `buyback_disclosure` → 「公司回购自己的股票」）。公司卡若要显示港股公司，需要能渲染 `company:hk-secucode:` 前缀——本片没碰 cockpit 的公司卡。
6. **节奏（policy）。** 建议基线：每交易日 20:30 HKT 之后一次（照 owner 现有 cron；周一覆盖周末）。翌日披露报表在次日 08:30 前提交，所以那个钟点之后拉一次就够；DI 通知有 3 个营业日的申报期，所以按窗口回看（默认 14 天）而不是只问当天。这两条写在 `mission_hkex_lane.BUYBACK_CADENCE_NOTE` 与 `DEFAULT_DI_LOOKBACK_DAYS`，大脑可调。
7. **与美股回购切片对齐**：`buyback_disclosure` 的字段集本片是按你给的清单逐字实现的。两处需要一起裁的：`cumulative_shares_ytd` 的期间基准（第 3.1 节）、`cluster_key` 要不要进 payload（第 4 节）。
8. **全市场表缓存（未来）。** 每日回购报表是全市场一张表。覆盖五家港股公司时正确做法是每天读一次、过滤五次，而不是问五次。lane 目前是「每公司每操作每天一次」，五家会读五次同一份 workbook。配额（20/日）扛得住，但这是下一波该收掉的浪费——和 S4 对 `buybacks` 的结论一模一样。

---

## 7. 验收：测试

命令（worktree 根目录）：

```
PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
```

```
Ran 5534 tests in 516.644s
OK (skipped=1)
```

新增文件：

| 文件 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/test_hkex_filings_core.py` | 47 | 四个 schema 哈希互异且共用一个 source 哈希、adapter 哈希绑 reader 与主机、grade 不在任何 figure 路径上、ref scheme 往返且不认 `company:sec-cik:`、**仓库里没有 mission 用过港股 ref**、**价格权威仍拒绝 `0700.HK`**、SRRPT 只有 www3 一种写法、**`t1code=-1` 组不出来**、DI 的 `dd/mm/yyyy`、`sid` 必须是正整数、公告路径不可穿越、profile 主机集合 == 操作实际到达的主机、四条 forbidden route、无 fallback、只有日回购声称 `enumerated`、月报表契约冻结 `figures_available: false`、逐字字段都是字符串、`--check` 干净、四条治理记录与 builder 逐字节相同且都是 `proposed`、跨操作批准不通用、配额四条与 `MAX_DI_NOTICES + 2` 的乘积、fragility 表覆盖四条路由、`buyback_disclosure` 字段集与商定清单逐字相等、`PAYLOAD_FIELDS` 仍等于 `EVENT_KINDS`、`notes_text_hash` 只加在两个 DI 类型上、capability map 条目 |
| `tests/test_hkex_filings_adapter.py` | 63 | 每份 capture 带 URL/哈希/字节数、**workbook 网格 == 真 workbook（缺 xlrd 时 skip）**、交易日 = 打印日前一天、逐字数字与分隔符保留、币种独立成列、`universe_row_count`、没回购是答案不是失败、`711` == `00711`、缺失的最低价保持缺失、报表末尾的整段说明不是行、servlet 的 JSON-in-JSON、JSONP 剥壳、前缀匹配多于一个即拒、人民币柜台代码不丢、`record_count` 与 `row_count` 并列、月份从标题读、**空单元格不再被读成下一列**、**`63,494(L)0(S)` 是两个事实**、均价带币种、事件前后来自表格页、1316 不算买卖、未公布的原因代码不猜、未读表格页的行如实说、capacity → D/I、notes 全文与哈希、四个 wire 过冻结契约、同一 capture 两次同结果、跨操作 capture 拒收、**均价是冻结商、事件里为 null**、**价格比较 unavailable 且带理由**、pace 的窗口不含本日、cluster key、cumulative 带 basis、DI 的 size_vs_holdings 与 trailing 90 天、事件 payload 过 `validate_payload`、**`cumulative_shares_ytd` 恒 null**、event_key 跟行不跟运行 |
| `tests/test_hkex_filings_lane.py` | 43 | 未批准 → 无产物、跨操作记录拒收、schema 漂移拒收、四个操作端到端跑通、**重放与 argv 不符即拒（00700 的 capture 不能当 00001 用）**、跨操作 capture 拒收、窗口不符拒收、双模式互斥、**时钟不进产物哈希**、同 capture 两次同 invocation、**不同时刻同一 invocation**、**改一个数字换一个 invocation**、summary 列出每一份读过的文件、失败也写 summary、主机白名单在 fetch 处生效（`www.hkexnews.hk` 被拒）、launcher 逐操作开关、缺窗口即拒、命令带的是该操作自己的记录、**只有港股名字进这条 lane 的 universe**、**没有港股名字就 idle 并说明是 owner 决定**、两个 scope 都要、日回购最先读、四个操作轮完当天就 idle、未批准的操作不排期、失败不标记当天已读、连续失败转 held、读到的行成为派生上下文的历史、事件只记一次、没接账本时如实说、**lane 四处登记**、plist 只在播过种的机器上打开、installer 四条一起播种 |

修改的共享测试（只加不删）：`tests/test_connector_inventory.py`（profile 集合与 public 集合各加一条）、`tests/test_connector_quota_policy.py`（精确配额清单加四条）。

八份 fixture（`tests/fixtures/hkex-filings/`，共约 517 KB），全部是 2026-09-10 一次性、只读抓下来的真实调用：

| 文件 | 内容 |
| --- | --- |
| `next-day-disclosure-00700-20260909.json` | SRRPT20260909 的 canonical 文本网格（131 × 14），含 URL 与 workbook 的 sha256 |
| `share-buyback-report-20260909.xls` | 同一份 workbook 的原始字节（45,056 B），只被那条 skip-able 的测试打开 |
| `announcements-index-00700.json` | `prefix.do` + 全分类标题检索（2026-08-01 → 2026-09-10，24 条） |
| `monthly-returns-00700.json` | `prefix.do` + `t1code=51500`（1 条：2026 年 8 月月报表） |
| `disclosure-of-interests-00700.json` | DI 公司列表 + 全通知列表（2026-01-01 → 2026-09-10，21 条）+ 前 7 条通知各自的 Form 3A 表格页 |

抓到的真实数字（可核对）：腾讯 2026-09-08 回购 230,000 股，最高 HKD 439.60、最低 HKD 435.20，代价 HKD 100,442,863.00，本次授权以来累计 44,382,700 股 / 0.48676%；均价（算出来的）436.708100。杨绍信 2026-04-10 的 Form 3A（`DA20260415E00531`）：涉及 5,000 股，均价 HKD 503.0000，事件前 68,494 股、事件后 63,494 股，capacity 2101。腾讯在 DI 数据库里的 `sid` 是 6893；该类已发行股份 9,103,122,790。

---

## 8. 开放问题（要 owner 或集成裁决）

1. **`cumulative_shares_ytd` 的期间基准。** 见 3.1。港股这一格现在是 null。要么给 `buyback_disclosure` 加一个 `cumulative_basis`，要么把字段改成不声称期间的名字。**这是跨切片合同变更，本片不擅自做。**
2. **`cluster_key` 要不要进 payload。** coordinator 要求按周归并，本片放在 `context` 里而不是 payload 里，理由同上。
3. **mission universe 收不收港股名字。** 不收的话这条 lane 永远 `idle`（正确但无产出）。收的话见第 6 节第 2 条的三件事。
4. **港股价格。** `market_price._TICKER_RE` 要不要放宽到允许数字开头。放宽之前，「均价 vs 现价」「回购占日成交比」这类比较都做不了。
5. **证据层级。** 本连接器的行是**交易所与证监会自己发布的**（不是 vendor 归一化），所以 `HKEX_EVIDENCE_TIER = primary_filing`。但 `HKEX_GRADE` 明确把它挡在所有 figure 路径之外——它们是权益与库存披露，不是经营数字。建议 owner 确认这个区分：可作为 Claim 的一手来源（谁买了谁卖了），不可作为财务数字进 Ledger。
6. **全市场表读一次过滤多次**（第 6 节第 8 条），要不要现在做。
7. **月报表的数字要不要读。** 需要一个 PDF 读取路径（仓库已有可选的 `pdf` extra 与 `pypdf`），但版面 PDF 的行列可靠性是另一件事。建议等有第二家港股公司进 coverage 之后再评估。
