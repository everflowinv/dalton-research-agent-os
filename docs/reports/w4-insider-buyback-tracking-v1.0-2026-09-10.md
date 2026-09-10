# W4 切片交付报告：insider / buyback tracking（美国侧）v1.0

日期：2026-09-10
分支：`w4-insider-buyback`（worktree `~/Projects/dalton-w4-insider-buyback-worktree`），基线 main `ba99ef9`
作者：Opus 5 subagent
owner 要求（原意）：日常跟踪必须包含 filing，尤其是管理层减持与回购公告；**它们重不重要、值不值得深挖是大脑的判断，不是规则**。
小额内部人卖出是弱信号；大额卖出可能是看空（管理层认为股价已充分反映价值），也可能是 overhang 出清（市场早有预期，利空已落地）。
回购必须结合买入价格、趋势、节奏与其他指标一起读。只有香港每日披露回购，美国公司只在 10-Q / 10-K（Item 2 issuer purchases 表）与业绩电话会上披露。

---

## 0. 一句话

内部人交易与美国回购从「一条 payload」变成「一张算出来的表」：每条 `insider_transaction` 事件带一份可重放、零模型调用的派生上下文（占持仓比例、美元金额、90 天同人与全体内部人累计、Form 4 的 10b5-1 勾选框逐字读出、角色、是否被 Form 144 或我们自己的 Claim 预告过），新增封闭事件种类 `buyback_disclosure` 由两个 producer 产出（10-Q/10-K Item 2 月度行、8-K 授权），并配一份把「买入价 vs 现价、节奏 vs 剩余授权、环比、占市值与占 TTM FCF、是否只抵消股本增长」算好的上下文。owner 的解读被写成 **considerations**（供权衡的考量）而不是规则，五词决定词表一字未动。

---

## 1. 做了什么

### 1.1 内部人派生上下文（新文件 `src/dalton_core/insider_context.py`）

零模型调用、纯函数、可重放：全部输入是事件账本的一个切片，同一账本明天算出同一张表。

| 派生量 | 说明 |
| --- | --- |
| `percent_of_holding_following` | 交易股数 ÷ 交易后持股 × 100，owner 的原始口径。持股为 0 时不做除法，返回 `None` 并由 prompt 打印「他现在什么都不剩」 |
| `usd_value` | 股数 × 每股价格。价格为 0（授予）→ `0.00`（这是事实）；filing 根本没有价格字段 → `None`（这是另一回事） |
| `owner_trailing` / `issuer_trailing` | 同一 owner 与该发行人**全体**内部人的 90 天累计（笔数、股数、美元），处置与买入分开 |
| `plan_10b5_1` | `yes` / `no` / **`unknown`**。逐字读 Form 4 XML 的 `aff10b5One`；X0508 及 2023 修订前的表根本没有该元素，缺失一律 `unknown`，绝不当成 `no` |
| `role` | Form 4 的 `reportingOwnerRelationship`（director / officer:title / ten_percent_owner），S5 已有 |
| `anticipated` | `true` / `false` / `unknown` + ref。两条来源：(a) 同一人在 90 天窗口内的 **Form 144**（S5 已把它记成 `person_type` 以 `planned sale` 开头的 `ownership_change`）；(b) claim 账本里预告卖出的 Claim（10b5-1 / trading plan / planned sale / lock-up / secondary offering / form 144）。**没有账本可查 = `unknown`，查了没有 = `false`**，两者严格区分 |
| 编辑性 flag | `routine_award`（code A 且 0 价）、`tax_withholding`（code F）、`no_consideration`、`zero_shares`；withholding 与无对价行**不计入**处置合计，另列 |
| `exercise_and_sell` | 同一人同一天 M + S 配对，按净额与已实现现金报告，作为一件事而不是两件 |
| `cluster_owner_count` | 7 天内交易的内部人数（「同一周多人减持」是与「本季多人减持」不同的事实） |
| `ledger_earliest` | 账本能看到的最早一笔。上线一周的 Core 的「90 天累计」不是 90 天累计，这一行把它说出来 |

`CONSIDERATIONS` 把 owner 的解读逐条写进 prompt，措辞是「考量，不是规则；你来决定」。测试断言 block 里不出现任何一个五词决定词。

### 1.2 10b5-1 勾选框的读法（`sec_ownership_adapter.rule_10b5_1_checkbox`）

**没有动被冻结的 connector 输出契约。** `form4_transactions` 的 output schema 是 `additionalProperties: false`，其哈希经 `ownership_schema_hash` 进入 writer 正在持有的治理批准记录；往 wire 上加一个字段会静默作废一份生产中的批准。所以勾选框是一个**独立的纯函数**读同一份字节，由 `sec_ownership_cli` 调用后写进事件 payload（`plan_10b5_1`）。测试直接断言 `parse_form4` 的 wire 里没有这个字段。

同时 payload 新增 `footnotes_hash`：脚注「effected pursuant to a Rule 10b5-1 trading plan adopted on 22 May 2026」很常见，但那是律师写的散文不是表上的框，**只记哈希、不做解释**（按协调者转来的 filings-alert 规则）。

### 1.3 `buyback_disclosure` 事件种类（`research_event.py` + 新文件 `buyback_disclosure.py`）

- `EVENT_KINDS` 加 `buyback_disclosure`；`PAYLOAD_FIELDS` 加 25 字段的封闭 payload；`DEFAULT_TIER_BY_KIND` 定为 `primary_filing`。
- `disclosure_kind` 两值封闭：`issuer_purchases_table` / `authorisation`。公司**做了什么**与公司**被允许做什么**是两件事，授权只是许可、公司常年不用完。
- **Producer (a) 10-Q / 10-K Item 2**：在既有文档抽取路径的同一段确定性渲染上读
  （`public_web_extraction_source.render_public_web_text`，renderer `html-visible-blocks:0.1`），
  按月度行产出：`shares_purchased` / `average_price_paid` / `shares_purchased_under_plans` /
  `remaining_authorisation` + **单位来自 filing 自己的表头**（`(in millions of U.S. dollars)`）。
  表头没写单位就是 `None`，绝不猜——猜错就是一千倍。
  逐字校验用与模型抽取同一个 `document_numeric_claim.numbers_in`：每个数字必须出现在它被读出的那一段里；
  破折号是 filed zero，记为 `0` 并在 `filed_as_dash` 里标出、跳过逐字校验。
  另有一条**真正有用的对账**：月度行加总必须等于 filer 自己印的 Total 行，并给出加权均价与 filer 均价的偏离（容差 0.5%）。
  列错位（把股数读成价格）是这里唯一现实的失败模式，逐行逐字校验抓不到它，Total 对账抓得到。
- **Producer (b) 8-K 授权**：先按 item 过滤（8.01 / 7.01），再要求正文出现 repurchase / buyback / share purchase program，
  才读金额、单位、`authorisation_change`（`new` / `increase` / **`unknown`**）与「尚可动用」金额；
  记 accession 与 exhibit 文件名。句子里没有金额就什么都不读。
- **Transcript 仍是 `transcript` 事件**：没有新造事件种类，回购 aspect 通过 `transcript_mentions_buyback()`
  以查表方式接上（P12b 既有 `management_and_capital_allocation`，`claim_index_tagging` 的
  `buyback|repurchase` 规则已经指向它），结果渲染在回购上下文块里，让大脑同屏看到。

### 1.4 回购派生上下文（新文件 `src/dalton_core/buyback_context.py`）

| 派生量 | 口径与来源 |
| --- | --- |
| `price_comparison` | 均价 vs 最近收盘（yfinance 价格权威 `latest_close`），带 `market_price_series_version` ref |
| `pace` | 本季支出 vs 剩余授权（按 filing 表头单位换算）→ 还剩几个季度 / 几个月。**单位缺失就不算**，理由写明「猜单位就是一千倍」 |
| `trend` | 按 accession 分组的季度合计，环比百分比 + 最近 6 个季度序列。只有一个季度时明说「下一份 10-Q 之后才可读」 |
| `size` | 占市值（yfinance `market_cap` observation）、占 TTM FCF（`coverage_mission_statement_lines` 的 OCF − capex，按冻结的 concept 名单，四个季度） |
| `dilution_offset` | 两个已申报股本数之差 vs 本季回购股数 → 「回购里有多大比例真的落到股本上」；股本没降则 `offsets_issuance_only: true` |

每个缺失输入都带理由（「本 Core 没有该公司的现金流量表行，FCF 无法计算；它不是 0，也不被假设」）。
每个数字都指回 accession 或 invocation / price version ref。

### 1.5 判断层接线（`event_judgement.py` / `event_judgement_cli.py`）

- 新 `DERIVED_CONTEXT_KINDS = {insider_transaction, buyback_disclosure}` 与 `derived_context()`：按事件种类构建派生块，
  返回 `{context, lines, refs}`。
- `build_judge_prompt` 在事件块之后打印 `derived_lines`；`allowed_refs` 并入 `derived_refs`，
  于是「派生块里印出来的 ref」正好是模型被允许引用的 ref。
- 派生块构建失败**不阻断判断**：块变成一句「could not be built: <原因>」，事件仍按 payload 判断。
  否则就成了算术在决定哪些事件配被判断。
- CLI 每次 run 读一次价格与市值（`_price_reads`），没有价格表的 Core 得到空 map，上下文块自己说明。

### 1.6 跟踪接线

- `deploy/phase9/p14a-tracking-policy-v1.json`：新增 `sec-ownership` cadence，**86400 秒、fixed**
  （Form 4 法定 T+2 交易日申报，每交易日一次即可比 filer 落后不超过一天；固定不可调，理由与 filings index 相同：
  管理层减持与 13D 是覆盖分析师不能晚知道的两件事，大脑调频率、不决定看不看）。
  周末缺口由 lane 自身覆盖：ownership lane 从 filings index 推导「哪些还没读」，而不是从「上次什么时候跑」，
  因此周一那一跑会把上周五的交易一并读掉（对应 OpenClaw filings-alert cron 周一 3 天窗口的做法）。
- `sec` cadence 的 `because` 改写：明说它覆盖 filings index 及由该 block 派生的一切（8-K item、业绩日、10-Q/10-K Item 2 表），
  并写明**美国没有更快的回购来源**。
- `source_capability_map.py`：
  - `CONTENT_KINDS` 新增封闭词 `ownership_filing`、`buyback_disclosure`；
  - `sec` 条目写清 CAN / CANNOT，含「美国回购只来自 10-Q/10-K、8-K、电话会」「香港每日回购申报是另一个正在并行建设的 connector，不要指望在这里拿到」；
  - 新增 `sec-ownership` 行（四个 ownership operation），通过新的 `SLUG_INVENTORY_ALIASES` / `SLUG_OPERATIONS`
    借用 `sec` 的 inventory profile 但**只报自己的 operation 与自己的配额行**——否则会把它没有的能力和没花的预算报给大脑；
  - 新增 `cn-hk-findata` 行：A 股全市场回购表可给，**HKEX 每日回购申报不在这里、由并行切片建，本切片不建**。
- `tracking_lane_cli.company_events` 加入 `buyback_event_candidates`：不按 `lookback_days` 开窗（美国一个季度才披露一次，
  7 天窗口只会在 filing 落地当天看得见一次），改为按持有 filing 数量设界；重复读只花一次查表，因为账本对「事件说了什么」幂等。

---

## 2. 没做什么

1. **不建香港每日回购 connector。** 明确按指令不建；只在能力图里指名它存在、由并行切片负责。
2. **不动 `form4_transactions` 的 output schema / connector 治理身份。** 见 1.2，勾选框走独立函数。若日后要把它放进 wire，
   需要一次 connector profile 版本升级 + 重新审批，属于 connector 层，不在本切片范围。
3. **不新建 lane、不新建 schema、不新建 authority。** 因此没有「四处登记」要做：
   producer 挂在既有的 tracking lane（`tracking_lane_cli`），派生上下文挂在既有的 judgement lane。
4. **不新增模型调用**，因此没有新的 purpose tier、没有新的 budget pool 条目。派生上下文完全是算术。
5. **8-K 正文不去抓。** `sec_earnings_release` 已经写明 Dalton 没有抓取 8-K 文档的权限（那需要 discovery plan 新增
   `form: 8-K` 的 spec，是一次 plan 版本）。因此 8-K 授权 producer 只在**本 Core 已持有该 8-K 正文时**才产出事件；
   否则什么都不产出。金额从来不从 filings index 的 `primaryDocDescription` 猜。
6. **10-Q Item 5 的「Trading Arrangements」表没有解析。** ACN 的真实 10-Q 里有一张
   「本季度高管采纳/终止的 10b5-1 计划」表（姓名、职务、采纳日、计划期间、拟卖股数）——这是比 Form 144 更强的
   「预告卖出」证据。留作 open question（见 §5）。
7. **没有做 `contracts/` 下的 JSON schema 变更**：事件账本没有对应的 `*.schema.json`，它的契约就是
   `research_event.PAYLOAD_FIELDS`，已扩展并有测试。
8. **没有碰** `writer_server.py`、`coverage_mission.py`、`bounded_planner_driver.py`、`macos_launchagent.py`、
   `install.sh`、`PROJECT_STATUS.md`、cockpit 两个文件。

---

## 3. 集成时要接的线

1. **事件 id 会变（一次性）。** `insider_transaction` 的 payload 多了 `plan_10b5_1` 与 `footnotes_hash`，
   payload_hash 因而改变，同一份 Form 4 重读一次会产生**一条新事件**而不是 duplicate。
   参照 C1 给 `calendar` 扩 payload 时的核验：live Core 目前没有 `research_events` 表，因此实际影响为零；
   部署前请再核一次（`SELECT COUNT(*) FROM research_events WHERE kind='insider_transaction'`）。
2. **cadence policy 是部署件。** `deploy/phase9/p14a-tracking-policy-v1.json` 增加了 `sec-ownership` 一行。
   live Core 若以自己的副本运行，需要同步这一行，否则 `due_sources` 不会报告 ownership lane 的到期。
3. **cockpit 未接线。** 回购事件与派生上下文没有 cockpit 呈现；`cockpit_plane` / `cockpit_control.html` 属集成时统一做。
   建议至少在「事件」面板上把 `buyback_disclosure` 的 `disclosure_kind` 显示出来（表 vs 授权是两种东西）。
4. **文档索引是 producer 的输入。** `buyback_event_candidates` 读 `document_index_documents`；
   一个从未跑过文档抽取的 Core 得到空列表（不是错误）。要让回购 producer 真正出数，10-Q / 10-K 必须已被 fetch 并入索引。
   accession 从 `sec:filing:<accession>` record ref 或 EDGAR 归档路径的 18 位目录名推导，两者都没有的文档会被
   **跳过并在结果里列出理由**，不会被猜。
5. **statement 行的 concept 名单是冻结的**（`OPERATING_CASH_FLOW_CONCEPTS` / `CAPEX_CONCEPTS` / `SHARE_COUNT_CONCEPTS`）。
   若某家公司用别的 XBRL tag 报同一行，FCF 与股本比较会报 `unavailable` 并说明找过哪些 tag——按设计不猜，
   但集成时值得对五家覆盖公司实跑一次看看命中率。
6. **`source_capability_map` 的 alias 机制是新的**（`SLUG_INVENTORY_ALIASES` / `SLUG_OPERATIONS`）。
   以后再有「同一 connector profile、分开审批的 operation 组」可以复用；不用它的 slug 行为完全不变。

---

## 4. 验收

### 4.1 fixture（离线，一次性抓取，accession 已记录）

`tests/fixtures/sec-buyback/MANIFEST.json` 记录了每份 fixture 的 accession、issuer、URL、抓取方式与「为什么是它」。

| 文件 | accession | 说明 |
| --- | --- | --- |
| `form4-10b5-1-nvda-0001696841-26-000010.xml` | `0001696841-26-000010` | **真实**。NVDA，`<aff10b5One>1</aff10b5One>`，officer 的三笔 code S 卖出。逐字整份保留 |
| `form4-no-plan-acn-0001487630-26-000018.xml` | `0001487630-26-000018` | **真实**。ACN，`<aff10b5One>0</aff10b5One>`——「框在、没勾」与「根本没有这个框」得以区分。逐字整份保留 |
| `acn-10q-item2-0001467373-26-000032.txt` | `0001467373-26-000032` | **真实**。ACN 10-Q（2026-05-31 季度）Part II 的 Item 1A → Item 3 的确定性 block 渲染：三行月度、Total 行、只有首行标 `$`、5 月一列破折号、表头「(in millions of U.S. dollars)」、四条脚注、两端 section 边界，全部完整 |
| `8k-authorisation-synthetic.txt` | — | **合成**（文件首行自己写明）。Dalton 无权抓取 8-K 正文，故未抓真实 8-K；措辞形似回购新闻稿 |

`tests/fixtures/sec-ownership/form4-sale.xml`（既有的合成 X0508 表）被复用为「表上根本没有勾选框」的第三态。

测试无网络、无模型调用。

### 4.2 新增测试

- `tests/test_insider_context.py`（35 项）：勾选框三态 + 两处元素互相矛盾 = `unknown`；connector wire 未被加宽；
  payload 契约；占比 / 金额 / flag 算术；90 天双窗口；withholding 与授予单列；有行缺价则美元合计标为下限；
  M+S 净额；同周聚集计数；Form 144 预告（同人 / 他人 / 窗口外三种）；无账本 = `unknown`、有账本无命中 = `false`、命中 = `true` + ref；
  prompt block 打印全部要求的数字且**不出现任何决定词**；派生块进入 judge prompt 且其 ref 变成可引用。
- `tests/test_buyback_disclosure.py`（59 项）：真实 Item 2 表的三行逐字、单位与币种来自表头、破折号=filed zero、
  Total 行分离、Total 对账（含「列错位被对账抓到」的反例）、逐字校验拒绝不在原段中的数字、
  section 缺失 / 不可读的两种理由；8-K item 过滤、非回购正文什么都不读、new vs increase vs unknown、exhibit 记录；
  事件契约（封闭 payload、每行不同 event_key、未声明字段被拒、无日期不产事件）；
  文档读取（accession 两条推导路径、无法归属的文档被跳过并列理由、无索引不报错）；
  statement 行读取（FCF 由两条 concept 计算、缺行说明「不是 0」）；
  回购上下文（价格对比、单位缺失不换算、节奏、单季不成趋势、双季环比、占市值与占 FCF、只抵消增发被指名、授权无价可比）；
  能力图（新内容词、CAN/CANNOT 文本、ownership 行的 operation 与配额收窄、香港归属、`sources_for`、map 与 prompt table 仍可构建）；
  cadence（两条都常驻、Form 4 每交易日一次且不可调、`sec` 说明美国没有更快来源）；
  tracking lane（扫描接入、事件写入后重复写为 duplicate、派生块进入 judge prompt）。

### 4.3 全量

```
$ cd ~/Projects/dalton-w4-insider-buyback-worktree
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
----------------------------------------------------------------------
Ran 5475 tests in 620.893s

OK (skipped=1)
```

---

## 5. 遗留问题（给 owner / 主 agent）

1. **10-Q Item 5「Trading Arrangements」表没有解析。** 真实 ACN 10-Q 里就有：
   「Joel Unruch，总法律顾问，2026-04-29 采纳，2026-07-30 — 2027-04-23，24,000 股」。
   这是比 Form 144 更早、更具体的「预告卖出」证据，且同样是确定性可解析的表。
   要不要在下一个切片里把它做成 `anticipated` 的第三条来源？（我判断值得，但它是新的解析面，不塞进本切片。）
2. **8-K 正文抓取权限。** 目前美国回购授权只有在 8-K 正文碰巧已被抓进 Core 时才读得到。
   要让「授权公告」真正成为每日跟踪的一环，需要在 discovery plan 里加一个 `form: 8-K` 的 spec（一次 plan 版本 + owner 发布）。
   在此之前，授权这条腿是半通的，报告里已如实标注。
3. **`anticipated: false` 的强度。** 现在的 `false` 意思是「查了这家公司的 Claim，没有一条预告卖出」。
   Claim 覆盖薄的公司会大量出现 `false`，而真相更接近「我们没写过」。
   是否要在覆盖厚度低于某阈值时把它降级为 `unknown`？这是一条策略判断，我没有替 owner 定。
4. **XBRL concept 命中率。** FCF 与股本比较依赖三组冻结的 tag 名单。五家覆盖公司实跑一次前，
   不知道有多少家会落到 `unavailable`。建议集成时跑一遍并把结果记进 PROJECT_STATUS。
5. **回购事件的判断预算。** 一份 10-Q 会一次产出三条 `buyback_disclosure` 事件（三个月），
   判断 lane 的 `MAX_EVENTS_PER_COMPANY = 3` 会被它们占满一次 run。
   要不要在判断层把同一 accession 的月度行合并成一次判断？（合并会丢掉「哪个月停了」这个正是我们想看的信号，
   所以我倾向不合并，而是把 per-company 上限调高；这是 C2 预算池的事，留给主 agent。）
