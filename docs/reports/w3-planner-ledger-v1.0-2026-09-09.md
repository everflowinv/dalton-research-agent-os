# C2b：Tier-1 规划调用进日账本 v1.0

日期：2026-09-09（合入日 2026-09-10）
分支：`w3-planner-ledger`（worktree `~/Projects/dalton-w3-planner-ledger-worktree`），基线 main `eaf48f0`，中途合入 main `eb8e5fb`
依据：[C2 报告](c2-budget-pools-and-tick-ledger-v1.0-2026-09-09.md) 第 1 节「池是准入闸门」与其收尾清单里点名的 planner 缺口、[P14e 报告](p14e-research-tasks-v1.0-2026-09-09.md) 开放问题 2

---

## 0. 一句话

C2 把一天的钱分成四个具名的池，但**唯一一类没有进日账本的付费调用，恰好是决定「接下来做什么」的那一类**——`BoundedPlannerLoop` 的 Tier-1 规划调用：`writer_server` 的 `planner_model_config` 里没有 `budget_db`，`LLMResearchPlannerModelWorker.run_once` 从不准入。于是 owner 那 25% 的 `adhoc` 池，对它被设计出来要管的那些调用，是**不可执行的**。C2b 把这条路接上：同一本日账本、同一套池、同一个 `admit`/`settle` 代码，池空了就返回 hold 而不是抛异常，也不吃掉 loop 的一轮。

## 1. 缺口的形状

P14e 的 `ResearchTask` 是这样花钱的：一个 inquiry 被准入成一个 bounded loop，loop 每一轮向 `llm_planner_execute` 要一次模型判断，每次上限 `planner_max_cost_usd`（$0.50）。P14e 的 `pool_state` 按「今天已准入的任务 × 每任务轮数 × 单轮价」预留，25% 的边界靠**预留**守住。

问题是预留之外没有第二端：

- 那些调用真的发生时，`writer_server._op_llm_planner_execute` 直接开 `ModelRouter` + broker，**中间没有 `ThesisImpactBudgetStore`**。所以 mission 的日上限、日调用数、四个池，都看不见它们。
- 反过来 `pool_state` 也就永远只有 `reserved`，没有 `settled` 可减——不是没写，是**账本里根本没有这一行**。

也就是说：25% 是一条画在纸上的线，越过它的那支笔不在纸上。

## 2. 现在绑住了什么

一次 Tier-1 规划调用的完整路径（`*` 是 C2b 新增）：

```
driver ──llm_planner_execute{context_pack_ref, …, pool*}──▶ writer
  writer: coordinator.prepare  → model_work_ready
  writer: loop = bounded_planner.loop(context.loop_version_ref)
  writer: pool = pool_for_loop(loop)              *  ← 权威在 Core
  writer: 驱动声明的 pool 与之不符 → 拒绝           *
  writer: binding = _planner_budget_binding(pool) *  ← mission 指针 + pool_caps
  worker: _pool_gate(work)  →  池空 → 返回 rejected  *  ← 在 claim 之前
  worker: scheduler.claim                            （拿租约、占一次 attempt）
  worker: router.select → route decision
  worker: admit_day_ledger(...)                    *  ← 在 broker 之前
  worker: adapter.execute → invocation, envelope
  worker: settle_day_ledger(实际服务那条链路的成本)  *
  worker: scheduler.complete
```

三个位置是有意的：

1. **`_pool_gate` 在 `claim` 之前。** 池空是「今天做完了」，不是「坏了」。如果拒绝发生在 claim 之后，它就已经占了一个租约、烧掉了这个 WorkOrder 的一次有界 attempt；而池会空一整天，接下来几个 tick 会把剩下的 attempt 逐个烧完，**一个预算决定就把 loop 永久废了**。在租约之前拒绝，loop 一分钱一次机会都不损失，午夜池满了继续跑。
2. **`admit` 在路由决策之后、broker 之前。** 准入行要记 `route_decision_ref`，而没被账本准入的调用一分钱都不能付。
3. **`settle` 用**实际服务的那条链路**的价格。** 与 cockpit 调用同一条规则：provider 报了实际成本就用实际成本，否则用**服务方**的价目表（不是第一条被问到的链路的，也不是预留额）。broker 抛异常（什么都没送出）或 envelope 不是 `succeeded`（broker 拒绝而非服务）→ 结算 0，整笔预留当天退回池里。

### 共用同一段准入代码，不是抄一份

`cockpit_model` 里加性地抽出四个公开函数，`CockpitModel.call` 的非链式分支改用它们，planner worker 也用它们：

| 函数 | 做什么 |
| --- | --- |
| `admit_day_ledger(budget, …)` | 一次预留。返回三种形状之一，**任何预算答案都不抛异常**：`{"status": "admitted", …}` / `{"status": "pool_exhausted", "rejection", "failure"}` / `{"status": "refused", "failure"}` |
| `settle_day_ledger(budget, admission, actual_micros=)` | 结算。**不传池**——`settle()` 在同一个事务里从准入行把池抄过来，这正是「池和账不可能对不上」的 schema 保证 |
| `call_cost_micros(invocation, route, profile, reserved)` | 「服务方的价格」这条规则的唯一出处 |
| `pool_refusal_message(rejection)` | 池空这件事对外说的那句话 |

两份「准入，然后读它说不的三种方式」一定会漂移，而漂移的方向就是**其中一份不再检查池**。所以是抽取，不是复制。

`CockpitModel._chained` 这条分支**没有动**：它有自己的多链路准入形状，改它不属于这一片。

## 3. 池由 loop 自己决定

第三种付费调用的形状。cockpit 调用按 **purpose** 准入，tick lane 按 **operation** 准入，Tier-1 loop 两者都不是——它是**一个 loop 的模型调用**，而它花哪个池的钱，是「这个 loop 为什么存在」的属性。

```python
LOOP_ADMISSION_POOLS = {"inquiry": "adhoc"}

def pool_for_loop(loop):  # loop["admission"]["source"]
    ...  # 没有 admission 块 = P14e 之前的 owner 自研 = coverage
```

- P14e 的 inquiry loop → **`adhoc`**，就是那 25% 存在的理由。
- 其他所有 loop（owner 直接建的、mandate 来的）→ **`coverage`**，mission 本来就要做的活。

**读的是 loop 记录，不是调用方给的参数。** 驱动可以在 `llm_planner_execute` 里**声明**一个 `pool`（它从 `bounded_planner_active_loops` 投影里读到的那个），但 Core 会用 loop 自己算一遍，不一致就拒绝——和驱动传 doctrine pack hash、由 writer 核验是同一个形状。一个能自己报池名的驱动，会报最便宜的那个，那 25% 就成了建议值。

投影里现在每个 loop 带 `"pool"`，所以驱动不必从 ref 前缀去猜。

## 4. 驱动侧：池空 = hold，不是 fallback

`bounded_planner_driver` 看到 `{"status": "rejected", "reason": "pool_exhausted"}`：

```python
skipped.append({"loop_version_ref": …, "reason": "pool_exhausted",
                "pool": …, "lane_status": "skipped:pool_exhausted"})
continue
```

`continue` 的位置在**确定性 planner 兜底之前**，这是关键。P14e 的兜底是给「模型调用失败」用的；池拒绝不是失败。掉进兜底会消耗掉池刚刚拒绝掉的那一轮，loop 到了明天少一轮、什么也没换到。writer 在租约之前就拒绝了，所以什么都没计费、什么都不用回滚。

窄口径：**只有** `rejected` + `pool_exhausted` 触发 hold，其他所有 planner 结果保持 P14e 的行为不变。

## 5. 配置与安装：为什么没有新 flag

`planner_model_config` 现在可以带 `budget_db` + `budget_policy_ref`。但**没有加 `--planner-budget-db` 参数**，`research_planner_setup.py` / `model_configurations.py` / `install.sh` 也**一行都没改**。

理由：writer 的 argv 由 `macos_launchagent.py` 拼（这一片不能动它），而 `research_planner_setup.install()` **本来就**把这两个键写进了 `<state_dir>/research-planner-model-config.json`（第 214-215 行，和 deliverable 配置同一套模式），`model_configurations` 在 owner 抬日上限时**本来就**会重指这个文件。再放一份到命令行，就是第二个要重指的地方——而被忘掉的那个一定是这一个。

所以 `writer_server.main()` 从 Core 旁边把它读出来并 merge 进 `planner_model_config`：

```python
def planner_budget_config(state_dir) -> dict[str, str]:
    # 文件不在 / 读不了 / 少键 / budget_db 是相对路径 → {}
```

四种「没有」都返回 `{}`，planner 保持今天的行为，**并且 op 结果里带 `budget: {"status": "unbudgeted"}`**。这一点是刻意的：一个悄悄不生效的预算，比一个明确缺席的预算更糟。「planner 上账本了吗」现在一条 op 结果就能回答，不必去翻安装历史。

**owner 需要做的一步**：重跑 `install.sh`（`DALTON_PLANNER_MODEL_PROFILE` 或 `DALTON_PLANNER_MODEL_TIER` 已设的情况下），让它重写 `research-planner-model-config.json`，然后重启 writer。在那之前一切照旧，只是 op 结果里写着 `unbudgeted`。

## 6. `pool_state` = cap − reserved − settled

`research_task.pool_state(..., budget_db=…)` 现在多返回 `settled_micros`，`remaining_micros = max(cap − reserved − settled, 0)`。`research_task_cli` 的三个读点都传上了 `state_dir / "thesis-impact-budget.sqlite"`——决定「今天还能准入几个 inquiry」的那条 lane，正是最需要按已花掉的钱来读上限的。

口径沿用 C2 自己的 `day_pool_spend`：**结算了的按结算额，还开着的按预留额**。

**诚实的话**：`reserved` 与 `settled` 会重叠——今天准入、并且已经调用过的任务，两边都算了它。两个数量的是不同的东西（预留是今天的任务还可能跑的每一轮，结算是已经花掉的钱），要不重叠地合并需要按 loop 对账。这里选择双算，因为可选的两种错误里，**只有「对着一个已经花掉的上限继续准入」会越过 owner 的边界**。超额时读作 0，不读作负数。

`budget_db` 不传 → 完全是 P14e 的读数（只看预留）。planner 还没上账本的安装，不会被告知它花了一笔谁也找不到的钱。

## 7. 两处发现，写下来而不是抹掉

1. **pre-lease 闸门的 attempt 号错了一位。** Scheduler 是向前编号的：`enqueue` 写的第一条事件就带着 attempt 1，requeue 写的那条带着「刚结束那次 +1」。所以**最新一条事件的号本身就是下一次 attempt 的号**，原来的 `max(...)+1` 让闸门按 N+1 预留、而真正的准入用 N，两边说的不是同一次尝试。测试抓到的，已修。
2. **日账本是 WAL，C2 的只读打开会拒绝没有 sidecar 的库**——也就是没有任何进程持有它的时候。部署里 writer 一直持有它、这条 lane 在旁边读，所以正常；但 writer 停着的时候，`settled` 这一项会掉出去，读数退回「只看预留」。那是 P14e 的数，不是一个错的数，`day_settled_micros` 的 docstring 里把这件事说出来了。

另外 `research_task_view` 在交接的代码里用了一个自己没有的 `budget_db`（有 mission 时必 NameError），已修。

## 8. 文件

| 文件 | 变化 |
| --- | --- |
| `budget_pools.py` | `LOOP_ADMISSION_POOLS`、`pool_for_loop` |
| `cockpit_model.py` | 加性抽出 `admit_day_ledger` / `settle_day_ledger` / `call_cost_micros` / `pool_refusal_message`，`call` 非链式分支改用 |
| `llm_research_planner_worker.py` | 可选 `budget` / `budget_policy_ref` / `mission_binding`（三个一起或都不给）、pre-lease 闸门、准入、结算、返回式池拒绝、`budget` 报告 |
| `writer_server.py` | `OPERATION_FIELDS["llm_planner_execute"]` 加可选 `pool`；`_op_llm_planner_execute` 推导权威池、核验声明、开账本、原样返回池拒绝；`_active_mission_version`、`_planner_budget_binding`、`planner_budget_config`；`main()` merge；active_loops 投影带 `pool` |
| `bounded_planner_driver.py` | 传 `pool`；`pool_exhausted` → hold |
| `research_task.py` | `day_settled_micros`；`pool_state`/`plan_admissions`/`research_task_view` 接 `budget_db` |
| `research_task_cli.py` | 三个读点接上日账本 |

`coverage_mission.py`、`macos_launchagent.py`、`cockpit_*`、`PROJECT_STATUS`、`tests/test_lane_registry`、`deploy/macos/install.sh`、`tests/test_service.py`：**未改**。

## 9. 测试

新增 19 个用例：worker 侧 6（准入/结算落到 `adhoc` 池且带 `pool_lane`、结算的是服务方 $0.001 而不是 $0.50 预留、池空在租约前返回且不掉 attempt、池满了同一个 WorkOrder 照跑、broker 没服务结算 0、`unbudgeted` 照说、半套账本构造即拒）、驱动侧 4（hold 不进兜底、池随调用走、旧投影不发 pool、非池失败仍走兜底）、writer 侧 5（投影带池、非四池之一被拒、驱动报了更便宜的池被指名拒绝、无预算配置绑定为 None、`pool_for_loop` 语义）、`pool_state` 4、`planner_budget_config` 5。

```
$ .venv/bin/python -m unittest tests.test_llm_research_planner_worker
Ran 10 tests in 0.421s
OK

$ .venv/bin/python -m unittest tests.test_bounded_planner_driver
Ran 34 tests in 9.702s
OK

$ .venv/bin/python -m unittest tests.test_research_task
Ran 41 tests in 5.352s
OK

$ .venv/bin/python -m unittest tests.test_research_planner_setup
Ran 23 tests in 0.236s
OK

$ .venv/bin/python -m unittest tests.test_budget_pools tests.test_cockpit_model_fallback tests.test_mission_research_task_lane
Ran 63 tests in 1.186s
OK

$ .venv/bin/python -m unittest discover -s tests -t .
Ran 4427 tests in 529.037s
OK (skipped=1)
```

全套在合入 main `eb8e5fb` 之后跑，`tests/test_rehearse_deploy.py` 那两个已知失败已由别处修好并合入，所以这次是**零失败**，不需要例外。（`PYTHONPATH=$PWD/src`，无模型调用。）
