# Gate 1：五家公司 SEC 季度收入增速验证简报 v0

日期：2026-08-21
状态：5/5 正式 closure 和进程重启后离线重放均通过；尚未运行 thesis-impact

## 结论

同一代码 commit `0b0f872c0f935098f8e41af339a93d8164684992` 已从 SEC Company Facts 完成
Microsoft、Apple、NVIDIA、Walmart、Amazon 五家公司的季度收入同比验证。每家公司都从同一份最新 10-Q
accession 选择 current/prior 季度事实，经 source verifier 和 numeric verifier 后生成一条正式 Claim，并在进程
重启后只读持久化 authority 完成无网络重放。五条主链都没有逐 plan 或逐 Claim 人工 gate，也没有模型调用和模型
成本。

完整结果 bundle 在仓库外生成；脱敏摘要见
[`docs/review-evidence/gate1-sec-revenue-growth-summary.json`](../review-evidence/gate1-sec-revenue-growth-summary.json)。
Bundle hash：`7f69dc9a483d3e04cc6c8c6eeb01563ad0e5e94e28d189df34350f812a95844b`。

## 五家公司结果

### MSFT — Microsoft

- Filing：[10-Q accession 0001193125-26-191507](https://www.sec.gov/Archives/edgar/data/789019/000119312526191507/0001193125-26-191507-index.html)，filed 2026-04-29。
- 口径：`us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`，USD。
- 2026-01-01..2026-03-31：82,886,000,000；同比期间 2025-01-01..2025-03-31：70,066,000,000。
- 同比：18.30%；source verifier / numeric verifier：`pass / pass`。
- Formal Claim：`claim-version:cb32bdeb2094e2dad62d3e4ee38ce18689dbbe75c7bf6a500905e1f457515e93`。
- Authority hash：`48df0b479615a5f16381051de1024c1716f34ee77177043f02ecf235fa9411d4`。
- Thesis impact：`not yet run`；实际模型成本：USD 0.00。

### AAPL — Apple

- Filing：[10-Q accession 0000320193-26-000020](https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/0000320193-26-000020-index.html)，filed 2026-07-31。
- 口径：`us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`，USD。
- 2026-03-29..2026-06-27：109,417,000,000；同比期间 2025-03-30..2025-06-28：94,036,000,000。
- 同比：16.36%；source verifier / numeric verifier：`pass / pass`。
- Formal Claim：`claim-version:1171073bf9e3fa62c44716c8667955fe77353c2a2449a220eea94f6463d2ab86`。
- Authority hash：`5c217a26c4d978e451251d4a06e6317189e9c801efccbff1aeba8aa7e321bc9f`。
- Thesis impact：`not yet run`；实际模型成本：USD 0.00。

### NVDA — NVIDIA

- Filing：[10-Q accession 0001045810-26-000052](https://www.sec.gov/Archives/edgar/data/1045810/000104581026000052/0001045810-26-000052-index.html)，filed 2026-05-20。
- 口径：`us-gaap:Revenues`，USD。
- 2026-01-26..2026-04-26：81,615,000,000；同比期间 2025-01-27..2025-04-27：44,062,000,000。
- 同比：85.23%；source verifier / numeric verifier：`pass / pass`。
- Formal Claim：`claim-version:5519dfdc333eba1b8c3a4fd68606213bf8a424704cf1efb28d889f28d8612247`。
- Authority hash：`81a077f0eff08195002585eb2fb6da5acb8ebf9f9cdf8eb8cbb0eb687db1c867`。
- Thesis impact：`not yet run`；实际模型成本：USD 0.00。

### WMT — Walmart

- Filing：[10-Q accession 0000104169-26-000102](https://www.sec.gov/Archives/edgar/data/104169/000010416926000102/0000104169-26-000102-index.html)，filed 2026-05-29。
- 口径：`us-gaap:Revenues`，USD。
- 2026-02-01..2026-04-30：177,751,000,000；同比期间 2025-02-01..2025-04-30：165,609,000,000。
- 同比：7.33%；source verifier / numeric verifier：`pass / pass`。
- Formal Claim：`claim-version:764913f2160c8afed8cc35cf0bdcd97507901d3cec13baf12274d6a8cf3781fd`。
- Authority hash：`c1d85db8566165a245841fce9d8b7c38704575bd2150db64fb62b4d421aef60c`。
- Thesis impact：`not yet run`；实际模型成本：USD 0.00。

### AMZN — Amazon

- Filing：[10-Q accession 0001018724-26-000026](https://www.sec.gov/Archives/edgar/data/1018724/000101872426000026/0001018724-26-000026-index.html)，filed 2026-07-31。
- 口径：`us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`，USD。
- 2026-04-01..2026-06-30：200,606,000,000；同比期间 2025-04-01..2025-06-30：167,702,000,000。
- 同比：19.62%；source verifier / numeric verifier：`pass / pass`。
- Formal Claim：`claim-version:3f6fcef8fb35eced988e16b98e1b62e4222b87d41ec923a7c7eafe78abb87b10`。
- Authority hash：`d40e2d5f1e043e3e1f5a9ed7d7d890797a2a7df3dbcfdcbdf844eb6d26c3c198`。
- Thesis impact：`not yet run`；实际模型成本：USD 0.00。

## 独立解析复核

本轮另用 `findata-analyst` / edgartools 重读官方 filing；它与 Dalton 的 Company Facts adapter 是两条独立解析路径，
但底层来源同为 SEC，因此不能称为两个独立信息源。

- `list-filings` 确认五个 accession 都是各公司截至 2026-08-21 的最新 10-Q。
- `search-text` 在五份 10-Q 的财务报表正文中命中全部 10 个 current/prior 数值，并确认单位均为 USD millions。
- 报表期间标题与 Company Facts duration 一致：MSFT、AAPL、NVDA、WMT、AMZN 都是三个月期间。
- 对十个数值独立复算得到 18.297034%、16.356502%、85.227634%、7.331727%、19.620517%，按两位小数
  分别为 18.30%、16.36%、85.23%、7.33%、19.62%。

通用 `smart-facts --keyword Revenue` 不能替代这条规则：它对 NVDA 和 WMT 误选 `CostOfRevenue`，对 MSFT 的同一
period end 还会优先返回九个月累计值。Gate 1 因此继续要求 exact accession、exact duration、同一 filing 的
current/prior、固定有序 concept allowlist 和 numeric verifier，不能只按关键词或 `period_end` 取数。

## Fail-closed 控制

控制样本只允许 Walmart 已过期的 `SalesRevenueNet`。结果为 `expected-fail-closed / failed`：

- candidate staging 六类记录均为 0；
- 正式 EvidenceVersion、ClaimVersion、ThesisVersion 均为 0；
- Core、candidate staging、coordinator、capability 四个 SQLite integrity check 均为 `ok`；
- 结果 hash：`98cf640487c599b0e3177b33e2fadc479dbf9ca2dbcdde1a7884e61c0bc45cd6`。

## 重放与验证

五家公司都已在首次网络运行结束、进程退出后执行：

```bash
python scripts/replay_sec_research_plan_canary.py \
  --output-dir <BUNDLE_DIR>/samples/TICKER
```

重放只读取落盘 authority，`network_calls=0`；closure 统一返回 `duplicate`，并保持 exact answer binding、formal
Claim ref/hash、1 条 Evidence、1 条 Claim、0 条 Thesis、0 个人工 gate，全部 integrity check 为 `ok`。

本机代码验证：相邻 research-plan / closure / thesis-impact 回归 81/81；Python 全量 587/587；broker 16/16；
显式 hermetic replay 1/1；build、compileall、JSON schema summary 和 `git diff --check` 全部通过。

本简报只验证正式财务 Claim 的同口径复现。Thesis impact 尚未运行，不构成投资建议。
