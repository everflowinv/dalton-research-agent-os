# Mission v14 product acceptance — in progress

The owner signed mission v14 at `2026-09-10T15:42:54.603016+00:00`. The actual immutable hash is `0bae07a75bf1a7685d3dc9896317502aa3468eaa187e84ba9108c957983e6bb5`. Read-only verification recomputed the complete record hash and compared every reviewed semantic field, actor, version and prior version. The rehearsal hash differed because publication time is included in the record; it is preserved as simulation evidence, not used as the expected live hash. Five private checker tests confirm timestamp variation is accepted while scope, actor and hash tampering are refused. The actual signed receipt is retained in the private activation packet; no second signature was made.

Live health remains running. Product audit still shows 0/15 target dossier, DebateMap and event-judgement products. ACN/EPAM/IBM/DXC have eligible claims and 30 unjudged events each. CTSH currently lacks a passed Initial Screen (earnings-call evidence gate) and has no unjudged event; the audit does not invent eligibility.

Concrete post-signature blockers and fixes in progress:

- Dossier: an old v13 refusal settled after v14 signing was assigned the current permission key, permanently holding the authorized v14 input. The coordinator now binds control state into launch identity, preserves it through settlement and gives new control state a distinct ticket digest; legacy unbound failure items are retired with history preserved. 64 focused tests passed. Root commit `bfe7f1f`.
- DebateMap/ZeroBase: worker reopening of an existing scheduler policy attempted an unnecessary write transaction and failed behind another writer lock. Existing identical immutable policy now returns through a read-only lookup; fresh policy creation still uses the transactional recheck. 29 focused tests passed. Root commit `775f34e`.
- EventJudgement: its declared input envelope and per-call cost cap reject all configured brain routes before any call; bounded prompt/cost admission is being repaired without increasing the owner budget.
- Deployment/process ownership: lifetime controller lock and pre-upgrade legacy-resident checks are integrated, with fresh-install/scanner edge cases under review. They prevent the duplicate controller observed during recovery.

These follow-up fixes are not yet deployed. Next: complete integrated tests and a current-state rehearsal, deploy the tested fixes using the already-signed mission receipt, then inspect actual new product records rather than treating queued/stub launches as successful products. Model calls made by the running authorized automation are distinct from the read-only diagnostic work reported here.
