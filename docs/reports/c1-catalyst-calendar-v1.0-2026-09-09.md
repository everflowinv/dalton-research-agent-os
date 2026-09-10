# C1 事件日历交付报告 v1.0

日期：2026-09-09
分支：`c1-catalyst-calendar`（worktree `~/Projects/dalton-c1-catalyst-calendar-worktree`）
分叉基线：main `2fa5934`（v1.0 主体）；事件桥修复在分支 `c1-event-bridge`，基线 main `3f21dbe`
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 C1 与「Daily tracking」一节、[vision 回顾](vision-review-against-plan-v1.0-2026-09-09.md) C1、蓝图 §5.2 P14f、ADR-0008、[P11a 市场层](p11a-market-layer-v1.0-2026-09-09.md)
全量测试：见第 7 节，原文粘贴

---

## 0. 一句话

Dalton 现在知道每家覆盖公司下一次开口是什么时候，也知道**谁这么说的**：yfinance 的日历给出 `estimated` 的日期，公司自己的 8-K Item 2.02 给出 `confirmed` 的日期，两者不一致时两个日期都留着、都带各自的 confidence、永不取平均；日期一动就出新版本带 `driver_event`，estimate 变 confirmed 出新版本带 `evidence_thicker`。P14f 的两个窗口由这条日历触发：**T−30 的 preview 不等确认**（分析师本来就是照预期日期备稿的），但事件与所有渲染它的地方都带着 `date_confidence` 与「日期未确认」的字样；公司确认之后再发一次，让判断层重新排期。**T+0..T+2 的 calibration 只认 confirmed**——那是对着一次已经发生的发布写的。ACN 的 10/1 今天就在窗口里，第 6 节有它触发的样子。

---

## 1. 交付清单

| 文件 | 内容 |
| --- | --- |
| `src/dalton_core/catalyst_calendar.py` + `catalyst_calendar_schema.sql` | `CatalystCalendarVersion` 权威：append-only、`content_hash`、三触发器、读回校验、`next_catalyst` / `upcoming`；以及 P14f 窗口的事件发射器 |
| `src/dalton_core/yfinance_calendar_adapter.py` | yfinance `calendar` 操作的取数与归一化，以及它变成的日历条目 |
| `src/dalton_core/sec_earnings_release.py` | 8-K Item 2.02 探测器：读**已经落盘**的 SEC filings index 原始产物，零网络调用 |
| `src/dalton_core/catalyst_calendar_cli.py` | 子进程：approval first → artifact always → contract last；两半互不牵连 |
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
- **`entry_ref` 是「哪一次事件」而不是「哪一天」**：`hash(company_ref, event_kind, anchor_date)`，`anchor_date` 是这次事件**第一次被看到的日期**，此后永不改变（`expected_date` 会变）。
- **「哪一次事件」由权威在合并时判定，不由来源给。** 两条陈述属于同一次事件，当且仅当日期相差不超过 `SAME_OCCURRENCE_DAYS = 45`（`_match()`，按 anchor 而不是按当前 `expected_date` 匹配，否则连续几次改期会让一条条目「走」出去）。45 这个数字坐在它要分开的两件事中间：一次改期是几天到两周；连续两次季报相隔 77 到 92 天（ACN 财年 Q4 在 10/1、Q1 在 12 月中，正好 77）。
- `subject` 现在**只是一个人看的标签**（`occurrence_label(anchor_date)` → `2026q4`），不是身份。它可以撞——ACN 一个日历 Q4 里报两次——所以任何要 key 的东西必须 key 在 `entry_ref` 上。

**这里改过一次，因为原来的写法两个方向都错。** 原来 `subject` 是日历季度，并且由来源映射器自己算：
- ACN 的财年 Q4（10/1）与 Q1（12 月中）同属日历 Q4 → 折成一条；filing 压过 vendor，已确认的 10/1 胜出，`expected_date` 成了过去日期，`next_catalyst()` 返回 `None`，**12 月那次 preview 根本不会开**，还附带一个假的 `disagreement`。
- 反过来，9/30 挪到 10/1 会被判成两次不同的事件。
- 现在两条都有测试钉住（`OccurrenceTests`）。教训写在 `SAME_OCCURRENCE_DAYS` 的注释里：**身份是权威的，一个能给事件命名的来源映射器就是一个能把两次事件合并掉的映射器。**

`sources[]` 每一项：

```
{kind ∈ {connector_invocation, filing}, ref, source_ref,
 observed_date, observed_at, confidence, note}
```

`confidence` 由 `kind` 推导并强制：`filing` → `confirmed`，其余 → `estimated`。调用方传错直接拒（`CatalystCalendarConflict`）。能给 vendor 贴 `confirmed` 的调用方就是能把两个来源的区别整个关掉的调用方，而这个区别是两个来源存在的全部理由。

**冻结的优先级**：`SOURCE_AUTHORITY = {"filing": 2, "connector_invocation": 1}`，同级按 `observed_at` 倒序、再按 ref。条目的 `expected_date` 与 `confidence` 取胜者的；输的那个日期留在 `sources[]` 里，`disagreement` 与 `disagreeing_dates` 把不一致直接说出来。**没有任何一处做平均。**

**一条压过优先级的规则**：**一个说「已经发生过」的来源，不能决定另一个来源说「还没到」的那次事件的日期**（`resolution: forthcoming_over_past`，写在条目里）。这发生在两条陈述近到算同一次事件、却分处今天两侧的时候：三周前一份已确认的发布，旁边是 vendor 已经滚到下一次的估计。filing 压过 vendor，而且它没说错任何事——它在回答另一个问题。让它赢的后果是 `expected_date` 变成过去日期，这次事件直接从 `next_catalyst` 里消失，preview 被静默取消。那条过去的陈述仍在 `sources[]` 与 `disagreeing_dates` 里，只是不再有资格说下一次是什么时候。全都是过去的条目不受影响，仍取已确认的发布日——那正是 calibration 要的。

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

两个读者的返回值里都带 `date_unconfirmed` 与 `date_caveat`（未确认时是 `"日期未确认"`，确认后是空串）。**注意语是被携带的，不是让每个消费者自己推的**：读这两个读者的东西全都是要把一个日期摆到人面前的东西，而「T−22 天」摆在 vendor 猜的日期旁边和摆在公司宣布的日期旁边，长得一模一样。

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
- **`earnings_dates` 是数组，因为 Yahoo 拿不准时会给一个窗口的两端。** 塌缩成一天就是替来源发明了它没给的精度。**但一个窗口是一次事件，不是两次**：条目层只出**一条**条目、一个来源、取**较早**那一端（日历是用来提前准备的，早准备不花钱），区间写进 `notes`。
  - 这里原来一个日期出一条条目，错了两次：把公司只会开一次的电话会摆成两次；而且两条条目带的是同一个 vendor，会在权威里折回同一次事件、同一个来源出现两次——**权威直接拒收，于是这家公司每天跑失败，三次之后被 lane 挂起，再也不问了**。有测试（CTSH 形状的两日窗口，跑到发布为止）。
- **过去的除息日不是将来的事件。** Yahoo 的 `Ex-Dividend Date` / `Dividend Date` 是它最后知道的那一次，不是下一次：live 抓下来 DXC 还报着 2020-03-23，停止分红六年之后；ACN 的是两个月前。wire 原样保留（产物就是那次调用），**条目层只在日期晚于观测日时才生成 `ex_dividend` 条目**。有测试钉住 DXC 那个真实的数。
- 每个日期都是 `estimated`。Yahoo 不说日期从哪来，来源是「某个 vendor 有这个数」的日期不是公司承诺过的日期。
- **先定时区再取日期。** `2026-10-01T23:30:00-07:00` 在 UTC 是 10 月 2 日；直接切前十个字符会把财报日提前一天，在月末还会连带把整条条目的季度标签也标错。不带时区的时间戳按它印出来的那天取，不假设成 UTC——那是在发明一个时区。

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
- **字节哈希对不上不是「没有」。** 本模块自己校验产物哈希（`read_submissions_artifact`），把「不在盘上」（`absent`）、「哈希对不上」（`tampered`）、「不是 JSON」（`unreadable`）分成三个词。`mission_sec_quarters.read_artifact` 三种都返回 `None`，对它自己的调用方没问题，对这里不行：哈希对不上是这条路径上唯一一种「已存的引用不再指向它自称的东西」的情况，必须以一句人能读的拒绝出现，而不是混进「这家公司还没跑过 discovery」。
- 探测器**取的是日期，不是文档**。那条 8-K 在字节里但不在该 invocation 的 `source_record_refs` 里（那次调用要的是 10-K），所以它不能被变成 filing URL 去抓——`sec_filings_index.build_sec_filing_url_authorities` 正是拒绝这件事的，而且拒得对。日期与文档是两件不同的请求，这里只提第一件。

### 3.2.1 两半独立失败

子进程先跑 filing 那一半，再跑 vendor 那一半，vendor 那一半整个包在自己的 `try` 里。**两个独立来源要么独立失败，要么就不是两个来源。**原来 SEC 那一半排在 wire 校验之后，于是 Yahoo 返回一个契约描述不了的东西，就把整次运行——连同一个跟 Yahoo 毫无关系、一次调用都没花的、从 filing 读出来的已确认日期——一起丢掉了。

结果多出一个状态词：

| `status` | 含义 | lane 的反应 |
| --- | --- | --- |
| `succeeded` | 两半都读到了（或 vendor 读到而 SEC 本就没被要求） | 发事件、记「今天问过」、清零失败计数 |
| `partial` | filing 那一半发布了，vendor 那一半失败 | 发事件、记「今天问过」，**同时记一次失败** |
| `failed` | 两半都没产出 | 不记「今天问过」，记一次失败 |

`partial` 两边都记，是因为两件事都真：日历确实学到了东西（今天再问一次不会修好 Yahoo），而一个持续坏掉的 vendor 不该躲在一个「基本还能用」的日历后面——连着三天 `partial`，这家公司被挂起，理由里写着 vendor 的原话。治理记录没批也走这条路：approval 仍然在碰网络之前检查（`artifact` 是 `null`），只是不再把 filing 那一半一起否掉。

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

**order 87 与 P14a 撞号**：注册表会拒绝重号（这是它该做的），主 agent 已定 C1 留在 87、P14a 合并时改号。本片不动。

**动过的一条别人家的测试**：`tests/test_mission_market_price_lane.py::RegistrationTests::test_it_runs_between_the_statements_and_the_model_specification` 原来断言的是「紧挨着」，被放宽为「在前 / 在后」，理由与 Wave 0 把注册表迁移检查从「等于」放宽为「包含」是同一条：它钉住的真实决定是「价格在 filing 之后、在要拿它来读的规格之前」，「而且中间永远不许有别的」从来不是那个决定，却让此后每一条落在这个区间的 lane 变成别人的失败测试。原因写在测试里。

---

## 5. 集成时要接的线

### 5.1 `record_event`（P14a）——已接上

lane 仍然**不 import** `research_event` 到模块层：`MissionCatalystLaneCoordinator(record_event=...)` 收一个可调用对象，测试传 fake。真正的绑定在 `dispatch()` 里，和它建 `CatalystCalendarAuthority` 是同一个写法：

```python
from .research_event import ResearchEventAuthority, record_event

events = ResearchEventAuthority(server.store)

def record(**event):
    current = mission()
    return record_event(events, mission=current,
                        actor_ref=current["autonomy"]["automation_principal"],
                        **event)
```

**这里原来是错的，而且错得不显眼。** 原来写的是 `getattr(server, "record_research_event", None)`——writer 上从来没有过这个属性，所以它永远是 `None`，lane 一辈子每个 tick 都报 `events_unwired`。那是一个长得跟接线一模一样的兜底。现在按注册表里别的 lane 的写法建权威，这种写法不可能「不小心缺席」。

**payload 的字段集由 P14a 的模块说了算，不由本片说了算。** `research_event.validate_payload` 拒绝未声明的字段，也拒绝任何嵌套值（它只收 text / int / bool / null）。对照之后：

| 原来发的 | 处置 |
| --- | --- |
| `event_kind`、`expected_date`、`calendar_version_ref`、`source_ref` | 本来就在声明里 ✓ |
| `window`、`entry_ref`、`date_confidence`、`disagreement` | **加进 `PAYLOAD_FIELDS["calendar"]`**（见下） |
| `confirmed` | 声明里本来就有，本片开始产出它——cockpit 早就在渲染它了 |
| `disagreeing_dates` | 是个 list，`validate_payload` 不收嵌套。**去掉**；布尔留着，要看日期就顺着 `calendar_version_ref` 去读完整版本 |
| `days_until`、`as_of` | 派生量，而且每天都变。**去掉**（理由见去重） |
| `event_key`、`subject`、`anchor_date`、`date_unconfirmed`、`date_caveat` | **去掉**：分别与账本自己的身份、`entry_ref`、`confirmed` 重复 |

最终 payload（`catalyst_calendar.calendar_event_payload`）九个字段，与 `PAYLOAD_FIELDS["calendar"]` 完全相等，有一条两边都 import 的测试钉住（`EventContractTests`）。**这两个模块之前正是因为各自只对着对方的 fake 测试，才能一边发九个字段、一边只声明五个，而没有一条红测试。**

**加宽是安全的，前提核验过了**：加宽一个 payload 契约只在「旧契约下还没写过任何东西」时安全，否则旧行会在新字段集下通过校验、读起来像是它对新字段「什么都没说」。核验方式：把 `/private/tmp/dalton-ro/core.sqlite` 复制到 `/tmp` 后查询——**live Core 上根本没有 `research_events` 这张表**，任何 kind 的事件都一条没写过，更不用说 calendar。

`confirmed` 与 `date_confidence` 是同一件事的两种写法：前者是 P14a 的词、cockpit 已经在用它印「日期未确认」，后者是计划要求事件携带的、也是将来出现第三种取值时需要的。两者在 `calendar_event_payload` 里由同一个值派生，不可能对不上，有测试。另有一条测试把本模块的 `UNCONFIRMED_DATE_CAVEAT` 钉死等于 cockpit 源码里印出来的那串字，免得一句注意语裂成两句。

### 5.1.1 窗口规则（2026-09-09 下午，主 agent 按分析师实践裁定）

| 窗口 | estimated | confirmed |
| --- | --- | --- |
| `preview`（T−30，`0 < days_until ≤ 30`） | **开**，`confirmed: false` + `date_confidence: estimated` | 开 |
| `calibration`（T+0..T+2） | **不开** | 开 |
| `date_change` | 开 | 开 |

- **preview 不等确认**，因为分析师就是这么工作的：等公司确认，要准备的那个月已经过掉大半了。代价是偶尔照一个后来会挪的日期备了稿，而对策是把日期是哪一种说出来，不是不干活。
- **确认本身是第二个事件**，而且现在是**账本**让它成为第二个事件的：确认改变了 payload（`confirmed` 与 `date_confidence` 都变），所以第二次写回 `fresh` 而不是 `duplicate`。有端到端测试（真 store，两行）。
- **calibration 永远只认 confirmed。** 它是对着一次发布写的；estimated 的日期只说明「大概率会报」，不说明「报过了」。对一场没人开过的电话会做校准不是薄，是错。
- 判据集中在 `emit_calendar_events` 里的 `UNCONFIRMED_DATE_WINDOWS`（`{preview, date_change}`）一个常量上。

**去重是账本的，不是本片的。** 事件身份是 `(company_ref, kind, payload_hash)`，而 payload 里不带任何随天变化的字段，所以一个开着的窗口每天早上算出同一个 payload，账本回 `duplicate` 且什么都不写。原来这里有一个进程内的 `set` 兼一个 `event_key`——那是对一个已经有答案的问题给的第二个答案，而且是更差的那个：重启就忘光，部署后第一个 tick 会把所有开着的窗口重记一遍。两者都删掉了。`days_until` / `as_of` 仍然在 `emit_calendar_events` 的返回行上（tick summary 要看），只是不在 payload 里。

**两个 grant，不是同一个。** 发布日历要 `observation`；把日历开出来的窗口记进账本是写 P14a 的账本，要的是**它的** scope `market_event`（由账本自己校验）。**live mission 现在只有前者**（`may_write` 里有 `observation`、没有 `market_event`），所以在 owner 发新版 mission 之前，日历照常维护、事件一条都不会写，tick 报 `events_ungranted` 并把原因带上——这是缺一个授权，不是 lane 坏了，因此**不消耗这家公司的重试预算**。有测试。

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

`--allow-network` 只对 yfinance；SEC 那一半是把 live 已落盘的五份 submissions 产物**只读复制**到临时 state 里再读（零 SEC 网络调用）。临时目录 `/tmp/c1-smoke-six`，live 状态未被写过。每家公司一次运行，两个来源一起跑。

```
ONE RUN PER COMPANY -- Yahoo (network) plus the filings already held
  ACN   succeeded vendor=read    yahoo=['2026-10-01'] sec=read/1 entries=2 next=2026-10-01
  CTSH  succeeded vendor=read    yahoo=['2026-10-28'] sec=read/1 entries=2 next=2026-10-28
  EPAM  succeeded vendor=read    yahoo=['2026-11-05'] sec=read/1 entries=2 next=2026-11-05
  IBM   succeeded vendor=read    yahoo=['2026-10-21'] sec=read/2 entries=2 next=2026-10-21
  DXC   succeeded vendor=read    yahoo=['2026-10-29'] sec=read/1 entries=2 next=2026-10-29

ENTRIES PER COMPANY
  ACN   2026-06-18 earnings     confirmed anchor=2026-06-18 label=2026q2 disagreement=False resolution=highest_authority
  ACN   2026-10-01 earnings     estimated anchor=2026-10-01 label=2026q4 disagreement=False resolution=highest_authority
  CTSH  2026-07-29 earnings     confirmed anchor=2026-07-29 label=2026q3 disagreement=False resolution=highest_authority
  CTSH  2026-10-28 earnings     estimated anchor=2026-10-28 label=2026q4 disagreement=False resolution=highest_authority
  EPAM  2026-08-06 earnings     confirmed anchor=2026-08-06 label=2026q3 disagreement=False resolution=highest_authority
  EPAM  2026-11-05 earnings     estimated anchor=2026-11-05 label=2026q4 disagreement=False resolution=highest_authority
  IBM   2026-07-22 earnings     confirmed anchor=2026-07-22 label=2026q3 disagreement=True  resolution=highest_authority
  IBM   2026-10-21 earnings     estimated anchor=2026-10-21 label=2026q4 disagreement=False resolution=highest_authority
  DXC   2026-07-30 earnings     confirmed anchor=2026-07-30 label=2026q3 disagreement=False resolution=highest_authority
  DXC   2026-10-29 earnings     estimated anchor=2026-10-29 label=2026q4 disagreement=False resolution=highest_authority

WINDOWS OPENING TODAY
  ACN   preview      2026-10-01 T-22  estimated "日期未确认"
```

ACN 的 10/1 与蓝图里「ACN 10/1 业绩实战」对上了。

三个值得单看的地方：

- **IBM 的 `sec=read/2` 与 `disagreement=True`**：两份 Item 2.02 落在同一次事件（7/14 与 7/22，相距 8 天，在 45 天以内），日历没有挑一个丢一个，两个 accession 都在 `sources[]` 里，条目取较晚那次并把不一致说出来。
- **每家的两条条目 anchor 不同、label 有的相同**：`2026q3` / `2026q4` 这些标签只是给人看的；身份是 anchor。ACN 今年 12 月中还会报一次，那次的 label 也是 `2026q4`——现在它会是第三条独立的条目，而不是把 10/1 那条覆盖掉。
- **DXC 那条 2020-03-23 的除息日一条条目都没生成**，这是设计。

### 6.1 现在会触发的窗口

只有 ACN 落在 T−30 里；其余四家在 T−42 到 T−57，还没到。ACN 的日期是 Yahoo 给的估计，所以 preview 照开，事件带着 `date_confidence: estimated` 与「日期未确认」。等 ACN 发出那份 Item 2.02 的 8-K，日历出新版本带 `evidence_thicker`，同一个 preview 窗口以 `confirmed` 再发一次，判断层可以据此把「照估计日期备的稿」升级为「照确认日期投入」。T+0..T+2 的 calibration 要等那份 8-K，这是设计。

同一天再问一次不会多写一行——payload 里没有随天变化的字段，账本按 `(company_ref, kind, payload_hash)` 认出它并回 `duplicate`。

**10 月之后那一段，有测试端到端钉住**（`OccurrenceTests::test_the_whole_accenture_autumn_end_to_end`）：10/1 的已确认发布入库之后，Yahoo 给出 12/17 的估计 → 两条条目而不是一条，`next_catalyst` 在 11 月中返回 12/17（`estimated`，T−32），preview 在 11/17 也就是 T−30 那天开火。这正是原来那个 occurrence key 会静默吞掉的一整次财报。

## 7. 验收

```
Ran 4126 tests in 595.982s
OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，在 `c1-event-bridge` 分支、main `3f21dbe` 之上。本片自己的五个测试文件共 141 项。）

各文件：

```
tests/test_catalyst_calendar.py          Ran 52 tests   OK
tests/test_yfinance_calendar_adapter.py  Ran 19 tests   OK
tests/test_sec_earnings_release.py       Ran 16 tests   OK
tests/test_catalyst_calendar_cli.py      Ran 19 tests   OK
tests/test_mission_catalyst_lane.py      Ran 36 tests   OK
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
| 事件窗口 | `EventWindowTests` 十一条（T−30 边界、T+0..T+2 边界、**estimated 开 preview 且带 `date_caveat`**、**estimated 三天都不开 calibration**、**确认之后同一窗口再发一次**、除息不开 preview、开着的窗口只发一次、日期一动窗口重开），加 lane 侧两条（`test_an_estimated_date_opens_a_labelled_preview_from_the_lane_too`、`test_the_tick_strip_carries_the_caveat_an_operator_needs_to_see`） |
| 注意语被携带而非推导 | `ReaderTests::test_a_reader_is_handed_the_caveat_rather_than_asked_to_derive_it` |
| **同一次事件 vs 两次事件** | `OccurrenceTests` 八条：相隔一季是两条条目、ACN 秋季端到端（10/1 已确认 + 12/17 估计 → 两条、`next_catalyst` 给 12/17、preview 在 T−30 开火）、窗口内改期是一次 move 且 anchor 不动、跨季一天的滑动仍是同一次、45 天坐在两种情况中间、一次运行的两半互相找到对方、过去的来源不能决定还没到的日期、全是过去的条目仍取已确认的发布日 |
| **Yahoo 的两日窗口** | `EntryTests::test_a_window_is_one_entry_dated_at_its_earlier_end`、`test_a_windowed_run_publishes_rather_than_failing`（CTSH 形状，跑到发布） |
| **两半独立失败** | `PublishingTests` 四条：畸形 vendor 响应不丢 filing 那一半（`status: partial`）、治理未批也一样、两半都空才是 `failed`、同一天的两半落在同一次事件上 |
| **写一条记一条** | `EventTests::test_a_writer_that_fails_halfway_does_not_lose_what_it_wrote`；`partial` 两条（照发事件并记「今天问过」、连三天挂起且理由是 vendor 的） |
| **事件契约（两边都 import）** | `EventContractTests` 七条：产出的字段集 == `PAYLOAD_FIELDS["calendar"]`、三个窗口 × 两种 confidence × 两种 disagreement 都过 `validate_payload`、payload 不含嵌套、不含随天变化的字段、`confirmed` 与 `date_confidence` 不会对不上、`calendar` 是账本认识的 kind、注意语与 cockpit 印的那串字相等 |
| **端到端接线（真 store、真账本）** | `WiringTests` 四条：走 `dispatch()` → `research_events` 里真有一行 `calendar`（`evidence_tier: derived`、`actor_ref` 是 mission 的 automation principal、`date_confidence: estimated`）、第二天不写第二行、确认之后写第二行、calibration 那行是 confirmed；`UngrantedEventScopeTests` 一条：只给 `observation` 不给 `market_event` 时报 `events_ungranted` 且不消耗重试预算 |
| **产物哈希与时区** | `SpoolTests` 两条（`tampered` / `unreadable` 各自成词）、`WireTests` 两条（带时区先归一化、不带时区按印出来的那天取） |
| lane 注册（新解释器） | `RegistrationTests::test_importing_this_module_does_not_pull_in_the_writer`（subprocess 实跑），另加 order / driver key / argv / unconfigured 四条 |
| yfinance 适配器（stub） | `FetchTests`（`sys.modules["yfinance"]` 换成 stub；`date` 对象要能过 canonical JSON；来源拒绝是「有理由的缺席」不是崩溃） |

**全量套件离线。** 唯一一次网络是抓 `tests/fixtures/market/acn-calendar.json` 这个 fixture，以及第 6 节的 smoke，两者都在套件之外。CLI 测试从不传 `--allow-network`，`run()` 自己也会拒绝两个模式都没选的 Namespace。

---

## 8. 没做什么

- **没有读 8-K 正文**，因此没有公司确认的未来日期；preview 照估计日期开并标注，calibration 要等它。第 3.3 与 6.2 节。
- **没有 guidance / investor_day / filing_due 的产出方。** 三个 `event_kind` 在词表里、能被写入、能开窗口，但目前没有来源填它们。`filing_due` 尤其可惜——10-Q 的法定截止日是可以从 filing 期末加规则算出来的，但那是**推算**，本片不做推算，要做得是另一个显式的、有自己规则冻结的东西。
- **没有 cockpit 接线**（越界；读者接口已就位）。
- **没有用财季判定同一次事件**（见开放问题 2）。
- **没有 `filings.files` 的翻页**：`filings.recent` 是大约一年 / 一千条的窗口，更老的 8-K 在 `filings.files` 里，而这套系统对那个没有抓取权限。对一个 120 天保留期的日历来说够用。

---

## 9. 开放问题

1. **live mission 还没有 `market_event`。** 在 owner 发一版把它加进 `autonomy.may_write` 之前，这条 lane 会维护日历、报 `events_ungranted`、一条事件都不写。这不是缺陷，是缺一个授权；但它是「接上之后还差最后一步」的那一步，需要 owner。
2. **`date_caveat` 的字面。** 现在是硬写的 `"日期未确认"`（`UNCONFIRMED_DATE_CAVEAT`），与 `dashboard_projector` 里「结果待确认」同一路数。如果 cockpit 之后要走一套统一的文案表，这个常量是唯一要改的地方。
3. **占位判据是「45 天以内」，不是财季。** 现在两条陈述算不算同一次事件，靠的是日期距离；真正的判据是财季（10-Q 的 `fiscal_period`，或 8-K 正文里的「fourth-quarter」）。45 天在这五家公司的实际节奏上有很宽的余量（改期几天 vs 两季 77 天以上），但一家一年报十次的公司会把这个余量吃掉。等 dossier 那一层拿得到财季，`_match()` 应该先用财季、退化时才用距离——那是一个函数的改动，因为身份的判定已经收在权威里的一处了。
4. **8-K 行不在 `source_record_refs` 里**这件事。本片的立场是「引用一个日期」和「取一份文档」是两件事，前者靠 accession + `items` + artifact hash 就够，后者必须走 URL authority 而那条路正确地拒绝了。如果集成时认为连引用日期也该要求该行在 envelope 里，那就得在 discovery plan 里加 `form: 8-K` 的 spec——同 6.1 的路径 1，两件事会一起解决。
5. **`acceptanceDateTime` 没有时区**。SEC 的字段是东部时间不带偏移；本片只在它自带 `Z` 时用它，否则用 `filingDate` 的午夜 UTC。这个值只用于给同一来源的两条陈述排序，`filingDate` 排得对，但如果以后有人拿它当墙上时钟用，这里要先改。
6. **DXC 的 `company_ref` 是九位**（`company:sec-cik:001688568`），别处已经记过这个疤。本片的 `issuer_for()` 原样返回、`submissions_artifacts()` 用 `lstrip("0")` 比对，两条都有测试；但这个疤该由谁来收，仍然没人认领。
