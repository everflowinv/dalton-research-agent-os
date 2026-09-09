# S4 中国 / 香港基本面连接器交付报告 v1.0

日期：2026-09-09
分支：`s4-cn-hk-findata`（worktree `~/Projects/dalton-s4-cn-hk-findata-worktree`）
分叉基线：main `61f4255`（2,627 项测试）
全量测试：`Ran 2709 tests in 365.576s` / `OK (skipped=1)`
未推送。未部署。未写 live 状态。未发布 mission 版本。未改 `writer_server.py` / `coverage_mission.py` / `bounded_planner_driver.py` / `macos_launchagent.py` / `install.sh` / `cockpit_*` / `PROJECT_STATUS.md` / `tests/test_service.py` / `tests/test_lane_registry.py`。

---

## 0. 一句话

Dalton 现在能读中国公司了：一条 `cn-hk-findata` 连接器，六个操作、六个 schema 哈希、六条 `proposed` 治理记录、六个真实主机、一个「审批优先—产物必留—契约最后」的子进程 CLI，和八份 2026-09-09 当天一次性抓下来的真实 fixture（茅台 + 腾讯）。没有 lane——mission universe 还是美股——但接一条 lane 需要什么，第 5 节写清楚了。

---

## 1. 提交清单

| commit | 内容 |
| --- | --- |
| `fd7b325` | 连接器身份、六个操作契约、六条治理记录、配额、共享测试的加项 |
| `3f3fe09` | 适配器（canonical 化 + 六个 wire）、子进程 CLI、`[cn-hk-data]` extra、八份真实 fixture、两个测试文件 |
| （本条） | 报告 v1.0 |

---

## 2. 连接器 `cn-hk-findata`

- `connector_ref: connector:cn-hk-findata`，`source_ref: source:cn-hk-findata`，`source_type: market_data`。
- transport `public_https`，target `transport:public-http:0.1`，`host_policy: literal_allowlist`，`auth: none`（无凭证槽，`credential_material: forbidden`）。
- `ADAPTER_LIBRARY = "akshare"`，`ADAPTER_LIBRARY_VERSION = "1.18.94"`——即 OpenClaw skill 在跑的那一版。版本进 adapter 哈希，换版本就是换 capability。
- gate `recorded_public_reference_shadow`，readiness `inventory_connected`，`lease_eligible=false`、`live_execution_allowed=false`。

**故意没有搬过来的东西**：skill 的自然语言 router。它在提问时从 87 个候选里挑接口，挑不中就按函数名相似度再挑一次并把结果标成 `degraded=true`——问行业板块返回分红表就是这么来的。CONNECTOR_PROTOCOL 已经写死了理由（Planner 每个 WorkOrder 只做一次语义选择并冻结，Runner 不在每次物理调用重跑模型或语义 Router），所以这里每个操作只绑一个函数、一个主机、一个厂商，调用前就定好。`route:cn-hk-findata-nl-router` 是 profile 里的 forbidden route。

### 2.1 六个操作

| 操作 | akshare 函数 | 主机 | 厂商 | 完备度上限 | 入参 |
| --- | --- | --- | --- | --- | --- |
| `financial_statements` | `stock_profit_sheet_by_report_em` / `stock_balance_sheet_by_report_em` / `stock_cash_flow_sheet_by_report_em`（A）；`stock_financial_hk_report_em`（HK） | `emweb.securities.eastmoney.com`（A）、`datacenter.eastmoney.com`（HK） | eastmoney | `partial` | `market`(a\|hk)、`ticker`、`statement_kind`(income\|balance\|cash)、`period_type`(report\|annual) |
| `shareholders` | `stock_gdfx_free_top_10_em` + `stock_zh_a_gdhs_detail_em` | `emweb.securities.eastmoney.com`、`datacenter-web.eastmoney.com` | eastmoney | `partial` | `a_ticker`(六位)、`period_end` |
| `buybacks` | `stock_repurchase_em` | `datacenter-web.eastmoney.com` | eastmoney | `partial` | `a_ticker` |
| `margin_balance` | `stock_margin_sse` / `stock_margin_szse` | `query.sse.com.cn`、`www.szse.cn` | sse / szse | **`enumerated`** | `exchange`(sse\|szse)、`start`、`end` |
| `northbound_flow` | `stock_hsgt_fund_flow_summary_em` | `datacenter-web.eastmoney.com` | eastmoney | `partial` | `as_of` |
| `ah_premium` | `stock_zh_ah_spot_em` | `push2.eastmoney.com` | eastmoney | `partial` | `ticker` |

只有 `margin_balance` 声明 `enumerated`：两所都按交易日发布完整的融资融券汇总，在一个有界窗口里可以逐日对账。其余五个是 vendor 的归一化或快照，没有 ID / revision chain 可对，所以是 `partial`——这不是谦虚，是 CONNECTOR_PROTOCOL 对 `enumerated` 的定义。

**入参字段名为什么这么长**：`statement_kind`、`a_ticker` 而不是 `statement`、`ticker`。`statement` 已经属于冻结的 `sec-financials` 契约、`date_from` 属于 `cninfo`，在共享的 `_field_schema` 里收紧它们会静默移动别人的批准。`a_ticker` 用 `^[0-9]{6}$` 把「这个操作上游根本没有港股路由」写进契约本身，而不是让一次运行去发现。

### 2.2 主机与被拒绝的路由

allowlist 是六个操作真正打到的地方的并集，一个不多：

```
datacenter-web.eastmoney.com   datacenter.eastmoney.com
emweb.securities.eastmoney.com push2.eastmoney.com
query.sse.com.cn               www.szse.cn
```

有一条测试断言 allowlist 里没有任何操作用不到的主机，也没有任何操作打 allowlist 之外的地方。不在名单上的就是 forbidden route。

`forbidden_target_refs`：`route:cn-hk-findata-nl-router`、`route:eastmoney-push2his-batch-probe`、`route:tencent-ah-quote-list`、`route:arbitrary-attachment-url`。

**`stock.gtimg.cn` 不在 allowlist 里，是刻意的。** 腾讯的 A+H 列表（`stock_zh_ah_spot`，也是 skill registry 里 `ah_spot` 绑的那个函数）在 akshare 1.18.94 里返回的列是 `代码/名称/最新价/涨跌幅/…`——**只有 H 股报价，没有比价，也没有溢价**。用它回答 `ah_premium` 会对一个没被回答的问题给出一个自信的数字。（附带一条：akshare 打它走的是明文 `http://`，不是 https。）所以 AH 溢价走的是东财 `push2.eastmoney.com` 的 `stock_zh_ah_spot_em`，它确实算比价和溢价。这条「考虑过、拒绝了、理由在此」记在 `REFUSED_VENDOR_ROUTES` 里，适配器在遇到 `vendor=tencent` 的 capture 时会把这段理由原样抛出来。

### 2.3 「不要批量探测东财」怎么落地

skill 的记录：2026-08-21 连续压 `push2his` 十几次之后，原本正常的 `fflow/daykline` 也被断开，安静数分钟。六个操作里只有 `ah_premium` 必须碰这个集群。落实为三件事：

1. 配额：`ah_premium` 每日 4 个单位 × 每单位 3 次物理调用。有一条测试断言它是六个里最小的。
2. `_call` 不重试。上游任何异常直接变成 `CnHkFinDataVendorRefusal`——连接被拒/重置/502/403 对这些主机不是瞬时错误。
3. `NO_BATCH_PROBE_HOSTS` 在身份模块里列出 `push2` / `push2his` / `33.push2`，测试钉住 `ah_premium` 的主机在这个集合里。

### 2.4 `fallback_used` / `source_vendor` / `caliber_note`

**每一行都带这三个字段**，不管是哪个操作。`source_vendor` 在冻结的输出契约里是**枚举**，枚举值正好是该操作的批准覆盖的厂商——所以一行来自没人批准的厂商，连 schema 都过不去，拒绝是契约的而不是适配器的客气。

调查结论（值得单独说）：**skill 声明的 12 条 vendor 回退，没有一条是给这六个操作的。** 它们全在行情 / 板块 / 资金流那一侧。基本面这条路每个操作只有一个厂商。所以：

- 没有任何操作声明 permitted fallback（`fallback_routes: []`，有测试）。
- skill 的 health/demotion 行为在这里复现为**带理由的拒绝**，而不是静默换源：capture 上 `fallback_used=true` → `CnHkFinDataVendorRefusal("…no fallback vendor is approved for it. The declared vendor was not reached, so there is no answer -- not a different answer.")`。
- CLI 把这类失败标成 `refusal_kind: "vendor_unavailable_no_substitute"`，与其它所有失败分开。lane 读到这个应该等，不应该换个地方问。
- 字段仍然留着，因为哪天批了一个回退，标签必须跟着数字走而不是跟着这次运行走。

`caliber_note` 承载的是「就算没回退也必须告诉读者的口径」，见下一节。

### 2.5 抓取当天发现的两个口径陷阱

这两条不是设计推演，是 2026-09-09 抓下来之后对着数字发现的，现在写在每一行上。

**(1) 两所的融资融券单位差一亿倍。** 上交所 `融资融券余额` 返回 `1,350,016,680,402`（元），深交所返回 `12,847.58`（亿元）。融券余量同理：沪市 `3,098,060,345`（股），深市 `11.79`（亿股）。两所都不在 payload 里标单位。契约因此给每行加了 `amount_unit` 与 `volume_unit`，`caliber_note` 写「不可直接相加」。另外沪市把那一列叫「融券余量金额」，深市叫「融券余额」——同一个量，两个名字，归一到 `short_balance_amount`，各自的原名留在口径注里。
**开放问题**：深市的单位是从量级读出来的（12,847.58 对 1.35e12），不是从上游的单位声明读出来的。请 owner 或后续 canary 用交易所页面确认一次。

**(2) 港股三表没有币种，也没有会计准则，而且不能从旁边那张表借。** `stock_financial_hk_report_em` 返回的列只有 `SECUCODE / REPORT_DATE / DATE_TYPE_CODE / FISCAL_YEAR / START_DATE / STD_ITEM_CODE / STD_ITEM_NAME / AMOUNT`。同一主机上的主要指标表（`stock_financial_hk_analysis_indicator_em`）确实有 `CURRENCY`，看起来正好补上这个洞——但实测腾讯 FY2025：三表的「营业额」是 `743,689,000,000`，主要指标表的 `OPERATE_INCOME` 是 `751,766,000,000`，`CURRENCY=HKD`、`IS_CNY_CODE=0`。差 1.09%，不是汇率。两张表不是同一次测量，一张的标签不描述另一张。**所以这个调用已经从适配器里删掉了**（省一次物理调用），港股行的 `currency` 与 `account_standard` 一律为 null，`caliber_note` 说明原因。一个错的币种比一个空的币种更糟：空的会让读者停下来，错的不会。

A 股这边相反：`CURRENCY` 就在三表里（`CNY`），`account_standard` 由适配器填 `中国企业会计准则`——不是猜，是这条路由的事实；留空会在 A 行和 H 行并排时被读成「和另一行一样」。

### 2.6 其它归一化决定（都有测试钉住）

- **A 股利润表/现金流的 `period_start` 是推出来的**：上游没有 `START_DATE` 这一列。这条路由是「报告期」，中国的中期报表一律从财年年初累计，财年即日历年，所以 `period_start = {year}-01-01`；资产负债表是时点，留 null。没有这一格，2026 中报的累计数和单季数只有同一个 `period_end`，就是一个数字出现两次。
- **`*_YOY` / `*_QOQ` 列不进 lines**：那是 vendor 算的百分比，不是公司报的行。把它放在可加总的数字旁边，早晚会有人加总它。
- **港股 `fiscal_year` 归一为四位年份**：上游的 `FISCAL_YEAR` 是 `"12-31"`（财年**结束日**）。放在一个叫 fiscal_year 的字段里会被当日期排序。
- **港股 `report_type` 带前缀 `date_type_code:`**：上游只给 `"001"` 且不发布对照表，所以它以「一个没人核验过的厂商代码」的样子出现，而不是一个标签。
- **`northbound_flow` 的金额单位是 akshare 的，不是东财的**：库把原值除以 10,000 再返回，`amount_unit: 亿元` 写在每行，口径注点名这次除法。这也是把库版本钉死的原因之一。
- **`northbound_flow` 校验快照日期**：接口不收日期参数，返回它手上那一天。返回的 `交易日` 与请求的 `as_of` 不符 → 拒绝。把陈旧快照记在请求日名下，等于把一个数字标到它并不描述的那个交易日。
- **`buybacks` / `ah_premium` 带 `universe_row_count`**：上游只有全市场表，没有按代码检索的接口。不说全表有多少行，「这家公司没有回购」和「表回来得短了」就是同一个空答案。
- **`MAX_PERIODS = 20`**（五年季度）、`MAX_HOLDER_COUNT_ROWS = 200`：超出的期数计入 `dropped_row_count`，不是静默丢掉。茅台的利润表上游有 103 个报告期（1998 年起）。

---

## 3. 配额

`connector_quota_policy.py`，重置时区 `Asia/Shanghai`，窗口 86,400 秒：

| 操作 | 单位 | 每日单位上限 | 每单位物理调用上限 | 理由 |
| --- | --- | --- | --- | --- |
| `financial_statements` | document | 20 | 12 | 一份报表史 = 一次报告期列举 + 每 5 期一次取数；一季度才变一次 |
| `shareholders` | document | 20 | 6 | 十大流通股东一张表 + 户数史（500 行一页） |
| `buybacks` | document | **4** | 40 | 六个里最贵：只有全市场表，为了一家公司要读完所有公司的答案再扔掉其余 |
| `margin_balance` | search | 40 | 1 | 一个交易所一天一个单位，够两所加回补 |
| `northbound_flow` | search | 8 | 1 | 一天动一次的数字 |
| `ah_premium` | search | **4** | 3 | 必须碰东财行情集群的那一个，见 2.3 |

---

## 4. 子进程 CLI

`src/dalton_core/cn_hk_findata_cli.py`（`dalton-cn-hk-findata`），形状照 `market_price_cli.py`：

- **审批优先**：治理记录必须 `approved`，`capability_id` 必须是这个操作的，`expected_source_hash` / `expected_schema_hash` 必须仍然描述打包契约。任何一条不满足，在读任何东西之前就退出，`artifact` 为 null。一个操作的记录跑不了另一个操作（有测试）。
- **产物必留**：整份 capture 先 canonical 化（列序保留、每格转文本、无 float）、再 `canonical_json` → sha256 → `RawSpool`。归一化失败的运行照样留产物（有测试）：读不出来的那份正是有人要看的那份。
- **契约最后**：wire 先过冻结的 output schema（`authority_resolver._schema_matches`），过不了就一个字节都不落盘（有测试）。
- **`summary.json` 永远写**，成功失败都写，含 `refusal_kind`、`source_vendor`、`fallback_used`、`caliber_notes[]`、`allowed_hosts`、`invocation_ref`、`artifact`。
- 模式二选一：`--allow-network` / `--fixture-file`；`run()` 自己也检查，不依赖 argparse（直接构造 Namespace 的调用方不该因为漏个 flag 就把请求发到东财）。
- **没有权威消费这些行**（见第 5 节），所以校验过的 wire 写在 summary 旁边（`wire-{operation}.json`，0600）。为了放它而现造半个权威，比说清楚「还没有权威」更糟。

---

## 5. 一条 lane 需要什么（本波不做）

mission universe 是美股，所以本波**没有 lane**，`lane_registry` 一行都没加。一条 statements 式的 lane 要接的东西：

```python
LANE = register_lane(LaneSpec(
    operation="dispatch_mission_cn_hk_fundamentals",
    order=82,                                   # statements(80) 之后、market price(85) 之前
    driver_key="mission_cn_hk_fundamentals",
    handler=dispatch,                           # coordinator 缓存在 server.lane_state
    init_kwarg="cn_hk_findata_launcher",
    argparse=add_arguments,                     # --cn-hk-findata-governance-dir
    launcher_factory=build_launcher,            # CnHkFinDataLauncher(state_dir, governance_dir)
    argv_fragment=argv_fragment,                # 治理目录存在才输出
    note="S4: one China or Hong Kong company's statements, holders, buybacks "
         "and the flows around them, from a vendor rather than from a filing.",
))
```

与 P11a 的价格 lane 相比，有五处不同，都是这条 lane 特有的：

1. **开关是六个治理文件，不是一个。** `build_launcher` 应当逐操作检查 `{state}/connector-governance/cn-hk-findata-{op}-v1.json` 是否存在且 `approved`，只装被批准的那几个操作；一个没批就整条 lane 不装，会让 owner 只能全批或全不批。
2. **coordinator 必须缓存在 `server.lane_state`。** 它持有的进程内状态是全部意义：哪家公司的哪个操作已经问过、哪个厂商刚拒绝过（拒绝后该操作在本进程内退避，这是 skill 健康记账的等价物，但按 (操作, 主机) 而不是按连接器整体——skill 花了四个月才学到这一点：`akshare: consecutive_failures 29` 那条记录是行情集群坏了把整个 akshare 判死，而龙虎榜和股东户数一直是好的）。
3. **节奏不是「每 tick 一家」。** 报表一个季度变一次，融资融券一天一次，AH 溢价盘中一直在动但配额只有 4。建议：`financial_statements` / `shareholders` / `buybacks` 按公司 × 季度触发（上次 `period_end` 与最新可得报告期不同才跑），`margin_balance` / `northbound_flow` 每交易日一次，`ah_premium` 每日一次。
4. **`may_write` 需要一个新 scope**（比如 `cn_hk_fundamentals`），并且要有一个 append-only 权威接住这些行。最省事的形状是复用 `statement_snapshot.py` 的样子：每期一个版本、`content_hash`、三触发器、每行绑 `invocation_ref` + `artifact_hash`。**但有一条硬约束**：这些数字是 vendor 的归一化，不是申报原文。它们的证据层级不能是 `filing`，`ValuationSnapshot` 的 `ALLOWED_FUNDAMENTAL_SOURCES` 也不应该加 `source:cn-hk-findata`——一家中国公司的一手数字在巨潮公告里，`cninfo` 连接器已经能到。这条连接器的用处是「快、宽、可对照」，不是「可入账」。
5. **mission universe 需要中国 / 香港名字**，以及一个 `market` 维度（现在 `coverage_mission` 的公司都是 `company:sec-cik:*`；A 股与港股需要另一种 company ref，例如 `company:cn-secucode:600519.SH` / `company:hk-secucode:00700.HK`）。这一条不在我的所有权范围内，是集成时要裁的。

其它接线：

- **`deploy/macos/install.sh`**（禁改，集成时补）：照 `roic-*` 的循环写法把六个 kind 种进 `{state}/connector-governance/`，全部 `proposed`。owner 就地改 `approved` 的那几个才会跑。
- **`.venv` 需要 akshare**：`pip install -e '.[cn-hk-data]'`。**注意仓库 `.venv` 是主 checkout 的符号链接**；我在其中装过 akshare 1.18.94 抓 fixture，抓完已经卸载，主 checkout 的环境与我接手时相同。测试全部离线，不 import akshare（有一条测试断言 `akshare` 不在 `sys.modules`）。
- **cockpit**：公司卡需要能显示 `source_vendor` 与 `caliber_note`；`refusal_kind: vendor_unavailable_no_substitute` 应当在 lane 状态面板上与普通失败区分开，否则「等」和「重试」看起来一样。

---

## 6. owner 要做的事

1. **批准治理记录。** 六份都在 `deploy/connector-governance/`，`status: proposed`，`approved_by: human:lumos`。改成 `approved` 之前，CLI 在碰网络前就拒绝。
   ```
   cn-hk-findata-financial-statements-v1.json
   cn-hk-findata-shareholders-v1.json
   cn-hk-findata-buybacks-v1.json
   cn-hk-findata-margin-balance-v1.json
   cn-hk-findata-northbound-flow-v1.json
   cn-hk-findata-ah-premium-v1.json
   ```
   可以分开批。六个 schema 哈希互不相同，批一个不会顺带批另一个。
2. **确认深交所融资融券的单位**（见 2.5(1)）：`亿元` / `亿股` 是从量级读出来的。
3. **裁决证据层级**：本连接器的行是 vendor 归一化（东财）或交易所汇总（沪深两所）。建议：交易所那两个（`margin_balance`）算「一手（交易所）」，东财那四个算「vendor」，都不得作为 filing 级数字进 Ledger。
4. **裁决 source 粒度**（见第 8 节的第一条开放问题）。

---

## 7. 验收：测试

```
Ran 2709 tests in 365.576s
OK (skipped=1)
```

命令：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`（worktree 根目录）。基线 main `61f4255` 是 2,627；本片新增 82 项。

新增文件：

| 文件 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/test_cn_hk_findata_core.py` | 29 | 六个 schema 哈希互异、共享一个 source 哈希、adapter 哈希绑库与版本、主机 allowlist 与操作一一对应、腾讯主机不可达、forbidden route、无 permitted fallback、契约里的厂商枚举等于身份里的、每行都有三个 provenance 字段、配额存在且行情集群那条最小、六份治理记录与 builder 逐字节相同且都是 `proposed`、`build_connector_inventory.py --check` 干净、**cninfo 三个哈希未动（钉死）** |
| `tests/test_cn_hk_findata_adapter.py` | 36 | canonical 化决定性（同一帧两次同哈希）、列序保留、重名列拒收、float→十进制文本、`1e15` 不出指数形式、缺失即 null、六个操作各自的真实 capture 过冻结契约、每行不是 float、wire 构建两次同字节、跨操作 capture 拒收、**未批准厂商拒收并点名**、**声明回退即拒绝而不重标**、腾讯路由的拒绝理由原样抛出、schema 拦下非法厂商、两所单位标注、港股币种为空且说明原因、A 股准则与币种、累计期起始日、`_YOY` 不入行、超期计数、空回购 / 非 A+H 对为空而非缺失、陈旧北向快照拒收、未 canonical 化的 capture 拒收 |
| `tests/test_cn_hk_findata_cli.py` | 17 | 未批准 → 无产物、跨操作记录拒收、schema 漂移拒收、source 漂移拒收、产物哈希 = canonical capture 的 sha256、同 capture 两次同产物同 invocation、**归一化失败仍留产物**、六个操作端到端跑通、非法 wire 一个字节不落盘、厂商拒绝单列 `refusal_kind`、summary 带口径注与主机、失败也写 summary、缺参数点名、双模式互斥（两处）、越界操作拒收、`akshare` 不在 `sys.modules` |

修改的共享测试（加项，未删断言）：`tests/test_connector_inventory.py`（profile 集合与 public 集合各加一个 slug）、`tests/test_connector_quota_policy.py`（精确配额清单加六条）。

八份 fixture（`tests/fixtures/cn-hk-findata/`，共 568 KB）全部是 2026-09-09 一次性、只读抓下来的真实调用：

| 文件 | 内容 |
| --- | --- |
| `financial-statements-a-600519-income.json` | 贵州茅台利润表（报告期）。**已截短**：上游返回 103 期，保留最近 24 期，`capture_note` 写明 |
| `financial-statements-hk-00700-income.json` | 腾讯控股利润表（年度）。**已移除** `statement_meta` 帧，理由见 2.5(2)，`capture_note` 写明 |
| `shareholders-600519.json` | 十大流通股东（2026-06-30）+ 63 期户数史 |
| `buybacks-600519.json` | 全市场回购表。**已截短**：原 5,510 行，保留 600519 的 2 行加另外 40 行，`capture_note` 写明 |
| `margin-balance-sse.json` / `margin-balance-szse.json` | 沪市 9 个交易日 / 深市单日 |
| `northbound-flow.json` | 沪深港通四行（沪股通 / 港股通(沪) / 深股通 / 港股通(深)），交易日 2026-09-09 |
| `ah-premium.json` | 东财 A+H 全表 204 行；测试取 01398 / 601398（工商银行），溢价 23.95 |

抓到的真实数字（可核对）：茅台 2026 中报 `TOTAL_OPERATE_INCOME = 92,278,072,083.21` CNY；茅台 2026-06-30 股东户数 296,404（上期 243,159，+21.90%）；茅台 2025-11-05 起那次回购已回购 2,188,614 股 / 2,999,933,749.57 元、状态「完成实施」；腾讯 FY2025 营业额 743,689,000,000；沪市 2026-09-04 融资融券余额 1,350,016,680,402 元。

---

## 8. 开放问题

1. **一个 profile 装了四个厂商，对不对？** CONNECTOR_PROTOCOL 说 connector 是 source / transport / auth / completeness / provenance 的权限边界，严格读下去，东财、上交所、深交所应该是三个 source。计划的 S4 行明确要求「一条 `cn-hk-findata` 连接器 + 每个厂商的主机都要声明 + `fallback_used` 时标口径」，所以按计划做了，并且用每行的 `source_vendor` 枚举 + 每操作的 schema 哈希把边界压回到操作粒度。**如果 owner 更想要三条连接器**（`cn-hk-eastmoney`、`cn-exchange-margin`、…），拆分是机械的：`PROFILE_DEFINITIONS` 拆成三个条目、重跑 build 脚本、重发治理记录，适配器与 CLI 不用动。
2. **港股覆盖到底有多厚？** 三表能拿到（腾讯 25 个年度期），但**没有币种、没有会计准则**（2.5(2)），也**没有股东**——akshare 里没有港股十大股东的等价函数，`shareholders` 因此在契约层就写死为 A 股（`a_ticker` 是六位数字）。港股的股东与回购要么走 `hkexnews`（skill 那边是 Gemini 检索，不是一手枚举），要么等一个新 source。
3. **`buybacks` 的代价与 `enumerated` 的诱惑。** 为一家公司读完全市场 12 页是本连接器最贵的操作。真要接 lane，正确做法是一天读一次全表、缓存、给五家公司各过滤一次，而不是问五次。这需要 lane 层的一个「全市场表缓存」概念，本波没做。
4. **`northbound_flow` 是快照，不是历史。** `stock_hsgt_fund_flow_summary_em` 只有「它手上那一天」。要做北向的时间序列得换 `stock_hsgt_hist_em` 一类的接口——那是另一个操作、另一个 schema 哈希、另一条批准，本波没开。
5. **`ah_premium` 只覆盖 A+H 双重上市的那 100 多对。** 单独上市的公司返回空行加 `universe_row_count`，这是正确答案，但 lane 不该反复问。
6. **被留在外面的 81 个 intent，为什么。** 大致三类：(a) **行情类**（`hist_a` / `hist_hk` / `index_*` / `minute_a` / `valuation_*`）——P11a 的 yfinance 连接器已经是价格层，再开一条会有两个互相矛盾的价格权威；(b) **情绪与热度类**（`hot_rank` / `comment_*` / `stock_news_*` / `global_news_*` / `lhb` / `zt_pool` / 资金流）——大众层级，S3 的雪球 / X 那条线才是它们的位置，而且这些正是 skill 里回退最频繁、口径最杂的一批；(c) **一手但另有归属**（`notice` / `research_report` / `jgdy`）——公告归 `cninfo`，研报归 AlphaEngine。剩下确实该进但本波没做的是：`financial_indicator`（ROE / 杜邦）、`fhps`（分红派息）、`ggcg`（董监高增减持）、`restricted_release`（解禁）、`pledge_ratio`（股权质押）、`profit_forecast`（一致预期，注意它与 P11b 的 consensus 双路会撞）。这六个是下一波最划算的加项，都不需要新主机。
7. **`stock_zh_ah_spot`（腾讯）在 skill 的 registry 里仍然绑着 `ah_spot`，而它已经不返回溢价了。** 这是 skill 侧的一个 bug，不是 Dalton 侧的；值得回传给 OpenClaw。
