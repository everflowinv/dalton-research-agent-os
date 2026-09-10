# INT3 — seeds, switches and the schema set — v1.0 — 2026-09-09

Branch `int3-seeds`, worktree `~/Projects/dalton-int3-seeds-worktree`, base main
`0fdbfab`, merged with main `9813b44` before this was written — the rehearsal
author's own fix (`3f21dbe`) and W3's revision loop landed first, and §1.1 and
§7 say what that changed. Nothing under `~/Library/Application Support/Dalton/`
was written: the one file recovered from there was read, and the rehearsal
opens the live root through `sqlite3.connect("...?mode=ro")` and `read_bytes`
only. No launchd agent was started or stopped, no live broker socket opened, no
network call and no model call made.

This closes the four findings the deploy rehearsal left open
(`docs/reports/ops-deploy-rehearsal-v1.0-2026-09-09.md` §3.3, §3.4, §5.1 and
§3.1's last paragraph). INT1 and INT2 had already landed most of the seed
blocks; what was left was the part a comment cannot do.

---

## 1. What was actually still missing

The rehearsal counted nineteen unseeded records against main `ebd2ea8`. INT1
and INT2 seeded fifteen of them before this branch started. So the interesting
number was not fifteen — it was **zero enforcement**. `install.sh` said in a
comment which absences were deliberate; the other absences read identically
from outside, and that is exactly how `yfinance-calendar-v1.json` sat committed
for a day with C1's lane one block away and reporting `unconfigured` for ever.

A comment is not a check. The rule is now:

> every file in `deploy/connector-governance/` is either copied into the
> runtime governance directory by its lane's all-or-nothing block, or named in
> `DELIBERATELY_UNSEEDED` in `install.sh` with the reason beside it.

```
committed 35  =  seeded 32  +  deliberately unseeded 3      (disjoint)
```

`tests/test_service.py::DeliberatelyUnseededTests` asserts both directions and
that the two sets do not overlap, so a record added to the repo and forgotten
in the script fails the suite instead of becoming a lane nobody switched on.

### 1.1 Reading the seeds out of the script rather than transcribing them

The check needs to know what `install.sh` seeds. The old answer — match the
connector's name against the script's non-comment text — is wrong in a way that
matters: six blocks are written as

```sh
for cn_hk_kind in financial-statements shareholders buybacks ... ; do
  cn_hk_file="$governance_dir/cn-hk-findata-${cn_hk_kind}-v1.json"
```

so the literal file name appears nowhere in the file. All six `cn-hk-findata-*`
records read as *unseeded* while the script was seeding them.

**The rehearsal author found this independently and landed it first** (main
`3f21dbe`), as `_expanded_loops` in `tests/test_rehearse_deploy.py`. This
branch had the same expansion in `scripts/rehearse_deploy.py`, one layer
further on: it resolves the `cp` *pair* — source and destination, both through
their shell variables — so a record counts as seeded only when it lands in
`$governance_dir`. That is what makes the Guidepoint narrowing note, copied
into `governance-decisions/`, correctly read as *not* seeded, and it is what
lets `INSTALL_SEEDS` be compared to the script as an exact set rather than
name by name.

Merged to **one** implementation: `expand_for_loops` and `install_script_code`
live in `rehearse_deploy`, and the test module's `_install_script_code` calls
them. Two expanders that could disagree would be precisely the drift this is
about.

### 1.2 The three deliberate absences

| record | why |
| --- | --- |
| `roic-list-transcripts-v1.json`, `roic-get-transcript-v1.json` | roic.ai answers **403 on every page, site-wide, since 2026-08-29** — confirmed here on NVDA and AAPL, and independently by the OpenClaw source survey, which reaches it the same way (bare HTTPS, browser UA, no cookie, no key, no proxy). Nothing in the writer loads either record. Seeding them buys no lane and costs the owner two approvals to make about a source that answers nothing. Both stay committed: when roic is reachable, the change is to seed them. |
| `guidepoint-get-transcript-narrowing-v1.json` | A note, not an approval. It describes an operation the upstream does not have, nothing loads it, and a permanently-proposed record in the runtime directory is an approval to make about nothing. It *is* seeded — into `governance-decisions/`, where the owner reads it and no lane looks. INT2's reasoning, now written down where a test can hold it. |

These are the only two of the rehearsal's suggested reasons that apply. There
is no Reddit governance record in this repository to leave out (the survey's
"Reddit is dead upstream" is about an OpenClaw skill template), and the four S1
feed records are not deliberate absences any more — INT2 seeds them, gated on
the workspace being present.

## 2. `sec-filings-index-v1.json` — recovered, and checked rather than trusted

It existed on the live Core and in no repository, while
`macos_launchagent.render` names it **unconditionally**. A Core rebuilt from
scratch got a writer with `--sec-filings-governance` pointing at a path with no
file behind it.

What makes it safe to commit a file copied off one machine is that it is not a
copy of a machine. Before committing, the record was rebuilt from the packaged
SEC template through `sec_filings_index.build_filings_index_governance_record`,
using the live record's own `approved_by`, `status`, `effective_from` and
`max_lease_seconds`:

```
built hash  : 1f8acea7e524ec1a0b18131c0a354f8f7084823a9bb74f00a361a74aba89c02c
live  hash  : 1f8acea7e524ec1a0b18131c0a354f8f7084823a9bb74f00a361a74aba89c02c
equal record: True
live self-consistent: True
```

Byte-identical, and the file on disk is exactly `canonical_json(record) + "\n"`
— the same serialisation every other committed record uses. Four tests pin
this (`FilingsIndexRecoveryTests`), including the re-derivation, so a change to
the packaged SEC contract that moves the schema hash fails here rather than
failing the lane closed on the live Core.

**It is committed and seeded `approved`.** That is not the usual rule, and it
is the same exception `alphaengine-get-document-v1.json` and
`sec-company-facts-v1.json` already carry: the approval is the owner's own,
given on 2026-08-26, and it predates the repository carrying the record.
Seeding is copy-once, so re-proposing it here would not ask the owner for an
approval — it would take one away from every Core rebuilt after this. The
record and the filings lane's discovery plan are now one all-or-nothing block.

## 3. The four lane switches nothing wrote

INT2 landed `tracking-policy.json`. The other three are model configurations,
and the reason they had no block was real: which two model families judge and
verify is the owner's decision, and a judge verified by its own model is not
verified.

That is an argument for a *gate*, not for an absence — the planner and the
deliverable have had exactly this shape since P13k. So:

```sh
DALTON_CLAIM_INDEX_MODEL_TIER=cheap        # P12b
DALTON_EVENT_JUDGEMENT_MODEL_TIER=brain    # P14a, both or neither
DALTON_EVENT_VERIFIER_MODEL_TIER=verifier
```

Set, and `install.sh` writes the configuration and the writer's plist carries
the lane. Unset, nothing is written and the lane is absent, which costs
nothing. Setting one of the judgement pair without the other **exits 2 and
installs neither**; setting both to the same value exits 2 too, because a
judgement whose verifier shares the producer's family comes back
`gated:same_family` at the route and every judgement would take that refusal.

No new setup module: `research_planner_setup.install` already takes
`policy_id` and `config_file_name` as arguments — that is why
`deliverable_model_setup` is nine lines — so the three blocks reuse it. Their
policy ids are `model-routing-policy:dalton-openclaw-{event-judgement,
event-verifier,claim-index}`.

The blocks sit *after* the model catalog sync, because pinning a profile the
router has never seen is refused, and *before* the plists are rendered, because
a lane whose configuration is not yet on disk gets no argument.

## 4. Bootstrap applies every schema

`dalton-bootstrap` opened five authorities. The other fifty schemas ran
the first time the writer or a lane constructed their authority — on a live
Core, several minutes *after* `install.sh` had exited zero. A deploy that broke
a schema did not fail the install; it failed one lane on one tick, as
`unavailable:OperationalError` in a heartbeat nobody was reading.

`bootstrap.apply_packaged_schemas` now iterates the packaged `*_schema.sql` set
and applies each one with the loader every authority uses —
`connection.executescript(path.read_text())` — rather than importing fifty
classes, so it cannot drift from what the authorities run.

It is idempotent and safe on a live Core: every `CREATE` in every packaged
schema is `IF NOT EXISTS` and the only two writes are `INSERT OR IGNORE`, so
applying one to a populated database is a no-op. `ALTER`-and-rebuild migrations
still belong to their authority's constructor; this is about the schema
existing and being valid SQL.

Two details worth naming:

- **A sidecar the Core does not have is applied into a scratch database and
  thrown away.** The SQL still runs and a broken schema still fails, but the
  install creates no `document-index.sqlite` that the deploy would never have
  created and an operator would later have to explain. On a fresh root that is
  11 of the 53.
- **A packaged schema with no entry in `SCHEMA_DATABASES` raises.** A new
  `*_schema.sql` that nobody mapped would otherwise be skipped in silence,
  which is the exact failure mode this replaces.

```
$ dalton-bootstrap ... (fresh root)
"schemas_applied": "55", "schemas_applied_to_scratch": "11"
second run: identical
```

Three schemas had no owner anywhere when this started. `extraction_backlog_
schema.sql` (W2) is the rehearsal author's fix, kept as merged —
`_CONNECTION_AUTHORITIES` names `DocumentProvenanceStore` rather than sniffing
the signature, so a class that later grows a store argument fails loudly. W3's
`thesis_revision_schema.sql` and `deliverable_reopen_schema.sql` arrived with
main `9813b44` and are added here, to both the rehearsal's migration list and
the bootstrap table — the second of which is the point: the table *raises* on
an unmapped schema, so those two could not have reached a deploy unapplied
once this change is in.

## 5. The rehearsal, on a fresh copy of the live Core

Re-run after the merge, against merged main.

```
scripts/rehearse_deploy.py --temp-root /tmp/dalton-int3-rehearsal-3
```

```
ok     1.8s  copy live state (read-only) -- 23 copied, 0 absent, 810 MB
ok     0.0s  rewrite service.json onto the temp root
ok     1.2s  dalton-bootstrap -- core=core.sqlite tokens=writer-tokens.json
ok     6.8s  migrations (every *_schema.sql) -- 55/55 schemas applied
ok     0.0s  governance seeds -- 16 seeded, 15 already present, 10 gated out, 0 absent from repo
ok     0.2s  model catalog sync (copy of model-router.sqlite)
ok     0.0s  render LaunchAgent plists and diff -- 3 plists rendered
ok     0.0s  mission grants (autonomy.may_write) -- may_write grants 11, missing 11
ok     0.0s  lane switches on disk -- 4/7 lane switches on disk
ok     0.3s  start writer against the temp Core -- writer pid 69534, socket bound
ok     4.6s  one controller tick -- 27 entries, 0 escaped, 4.6s (service tick_seconds=5.0)
```

The two shut gates are named rather than skipped in silence:

```
gate shut, 2 seed(s) not installed -- company-wiki: no wiki index at
  ~/.openclaw/workspace/wiki-index.sqlite
gate shut, 8 seed(s) not installed -- crowd-tools: not set to an executable:
  DALTON_AGENT_REACH_TOOL, DALTON_XUEQIU_HOT_RANK_TOOL, DALTON_XREACH_TOOL
deliberately not seeded, with a reason in install.sh:
  guidepoint-get-transcript-narrowing-v1.json, roic-get-transcript-v1.json,
  roic-list-transcripts-v1.json
```

### 5.1 The tick table, verbatim

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
mission_tracking           launched
mission_catalyst_calendar  launched
company_model_spec         idle          every company has a current specification
company_model_forecast     launched
research_plan              launched
claim_index                unconfigured  no claim index lane on this writer
initial_screen             launched
event_judgement            unconfigured  no judgement lane on this writer
mission_reopen             ungranted     任务 coverage-mission-version:us-it-services:13 的 autonomy.human_checkpoints 里没有
sales_notes_feed           unconfigured  no source:sales-notes lane on this writer
company_wiki_feed          unconfigured  no source:company-wiki lane on this writer
debate_map                 launched
company_dossier            unconfigured  no company-dossier lane on this writer
mission_crowd_sources      unconfigured  no crowd-source lane on this writer
research_task              unconfigured  no research task lane on this writer
mission_reflection         launched
mission_sec_dispatch       idle          settled=0
forecast_reconciliation    idle
tick_ledger                recorded      lane_count=24
```

```
ok     4.6s  one controller tick -- 27 entries, 0 escaped, 4.6s (service tick_seconds=5.0)
```

**Zero lanes escaped.** Twenty-seven entries rather than twenty-six: W3's
`mission_reopen` arrived with the merge and reports `ungranted` for a reason an
operator can act on.

Two lanes moved because of this branch, and both are worth reading carefully,
because `launched` means a child *started*:

- `mission_tracking`: `unconfigured` -> `launched`. The child ran and reported
  `tracking_status: ungranted, the mission does not grant market_event,
  cost_micros 0` — the correct answer until runbook step 7.
- `mission_catalyst_calendar`: `unconfigured` -> `launched`. The child ran and
  **refused before any network call**:
  `CatalystCalendarRunError: yfinance calendar governance record is not
  approved`. That is the seed doing exactly what it should: the record is
  seeded `proposed`, the file's presence installs the lane, and the approval —
  not the file — is what makes it fetch. Runbook step 6 approves it.

Neither child made a network call, a model call, or spent anything. The four
fetching lanes (`mission_source_discovery`, `mission_sec_quarters`,
`mission_statements`, `mission_sec_dispatch`) were all `idle`, as before.

`4/7 lane switches on disk` rather than `3/7`: the tracking policy is seeded.
The three absent ones are the model configurations, and the rehearsal names the
environment variable that would write each.

### 5.2 What did not change

The plist diff, the mission-grant list, the verifier phase pin and the
pre-migration admission count are all as the runbook already describes them.
The plist diff grew by the Guidepoint, tracking-policy and calendar arguments,
which is the seeds' whole point: `argv_fragment` turns a lane on only when its
file is already on disk, so those arguments appearing *is* the evidence that
`install.sh` seeds before it renders.

## 6. Runbook changes

`docs/reports/deploy-runbook-v1.0-2026-09-09.md`:

- **step 2** — the governance backup is no longer the only copy of
  `sec-filings-index-v1.json`, but is still the only record of which approvals
  this machine has given.
- **step 3** — the three optional environment variables, what each installs,
  and why they must be set on *this* command rather than afterwards.
- **step 6** — approve `yfinance-calendar-v1.json` alongside the price record;
  what its lane does before the approval; the three deliberate absences.
- **step 10** — was "install the lane switches by hand"; is now a verification
  and a pointer back to step 3.
- **step 11** — the three lanes whose status should differ from the previous
  rehearsal's table.

## 7. Tests

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest tests.test_rehearse_deploy tests.test_service
......
----------------------------------------------------------------------
Ran 111 tests in 3.804s

OK
```

Full suite:

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
Ran 4220 tests in 641.648s

OK (skipped=1)
```

(main `9813b44` is 4,195; +25 — eight in `test_rehearse_deploy`, seventeen in
`test_service`. The broker-client `FUTURE` flake is fixed on main and did not
recur.)

Two tests were *replaced* rather than added, and both were asserting something
that is no longer true:

- `test_a_live_record_with_no_committed_source_is_named` used
  `sec-filings-index-v1.json` as its example orphan. It is committed now, so
  the example is a name no repository carries, and a second test asserts the
  filings record is no longer an orphan *and* is seeded — either alone is worth
  nothing.
- `test_every_governance_record_the_script_seeds_is_in_the_list` matched names;
  it now compares the two sets exactly, resolved through the `cp` pair.

The ones that earn their keep:

- `DeliberatelyUnseededTests` (in `test_service.py`) and
  `test_the_two_sets_cover_the_repo_exactly_and_do_not_overlap` — committed ==
  seeded ∪ deliberately-unseeded, disjoint, every named record exists, and
  every entry in the array has a reason above it.
- `test_a_loop_written_seed_is_read_as_a_seed` and
  `test_a_comment_naming_a_record_is_not_a_seed` — the two ways this reading of
  `install.sh` could go quietly wrong: a name that is only in a loop template,
  and a name that is only in a comment.
- `FilingsIndexRecoveryTests.test_it_re_derives_from_the_packaged_contract` —
  the record is the packaged contract's, not one machine's.
- `BootstrapSchemaTests.test_a_broken_schema_fails_here` and
  `test_a_schema_with_no_declared_database_is_refused` — the two ways a schema
  can reach a deploy without being applied. The second is not hypothetical: W3
  landed two schemas with no owner while this branch was open.
- `BootstrapSchemaTests.test_it_creates_no_database_the_deploy_would_not` — the
  scratch-database property, which is what keeps this additive.

## 8. Open

1. **The seed list is still a copy.** `install.sh` writes each block,
   `LaneSpec.argv_fragment` decides whether the lane turns on, and
   `INSTALL_SEEDS` carries a third list. This branch made the *governance
   record* half of that checkable by reading the shell script; the plan and
   policy files are still transcribed, and both `tracking-policy.json` and the
   probe-template manifest were missing from `INSTALL_SEEDS` until this merge
   noticed. Wave 0's open question 3 — a `governance_seed` field on `LaneSpec`
   — would collapse the rest.
2. **`DELIBERATELY_UNSEEDED` is not read at runtime.** It is a shell array
   nothing sources, kept honest by a test. That is the cheapest thing that
   works and it is not the same as the installer refusing to run with an
   unaccounted record; if seeding ever moves out of the shell script, this
   should move with it.
3. **roic is a decision that should be revisited, not forgotten.** The records
   are committed and the block to seed them is two lines. Someone should check
   whether Firecrawl — the survey's one transport that passes some 403s —
   changes the answer; that would be a new transport, so a new profile version
   and a new approval.
4. **The rehearsal still launches real lane children.** Two more than before,
   and both refused for the right reason with nothing spent — but that is a
   property of the current mission grants and the current approvals, not a
   guarantee. A `--no-children` mode remains the way to make it one.
