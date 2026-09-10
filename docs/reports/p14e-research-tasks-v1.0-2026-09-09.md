# P14e 专项研究派发：inquiry 变成有预算的 BoundedPlannerLoop

日期：2026-09-09（v1.3：2026-09-10 修 W2 报告第九节的四条 finding；基线 main `0fdbfab`）
分支：`p14e-findings`（前身 `p14e-adhoc-research` → `p14e-pool-test`）
状态：代码完成，全量测试通过；**live 未启用**，需要 owner 三步（见 §4）

---

## 1. 一句话

`ResearchTask` 不是新对象、不是新 dispatcher、不是新表：它是一条 **question 来自 planner
inquiry** 的 `BoundedPlannerLoop`。派发它的是已经存在的 `bounded_planner_driver.run_once`，
本切片只加了「准入」和「预算池」两件事。

蓝图 §3 ③ 说的「inquiry 没有下游」到此闭合：最新一版 live 研究计划的三条 inquiry，在 owner
放行后会各自变成一条 2 轮、$1.00 预算的 loop（§7 冒烟实测）。

## 2. 复用什么、新增什么

**复用（一行代码都没改的）**

| 复用物 | 起什么作用 |
| --- | --- |
| `bounded_planner_driver.run_once` | 每 tick 遍历 `bounded_planner_active_loops`，提议 → 准入 → 跑探针 → 记 outcome → 记 observation。研究任务就是其中一条 loop，**驱动侧零改动** |
| `BoundedPlannerAuthority.create_loop` / `submit_proposal` / `admit_proposal` | rounds/cost_units/seconds 三重预算与终态闸门原样使用，`_budget_snapshot` 与 `_admission_reason` 就是「budget_exhausted」的实现 |
| `bounded_planner_loop_schema.sql` 十张表 | **loop 表就是账本**，本切片没有建 `research_task_schema.sql` |
| `ResearchQuestionBacklog.record_question` | inquiry 落成一条 exact question（P8c-3 给 observation 用的同一个入口） |
| `bounded_probe_executor` / `bounded_alphaengine_probe` | 探针执行；`executable_probe_operations()` 从这两个模块**读**，不写死 |
| `CoverageMissionAuthority.latest_research_plan` | inquiry 的来源 |
| `LaneChildLauncher`（P13aj） | 票据、单槽、重启后 orphaned 判定 |
| `lane_registry.LaneSpec`（Wave 0） | 接线只有一行 `LANE_MODULES` |
| `thesis_impact` 独立核验者模式 | 预算「先预留、后结算、失败就守旧」的读法，见 §3 |

**新增**

| 文件 | 是什么 |
| --- | --- |
| `src/dalton_core/research_task.py` | 适配器 + 授权判定 + 预算池 + cockpit 投影。无 schema |
| `src/dalton_core/mission_research_task_lane.py` | lane：每 tick 准入 ≤N 条、结算已完成的、无变化就 hold |
| `src/dalton_core/research_task_launcher.py` | `LaneChildLauncher` 子类（约 70 行，全是本 lane 自己的部分） |
| `src/dalton_core/research_task_cli.py` | 子进程：读计划、判授权、按序准入、写 summary。**不调模型、不跑探针** |
| `deploy/phase8/p14e-adhoc-probe-templates-v1.json` | 三个 ProbeTemplate 的 **proposed** 发布清单 |
| `scripts/run_p14e_research_task_smoke.py` | 只读冒烟 |
| `tests/test_research_task.py`、`tests/test_mission_research_task_lane.py` | 33 项 |

**共享文件的改动（全部是加法）**

- `bounded_planner_driver.py`（review 时解禁，只做两处最小改动，见 §9）：探针执行包 try、
  materialize 失败按「pending_round」如实报、无轮次的 loop 不问付费模型、
  `DEFAULT_PLANNER_MAX_COST_USD` 命名一次。

- `bounded_planner_loop.py`：新增 `INQUIRY_ADMISSION_SOURCE`、`_validate_admission`、
  `create_loop(admission=...)`、`loop_for_admission()`、`admitted_loops()`。
  `admission` 缺省为 `None` 时行为逐字节不变——identity 里不加字段，所以**历史 loop 的
  version_id 不变**；只有 inquiry loop 允许 `automation:` actor，人工 loop 仍强制 `human:`。
- `agenda_control.py`：`adhoc_research_enabled` 从字面量 `False` 变成 `self.adhoc_research_enabled()`。
- `lane_registry.py`：`LANE_MODULES` 加一行。
- `tests/test_lane_registry.py`：四处字面量集合加本 lane（那些集合是「掉一条 lane 要看得见」的护栏）。

`answer_routing.py` **没有改**。理由见 §8 开放问题①。

## 3. 预算池 `adhoc`

- **口径**（v1.2：C2 合入后改为读 C2）：cap 由 `budget_pools.pool_caps(mission["budget"])`
  的 `caps_micros["adhoc"]` 给出——mission 声明了 `budget.pools` 就用声明的，没声明就用
  `DEFAULT_SHARES["adhoc"]`（25%），并如实标 `caps_defaulted`。`research_task.POOL_SHARE`
  现在就是 `DEFAULT_SHARES["adhoc"]`，不再是本模块自己写的 `Decimal("0.25")`。
  live 是 $100 → **$25/天**。lane 归池由 C2 的 `LANE_POOLS["dispatch_research_task"]`
  决定，`LaneSpec` 上不再重复声明（C2 有一条测试钉「没有 lane 覆盖那张表」）。
- **预留而非事后统计**：一条任务在准入时就按 `max_rounds × $0.50`（`bounded_planner_driver`
  的 `planner_max_cost_usd` 缺省值）预留。只统计已结算的池会在第一笔账单出来之前把一天的
  任务全放进来——这正是 `ThesisImpactBudgetStore.admit` 用 `reserved_micros` 预留、
  `settle` 再改成实际值的原因，此处沿用同一读法。
- **账本在哪**：`day_reserved_micros()` 直接从 loop 表推导（`admission.source='inquiry'` 的
  loop，按 `created_at` 的日期 + 自己的 `budget.max_rounds`）。**没有第二本账**，所以池和
  准入永远不可能各说各话。这是**预留**那一半；**结算**那一半现在是 C2 的
  `budget_pools.pool_status`（池随 admission 进日账本，settle 从 admission 行继承池）。
  两者量的是不同的东西，都要有：准入时还没有任何花费可数，结算时预留已经不是事实。
- **日期口径**：池的「今天」是**权威给 loop 盖的 `created_at` 的 UTC 日期**，不是别处的
  本地日期。测试里写死过一个日期，结果第一次跨过 UTC 午夜（本地 20:00 EDT）之后，
  池就按一个没有任何 loop 的日子去算，读出来永远是空的（见 §10）。
- **超池**：lane 返回 `skipped:pool_exhausted`（C2 要推广的就是这个词），单条 inquiry 在
  `plan_admissions` 里返回 `reason: "pool_exhausted"`。不借用、不排队。
- **写入范围**：任务的产出只经既有 scope——Claim 走 candidate staging、observation 走
  `record_observation_followup`、deliverable 走 `mission_deliverable`。loop 的探针模板一律
  `read_only`（`publish_probe_template` 本身拒绝带 `write:`/`delete:` 的 side effect），
  所以一条任务**在结构上**写不了 Thesis / Playbook / Mission。

live 数字：$25 池 ÷ $1.00 一条任务 = 一天最多 25 条专项研究，而 extraction 的 $75 一分不动。

## 4. owner 启用步骤（三步，缺一不可）

`adhoc_research_enabled` 现在是一个**问句**，答案由两个版本化的 owner 动作决定，第三步是
live 特有的前置条件：

1. **发一版 mission**，`autonomy.may_write` 加 `research_task`
   （Wave 0 已把这个词加进 `AUTOMATION_WRITE_SCOPES`，live 第 13 版没有授予）。
   缺它 → `mission_does_not_grant_research_task`。
2. **发布 ProbeTemplate**：按 `deploy/phase8/p14e-adhoc-probe-templates-v1.json`，
   **只发 `status: active` 的那一个**（SEC filings index），`actor_ref` 必须是 `human:`
   （本切片从不代签，测试用 fixture 自己发）。缺它 → `no_executable_adhoc_template_published`。
3. **发一版 mandate**，`scope_refs` 覆盖 universe 五家。live 第 7 版只有
   `industry:us-it-services` 与 ACN，而最新计划的三条 inquiry 全是 EPAM / IBM / CTSH，
   现在一条都进不来（§7）。缺它 → 每条 `out_of_mandate_scope`。

这三步之外**不需要别的**：ProbeTemplate 发布本身就是「policy version enables it」那一腿
（人签、版本化、append-only、可按上文三条路撤销），没有第四个开关。

另外要装 lane：在 state 目录放 `research-task-lane.json`
（`{"max_admissions_per_tick": 1, "retired_templates": []}`），
`argv_fragment` 才会给 writer 加 `--research-task-lane`。没有这个文件，lane 整个不存在，
LaunchAgent 的 argv 一字不变（`test_service` 的断言因此不受影响）。

**三个模板里今天只有一个能跑。** `bounded_probe_executor` 只执行
`(get_company_facts, public_sec_read)`，`bounded_alphaengine_probe` 只执行
`(alphaengine_get_document, alphaengine_read)`。`bindable_templates()` 按
**`(operation, permission_scope)` 对**放行（两个执行器都是**先**校验 scope，而
`permission_scope` 是模板里的自由文本，只比 operation 会让一个把 `public_sec_read` 写成
`public-sec-read` 的重发版本通过准入、然后在执行处被拒）。SEC filings index 今天可用，
web-search 与 AlphaEngine `search_library` 是「已入目录、等执行器」。

**撤销**（review S4）：Core 的模板版本是 append-only、没有 status 列，所以撤销有三条路，
owner 三条都需要——(a) 用一个执行器不接受的 `(operation, permission_scope)` 对重发该模板，
这是权威层的撤销；(b) 目录层：`ADHOC_PROBE_TEMPLATES` 与部署清单里把 `status` 改成
`retired`，对所有部署生效；(c) 本机层：`research-task-lane.json` 的 `retired_templates`
列出模板 ref，今晚就能生效、不用发版。三条任意一条命中，该模板即不可绑；全部模板都不可绑
时授权自动回到 `no_executable_adhoc_template_published`。

## 5. ProbeTemplate 目录：一个 active，两个 retired

| template_ref | operation | permission_scope | cost（units/attempts/seconds） | status |
| --- | --- | --- | --- | --- |
| `probe-template:adhoc-sec-filings-index:v1` | `get_company_facts` | `public_sec_read` | 1 / 2 / 120 | **active** |
| `probe-template:adhoc-alphaengine-search-library:v1` | `alphaengine_search_library` | `alphaengine_read` | 2 / 1 / 120 | **retired** |
| `probe-template:adhoc-web-search:v1` | `public_web_search` | `public_web_read` | 1 / 2 / 60 | **retired** |

**v1.3 改（finding 3）**：后两个原本挂着 `status: active` 发布，读起来像可用的能力，
但 `_parameters_for` 只有 `get_company_facts` 一条分支，它们**永远拿不到参数、永远不会出现在
任何 binding 里**。复核 main 上的执行器后确认：`bounded_probe_executor` 仍只跑
`get_company_facts`，`bounded_alphaengine_probe` 仍只跑 `alphaengine_get_document`
（`search_library` 没有对应物）。所以**不是补分支，而是把它们标成 `retired` 并写明理由**
（`retired_reason`：`no executor: ...`）——补一条拿不到执行器的分支只会把失败推后。
`_parameters_for` 的 `return None` 处也写下了这一条：没有执行器就没有参数可造。

同时 **cockpit 投影只宣传 `bindable_templates`**：`research_task_view` 新增
`templates` 字段，值就是可绑集合（今天只有 SEC 那一个），`grant.template_refs` 与它一致。
目录里有、执行器跑不了的东西，不会再作为「能力」出现在 owner 面前——那正是让人以为
「web search 已经跑过了」的路径。冒烟脚本也不再模拟发布 retired 的模板。

`allowed_hosts` 与 `cost_estimate_usd` 只在清单里（Core 的模板记录是闭合形状，host 白名单由
执行探针的 transport 强制），清单与 `research_task.ADHOC_PROBE_TEMPLATES` 由测试钉死不许漂。

## 6. 测试

```
Ran 4104 tests in 603.295s

FAILED (failures=14, skipped=1)
```

**这 14 项全部在 `tests/test_rehearse_deploy.py`，且在基线 main `0fdbfab` 上原样失败**
（`git stash` 后单跑该模块：`Ran 47 tests ... FAILED (failures=14)`）——
`test_every_governance_record_the_script_seeds_is_in_the_list`（13 条 connector 治理记录）
与 `test_every_shipped_schema_has_a_named_owner`，都与 P14e 无关（本切片不新增 schema、
不新增治理记录）。除它们之外全部通过。P14e 相关模块单跑：

```
Ran 119 tests in 5.651s
OK
```

（`tests.test_research_task` / `tests.test_mission_research_task_lane` /
`tests.test_bounded_planner_driver` / `tests.test_agenda_control` / `tests.test_budget_pools`）

`tests/test_mission_research_task_lane.py` 单跑（连跑三次）：

```
Ran 13 tests in 0.573s
OK
```

（合 main `0fdbfab` 之后；本切片 55 项。命令：
`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`）

合并时 `tests/test_lane_registry.py` 取 main 那一侧：那四处字面量在 main 上已改成**包含**
断言（「P14-0 之前写死过的 lane 仍在」），P14-0 之后新增的 lane 不该出现在那里，
`dispatch_research_task` 因此从三处集合里撤出，改由本切片自己的测试钉。

覆盖到的每条要求：三条 inquiry 的计划里一条准入、一条同哈希被拒（`already_admitted`）、
一条出宇宙被拒（`out_of_universe`）；宇宙内但 mandate 外单独一条（`out_of_mandate_scope`）；
改了问题文本就是新任务；跨计划再出现同一条仍被拒；池耗尽（`pool_exhausted` /
`skipped:pool_exhausted`）；昨天的任务不花今天的池；终态闸门（`human_deprioritized` 成功、
`coverage_complete_unobservable_candidate` 被闸门驳回）；模板子集（只有可执行的能绑、
非 ad-hoc 目录的模板不会被绑、行业级 inquiry 无可绑模板）；开关语义（两个 owner 动作、
cockpit flag 在解析器缺席/抛错时都是否）；人工 loop 仍需 `human:`、且 identity 不变；
lane 的 launched / busy / held / 一小时后解除 / 失败后 hold / not_granted / 结算；
LaneSpec 注册与「没有配置就没有这条 lane」。

review 之后补的：scope 写错的重发模板不可绑（B1）；执行契约就是两个执行器各自的那一对；
模板三条撤销路径各一例，其中 lane 配置撤销会让 lane 在 spawn 之前就停（S4）；
换行重排的同一问题哈希相同、真改了文本哈希不同（S1）；entry 自带 `ordinal`、
第一条被拒时子进程仍准入正确的那一条（S2）；`limit` 之外的条目报
`deferred_to_a_later_tick` 而不是 `pool_exhausted`；已准入的任务可以用 `prior_version_ref`
升到 v2 而不变成新任务（S3）；driver 的四项（pending_round 不付费、doctrine 故障仍报
doctrine 故障、没轮次不问付费模型但仍跑免费规划器、单价只写一处）；被执行器拒绝的探针变成
一轮 `source_unavailable` 且下一轮能继续（B2）。

## 7. 冒烟（只读，无模型调用）

`scripts/run_p14e_research_task_smoke.py --core /tmp/p14e-core.sqlite`（先把 live 只读副本
复制到 /tmp，脚本再用 SQLite backup 复制一次，所有模拟写入都落在副本里）：

- live 授权：`granted: false`，原因 `mission_does_not_grant_research_task` +
  `no_executable_adhoc_template_published`。
- 池：`cap_usd 25.000000`（mission $100 的 25%），今日已预留 0。
- 最新计划 `mission-research-plan:4005c857…`（2026-09-09T15:24Z）三条 inquiry，**按现状全部
  `out_of_mandate_scope`**（EPAM / IBM / CTSH 都不在 mandate v7 的 scope 里）。
- 把 mandate 放宽到 universe 后（只在内存里模拟）：三条**全部可准入**，各
  `max_rounds 2 / max_cost_units 2 / max_seconds 240`，预留 **$1.00** 一条，合计 $3.00，
  占当日池的 12%。

## 8. review 之后改了什么（B1 / B2 / S1–S4）

| 编号 | 改动 |
| --- | --- |
| B1 | 可绑判定从 operation 改成 `(operation, permission_scope)` 对，从两个执行器模块读 |
| B2(a) | materialize 失败时区分「round 还挂着」与「doctrine 坏了」；**付费模型调用之前**就知道 loop 的状态；`remaining_budget.rounds_remaining < 1` 的 loop 不问付费模型，但仍跑免费的确定性规划器，好让它能走到终态 |
| B2(b) | 探针执行包 try：执行器拒绝（scope / operation 不符）不再从 `run_once` 抛出去，而是记一轮 `status: failed` 的 ResultEnvelope → `source_unavailable` outcome。**「loop 永远 pending」这个状态被关掉了**——它原本会让此后每个 tick 都 materialize 失败，而且 summary 里看不出来 |
| B2(c) | `DEFAULT_PLANNER_MAX_COST_USD` 在 driver 里命名一次，`research_task.default_planner_cost_usd()` 读它，单价不再有第二份 |
| S1 | inquiry 哈希前 `" ".join(v.split())`：模型换行方式变了不算新问题 |
| S2 | entry 带 `ordinal`，子进程按 ordinal 取 inquiry（`zip` 在有条目被拒时会错位） |
| S3 | `prior_version_ref` 指向该哈希的 head 时跳过去重，inquiry loop 可以升版（ADR-0008 的 revise 入口）；`loop_for_admission` 返回 head 而不是首版 |
| S4 | 模板撤销三条路（见 §4） |
| nits | `authority.coverage_manifest()` 公开读法取代 `_one`；失败 hold 从**看见失败**的时刻起算而不是从启动起算；`plan_admissions(limit=...)` 只为本轮真会准入的条目扣池 |

**仍然欠着的**：按池归集的**结算**（C2）。loop 的模型提议走 `llm_planner_execute`，那笔钱记在
mission 级绑定上，不是 `adhoc` 池；池今天是准入闸门，不是结算口径。但「卡住的 loop 每个
tick 都要计费」这个具体故障已经关掉了：pending 的 loop 不再被问付费问题，而且它根本不会再
卡住。

## 8b. W2 报告第九节的四条 finding（v1.3 修）

| # | 位置 | 改法 |
| --- | --- | --- |
| 1 | `bounded_planner_driver` 探针执行 | 裸 `except Exception` 收窄成 `except BoundedProbeExecutionError`；传输层异常改为**暂挂并在下一 tick 续跑** |
| 2 | `agenda_control.serve` | 在唯一的生产构造点注入 grant resolver；resolver 从 `config.cockpit.core_db` **只读**打开 Core |
| 3 | `research_task.ADHOC_PROBE_TEMPLATES` | 两个跑不了的模板标 `retired` + 理由；cockpit 投影只宣传可绑集合 |
| 4 | `plan_admissions` 的拒绝理由 | 行业级 inquiry 改报 `industry_inquiry_has_no_company_probe` |

**1 的关键不是收窄，是「暂挂」得是真的暂挂。** AlphaEngine 那条分支是一次 **writer RPC**，
writer 忙 / 重启会抛普通异常；原来它和执行器的拒绝一样被写成 failed envelope，coverage item
从此永久 `source_unavailable`。但只是「不写终态就 continue」会退回 B2 修掉的那个坑：
round 已经准入、没有 outcome，loop 永远 pending，而 `run_once` 只为**本 tick 新准入的**
proposal 跑探针，没人会回来收拾它。所以这次把执行那段抽成 `_advance_round(loop, round)`，
并让 `run_once` 在 `propose_next` 返回 `pending_round` 时**拿回那个 round 继续跑**
（`propose_next` 对 pending 的 loop 是只读的，不写任何东西；doctrine 分支 materialize 报
「round is pending」时也落到同一条路上）。`_advance_round` 还会先看 work order 是否已有
formal result——有就不重跑，只补记 outcome，顺带修掉「完成了但记 outcome 前崩了」那一格。
执行器自己的拒绝（scope / operation / locator 不对）仍然是终态：那是对这枚探针的判断，
重试不会变。两条都有测试：拒绝 → 一轮 `source_unavailable`；瞬时故障 → 本 tick
`probe_transport_unavailable:RuntimeError` 且 `outcomes == []`，下一 tick `resumed: true`
且同一个 `round_ref` 拿到 `observed`。

**2 的两半。** `AgendaControlPlane` 一直接 `research_task_grant`，但**没有任何调用点传它**，
所以 P14e 把写死的 `False` 换成了恒为 `False` 的表达式——注释里写着「cockpit 进程没有 Core
句柄，所以 resolver 是注入的」，注入没做完。现在 `serve()` 唯一的生产构造点传入
`_research_task_grant(config)`：cockpit 段没配就仍是 `None`（答案还是「否」，但理由变成
「不知道 Core 在哪」而不是「线没接完」）；配了就用 `research_task.cockpit_grant_resolver`
从 `core_db` **只读**读答案。为什么不能复用 `read_grant`：那条路要构造两个 authority，
两个都会在构造时跑自己的 schema 脚本，对只读连接是不可能的。所以另写了
`readonly_grant`，直接查两张表并用行上的 `content_hash` 校验记录；读不到、哈希对不上、
表不存在，一律**不授权并说明原因**，绝不默认放行。每次调用现读，因为授权的两个 owner 动作
是在 cockpit 运行期间发生的。

**4 为什么只改一句话。** planner schema 明确允许 `company_ref: null`；到了绑定这一步，
目录里唯一能绑的探针按 CIK 取数，而行业不是公司。原来报 `no_bindable_template`，读起来像
「缺模板」，实际是「模板在，但它按公司取数」。真要支持得有一个不按 CIK 取数的行业探针，
那是独立的一块活（行业是独立主体、证据不属于任何一家公司——见 W2 报告第一节），不在本切片；
这里先把理由说清楚。

## 9. 池的日期与那条「顺序相关」的测试（2026-09-10 修）

`test_an_exhausted_pool_is_a_skip_...` 在全量里一度失败（`'launched' != 'skipped:pool_exhausted'`）。
排查结论与「模块顺序 / 全局状态泄漏」无关：

- 全仓 grep 过 `DEFAULT_PLANNER_MAX_COST_USD`、`POOL_SHARE`、`DEFAULT_SHARES`、p9a fixture
  清单，**没有任何测试改写它们**；`load_mission_manifest()` 每次重读文件，不共享 dict。
  唯一的 `patch.object` 是 P14e 自己对 `ADHOC_PROBE_TEMPLATES` 的，带上下文管理器自动还原。
- 真正的原因是**时钟**：测试写死 `DAY = "2026-09-09"`，而 loop 的 `created_at` 用的是
  UTC。本地 2026-09-09 20:00 EDT 就是 UTC 2026-09-10 00:00，之后新建的 loop 全落在
  10 号，`day_reserved_micros(day="2026-09-09")` 恒为 0，于是循环把 10 条全准入了也说
  「池还满着」，lane 自然 `launched`。当天 20:00 之前跑（单跑或全量）都过，之后都不过——
  与合并顺序同时发生，容易看成顺序问题。

改法：`DAY` 改成 `datetime.now(timezone.utc).date().isoformat()`，lane 测试的时钟设成
那一天的正午；耗尽用例先自证算术（$20 日额 → $5 池 ÷ 每条 $1.00 = 正好 5 条，余数 0），
准入 5 条后**先断言 `pool_state(day=DAY)["remaining_micros"] == 0`** 再问 lane，
这样万一日期再对不上，失败会指着日期说话，而不是丢出一句没头没脑的 `launched`。
另加一条测试钉「lane 的 cap 就是 C2 发布的那个」。

## 10. 开放问题

1. **`answer_routing._route_budget` 的硬禁用没有解除。** 那里写着
   「ad-hoc research must remain disabled in S5 v0.2」，且
   `contracts/answer-sufficiency-policy-version.schema.json` 把 `adhoc_research_route.enabled`
   钉成 `const false`、`answer-route-decision` 把 `adhoc_research_route_available` 钉成
   `const false`。本切片**故意不动**：那是「cockpit 问答能否就地开一条专项研究」这条路由，
   与「planner inquiry 变成研究任务」是两件事，动它要改两份 contract 与 policy schema 版本。
   建议随 P15（对话层）一起裁决：要么把 policy schema 升到 0.3 并允许 enabled，要么明确
   ad-hoc research 永远只从 planner 侧进入。
2. ~~**C2 的预算池应该推广什么。**~~ **已由 C2 落地**：`LaneSpec.budget_pool` /
   `pool_share`、`LANE_POOLS`、`skipped:pool_exhausted` 作为常量、以及最关键的那块——
   admission 带池进日账本、settle 从 admission 行继承池，所以**结算**也按池归集了。
   P14e 这边只做了最小对接：cap 读 C2，`POOL_SHARE` 读 C2，lane 归池交给 `LANE_POOLS`。
   还没做的是让 `pool_state` 把 C2 已结算的 adhoc 花费也减掉（需要把日账本的连接递到
   lane / CLI 里）；今天两边量的是预留与结算两件事，各自都对，合并读数留给集成。
3. **另外两个模板的执行器还没有，模板已按 finding 3 撤下。** 要让 web-search /
   AlphaEngine `search_library` 可用，需在 `bounded_probe_executor` 按 `metadata.operation`
   分派，或在 writer 侧新增两个 `bounded_*_probe` 操作，然后把目录里的 `status` 改回
   `active` 并给 `_parameters_for` 补分支。三件事要一起做，少一件目录就又在宣传空能力。
5. **行业级探针**（finding 4 的下半截）：需要一个不按 CIK 取数的探针，行业级 inquiry 才有
   下游。今天它们被诚实地拒绝，理由是 `industry_inquiry_has_no_company_probe`。
6. **DXC 的 CIK 补零**：`company:sec-cik:001688568` → `CIK001688568`（九位，SEC 要十位）。
   coordinator 说集成时自己修（`matched.group(1).zfill(10)`），本切片按嘱未动。
4. **一条任务的模型开销记在哪。** C2 的 `PURPOSE_POOLS` 已经把 `research_task` /
   `adhoc_research` 指到 `adhoc` 池，`llm_planner_execute` 也按池显式携带；P14e 这边的
   `pool_state` 仍只报预留。把两个数字合成一行给 cockpit 看，是集成时的小活。
7. ~~**cockpit 接线。**~~ **已按 finding 2 接上**：`serve()` 注入
   `cockpit_grant_resolver(config.cockpit.core_db)`，不需要新的 writer 操作。
   仍然要注意：cockpit 段没配 `core_db` 的部署，开关还是「否」。
