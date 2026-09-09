# C1 事件日历交付报告 v1.0

日期：2026-09-09
分支：`c1-catalyst-calendar`（worktree `~/Projects/dalton-c1-catalyst-calendar-worktree`）
分叉基线：main `2fa5934`（2,512 项）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 C1 与「Daily tracking」一节、[vision 回顾](vision-review-against-plan-v1.0-2026-09-09.md) C1、蓝图 §5.2 P14f、ADR-0008、[P11a 市场层](p11a-market-layer-v1.0-2026-09-09.md)
全量测试：见第 7 节，原文粘贴

---

## 0. 一句话

Dalton 现在知道每家覆盖公司下一次开口是什么时候，也知道**谁这么说的**：yfinance 的日历给出 `estimated` 的日期，公司自己的 8-K Item 2.02 给出 `confirmed` 的日期，两者不一致时两个日期都留着、都带各自的 confidence、永不取平均；日期一动就出新版本带 `driver_event`，estimate 变 confirmed 出新版本带 `evidence_thicker`。P14f 的两个窗口（T−30 preview、T+0..T+2 calibration）由这条日历触发，**且只由 confirmed 触发**——今天这意味着一个窗口都还没开，原因写在第 6 节，那是本片最需要 owner 裁决的一件事。

---

## 1. 交付清单

| 文件 | 内容 |
| --- | --- |
| `src/dalton_core/catalyst_calendar.py` + `catalyst_calendar_schema.sql` | `CatalystCalendarVersion` 权威：append-only、`content_hash`、三触发器、读回校验、`next_catalyst` / `upcoming`；以及 P14f 窗口的事件发射器 |
| `src/dalton_core/yfinance_calendar_adapter.py` | yfinance `calendar` 操作的取数与归一化，以及它变成的日历条目 |
| `src/dalton_core/sec_earnings_release.py` | 8-K Item 2.02 探测器：读**已经落盘**的 SEC filings index 原始产物，零网络调用 |
| `src/dalton_core/catalyst_calendar_cli.py` | 子进程：approval first → artifact always → contract last |
| `src/dalton_core/catalyst_calendar_launcher.py` | `CatalystCalendarLauncher(LaneChildLauncher)` |
| `src/dalton_core/mission_catalyst_lane.py` | 每天每公司一个有界孩子的无队列 lane + `LaneSpec` 注册 |
| `deploy/connector-governance/yfinance-calendar-v1.json` | 新治理记录，`status: proposed`，`approved_by: human:lumos` |
| 共享增量 | `connector_inventory.PROFILE_DEFINITIONS`（yfinance 加一个操作 + 一份输出契约）、`connector_governance`（一个 kind）、`connector_quota_policy`（一条配额）、`lane_registry.LANE_MODULES`（一行） |
| 测试 | `tests/test_catalyst_calendar.py`、`test_yfinance_calendar_adapter.py`、`test_sec_earnings_release.py`、`test_catalyst_calendar_cli.py`、`test_mission_catalyst_lane.py`；fixture `tests/fixtures/market/acn-calendar.json`（一次只读网络抓取） |

`writer_server.py`、`coverage_mission.py`、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、`cockpit_*`、`PROJECT_STATUS.md` **一行都没动**——lane 注册表把它们全推导出来了（第 4 节有核验）。

未推送。未部署。未写 live 状态。未发布 mission 版本。

---

## 2. 形状

### 2.1 `CatalystCalendarVersion`

一公司一条链，`calendar_ref = catalyst-calendar:{company_ref}`。版本记录：

```
{schema_version, id, created_at, calendar_ref, version, prior_version_ref, company_ref,
 entries[], entry_count, change_reason, evidence_refs[], changes[],
 next_catalyst_date, as_of, actor_ref, content_hash}
```

条目（`entries[]`，全部由权威推导，调用方给不出）：

```
{entry_ref, event_kind, subject, expected_date, confidence,
 disagreement, disagreeing_dates[], sources[], notes}
```

- `event_kind ∈ {earnings, guidance, investor_day, filing_due, ex_dividend, other}`
- `confidence ∈ {confirmed, estimated}`
- **`entry_ref` 是「哪一次事件」而不是「哪一天」**：`hash(company_ref, event_kind, subject)`。日期挪了必须还是同一条，否则一次改期会被读成「取消 + 新增」。
- `subject` 是「occurrence key」，由 `quarter_subject(date)` 给出日历季度（`2026q4`）。两个来源要先同意在说同一件事，才谈得上一致或不一致，而 Yahoo 和 SEC submissions index 谁都不给财季。**这个近似的方向是故意的**：跨季边界的日期会变成两条并列的条目，而不是一次错误的合并——重复在日历上看得见，静默合并看不见。

`sources[]` 每一项：

```
{kind ∈ {connector_invocation, filing}, ref, source_ref,
 observed_date, observed_at, confidence, note}
```

`confidence` 由 `kind` 推导并强制：`filing` → `confirmed`，其余 → `estimated`。调用方传错直接拒（`CatalystCalendarConflict`）。能给 vendor 贴 `confirmed` 的调用方就是能把两个来源的区别整个关掉的调用方，而这个区别是两个来源存在的全部理由。

**冻结的优先级**：`SOURCE_AUTHORITY = {"filing": 2, "connector_invocation": 1}`，同级按 `observed_at` 倒序、再按 ref。条目的 `expected_date` 与 `confidence` 取胜者的；输的那个日期留在 `sources[]` 里，`disagreement` 与 `disagreeing_dates` 把不一致直接说出来。**没有任何一处做平均。**

**一个来源占几个槽**：vendor 一个来源一个槽（明天再读一次 Yahoo 还是 Yahoo，不是第二个证人——否则一个 vendor 靠重复就能压过一份 filing，而且条目会永远和自己的历史「不一致」）；filing 一个 accession 一个槽（8-K 是不可变的公告，同一季度两份就是两次公告——IBM 2026 年 7 月 14 日和 7 月 22 日各发过一份 Item 2.02，合成「SEC 当前的看法」会静悄悄丢掉两个事实里的一个）。

### 2.2 出版规则（ADR-0008）

新版本只在**加了条目、日期动了、confidence 变了**三者之一时出；其余是 `duplicate`，只花一次读。`change_reason` 由 diff 推导（`required_change_reason`）：任一日期挪动 → `driver_event`；否则 → `evidence_thicker`。调用方给的理由与推导不符直接拒，除非是 `human_revision`。一个版本不能被贴上自己内容否认的理由，否则理由链就不值得读。

`evidence_refs` 必填且不得重复。

**被取代的读数在链上，不在条目里。** ADR-0008 要 `superseded_by`；日历这里换了个位置：昨天的 Yahoo 日期不留在今天的条目旁边，因为一个每天早上被重读一次的来源会让条目无界增长。它留在上一版里（永不删除），并且那一版的 `changes[]` 用 `from` / `to` 把变化点名。取代了什么永远查得回来，只是往回一跳。这条写在模块 docstring 里。

**老化**：`expected_date` 早于 `today − MAX_PAST_DAYS`（120 天）的条目在**别的变化发生时**顺带移出当前版本，记为 `retired`。老化本身永远不构成出版理由——否则日历会因为时间流逝自己出版本。

### 2.3 读者

- `next_catalyst(company_ref, now)` → 最近的、还没发生的一条（当天算「还没发生」，calibration 窗口当天就开），带 `days_until`、`confidence`、`disagreement`、`version_ref`。
- `upcoming(now, horizon_days)` → 所有公司在 horizon 内的条目，按日期排。cockpit 的「下一个催化剂 T−N 天」直接读这两个。
- `entries()` / `versions()` / `version(ref)`。

---

## 3. 两个来源

### 3.1 yfinance `calendar` 操作（新）

- 身份、契约、治理记录、配额都按 P11a 的 `daily_prices` / `analyst_estimates` 同一套：`yfinance_core.CALENDAR_OPERATION`、kind `yfinance-calendar`、capability `capability:dalton:connector:yfinance-calendar`。schema hash 只绑这一个操作，所以批了读价格不等于批了读日历。
- 输出契约（`connector_inventory._output_schema("yfinance", "calendar")`）：

```
{schema_version, ticker, as_of, captured_at,
 earnings_dates[] (0/1/2 条), dividend_date|null, ex_dividend_date|null,
 source_record_refs[], next_cursor, provider_status}
```

- **契约里没有 consensus 数字。** Yahoo 在同一个块里给 EPS 与收入一致预期，`analyst_estimates` 已经在带这两个了。两个操作声称同一个数字，就是这两个数字后来对不上的成因。
- **不用 `Ticker.earnings_dates`。** 那个访问器抓的是 `finance.yahoo.com`，不在本连接器的 host allowlist（`query1` / `query2`）里。数据方便不是绕过批准范围的理由。`Ticker.calendar` 是对 `query2` 的一次 `quoteSummary` 请求，配额记 1 次物理调用。
- **`earnings_dates` 是数组，因为 Yahoo 拿不准时会给一个窗口的两端。** 塌缩成一天就是替来源发明了它没给的精度。
- **过去的除息日不是将来的事件。** Yahoo 的 `Ex-Dividend Date` / `Dividend Date` 是它最后知道的那一次，不是下一次：live 抓下来 DXC 还报着 2020-03-23，停止分红六年之后；ACN 的是两个月前。wire 原样保留（产物就是那次调用），**条目层只在日期晚于观测日时才生成 `ex_dividend` 条目**。有测试钉住 DXC 那个真实的数。
- 每个日期都是 `estimated`。Yahoo 不说日期从哪来，来源是「某个 vendor 有这个数」的日期不是公司承诺过的日期。

### 3.2 SEC 8-K Item 2.02 探测器（零新增网络调用）

现成的 `list_filings` 调用要一个 form，SEC 回的是发行人整个 `filings.recent` 块——**所有 form 都在里面**，而原始 body 被 spool 下来并哈希了。核验（读 live 只读副本 + 已落盘产物）：五家公司的产物里各有 28 / 45 / 101 / 118 / 123 条 8-K，且 SEC 现有的归一化器**丢掉不读**的两列都在：`items`（`"2.02,9.01"` 这种）与 `reportDate`。

所以探测器读的是**已经发生过的、被治理过的那次调用的产物**。读回路径：

```
core.sqlite: connector_call_specs(operation='list_filings')
  → connector_invocations → connector_source_envelopes.raw_response_hash
  → mission_sec_quarters.read_artifact(state_dir, hash)   # 三个 spool root，哈希校验
  → payload["filings"]["recent"]
```

- `items` 按逗号切开后**整项比对**，不做子串匹配（子串会把假想中的 `12.02` 也算进来）。
- 取 `reportDate`（结果公布那天），缺了才回退 `filingDate`；回退是显式的不是默认的。
- 老产物没有 `items` 列 → 返回空而不是抛异常：那是更薄的答案，不是坏掉的 lane。有测试。
- 探测器**取的是日期，不是文档**。那条 8-K 在字节里但不在该 invocation 的 `source_record_refs` 里（那次调用要的是 10-K），所以它不能被变成 filing URL 去抓——`sec_filings_index.build_sec_filing_url_authorities` 正是拒绝这件事的，而且拒得对。日期与文档是两件不同的请求，这里只提第一件。

### 3.3 未来日期的那一半：机制在，产出方还没有

很多公司会提前几周发一份「将于 X 日公布 X 季度业绩」的 8-K。**读那句话需要 filing 正文**，而 Dalton 现在没有抓 8-K 文档的权限（要在 discovery plan 里加一条 `form: 8-K` 的 spec 再走 fetch lane，那是一个 plan 版本，不是本片）。

所以：`sec_earnings_release.announced_next_date_entry(...)` 把这条路径建好、测好、可达，**日期由调用方给**。这个模块不解析任何正文，也绝不猜。这是本文件里最重要的一条自我约束——一个会猜日期的函数是这个文件可能出现的最糟的东西。

结果就是第 6 节那个待裁决项。

---

## 4. Lane

`mission_catalyst_lane.MissionCatalystLaneCoordinator`，无队列（照 model spec / market price lane）：

- **每天每公司一次，这就是全部节奏。** 计划的 daily-tracking 表把「SEC 8-K / 财报日历」列为**每日、固定**，这条 lane 照字面执行：五家公司一天五次调用。一个财报日期公布一次之后三个月不变，问更勤只是在花市场层赖以运转的那点善意。
- 失败的运行**不计入「今天问过了」**——一次瞬时错误不该让日历少一天。连续失败 3 次的公司让出槽位（进程内）。
- 授权：`autonomy.may_write` 里要有 **`observation`**（已在 `AUTOMATION_WRITE_SCOPES` 里，`coverage_mission.py` 未改）。没有就每 tick 返回 `ungranted` 且不起孩子。选 `observation` 而不是 `market_event`，是因为后者留给 P11d 的 `MarketEvent`。
- 开关是治理文件本身：`{state}/connector-governance/yfinance-calendar-v1.json` 存在就装，不存在整条 lane 不装。
- 不调任何模型，因此不碰 `cockpit_model.PURPOSES`、`model_configurations`、`raise_day_budget_cap`。

注册（`LaneSpec`，order **87**，driver key `mission_catalyst_calendar`，init kwarg `catalyst_calendar_launcher`）。推导结果已核验：

```
85 dispatch_mission_market_prices     mission_market_prices     market_price_launcher
87 dispatch_mission_catalyst_calendar mission_catalyst_calendar catalyst_calendar_launcher
90 dispatch_company_model_spec        company_model_spec        model_spec_launcher
```

`dispatch_mission_catalyst_calendar` 出现在 `writer_server.CORE_OPERATIONS` / `CORE_DISCOVERY_OPERATIONS` / `OPERATION_FIELDS`（空集）里，`mission_catalyst_calendar` 出现在 driver 的 tick 顺序里。

**动过的一条别人家的测试**：`tests/test_mission_market_price_lane.py::RegistrationTests::test_it_runs_between_the_statements_and_the_model_specification` 原来断言的是「紧挨着」，被放宽为「在前 / 在后」，理由与 Wave 0 把注册表迁移检查从「等于」放宽为「包含」是同一条：它钉住的真实决定是「价格在 filing 之后、在要拿它来读的规格之前」，「而且中间永远不许有别的」从来不是那个决定，却让此后每一条落在这个区间的 lane 变成别人的失败测试。原因写在测试里。

---

## 5. 集成时要接的线

### 5.1 `record_event`（P14a）

lane **不 import** `research_event`。`MissionCatalystLaneCoordinator(record_event=...)` 收一个可调用对象；`dispatch()` 现在从 `getattr(server, "record_research_event", None)` 取。集成时把它绑上：

```python
# writer_server 侧（或 mission_catalyst_lane.dispatch 里那一行）
coordinator = MissionCatalystLaneCoordinator(
    authority=CatalystCalendarAuthority(server.store),
    launcher=launcher,
    mission=mission,
    record_event=server.research_events.record_event,   # P14a 的入口
)
```

期望签名（按计划第 3 节的合同）：

```python
record_event(company_ref=..., kind="calendar", occurred_at=..., source_refs=[...], payload={...})
```

`payload` 里有 `event_key`（确定性的：`hash(company_ref, entry_ref, window, expected_date)`）、`window ∈ {preview, calibration, date_change}`、`entry_ref`、`event_kind`、`subject`、`expected_date`、`confidence`、`disagreement`、`disagreeing_dates`、`days_until`、`as_of`。`source_refs` 是该条目所有来源的 ref 加上日历版本 ref。

**没接上时不会静默丢**：tick summary 里 `settled.events.status == "events_unwired"`，并把本该发出的事件列出来。谁都不用几个月后才发现窗口一直开进了虚空。

**去重**：`emit_calendar_events(is_emitted=...)` 收一个谓词。现在由 coordinator 的进程内 `set` 提供，所以 preview 窗口开着的三十天里只发一次；`event_key` 带日期，所以日期真动了窗口会重开。**集成时建议把 `is_emitted` 换成对事件账本的持久查询**（按 `payload.event_key`），否则 writer 重启会重发一次。这是目前唯一一个「进程内状态承担了本该持久的职责」的地方，故意留成一个参数。

### 5.2 `install.sh` 种子

本片不改 `install.sh`（越界）。要加的块，照 `sec-company-facts-v3` 的写法：

```bash
# C1: the catalyst calendar reads Yahoo's diary for the covered companies. Its
# own record, because a schema hash binds one operation and an approval to read
# prices is not an approval to read anything else Yahoo serves. Seed once as
# *proposed*; the owner approves in place with
# `dalton-connector-governance approve`. Until then the lane is not installed.
yfinance_calendar_file="$governance_dir/yfinance-calendar-v1.json"
if [[ ! -f "$yfinance_calendar_file" && -f "$repo_root/deploy/connector-governance/yfinance-calendar-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/yfinance-calendar-v1.json" "$yfinance_calendar_file"
  chmod 600 "$yfinance_calendar_file"
fi
```

注意：`install.sh` 里现在**一条 yfinance 记录都没有种**（`grep yfinance deploy/macos/install.sh` 无输出），P11a 的 `yfinance-daily-prices-v1.json` / `yfinance-analyst-estimates-v1.json` 也还没接。三条一起加最省事。

记录事实：

| 字段 | 值 |
| --- | --- |
| `id` | `connector-governance:yfinance-calendar:v1` |
| `capability_id` | `capability:dalton:connector:yfinance-calendar` |
| `content_hash`（proposed） | `74f37a5aa7912d850983746e2eafdebedfd3abf26ef8e927df9be86a9bac6037` |
| `expected_source_hash` | `2f7c231d55f3667ed7cf5851f107eaf931010b7bbda8e584209f4f28796d919f`（与另两个 yfinance 操作共享） |
| `expected_schema_hash` | `bbf9c9dcf34e5f8b5cc9b84457aa5999e069fc72521266d36537125d460d10ea` |

### 5.3 mission 版本

要发一版把 `observation` 留在 `autonomy.may_write` 里（live mission 已经有了——`p9a` manifest 授的七个写入范围里就有它）。也就是说**这条 lane 不需要新的 mission 版本**，只要治理记录被批准。这是本片唯一一个不用等 owner 发 mission 的 lane。

### 5.4 打包

`catalyst_calendar_schema.sql` 由 Wave 0 的 `*_schema.sql` 通配符覆盖，`pyproject.toml` 不用改。`tests/fixtures/market/acn-calendar.json` 是测试资源，不进包。

### 5.5 cockpit

`upcoming(now, horizon_days)` 与 `next_catalyst(company, now)` 就是 vision 里「公司卡：下一个催化剂 T−N 天」要的两个读法，返回值里已经有 `days_until` / `confidence` / `disagreement`。cockpit 接线按计划统一由 Wave 2 的专门 agent 做，本片没碰 `cockpit_*`。

---

## 6. Smoke：五家公司，只读

`--allow-network` 只对 yfinance；SEC 那一半是把 live 已落盘的五份 submissions 产物**只读复制**到临时 state 里再读（零 SEC 网络调用）。临时目录 `/tmp/c1-smoke-five`，live 状态未被写过。

```
PASS 1 -- Yahoo only (network)
  ACN   succeeded calendar=fresh        yahoo=['2026-10-01'] sec=not_requested/0 next=2026-10-01 entries=1
  CTSH  succeeded calendar=fresh        yahoo=['2026-10-28'] sec=not_requested/0 next=2026-10-28 entries=1
  EPAM  succeeded calendar=fresh        yahoo=['2026-11-05'] sec=not_requested/0 next=2026-11-05 entries=1
  IBM   succeeded calendar=fresh        yahoo=['2026-10-21'] sec=not_requested/0 next=2026-10-21 entries=1
  DXC   succeeded calendar=fresh        yahoo=['2026-10-29'] sec=not_requested/0 next=2026-10-29 entries=1

copied 5 spooled SEC submissions artifacts (read-only)

PASS 2 -- Yahoo plus the company's own Item 2.02 8-Ks (no new SEC call)
  ACN   succeeded calendar=fresh        yahoo=['2026-10-01'] sec=read/1 next=2026-10-01 entries=2
  CTSH  succeeded calendar=fresh        yahoo=['2026-10-28'] sec=read/1 next=2026-10-28 entries=2
  EPAM  succeeded calendar=fresh        yahoo=['2026-11-05'] sec=read/1 next=2026-11-05 entries=2
  IBM   succeeded calendar=fresh        yahoo=['2026-10-21'] sec=read/2 next=2026-10-21 entries=2
  DXC   succeeded calendar=fresh        yahoo=['2026-10-29'] sec=read/1 next=2026-10-29 entries=2

THE CALENDAR AS IT STANDS
  ACN   next=2026-10-01 (estimated, T-22) entries=2 confirmed_past=['2026-06-18']
  CTSH  next=2026-10-28 (estimated, T-49) entries=2 confirmed_past=['2026-07-29']
  EPAM  next=2026-11-05 (estimated, T-57) entries=2 confirmed_past=['2026-08-06']
  IBM   next=2026-10-21 (estimated, T-42) entries=2 confirmed_past=['2026-07-22']
  DXC   next=2026-10-29 (estimated, T-50) entries=2 confirmed_past=['2026-07-30']

  upcoming(45d):
    2026-10-01 earnings     estimated T-22  company:sec-cik:0001467373
    2026-10-21 earnings     estimated T-42  company:sec-cik:0000051143
```

ACN 的 10/1 与蓝图里「ACN 10/1 业绩实战」对上了。

IBM 那条 `sec=read/2` 值得单独看一眼——两份 Item 2.02 落在同一个季度，日历没有挑一个丢一个：

```
2026q3 2026-07-22 confirmed disagreement=True ['2026-07-14', '2026-07-22']
    filing 2026-07-22 sec:filing:0000051143-26-000077
    filing 2026-07-14 sec:filing:0000051143-26-000070
2026q4 2026-10-21 estimated disagreement=False []
    connector_invocation 2026-10-21 connector-invocation:yfinance:1d4992782b293610b0
```

DXC 那条 2020-03-23 的除息日**一条条目都没生成**，这是设计。

### 6.1 待裁决：现在一个 P14f 窗口都开不了

五家公司的下一次财报**全是 `estimated`**，因为没有任何公司发过的东西确认过这些日期，而计划写的是「只有 confirmed 驱动 preview」。ACN 的 10/1 已经在 T−22，落在 preview 窗口里，但按规则不发事件。

也就是说：**P14f 的 preview 在 8-K 正文读取器落地之前不会开火。** 两条路，请 owner 选：

1. **接受，先把 8-K 正文那条路打通**：在 discovery plan 里加一条 `form: 8-K`（一个 plan 版本 + 一个 spec_ref），让 fetch lane 能抓公告正文，再加一个窄到只认「will report ... on \<date\>」的日期读取器，把结果喂进 `announced_next_date_entry`。这是「正确但要一片工作」。
2. **放宽**：允许 `estimated` 的日期开 preview，但报告抬头必须写明日期是 vendor 估计的、以及它上一次改动是什么时候。这是「今天就能跑，但有把四周注意力押在猜测上的风险」——ACN 的 10/1 从 live 数据看非常可能是对的（Yahoo 的 `earningsTimestampStart == earningsTimestampEnd`，说明它不是一个窗口）。

代码两条都支持：窗口判据集中在 `emit_calendar_events` 里一个 `entry["confidence"] == "confirmed"` 的判断上，放宽是一行加一个理由。**本片按计划的字面实现了第 1 条**（严格），没有替 owner 做这个决定。

---

## 7. 验收

```
Ran 2617 tests in 320.331s
OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`；基线 main `2fa5934` 是 2,512 项，本片新增 105 项。）

各文件：

```
tests/test_catalyst_calendar.py          Ran 34 tests   OK
tests/test_yfinance_calendar_adapter.py  Ran 15 tests   OK
tests/test_sec_earnings_release.py       Ran 14 tests   OK
tests/test_catalyst_calendar_cli.py      Ran 15 tests   OK
tests/test_mission_catalyst_lane.py      Ran 27 tests   OK
```

覆盖到的行为，按交付要求逐条：

| 要求 | 测试 |
| --- | --- |
| estimated vs confirmed | `ConfidenceTests`（含「vendor 不能被贴 confirmed」「filing 不能被贴 estimated」两条拒绝） |
| 不一致的处理 | `DisagreementTests`（两个日期都留、confirmed 胜、不取平均、重读不算第二个证人、同季两份 filing 是两次公告、旧读数不能把日历往回推） |
| 日期挪动 → 新版本带理由 | `VersionTests::test_a_moved_date_is_a_new_version_with_driver_event`（并核验旧日期在链上一跳处） |
| estimated → confirmed | `test_estimated_becoming_confirmed_is_evidence_thicker` |
| duplicate | `test_the_same_reading_twice_is_a_duplicate`、CLI 的同名一条 |
| `change_reason` 不能被贴错 | `test_a_reason_the_diff_contradicts_is_refused`、`test_the_change_reason_vocabulary_is_adr_0008s`（钉住 = `model_forecast_driver.CHANGE_REASONS`） |
| next / upcoming | `ReaderTests` 六条 |
| 事件窗口 | `EventWindowTests` 九条（T−30 边界、T+0..T+2 边界、estimated 不开 preview、除息不开 preview、开着的窗口只发一次、日期一动窗口重开） |
| lane 注册（新解释器） | `RegistrationTests::test_importing_this_module_does_not_pull_in_the_writer`（subprocess 实跑），另加 order / driver key / argv / unconfigured 四条 |
| yfinance 适配器（stub） | `FetchTests`（`sys.modules["yfinance"]` 换成 stub；`date` 对象要能过 canonical JSON；来源拒绝是「有理由的缺席」不是崩溃） |

**全量套件离线。** 唯一一次网络是抓 `tests/fixtures/market/acn-calendar.json` 这个 fixture，以及第 6 节的 smoke，两者都在套件之外。CLI 测试从不传 `--allow-network`，`run()` 自己也会拒绝两个模式都没选的 Namespace。

---

## 8. 没做什么

- **没有读 8-K 正文**，因此没有公司确认的未来日期。第 3.3 与 6.1 节。
- **没有 guidance / investor_day / filing_due 的产出方。** 三个 `event_kind` 在词表里、能被写入、能开窗口，但目前没有来源填它们。`filing_due` 尤其可惜——10-Q 的法定截止日是可以从 filing 期末加规则算出来的，但那是**推算**，本片不做推算，要做得是另一个显式的、有自己规则冻结的东西。
- **没有 cockpit 接线**（越界；读者接口已就位）。
- **没有把 `is_emitted` 做成持久查询**（要等 P14a 的事件账本，见 5.1）。
- **没有 `filings.files` 的翻页**：`filings.recent` 是大约一年 / 一千条的窗口，更老的 8-K 在 `filings.files` 里，而这套系统对那个没有抓取权限。对一个 120 天保留期的日历来说够用。

---

## 9. 开放问题

1. **6.1 的裁决**：preview 等 8-K 正文读取器，还是允许 estimated 开窗（带标注）？——需要 owner。
2. **`subject` 的季度近似**。跨季边界的改期会产生两条并列条目而不是一次改期。真正的修法是拿到财季（10-Q 的 `fiscal_period`，或 8-K 正文里的「fourth-quarter」），两者都要更多输入。目前的近似方向是安全的（重复而不是错误合并），但值得在 dossier 那一层拿到财季之后回来收掉。
3. **8-K 行不在 `source_record_refs` 里**这件事。本片的立场是「引用一个日期」和「取一份文档」是两件事，前者靠 accession + `items` + artifact hash 就够，后者必须走 URL authority 而那条路正确地拒绝了。如果集成时认为连引用日期也该要求该行在 envelope 里，那就得在 discovery plan 里加 `form: 8-K` 的 spec——同 6.1 的路径 1，两件事会一起解决。
4. **`acceptanceDateTime` 没有时区**。SEC 的字段是东部时间不带偏移；本片只在它自带 `Z` 时用它，否则用 `filingDate` 的午夜 UTC。这个值只用于给同一来源的两条陈述排序，`filingDate` 排得对，但如果以后有人拿它当墙上时钟用，这里要先改。
5. **DXC 的 `company_ref` 是九位**（`company:sec-cik:001688568`），别处已经记过这个疤。本片的 `issuer_for()` 原样返回、`submissions_artifacts()` 用 `lstrip("0")` 比对，两条都有测试；但这个疤该由谁来收，仍然没人认领。
