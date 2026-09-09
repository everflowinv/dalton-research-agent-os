# P14e 专项研究派发：inquiry 变成有预算的 BoundedPlannerLoop

日期：2026-09-09
分支：`p14e-adhoc-research`（基线 main `bd7021f`）
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

- `bounded_planner_loop.py`：新增 `INQUIRY_ADMISSION_SOURCE`、`_validate_admission`、
  `create_loop(admission=...)`、`loop_for_admission()`、`admitted_loops()`。
  `admission` 缺省为 `None` 时行为逐字节不变——identity 里不加字段，所以**历史 loop 的
  version_id 不变**；只有 inquiry loop 允许 `automation:` actor，人工 loop 仍强制 `human:`。
- `agenda_control.py`：`adhoc_research_enabled` 从字面量 `False` 变成 `self.adhoc_research_enabled()`。
- `lane_registry.py`：`LANE_MODULES` 加一行。
- `tests/test_lane_registry.py`：四处字面量集合加本 lane（那些集合是「掉一条 lane 要看得见」的护栏）。

`answer_routing.py` **没有改**。理由见 §8 开放问题①。

## 3. 预算池 `adhoc`

- **口径**：`cap = 0.25 × mission.budget.max_daily_cost_usd`。live 是 $100 → **$25/天**。
- **预留而非事后统计**：一条任务在准入时就按 `max_rounds × $0.50`（`bounded_planner_driver`
  的 `planner_max_cost_usd` 缺省值）预留。只统计已结算的池会在第一笔账单出来之前把一天的
  任务全放进来——这正是 `ThesisImpactBudgetStore.admit` 用 `reserved_micros` 预留、
  `settle` 再改成实际值的原因，此处沿用同一读法。
- **账本在哪**：`day_reserved_micros()` 直接从 loop 表推导（`admission.source='inquiry'` 的
  loop，按 `created_at` 的日期 + 自己的 `budget.max_rounds`）。**没有第二本账**，所以池和
  准入永远不可能各说各话。
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
2. **发布 ProbeTemplate**：按 `deploy/phase8/p14e-adhoc-probe-templates-v1.json`，用
   `publish_probe_template` 逐个发布，`actor_ref` 必须是 `human:`（本切片从不代签，测试用
   fixture 自己发）。缺它 → `no_executable_adhoc_template_published`。
3. **发一版 mandate**，`scope_refs` 覆盖 universe 五家。live 第 7 版只有
   `industry:us-it-services` 与 ACN，而最新计划的三条 inquiry 全是 EPAM / IBM / CTSH，
   现在一条都进不来（§7）。缺它 → 每条 `out_of_mandate_scope`。

另外要装 lane：在 state 目录放 `research-task-lane.json`（`{"max_admissions_per_tick": 1}`），
`argv_fragment` 才会给 writer 加 `--research-task-lane`。没有这个文件，lane 整个不存在，
LaunchAgent 的 argv 一字不变（`test_service` 的断言因此不受影响）。

**三个模板里今天只有一个能跑。** `bounded_probe_executor` 只执行 `get_company_facts`，
`bounded_alphaengine_probe` 只执行 `alphaengine_get_document`；driver 的探针执行**不在
try 里**，绑一个没人能执行的模板会把整个 controller tick 掀掉。所以
`bindable_templates()` 只放行 operation 在 `executable_probe_operations()` 里的模板：
SEC filings index（`get_company_facts`）今天可用，web-search 与 AlphaEngine `search_library`
是「已入目录、等执行器」。这是本切片最重要的一条安全边界。

## 5. 三个 ProbeTemplate

| template_ref | operation | permission_scope | allowed_hosts | cost（units/attempts/seconds） | 今天可绑 |
| --- | --- | --- | --- | --- | --- |
| `probe-template:adhoc-sec-filings-index:v1` | `get_company_facts` | `public_sec_read` | `data.sec.gov` | 1 / 2 / 120 | ✅ |
| `probe-template:adhoc-alphaengine-search-library:v1` | `alphaengine_search_library` | `alphaengine_read` | `127.0.0.1` | 2 / 1 / 120 | ❌ 等执行器 |
| `probe-template:adhoc-web-search:v1` | `public_web_search` | `public_web_read` | `*` | 1 / 2 / 60 | ❌ 等执行器 |

`allowed_hosts` 与 `cost_estimate_usd` 只在清单里（Core 的模板记录是闭合形状，host 白名单由
执行探针的 transport 强制），清单与 `research_task.ADHOC_PROBE_TEMPLATES` 由测试钉死不许漂。

## 6. 测试

```
Ran 2113 tests in 254.477s

OK (skipped=1)
```

（基线 2,080 + 本切片 33。命令：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`）

覆盖到的每条要求：三条 inquiry 的计划里一条准入、一条同哈希被拒（`already_admitted`）、
一条出宇宙被拒（`out_of_universe`）；宇宙内但 mandate 外单独一条（`out_of_mandate_scope`）；
改了问题文本就是新任务；跨计划再出现同一条仍被拒；池耗尽（`pool_exhausted` /
`skipped:pool_exhausted`）；昨天的任务不花今天的池；终态闸门（`human_deprioritized` 成功、
`coverage_complete_unobservable_candidate` 被闸门驳回）；模板子集（只有可执行的能绑、
非 ad-hoc 目录的模板不会被绑、行业级 inquiry 无可绑模板）；开关语义（两个 owner 动作、
cockpit flag 在解析器缺席/抛错时都是否）；人工 loop 仍需 `human:`、且 identity 不变；
lane 的 launched / busy / held / 一小时后解除 / 失败后 hold / not_granted / 结算；
LaneSpec 注册与「没有配置就没有这条 lane」。

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

## 8. 开放问题

1. **`answer_routing._route_budget` 的硬禁用没有解除。** 那里写着
   「ad-hoc research must remain disabled in S5 v0.2」，且
   `contracts/answer-sufficiency-policy-version.schema.json` 把 `adhoc_research_route.enabled`
   钉成 `const false`、`answer-route-decision` 把 `adhoc_research_route_available` 钉成
   `const false`。本切片**故意不动**：那是「cockpit 问答能否就地开一条专项研究」这条路由，
   与「planner inquiry 变成研究任务」是两件事，动它要改两份 contract 与 policy schema 版本。
   建议随 P15（对话层）一起裁决：要么把 policy schema 升到 0.3 并允许 enabled，要么明确
   ad-hoc research 永远只从 planner 侧进入。
2. **C2 的预算池应该推广什么。** 本切片证明了三件事可以先做：(a) 池名进
   `LaneSpec(budget_pool=..., pool_share=...)`；(b) 池的日账**从各 lane 自己的权威推导**，
   不建第二本账；(c) 超池的词统一为 `skipped:pool_exhausted`。真正缺的一块是
   `ThesisImpactBudgetStore.admit` 目前只按 `mission_ref` 分组，没有 pool 维度——C2 应该给
   `mission_binding` 加一个 `pool` 字段（或允许 pool 限定的 scope key），这样**实际结算**的
   钱也能按池归集，而不只是准入时的预留。未用份额只允许 `coverage` 借用那条，需要在同一处实现。
3. **另外两个模板的执行器。** web-search 与 AlphaEngine `search_library` 已入目录但不可绑。
   要让它们可用，`bounded_probe_executor` 需要按 `metadata.operation` 分派（并把 driver 里
   那段执行放进 try），或由 writer 侧新增两个 `bounded_*_probe` 操作。这需要碰
   `bounded_planner_driver.py` / `writer_server.py`，本切片被禁止触碰，故留给集成。
4. **一条任务的模型开销现在计在 planner 账上。** loop 的模型提议走
   `llm_planner_execute`（writer 操作），它用的是 mission 级绑定，不是 `adhoc` 池。所以池
   目前是**准入时的预留闸门**，而不是结算口径；口径统一依赖开放问题②。
5. **cockpit 接线。** `AgendaControlPlane(research_task_grant=...)` 的解析器需要一个能读 Core
   的入口；`writer_server.py` 与 cockpit 两个文件本切片禁止触碰，建议集成时加一个只读操作
   （返回 `research_task.read_grant(store)` 的 `granted` 与 `reasons`）并注入。缺省仍是「否」。
