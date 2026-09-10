# Deploy runbook — main `ebd2ea8` onto the live Core — v1.0 — 2026-09-09

Owner-facing. Every command below was run against a copy of the live state by
`scripts/rehearse_deploy.py` on 2026-09-10 (see
`docs/reports/ops-deploy-rehearsal-v1.0-2026-09-09.md`), except the four that
cannot be rehearsed at all — `launchctl`, `openclaw gateway restart`,
`dalton-gov` and `dalton-connector-governance approve` — which are marked
**not rehearsed**.

Read the whole thing before starting step 1. Steps 6–10 are approvals and
publications; the system runs correctly without them, it just runs *narrower*
— eleven lanes will report `ungranted` or `unconfigured` every tick and spend
nothing. That is a safe place to stop for a day if the deploy runs long.

Shell variables used throughout:

```sh
export DALTON_ROOT="$HOME/Library/Application Support/Dalton"
export STATE="$DALTON_ROOT/state/dalton-core"
export CONFIG="$DALTON_ROOT/config/service.json"
export VENV="$DALTON_ROOT/runtime/venv"
export REPO="$HOME/Projects/dalton-research-agent-os"
export DOMAIN="gui/$(id -u)"
export OWNER=human:lumos
```

---

## 1. Stop the service, in this order

`install.sh` does this itself; do it by hand first if you want to take the
backup against a quiet Core, which is the recommendation.

```sh
for label in space.lumos.dalton.thesis-impact space.lumos.dalton.control space.lumos.dalton.controller; do
  launchctl print "$DOMAIN/$label" >/dev/null 2>&1 && launchctl bootout "$DOMAIN/$label"
done
"$VENV/bin/python" -m dalton_core.launch_drain --state-dir "$STATE" --timeout 600
launchctl bootout "$DOMAIN/space.lumos.dalton.writer"
```

Expected: the drain prints a JSON line ending `"status": "drained"` (or
`"status": "timeout"` with the list of children still running — that is not
fatal, proceed). Then:

```sh
launchctl list | grep space.lumos.dalton   # expected: no output
```

The controller goes down **first** and the writer **last**. Reversed, the
controller keeps launching lane children behind the drain and it waits the full
600 s for nothing — that happened on the first deploy that had a drain.

**not rehearsed** (launchctl).

## 2. Back up

```sh
"$VENV/bin/dalton-backup" create --backup-root "$STATE/backups" \
  --database core=$STATE/core.sqlite \
  --database scheduler=$STATE/scheduler.sqlite \
  --database model-router=$STATE/model-router.sqlite \
  --database thesis-impact-budget=$STATE/thesis-impact-budget.sqlite \
  --database projection=$STATE/dashboard-projection.sqlite
cp -R "$STATE/connector-governance" "$STATE/backups/connector-governance-$(date -u +%Y%m%dT%H%M%SZ)"
cp "$CONFIG" "$DALTON_ROOT/config/service.pre-ebd2ea8-$(date -u +%Y%m%d).json"
```

Expected: a snapshot id on stdout; note it, step 12 needs it.

The `cp -R` of the governance directory is **not optional**. The rehearsal
found that `sec-filings-index-v1.json` exists on the live Core and in no
repository — `install.sh` cannot re-create it, and the writer's plist names it
unconditionally. Losing it means a writer that will not start its filings lane
and an approval nobody can reproduce.

## 3. Run the installer

```sh
cd "$REPO" && git log --oneline -1     # expected: ebd2ea8
deploy/macos/install.sh
```

Expected: pip output, then the seed block silently (it prints nothing when
every record is already present), then the catalog sync report as JSON, then
four plist paths, then `launchctl` output, then `dalton-health` exiting 0.

Two things to watch:

- The catalog sync now **fails the install** rather than warning. If it prints
  `model catalog sync failed`, stop and read step 4 — the gateway may need its
  reload first.
- The sync will register five profiles and retire six. That is expected and
  idempotent; a second `install.sh` writes nothing.

If you want the reading throughput and model tiers the live Core is already
running, they are persisted in `service.json` and a plain re-install keeps
them. Only set the environment variables if you are changing one.

## 4. Reload the gateway

**not rehearsed** — this is the one step that touches OpenClaw.

```sh
openclaw gateway restart
```

Expected: the gateway comes back and `~/.openclaw/dalton-model-broker.sock`
is re-created. Do this **before or immediately after** step 3: the cheap
fallback chain's middle link is `zai/glm-5.3-flash`, which the broker only
offers once it has re-read its plugin config. Until then that link is dead
weight — the chain records it as a link that did not serve and falls through to
`google/gemini-3.5-flash-lite`, so nothing is lost, but you are running a
two-link chain that says three.

Verify without writing anything:

```sh
cd "$REPO" && PYTHONPATH="$REPO/src" "$VENV/bin/python" \
  scripts/sync_openclaw_model_catalog.py --check-only \
  --openclaw-config ~/.openclaw/openclaw.json \
  --model-router-db "$STATE/model-router.sqlite"
```

Expected: `"catalog_in_sync": true` and exit 0. Exit 2 means they still
disagree.

## 5. Migrate the day ledger with the service still stopped

`install.sh` does not do this and the writer will not do it either — the day
ledger is opened by the thesis-impact worker, which is a separate agent.
`ALTER TABLE ADD COLUMN` is instant but wants a write lock, and the live writer
holds a long-lived connection to that database.

```sh
PYTHONPATH="$REPO/src" "$VENV/bin/python" -c \
  "from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore as S; S('$STATE/thesis-impact-budget.sqlite').close()"
```

Expected: no output, exit 0. Verify:

```sh
sqlite3 "$STATE/thesis-impact-budget.sqlite" \
  "SELECT COUNT(*) FROM pragma_table_info('thesis_impact_day_admissions') WHERE name='pool';"
```

Expected: `1`.

**What this does to the numbers on day one.** The four new columns are
nullable and nothing already written moves — no `content_hash` changes, and an
admission recorded last week still verifies today. But every admission written
before the migration has `pool IS NULL`, and `pool_status` reports those under
`unpooled_micros` rather than guessing a pool for them. The rehearsal measured
**5,036 of 5,037** existing admissions in that state. So for the first day the
pool figures will not add up to the day cap, and `unpooled` will be most of the
spend. That is correct and it drains as those admissions settle.

**The replay case.** An admission recorded before C2 has a `mission_binding`
with no pool fields. Replaying that same call after the migration sends a
binding that *does* have them, and `admit`'s replay check would normally read
that as "the binding changed" and refuse — which would jam exactly the work
that was in flight at the moment of the migration. The comparison treats a
stored binding with no pool keys at all as the same admission gaining a
dimension (`_POOL_BINDING_KEYS`); any other changed field is still a conflict.
Nothing to do here, but if you see a refused replay on deploy day, that is the
code path to look at first.

## 6. Approve the connector records the deploy seeded

**not rehearsed** (an approval is a human act and the rehearsal must not forge
one).

```sh
"$VENV/bin/dalton-connector-governance" show --path "$STATE/connector-governance/yfinance-daily-prices-v1.json"
"$VENV/bin/dalton-connector-governance" approve \
  --path "$STATE/connector-governance/yfinance-daily-prices-v1.json" \
  --approved-by "$OWNER"
```

Expected: `show` prints `"status": "proposed"`, `approve` prints the record
with `"status": "approved"` and your `approved_by`.

`yfinance-analyst-estimates-v1.json` may stay `proposed` — nothing consumes it
yet.

**Ordering that matters:** the market-price lane's plist argument is written
only when the record file is *on disk*, and `install.sh` seeds it in step 3, so
step 3 already put `--market-price-governance` into the writer plist. Approving
in place is enough; you do **not** need to re-run `install.sh`. You do need the
writer restart in step 11.

Not seeded, and therefore not approvable yet — the catalyst-calendar lane's
record `yfinance-calendar-v1.json` is committed in `deploy/connector-governance/`
and `install.sh` has no block for it. Until someone adds that block the C1 lane
reports `unconfigured` every tick. Same for the six `cn-hk-findata-*`, the three
`xueqiu-*`, the three `x-xreach-*`, `employee-reviews-blind-v1.json`,
`guidepoint-get-transcript-narrowing-v1.json`, and the four S1 feed records —
the last four deliberately, per the comment in `install.sh`.

## 7. Publish the mission version

**not rehearsed** past the params file, which was built and validated against a
copy of the live Core.

```sh
cd "$REPO"
PYTHONPATH="$REPO/src" "$VENV/bin/python" scripts/build_mission_v2_params.py \
  --source-core "$STATE/core.sqlite" \
  --mission-ref coverage-mission:us-it-services \
  --add-scope market_price \
  --add-scope market_event \
  --add-scope claim_index \
  --add-scope research_task \
  --add-scope dossier \
  --add-scope debate_map \
  --add-scope valuation \
  --add-scope consensus_estimate \
  --add-scope forecast_revision_proposal \
  --add-scope thesis_revision_candidate \
  --add-scope conviction_call \
  --output /tmp/us-it-services-mission-v14.params.json
```

Expected `autonomy.may_write`, exactly (verified against the live manifest):

```
evidence, claim, forecast_line, model_run, research_question, observation,
stage_record, forecast_reconciliation, source_discovery, claim_challenge,
deliverable, market_price, market_event, claim_index, research_task, dossier,
debate_map, valuation, consensus_estimate, forecast_revision_proposal,
thesis_revision_candidate, conviction_call
```

and `prior_version_ref: coverage-mission-version:us-it-services:13`.

**Then edit the params file by hand**, because `build_mission_v2_params.py` has
no `--add-checkpoint`: add `"thesis_revision_candidate"` to
`autonomy.human_checkpoints`, which then reads

```
deep_insight_gate, investment_memo, thesis_admission, thesis_revision,
forecast_overturn, scope_expansion, budget_expansion, thesis_revision_candidate
```

Without both the scope *and* the checkpoint, `revise_thesis` records `queued`
and names ADR-0007 instead of proposing.

**Sources.** The `source_plan` carries over unchanged and should stay that way
this deploy — five rows: `source:sec-edgar` connected, `source:alphaengine`
connected, `source:web-search` connected, `source:company-ir` not_connected,
`source:guidepoint` not_connected. Guidepoint stays `not_connected` because
this repo ships no `us-it-services-guidepoint-v1.json` discovery plan, so the
lane cannot run whatever the manifest says. If that plan ever lands, the flag is
`--set-source-status source:guidepoint=connected` on the command above.

Publish:

```sh
"$VENV/bin/dalton-gov" \
  --token-config "$STATE/writer-tokens.json" \
  --socket "$STATE/run/writer.sock" \
  --actor "$OWNER" \
  --operation create_coverage_mission \
  --params /tmp/us-it-services-mission-v14.params.json
```

Expected: a JSON record with `"version": 14`. This needs the writer running,
so run it **after** step 11 and then wait one tick, or start the writer alone
first. Verify:

```sh
sqlite3 "$STATE/core.sqlite" \
  "SELECT mission_version_id FROM coverage_mission_versions ORDER BY created_at DESC LIMIT 1;"
```

Expected: `coverage-mission-version:us-it-services:14`.

## 8. Re-sign the auto-commit policy (ADR-0007)

**not rehearsed.**

Publish and sign a governance policy version whose
`research_candidate_auto_commit.rules` lists
`research-auto-commit:mission-verified-figure:v1` — the constant is
`research_verification.MISSION_VERIFIED_FIGURE_RULE_REF`, and the shape is the
one ADR-0005 already uses for
`research-auto-commit:mission-document-qualitative:v1`.

Until that rule is in a signed policy, figures only reach the Ledger through
human review (`commit_reviewed_candidate`). That is a working path, not a
broken one, so this step can slip a day without anything failing.

Verify:

```sh
sqlite3 "$STATE/core.sqlite" \
  "SELECT id FROM governance_policy_versions ORDER BY created_at DESC LIMIT 1;"
```

## 9. Repoint the verifier phase pin

**not rehearsed** — repointing an immutable phase pin is an owner decision.

`VERIFIER_POLICY_REF` (`model-routing-policy-version:dalton-openclaw-verifier:1`)
pins `profile:gemini-3-7-flash`. The broker has not offered that model for some
time, and the catalog sync in step 3 gives it a `retired` version. The
rehearsal confirmed this on a copy: after the sync, that profile's latest
version carries `status: retired`.

The consequence is a *better* failure than before — verifier routing is now
refused with `profile_retired` at the router instead of failing at the broker —
but it is still a refusal, and every verifier call will take it. The intended
replacement is the `verifier` tier chain:

```
profile:claude-fable-5-1 -> profile:zai-glm-5-3 -> profile:gemini-3-5-flash-lite
```

Append a new verifier policy version carrying that chain and repoint
`VERIFIER_POLICY_REF` at it. Note that `thesis_impact.enabled` is `false` in
the live `service.json`, so nothing is calling the verifier today — this can be
deferred, but it must not be *forgotten*, because turning thesis-impact back on
without it fails closed on every call.

## 10. Install the lane switches you want on

Four lanes are files-on-disk away from existing, and `install.sh` writes none
of them. Skip any you do not want; a lane that is absent costs nothing.

```sh
# P14a tracking lane
cp "$REPO/deploy/phase9/p14a-tracking-policy-v1.json" "$STATE/tracking-policy.json"
chmod 600 "$STATE/tracking-policy.json"
```

`event-judgement-model-config.json`, `event-verifier-model-config.json` and
`claim-index-model-config.json` have no setup module wired into `install.sh`
either. The two judgement configs must point at routing policies in **different
model families**, or every judgement fails closed on the independence check.
Writing them is a change to `install.sh` and belongs to the P14a / P12b owners,
not to this deploy.

## 11. Start, and watch the first tick

```sh
for label in space.lumos.dalton.writer space.lumos.dalton.controller space.lumos.dalton.control; do
  launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$label.plist"
  launchctl enable "$DOMAIN/$label"
  launchctl kickstart -k "$DOMAIN/$label"
done
"$VENV/bin/dalton-health" --config "$CONFIG" --max-age-seconds 45
```

Expected: `dalton-health` exits 0 within about 30 s. (`install.sh` retries it
fifteen times at two-second intervals, so if you ran step 3 without stopping
first, this already happened.)

The first tick:

```sh
sleep 20 && python3 -m json.tool "$STATE/run/heartbeat.json" | head -60
```

Expected: **no lane whose status begins `unavailable:`**. Every lane should
read one of `idle`, `launched`, `ungranted`, `unconfigured`, `held` or
`deferred`, and `tick_ledger` should read `recorded`. The rehearsal's table for
this exact code against a copy of this exact state is in
`docs/reports/ops-deploy-rehearsal-v1.0-2026-09-09.md`; the two lanes that
should *change* after steps 6 and 7 are `mission_market_prices` (`ungranted` ->
`launched`/`idle`) and `claim_index` (stays `unconfigured` until step 10).

Then confirm the tick ledger is being written, which is new in this deploy:

```sh
sqlite3 "$STATE/tick-ledger.sqlite" \
  "SELECT day, COUNT(*), SUM(idle) FROM tick_ledger_ticks GROUP BY day ORDER BY day DESC LIMIT 3;"
```

Expected: a row for today with a count that grows every five seconds.

Watch for a day: `document_extraction`'s admissions now carry a pool, so the
coverage cap constrains extraction for the first time. If extraction starts
reporting `skipped:pool_exhausted`, that is the new behaviour working, not a
fault — but it is the first thing worth looking at on day two, along with the
`maintenance` pool at 5%, which is the tightest and where `claim_index` will
land.

## 12. Rollback

Stop everything as in step 1, restore the snapshot from step 2, put the old
plists back, restart:

```sh
launchctl bootout "$DOMAIN/space.lumos.dalton.controller" 2>/dev/null
"$VENV/bin/python" -m dalton_core.launch_drain --state-dir "$STATE" --timeout 300
launchctl bootout "$DOMAIN/space.lumos.dalton.writer" 2>/dev/null
"$VENV/bin/dalton-backup" verify --backup-root "$STATE/backups" --snapshot-id "$SNAPSHOT" --restore-root /tmp/dalton-rollback-check
# only after verify passes:
cp /tmp/dalton-rollback-check/*.sqlite "$STATE"/
cp "$DALTON_ROOT/config/service.pre-ebd2ea8-"*.json "$CONFIG"
cd "$REPO" && git checkout <previous-main> && deploy/macos/install.sh
```

The one thing rollback cannot undo is the mission version and the governance
approvals: both are append-only by design. Rolling the code back with mission
v14 in place is safe — a mission may grant a scope no lane claims — but a
`git checkout` to a commit whose `AUTOMATION_WRITE_SCOPES` does not contain
`conviction_call` will refuse to *read* v14 and the mission will fail closed.
If you are rolling back after step 7, publish a v15 that drops the added
scopes rather than trying to delete v14.
