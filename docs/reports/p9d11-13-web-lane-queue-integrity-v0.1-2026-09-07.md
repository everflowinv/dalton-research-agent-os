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

## First deploy: two things the live system said back

**The drain could not converge.** It waited its full 600 s. The reason is that
the *controller* is what launches a lane child every tick, and the installer
stopped it only after the drain, in the same loop as the writer. With a slow
fetch every five minutes there was rarely a two-second gap. The order is now:
stop thesis-impact, control and controller; drain; stop the writer. The fetch
that the timeout then interrupted was settled `orphaned`, so this deploy cost
one more slot, the last one this defect gets.

**Sixteen rows could no longer rebuild a URL authority.** The host backfill
reported `search SourceEnvelope refs differ from exact Gemini citations` for six
rows, and a fetch child failed the same way. Counting across the active version:
112 rows fine, 16 not, all from the two discoveries whose citations included the
Gemini redirect proxy. That was a regression from P9d-10. The authority rebuild
re-normalizes the raw search bytes and compares the refs with the envelope; the
envelope had been recorded with the proxies among its refs, the new normalizer
drops them, so the comparison failed and every real URL in those discoveries
became unfetchable.

The fix keeps the envelope as authority over what was cited. It is re-verified
with the normalization of its own era (the pre-P9d-10 normalization is kept
behind `drop_redirect_proxies=False` for exactly this purpose), and the policy of
which citations may become documents is applied only to what is emitted. A ref
the search never cited is still refused in both eras.

**What worked on the first tick.** Plan v3 active with its hash; six v3
documents carried forward as `discovered` under v4; nineteen rows learned their
host; the hand-fetched PDF settled `acquired` and entered the review queue.

## Second and third deploys: the drain still waited, and the diagnosis was wrong twice

The drain waited its full 600 s again with the controller down, on a single
fetch child that had written its summary within seconds and then stayed "alive"
for twelve minutes. The first reading was a child that never exits; a fix that
forced `os._exit` after the summary went out on the third deploy. It changed
nothing: a *new-code* child showed the same signature three minutes after the
writer restarted.

`ps` gave the answer: state `Z`. The child had exited. The writer only reaps a
child when it next polls the process handle, on its next tick, so for up to
five minutes an exited child is a zombie, and `kill(pid, 0)` succeeds on a
zombie. With the controller stopped there is no next tick at all, so the old
writer never reaped, and the drain counted the zombie as running until its
timeout. Every "child alive after its summary" observation, failed or
succeeded, was this.

The forced exit is reverted; it was a fix for a problem that did not exist. The
drain now treats a zombie as exited (`/proc` on Linux, `ps` on macOS), with a
test that spawns a child, observes it as a zombie, and checks the drain ignores
it. The two earlier sections of this report that spoke of a hang were written
before this was known; this section supersedes them.

## A census of the ticket directories found the larger loss

Counting every ticket on disk by status and by whether a summary exists:

| lane | orphaned with a summary present |
| --- | --- |
| discoveries | 6 |
| fetches | 5 |
| acquisitions | 2 |

Thirteen children had **finished and written their summary** and were still
settled `orphaned`. Settlement happens on the next tick, up to five minutes
after the child exits; if the writer restarted in that gap it lost the process
handle, saw `running` with a dead pid, and called the work orphaned. Each one
parked a company/spec pair for its retry interval. The drain cannot help here,
because the child is not running.

The three mission launchers now adopt a finished child's own summary in that
situation: when the pid is gone and `summary.json` carries a terminal status,
the ticket takes that status and records `adopted_from_summary: true`. This is
not guessing success from a stray file. The summary is the child's own record,
written into a per-launch owner-only directory, and every settle path still
re-verifies authority: a fetch or acquisition summary that says `succeeded` is
only honoured if the bytes are in Core through this source's own connector. A
dead pid with no summary, or a summary without a terminal status, stays
`orphaned`. The SEC lane launcher is deliberately unchanged; its child writes
formal Claims itself and its test that a dead pid must never be promoted from
disk stands.

## The two proxy rows

The host backfill kept reporting the two documents that *are* the redirect
proxy: no authority can be rebuilt for them, so every tick logged the same two
failures and, once a day, a fetch child would have been spent learning it
again. The backfill now reads hosts through `cited_url_hosts`, which sees every
ref the envelope names, proxies included, so those rows carry their true host.
The coordinator holds the provider's redirect-proxy hosts together with the
plan's `skip_hosts`. That is not policy but a physical fact: the transport
refuses to follow a redirect out of the pinned host, so fetching such a row can
only ever fail.
