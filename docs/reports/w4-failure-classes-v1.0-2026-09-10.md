# W4 失败分类与 cockpit 四格 v1.0

日期：2026-09-10
分支：`w4-failure-classes`（基于 main `ba99ef9`）
提交：`P17d: classify lane failures before counting them, and put the four panels in one row`
依据：并行开发计划 v1.0 §4 规则 1–14；Chem 复盘启示 v1.0 §3.4、§3.5、§5 第 6–7 项

---

## 0. 一句话

失败先分类再计数：依赖不可用不再吃重试预算，而是挂在依赖名下、依赖一好就自动恢复；
内容读不出来一次即终态；其余照旧有限重试。四条计数型 lane 已迁到公共层，
cockpit 首页开头多了一行四格，每格点开到它自己那页。

---

## 1. 背景：Task 62

前一代项目的 62 号任务（linde `peer_read_across_analysis`）三次拿到
`AlphaEngine Desktop status=no_module_page`，然后被永久悬置。任务本身没有任何问题，
是桌面会话断了。而「三次失败就 held」的预算算不出「依赖坏了」和「这件事做不成」的差别。
同时它的 cron 历史 574 次全部 `ok`、健康页显示正常——这个外部依赖故障只在一份任务 CSV
的一个格子里可见。

这份切片就是这两件事：**分类**（§2–§4）和**并排显示**（§6）。

---

## 2. 做了什么

### 2.1 `src/dalton_core/lane_failure_class.py`（新）

纯函数分类器 + 公共失败预算，不碰数据库、不发模型调用。

**三个类**：

| 类 | 含义 | 行为 |
| --- | --- | --- |
| `dependency_unavailable` | 源 / 桌面会话 / 配额 / 传输 / writer RPC 不在 | **挂起**在依赖名下，不消耗重试预算；依赖的探针成功后自动恢复 |
| `content_refused` | 源答了，但答案用不了：读不出、空、哈希不符、被 verifier 拒 | **终态**，第一次就终态，永不重试 |
| `transient` | 其余一切 | 今天的有限重试，原封不动 |

**分类只读 lane 已经在说的话**。`classify(reason, status=..., lane=...)` 对
`"<status> <reason>"` 做大小写无关的子串匹配，规则表 `RULES` 是有序的，先匹配到的赢。
没有新的模型调用、没有要 lane 填的新字段。**匹配不上的一律 `transient`，且原文逐字保留**
（`rule="unmapped"`）——这个模块要防的失败模式，正是一个分类器对没人教过它的句子
悄悄发一个终态判决。

**规则匹配的是「故障词汇」，绝不是厂商名**。这是整个设计的精度所在：
`MarketDataAdapterError: Yahoo has no such ticker` 里有「Yahoo」但不是故障，
把它读成依赖不可用会把一家公司永久挂在一个工作正常的源上——换了身衣服的同一个永久悬置。
厂商名只用来在规则已经判定「有依赖故障」之后**给这个依赖起名**（`VENDORS` 表），
起错名的代价是标题不对，不是丢工作。规则没给名字时按 `VENDORS` 找，再找不到看
`LANE_DEPENDENCIES`（只连一个外部源的 lane），最后才是 `unknown`。

**顺序是设计的一部分**：`budget_refused` 必须在 `refused` 之前读成配额；
`unreadable_last_run`（Guidepoint 读不到**自己**那条上次运行记录）必须在
`unreadable` 之前读成 transient——反过来读，一条打不开自己 cadence 行的 lane
会宣布这个查询永远无解。

**`LaneFailureBudget`** 替换四条 lane 各自抄的 `self._failures: dict[str,int]` +
`self._failure_reason: dict[str,str]`。计数部分不变（三次 transient 失败 → `held`），
新增两件：

- `dependency_unavailable` 不加计数，item 挂在依赖名下，挂多久都不会到期。
- `content_refused` 第一次就是终态，`clear()` 和 `dependency_answered()` 都不解除它
  ——那是关于字节的判断，不是关于源的。

`blocked(item)` 现在返回三个不同的词而不是一个 `held`：`terminal`（永远不会变）、
`parked`（源回来就变）、`held`（部署一次就变）。Chem 复盘读不出来的就是这三者被压成一个词。

### 2.2 挂起不是换个名字的悬置：半开探针

**一个挂起的 item 仍然会被探**，否则这就是 Task 62 换了身衣服。

- 挂起后的**第一次尝试是免费的**。这正是 P14e 的形状：writer RPC 失败让那一轮
  hold，**下一个 tick 把它捡起来重跑**。
- 免费探针花掉之后，每 `PARK_PROBE_INTERVAL_SECONDS`（1800 秒，取自 statements lane
  自己的 `CONFIGURATION_HOLD_SECONDS`）放一次探针，**每个依赖一次，不是每个 item 一次**
  ——五家公司挂在同一个死页面上不该变成五次请求。
- 被放行的那一次运行**就是**探针：成功则 `clear()` → `dependency_answered()`，
  挂在同一依赖上的全部一起恢复；失败则重新挂起、间隔重新计时（免费探针不退还，
  否则间隔永远不会生效）。

### 2.3 `src/dalton_core/lane_failure_ledger.py` + `lane_failure_ledger_schema.sql`（新）

append-only sidecar sqlite，`lane-failure-ledger.sqlite`，就放在 tick ledger 旁边，
形状照抄 C2 的 tick ledger：一行一个事件、永不 UPDATE 永不 DELETE（两个 trigger 强制）、
`content_hash`、重复 append 是同一行、保留期是读侧的 90 天窗口而不是删除策略。

**「现在挂着什么」是对事件的折叠（fold），不是一个要有人保持同步的状态列。**
这是 append-only 在这里的意义：重启前后重放得到同一个答案，没有任何簿记能和证据脱节。
`summarise_events(rows)` 是纯函数，cockpit、测试、报告跑的是同一个折叠，且不需要数据库。

事件词表：`parked` / `parked_again` / `resumed` / `dependency_ok` / `terminal` / `held`。
`parked_again` 和 `parked` 分开，因为「这个依赖已经坑了我们十一次」和「这件事在等」
是两个事实，前者才告诉运维该去修哪个。

`lane_budget(lane, state_dir=...)` 构造时**重放**台账的持久部分（挂起 + 终态），
所以 writer 在 AlphaEngine 还没好的时候重启，不会先往死页面上撞三个子进程再重新发现。
transient 计数**不**重放：它一直就是进程内的，含义就是「自这个 writer 启动以来」，
而重启几乎总是一次部署，也就是最可能已经修好了那件事的东西。
重启后免费探针会重置，理由同上。

**没有 authority**。这是运维台账，不是研究台账，形状与 C2 tick ledger 一致
（sidecar + bootstrap 一行 + `MigrationSpec` 一条），不需要 `dalton_authorized()`
三触发器，也没有引入任何新权限。

### 2.4 已迁移的 lane（行为改变）

四条「计数型预算」lane 从各自的私有 dict 迁到公共层，`self._failures` /
`self._failure_reason` 已经删除（测试会验证它们不再出现在源码里）：

| lane | driver_key | 依赖默认名 |
| --- | --- | --- |
| `mission_market_price_lane.py` | `mission_market_prices` | `market_data` |
| `mission_catalyst_lane.py` | `mission_catalyst_calendar` | 按原文 |
| `mission_consensus_lane.py` | `mission_consensus` | 按原文 |
| `mission_ownership_lane.py` | `mission_ownership` | `sec` |

每条现在多接一个 `failure_ledger_dir`（与 ownership 原有的 `state_dir` 分开：
那个是子进程写 ticket 的目录，属于 launcher；台账是 Core 的 sidecar，cockpit 只读打开）。
`dispatch()` 里用 `getattr(server, "state_dir", None)`——对着 stub writer 跑的 lane
没有 state 目录，而一条因为自己的簿记而拒绝运行的 lane 是 fail-closed 用错了地方。

`skipped[]` 每行现在多两个字段 `failure_class` 和 `dependency`，`reason` 从
恒为 `"held"` 变成 `held` / `parked` / `terminal` 三选一。

---

## 3. 词表迁移：映射到了哪里，哪些没映射

「迁移」= 把各 lane 已经在发的 reason 字符串映射进分类表，不要求 lane 多说一个字。
`tests/test_lane_failure_classes.py::LaneVocabularyMigrationTests` 把 21 条真实词条
钉成了断言。

### 3.1 映射了的

- **结构化 `prefix:detail`**：`probe_transport_unavailable:*`（→ `writer_rpc`）、
  `not_registered:*`、`skipped:pool_exhausted`、`refused:*`、`gated:model_reservation_overrun`、
  `budget_refused` / `budget_rejected`、`quota_exhausted`。
- **异常类名**（全库最常见的失败原因 `f"{type(exc).__name__}: {exc}"`）：
  `ConnectionError` / `TimeoutError` / `HTTPError` / `URLError` / `ConnectorError` /
  `TransportError` / `RemoteDisconnected` → `dependency_unavailable`，依赖名从句中厂商推。
- **AlphaEngine 桌面会话**：`no_module_page`、`desktop status`、`desktop session`、
  `session_expired`、`not_signed_in` → `alphaengine_desktop`。
- **内容类**：`unreadable`、`hash_mismatch`、`empty_document`、`unparseable`、
  `parse_error`、`unsupported_content_type`、`rubric_refused`、`constitution_refused`、
  `unresolvable_refs`、`no filing found for this company`、`returned no filing with XBRL`。
- **中文原文**（两条 lane 写中文）：`还没有接入`（→ 源未接入）、`读不到`、`哈希不符`。
- **回退形式**：`last run: <status>`、`ticket is missing`、`lane ticket is no longer on disk`
  → transient。

### 3.2 已分类但**未**迁移预算机制的 lane

这些 lane 的词表进了表，但它们的 hold 机制不是「计数型预算」，没有可替换的东西，
所以行为一个字没动：

- **签名式 hold（10 条）**：`claim_index`、`conviction_call`、`debate_map`、
  `company_model_spec`、`company_model_forecast`、`forecast_sensitivity`、
  `company_dossier`、`industry_framework`、`deep_insight_gate`、`mission_reflection`。
  它们的 hold 键是 `<subject>|<input digest>`：输入一变就自动重开。这本来就不会永久悬置，
  换成计数预算反而是退步。
- **时间 / tick 冷却（6 条）**：`mission_crowd_sources`（12 tick）、`research_task`（1h）、
  `document_extraction`（1h）、`mission_source_discovery` / `sales_notes_feed` /
  `company_wiki_feed`（1h + plan 里的 `retry_interval_days`）、`guidepoint_discovery`（cadence）。
  这些是间隔重试，不是预算。
- **SQL 持久预算（2 条）**：`mission_statements`（`coverage_mission_statement_dispatches.attempt`
  CHECK 0–3）、`mission_sec_quarters`（`MAX_ATTEMPTS_PER_FILING` + `attempt_voids` 表）。
  见 §3.3。
- **`mission_tracking`**：根本没有失败预算，没有可迁移的东西。

### 3.3 没能映射 / 明确留在原地的

1. **`mission_statements` 的 `COMPANY_FAILURE_MARKERS`**（P13 已有的「公司 vs 我们的配置」
   分法）。三个 marker 里 `carries no lane ticket` 已经放进 `LANE_RULES` 保持 transient；
   另外两个（`no filing found for this company`、`returned no filing with XBRL`）在通用表里
   落到 `content_refused`。**这条 lane 的预算没有迁移**：迁移会把它「未归因 → 算我们的」
   的默认策略改成「未归因 → transient」，那是一次语义改动，应该由懂 P13 那个 IBM 事故的人
   单独做一片。
2. **`mission_sec_quarters` 的中文理由**。两条映射了（`原始件读不到或哈希不符` → content_refused），
   其余（`这份 filing 已经试过 N 次`、`已有四个季度`、`原始件里没有还没入库的季度`）
   **不是失败**，是队列陈述，落到 `unmapped`/transient 且它的 lane 从不把它们喂给预算。
   它的预算在 SQL 里（含 `attempt_voids` 的「退还一次重试」语义），**未迁移**。
3. **`document_extraction` 的 `gated:<gate_reason>` 一族**。`gated:model_reservation_overrun`
   映射成了 `model_budget`；但 `gated:mission does not grant …`、
   `gated:active governance policy does not list …` 这类是**治理拒绝**，
   既不是依赖故障也不是内容不可用也不是临时错误——三类里没有它的位置。
   现状：落 `transient`，原文保留。**这是唯一一个真正没归好类的家族**，
   建议后续要么加第四类 `not_permitted`，要么让 gate 自己走 lane 状态而不是失败理由。
   同族的 `max_windows` / `nothing_to_draft` / `drained` / `budget_rejected` 里，
   前三个是「做完了 / 没得做」，落 transient 无害且该 lane 不把它们当失败。
4. **各交付物的 `*_status` 词表**（`binding_drift`、`no_new_evidence`、`verification_failed`、
   `not_eligible`、`causal_chain_unmapped`、`duplicate`…）。这些是**判断层的结论**，
   不是失败，全部落 `unmapped`/transient，且它们的 lane 从不把它们送进预算。
   我没有把它们塞进分类表，因为给「这一版没有新证据所以不发」发一个终态判决是错的。
5. **`not_registered:ConnectorError` 的依赖名是 `unknown`**：source discovery 一条 lane 接多个源，
   句子里说不出是哪个。cockpit 上的标签是「说不出名字的依赖（原文见明细）」，
   明细里是逐字原文。

---

## 4. cockpit：运维待办与四格

### 4.1 `CockpitPlane.ops_backlog()` + 路由 `/v1/cockpit/ops`

只读。列出挂起的工作，**按依赖分组**，每组带 `first_seen` / `last_seen` / `attempts` /
涉及的 lane（中文名）/ 每件的原文理由；终态的另列一段。
数据来自 append-only 台账的折叠，所以这一页和台账重放不可能给出不同答案。
没有台账文件的 Core 返回 `available: false` + 一句中文，而不是一张看起来正常的空表。

依赖名与失败类都过 `DEPENDENCY_LABELS` / `FAILURE_CLASS_LABELS`（ADR-0006：
页面不带机器语言）。

### 4.2 首页四格

`cockpit_control.html` 的「研究目标」页开头新增一张卡「一眼看全」，
里面是 `<div class="grid c4" id="ops-panels">`：

| 格 | 数字 | 点开到 |
| --- | --- | --- |
| 流水线在跑 | `running/total`，副标注 idle / 动不了 | 往下滚到本页的 lane 列表 |
| 还没填上的来源缺口 | 行业框架 open gaps + 抽取 backlog 排队份数 | 「看每个来源能取什么」（`/v1/cockpit/sources`） |
| 挂起 / 不再重试的工作 | 挂起件数 + 终态件数 | 「运维待办」（`/v1/cockpit/ops`） |
| 上周产物验收 | 上一个已结束周的打分数 | 「每周回头看」（`/v1/cockpit/reflection`） |

数据在 `overview()` 里新增的 `ops` 键下，和页面本来就有的那一次 fetch 一起回来。

**每格数的是它自己那页显示的东西**，不是第二次独立读数——
`test_the_lane_panel_and_the_lane_rows_cannot_disagree` 钉住了这一点
（四个桶的计数 + `other` 必须等于 lane 行数）。
桶表没见过的状态词计入 `other` 而不是被丢掉：数不出来的 lane 正是这一格要终结的东西。

复用的读者，全部是已有的、之前没接到 cockpit 上的：

- 行业框架 gap：直读 `industry_framework_versions` 最新版的 `gaps`
  （`IndustryFrameworkAuthority` 要 store 且会建表，而这个连接是 `mode=ro`）。
- 抽取 backlog：`extraction_backlog.extraction_backlog(core, company_ref)`，
  按 mission 的公司汇总。**复用而不是重写 COUNT(\*)**：它区分的三种排队
  （本版待抽取 / 旧版已获取未入队 / 已发现未取）正是 W2 报告要说的事。
- 上周验收：`research_cycle_reflection.closed_week` / `quality_scores_published` /
  `journal_feedback`。窗口用 Q2 自己的 `closed_week`，不是「最近七天」——
  否则一页上会出现两个关于「上周」的不同数字。

每个读者都按 cockpit 惯例守 `_table_exists` / try-except，老 Core 上降级不报错。

---

## 5. 没做什么

- **没有迁移 statements 与 sec_quarters 的 SQL 持久预算**（见 §3.3.1/2）。
  它们现在仍然是「N 次后 held」，只是词表已经可分类。
- **没有加第四类 `not_permitted`**（治理拒绝）。见 §3.3.3。
- **没有把签名式 hold 与冷却式 hold 的 10+6 条 lane 接到台账上**。
  它们不会永久悬置，但它们的 `dependency_unavailable` 也就不会出现在运维待办里
  ——比如 `claim_index` 因为 `model_unavailable` 而 hold，今天只在 lane 行的 detail 里可见。
  这是接下来最值得做的一片：给它们的 hold 判断加一次 `classify()`，
  分类结果送台账，hold 机制本身不动。
- **没有 authority、没有新权限、没有 writer 操作**。运维待办是纯读。
- **没有碰 live 状态目录、没有部署、没有发 mission 版本、没有重启 OpenClaw 网关。**
- **没有 push。**

---

## 6. 集成时要接的线

1. **四处登记**：`bootstrap.SCHEMA_DATABASES` 加了
   `("lane_failure_ledger_schema.sql", "lane-failure-ledger.sqlite")`；
   `scripts/rehearse_deploy.SIDECAR_MIGRATIONS` 加了对应的 `MigrationSpec`。
   本片**没有新增 lane**，所以 `LANE_MODULES` 与 `REGISTRY_LANE_LABELS` 无需改动
   （已有测试 `test_lane_failure_classes::test_each_migrated_lane_parks_under_its_registry_key`
   验证四条被迁移的 lane 的 `DRIVER_KEY` 都在 `REGISTRY_LANE_LABELS` 里——
   挂在一个 cockpit 标不出名字的键下的 item 是看不见的）。
2. **`install.sh`**：新 sidecar 是空表起步，无治理记录要播种，
   不需要进 `DELIBERATELY_UNSEEDED`。
3. **`cockpit_plane.py` 冲突面**：改动集中在三处——模块级新增四张 label 表
   （紧跟 `LANES_SHOWN_ELSEWHERE` 之后）、`overview()` 里两处
   （`lane_rows` 提到局部变量 + 新增 `"ops"` 键）、`_lane_states` 之前插入的一段新方法。
   与其它切片合并时按「两边各追加一行」处理 label 表即可，但**合并后必须先
   `python -c "import dalton_core.cockpit_plane"`**（规则 12）。
4. **`agenda_control.cockpit_view`**：新增一条 `if path == "/v1/cockpit/ops"`，
   在 `reflection` 之前。`do_GET` 无需改动。
5. **`cockpit_control.html`**：新增一张卡 + 一段 CSS + `renderOpsPanels` / `openOps` /
   `renderOps`；并把原来匿名的 `$("open-sources").onclick=async()=>{...}` 与
   `open-reflection` 改成具名函数 `openSources` / `openReflection`（四格要用同一个渲染器，
   两个入口一个渲染器——数字和页面能不一致正是这一行要防的）。
6. **本片改了两个已有测试文件的三处断言**：
   `test_mission_market_price_lane.py::test_a_success_clears_the_failure_budget` 与
   `test_mission_catalyst_lane.py` 的两处，把 `lane._failures` 这个已被删除的私有字段
   换成公开的 `lane.budget.attempts(...)` / `.blocked(...)`。行为断言未放宽。
7. **规则 14**：本片本身就是「一次 live 事故 → 一条测试」的产物；
   `tests/test_lane_failure_classes.py` 的 `test_a_dependency_outage_never_spends_the_budget`
   与 `test_a_parked_item_is_probed_rather_than_abandoned` 是 Task 62 的回归测试。

---

## 7. 新增 / 改动文件

新增：

```
src/dalton_core/lane_failure_class.py
src/dalton_core/lane_failure_ledger.py
src/dalton_core/lane_failure_ledger_schema.sql
tests/test_lane_failure_classes.py          (42 tests)
tests/test_cockpit_ops_panels.py            (19 tests)
docs/reports/w4-failure-classes-v1.0-2026-09-10.md
```

改动：

```
src/dalton_core/bootstrap.py                 (+1 行 schema 登记)
scripts/rehearse_deploy.py                   (+1 条 MigrationSpec)
src/dalton_core/mission_market_price_lane.py
src/dalton_core/mission_catalyst_lane.py
src/dalton_core/mission_consensus_lane.py
src/dalton_core/mission_ownership_lane.py
src/dalton_core/cockpit_plane.py             (四张 label 表、ops_backlog、四个 panel 读者)
src/dalton_core/agenda_control.py            (+1 条路由)
src/dalton_core/cockpit_control.html         (四格 + 运维待办 reader)
tests/test_mission_market_price_lane.py      (私有字段 → 公开 API)
tests/test_mission_catalyst_lane.py          (同上，两处)
```

---

## 8. 验收结果

全量测试（`cd <worktree> && PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`）：

```
Ran 5442 tests in 481.400s

OK (skipped=1)
```

确定性、无网络、无模型调用：新增的两个测试文件不打开任何 socket，
不构造任何 adapter，时钟全部注入（`NOW = 2026-09-10T09:00Z`），
台账全部写在 `tempfile.TemporaryDirectory()` 里。

---

## 9. 留给人的问题

1. **治理拒绝要不要第四类？** `gated:mission does not grant …` 既不是依赖故障、
   不是内容不可用、也不是临时错误。现在落 transient（原文保留）。
   我倾向于**不**加第四类，而是让 gate 走 lane 状态（`ungranted` / `unapproved`
   已经在 cockpit 上有位置），但这需要动 document_extraction 的 `stop_reason` 契约。
2. **探针间隔 30 分钟合适吗？** 取自 statements lane 自己的
   `CONFIGURATION_HOLD_SECONDS`。AlphaEngine 桌面会话断了以后要人重新登录，
   30 分钟一次探针在人还没来的时候是纯浪费；但配额类依赖 30 分钟又偏长。
   要不要按依赖分档（会话类 2 小时、配额类 15 分钟）？
3. **statements / sec_quarters 的 SQL 预算迁不迁？** 迁的话要改
   P13 那条「未归因 → 算我们的配置」的默认，以及 `attempt_voids` 的退还语义。
   我没有动，因为那是一次语义改动而不是重构。
4. **要不要把 16 条签名式 / 冷却式 lane 也接到台账上？** 只加 `classify()` 调用、
   不动 hold 机制，运维待办就能看到全部 lane 的依赖故障。工作量小，
   但要逐条确认哪个字段是「失败理由」——有几条 lane 的 hold 理由其实是
   「输入没变」，不是失败。
