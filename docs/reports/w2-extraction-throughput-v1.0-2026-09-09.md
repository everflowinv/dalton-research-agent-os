# W2 抽取吞吐：研报为什么没变成 Claim v1.0

日期：2026-09-09
分支：`w2-extraction-throughput`（worktree `~/Projects/dalton-w2-extraction-throughput-worktree`），
基线 main `8198f0f`，已 `git merge main` 到 `7011104`（含 P12c DebateMap、P12a 档案、C2 预算池）
数据：live Core 只读副本 `/private/tmp/dalton-ro/core.sqlite`（12:23）、`state/dalton-core/extractions/` 的 506 份
`summary.json`（09-07 09:51 → 09-09 21:08）、`run/heartbeat.json`（09-09 21:34）、`thesis-impact-budget.sqlite`
状态：交付在审；无 live 写入、无模型调用、无部署

---

## 0. 一句话

**抽取车道没有堵，它是饿的。** DebateMap 读到的「ACN 有 885 份卖方文档、只有 5 条卖方 Claim」是两次计数错误叠加：
885 是**全 mission、跨 13 个 mission 版本的行数**，ACN 自己只有 **12 份**卖方文档、其中 **3 份被取到**、**2 份被读过**，
产出 5 条 Claim。真正的瓶颈依次是：**取（acquisition）在 AlphaEngine 130/24h 上耗尽**、
**discovery planner 按「券商观点 required = 3」的清单下限主动叫停**、
**一份点名四家覆盖公司的研报只能变成一家的 Claim**、以及**行业主体在抽取路径上根本没有入口**。
预算不是限制项：抽取车道 09-08 读了 1,510 个窗口、实付 **$0.61**。

---

## 一、诊断（先给数字）

### 1.1 文档：按公司 × spec × 状态（去重到 distinct `document_ref`）

`coverage_mission_discovered_documents` 一份文档在每个 mission 版本各有一行，所以行数 ≠ 文档数。
下表按 `document_ref` 去重，取它到过的最好状态。

| 公司 | spec | acquired | failed | 只 discovered | 合计 |
| --- | --- | ---: | ---: | ---: | ---: |
| **ACN** | annual-report-10k | 1 | 0 | 0 | 1 |
| | earnings-call-transcripts | 4 | 0 | 7 | 11 |
| | **sell-side-reports** | **3** | 0 | **9** | **12** |
| | industry-demand | 23 | 7 | 4 | 34 |
| | competitive-landscape | 19 | 3 | 1 | 23 |
| | management-changes | 33 | 9 | 3 | 45 |
| CTSH | sell-side-reports | 18 | 2 | 0 | 20 |
| EPAM | sell-side-reports | 18 | 2 | 0 | 20 |
| IBM | sell-side-reports | 20 | 0 | 4 | 24 |
| DXC | sell-side-reports | 3 | 0 | 17 | 20 |

全 mission：**454 份 distinct 文档被取到**（`status='acquired'`），卖方 96 份、其中 62 份取到。
**885 = 卖方 spec 在 `coverage_mission_discovered_documents` 里的总行数**
（IBM 204 + CTSH 182 + EPAM 182 + ACN 120 + DXC 197），跨版本、跨全部五家。

### 1.2 审阅队列：446 条「在等」里只有 7 条车道看得见

`coverage_mission_document_reviews` 的状态词表是 `awaiting_human_extraction` / `extraction_staged` / `dismissed`
（`coverage_mission_schema.sql:251`），没有 in_review / stale。

| mission 版本 | awaiting | dismissed | staged |
| --- | ---: | ---: | ---: |
| v2 / v4 / v5 / v7 / v8 / v9 / v10 / v11 / v12 | 33 / 62 / 73 / 55 / 113 / 4 / 9 / 82 / 8 | 26 / 19 / 160 / 82 / 48 / 157 | 32 / 17 / 89 / 56 / 23 / 92 |
| **v13（当前指针）** | **7** | 29 | 12 |
| 合计 | 446 | 521 | 321 |

抽取子进程只查当前指针下的行（`document_extraction_cli.py:314`
`document_reviews(mission["id"], state="awaiting_human_extraction")`），协调器的 `_awaiting()` 同样
join `coverage_mission_pointer`（`document_extraction_launcher.py:286`）。446 条 awaiting 行去重后是
**195 份 distinct 文档**，其中当前版本下只有 **7 份**。

**但这不是丢工作。** 按 distinct 文档取「它到过的最好审阅状态」：454 份 acquired 文档里
**447 份已经读到终态**（staged 或 dismissed），只有 **7 份**从未被读过。老版本上的 awaiting 行，
绝大多数是同一份文档在更早版本上的残影，那份文档在更晚的版本里已经读完了。

| spec | 曾 staged | 只 dismissed | 仍未读 | acquired |
| --- | ---: | ---: | ---: | ---: |
| annual-report-10k | 5 | 0 | 0 | 5 |
| earnings-call-transcripts | 17 | 27 | 1 | 45 |
| **sell-side-reports** | **30** | **32** | **0** | **62** |
| industry-demand | 56 | 32 | 2 | 91 |
| competitive-landscape | 52 | 79 | 1 | 132 |
| management-changes | 38 | 78 | 3 | 119 |

**全部 62 份取到的卖方研报都已经被读过了。** 队列是空的。

### 1.3 每 tick 的真实吞吐 vs 配置界

配置（活的 ticket 参数，`extractions/*/summary.json`）：`max_windows: 30`、`max_numeric_windows: 10`、
`max_discovery_windows: 10`。代码默认是 `DEFAULT_MAX_WINDOWS_PER_TICK = 4`
（`document_extraction_launcher.py:33`）与 CLI 的 `DEFAULT_MAX_WINDOWS = 2`，live 通过
`service.json` 的 `document_extraction.max_windows_per_tick` 抬到了 30。

| 日 | 运行 | max_windows | drafted | admitted | reviews_complete | skipped | `nothing_to_draft` | `max_windows` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 09-07 | 177 | 4 | 671 | 659 | 228 | 131 | 1 | 168 |
| 09-08 | 214 | 30 | 1,734 | 1,386 | 603 | 711 | 48 | 132 |
| 09-09 | 115 | 30 | 787 | 321 | 176 | 582 | **58** | 16 |

09-08 抬到 30 之后，当天就把队列读干净了：09-09 有 **58/115 次运行报 `nothing_to_draft`**，
只有 16 次撞到 `max_windows`。最后一次运行（21:08，heartbeat 里 `status: "held"`）：
`reviews_scanned 14 / drafted 0 / reviews_complete 0`，14 条里 6 条是**永久读不了**的：

```
3 | PublicWebSourceError: fetched page is not valid UTF-8; it is not rendered
1 | PublicWebSourceError: fetched PDF is encrypted and is not rendered
1 | FetchLaunchRejected: no completed acquisition ticket for this document
1 | ResearchVerificationError: source offset must be a valid bounded window
```

这些 review 永远 `complete=False`，所以永远不会被 `_admit_complete_reviews` 关掉，
于是 `awaiting` 永远 > 0，协调器每小时（`IDLE_HOLD`）重新起一个什么都做不了的子进程。

### 1.4 预算：不是限制项，但**预留**是

`thesis-impact-budget.sqlite`，只取 `work:document-extraction-%`：

| 日 | 结算窗口 | 预留 | 实付 | 均值/窗口 | 最大/窗口 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 09-07 | 608 | $30.40 | $0.369 | 608 micros | 1,793 |
| 09-08 | 1,510 | $75.50 | $0.610 | 418 micros | 1,394 |
| 09-09 | 707 | $35.35 | $0.208 | **294 micros** | 1,362 |

日上限 $100（`thesis-impact-day-budget-policy:production:2`，09-09 04:01 从 $25 抬上来），
mission `max_daily_paid_calls: 9000`。全天全 mission 实付：09-08 $1.90、09-09 $17.23（其中抽取 $0.21）。
**预留是实付的 170 倍**：`build_work` 写死 `max_cost_usd: 0.05`（`document_extraction.py:270`），
而 deepseek-v4-flash 的价目表（$0.14/M in、$0.28/M out）对上 WorkOrder 自己的 token 界
（16k in / 3k out）算出来的最坏情况是 **$0.00308**。今天不咬人只是因为顺序执行、结算即释放
（`ThesisImpactBudgetStore._day_committed` 把已结算的算实付、未结算的算预留）；
一旦并发或者日上限调低，先撞上的就是这个数。

**但价目表只是下界（review B1）。** 一个窗口有**两条**计价路径，算术不一样：
provider 回了 usage 就按 token 计量（价目表管得住），没回就走 route 自己的**整请求**估价
`calculator:model-route-estimate:0.1`（`model_accounting._route_estimate_micros`），
它跟这个窗口的 token 界没有任何关系。live 上有 **41 个窗口走了这条路，均值 4,471 micros、最大 5,496**，
都高于价目表推导的 3,080。预留低于扣款 = `MODEL_COST_EXCEEDED_RESERVATION` 告警 + **不调 `settle()`**
= 这笔预留永远挂在日上限上。所以最终实现取
`min(契约上限, 2 × max(价目表推导, route 估价))`，headroom 是 2 倍且写在常量里。

真正会先咬人的是**次数**：30 + 10 + 10 = 50 次调用/tick × 288 tick/天 = 14,400 > `max_daily_paid_calls 9000`。
配置的 30 不是保守，是**没有对着 mission 的调用上限算过**。

### 1.5 归因：一份研报只能属于一家公司；行业主体没有入口

`admit_suggestions` 把每条 Claim 的主体写死成 review 的公司
（`document_extraction.py`，原 `subject_ref=context["company_ref"]`）。而 AlphaEngine 搜索返回的
wire 本身就带着答案，`live_mcp_connector.py:1327` 只取了 `doc_id`，把 `title` / `sources` / `companies` 扔了：

```json
{"doc_id":"320000610220657",
 "title":"Payments, Processors, and IT Services: Wells Weekly Payments Pulse",
 "sources":["Wells Fargo Securities, LLC"],
 "companies":["Paypal Holdings, Inc.","Infosys Ltd",
              "Cognizant Technology Solutions Corporation",
              "EPAM Systems, Inc.","Accenture PLC", ...]}
```

这份 Wells Fargo 的研报点名了 ACN / CTSH / EPAM，它是 Cognizant 的检索返回的，
所以它产出的每一条 Claim 都是 Cognizant 的，Accenture 一条都拿不到。
把 spool 里还在的 24 份搜索原始响应重放一遍（见第五节），**162 份文档被复原出出处，
12 家券商，10 份卖方文档点名 Accenture，其中 3 份是挂在别家名下的**。

行业主体（`industry:us-it-services`）**0 条 Claim**：`claim_versions` 的 `subject_ref` 只有五个 CIK。
P13f 已经给行业建了自己的清单（`mission_stage.INDUSTRY_BASE_ITEMS`：行业需求、竞争格局，各 required 3），
但抽取路径不知道这回事——一份市场规模报告由 Accenture 的检索找到，就成了 Accenture 的 Claim。
`coverage_mission_document_reviews` 的 `UNIQUE(mission_version_ref, document_ref)` 决定了
**一份文档只能有一条 review**，所以「一份文档多个主体」只能在**准入时**解决，不能靠多开 review。

### 1.6 与 P12b「sell_side 217」对账

P12b 规则打标的 `sell_side: 217` 是**全 mission 的 Claim 数**，逐公司拆开（P12c 第八节）
ACN 5 + EPAM 55 + CTSH 149 + IBM 8 + DXC 0 = 217。和 1.1 的文档数对得上：
CTSH 读了 18 份卖方 → 149 条，ACN 读了 2 份 → 5 条。**每份文档的产出没有异常，是文档数差了六倍。**

各 tier 的实测产出（本仓库自己写的结案语，按 distinct 文档取最好一次）：

| tier | 读过的文档 | Claim/文档 |
| --- | ---: | ---: |
| filing | 5 | 17.40 |
| management | 44 | 5.59 |
| sell_side | 62 | **2.35** |
| industry | 219 | 3.07 |
| news | 116 | 1.17 |

**卖方研报是产出最低的一档**，因为抽取契约拒绝一切数字（目标价、EPS 都进不来）、
免责声明过滤器又吃掉研报开头的大半页。这对 DebateMap 的验收是个独立的坏消息：
就算把 ACN 剩下的 9 份卖方研报全取回来，也只多约 21 条卖方 Claim。

---

## 二、根因排序

1. **取不到，不是读不完。** AlphaEngine `spent 133 / cap 130`，heartbeat 明写 `budget_exhausted`。
   ACN 的 9 份卖方研报、7 份电话会都停在 `discovered`。**owner 层**。
2. **discovery planner 按清单下限主动叫停。** `SOURCE_BASE_ITEMS` 的 `broker_research.required = 3`
   （`mission_stage.py:106`），于是 heartbeat 里是
   *"the plan says stop: Cancel the queued report. Eleven reports are held and nine read against a requirement of three."*
   清单是 Initial Screen 的**证据下限**，不是 DebateMap 的**互证要求**（多空各 ≥2 家独立券商），
   而后者今天在代码里没有任何表达。**规格层，Wave 2 的 DebateMap 需要自己声明需求。**
3. **一份研报只能属于一家公司。**（本片已修）
4. **行业级文档产不出行业主体 Claim。**（本片已修）
5. **出处（券商）不落库。** 独立性阶梯只能数文档，数不了券商。（本片已修）
6. **永久读不了的 review 不会关闭。** 6/14 条僵尸把队列深度钉在非零。（本片已让它可见、可定位）
7. **预留是实付的 170 倍；per-tick 界没对着 mission 的调用上限算过。**（本片已修）

---

## 三、改了什么

全部新增文件 + 三处外科手术式改动。没有碰 `writer_server.py` / `coverage_mission.py` /
`bounded_planner_driver.py` / `macos_launchagent.py` / `install.sh` / cockpit，没有改任何已哈希的契约。

### 新增

- **`document_provenance.py`**（纯函数，无 I/O）：tier 词表（`filing` / `management` / `sell_side` /
  `expert` / `sales_note` / **`industry`** / `news` / `crowd` / `other`，与 P12c 独立性阶梯同词；
  **`industry` 是本轮 review S4 新分出来的一档**——一份市场规模报告不是新闻页，它排在「点名公司的研报」
  之下、新闻之上，而且它正是行业主体 Claim 的来源，混进 `news` 既排错了序也把它藏了起来）、
  `parse_search_metadata`（从 AlphaEngine 搜索原始字节里读回 title / sources / companies）、
  `broker_key`（去掉 "Securities, LLC" 之类的法律外壳，让 "TD Cowen" 和 "TD Cowen, LLC" 是一家）、
  `covered_subjects`、`subjects_for_statement`（一条陈述归谁）。
- **`extraction_priority.py`**：`review_sort_key`（**证据价值优先**，同档内**覆盖最薄的公司优先**，
  公司优先级只做第三顺位）、`thinness_ranks`、`window_reservation_micros`、`safe_windows_per_tick`
  （从日上限 + 调用上限 + 服务模型的价目表推导，不写死；返回推导过程）。
  **加上老化（review S4）**：严格优先级就是饿死——永远读最好的那份，就永远不读最差的那份，
  而「最差的那档」正是行业主体的 Claim 来源。每等满 `STARVE_AFTER_DAYS = 7` 天升一级，
  是**爬梯子不是插队**，且永远不越过 filing（`BEST_AGED_VALUE = 1`）。
- **`extraction_backlog.py` + `extraction_backlog_schema.sql`**：追加式 `document_provenance_records`
  表（只写 wire 已经说过的话，no-update / no-delete 触发器）、`DocumentProvenanceStore`、
  `backfill_provenance`（重放 spool 里还在的搜索响应）、`observed_yield`（从本仓库自己写的结案语
  里量出每档的实际产出）、**`extraction_backlog(company)`**（第三项交付）。
- **`tests/test_extraction_throughput.py`**：39 项。

### 改动

- `document_extraction.py`
  - `reservation_micros(work, route, profile)`：预留 = `min(契约上限, 2 × max(价目表推导, route 估价))`。
    上限仍是 WorkOrder 契约里的 `max_cost_usd`。**WorkOrder 的 budget 一个字节没动**——它进
    `content_hash(work.to_dict())`，动了就是让 live 上 1,700 多个已存结果全部 drift。
  - `recorded_subjects` / `statement_subjects`：读追加表；表不存在就返回空，行为与今天逐字节一致。
    **覆盖范围在读的时候现算**（review S2），不存进哈希：universe 会变，行是 append-only，
    存进去就意味着「昨天新进覆盖的公司」在上周记录的研报上永远认不出来，而且重放会被判成冲突。
  - `admit_suggestions`：每条 suggestion 按它**自己点名的主体**准入，可以是多个。
    **review 那家公司永远在结果里**（review B2/B3）：这条陈述是带着那家公司当主体起草出来的，
    去掉它等于无声撤回。额外主体只在陈述**完全不点名 review 公司**时加进去——
    「分析师在估值上更偏好 Cognizant 而非 Accenture」是一句关于排序的话，起草时的主体是 Cognizant，
    把它铸成 Accenture 的 Claim 就是往 Accenture 的档案里塞一句从来不是写给它的话。
    只有一个主体时，`pair_key` 与 idempotency key 与今天**完全一致**，重放仍然是 duplicate。
    行业规则按 owner 的写法保留：行业级 spec 的文档里，一条谁都不点名、但点名了行业的陈述
    **额外**归 `industry:us-it-services`；电话会里的同一句话仍然只归公司。
- `document_extraction_cli.py`：换用新排序（失败则退回旧排序，排序从来不是闸）、
  每轮先做一次尽力而为的出处回填、把「永久读不了」的 review 单列进 `unreadable_reviews`
  （加密 PDF / 非 UTF-8 / 取件票据丢失 —— 这些明天还是读不了，重试不是耐心是死循环）。
- `tests/test_document_extraction_admission.py`：那条断言「断线后整笔预留不释放」的用例，
  金额从写死的 50,000 改成用同一个推导函数算，跟着 profile 走。

---

## 四、测试

```
Ran 3992 tests in 385.675s

FAILED (failures=1, skipped=1)
```

把那一条 main 的时间依赖测试排除后：

```
Ran 3991 tests in 464.457s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，merge main `7011104` 之后。
本片新增 58 项 + 3 项端到端准入用例。）

**那一条失败不是本片的。它看起来像「测试依赖时钟」，但 review 复核后确认是一个生产缺陷。**
`tests.test_mission_research_task_lane.LaneTests.test_an_exhausted_pool_is_a_skip_with_the_name_c2_will_generalise`
报 `'launched' != 'skipped:pool_exhausted'`。在 main 检出（`7011104`）上单独跑同样失败。查清了：

- `research_task.day_reserved_micros` 用 **loop 的 `created_at` 前 10 个字符**当日期
  （`research_task.py:448`，注释写明「不另设账本，用 loop 自己的准入日」）；
- 但那个 `created_at` 是**墙上时间**，而测试与 `ResearchTaskCoordinator` 用的是冻结时钟
  `2026-09-09T12:00Z`。实测 loop 落在 `2026-09-10T00:24Z`；
- 于是 `pool_state(day='2026-09-09')` 数到 `reserved_micros: 0`、`remaining_micros: 5000000`，
  池子永远不满，`dispatch_once()` 返回 `launched`。

也就是说这条测试在 UTC 09-09 当天通过、过了午夜就失败。

**但冻结时钟只是它最先暴露的形状。** code-review 复核 P14e diff 后确认了更根本的一层：
`create_loop` 的 `created_at` 来自 `bounded_planner_loop._now()`（`bounded_planner_loop.py:595` → `:82`），
**没有时钟接缝**；而它被分到哪一天，用的是调用方的时钟
（`mission_research_task_lane.py:157` 的 `ResearchTaskCoordinator.clock`、
`research_task_cli.py:96` 的 `run_admissions` 自带 `now`）。于是在生产上：
**一轮准入在 UTC 午夜前开始、午夜后落库，它查的是今天的池子，reservation 却记进明天的桶——
这个任务从此不被计入任何它据以准入的池子。** 这不是测试的毛病。

修法两条都对；review 认为更耐用的是**在 admission 记录里写下显式的准入日**，因为它经得起重放。
**属于 P14e / C2 的切片，本片没有改动它。**上面的失败数就是这一条。

review 另外在同一片池子算术上标了两条本片同样没碰的：
`bounded_planner_loop.py:375` 的 `admitted_loops` 返回的是每个 loop 的**所有版本**而非 head
（`active_loops` 就在它下面，行为相反），而 `day_reserved_micros` / `settle` / `research_task_view`
都按「一个任务一行」遍历它，所以一旦有人真的走了 `create_loop:504` 有意支持的修订路径，
同一个任务的预算会被记进池子两次；以及 `research_task.py:64` 的 `POOL_SHARE 0.25`
对上 $1.00 的单任务最低成本，意味着在仓库里所有 mission fixture 用的 0.5 预算下
**没有任何 inquiry 可以准入**，车道会永远报 `pool_exhausted` 而一分钱没花。

新用例覆盖：

- **队列排序**：末位公司的电话会先于首位公司的新闻页；filing > 电话会 > 研报；同档内薄的先读；
  市场报告排在新闻之上、研报之下。
- **老化（S4）**：不给时钟就是纯优先级；每满一个周期升一级；爬而不跳、永不越过 filing；
  饿了六个周期的市场报告最终超过刚到的电话会；时间戳读不了或来自未来的一律不升级。
- **预留（B1）**：pin 在 route 估价那条路上——live 最大值 5,496 micros 时预留 10,992；
  live 均值 4,471 有 2 倍余量；route 没有精确估价（空快照 / 别的 profile / 不合格候选）时退回价目表下界；
  极小估价不会把价目表下界拉低；契约上限仍是天花板；没有价目表的 profile 预留上限而不是一分钱。
- **多主体准入（B2/B3/S1）**：一句完全写别家的陈述额外归那家；**同时点名两家的比较句只归 review 那家**；
  **review 那家公司在任何情况下都不掉**（五种句式逐一验）；老 Claim 集合 ⊆ 新 Claim 集合（回放固定队列）。
- **端到端（S1，走真正的 `admit_suggestions` 循环）**：两主体计划产出两个候选、两条 Claim、
  两个不同的 `candidate_claim_ref`（一个引用只有一个 correction set）；重放两条都是 duplicate；
  **单主体时的 key 与今天逐字节一致**——先只跑主体、再跑两主体，主体那条是 duplicate、只多写一条 Claim。
- **行业主体**：行业文档里的市场事实**额外**归行业；同一句话在电话会里仍只归公司。
- **批量界推导**：$0.00308/窗口、调用上限先咬人、上下夹逼、拒绝算不出来的输入。
- **出处落库**：一家两种拼法算一家；重放是 duplicate；事实不一致是冲突而非覆盖；
  tier 不在词表里被拒；**覆盖范围现算（S2）**——universe 收窄/放宽给出不同答案，而重放仍是 duplicate；
  试图把 `covered_subjects` 写进哈希会被拒；**列名就是 P12c `_ATTRIBUTION_COLUMNS` 找的那些**。
- **产出统计（S3）**：`windows_per_document` 自带 `windows_basis`，没有窗口证据时如实写 `default`；
  未定价的 backlog 报 `unknown` 而不是 `$0`。

## 五、Smoke（只读，`/tmp` 副本）

### 5.1 新排序下接着读的 50 份

把 446 条 open review 去重成 195 份 distinct 文档，各取前 50：

| | filing | management | sell_side | industry | news | 公司分布 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| **今天**（公司优先） | 1 | 0 | 18 | 20 | 11 | CTSH 29、ACN 21，其余 0 |
| **新排序** | **5** | **17** | **20** | 8 | 0 | EPAM 26、CTSH 19、IBM 2、DXC 2、ACN 1 |

今天的下 50 个窗口只覆盖两家公司、且 31 份是行业报告与新闻；新排序是 5 份 filing + 17 份电话会 +
20 份研报，五家都碰得到。ACN 只占 1 份——**因为 ACN 已经没有能读的了**，它的 12 份文档里 3 份取到、都读完了。
这本身就是诊断结论的直接证据。

**老化今天不改变任何东西，这是对的。** 队列里最老的 open review 只有 3 天
（195 条的中位数是 1 天，没有一条满 7 天），所以升级项为零，上表两栏一致。
把队列冻住往后推，老化才开始咬：

| 停滞 | 前 50 的构成 |
| --- | --- |
| +0 天 | filing 5、management 17、sell_side 20、industry 8 |
| +21 天 | 同上（高档还没读完，轮不到低档） |
| **+63 天** | filing 5、sell_side 18、**industry 18、news 9** |

也就是说：只要车道在动，老化不打扰它；一旦某档被高档长期压住，它会**爬上来**而不是插队。

### 5.2 出处回填与 P12c 的接缝（重放 spool 里还在的搜索响应）

```
{'envelopes': 24, 'recorded': 162, 'duplicate': 318, 'unreadable': 0, 'status': 'complete'}
document_attribution_rows: 162
  'alphaengine-doc:320000610220657' -> {'publisher': 'Wells Fargo Securities, LLC',
                                        'sources': 'Wells Fargo Securities, LLC',
                                        'title': 'Payments, Processors, and IT Services: ...'}
distinct houses: 12
['Citi','Deutsche Bank','Goldman Sachs','Guggenheim Securities LLC','HSBC','J.P.Morgan',
 'Morgan Stanley','RBC Capital Markets','TD Cowen','UBS','Wells Fargo Securities, LLC','Wolfe Research']
sell-side documents naming Accenture: 10   ... filed under another company: 3   (Wells Fargo)
```

spool 里只剩 24 份搜索响应（早期的已经轮转掉），所以这是**下界**：162 份文档拿回了出处，
109 份带券商名。新取的每一份文档从此都会带着出处入库。

**与 P12c 的接法。** `debate_map_draft.document_attribution()` 是从
`coverage_mission_discovered_documents` **的列**上读 `publisher`/`broker`/`authors`/`sources`/`title` 的，
而那张表是已哈希的合同，本片不能动它。所以两件事：
(1) `document_provenance_records` 的列名就用它找的那几个（`broker`、`authors`、`sources`、`title`），
(2) 本片给出同形状的读函数 `document_attribution_rows(connection)`，集成时一行合并即可：

```python
attribution = {**document_attribution(conn), **document_attribution_rows(conn)}
```

冲突时以本片为准——上游 wire 说的 publisher 比从标题猜出来的更可信。
没有这张表的 Core 拿到空 dict，正是那条阶梯已经能处理的情况。

### 5.3 `extraction_backlog(company)`（第三项交付，供 cockpit 与 P14a）

预留按 10,992 micros/窗口（价目表 3,080 与 live route 估价最大值 5,496 取大、再乘 2 倍 headroom），
产出与窗口数都按本机实测：

| tier | 读过的文档 | Claim/文档 | 窗口/文档 |
| --- | ---: | ---: | ---: |
| filing | 5 | 17.40 | 18 |
| management | 44 | 5.59 | 3 |
| sell_side | 62 | **2.35** | 5 |
| **industry** | 219 | 3.07 | 2 |
| news | 116 | 1.17 | 1 |

| 公司 | 排队文档 | 预期 Claim | 预期成本（上界） | 已知券商 |
| --- | ---: | ---: | ---: | ---: |
| ACN | 46 | 127 | $0.696 | 7 |
| CTSH | 28 | 68 | $0.326 | 3 |
| EPAM | 38 | 109 | $0.480 | 7 |
| IBM | 19 | 56 | $0.308 | 2 |
| DXC | 66 | 208 | $1.158 | 8 |

**每一档的 `awaiting_extraction` 与 `acquired_unqueued` 都接近 0，整个 backlog 都是 `discovered`。**
全五家读完剩下的一切，模型成本上界合计 **$2.97**（按实付均值算是 $0.08）。
不给 `reservation_micros` 时这一列报 `null` 与 `cost_basis: "unknown"`，不报 `$0`。

### 5.4 推导出来的每 tick 界

```
reservation floor (rate card): 3080 micros
reservation used  (route-estimate max × headroom): 10992 micros   (was a flat 50000)
  passes=1 share=0.5: windows/tick=15  bound_by=paid_calls  by_cost=15  by_calls=15
  passes=3 share=0.5: windows/tick=5   bound_by=paid_calls  by_cost=5   by_calls=5
  passes=1 share=1.0: windows/tick=31  bound_by=paid_calls  by_cost=31  by_calls=31
```

（日上限 $100、`max_daily_paid_calls 9000`、288 tick/天。）
**结论与直觉相反：正确的界比 live 现在配的 30 更小，不是更大。** 30 + 10 + 10 意味着
满负荷时一天 14,400 次付费调用，超出 mission 自己的 9,000。今天没炸只是因为队列早就空了。
把车道份额提到 100%、只跑 prose 一档，推导值是 31——这才是 live 那个 30 的合法依据。
注意补上 route 估价这条路之后，**成本与次数两条界几乎重合**（15 vs 15），
也就是说预留一旦诚实，钱和次数一样紧——这正是 B1 想说的事。

## 六、到「ACN 多空各 ≥2 个独立卖方来源」还差多久

三段账，按顺序：

1. **取 9 份 ACN 卖方研报**：AlphaEngine 130/24h、且 `get_document` 每 session 约 20 次上限，
   一份研报按 3 页算约 3 次调用。9 份 ≈ 27 次调用，在**一天的额度内**，前提是
   **discovery planner 不再因为「required = 3 已满足」叫停**（第二节根因 2，不在本片范围）。
2. **读**：27 个窗口，新排序下 `sell_side` 排在 news 之前，按推导界 15 窗口/tick、
   五分钟一 tick，**两个 tick 内读完**，成本 $0.083。
3. **数得出独立来源**：这是本片解决的部分。ACN 现有 3 份 + 新增 9 份 = 12 份，
   加上多公司归因带来的**另外 3 份挂在别家名下、点名 Accenture 的 Wells Fargo 研报**——
   不过按 owner 裁决（B2）只有那份研报里**完全不点名 Cognizant 的句子**才会成为 Accenture 的 Claim，
   所以这 3 份的增量是「几条」而不是「几十条」。出处落库后，独立性阶梯数的是**券商**：
   live spool 重放已经能看到 12 家，ACN 名下 7 家。

**所以：只要第 1 步的 planner 松口，一天之内 ACN 就能有两位数的卖方 Claim 和多家券商的出处。**
但要提醒 DebateMap：卖方 tier 的实测产出只有 2.35 条 Claim/文档，且抽取契约拒绝一切数字，
所以「目标价分歧」这类最有辨识度的多空证据**不会**从这条路进来——它属于 P11b 的 consensus 双路。

---

## 七、留给 owner 的（不是代码）

1. **AlphaEngine 130/24h**，heartbeat 09-09 21:34 已 `spent 133 / cap 130`。
   ACN 的 9 份研报、7 份电话会、DXC 的 17 份研报都停在这里。
2. **`get_document` 每 session 约 20 次的上限**：一份多页研报要多次调用，这是取一份研报的真实单价。
3. **`broker_research.required = 3` 该不该为 DebateMap 抬高。** 这是清单语义问题：
   Initial Screen 要 3 份就够，「多空各 ≥2 个独立券商」要的是**不同券商**而不是**更多份数**。
   建议 DebateMap 自己声明一条按 `broker_key` 去重的需求，而不是把清单下限调大。
4. **`document_extraction.max_windows_per_tick` 现在配的 30/10/10 超出 mission 的 9,000 次调用上限**
   （满负荷 14,400）。本片给了推导函数，但没有改 `service.json`——那是部署动作。
5. **main `7011104` 上有一个 ad-hoc 研究池的生产缺陷**（本片只是撞上了它的测试形态）：
   loop 的 `created_at` 没有时钟接缝，分日却用调用方的时钟，所以跨 UTC 午夜的一轮准入
   会把 reservation 记进明天的桶，任务不被计入任何它据以准入的池子。
   同一片算术上还有两条（`admitted_loops` 返回所有版本导致预算重复计入；
   `POOL_SHARE 0.25` × $1.00 最低成本使仓库里所有 fixture 预算下无任何 inquiry 可准入）。
   诊断见第四节，均由 code-review 复核确认，修在 P14e / C2 的切片里。

## 八、接线需求（集成时统一做，本片没碰）

- `extraction_backlog(company)` 接进 cockpit 与 P14a 的 SourceCapabilityMap 消费者。
- `extraction_backlog_schema.sql` 走 Wave 0 的 `*_schema.sql` glob 打包，无需改 `pyproject.toml`。
- 抽取子进程 summary 新增 `provenance`（按 mission 指针逐条）与 `unreadable_reviews` 两个字段，
  cockpit 可直接读 `unreadable_reviews` 来回答「队列深度为什么下不去」；六类永久失败里现在也含
  「渲染出来是空的」（`source offset must be a valid bounded window`）。
- P12c 的 `document_attribution()` 与本片的 `document_attribution_rows()` 一行合并（见 5.2）。
