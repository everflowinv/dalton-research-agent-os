# DebateMap mission-binding cross-review — 2026-09-10

## Result

The review found and removed a false provenance projection on databases created
before `mission_rebind` existed. The old implementation stored
`evidence_thicker` in the indexed `change_reason` column while canonical JSON
said `mission_rebind`, and `_decode` deliberately ignored that mismatch. The
authority now performs an idempotent table rebuild when the deployed CHECK is
old. It copies all rows verbatim, recreates indexes and append-only triggers,
restores foreign keys, and refuses a migration attempted inside a transaction.
Afterward both canonical JSON and the indexed column truthfully store
`mission_rebind`; `_decode` again rejects every column mismatch.

The mission authorization read, current map read, novelty decision, and insert
now share one `BEGIN IMMEDIATE` authority transaction. Mission construction,
which may apply its own migrations, happens before that transaction. This
closes the gap in which a mission pointer could change after authorization but
before publication.

`subject_kind` is checked against mission membership: universe members are
companies and the mission's industry subject is an industry. A caller cannot
bind a company ref as an industry or vice versa.

Mission changes no longer bypass normal novelty. `mission_rebind` is admitted
only when the mission changes and map content, evidence fingerprint and refs,
policy, Constitution, rejection set, and producer/verifier attribution are
identical. A changed policy or debate under that reason is a duplicate/refusal,
while normal content changes must pass the existing debate/status/ref novelty
rules. The lane's rebind fast path already compares mission, policy,
Constitution, and evidence and its real test proves zero model calls.

The shared `SCHEMA_VERSION = "0.2"` is used only for DebateMapVersion records.
`DEBATE_POLICY` has its own literal contract and `POLICY_HASH`; no policy schema
was accidentally advanced.

## Deployment coverage

`DebateMapAuthority` remains in `CORE_MIGRATIONS`, so bootstrap and deployment
rehearsal instantiate the authority and execute the CHECK migration before
lanes run. The focused rehearsal tests cover the migration registry and all
migration symbols. A purpose-built old-table test inserts a real immutable
record, runs the authority migration, proves `record_json` and `content_hash`
are byte-identical, checks the widened table SQL, then publishes a mission-only
rebind and verifies the indexed reason is exactly `mission_rebind`.

## Validation

```text
PYTHONPATH=src python3 -m unittest tests.test_debate_map \
  tests.test_debate_map_lane tests.test_deep_insight_gate_lane \
  tests.test_rehearse_deploy
Ran 240 tests in 5.293s — OK
```

No live database, model call, deployment, or network access was used.
