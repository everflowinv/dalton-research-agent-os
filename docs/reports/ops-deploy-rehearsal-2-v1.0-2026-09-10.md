# OPS — deploy rehearsal #2 against a copy of the live Core — v1.0 — 2026-09-10

Branch `ops-rehearsal-2`, worktree `~/Projects/dalton-ops-rehearsal-2-worktree`,
base main `8717de0`. Supersedes the measurements in
`docs/reports/ops-deploy-rehearsal-v1.0-2026-09-09.md`, which was taken on
`ebd2ea8` — about twenty slices ago. The owner-facing output of this run is
`docs/reports/owner-steps-after-deploy-v2.0-2026-09-10.md` (supersedes v1.0);
the runbook `docs/reports/deploy-runbook-v1.0-2026-09-09.md` is updated in
place.

**Every number in this report was measured against main `8717de0`.** The two
branches that were still in flight when this run started — `prior-research` and
`model-selection` — are *not* in it. `model-selection` has since merged, and it
brings a lane, a schema and install.sh seeds, so three counts here are now
lower bounds rather than the current truth: 61 schemas (§2), 48 seeds (§3) and
31 lanes (§1). Nothing else in the report depends on those counts — the
confinement fixes, the re-hash result, the mission grants and the owner steps
all hold. The hardened harness is what to re-run against current main; §9 is
what changed in it, and it now takes `--source-root`.

Live state was read through `sqlite3.connect("...?mode=ro")` plus the backup
API into `/tmp/dalton-rehearsal2-20260910T073000Z/live`, and the rehearsal ran
against that copy. No launchd agent was started or stopped. No live broker
socket was opened. No network call and no model call was made.

**Except once, and §0 is about that.**

---

## 0. What went wrong on the first attempt

Read this before the good news, because the good news was produced by a fixed
harness and the first run of the unfixed one wrote to the live Core.

`--live-root` was pointed at the read-only copy under `/tmp`, as the brief
says. Two things then happened that the harness was not built for:

1. The copy had been `chmod -R a-w`, and a `mode=ro` SQLite open of a **WAL**
   database still needs to create the `-shm` sidecar. Step 1 (copy) failed on
   the first database.
2. `shutil.copy2` had already carried the read-only mode bits onto the temp
   `service.json`, so step 2 (rewrite the config onto the temp root) failed
   with `PermissionError`.

Neither failure stopped the run. `Rehearsal.run()` recorded each failed step
and carried on, and step eleven built a `BoundedPlannerDriver` from a
`service.json` that had **never been rewritten** and therefore still named
`~/Library/Application Support/Dalton/...`. The tick ran against the live Core.

What was and was not damaged, measured rather than assumed:

- Every writing lane came back `unavailable:RemoteAuthorizationError`. The live
  writer refused the rehearsal's token, so **nothing was published through the
  writer**.
- Read-only lanes returned `idle` / `held`.
- The one real write was the **C2 tick ledger**, which the driver opens
  directly rather than through the writer. It created
  `state/dalton-core/tick-ledger.sqlite` — a file the deployed service does not
  write, because the deployed build predates C2 — containing exactly one row,
  `tick:0dba6b24e9872cecc37b8ac1efeeb689` at `2026-09-10T07:01:36Z`.
- `thesis-impact-budget.sqlite-shm` / `-wal` appeared as read-side artefacts of
  the `mode=ro` open. The `-wal` is 0 bytes and the database's mtime never
  moved, so there is no unreplayed data.
- A directory listing taken before the run diffs clean against one taken after,
  apart from those three names.

The ledger file was quarantined (moved, not deleted) to
`/tmp/dalton-quarantine-20260910/tick-ledger.sqlite.rehearsal-stray`. **The
owner checklist carries a pre-install check that no `tick-ledger.sqlite` exists
under the live root**, so that a rehearsal tick cannot become the first row of
the real ledger.

A rehearsal is allowed to fail. It is not allowed to fail *onto the thing it is
rehearsing against*. §9 is the four changes that make this class of accident
impossible rather than unlikely.

---

## 1. The tick table, verbatim

One `BoundedPlannerDriver.run_once` against the temp Core, writer started from
the rendered writer plist, broker stubbed with a local UNIX socket that answers
one refusal.

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
mission_ownership          ungranted     this mission does not grant market_event in autonomy.may_write; reading owners
mission_consensus          ungranted     this mission does not grant consensus_estimate in autonomy.may_write; publishi
company_model_spec         idle          every company has a current specification
company_model_forecast     launched
forecast_sensitivity       idle          every company's sensitivity table matches its model
research_plan              launched
claim_index                unconfigured  no claim index lane on this writer
initial_screen             launched
event_judgement            unconfigured  no judgement lane on this writer
earnings_season            unconfigured  no earnings-season lane on this writer
mission_reopen             ungranted     任务 coverage-mission-version:us-it-services:13 的 autonomy.human_checkpoints 里没有
sales_notes_feed           unconfigured  no source:sales-notes lane on this writer
company_wiki_feed          unconfigured  no source:company-wiki lane on this writer
debate_map                 launched
company_dossier            unconfigured  no company-dossier lane on this writer
deep_insight_gate          unconfigured  no deep-insight-gate lane on this writer
industry_framework         launched
mission_crowd_sources      unconfigured  no crowd-source lane on this writer
conviction_call            idle          no covered company can be shown to differ from the market today
research_task              unconfigured  no research task lane on this writer
mission_reflection         launched
mission_sec_dispatch       idle          settled=0
forecast_reconciliation    idle
tick_ledger                recorded      lane_count=31
```

```
ok     2.1s  one controller tick -- 34 entries, 0 escaped, 2.1s (service tick_seconds=5.0)
```

**Zero lanes escaped.** Not one `unavailable:<Exception>`, not one
`unrecorded:<Exception>`. Thirty-one registered lanes (v1 measured
twenty-three), plus `mission_sec_dispatch` and `forecast_reconciliation`, which
the driver calls outside the lane loop, plus the tick ledger's own row.

Every lane that could not run looked at its own preconditions and declined in a
word an operator can act on:

| word | means | count | the owner action that changes it |
| --- | --- | --- | --- |
| `launched` | a child started (**not** that it succeeded) | 7 | read the *next* tick's `settled` |
| `idle` | nothing to do; preconditions met | 10 | none |
| `ungranted` | the mission does not carry a `may_write` word or a checkpoint | 4 | §6 — publish one mission version |
| `unconfigured` | a file the lane's switch reads is not on disk | 8 | §5, §7 — an env var at install time or a file to author |
| `held` | authorised and deliberately quiet | 0 this tick | — |
| `recorded` | the tick wrote its own ledger row | 1 | none |

`tick_ledger: recorded, lane_count=31` on the first tick after the migration is
C2's bookkeeping doing its job. The ledger write is the last thing a tick does
and is allowed to fail, so a silent `unrecorded:` would have been easy to miss.

### 1.1 `load_lanes()` order

`lane_registry.LANE_MODULES` — 27 modules, imported in this order, registering
31 lanes between them:

```
dalton_core.writer_lanes                  dalton_core.mission_crowd_source_lane
dalton_core.mission_sec_quarters          dalton_core.mission_feed_lane
dalton_core.mission_statement_lane        dalton_core.mission_research_task_lane
dalton_core.mission_market_price_lane     dalton_core.mission_event_judgement_lane
dalton_core.mission_tracking_lane         dalton_core.mission_earnings_season_lane
dalton_core.mission_catalyst_lane         dalton_core.mission_reopen_lane
dalton_core.mission_consensus_lane        dalton_core.mission_reflection_lane
dalton_core.mission_ownership_lane        dalton_core.mission_debate_map_lane
dalton_core.mission_model_spec_lane       dalton_core.mission_dossier_lane
dalton_core.mission_guidepoint_lane       dalton_core.mission_deep_insight_lane
dalton_core.mission_model_forecast_lane   dalton_core.mission_industry_framework_lane
dalton_core.mission_sensitivity_lane      dalton_core.mission_conviction_lane
dalton_core.mission_claim_index_lane
dalton_core.research_planner_launcher
dalton_core.initial_screen_launcher
```

Import order is not tick order. The tick order is the `LaneSpec.order` field,
which is the table in §1; the gaps in it (30, 35, 40 … 85, 86, 87, 88, 89 …
116, 117, 118 … 135, 137, 138, 139 … 141) are the deliberate insertion points
each slice claimed.

---

## 2. Migrations — 61 of 61

```
ok     4.9s  migrations (every *_schema.sql) -- 61/61 schemas applied
```

48 core authorities on `core.sqlite` plus 13 sidecar stores. Every `*.sql`
shipped in `dalton_core` has a named owner in
`scripts/rehearse_deploy.CORE_MIGRATIONS` / `SIDECAR_MIGRATIONS`, and
`MigrationCoverageTests` fails if one does not — a schema with no entry would
let this report say "clean" without having run that migration. v1 measured 52
schemas; nine have landed since.

`dalton-bootstrap` still opens only a handful of these. The rest run the first
time the writer or a lane constructs their authority, which on a live Core is
minutes *after* `install.sh` has exited zero. That is why the rehearsal runs
them all up front.

Two findings from the migration step, both expected:

- **C2's pool columns.** `5042 / 5046` admissions on the copy have
  `pool IS NULL` and replay as `unpooled` in `pool_status` until they settle.
  On deploy day the pool figures will not add up to the day cap. It drains.
  (v1 measured 5,036 / 5,037 — the ratio has not moved, the ledger has grown.)
- **The `mission_deliverable` CHECK.** See §8.

---

## 3. Seeds

```
ok     0.0s  governance seeds -- 23 seeded, 15 already present, 10 gated out, 0 absent from repo
```

`INSTALL_SEEDS` now carries **48** seed-once copies, 13 of them gated. `0
absent from repo` is the check that matters: every seed source is a file the
repository actually ships, so no lane is unreachable no matter what the owner
sets.

### 3.1 The gate table, as evaluated on this machine today

| gate | condition install.sh checks | open here | seeds behind it |
| --- | --- | --- | --- |
| `market-digest` | `$DALTON_OPENCLAW_WORKSPACE/skills/market-digest/output` is a directory | **open** | `sales-notes-list-notes-v1.json`, `sales-notes-get-note-v1.json` |
| `company-wiki` | `$DALTON_OPENCLAW_WORKSPACE/wiki-index.sqlite` exists | **shut** | `company-wiki-list-documents-v1.json`, `company-wiki-get-document-v1.json` |
| `any-feed` | either feed lane installed (union of the two above) | **open** | `feed-plans/p9-us-it-services-feeds-v1.json` |
| `crowd-tools` | `DALTON_AGENT_REACH_TOOL`, `DALTON_XUEQIU_HOT_RANK_TOOL`, `DALTON_XREACH_TOOL` all name executables | **shut** | 7 crowd records + `phase9/p9-us-it-services-crowd-sources-v1.json` |

Verbatim:

```
gate shut, 2 seed(s) not installed -- company-wiki: no wiki index at
  /Users/everflow/.openclaw/workspace/wiki-index.sqlite
  (company-wiki-get-document-v1.json, company-wiki-list-documents-v1.json)
gate shut, 8 seed(s) not installed -- crowd-tools: not set to an executable:
  DALTON_AGENT_REACH_TOOL, DALTON_XUEQIU_HOT_RANK_TOOL, DALTON_XREACH_TOOL
  (employee-reviews-blind-v1.json, p9-us-it-services-crowd-sources-v1.json,
   x-xreach-search-v1.json, x-xreach-thread-v1.json,
   x-xreach-user-timeline-v1.json, xueqiu-get-post-v1.json,
   xueqiu-hot-rank-v1.json, xueqiu-search-posts-v1.json)
```

A gated seed that does not land is `install.sh` working as designed. The
all-or-nothing rule exists because a crowd-source lane switched on with no host
tool refuses every run with "no tool configured", which is a worse state than
not being installed.

### 3.2 Deliberately unseeded

```
deliberately not seeded, with a reason in install.sh:
  guidepoint-get-transcript-narrowing-v1.json,
  roic-get-transcript-v1.json,
  roic-list-transcripts-v1.json
```

Unchanged from INT3, and still held by the test that asserts every committed
record is either seeded or named in `DELIBERATELY_UNSEEDED` with a reason.
`unseeded_governance_records()` returned empty — no record is silently
switching nothing on.

---

## 4. Model catalog sync

```
ok     0.1s  model catalog sync (copy of model-router.sqlite)
catalog registered: profile:claude-fable-5-1, profile:gemini-3-8-flash,
  profile:qwen-deepseek-v4-flash-0731-low-calibration, profile:zai-glm-5-3,
  profile:zai-glm-5-3-flash
catalog retired: profile:gemini-3-7-flash, profile:gemini-flash-latest,
  profile:glm-5-2, profile:gpt-5-5, profile:openrouter-ox-alpha,
  profile:qwen-deepseek-v4-pro
```

Five registered, six retired — the numbers the v1 runbook predicted, now
actually *measured*, because §9.4 fixed a check that had never been able to
report anything. A second run against the same router returns
`added_profile_ids: []`, `retired_profile_ids_this_run: []`,
`revived_profile_ids: []`, `changed: false`, `catalog_in_sync: true`: the sync
is idempotent and `install.sh` converges.

`profile:zai-glm-5-3-flash` — the cheap chain's middle link — **is** in
`~/.openclaw/openclaw.json` and is registered by this sync. What the sync
cannot tell you is whether the *running* gateway has re-read its plugin config.
The gateway reload stays in the runbook as a step, not as a question.

The one finding, unchanged and still the owner's decision:

```
verifier phase pin model-routing-policy-version:dalton-openclaw-verifier:1 names
profile:gemini-3-7-flash, which this sync retired; verifier routing will be
refused with profile_retired until the owner repoints the pin at the verifier
tier chain
```

---

## 5. Plist diff against the installed LaunchAgents

Three plists rendered (`writer`, `controller`, `control`; `thesis-impact` is
disabled in the live `service.json` and neither side has it). After mapping the
venv, the log directory and the root back to their live forms, the controller
and control plists are **byte-identical** to the installed ones. Every
difference is in the writer's `ProgramArguments`, and every one of them is an
addition — this deploy removes no argument:

```
+--guidepoint-search-governance   .../connector-governance/guidepoint-search-library-v1.json
+--guidepoint-discovery-plan      .../discovery-plans/us-it-services-guidepoint-v1.json
+--guidepoint-mcp-endpoint        http://127.0.0.1:8943/mcp
+--market-price-governance        .../connector-governance/yfinance-daily-prices-v1.json
+--tracking-policy                .../tracking-policy.json
+--catalyst-calendar-governance   .../connector-governance/yfinance-calendar-v1.json
+--sec-ownership-governance-dir   .../connector-governance
+--consensus-governance           .../connector-governance/yfinance-analyst-estimates-v1.json
+--industry-framework-policy      .../p12e-industry-framework-policy-v1.json
+--model-forecast-lane            (flag only)
+--forecast-sensitivity-lane      (flag only)
+--reflection-lane                (flag only)
+--debate-map-model-config        .../initial-screen-model-config.json
+--industry-framework-model-config .../initial-screen-model-config.json
+--conviction-call-model-config   .../initial-screen-model-config.json
```

Two things worth stating plainly:

**Three new lanes share one model configuration.** `debate_map`,
`industry_framework` and `conviction_call` all point at
`initial-screen-model-config.json` — the same routing chain, the same
day-ledger name, the same broker. `_argv_diff` reports them as flags with no
value because the path is already in the live argv under
`--initial-screen-model-config`, which is a limitation of an argument-level
diff, not a missing value; the rendered plist carries the path four times. This
matters for P12c: the debate map's verifier must reach a *different* model
family or the lane stops for ever at `not_independent`, and it cannot do that
from a chain that has only one.

**The dossier lane is absent from the plist entirely.** No
`--company-dossier-model-config`, because the lane's `argv_fragment` emits
nothing until both its policy and its verifier configuration are on disk, and
neither is. That is why `company_dossier` reads `unconfigured` in §1 and why
`deep_insight_gate` reads `unconfigured` behind it.

`_check_plist_referenced_seeds` found no argument pointing at a path that does
not exist.

---

## 6. Mission grants

```
ok     0.0s  mission grants (autonomy.may_write) -- may_write grants 11, missing 11;
                                                    checkpoints 7, missing 3
```

Live `coverage-mission-version:us-it-services:13` grants eleven words:

```
evidence, claim, forecast_line, model_run, research_question, observation,
stage_record, forecast_reconciliation, source_discovery, claim_challenge,
deliverable
```

and eleven are missing — one per merged lane that writes something:

| missing word | who needs it | what it costs to omit |
| --- | --- | --- |
| `market_price` | P11a | price lane `ungranted`; no valuation, no dossier price history |
| `market_event` | P14a | tracking lane writes no event; judgement and reflection are empty behind it |
| `consensus_estimate` | P11b | both consensus routes silent |
| `valuation` | P11a | no valuation snapshot, so no target-price bridge |
| `claim_index` | P12b | index lane `held: not_authorized`, spends nothing |
| `research_task` | P14e | ad-hoc entry answers `mission_does_not_grant_research_task` |
| `dossier` | P12a | dossier lane `held: not_authorized` before any model call |
| `debate_map` | P12c | debate lane `held: not_authorized` |
| `forecast_revision_proposal` | P14a | an event can never propose a forecast change |
| `thesis_revision_candidate` | P14a / ADR-0007 | `revise_thesis` records `queued` |
| `conviction_call` | P15d | conviction lane holds |

Checkpoints. Live carries seven — `deep_insight_gate`, `investment_memo`,
`thesis_admission`, `thesis_revision`, `forecast_overturn`, `scope_expansion`,
`budget_expansion`. **Three are missing**, and this rehearsal is the first to
say so: v1's check named `thesis_revision_candidate` in a single hard-coded
`if`, written when it was the only one.

| missing checkpoint | lane | measured consequence |
| --- | --- | --- |
| `thesis_revision_candidate` | P14a / thesis revision | `revise_thesis` queues and names ADR-0007 |
| `gate_reopen` | P14d reopen lane | `mission_reopen: ungranted` in §1 — that row is this |
| `conviction_call` | P15d | a call nobody agreed to decide is not made |

`deep_insight_gate` is already there; the Playbook makes it mandatory.

---

## 7. Lane switches on disk

```
ok     0.0s  lane switches on disk -- 5/8 lane switches on disk
```

Five are present after the install steps: `tracking-policy.json` and
`p12e-industry-framework-policy-v1.json` (seeded unconditionally),
`document-extraction-model-config.json`, `initial-screen-model-config.json`,
`research-planner-model-config.json` (already on the live Core).

Three are absent, and all three are absent *because nobody named them*, not
because anything failed:

```
event_judgement (P14a): event-judgement-model-config.json absent
event_judgement verifier (P14a): event-verifier-model-config.json absent
claim_index (P12b): claim-index-model-config.json absent
```

Each is written by `install.sh` when the matching `DALTON_*_MODEL_TIER` (or
`_PROFILE`) is set on the install command. The judgement pair is all-or-nothing
and the two must be different model families, or every judgement returns
`gated:same_family` before anything is paid for.

`LANE_SWITCHES` does **not** yet cover P12a's `p12a-dossier-policy-v1.json` and
`dossier-verifier-model-config.json`, which are the reason `company_dossier`
and `deep_insight_gate` read `unconfigured` in §1. They are carried as owner
steps in the v2.0 checklist rather than added to the switch list here: the
switch table is what `install.sh` claims to write, and `install.sh` does not
claim to write those two.

---

## 8. Re-hash and DDL comparison against the live copy

Baseline: the untouched copy the rehearsal read from. Subject: the temp Core
after all 61 migrations *and* one tick. Rows the tick added count as additions,
not as movements.

```
core.sqlite
  tables: 180 before, 216 after; new 36; dropped 0
  DDL changed on an existing table: ['mission_deliverable_versions']
  content-hashed tables: 131; pre-migration rows compared: 87068
  rows not byte-identical after the migrations: 0
  rows added by the migrations and the one tick: 0
scheduler.sqlite            4 hashed tables, 50178 rows, 0 moved, 3 added
thesis-impact-budget.sqlite 5 hashed tables, 10035 rows, 0 moved, 0 added
dashboard-projection.sqlite 1 hashed table,   1152 rows, 0 moved, 0 added
research-coordinator.sqlite 7 hashed tables,   231 rows, 0 moved, 0 added
research-review/candidate-staging.sqlite  8 hashed tables, 7756 rows, 0 moved, 0 added
catalog.sqlite / cockpit/journal.sqlite / model-router.sqlite  no content-hashed rows

=== summary ===
rows not byte-identical across every content-hashed table: 0
tables whose DDL changed vs live: core.sqlite: mission_deliverable_versions
```

**156,420 content-hashed rows across 26 tables in six databases, and not one
byte moved.** Thirty-six tables are new — they are the new authorities' own
tables and they start empty. Nothing was dropped.

### 8.1 The one table whose DDL changed

`mission_deliverable_versions`. The migration rebuilds the table to widen one
CHECK; every other column, constraint and index is byte-identical.

```
- kind TEXT NOT NULL CHECK(kind IN (
-     'industry_framework','initial_screen','industry_model','company_model',
-     'forecast_lines','investment_memo','weekly_brief'
- )),
+ kind TEXT NOT NULL CHECK(kind IN (
+     'industry_framework','initial_screen','industry_model','company_model',
+     'forecast_lines','investment_memo','weekly_brief','event_note',
+     'deep_insight_gate','earnings_preview','earnings_calibration'
+ )),
```

Four kinds added by four slices: `event_note` (P14a), `deep_insight_gate`
(P12d), `earnings_preview` and `earnings_calibration` (P14f). The rehearsal
checks the *vocabulary* — `mission_deliverable.DELIVERABLE_KINDS` — rather than
one literal, because v1 checked only for `'event_note'` and would have passed a
Core migrated as far as P14a and no further.

A rebuild is the one migration in this deploy that rewrites a live table, so it
is worth naming what it did not do: 4,127 rows of
`mission_deliverable_versions` came through byte-identical, `content_hash`
included, and they are part of the 87,068 above.

---

## 9. What was fixed in the harness, and why

Four changes to `scripts/rehearse_deploy.py`. No lane module was touched.

**9.1 A fatal step stops the run.** `step()` takes `fatal=True`; the copy, the
rewrite and the new confinement check are fatal, and everything after a failed
one is recorded as `skip` rather than run. Non-fatal steps still fail and let
the run gather findings, which is the point of a rehearsal.

**9.2 A confinement step, separate from the rewrite.** The rewrite is what is
supposed to make the configuration temp-only, so it cannot also be the thing
that checks it. `confine_to_temp_root` reads back the written file, asserts
that every absolute path in `bounded_planner.config` — the exact mapping
`run_tick` builds the driver from — lies under the temp root, and refuses
otherwise. `run_tick` refuses to construct a driver unless that step passed,
and re-checks. Paths elsewhere in the file are reported as findings, with host
executables (`tailscale_executable`, `openclaw_executable`) allowed.

**9.3 `HOME` is pointed at an empty directory inside the temp root** for the
writer child and for the in-process tick. `Path.home()` reads `HOME` first on
POSIX, so a module reaching for `~` that the rehearsal has not audited resolves
to `<temp>/home`, where there is no Dalton root at all. The real home is
captured once at construction for the three steps that legitimately need it —
the OpenClaw catalog, the installed LaunchAgents to diff against, the seed
gates.

**9.4 `--source-root`, and two checks that could not fail.**
`--live-root` may now be a copy: `--source-root` names the root it was taken
from, and that is what drives the path rewrites and the plist normalisation. A
copied `service.json` is a byte copy — it spells the original root, not the
path it now lives at — so keying the replacements off the copy's own path
replaced nothing, which is how §0 happened. Additionally, `run_catalog_sync`
was reading `registered` / `retired` / `revived`, three keys the sync report has
never carried; every lookup returned nothing, so the step reported no catalog
movement whatever the sync did and the idempotence check compared two empty
lists. The keys are now `added_profile_ids`, `retired_profile_ids_this_run`,
`revived_profile_ids`, pinned by a test against
`openclaw_catalog_reconcile.py`. §4 is the first measurement this check has
ever produced. And `check_mission` tested one hard-coded checkpoint word; it now
tests all four, which is where §6's `gate_reopen` and `conviction_call` rows
come from.

---

## 10. Tests

New in `tests/test_rehearse_deploy.py`: `ConfinementTests` (12),
`FatalStepTests` (4), `SourceRootTests` (4), `TempHomeTests` (5),
`CatalogChangeKeyTests` (3), `CheckpointTests` (4).

The new cases, verbatim (`python -m unittest tests.test_rehearse_deploy -v`;
a few `... ok` markers wrap onto the next line in the runner's own output and
are reproduced as they came out):

```
test_the_keys_are_the_ones_the_sync_report_is_built_from (tests.test_rehearse_deploy.CatalogChangeKeyTests.test_the_keys_are_the_ones_the_sync_report_is_built_from) ... ok
test_the_retirement_key_is_the_per_run_one_not_the_cumulative_one (tests.test_rehearse_deploy.CatalogChangeKeyTests.test_the_retirement_key_is_the_per_run_one_not_the_cumulative_one)
test_the_words_the_rehearsal_prints_are_the_three_movements (tests.test_rehearse_deploy.CatalogChangeKeyTests.test_the_words_the_rehearsal_prints_are_the_three_movements) ... ok
test_a_mission_carrying_every_checkpoint_is_clean (tests.test_rehearse_deploy.CheckpointTests.test_a_mission_carrying_every_checkpoint_is_clean) ... ok
test_each_missing_checkpoint_is_named_with_its_reason (tests.test_rehearse_deploy.CheckpointTests.test_each_missing_checkpoint_is_named_with_its_reason) ... ok
test_every_required_checkpoint_is_a_word_the_vocabulary_knows (tests.test_rehearse_deploy.CheckpointTests.test_every_required_checkpoint_is_a_word_the_vocabulary_knows) ... ok
test_the_lanes_that_refuse_on_a_checkpoint_are_the_ones_listed (tests.test_rehearse_deploy.CheckpointTests.test_the_lanes_that_refuse_on_a_checkpoint_are_the_ones_listed)
test_a_config_naming_the_real_root_is_refused (tests.test_rehearse_deploy.ConfinementTests.test_a_config_naming_the_real_root_is_refused) ... ok
test_a_config_with_no_planner_block_is_refused (tests.test_rehearse_deploy.ConfinementTests.test_a_config_with_no_planner_block_is_refused) ... ok
test_a_confined_config_passes_and_sets_the_flag (tests.test_rehearse_deploy.ConfinementTests.test_a_confined_config_passes_and_sets_the_flag) ... ok
test_a_host_executable_elsewhere_is_a_finding_not_a_refusal (tests.test_rehearse_deploy.ConfinementTests.test_a_host_executable_elsewhere_is_a_finding_not_a_refusal) ... ok
test_a_path_outside_the_root_is_reported (tests.test_rehearse_deploy.ConfinementTests.test_a_path_outside_the_root_is_reported) ... ok
test_a_path_under_the_root_is_not_foreign (tests.test_rehearse_deploy.ConfinementTests.test_a_path_under_the_root_is_not_foreign) ... ok
test_a_relative_string_is_not_a_path (tests.test_rehearse_deploy.ConfinementTests.test_a_relative_string_is_not_a_path) ... ok
test_a_sibling_directory_is_not_mistaken_for_a_child (tests.test_rehearse_deploy.ConfinementTests.test_a_sibling_directory_is_not_mistaken_for_a_child)
test_nested_and_listed_paths_are_all_examined (tests.test_rehearse_deploy.ConfinementTests.test_nested_and_listed_paths_are_all_examined) ... ok
test_state_elsewhere_in_an_undriven_block_is_reported (tests.test_rehearse_deploy.ConfinementTests.test_state_elsewhere_in_an_undriven_block_is_reported) ... ok
test_the_root_itself_counts_as_inside (tests.test_rehearse_deploy.ConfinementTests.test_the_root_itself_counts_as_inside) ... ok
test_the_tick_refuses_to_build_a_driver_unconfined (tests.test_rehearse_deploy.ConfinementTests.test_the_tick_refuses_to_build_a_driver_unconfined) ... ok
test_a_failed_fatal_step_skips_everything_after_it (tests.test_rehearse_deploy.FatalStepTests.test_a_failed_fatal_step_skips_everything_after_it) ... ok
test_a_failed_ordinary_step_does_not_stop_the_run (tests.test_rehearse_deploy.FatalStepTests.test_a_failed_ordinary_step_does_not_stop_the_run) ... ok
test_a_skipped_step_is_reported_as_skipped_not_as_a_pass (tests.test_rehearse_deploy.FatalStepTests.test_a_skipped_step_is_reported_as_skipped_not_as_a_pass) ... ok
test_the_run_is_not_ok_when_a_step_was_skipped (tests.test_rehearse_deploy.FatalStepTests.test_the_run_is_not_ok_when_a_step_was_skipped) ... ok
test_a_copy_rewrites_the_original_root_onto_the_temp_root (tests.test_rehearse_deploy.SourceRootTests.test_a_copy_rewrites_the_original_root_onto_the_temp_root) ... ok
test_inverting_takes_a_rendered_plist_back_to_the_original_root (tests.test_rehearse_deploy.SourceRootTests.test_inverting_takes_a_rendered_plist_back_to_the_original_root) ... ok
test_the_copy_is_still_where_the_files_are_read_from (tests.test_rehearse_deploy.SourceRootTests.test_the_copy_is_still_where_the_files_are_read_from) ... ok
test_the_source_root_defaults_to_the_live_root (tests.test_rehearse_deploy.SourceRootTests.test_the_source_root_defaults_to_the_live_root) ... ok
test_home_is_restored_after_the_block (tests.test_rehearse_deploy.TempHomeTests.test_home_is_restored_after_the_block) ... ok
test_home_is_restored_when_the_block_raises (tests.test_rehearse_deploy.TempHomeTests.test_home_is_restored_when_the_block_raises) ... ok
test_the_real_home_is_captured_before_anything_reassigns_it (tests.test_rehearse_deploy.TempHomeTests.test_the_real_home_is_captured_before_anything_reassigns_it) ... ok
test_the_temp_home_has_no_dalton_root_under_it (tests.test_rehearse_deploy.TempHomeTests.test_the_temp_home_has_no_dalton_root_under_it) ... ok
test_the_temp_home_lives_inside_the_temp_root (tests.test_rehearse_deploy.TempHomeTests.test_the_temp_home_lives_inside_the_temp_root) ... ok
```

The whole suite on this worktree, verbatim
(`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`):

```
Ran 5345 tests in 418.042s
OK (skipped=1)
```

Base main `8717de0` was 5,313 tests; the 32 new cases above make 5,345.

---

## 11. Where the owner picks this up

`docs/reports/owner-steps-after-deploy-v2.0-2026-09-10.md` — the consolidated
checklist, in order, superseding v1.0. `docs/reports/deploy-runbook-v1.0-2026-09-09.md`
carries the mechanical steps and has been updated in place for the step order
this run measured.
