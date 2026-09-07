# P9d-11/12/13: the web lane's queue stops leaking

*2026-09-07*

Three defects found while watching the first day of autonomous web research,
each one a way for a discovered document to fall out of the queue without
anyone being told. All three are fixed here and deployed together.

## P9d-11: deploys orphaned in-flight work

**What happened.** Every deploy boots the writer out. Each launcher's
`close()` terminates its running child, and the next tick settles that ticket
as `orphaned (exit None)`. The lane then parks that company/spec pair for its
retry interval, which is a day. Two deploys on 2026-09-07 burned two slots
exactly that way. This is Dalton's own behaviour, not launchd's.

**What was considered and rejected.** Detaching children into their own
session and adopting their summary on completion. The SEC lane launcher carries
an explicit test that a dead pid must never be promoted to success from a
summary file on disk. That rule is worth keeping for every lane, so the
launchers are untouched.

**The fix.** `install.sh` now calls `dalton_core.launch_drain` after the new
wheel is installed and before `launchctl bootout`. It polls the four ticket
directories (`acquisitions`, `discoveries`, `fetches`, `sec-lane-runs`) until no
ticket is both `running` and backed by a live pid, or until `DRAIN_TIMEOUT`
(default 600 s) passes, in which case it says so and the deploy proceeds. It is
read-only: it never signals a child, never rewrites a ticket, never opens Core.

## P9d-12: two ways a document never reached the human queue

**Stranded by a mission version change.** Every acquisition query joins the
active version pointer, so publishing a new CoverageMission version silently
strands whatever the prior version had discovered but not finished. Live,
v3 → v4 stranded ten URLs, six of which were never re-cited.

The tick now runs `carry_forward_superseded_documents` before any new spend. It
copies each unfinished row from a superseded version into the current one,
**under the current version's own grant**: the company must still be in the
universe and the source still `connected` for automation, exactly as for a
fresh discovery. Rows the grant refuses are reported with the reason, not moved.
The copy keeps `document_ref`, `discovery_ref` (the original envelope remains
the only route from ref to URL), `host` and the original timestamps, so queue
order and retry timing are preserved. `acquisition_launched` becomes
`discovered`: its bytes will land through the old ticket and the already-held
path settles the copy without a second fetch. `acquired` rows whose review under
the old version was already resolved are finished and are not copied. It is
idempotent.

**Already in authority, never queued.** When search cites a URL whose bytes
Core already holds (a human fetched it first), the row was recorded
`already_in_authority`, and nothing ever moved it: review registration required
`acquired`. Live, the one such document was the hand-fetched Accenture results
PDF, which is exactly the kind of source the mission wants read.

The tick now settles every `already_in_authority` row to `acquired` (after
re-checking this source's authority actually holds the bytes) and registers its
review. No fetch is spent. `settle_document_already_held` accepts both
`discovered` and `already_in_authority`.

The AlphaEngine coordinator tests that encoded the old rule ("already held
means never queued") were updated to the new one; that rule was the bug.

## P9d-13: the queue had no order and the ledger knew no hosts

**What was missing.** A discovered document row carried only a hashed ref. The
mission view could not say which host a document was on, and the queue was
strictly FIFO. First-party investor-relations pages sat behind aggregators, and
hosts that return 403 to the lane cost one governed call per URL to learn it
again.

**The ledger learns the host.** `coverage_mission_discovered_documents` gains a
nullable `host` column (additive `ALTER TABLE` for existing databases; fresh
ones carry it in the DDL). The search child records it at discovery time from
the same URL authorities it already rebuilds for the owner. Rows recorded before
this slice are backfilled by the coordinator, 25 per tick, from each row's exact
discovery envelope through the read-only connector spool; a row with no host is
neither preferred nor skipped.

**The plan carries the policy.** Discovery plan schema 0.3 (web-search only)
adds a closed `acquisition` block:

```json
"acquisition": {
  "preferred_hosts": ["newsroom.accenture.com", "investors.epam.com", "..."],
  "skip_hosts": ["news.alphastreet.com", "www.spglobal.com"]
}
```

Preferred hosts are fetched before the rest, oldest first within each group.
Skipped hosts are never fetched and never retried, and the idle tick reports how
many rows the skip is holding. The two lists cannot overlap; hostnames are
validated; each list holds at most 50. The policy is human-authored and hash
bound like everything else in the plan, so it is a new plan version:
`discovery-plan:us-it-services:web-search:3`. Companies, specs and budget are
byte-identical to v2.

Only hosts verified on 2026-09-07 to return 403 to the lane's own user agent on
every path are skipped. `seekingalpha.com`, `stockanalysis.com` and
`www.reddit.com` also failed live, but their roots answered 200 to a probe, so
they are not on the list; now that failures carry a reason, the ledger will say
whether they belong there. Two of the preferred hosts, `investors.epam.com` and
`investors.cognizant.com`, answered 403 at the root. Preference costs at most one
legible failure per URL, so they stay listed; the ledger will show what they do.

## What this does not do

- It does not learn a skip list from failures. That would be automation writing
  policy. The per-host failure reasons are in the ledger for the owner to read.
- It does not fetch anything new. Every change here is queue bookkeeping under
  the same grants and the same budget.
- It does not change the launchers. See P9d-11 above for why.
