# P11a / P11c 市场层交付报告 v1.0

日期：2026-09-09
分支：`wave1a-market-layer`（worktree `~/Projects/dalton-wave1a-market-layer-worktree`）
基线 main：`08c66d0`
HEAD：`750de1a`
全量测试：`Ran 2173 tests in 250.643s` / `OK (skipped=1)`（基线 2,034 通过 1 跳过；本片新增 139 项）

---

## 0. 一句话

Dalton 现在能看见价格了：一条 yfinance 连接器（两个操作，两份治理记录，都是 `proposed`）、一个 append-only 的 `MarketPriceSeriesVersion` 权威（每根 K 线绑定一次 connector invocation 与原始产物哈希）、一条无队列的 tick lane，以及一个 derived_deterministic 的 `ValuationSnapshot`；`VALUATION_AUTHORITY_ROLES` 从「五个都要而一个都没有」放宽为「price + shares 必须有」，S6 的估值路径由此第一次可以写出东西。

---

## 1. 提交清单

| commit | 内容 |
| --- | --- |
| `b2ecd54` | connector index 哈希由脚本重生成而非手抄（已被主 agent cherry-pick 到 main 为 `99f6a9b`） |
| `1f840ec` | yfinance 连接器身份、两个操作契约、两份治理记录、配额 |
| `68792dd` | `MarketPriceSeriesVersion` 权威 + schema + 适配器 + ACN 真实 fixture |
| `b11a4fc` | 子进程 CLI + launcher + 无队列 tick coordinator + `market-data` extra |
| `750de1a` | `ValuationSnapshot` 权威 + schema + `model_input` 闸门放宽 |

未推送。未部署。未写 live 状态。未发布 mission 版本。

---

## 2. 做了什么

### 2.1 yfinance 连接器（slug `yfinance`）

- `source_ref: source:yahoo-finance`，`source_type: market_data`，transport `public_https`，host allowlist 只有 `query1.finance.yahoo.com` / `query2.finance.yahoo.com`，auth `none`，`ADAPTER_LIBRARY = "yfinance"`。
- 两个操作一次定义完，避免二次动哈希钉死的 profile：
  - **`daily_prices`**（入参 `ticker` / `start` / `end`）：输出 `bars[]`（date、open、high、low、close、**adj_close**、volume，全部十进制字符串）+ `observations[]`（`shares_outstanding`、`market_cap`，各自带 `as_of`）+ `auto_adjust`（契约层 `enum: [false]`）。
  - **`analyst_estimates`**（入参 `ticker`）：`price_target`（current / high / low / mean / median / number_of_analysts）、`recommendations[]`（按 period 的 strong_buy…strong_sell）、`eps_estimates[]` 与 `revenue_estimates[]`（period / avg / low / high / year_ago / growth / number_of_analysts / currency）。**契约、适配器函数、真实 fixture 都已就位；它的 lane 是 Wave 2。**
- `yfinance_core.py`：`yfinance_contract` / `_source_hash` / `_schema_hash(operation)` / `_adapter_hash(operation)` / `_identity(operation)` / `_permissions` / `_output_schema(operation)` / `build_yfinance_governance_record(operation=…)`，形状照 `roic_transcript_core.py`。另有 `invocation_ref(...)`：把 operation、source、adapter 哈希、治理记录、入参、产物哈希一起哈希成一次调用的名字——同窗口同批字节是同一次调用，字节变了就是另一次调用，这正是版本链需要分辨的东西。
- `connector_governance.py`：两个 kind（`yfinance-daily-prices`、`yfinance-analyst-estimates`）、四个惰性哈希 thunk、`GOVERNANCE_KIND_REGISTRY` 两项、`build_governance_record` 分派、`__all__` 导出。
- `deploy/connector-governance/yfinance-daily-prices-v1.json`、`yfinance-analyst-estimates-v1.json`，均 `status: proposed`，`approved_by: human:lumos`。
- `connector_quota_policy.py`：`daily_prices` 200 单位 × 2 次物理调用（download + info），`analyst_estimates` 50 单位 × 4 次。理由写在代码里：这是个从未答应服务我们的非官方免费源，天花板是礼貌不是算术。
- **这个连接器故意不暴露财务报表。** Yahoo 有，但抓来的二手财报数字和 filing 里的数字长得一模一样——一旦准进来，「每个数字都能回指 filing」就只是装饰。有一条测试钉住操作集合。

### 2.2 `MarketPriceSeriesVersion`（`market_price.py` + `market_price_schema.sql`）

- 每公司一条 append-only 版本链（`series_ref = market-price-series:{company_ref}`，按 company 而非 ticker 命名）；三触发器（`dalton_authorized()` insert guard、no_update、no_delete）、`content_hash`、写后读回校验。
- 每根 K 线携带 `invocation_ref` + `artifact_hash`。
- **发布规则**：只有当出现「上一版没有的交易日」或「某一天的数字变了」才发新版；否则 `duplicate`。新版携带完整历史，并显式列出 `added_bar_dates` 与 `restated_bar_dates`——拆股/分红/Yahoo 更正在版本链上看得见，而不是靠对比推断。改写历史永远是新版本，不是 update。
- **不由 observations 触发发版**：盘中市值每秒都在动，一次没抓到新交易日但市值少了六百万，不是关于公司的新事实；否则整个下午每 tick 一版。同一天的同类观测取最新一次覆盖旧的。
- 读者：`series()`、`latest_close()`（同时给 close 与 adj_close 及 `as_of` 与两个 ref）、`latest_observation(kind)`、`versions()`。
- 校验：close/open 必须落在当日 high/low 之内、low ≤ high、volume ≥ 0、同窗口不得重复日期、float 直接拒收（二进制浮点不是印出来的价格）、ticker/currency 不得中途变更。

### 2.3 tick lane

- `market_price_cli.py`（子进程）：**approval first → artifact always → contract last**。治理记录必须 approved 且哈希仍描述打包契约（不然在碰网络之前就拒）；库的完整输出 canonical + sha256 + 写入 RawSpool，**失败的运行也留产物**；wire 先过冻结的 output schema，再进权威。`summary.json` 总是写。退出码取自 status。模式二选一：`--allow-network` / `--fixture-file`，另有 `--no-publish`。
- `market_price_launcher.py`：`MarketPriceLauncher(LaneChildLauncher)`，ticket 前缀 `market-price-run`，目录 `market-price-runs`，digest = `sha256(prefix|company|ticker|start|end)[:24]`。
- `mission_market_price_lane.py`：`MissionMarketPriceLaneCoordinator`，**无队列**（照 `mission_model_spec_lane`）：先结算上一 tick 的孩子，再从 mission universe 里挑一家。窗口 = 首跑回溯 3 年，之后从最后一根 K 线的次日起（Yahoo 的 `end` 是开区间，所以取「明天」）。已经最新的公司直接跳过；一次「什么都没新增」的运行会把该公司搁置 6 小时（否则周末每五分钟花一次调用重新发现周六不是交易日）；连续失败 3 次的公司让出槽位，成功一次即清零。
- **授权检查**：mission 的 `autonomy.may_write` 里没有 `market_price` 就返回 `ungranted` + 一句话理由，不起孩子，每 tick 如此直到 owner 发新版 mission。这是正确行为，不是要绕开的 bug。

### 2.4 `ValuationSnapshot`（`valuation_snapshot.py` + schema）

- `kind: derived_deterministic`，`formula_version: "valuation-formula:p11c:0.1"` 写进每条记录。
- 指标与冻结公式：

| 指标 | 公式 | 精度 |
| --- | --- | --- |
| `trailing_pe` | `market_cap / net_income_ttm` | 4 位小数 |
| `price_to_sales` | `market_cap / revenue_ttm` | 4 位小数 |
| `ev_to_ebitda` | `(market_cap + total_debt - cash_and_equivalents) / (operating_income_ttm + depreciation_amortisation_ttm)` | 4 位小数 |
| `fcf_yield` | `(operating_cash_flow_ttm - capital_expenditure_ttm) / market_cap` | 6 位小数 |

  `market_cap = close × shares_outstanding`（自己算，不用 yfinance 的 `marketCap` 字段；后者仍作为观测入库）。
- **每个输入都是 ref**：价格 = 版本 ref + bar date + invocation + artifact；股本 = 版本 ref + 观测日期；filed 数字 = concept + statement + period + **accession**，且 `source_ref` 必须在 `ALLOWED_FUNDAMENTAL_SOURCES = {"source:sec-edgar"}` 内——**从 yfinance 取基本面会被按 source 拒绝**，有专门测试。
- 角色区分 flow / instant：`trailing_sum` 角色必须正好 4 个季度（三个季度当一年会低估每一个倍数），`instant` 角色必须正好 1 个时点；同一期间重复计入拒收；capex 必须为正的流出量（预先取负会让自由现金流翻倍），有测试。
- **缺输入 = 该指标 `unavailable` + 一句人能行动的理由**，绝不猜、绝不回退到 yfinance 基本面。分母非正也 `unavailable`（亏损公司不给 P/E，「-40x」会诱导读者拿它和 40x 比）。整份快照不会因为一个概念缺失而失败。
- **分位数**：`PERCENTILE_METHOD = "share_of_history_at_or_below"`，在每根库存 K 线上用「该日已披露」的基本面窗口重算指标；早于第一个窗口的 K 线被排除而不是回填（拿 2026 年的财报去给 2024 年的价格估值＝会看未来的分位）。样本 < 30 根不给分位，只给根数与理由。**只有一个基本面窗口时，`percentile_basis` 老实写 `price_only`**——基本面不动，倍数分位就是价格分位换了身衣服。喂进多个带日期的窗口才会变成 `price_and_filed_fundamentals`。
- 版本链：同 binding（价格 + 股本 + 窗口 + 公式版本 + 历史日期集合）→ `duplicate`；价格或财报一变就是新版本，旧版本的数字不变。

### 2.5 闸门（`model_input.py`）

```python
VALUATION_AUTHORITY_ROLES = frozenset({"price", "shares", "fx", "rates", "consensus"})
REQUIRED_VALUATION_AUTHORITY_ROLES = frozenset({"price", "shares"})
```

旧闸门要求五个角色全到，而系统里一个都产不出来——「估值需要五个」实际等于「估值永不可发布」。在什么都供不上的时候这是对的默认。现在 P11a 供得上「一个倍数在算术上不可能没有的那两个」。另外三个仍在词表里、被声明时仍被校验（声明 `fx` 依然必须绑定一个该角色的冻结 actual 输入），只是不再必需：美元本土公司没有 FX 可绑，而要求 consensus 才能发 P/E 是把「它交易在什么价位」和「街上怎么想」混为一谈。必需集合是具名常量，将来再放宽会以带理由的 diff 形式出现。

`tests/test_model_input_ledger.py` 里那条测试改名为 `test_valuation_output_requires_price_and_shares_actual_authorities`（断言不变：只有 price 仍然被拒）；打开的正向路径在 `tests/test_valuation_snapshot.py::ValuationGateTests`。

---

## 3. 集成时要接的线

### 3.1 `lane_registry.LaneSpec` 需要什么（Wave 0 落地后一行注册）

```python
LaneSpec(
    operation="dispatch_mission_market_prices",
    core_only=True,                       # 与 dispatch_company_model_spec 同类：无外部副作用声明
    param_fields=frozenset(),             # 无参数，和 dispatch_company_model_spec 一样
    build_coordinator=lambda server: MissionMarketPriceLaneCoordinator(
        authority=MarketPriceSeriesAuthority(server.store),
        launcher=server._market_price_launcher,
        mission=server._active_mission_params,   # 与 model-spec lane 取 mission 的方式相同
    ),
    launcher_factory=lambda args, state_dir: MarketPriceLauncher(
        state_dir=state_dir,
        governance_path=args.market_price_governance,
    ),                                    # governance_path 为 None 时不构造 launcher，lane 保持关闭
    argparse=("--market-price-governance",),          # 单个可选路径参数，默认 None
    argv_fragment=[                                    # macos_launchagent.render 拼接
        "--market-price-governance", str(market_price_governance),
    ],                                                 # 条件：该文件存在（照 --statement-lane-governance 的样子）
    driver_key="mission_market_prices",   # bounded_planner_driver.run_once 的结果键
)
```

要点：

1. **operation 名建议 `dispatch_mission_market_prices`**，与 `dispatch_mission_statements` / `dispatch_company_model_spec` 同族。
2. **launcher 构造参数**：`state_dir`（= `Path(args.db).parent`，与其它 lane 一致）、`governance_path`、可选 `actor_ref`（默认 `automation:coverage-mission`）。
3. **coordinator 构造参数**：`authority`（`MarketPriceSeriesAuthority(store)`）、`launcher`、`mission`（无参 callable，返回 mission params dict 或 None）、可选 `clock`、`backfill_years`。
4. **不需要模型配置**，不碰 `cockpit_model.PURPOSES`，不碰 `scripts/raise_day_budget_cap.py`——这条 lane 一次模型调用都不做。
5. **argv 片段的开关条件**：治理记录文件存在即开，不存在即关（`--statement-lane-governance` 的先例）。写入靠的是 mission 授权，不是 launchagent。
6. `writer_server.close()` 需要 `self._market_price_launcher.close()`。
7. `daily_prices` 的子进程还需要 `market-data` extra 已安装；未安装时 CLI 会以一句「install this package with the [market-data] extra」失败，而不是 import 崩溃。

### 3.2 `deploy/macos/install.sh`

- **治理种子块**：新增两个 kind，照 `roic-*` 的循环写法：
  ```sh
  for yf_kind in yfinance-daily-prices yfinance-analyst-estimates; do
      # seed deploy/connector-governance/${yf_kind}-v1.json
  done
  ```
  两份都是 `proposed`。**owner 需要就地把 `yfinance-daily-prices` 改成 `approved`**，lane 才会真的跑；`yfinance-analyst-estimates` 可以先留 `proposed`（Wave 2 才有消费者）。
- **不需要模型配置块**。
- `.venv` 里需要 `yfinance`（`pip install -e '.[market-data]'` 或直接 `pip install yfinance`）。注意仓库 `.venv` 是主 checkout 的符号链接；本片已在其中装了 yfinance 1.7.0。
- `tests/test_roic_transcript_core.py` 有一条 `test_the_installer_seeds_both` 断言 install.sh 提到两个 roic kind。我**没有**为 yfinance 写对应断言，因为 install.sh 在我的禁改清单里；集成时补上 install.sh 后，建议在 `tests/test_yfinance_core.py::ShippedRecordTests` 里加一条同形状的测试。

### 3.3 mission 版本

下一版 `coverage-mission:us-it-services` 的 `autonomy.may_write` 需要加 `market_price`。在那之前 lane 每 tick 返回 `ungranted`，一次网络调用都不发。（`AUTOMATION_WRITE_SCOPES` 加词由 Wave 0 负责；我没有改 `coverage_mission.py`。）

### 3.4 cockpit

- 公司卡需要展示：最新 close 与 `as_of`、区间涨跌、`ValuationSnapshot` 的四个指标与各自分位（`unavailable` 的要显示理由字符串而不是空白）。
- 建议把 `percentile_basis` 直接显示出来：`price_only` 的分位必须让读者知道它只是价格分位。
- `cockpit_plane` 的 lane 状态面板需要 `dispatch_mission_market_prices` 的 `status`（`launched` / `idle` / `busy` / `ungranted` / `rejected`）与 `skipped[].reason`。
- **`ungranted` 应当在 cockpit 上可见**，否则一条因为缺授权而永远沉默的 lane 看起来和一条健康的空闲 lane 一模一样。

### 3.5 连接器 index 合并

四条连接器分支并行，`index.json` 会冲突。已交付 `scripts/build_connector_inventory.py`（含 `--check` 与具名 diff 摘要，main 上为 `99f6a9b`）：冲突时任取一边，重跑脚本，读摘要确认动的只有新连接器，`--check` 归零即可。

---

## 4. yfinance 的注意事项（务必保留）

1. **`auto_adjust=False`，永远。** 默认的 `True` 会把 Close 悄悄换成复权序列并删掉 Adj Close，之后的读者拿到一列却分不清是哪一列。契约层把 `auto_adjust` 钉成 `enum: [false]`，适配器另有一道拒绝：带复权拍下来的产物不能被当成 Close 入库。
2. **Close 与 Adj Close 分列入库，绝不在复权价上再加股息。** Adj Close 里已经含了。真实证据：ACN 2023-09-11 的 Close 是 `325.87`，Adj Close 是 `308.53607`，三年 5.6% 的差就是分红——谁把它们混为一谈，谁的总回报就错一次或两次。
3. **展平 MultiIndex。** `yf.download` 即便只要一个 ticker，列也是 `(field, ticker)` 二级索引，`row["Close"]` 直接 KeyError，而 `row[("Close","ACN")]` 的形状在要两个 ticker 时又会变。适配器只在一处展平。
4. **Yahoo 给的是被拓宽成 double 的单精度数。** ACN 开盘 183.78 到手是 `183.77999877929688`。原样入库会让每一条引用里出现一个交易所从没印过的数字，而且不同库版本的拓宽方式一变就看起来像一次 restatement。处理办法：当一个 double **恰好**是某个 float32 时（用 `struct` 判定，不是猜），取能 round-trip 回同一个 float32 的最短十进制；不是 float32 时原样保留。没有小数位常数，因此不会在次分位报价或 1998 年的复权价上出错。
5. **`shares_outstanding` / `market_cap` 没有历史。** Yahoo 只给「它现在知道的最新值」，所以它们是各自带 `as_of`（＝读取当天）的独立观测，不是 K 线上的字段。任何需要历史股本的估值都必须另找来源，不能假装这里有。
6. **不要从这里取财报。** 见 2.1。
7. **`analyst_estimates` 的每个块都可能整块消失**，所以契约里所有数字都可空；一个块没了不影响其它块。`growth` 一类字段带着 Yahoo 自己的浮点噪声（如 `0.012200001`），照原样保留——它们是意见，不是入账数字。
8. **这是非官方源。** 没有 API、没有条款、没有申诉渠道，endpoint 随时可能改形状。配额小是礼貌；库版本变动应视为可能的 restatement 来源。

---

## 5. 验收

### 5.1 全量测试（原文）

```
Ran 2173 tests in 250.643s

OK (skipped=1)
```

命令：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`（worktree 内；已核验 `dalton_core.__file__` 指向 worktree）。基线 2,034 通过 / 1 跳过，本片新增 139 项，无跳过、无静默失败。

新增测试文件：`tests/test_yfinance_core.py`、`tests/test_market_price_adapter.py`、`tests/test_market_price_authority.py`、`tests/test_market_price_cli.py`、`tests/test_mission_market_price_lane.py`、`tests/test_valuation_snapshot.py`。fixture：`tests/fixtures/market/acn-daily-prices.json`（ACN 十个交易日的真实 `yf.download`）、`acn-analyst-estimates.json`（真实 ACN 一致预期）。**全部离线运行**，网络只用于抓 fixture 与烟测。

### 5.2 烟测（真实网络，写入临时 state 目录 `/tmp/p11a-smoke2`，未碰 live）

第一跑，ACN 三年回填：

```json
{
 "status": "succeeded",
 "failure_reason": null,
 "series_status": "fresh",
 "bar_count": 752,
 "first_bar_date": "2023-09-11",
 "last_bar_date": "2026-09-09",
 "added_bar_count": 752,
 "restated_bar_dates": [],
 "invocation_ref": "connector-invocation:yfinance:a650bfed278b2a49559de2b0b741df97",
 "series_version_ref": "market-price-series-version:84a71cd3ed84585bdda94c973b90ed36"
}
```

第二跑，同一个已收盘的窗口再来一次：

```json
{
 "status": "succeeded",
 "failure_reason": null,
 "series_status": "duplicate",
 "bar_count": 4,
 "first_bar_date": "2023-09-11",
 "last_bar_date": "2026-09-09",
 "added_bar_count": 0,
 "restated_bar_dates": [],
 "invocation_ref": "connector-invocation:yfinance:9f6e402b5f4269cc13160ea1de18cf83",
 "series_version_ref": "market-price-series-version:84a71cd3ed84585bdda94c973b90ed36"
}
```

落库后读回：

```
versions: 1
latest_close  2026-09-09  close=177.05  adj_close=177.05
              artifact=cc033b6e243b03b0bab32fd970b8512c98330ddd1ff78f825bc5e6b661f25b0a
shares_outstanding  as_of=2026-09-09  611942109
market_cap          as_of=2026-09-09  108344344576
first bar  2023-09-11  open=327.49 high=328.23 low=324.49 close=325.87 adj_close=308.53607 volume=1551300
           invocation_ref=connector-invocation:yfinance:a650bfed278b2a49559de2b0b741df97
```

原始产物 128 KB 在 `connector-spool`。**752 根 ≥3 年日线、每根绑 invocation 与 artifact 哈希**——蓝图 Wave 1 验收「五家 ≥3 年日线且每点绑 connector invocation」在 ACN 上已成立；另外四家只差 mission 授权与一次 lane tick。

烟测过程中还意外撞见了一次真实的 restatement：当天盘中先后两跑，`2026-09-09` 这一根被列进 `restated_bar_dates` 并发了新版本——收盘前的最新一根本来就会动，而版本链把它记了下来而不是覆盖掉。（这也暴露了一个当场修掉的缺陷：最初的发布规则把「市值变了」也算作新事实，会导致整个交易时段每 tick 一版；现已改为只由 K 线增删改触发，有测试钉住。）

---

## 6. 没做什么

- **没有接线**：`writer_server.py`、`bounded_planner_driver.py`、`macos_launchagent.py`、`deploy/macos/install.sh`、`coverage_mission.py`、cockpit 两个文件、`docs/PROJECT_STATUS.md`、`tests/test_service.py` 一律未动（禁改清单）。lane 需要的注册信息见 §3.1。
- **`analyst_estimates` 没有 lane**：契约、适配器、fixture、治理记录都在，`ConsensusEstimateVersion` 与它的 tick lane 是 Wave 2（P11b）。
- **没有端到端的真实 ACN 估值**：`ValuationSnapshot` 的输入需要 statements 权威给出的 filed 数字（8 个角色 × 4 个季度 + 2 个时点），本片没有把 statements 侧的取数器接上（那会碰 `coverage_mission.py`）。公式已用手算数字逐一验证。
- **没有 `MarketEvent`**（P11d，Wave 2）。
- **没有历史股本序列**：yfinance 只给最新值，所以多期基本面窗口目前只能靠 statements 侧提供，估值分位在只有一个窗口时如实标 `price_only`。

---

## 7. 待决 / 开放问题

1. **`yfinance-daily-prices` 需要 owner 批准**（就地把 `deploy/connector-governance/yfinance-daily-prices-v1.json` 的 `status` 改成 `approved`）。在此之前子进程会以「governance record is not approved」拒绝。
2. **下一版 mission 需要授予 `market_price`**。没有它 lane 永远 `ungranted`。
3. **估值分位的诚实度取决于基本面窗口数**。要让 `percentile_basis` 变成 `price_and_filed_fundamentals`，需要有人把 statements 权威里逐季的 TTM 窗口按「披露日」喂进来。建议在集成时把这件事挂到 statements lane 结算之后：每来一份新 10-Q，就多一个带日期的窗口。是否值得为此建一个 `valuation_snapshot_inputs` 投影，请主 agent 定。
4. **历史股本从哪来？** 目前所有历史多期估值都用「最新股本」，这对回购力度大的公司会系统性偏差。SEC 的 `dei:EntityCommonStockSharesOutstanding` 或 `us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding` 是候选，但那要在 statements 侧取。先记为缺口。
5. **EV 的债务口径**：现在用 `total_debt` 与 `cash_and_equivalents` 两个角色，由调用方给出 concept。租赁负债是否计入、短期投资算不算现金，是需要在 concept 映射里写死的判断——这属于 statements/规格侧的决定，本片只保证「给什么就算什么，且每个数字带 accession」。
6. **`fcf_yield` 的 capex 符号约定**：必须传正的流出量。若将来接自动取数器，取的是 `PaymentsToAcquirePropertyPlantAndEquipment`（正数），不要传现金流量表里带负号的呈现值。已有测试拒绝负值。
7. **yfinance 版本漂移**：本片在 1.7.0 上开发与抓取；计划文档里记的系统 Python 是 1.2.0。extra 用的是下限 `>=0.2.31` 而非区间——这是个非官方源，能随时取到修复比锁窄更重要，但库升级应当被当作可能的 restatement 来源观察一次。
8. **`.venv` 是主 checkout 的符号链接**，本片按指示在其中安装了 `yfinance`（连带 pandas 3.0.5 / numpy 2.5.3 等）。若主 checkout 的测试对这些包敏感，请主 agent 复核；本片的全量 2,173 项在装完之后全绿。
