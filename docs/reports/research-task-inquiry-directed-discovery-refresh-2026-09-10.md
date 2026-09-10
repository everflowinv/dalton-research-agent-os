# Inquiry-directed discovery refresh — 2026-09-10

## Result

A proposed, inactive-until-human-publication ProbeTemplate now connects an admitted ResearchTask probe to the existing governed AlphaEngine discovery child. The implementation is intentionally narrow: only companies admitted by the exact mission/mandate and the published template plan scope whose normalized inquiry text maps to earnings-call/transcript or sell-side research intent receive this binding. Unmapped intents and companies outside those authorities receive no binding.

The writer re-verifies the exact Scheduler WorkOrder, bounded-loop/template binding, mission version/hash, inquiry hash, and the configured discovery plan ref/hash before launch. It continues to rely on the existing source authorization, connector governance, query compiler, and launcher. The proposal does not sign or publish the template.

A retry after a caller deadline reuses the existing mission/company/spec/query dispatch and polls its ticket. A running child produces a transient hold, not a failed/free probe, so the Scheduler does not settle it and a later tick cannot create a second acquisition. Success requires the child summary's formal search outcome plus connector invocation and source-envelope refs/hashes; process exit success alone is refused.

## Files

- `src/dalton_core/research_task.py`: proposed template, exact intent mapping, ACN confinement, discovery-plan binding.
- `src/dalton_core/bounded_alphaengine_search_probe.py`: bounded writer launch/poll adapter and formal ResultEnvelope projection.
- `src/dalton_core/bounded_planner_driver.py`: operation dispatch with transient-hold behavior.
- `src/dalton_core/writer_server.py`: authority re-validation and idempotent governed child launch.
- `deploy/phase8/p14e-adhoc-probe-templates-v1.json`: reviewable proposed template only.

## Validation

- `PYTHONPATH=src python3 -m unittest tests.test_bounded_alphaengine_search_probe tests.test_research_task tests.test_bounded_planner_driver`
- 92 focused tests passed after adding the real Store/Scheduler/WriterServer authority fixture.
- The integration fixture proves one launch/dispatch/WorkOrder on exact replay and rejects scheduler, template-head, discovery-plan, mission, company, ref, and hash drift before launch.
- `python3 -m py_compile` passed for all four changed Python modules.
- `git diff --check` passed.

No network request, paid model call, live-state write, signature, or deployment occurred.
