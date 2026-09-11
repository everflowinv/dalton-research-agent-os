# Mission source shortfall retry regression

## Finding

The source discovery coordinator still evaluates every company in the active
mission; an earlier `initial_screen` decision does not remove that company from
discovery. The apparent inactivity came from a cadence regression. Commit
`4354037` made a completed search that remained below its checklist floor use
the configured short retry interval. A later pagination change retained an
immediate continuation-page exception but stopped passing
`use_retry_interval=True` when no cursor existed. Such a source then waited for
the normal seven-day rediscovery interval despite a known shortfall.

The repair restores the short retry interval whenever the authoritative current
checklist remains below its floor. A valid continuation cursor remains eligible
immediately, an open child remains blocking, and a satisfied item continues to
use the normal rediscovery interval. No budget, permission, plan, or connector
gate is bypassed.

## Read-only production evidence

A coherent SQLite snapshot taken on 2026-09-11 showed active mission v14 and
the following current counts: ACN earnings 0/4 and annual 0/1; CTSH earnings
1/4 and annual 0/1; EPAM earnings 2/4 and annual read 0/1; IBM earnings 0/4;
DXC annual 0/1 and broker research 1/3. These are current-source readiness
counts, while the folded stage authority still contains prior human
`gate_passed` decisions for ACN, EPAM, IBM, and DXC and a `gate_failed` decision
for CTSH. The counts fold the same mission family across mission versions; the
gaps are not caused by a v14 reset.

The same snapshot showed the shared AlphaEngine allowance exhausted at 130/130.
It also contained many dismissed documents, including both provably wrong
issuer records and records that were read successfully but yielded no new
admissible statement. Those categories must remain distinct; a dismissed row
alone is not evidence of wrong-company attribution.

The latest plan was
`mission-research-plan:2363e4431d419e144f1d981867f95e14`, created at
2026-09-11T03:46:33.937808Z with content hash
`8494f78c4882398f9052505aa36d4d32b861ca114aef47f6a051ac4cc5dda1cf`.
It explicitly stops EPAM earnings-call acquisition because the shared allowance
has zero calls remaining and one queued transcript would still leave the floor
short. It separately stops ACN earnings-call acquisition while the allowance is
exhausted, placing renewed capacity after EPAM's shorter completion path. It
also stops the industry-demand and competitive-landscape queues because they
already hold 57 and 55 documents against floors of three. Before the companion
acquisition fix, discovery honored these directives but acquisition selected
already-queued documents without consulting the plan. Thus continued web-page
work could coexist with an explicit planner stop.

The companion repair filters only exact stopped company/spec pairs from fresh
and retry acquisition selection. It does not delete or settle those rows. A
later plan change therefore makes the same rows eligible again, and unrelated
queued work remains selectable.

Private refs-only evidence is retained outside the repository at
`stage-checklist-audit-20260911T0350Z.json` (SHA-256
`1003dddc823435c3f29d8babf17d29c9c28ff0a9d2c2f888c8d2bf858fd14326`).

## Verification

Focused tests cover a below-floor completed search both before and after its
configured retry interval, an immediate valid continuation cursor, and the
existing eight-day normal rediscovery behavior. The repair does not dispatch a
model call or alter live state.
