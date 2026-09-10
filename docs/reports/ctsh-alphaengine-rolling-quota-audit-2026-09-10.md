# CTSH AlphaEngine rolling-quota audit — 2026-09-10

Status: read-only production-state and accepted-source audit. No live state was changed, no budget was enlarged, and no connector or model call was made.

At **2026-09-10 23:30:53 UTC**, the trailing 24-hour AlphaEngine window held **130 / 130 calls**: **22 `search_library`** calls and **108 `get_document` page** calls. The first counted call expires at **2026-09-11 16:26:44 UTC / 12:26:44 EDT**; capacity then returns one call at a time as later calls age out.

CTSH holds one of the four required earnings-call periods: `FY2026-Q2`. The missing periods are `FY2025-Q3`, `FY2025-Q4`, and `FY2026-Q1`. It has no pending earnings-call document. Its sell-side requirement is already overfilled at 20 documents against a floor of 3. AlphaEngine search and document governance are approved and the mission marks the source connected. The current blocker is the exhausted rolling allowance together with a research-plan directive that explicitly waits for that allowance; there is no source-discovery failure hold.

The stop is not permanent in the accepted code:

- `research_planner_cli.read_spend()` recomputes `alphaengine_24h.spent`, `cap`, and `remaining` from `count_recent_alphaengine_calls()` whenever the planner child runs.
- `research_state.build_research_state()` includes that spend object in the state content hash while excluding only the observation timestamp.
- `research_planner_cli.run_planner()` keys both existing-plan lookup and the model request identity to the state hash. A rolling-window change therefore creates a new planning identity rather than replaying the old stop.
- `research_planner_launcher.ResearchPlannerCoordinator` launches again after its one-hour `IDLE_HOLD` even when its cheap database-count signature has not moved.
- `mission_source_discovery.MissionSourceDiscoveryCoordinator._plan_decision()` reads `CoverageMissionAuthority.latest_research_plan()`, so a newly stored plan supersedes the prior directive.

One timing constraint remains bounded: source discovery runs before the planner in a controller tick, so a newly released call can first be consumed by a finite queued acquisition. Queue status changes feed the full planner state, and the hourly planner retry continues; once no allowed queued work consumes the opening, the planner observes positive remaining capacity.

Next regression: with a fixed clock, store a `remaining=0` stop plan and one AlphaEngine invocation; advance beyond both the invocation's 24-hour expiry and `IDLE_HOLD`, then assert a planner relaunch, a different state hash and request identity, and that source discovery reads the newly stored directive rather than the old stop.
