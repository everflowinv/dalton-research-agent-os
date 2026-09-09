# C2：容量配额池与 tick 账本 v1.0

日期：2026-09-09
分支：`w2-budget-pools`（worktree `~/Projects/dalton-w2-budget-pools-worktree`），基线 main `7708d43`（2,843 项）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 C2 与第 1 节「日累计不超过 mission `max_daily_cost_usd` 的 25%」、[vision 复盘 C2 / C4](vision-review-against-plan-v1.0-2026-09-09.md)、[P14e 报告](p14e-research-tasks-v1.0-2026-09-09.md) 开放问题 2、Q2 报告「Core 里没有 tick 账本」

---

## 0. 一句话

一天的钱从「先到先得的队列」变成**四个具名的池**，每一笔准入都带着自己的池写进日账本、结算时由账本自己把池抄给结算行（所以「池和账对不上」在 schema 层就不可能发生）；同时每个 tick 现在留下一行**永不覆盖**的记录，Q2 那三个 `available: false` 的指标（闲置率、lane 卡顿、按池的花费）今天起可算。

## 1. 池的语义

| 池 | 默认份额 | 装什么 |
| --- | --- | --- |
| `coverage` | 55% | mission 存在的理由：发现、抽取、报表、股价、模型规格与预测、研究计划、Initial Screen |
| `event_response` | 15% | 今天发生了什么：价格异动、filing、新闻、事件判断 lane、tracking 频率调整 |
| `adhoc` | 25% | 没人事先写下来的问题——P14e 的 `ResearchTask`。25% 就是主 agent 解禁 `adhoc_research` 时定的边界 |
| `maintenance` | 5% | 收拾架子：catalog、Claim 索引打标、质量打分、每周 reflection、退役与清理 |

四条规则：

1. **池是准入闸门。** 付费调用准入前先看它的池还有没有额度。没有就返回
   `{"status": "rejected", "reason": "pool_exhausted", "pool", "spent", "cap", ...}`，lane 说
   `skipped:pool_exhausted`（P14e 造的词，C2 把它推广成常量 `budget_pools.POOL_EXHAUSTED_STATUS`）。
   **返回而不是抛异常**：池用完的 lane 不是坏了，是今天做完了；tick 摘要里写
   `unavailable:RuntimeError` 说的是反话。
2. **池同时是结算口径。** 池随 `mission_binding` 进入 `ThesisImpactBudgetStore.admit`，
   落成 `thesis_impact_day_admissions.pool` 一列；`settle()` **不接受调用方给的池**，而是在同一个
   事务里从准入行读出来抄进 `thesis_impact_day_settlements.pool`。这就把 P14e 开放问题 2 与
   P14a review 都点到的「准入有池、结算没池」关掉了：池和账是同一行。
3. **未用份额只有 `coverage` 能借，且只能在被借池的当天过半之后借。** 上午十一点还没动过的
   事件池不是闲置额度，是安静的上午。借出的准入带 `borrowed_from`（`{lender: micros}`），
   出借方的「已借出」会从它自己的可借额度里扣掉——同一块闲钱不会被借第二次。借款要么足额
   要么不借（半额贷款等于把缺口甩给下一个发现它的 cap）。
4. **超池的拒绝不毒化身份。** 它写进新表 `model_budget_pool_rejections`，不写
   `thesis_impact_day_rejections`——后者的行是对某个准入身份的**永久判决**（「被拒过的准入不能
   再被准入」），而池在午夜会重新装满。

### mission 预算里的字段（需要 `coverage_mission.py` 的 owner 加）

本片**没有改** `coverage_mission.py`。`budget` 是三个键的闭集，加第四个键要动那个文件与 mission 版本。
所以：

```json
"budget": {
  "max_daily_paid_calls": 9000,
  "max_daily_cost_usd": 100.0,
  "max_alphaengine_calls_24h": 130,
  "pools": {"coverage": 55.0, "event_response": 15.0, "adhoc": 25.0, "maintenance": 5.0}
}
```

- 形状：**美元**（不是份额），四个池名必须齐全，和 ≤ `max_daily_cost_usd`。少一个、多一个、
  为负、超订，都是 `BudgetPoolError`，不做「善意修复」。
- 集成时要做的：`coverage_mission.py` 的 `_closed(body["budget"], frozenset({...}), "budget")`
  加 `"pools"`，并按上面的规则校验；常量与校验函数已经在
  `budget_pools.pool_caps()` 里，直接调即可（`MISSION_POOLS_FIELD` 是那个字段名的唯一出处）。
- 在那之前：`pool_caps()` 用默认份额推导，返回 `"defaulted": true` 与
  `"source": "budget_pools.DEFAULT_SHARES"`，`pool_status()` 原样透出成 `caps_defaulted`。
  **推导出来的分法会说自己是推导的**，不冒充 owner 的决定。

## 2. lane → 池 映射表

映射集中在 `budget_pools.LANE_POOLS`（按 operation 名），**没有改任何 lane 模块**。
优先级：`LaneSpec(budget_pool=...)` > `LANE_POOLS` > `coverage`（默认，也就是今天所有 lane 的现状）。

| lane operation | 池 |
| --- | --- |
| `dispatch_mission_source_discovery` / `dispatch_guidepoint_discovery` | coverage |
| `dispatch_document_extraction` | coverage |
| `dispatch_mission_stage` | coverage |
| `dispatch_mission_sec_quarters` / `dispatch_mission_statements` | coverage |
| `dispatch_mission_market_prices` | coverage |
| `dispatch_company_model_spec` / `dispatch_company_model_forecast` | coverage |
| `dispatch_research_plan` / `dispatch_initial_screen` | coverage |
| `dispatch_sales_notes_feed` / `dispatch_company_wiki_feed` | coverage |
| `dispatch_claim_review` | maintenance |
| `dispatch_research_task` | **adhoc** |
| （未登记，预留）`dispatch_research_events` / `dispatch_event_judgement` / `dispatch_tracking_cadence` / `dispatch_market_events` / `dispatch_catalyst_calendar` | event_response |
| （未登记，预留）`dispatch_claim_index` / `dispatch_catalog_sync` / `dispatch_research_reflection` / `dispatch_claim_retirement` | maintenance |

cockpit 形态的模型调用不是按 lane 而是按 **purpose** 准入的（`cockpit_model` 是唯一的准入口），
所以另有 `PURPOSE_POOLS`：`ask` / `goal` / `steer` / `draft` / `plan` / `model_spec` → coverage；
`claim_index` / `quality` → maintenance；预留 `research_task` / `adhoc_research` → adhoc、
`event_judgement` / `tracking` → event_response。未登记的 purpose → coverage。

`LaneSpec` 新增两个字段，默认值保持今天的行为不变：
`budget_pool: str | None = None`（None = 用中央映射）、`pool_share: float | None = None`
（**v1.0 只登记不执行**：按 lane 的子上限需要日账本里有按 lane 的归集，而今天只有 cockpit
形态的调用有 `pool_lane`；`pool_status` 会把它透出来）。

## 3. tick 账本

`tick_ledger.py` + `tick_ledger_schema.sql`，两张表：`tick_ledger_ticks`（一 tick 一行）与
`tick_ledger_lanes`（一 tick 一 lane 一行：driver key、lane operation、池、状态原文、状态词、
是否 idle、是否 `pool_exhausted`、lane 报的小计数（bounded JSON）、lane 自报的池花费增量）。
`bounded_planner_driver.run_once` 在每个 tick 末尾**一个事务**写入，位置在
`scheduler.sqlite` 旁边（`tick-ledger.sqlite`，从 `scheduler_db.parent` 推出来，**没有动
`BoundedPlannerDriverConfig` 的闭合形状**，所以已装好的 `service.json` 不用改）。

- 读者：`ticks(window)`、`idle_ratio(window)`、`lane_stalls(window)`、`spend_by_pool(window)`。
  `window` 可以是 `None`（保留窗）、天数、或 `(since, until)` 两个 `YYYY-MM-DD`。
- **保留是读者的窗口，不是删除**：任何读者都不会看超过 90 天（`RETENTION_DAYS`），行永远留着，
  以后加归档器不必先相信「没人删过证据」。
- 空窗口报 `available: false` 加原因，不报一个舒服的 0——这是 Q2 立的规矩。
- `idle` 是状态**词**不是「没有」：一个什么都没报的 lane 记成 `missing` 而不是 idle。
- 池花费：tick 行同时记当天的**累计**（来自日账本 `day_pool_spend_at`，按当天、按池）与相对
  当天上一个 tick 的**增量**。累计能和账本对账，增量能按周求和。日账本不在、读不了、还没迁移
  过，都只是「这一格没有数」，不影响 tick。
- 写账本失败**不会**让 tick 失败，但也**不会**默默失败：摘要里多一个
  `tick_ledger: {"status": "recorded" | "duplicate" | "unrecorded:<Type>"}`。这个键同时加进了
  `RESERVED_DRIVER_KEYS` 与驱动自己的 `_RESERVED_SUMMARY_KEYS`（两者仍由那句 `assert` 锁着），
  这样它既不会被某个 lane 的 driver key 覆盖，也不会被 Q2 的 `idle_tick_ratio` 误当成一条 lane。
- 同一个 tick 重复写是 no-op（tick id = `content_hash(started_at, ended_at)`），一次重试不会
  把一天算两遍。

## 4. Q2 / P14e / P14a 现在应该读什么

| 谁 | 原来 | 现在 |
| --- | --- | --- |
| Q2 `ResearchCycleReflection.idle_tick_ratio` | 靠调用方交 `--tick-summary-dir`，没有就 `available: false` | `TickLedger(path).idle_ratio(window)`，返回形状与它自己的输出对齐（`available` / `ticks` / `idle_ticks` / `ratio` / `idle_by_lane`） |
| Q2「lane 卡顿」 | 不可算 | `lane_stalls(window)`：每条 lane 的 `ticks` / `stalls` / `stall_ratio` / `longest_stall_run` / `last_status` / `pool_exhausted_ticks`。**超池不算卡顿**，它是预算做的决定 |
| Q2「各 lane 花费 / 池上限」按前缀猜 | `work:cockpit-plan-...` 猜用途 | `spend_by_pool(window)`（周级）与 `budget_pools.pool_status(store, mission=..., day=...)`（日级）。每一微元都是**准入时**归的池，不是事后从 id 上猜的 |
| P14e `research_task.pool_state` | 从 loop 权威推导 adhoc 池的**预留** | 保持不变（准入闸门仍然可以从自己的权威推导）；**实际花掉的钱**现在从 `pool_status(...)["pools"]["adhoc"]["spent_micros"]` 读，两者第一次可以互相对账 |
| P14a 事件 lane | 无池 | 在 `LANE_POOLS` 里已预留 `event_response`；lane 模块**不需要改**，合并即生效 |
| INT1 cockpit | 无 | `pool_status(...)`：每池 `cap/spent/borrowed/borrowed_from/lent/remaining/exhausted`，加 `exhausted_lanes`（今天撞过 `pool_exhausted` 的 lane、池、work order、时刻、当时的 spent/cap）、`unpooled_micros`、`borrow_open`、`caps_defaulted` |

## 5. 迁移

- `budget_pools.apply_pool_migration(connection)` 由 `ThesisImpactBudgetStore.__init__`（非只读时）
  调用：跑 `budget_pools_schema.sql`（新表 `model_budget_pool_rejections` + 索引），再按
  `PRAGMA table_info` 幂等地补四列——`thesis_impact_day_admissions.pool` /
  `.borrowed_from` / `.pool_lane` 与 `thesis_impact_day_settlements.pool`，全部可空。
- **没有任何 content hash 变化。** 池是**列与 binding 记录**，不是被哈希的 wire 里的字段：
  上周写下的准入今天仍然按原样校验。这是刻意的取舍——池是「这一天怎么分账」的路由事实，
  不是准入的身份。
- 没有池的准入（C2 之前的每一个调用者，包括 `document_extraction`）行为**完全不变**：
  不过闸门、不占池、日 cap 仍然抛 `ThesisImpactDayBudgetExceeded`，在 `pool_status` 里记成
  `unpooled_micros`（而不是偷偷算进 coverage）。
- schema 文件走 Wave 0 的 `*_schema.sql` glob，打包不用改 `pyproject.toml`。
- tick 账本**故意不开 WAL**：进过 WAL 的库头里会留下标记，而 `connect_read_only` 拒绝没有
  `-wal` / `-shm` 兄弟文件的 WAL 库——干净关闭的账本正好就是那个状态。读者是 cockpit 与周报，
  读的是几分钟前写完并关闭的文件。

## 6. 只读实测（live 副本，`/tmp/c2-smoke/thesis-impact-budget.sqlite`）

按默认分法给 live 日账本的准入分类（用 `classify_legacy_work_order`，只用于读历史，
live 准入永远不猜）。mission `coverage-mission:us-it-services`，`max_daily_cost_micros`
= 100,000,000（100 USD/天）：

| 日期 | 准入数 | 总花费 | coverage(55) | event(15) | adhoc(25) | maint(5) |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-09 | 1,958 | 18.8064 | 18.8064 | 0 | 0 | 0 |
| 2026-09-08 | 2,434 | 4.5507 | 4.5507 | 0 | 0 | 0 |
| 2026-09-07 | 626 | 0.4366 | 0.4366 | 0 | 0 | 0 |

今天的 18.81 USD 按 work order 家族拆开：

| 家族 | 次数 | 花费 USD | 池 |
| --- | --- | --- | --- |
| `document-extraction` | 707 | 0.2577 | coverage |
| `document-numeric` | 679 | 0.3750 | coverage |
| `metric-discovery` | 402 | 0.1579 | coverage |
| `cockpit-plan`（研究计划） | 105 | **14.4368** | coverage |
| `cockpit-draft` | 48 | 0.0729 | coverage |
| `cockpit-model_spec` | 15 | 3.5046 | coverage |
| `cockpit-ask` | 2 | 0.0015 | coverage |

结论三条：(1) 按默认分法**今天不会有任何池被打爆**（coverage 用掉 18.81/55 = 34%）；
(2) 今天所有的钱都是 coverage，`event_response` / `adhoc` / `maintenance` 三个池还没有消费者
（对应的 lane 没上线或没跑）——所以 15/25/5 这三档目前是**为将来预留**，不是对现状的描述；
(3) 单笔最贵的是研究计划（`cockpit-plan`，占今天的 77%），它落在 coverage，这一点值得 owner
知道：**规划本身比抽取贵一个数量级**。

## 7. 测试

新增 `tests/test_budget_pools.py`（36 项）与 `tests/test_tick_ledger.py`（16 项）：池上限
（有 / 无 `pools` 字段）、按池准入、借用规则（只有 coverage、只在过半之后、同一块闲钱不借两次、
足额或不借）、超池返回而非抛出且不毒化身份、结算归到准入的池且金额是**实际服务的那一条链路**
的价目、registry 对没声明池的 lane 行为不变、非法池名 / 份额在注册时被拒、tick 每 tick 每 lane
写一行、四个读者在两个合成周上的答案、保留窗口、账本写失败被报告而不掀 tick。

全量：

```
Ran 2895 tests in 305.372s

OK (skipped=1)
```

（基线 2,843 + 52 = 2,895；命令 `PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`）

## 8. 没做 / 开放问题

1. **`llm_planner_execute` 还没有池维度。** driver 通过写入端 RPC 让 loop 前进，
   `OPERATION_FIELDS["llm_planner_execute"]` 是闭集，加一个 `pool` 参数必须动
   `writer_server.py`（本片禁止触碰）。所以研究任务 loop 的模型花费目前仍然算在 planner 的
   mission 绑定上，而不是 `adhoc` 池。集成时的最小改动：给该 operation 的字段集加 `"pool"`，
   在 `_op_llm_planner_execute` 里透传到它的 `mission_binding`。**这是 P14e 开放问题 2 剩下的
   最后一半**，也是 driver「把 lane 的池传进准入」唯一还差的一处。
2. **`document_extraction.py` 的准入还没带池。** 它不在本片的所有权范围内。带上之后
   `unpooled_micros` 会归零、coverage 池才真正开始约束抽取——从第 6 节看，加上也不会打爆。
   改动是一行：给它的 `mission_binding` merge 一个 `mission_pool_scope(mission, operation=...)`。
3. **`maintenance` 5% 可能偏紧。** Claim 索引打标（`claim_index`）是逐条 Claim 的模型调用，
   一旦上线，5% 是否够要看实测；今天没有 live 数据可以判断。第一次出现
   `skipped:pool_exhausted` 的多半是它。
4. **借用规则用的是日账本自己的时钟**（`ThesisImpactBudgetStore.clock`），而 cockpit 传的是
   自己的 clock。生产里两者都是真实时间所以一致；写测试的人要知道这一点（本片的测试用
   「所有池都是 0」来避开时钟依赖）。
5. **`pool_share` 只登记不执行**（见第 2 节末）。要执行需要日账本按 lane 归集，也就是先做第 1、2 条。
6. **`raise_day_budget_cap.py`** 现在会在输出里多一块 `default_pool_caps_usd`，告诉 owner 新的
   日 cap 按默认分法各池是多少。它**不写** mission 的 `pools`——那是 mission 版本的事，人来发。
7. **cockpit / install.sh 接线没做**（本片禁止）。INT1 要的入口是
   `budget_pools.pool_status(store, mission=..., day=...)` 与
   `tick_ledger.summarise(path, window)`，两个都是纯读。
