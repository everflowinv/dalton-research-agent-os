# F14 Batch C cadence-lane failure budgets

Date: 2026-09-10  
Baseline: `4c28816`  
Branch: `f14-cadence`

## Result

The reflection, zero-base review, research-task, and crowd-source coordinators now classify real child or runner failures through `LaneFailureBudget`. Production dispatch supplies the scheduler state directory, so dependency parks, content-terminal decisions, and authorization holds survive coordinator restarts in the shared append-only ledger.

Each lane keeps its existing business key: week plus reflection input digest, zero-base mode plus batch, the research plan/task/template signature, and crowd source plus company. A changed business input therefore remains eligible, while content refusal is terminal only for the exact old input. Crowd failures are isolated per source/company pair, so one failed company does not stop the next eligible company.

Dependency failures consume no transient budget. The first same-input retry is the governed dependency probe; repeated outages return to the persisted park interval, and success clears the dependency backlog. Research-task probes bypass its normal unchanged-plan idle timer, which otherwise remains intact. Transient behavior keeps the prior finite limits. Missing grants and rejected crowd governance records are classified as `not_permitted`; a restored grant or changed runner-governance configuration clears that authorization state.

Normal scheduler states remain outside the ledger: no mission, no due zero-base work, an existing reflection, unchanged research inputs, disconnected crowd sources, and a source with no job do not record failures.

## Validation

`PYTHONPATH=$PWD/src python3 -m unittest tests.test_mission_reflection_lane tests.test_mission_zero_base_lane tests.test_mission_research_task_lane tests.test_crowd_source_lane`

Result: 96 tests passed. The focused set covers restart replay and same-signature dependency probes for reflection, zero-base review, and research task, plus the existing crowd per-company failure isolation, cooldown, configuration-reset, quiet-state, and producer/verifier-pair compatibility contracts.

`python3 -m py_compile` passed for all four changed lane modules, and `git diff --check` passed.

The shared replay implementation remains outside this branch. This branch uses its existing API and is intended to be integrated with the main-line replay fix (`2700728`). Full-suite validation is owned by the main integration run.
