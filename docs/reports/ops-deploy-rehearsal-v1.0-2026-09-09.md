# OPS — deploy rehearsal against a copy of the live Core — v1.0 — 2026-09-09

Branch `ops-deploy-rehearsal`, worktree
`~/Projects/dalton-ops-deploy-rehearsal-worktree`, base main `ebd2ea8`
(3,932 tests). Nothing under `~/Library/Application Support/Dalton/` was
written. No launchd agent was started or stopped. No live broker socket was
opened. No network call and no model call was made. The runbook this produced
is `docs/reports/deploy-runbook-v1.0-2026-09-09.md`.

---

## 0. Why there is a script instead of a run of `install.sh`

`deploy/macos/install.sh` opens with

```sh
dalton_root="$HOME/Library/Application Support/Dalton"
state_dir="$dalton_root/state/dalton-core"
config_path="$config_dir/service.json"
```

and never reads an environment variable for any of them. `PYTHON_SOURCE`,
`DRAIN_TIMEOUT` and the `DALTON_*` model knobs are overridable; the root is not.
It also calls `launchctl bootout` on four labels in its first thirty lines. So
there is no way to point it at a scratch directory, and running it to find out
whether the deploy works *is* the deploy.

`scripts/rehearse_deploy.py` performs the same ordered steps against a
temporary root. It is not a substitute for `install.sh` — the owner still runs
that — it is a way to be wrong somewhere cheap first.

```
scripts/rehearse_deploy.py --live-root <ro> --temp-root /tmp/dalton-rehearsal-<ts>
```

It refuses a `--temp-root` outside `/tmp`, and refuses one equal to the live
root. Live databases are read through `sqlite3.connect("...?mode=ro")` plus the
backup API — the pattern the repo's other live canaries already use, and the
reason a `cp` of the three WAL files is not good enough: a checkpoint between
the header copy and the page copy hands you a torn database that opens fine and
is wrong.

The steps, in `install.sh`'s order:

1. copy live state (read-only)
2. rewrite `service.json` onto the temp root and onto a stub broker directory
3. `dalton-bootstrap`
4. every `*_schema.sql` — all 52, not the five bootstrap opens
5. the governance seeds and discovery plans `install.sh` seeds
6. the model catalog sync against a copy of `model-router.sqlite`
7. render the LaunchAgent plists and diff them against the installed ones
8. read `autonomy.may_write` off the copied mission
9. check the lane switch files
10. start the writer against the temp Core with the broker stubbed
11. one `BoundedPlannerDriver.run_once`

The broker is a real UNIX socket server in the temp root that answers one
line-delimited JSON refusal. It exists so the model path is *wired* — the
socket is there, the key is there, the client connects and gets an answer —
without a call leaving the machine and without `~/.openclaw/dalton-*.sock`
being opened.

---

## 1. The tick table, verbatim

One `run_once` against the temp Core, writer started from the rendered writer
plist, broker stubbed.

```
lane                       status        reason
-------------------------  ------------  ------------------------------------------------------------------------------
mission_source_discovery   idle          acquisitions_launched=0
guidepoint_discovery       unconfigured  no Guidepoint lane on this writer
document_extraction        launched      awaiting=15 max_discovery_windows=10 max_numeric_windows=10 max_windows=30
mission_stage              idle
claim_review               idle          deferred=1470 documents_read=40 scanned=2285 unreadable=815
mission_sec_quarters       idle
mission_statements         idle
mission_market_prices      ungranted     this mission does not grant market_price in autonomy.may_write; publishing a p
mission_tracking           unconfigured  no tracking lane on this writer
mission_catalyst_calendar  unconfigured  no approved catalyst-calendar connector on this writer
company_model_spec         idle          every company has a current specification
company_model_forecast     launched
research_plan              launched
claim_index                unconfigured  no claim index lane on this writer
initial_screen             launched
event_judgement            unconfigured  no judgement lane on this writer
sales_notes_feed           unconfigured  no source:sales-notes lane on this writer
company_wiki_feed          unconfigured  no source:company-wiki lane on this writer
debate_map                 launched
company_dossier            unconfigured  no company-dossier lane on this writer
mission_crowd_sources      unconfigured  no crowd-source lane on this writer
research_task              unconfigured  no research task lane on this writer
mission_reflection         launched
mission_sec_dispatch       idle          settled=0
forecast_reconciliation    idle
tick_ledger                recorded      lane_count=23
```

```
ok     4.8s  one controller tick -- 26 entries, 0 escaped, 4.8s (service tick_seconds=5.0)
```

**Zero lanes escaped.** Not one `unavailable:<Exception>`, not one
`unrecorded:<Exception>`. Twenty-three registered lanes plus the two non-lane
tick calls plus the tick ledger. Every lane that could not run looked at its own
preconditions and said so in a word an operator can act on: `ungranted` means
publish a mission version, `unconfigured` means a file is not on disk.

Six lanes launched a child. Those children ran against the temp Core, wrote into
the temp root, and reached the stub broker; none of them survived the writer's
shutdown and none of them made a network call. `mission_source_discovery`,
`mission_sec_quarters`, `mission_statements` and `mission_sec_dispatch` — the
four that would fetch — all reported `idle`.

`tick_ledger: recorded, lane_count=23` is C2's new bookkeeping doing its job on
the first tick after the migration, which was the specific thing worth proving:
the ledger write is the last thing a tick does and is allowed to fail, so a
silent `unrecorded:` would have been easy to miss.

## 2. Step timings and cost

```
ok     3.1s  copy live state (read-only) -- 23 copied, 0 absent, 807 MB
ok     0.0s  rewrite service.json onto the temp root
ok     0.4s  dalton-bootstrap -- core=core.sqlite tokens=writer-tokens.json
ok     3.9s  migrations (every *_schema.sql) -- 52/52 schemas applied
ok     0.0s  governance seeds -- 2 seeded, 16 already present, 0 absent from repo
ok     0.2s  model catalog sync (copy of model-router.sqlite)
ok     0.0s  render LaunchAgent plists and diff -- 3 plists rendered
ok     0.0s  mission grants (autonomy.may_write) -- may_write grants 11, missing 11
ok     0.0s  lane switches on disk -- 3/7 lane switches on disk
ok     0.3s  start writer against the temp Core -- writer pid 49598, socket bound
ok     4.8s  one controller tick -- 26 entries, 0 escaped
```

Whole rehearsal: **13 s wall, 817 MB on disk**. A second run half an hour later
took 32 s for the same steps — 10.4 s to copy and 15.1 s to migrate rather than
3.1 and 3.9 — because the live writer was busier and the read-only backup and
the migrations both wait on it. Both runs exited 0 with the same lane table, so
treat the spread as the honest number: **13–35 s**.

The live root is 23 GB; the
working set — the databases, the governance records, the plans, the model
configs — is 807 MB of it, and the other 22 GB is fetched documents that no
migration and no tick reads. The migration step is the one an owner should
budget for: 3.9 s here, against a live Core with the writer holding
connections it will be slower, but it is seconds, not minutes.

`install.sh` itself will be dominated by `pip install`, which this does not
rehearse.

## 3. Findings

### 3.1 Migrations — all 52 pass

Every `*_schema.sql` in `dalton_core` was applied against the copied live
databases, including all of the new ones. Nothing failed.

Worth naming, because it is not obvious from `install.sh`: **`dalton-bootstrap`
opens five authorities.** The other forty-seven schemas are applied the first
time the writer or a lane constructs their authority — which on a live Core is
several minutes *after* the install script has already exited zero. A deploy
that breaks a schema does not fail at install time; it fails on the first tick
that touches that table. That is why the rehearsal runs them all up front, and
why `tests/test_rehearse_deploy.py::MigrationCoverageTests` asserts the owner
list covers every shipped `.sql` — a new schema with no entry would let this
report say "clean" without having run that migration.

**P14a's `mission_deliverable` CHECK widening** ran on the live copy and the
rebuilt table carries `'event_note'`; `PRAGMA foreign_key_check` came back
empty. This is the only pre-existing table structure this deploy changes.

**C2's pool columns** were added to the live day ledger copy: four nullable
columns and `model_budget_pool_rejections`. No content hash moved.

### 3.2 The pre-migration admission replay case — measured

```
pre-migration admissions: 5036/5037 rows have pool IS NULL and replay as
'unpooled' in pool_status until they settle
```

Every admission the live Core has ever written predates the pool column, so on
deploy day the pool figures will not add up to the day cap and `unpooled` will
be most of the spend. It drains as those admissions settle. This is expected
and is in the runbook as step 5.

The replay-compatibility case itself (`_POOL_BINDING_KEYS`: a stored binding
with no pool keys at all is treated as the same admission gaining a dimension,
rather than as a changed binding) could not be triggered here because the
rehearsal makes no model calls. It is covered by C2's own tests; the runbook
names it as the thing to look at first if a replay is refused on deploy day.

**The ordering constraint is real and `install.sh` does not honour it.** The
day ledger migration wants a write lock and the live writer holds a long-lived
connection to that database, so it has to run with the service stopped. Nothing
in `install.sh` opens `ThesisImpactBudgetStore`, and the writer will not do it
either — the day ledger belongs to the thesis-impact agent, which is disabled
in the live `service.json`. Runbook step 5 is a one-liner the owner must run by
hand.

### 3.3 Seed path mismatches — three, one of them dangerous

**(a) `sec-filings-index-v1.json` exists on the live Core and in no
repository.** The writer's plist names it unconditionally
(`--sec-filings-governance`), `install.sh` has no seed block for it, and
`deploy/connector-governance/` does not carry it. A Core rebuilt from scratch
would get a writer pointed at a path that does not exist. Owner: whoever added
`--sec-filings-governance` to `macos_launchagent.render` (P10u). Until then the
runbook's backup step copies the whole governance directory, which is the
mitigation.

**(b) Nineteen committed governance records that `install.sh` never seeds:**

```
cn-hk-findata-ah-premium-v1, cn-hk-findata-buybacks-v1,
cn-hk-findata-financial-statements-v1, cn-hk-findata-margin-balance-v1,
cn-hk-findata-northbound-flow-v1, cn-hk-findata-shareholders-v1,
company-wiki-get-document-v1, company-wiki-list-documents-v1,
employee-reviews-blind-v1, guidepoint-get-transcript-narrowing-v1,
sales-notes-get-note-v1, sales-notes-list-notes-v1, x-xreach-search-v1,
x-xreach-thread-v1, x-xreach-user-timeline-v1, xueqiu-get-post-v1,
xueqiu-hot-rank-v1, xueqiu-search-posts-v1, yfinance-calendar-v1
```

Four of these — the `sales-notes-*` and `company-wiki-*` pair — are deliberate
and `install.sh` says so in a comment: a record alone does not turn a feed lane
on, and seeding half of what a lane needs gives the owner an approval to make
and a lane that refuses every tick, which reads like a fault rather than an
absence. That reasoning is right and it should be extended, not repealed.

The other fifteen have no such comment. **`yfinance-calendar-v1.json` is the
one to look at**: C1's catalyst-calendar lane is a single seed block away from
existing, the record is committed, and today the lane reports `unconfigured`
for ever. Owners: C1 (calendar), S3 (`xueqiu-*`, `x-xreach-*`,
`employee-reviews-*`), S4 (`cn-hk-findata-*`), S2 (`guidepoint-get-transcript-narrowing`).

**(c) `install.sh` seeds `guidepoint-search-library-v1.json` and
`guidepoint-get-transcript-v1.json`, and forty lines later a comment says "Not
seeded here: ... the Guidepoint lane's own record."** The comment is stale, not
the code. Harmless, but it is the kind of thing that makes the next person
distrust the rest of the comments.

### 3.4 Four lane switches nothing writes

`3/7 lane switches on disk` after the full install sequence. Absent:

| lane | file | who should write it |
| --- | --- | --- |
| `mission_tracking` (P14a) | `tracking-policy.json` | `install.sh` seed block; the source is `deploy/phase9/p14a-tracking-policy-v1.json`, committed and ready. P14a §9.5 says so explicitly. |
| `event_judgement` (P14a) | `event-judgement-model-config.json` | a setup module call in `install.sh` |
| `event_judgement` verifier (P14a) | `event-verifier-model-config.json` | same; must be a **different model family** or every judgement fails closed |
| `claim_index` (P12b) | `claim-index-model-config.json` | same |

`initial-screen-model-config.json` and `research-planner-model-config.json`
count as present because the live Core already has them; on a fresh machine
they are written only when `DALTON_DELIVERABLE_MODEL_*` / `DALTON_PLANNER_MODEL_*`
is set, which is by design.

### 3.5 Plist diff — three new writer arguments, all expected

Against the installed `space.lumos.dalton.writer.plist`, with the temp root,
log directory and venv mapped back:

```
space.lumos.dalton.writer: ProgramArguments: +--market-price-governance
space.lumos.dalton.writer: ProgramArguments: +.../connector-governance/yfinance-daily-prices-v1.json
space.lumos.dalton.writer: ProgramArguments: +--model-forecast-lane
space.lumos.dalton.writer: ProgramArguments: +--debate-map-model-config
space.lumos.dalton.writer: ProgramArguments: +--reflection-lane
```

Nothing removed, nothing reordered, and the controller and control plists are
byte-identical after normalisation. `space.lumos.dalton.thesis-impact.plist`
does not exist on either side (`thesis_impact.enabled` is `false`).

**Ordering that this found, in the rehearsal script rather than in the
product:** several lanes' `argv_fragment` turns the lane on only when its
governance record is *already on disk*. The first version of this script
rendered the plists before seeding, and reported
`mission_market_prices: unconfigured` as though the deploy were at fault. The
real `install.sh` seeds first and is correct. Fixed here; the corrected order
is what the table in §1 was produced by.

### 3.6 Catalog sync and the verifier pin

The sync ran against a copy of `model-router.sqlite` with the broker catalog
read from the live `~/.openclaw/openclaw.json` (read-only). It registered five
profiles:

```
profile:claude-fable-5-1, profile:gemini-3-8-flash,
profile:qwen-deepseek-v4-flash-0731-low-calibration, profile:zai-glm-5-3,
profile:zai-glm-5-3-flash
```

and a second run in the same rehearsal wrote nothing, which is the property
that matters — `install.sh` runs this on every deploy and now *fails* the
install if it errors, so a non-convergent sync would mean a deploy that can
never succeed twice.

Then, on the copy:

```
verifier phase pin model-routing-policy-version:dalton-openclaw-verifier:1
names profile:gemini-3-7-flash, which this sync retired; verifier routing will
be refused with profile_retired until the owner repoints the pin at the
verifier tier chain
```

Confirmed against the synced copy: that profile's latest version carries
`status: retired`. Nothing calls the verifier today (`thesis_impact.enabled` is
`false`), so this is deferrable — but turning thesis-impact back on without
repointing the pin fails closed on every call. Runbook step 9. Owner:
`model_deployment` / P14-M.

### 3.7 Mission grants — eleven words missing

The live mission is `coverage-mission-version:us-it-services:13` with eleven
`may_write` words. Merged main needs eleven more:

```
market_price, market_event, claim_index, research_task, dossier, debate_map,
valuation, consensus_estimate, forecast_revision_proposal,
thesis_revision_candidate, conviction_call
```

All eleven are already in `coverage_mission.AUTOMATION_WRITE_SCOPES`
(asserted by a test here), so a mission version can grant them today.
`human_checkpoints` also lacks `thesis_revision_candidate`, and
`scripts/build_mission_v2_params.py` has no `--add-checkpoint` — that one line
has to be edited into the params file by hand. The exact command and the exact
resulting list are runbook step 7; the params file was built and validated
against a copy of the live Core, so the only unrehearsed part is the
`dalton-gov` call itself.

### 3.8 Two things fixed in this branch, and what was left alone

Fixed here (script only, no product change): the rehearsal's own step order,
and its config handling — the first version disabled the outbox to avoid
network, which made `ServiceConfig` refuse an enabled weekly-brief coordinator
with no outbox bridge. The config is now rewritten for paths and nothing else,
which is also the more honest rehearsal: the blocks that reach the network
belong to `daltond`, and this never starts `daltond`.

**No migration was changed.** All 52 applied cleanly against live data, so
there was nothing to fix. The findings in §3.3, §3.4, §3.6 and §3.7 belong to
`install.sh`, `macos_launchagent`, `model_deployment` and the mission
respectively, and are reported rather than patched — this branch's remit was to
prove the deploy, not to redesign the seeding.

---

## 4. Tests

`tests/test_rehearse_deploy.py`, 47 tests over the script's pure parts: the
copy plan, path rewriting and its inverse, the seed list against `install.sh`
itself, schema-owner coverage, the plist diff, the lane-status classification
and table, the mission grant list, the lane switches, and the two `--temp-root`
guards.

The one that earns its keep is `InstallSeedTests`: `INSTALL_SEEDS` is a second
copy of a list that lives in a shell script, and it asserts in both directions
against the script's non-comment text — comments are stripped because half the
connector names in that file appear only in a comment explaining why they are
*not* seeded.

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest tests.test_rehearse_deploy
...............................................
----------------------------------------------------------------------
Ran 47 tests in 0.361s

OK
```

Full suite:

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
Ran 3979 tests in 411.085s

OK (skipped=1)
```

(main `ebd2ea8` is 3,932; +47.)

---

## 5. Open

1. **`sec-filings-index-v1.json` has no home.** Someone has to either commit it
   to `deploy/connector-governance/` and add a seed block, or make the writer's
   plist stop naming it unconditionally.
2. **The seed list is a fourth copy.** `install.sh` hard-writes each block, the
   lane's `argv_fragment` decides whether the lane turns on, the cockpit reads
   the record's status back out of that same `argv_fragment`, and now this
   script carries `INSTALL_SEEDS`. Wave 0's open question 3 — a
   `governance_seed` field on `LaneSpec` that `install.sh` derives from — would
   collapse three of the four. This script's list is the argument for doing it.
3. **The rehearsal launches real lane children.** Six did. They ran against the
   temp Core and reached the stub broker, and the four fetching lanes were all
   idle, so nothing left the machine — but that is a property of the live
   state's current queue, not a guarantee. A `--no-children` mode that stubs the
   launchers would make it one.
4. **`install.sh` still cannot be pointed anywhere.** Four lines and one
   `${DALTON_ROOT:-...}` would make the real script testable, and this script
   mostly unnecessary. That is a change to `install.sh` and was out of remit.

---

## 6. Addendum — re-run against merged main, 2026-09-10

Branch `ops-rehearsal-fix`. Main moved under this report: INT2 landed a second
batch of install seeds, and the extraction-throughput, dossier, debate-map and
ADR-0009 slices landed with them. Two of this report's tests failed on the new
main. Both failures were correct — the report had gone stale — and both are
fixed here. The rehearsal was re-run end to end on a fresh copy of live.

**Still zero escaped.** 53/53 schemas applied, 26 tick entries, no
`unavailable:` and no `unrecorded:`.

```
lane                       status        reason
-------------------------  ------------  ------------------------------------------------------------------------------
mission_source_discovery   idle          acquisitions_launched=0
guidepoint_discovery       idle          all_grants_refused
document_extraction        launched      awaiting=14 max_discovery_windows=10 max_numeric_windows=10 max_windows=30
mission_stage              idle
claim_review               idle          deferred=1470 documents_read=40 scanned=2285 unreadable=815
mission_sec_quarters       idle
mission_statements         idle
mission_market_prices      ungranted     this mission does not grant market_price in autonomy.may_write; publishing a p
mission_tracking           unconfigured  no tracking lane on this writer
mission_catalyst_calendar  launched
company_model_spec         idle          every company has a current specification
company_model_forecast     launched
research_plan              launched
claim_index                unconfigured  no claim index lane on this writer
initial_screen             launched
event_judgement            unconfigured  no judgement lane on this writer
sales_notes_feed           unconfigured  no source:sales-notes lane on this writer
company_wiki_feed          unconfigured  no source:company-wiki lane on this writer
debate_map                 launched
company_dossier            unconfigured  no company-dossier lane on this writer
mission_crowd_sources      unconfigured  no crowd-source lane on this writer
research_task              unconfigured  no research task lane on this writer
mission_reflection         launched
mission_sec_dispatch       idle          settled=0
forecast_reconciliation    idle
tick_ledger                recorded      lane_count=23
```

```
ok    15.3s  migrations (every *_schema.sql) -- 53/53 schemas applied
ok     0.0s  governance seeds -- 14 seeded, 16 already present, 10 gated out, 0 absent from repo
ok     4.3s  one controller tick -- 26 entries, 0 escaped, 4.3s (service tick_seconds=5.0)
```

### 6.1 §3.3(b) is closed: INT2 seeded all nineteen

This report said nineteen committed governance records had no seed path.
`install.sh` now seeds every one, and `unseeded_governance_records()` returns
empty. The test that asserted `sales-notes-get-note-v1.json` was unseeded was
asserting a bug, and now asserts the invariant instead: *no committed record
may be seeded by nothing*. **§3.3(b) above is superseded; §3.3(a) is not** —
`sec-filings-index-v1.json` is still on the live Core and in no repository.

### 6.2 The seed test had an invisible hole, and it is why (b) drifted

The reverse check matched each committed record's name against `install.sh`'s
text. INT2's blocks are written as
`cn-hk-findata-${cn_hk_kind}-v1.json` over a `for` loop, so the literal
filename appears nowhere in the script and the check silently **skipped** those
six rather than failing on them. Six records install.sh genuinely seeds sat
outside `INSTALL_SEEDS` with the suite green.

The fix expands each `for X in a b; do ... done` body once per word with `$X`
substituted, so both directions now resolve loop-built filenames. A test whose
gap is invisible is worse than no test, and this one had been passing for a day
while wrong.

### 6.3 Two errors in the appended seed list

Both found by reading `install.sh` rather than by a failing test:

- **`guidepoint-get-transcript-narrowing-v1.json` had the wrong destination.**
  The appended entry sent it to `connector-governance/`. `install.sh` puts it
  in `governance-decisions/` and says why: nothing loads it, so a
  permanently-`proposed` copy under `connector-governance/` would show the
  owner a lane waiting for an approval about nothing. Corrected, and pinned by
  a test.
- **Nine seeds were missing entirely** — the six China records, the Guidepoint
  discovery plan, the feed plan and the crowd-source map. The six China records
  are the ones §6.2 explains.

### 6.4 `optional` now means gated, and the gate is evaluated

`optional` used to mean install.sh's `[[ -f "$repo_record" ]]` guard, which is
on every block and so said nothing. It now means *gated*: install.sh only
reaches the copy when a condition outside the repository holds. `gate` names
which, as a key into `GATES`, and the rehearsal **evaluates** it.

That last part is not cosmetic. Seeding through a shut gate would have copied
the seven crowd-source records without any of the three host tools they need —
a lane switched on with no tool, which is exactly the half-installed state
install.sh's all-or-nothing rule exists to prevent, and the rehearsal would
have produced a tick table this machine will never produce. On this machine the
market-digest gate is open and the company-wiki and crowd-tools gates are shut:
10 seeds gated out.

The repo-existence guard is now asserted for *every* seed, gated or not: a gate
decides whether the owner's machine wants a lane, and does not excuse a seed
pointing at a file nobody committed.

### 6.5 `extraction_backlog_schema.sql` — the one genuinely missing owner

Of the sixteen schemas named as suspect, fifteen already had a `MigrationSpec`.
Only P10x's `extraction_backlog_schema.sql` was new. It is the first Core
authority constructed on `store.connection` rather than on `store`, so it
needed a construction branch as well as a spec; `_CONNECTION_AUTHORITIES` names
it rather than sniffing the signature, so a class that later grows a store
argument fails loudly instead of being handed the wrong object.

### 6.6 New plist arguments, and what `launched` does not mean

The catalyst-calendar and Guidepoint lanes are now installed by the seeds, so
the writer plist gains six more arguments beyond the three in §3.5:
`--guidepoint-search-governance`, `--guidepoint-discovery-plan`,
`--guidepoint-mcp-endpoint`, `--catalyst-calendar-governance` and the
market-price pair.

`mission_catalyst_calendar` therefore reads **`launched`** rather than
`unconfigured` — and its child failed closed:

```
"failure_reason": "CatalystCalendarRunError: yfinance calendar governance record is not approved"
"status": "failed"
```

No network call was made; a scan of every `*-runs` summary in the temp root
found no recorded fetch. This is the right behaviour and it is worth stating
plainly, because the tick summary cannot distinguish it: **`launched` means a
child started, not that it did anything.** A lane whose record is still
`proposed` looks identical to one that worked until you read the next tick's
`settled`. The runbook's step 11 now says so.

### 6.7 Tests

`tests/test_rehearse_deploy.py` grew from 47 to 58: the gate predicates, the
narrowing record's destination, the "every committed record has a seed path"
invariant, and the every-seed-source-exists check.

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest tests.test_rehearse_deploy
..........................................................
----------------------------------------------------------------------
Ran 58 tests in 0.098s

OK
```

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
Ran 4108 tests in 600.583s

OK (skipped=1)
```
