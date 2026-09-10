# W3 阶段阶梯跨版本折叠：阶段状态是任务的事实，版本只是出处 v1.0

日期：2026-09-09
分支：`w3-stage-ladder`（worktree `~/Projects/dalton-w3-stage-ladder-worktree`），基线 main `9813b44`，已两次 `git merge main`，末次到 `eb8e5fb`（P15d 合入之后）
作者：stage-ladder agent（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md) §5（append-only stage ledger）、[ADR-0008](../adr/0008-research-outputs-are-never-terminal.md) 与本片新增的 **2026-09-09 addendum**、[P14a 每日跟踪](p14a-daily-tracking-v1.0-2026-09-09.md) §5 B2、[P14b/P14d 修订回路](p14b-p14d-revision-loop-v1.0-2026-09-09.md) §4

全量测试：

```
Ran 4405 tests in 734.294s
OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`；逐条测试行见 §8。）

---

## 0. 一句话

`coverage_mission_stage_record` 绑的是**写它的那一版任务**，而所有读者都按那一版查询，于是每发一版 mission，公司的阶梯就清空一次——live 两天内 v7 → v13，35 条 `initial_screen entered` 是同样五家公司被重播了七遍，四个真正过闸的门只活在 v13 上；本片把阶梯的语义改成「**阶段状态是 `(mission_ref, company_ref)` 的事实，按时间跨所有版本折叠，版本是出处不是范围**」，写入端仍绑活跃版本。

## 1. 问题：同一个 bug，今天被撞了两次

| 撞点 | 症状 | 当时的绕法 |
|---|---|---|
| P14a 常驻跟踪（B2） | 一发新版本，四家过闸公司同一秒全部掉出每日跟踪，lane 还报告一切正常 | `screen_passed_companies` 自己按 `mission_ref` 跨版本查 `gate_passed` |
| P12d 深度认知门 | 滚版之后 `decide_deep_insight_gate` 直接死：stage 记录必须绑活跃版本，而新版本上 `deep_insight_gate cannot be entered before initial_screen gate_passed` | 无（本片修） |
| P14d 重开 / P14f 财报 / cockpit / dossier / Initial Screen 选择规则 | 同一个形状，尚未被撞到 | 无 |

三处以上的读者各自打补丁，说明这不是读者的 bug，是阶梯的语义没定义。

## 2. 规则（ADR-0008 addendum，逐字见 [ADR](../adr/0008-research-outputs-are-never-terminal.md)）

**阶段状态是 `(mission_ref, company_ref)` 的事实，一路带过版本，直到被取代。** 记录绑的 mission 版本是**出处**——它说这个状态是在哪一版、什么时候到达的，所以按阶段逐一报告——而不是范围。滚版不是公司阶梯上的事件：owner 改一次授权，Accenture 不会因此没被筛选过。

折叠有四条排序规则，只写在 `coverage_mission.fold_stage_status` 一处：

1. 记录按 `created_at` **跨该 `mission_ref` 的所有版本**按时间折叠。
2. **最后一个裁决胜出**。后来的 `gate_failed` 取代先前的 `gate_passed`（这就是重开的读法）；后来的 `gate_passed` 取代先前的 `gate_failed`（这是单版本内一直就有的重试）。
3. **`entered` 永远不取代裁决**。新版本重播 `entered` 不能把已过的闸倒回去。
4. 一条记录都没有 = 从未到达（`None`）。

两个读者对应两个**不同**的问题，不能混：

- `current_stage_state(mission_ref, company_ref)` —— 「现在站在哪」，认取代。
- `companies_at_or_past(stage, mission_ref)` —— 「有没有发生过」，**刻意单调**。P14a 的常驻性建在它上面：公司离开跟踪的唯一方式是被人移出 universe，不是某个门被重开。

## 3. 改了什么

### 3.1 `coverage_mission.py`（纯增量）

| 新增 | 说明 |
|---|---|
| `fold_stage_status(statuses)` | 四条排序规则唯一的实现，模块级纯函数 |
| `STAGE_DECISIONS` | `{gate_passed, gate_failed}`——关闸的两个状态 |
| `_mission_ref_of(cur, version_ref)` | 版本 → 任务；查不到就退回该版本自身（不静默折叠成空） |
| `_folded_rows(cur, mission_ref, company_ref=None)` | 该任务所有版本的 stage 记录，按 `created_at, record_id`。**版本号刻意不参与排序**：先发生的就是先发生的 |
| `_folded_statuses(cur, mission_ref, company_ref)` | `{stage: [status...]}` |
| `stage_state_by_company(mission_ref)` | `{company: {stage: [status...]}}`——`evaluate_mission` / cockpit 早就在说的形状，只是填满了 |
| `current_stage_state(mission_ref, company_ref)` | 交付物 ①。每个阶段带 `status` / `at` / `record_ref` / `mission_version_ref` / `mission_version_number` / 完整 `history`，外加 `current_stage`、`current_status`、`completed_stages`、`entered_stages`、`next_stage`、`record_count` |
| `companies_at_or_past(stage_ref, mission_ref)` | 交付物 ①。「进过该阶段，或过了它前一阶段的闸」 |

改动：

- `record_stage` 的阶梯校验改读折叠状态（`_folded_statuses`），**「必须绑活跃版本」的检查原样保留**，写下的记录仍绑活跃版本作为出处。`"gate_passed" in state[stage]` 改成 `fold_stage_status(state[stage]) == "gate_passed"`——这一步顺带让「`gate_failed` 之后重新过闸」在语义上成立（重开落地时用得上）。
- `mission_progress` 改调 `current_stage_state`。原来 `current_status = history[current][-1]` 再被 `"gate_passed" in history[current]` 覆盖，重开之后会读反。
- `_stage_state`（按版本）已无调用者，删除。`stage_records(version_ref)` **保持按版本**：writer_server 的 `coverage_mission_stage_records` op 问的就是「这一版有什么」，那是个正确的问题。

### 3.2 `mission_stage.py`

- `MissionStageDriver._stage_state` 改读 `stage_state_by_company(mission_ref)`。
- `evaluate_mission` 的 `stage_status` 改用 `fold_stage_status`（原来是「`gate_passed` 出现过就算过」，重开之后读反）。
- **`run_once` 不再重播 `entered`（交付物 ③ 的裁决）**：「还没进过」现在指**任何版本**都没进过。选「不写」而不是「写一条只作出处的重复」，理由三条：第二条 `entered` 没说第一条没说过的任何事；它按规则 3 动不了折叠状态；它让 append-only 账本每发一版长五行——live 41 条 stage 记录里 30 条是这么来的。折叠之后 `record_stage` 本来就会拒绝这条重复，跳过它是让这一 tick 安静，而不是报告五条它自己造出来的拒绝。

### 3.3 切到折叠读者的调用方（交付物 ④）

| 文件 | 函数 | 改动 |
|---|---|---|
| `tracking_cadence.py` | `screen_passed_companies` | P14a 自己的跨版本 SQL → `companies_at_or_past("deep_insight_gate", …)`。单调性由该读者保证 |
| `deliverable_reopen.py` | `passed_version` | 原来 `WHERE status='gate_passed' ORDER BY created_at DESC` 取第一条；现在读整条阶梯并折叠，被重开取代的过闸不再算过闸 |
| `mission_reopen_lane.py` | `passed_companies`、CLI 的公司列表 | 同上：`DISTINCT … status='gate_passed'` 看不见取代，会把已经开着的门每周再提议重开一次 |
| `initial_screen_cli.py` | run 里的 `stage_state` | `stage_records(mission["id"])` → `stage_state_by_company(mission_ref)`。**这是最贵的一处**：选择规则跳过「已过闸」的公司，按版本读的话，v14 一发，四份 Initial Screen 会被全部重写一遍 |
| `company_dossier_cli.py` | `screened_companies` | 同上；按版本读的话，发一版会让整个任务的 dossier 停到有人重新筛选为止 |
| `cockpit_plane.py` | `_stage_rows`、`document()` 的 `stage_history` | 跨版本查询；`stage_history` 每条另带 `mission_version_ref` 作为出处 |

### 3.4 没改、但列在这里（交付物 ④ 后半）

| 位置 | 为什么留着 |
|---|---|
| `research_cycle_reflection.open_human_checkpoints` | 已经是全局查询、已经按 (company, stage) 取最新一条。它的「最新记录胜出」与折叠的「最后裁决胜出」只在「`gate_passed` 后面跟一条 `entered`」时分歧，而那正是 `run_once` 现在不再产生的东西。留着，但它是下一个该换成 `fold_stage_status` 的地方 |
| `writer_server._op_coverage_mission_stage_records` | 按版本是这个 op 的正确语义（「这一版有什么」）。若 cockpit 之后需要折叠视图，那是一个**新** op，不是改这个 |
| `initial_screen_launcher.py:207` | `COUNT(*)`，一个体检数字，不问阶段状态 |
| `research_plan_executor.re_read_stage_records` | 同名不同物：那是研究计划的阶段记录，与 mission 阶梯无关 |
| `reflection_fixtures.py` | 测试夹具 |

## 4. 明确留下的口子

**被重开之后的第二次过闸仍然写不进去。** `record_stage` 对「已过闸」是终态的（折叠前在单版本内就是），而 ADR-0008 说重开一个已过闸的门是 `gate_reopen` 人类检查点，今天没有任何写入者。所以：

- 折叠**读**得对（规则 2），两个 live 读者已经依赖它（§3.3）；
- 但没有代码路径能**写**出那条 `gate_failed`。测试用一条直接插进表里的行来钉读法，并在测试的 docstring 里写明为什么是夹具。
- 变化点：折叠前，滚版之后 `initial_screen_cli` 的第二次 `gate_passed` 会写进新版本（因为新版本账本是空的）；折叠后它被拒，和同版本内一样。这条路径本来就 `except CoverageMissionError` 记 `not_recorded:…`，不会崩。**下一步**是给重开一个阶梯上的表示（写一条带 `gate_reopen` 批准的 `gate_failed`），那是 P14d 的续集，不是本片。

**时间戳并列。** 折叠按 `created_at, record_id`，同一微秒内两条记录由内容哈希决定先后——任意但确定。同一 (company, stage) 在一微秒内写两条需要两个事务在 1µs 内完成，做不到；live 41 条时间戳全不相同。

## 5. Live 副本核对（交付物 ⑤）

只读副本 `/private/tmp/dalton-ro/core.sqlite` → `/tmp/w3-core.sqlite`。任务 `coverage-mission:us-it-services`，13 版，活跃 v13，41 条 stage 记录，全部在 `initial_screen`。

### 5.1 逐公司：折叠 vs 每一版说什么

| 公司 | 折叠 | 出处 | v7 | v8 | v9 | v10 | v11 | v12 | v13 |
|---|---|---|---|---|---|---|---|---|---|
| ACN `…0001467373` | **gate_passed** | v13 | entered | entered | gate_failed | entered | entered | entered | gate_passed |
| CTSH `…0001058290` | **gate_failed** | v9 | entered | entered | gate_failed | entered | entered | entered | entered |
| EPAM `…0001352010` | **gate_passed** | v13 | entered | entered | entered | entered | entered | entered | gate_passed |
| IBM `…0000051143` | **gate_passed** | v13 | entered | entered | entered | entered | entered | entered | gate_passed |
| DXC `…001688568` | **gate_passed** | v13 | entered | entered | entered | entered | entered | entered | gate_passed |

`initial_screen entered` 共 35 条 = 5 家公司 × 7 个版本。

### 5.2 只读活跃版本 vs 折叠 vs 「如果今天发 v14」

| 公司 | 只读 v13 | 折叠 | 只读 v14（空） |
|---|---|---|---|
| ACN | gate_passed | gate_passed | **None** |
| CTSH | entered | **gate_failed** | **None** |
| EPAM | gate_passed | gate_passed | **None** |
| IBM | gate_passed | gate_passed | **None** |
| DXC | gate_passed | gate_passed | **None** |

两点值得看：

1. **今天（v13）唯一变的是 CTSH**：只读 v13 说「进行中」，折叠说「未通过，正在补」——它 v9 没过闸，之后六版只被重播了 `entered`。折叠说的是实话。选择规则不受影响（`gate_failed ≠ gate_passed`，本来就不跳过）；cockpit 的标签从「进行中」变成「未通过，正在补」。
2. **v14 一发，只读版本的一切归零**——这就是本片修的东西。

### 5.3 折叠读者跑在 live 副本上

```
mission_ref: coverage-mission:us-it-services   active v13
  ACN    folded=gate_passed  next=deep_insight_gate  prov=v13  n=9
  CTSH   folded=gate_failed  next=initial_screen     prov=v9   n=8
  EPAM   folded=gate_passed  next=deep_insight_gate  prov=v13  n=8
  IBM    folded=gate_passed  next=deep_insight_gate  prov=v13  n=8
  DXC    folded=gate_passed  next=deep_insight_gate  prov=v13  n=8

companies_at_or_past(initial_screen)    : 5
companies_at_or_past(deep_insight_gate) : IBM, EPAM, ACN, DXC
screen_passed_companies (P14a)          : ACN, EPAM, IBM, DXC
passed_companies (reopen lane)          : ACN, EPAM, IBM, DXC
mission_progress current_status         : ACN=gate_passed CTSH=gate_failed EPAM=gate_passed IBM=gate_passed DXC=gate_passed
passed_version  ACN  mission-deliverable-version:06e126b45f078b98e1a92d241c0f244f
passed_version  CTSH None
passed_version  EPAM mission-deliverable-version:45e773c2d992a2a58f695800e3dcd81a
passed_version  IBM  mission-deliverable-version:e0327e68e7f04e15d7f798e790c8bef4
passed_version  DXC  mission-deliverable-version:cc6d2b1855c4e3fc74e0ef0a7c9f1f5a
```

P12d 现在可以在 v13（或 v14、v15）上对这四家里的任意一家跑 `deep_insight_gate`。

### 5.4 没有任何内容哈希移动

`coverage_mission_schema.sql` **一个字节都没动**（`git diff HEAD~1 -- src/dalton_core/coverage_mission_schema.sql` 为空）——本片是读法，不是新对象。在 live 副本上重算：

```
schema diff above (empty = untouched)
stage records re-hashed: 41; mismatches: 0
all coverage_mission content-hashed rows re-hashed: 222; moved: 0
```

## 6. 顺手修的、不是本片的东西

基线 main `9813b44` 上 `tests/test_rehearse_deploy.py::MigrationCoverageTests::test_every_shipped_schema_has_a_named_owner` 就是红的：w3-revision-loop 合入时带进 `thesis_revision_schema.sql` 与 `deliverable_reopen_schema.sql` 两个 schema，没在 `scripts/rehearse_deploy.py` 的 `CORE_MIGRATIONS` 里登记，rehearsal 会在从未跑过这两条迁移的情况下报告部署干净。已在 clean checkout `9813b44` 上单独复现确认与本片无关。本片先自行登记以让套件转绿；`git merge main` 之后 main（`56cf508` 起）已有同样两条，**取 main 那一侧**，本片的重复删除。

## 7. 交付物对照

| # | 交付物 | 在哪 |
|---|---|---|
| ① | `current_stage_state` / `companies_at_or_past`，排序规则成文 | `coverage_mission.py`，`fold_stage_status` 的 docstring |
| ② | `record_stage` 用折叠校验、仍绑活跃版本；滚版后的 P12d 端到端 | `test_a_gate_passed_two_versions_ago_still_opens_the_next_stage` |
| ③ | `run_once` 不再重播 `entered`（已裁决 + 已测） | §3.2、`test_a_version_roll_re_seeds_nothing_and_the_ladder_carries_forward` |
| ④ | 已知读者切换 + 其余列表 | §3.3、§3.4 |
| ⑤ | Live 副本逐公司对照 + 零哈希移动 | §5 |
| ⑥ | ADR-0008 addendum | `docs/adr/0008-research-outputs-are-never-terminal.md` 末尾 |
| ⑦ | 本报告 | 本文件 |

## 8. 测试

新增/改动的测试，逐字：

```
test_a_later_gate_failed_supersedes_an_earlier_gate_passed (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_a_later_gate_failed_supersedes_an_earlier_gate_passed) ... ok
test_a_company_with_no_records_reads_as_never_started (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_a_company_with_no_records_reads_as_never_started) ... ok
test_a_gate_passed_two_versions_ago_still_opens_the_next_stage (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_a_gate_passed_two_versions_ago_still_opens_the_next_stage) ... ok
test_a_new_version_resets_nothing_the_company_already_did (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_a_new_version_resets_nothing_the_company_already_did) ... ok
test_companies_at_or_past_crosses_versions_and_stays_monotone (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_companies_at_or_past_crosses_versions_and_stays_monotone) ... ok
test_the_fold_rules_are_last_decision_wins_and_entered_never_supersedes (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_the_fold_rules_are_last_decision_wins_and_entered_never_supersedes) ... ok
test_the_folded_map_carries_every_version_in_time_order (tests.test_coverage_mission.StageLadderAcrossVersionsTests.test_the_folded_map_carries_every_version_in_time_order) ... ok
test_a_version_roll_re_seeds_nothing_and_the_ladder_carries_forward (tests.test_mission_stage.StageEntryTests.test_a_version_roll_re_seeds_nothing_and_the_ladder_carries_forward) ... ok
test_a_gate_that_passed_under_an_earlier_mission_version_is_still_passed (tests.test_deliverable_reopen.AssessmentTests.test_a_gate_that_passed_under_an_earlier_mission_version_is_still_passed) ... ok
test_a_pass_a_reopen_already_superseded_is_not_a_pass (tests.test_deliverable_reopen.AssessmentTests.test_a_pass_a_reopen_already_superseded_is_not_a_pass) ... ok
test_a_screen_that_passed_under_an_earlier_mission_version_still_counts (tests.test_dossier_lane.GrantTests.test_a_screen_that_passed_under_an_earlier_mission_version_still_counts) ... ok
```

已有的跨版本测试仍然绿，且现在读的是同一个折叠读者：`tests.test_tracking_cadence.…test_residency_survives_the_next_mission_version`、`…test_a_later_stage_does_not_end_residency`、`tests.test_coverage_mission.CoverageMissionTests.test_stage_records_bind_only_the_active_mission_version`。

全量（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`，`git merge main` 到 `eb8e5fb` 之后）：

```
Ran 4405 tests in 734.294s
OK (skipped=1)
```
