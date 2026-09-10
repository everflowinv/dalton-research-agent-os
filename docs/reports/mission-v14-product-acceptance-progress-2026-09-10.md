# Mission v14 product acceptance — in progress

The owner signed mission v14 at `2026-09-10T15:42:54.603016+00:00`. The actual immutable hash is `0bae07a75bf1a7685d3dc9896317502aa3468eaa187e84ba9108c957983e6bb5`. Read-only verification recomputed the complete record hash and compared every reviewed semantic field, actor, version and prior version. The rehearsal hash differed because publication time is included in the record; it is preserved as simulation evidence, not used as the expected live hash. Five private checker tests confirm timestamp variation is accepted while scope, actor and hash tampering are refused. The actual signed receipt is retained in the private activation packet; no second signature was made.

Live health remains running. Product audit still shows 0/15 target dossier, DebateMap and event-judgement products. ACN/EPAM/IBM/DXC have eligible claims and 30 unjudged events each. CTSH currently lacks a passed Initial Screen (earnings-call evidence gate) and has no unjudged event; the audit does not invent eligibility.

Concrete post-signature blockers and fixes in progress:

- Dossier: an old v13 refusal settled after v14 signing was assigned the current permission key, permanently holding the authorized v14 input. The coordinator now binds control state into launch identity, preserves it through settlement and gives new control state a distinct ticket digest; legacy unbound failure items are retired with history preserved. 64 focused tests passed. Root commit `bfe7f1f`.
- DebateMap/ZeroBase: worker reopening of an existing scheduler policy attempted an unnecessary write transaction and failed behind another writer lock. Existing identical immutable policy now returns through a read-only lookup; fresh policy creation still uses the transactional recheck. 29 focused tests passed. Root commit `775f34e`.
- EventJudgement: its declared input envelope and per-call cost cap reject all configured brain routes before any call; bounded prompt/cost admission is being repaired without increasing the owner budget.
- Deployment/process ownership: lifetime controller lock and pre-upgrade legacy-resident checks are integrated, with fresh-install/scanner edge cases under review. They prevent the duplicate controller observed during recovery.

These follow-up fixes are not yet deployed. Next: complete integrated tests and a current-state rehearsal, deploy the tested fixes using the already-signed mission receipt, then inspect actual new product records rather than treating queued/stub launches as successful products. Model calls made by the running authorized automation are distinct from the read-only diagnostic work reported here.

## 16:02 UTC checkpoint

The read-only live audit still observes 0/15 products and verifies the same signed mission hash. The integration branch has been pushed through `fb88543`; the external session's four-file main-worktree diff remains unchanged (SHA-256 `43832e8e6d32cec9bf48b59044d200df0d57a99d30598859c23d4c1360207932`).

Controller, installer, dossier, scheduler and child restart integration passes 154 tests in 11.983 seconds. Adopted children now match their recorded command during every status check, preventing an unrelated reused PID from keeping a lane busy forever. The private deployment-wrapper candidate performs the controller ownership check before its backup; it will be bound only after new release acceptance.

The event coordinator now incorporates the judge, verifier and tracking configuration content into its batch identity. A Cockpit route change can retry the same unjudged event after the previous child settles; unchanged configuration remains quiet. Twelve focused coordinator/configuration tests pass. The broader event test run exposed two grouped-evidence regressions in the initial prompt-budget patch; these remain release blockers while the event agent repairs mandatory context preservation and a second agent reviews it.

The [real catalog budget audit](event-live-catalog-budget-review-2026-09-10.md) independently verifies router admission using a live SQLite backup: the 5,000-byte input / 700-token output envelope reserves at most $0.085 for configured brain and independent verifier routes, within the unchanged $0.10 cap. This proves admission only; it does not prove prompt completeness or a successful model response.

## Owner-directed configurable budgets — current checkpoint

The owner explicitly approved raising the event per-call ceiling to $1 and then required every operating budget to be configurable. The prior $0.10 was an internal lane constant, not a signed owner limit. The signed $100 daily mission budget remains unchanged. The 5 KB trimming approach has been replaced: complete event/group context and verifier drafts/cited evidence are retained, with a configurable whole-prompt bound (60 KB default), 1,500 output tokens and $1 per call. A fresh live Core copy produced 12/12 complete prompts successfully (7,797–8,826 bytes); no model was called by that check. Current catalog worst-case admission at the full configured envelope is $0.675 for both brain routes.

Integrated budget work now includes purpose-scoped call/run resolution, central packaged defaults, budget-bound WorkOrder identity, real extraction/metric/numeric work budgets, multi-unit producer/verifier reservations, explicit event pool caps and batch configuration, bounded-planner service configuration, and retention of all four override blocks across reinstall/tier changes. The optional four-pool mission contract is now publishable and validated against the signed daily cap; no live mission was republished.

Cockpit budget editing is under final independent review: per-purpose call and run forms, actual runtime configuration binding, actor-bound writer operations, configuration hash checks, a serialized edit lock, immutable change receipts, and idempotent retry after a lost success response. The planner form edits its actual service budget and reports restart requirements. Thesis-impact budgeting uses a dedicated state budget overlay, with its writer-side consumer wiring being completed. The editor's 13 focused tests and prior 155 combined model/event tests passed; the new full-suite/frozen-release acceptance has not started yet.

Next: finish the thesis consumer and remaining connector cap configuration, close review findings, freeze and run the full suite/wheel/current-state rehearsal, deploy the tested follow-up without re-signing v14, and inspect real products. CTSH has only two of four required valid earnings calls; eight wrong-company documents remain dismissed. Its input gate will not be weakened to manufacture acceptance.

## Budget integration and owner-expectations review

Thesis-impact consumer wiring and Guidepoint signed tick limits are now integrated. The Cockpit editor reads the planner's actual service configuration and thesis-impact's budget-only overlay, serializes concurrent edits, rejects stale conflicting saves, and treats a retry of the already-applied value as unchanged. Invalid or incomplete service configuration is isolated to that budget view instead of breaking the model page. The current integrated focused run passed 385 tests in 17.768 seconds; this is not a new full-suite or deployment claim.

The remaining AlphaEngine probe limit is being made an optional signed mission budget, preserving 30 for older missions rather than silently inheriting the wider total connector allowance. Its actual admitted loop and WorkOrder must carry the exact mission version/hash before the writer can consume that limit. No live mission or permission is being changed.

The owner additionally requested a systematic Cockpit bug review and a renewed comparison against onboarding and vision documents after current work. The [gap refresh](owner-expectations-gap-refresh-2026-09-10.md) distinguishes existing implementation without product acceptance from real missing features. Investment Memo production is a concrete missing loop; Excel formula export remains later. Current release/product verification and Cockpit usability remain the immediate priority.
