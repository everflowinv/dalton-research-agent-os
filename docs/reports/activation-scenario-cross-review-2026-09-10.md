# Activation scenario cross-review — 2026-09-10

Reviewed activation simulation commit `64d3fd1` on an isolated worktree.

Three blockers were repaired. First, a SHA-256 over a candidate file proves only that those candidate bytes did not move. It did not prove that the candidate extended the Core's active mission. The simulation now resolves the active mission through `CoverageMissionAuthority.active_mission`, which validates the pointer's exact version ref and content hash, requires the candidate's `prior_version_ref` to name that head, and requires title, objective, industry, universe, research questions, deliverables, source plan, constitution/playbook/mandate bindings, and budget to remain identical. Existing write grants and checkpoints cannot be removed; activation may only add them, and the automation principal cannot change.

Second, `--approve-governance` accepted any syntactically valid JSON filename from the supplied directory. The hash-bound model manifest now carries a closed, unique `governance_allowlist`; every requested approval must appear in it. The committed manifest allowlist is empty, so the default simulation approves nothing. Approval still validates the proposal before and after changing its status and writes only under the confined temporary state.

Third, model catalog sync was a nonfatal base-rehearsal step, so a failed sync was followed by a successful-looking “SIMULATION mission=...” mutation in the copy. Activation now requires that exact prior step to have succeeded and otherwise stops before reading or publishing the candidate. The model manifest additionally validates all nine role names, the tier vocabulary, and four distinct producer/verifier tier pairs before installing configs.

The temporary-root check now uses resolved path ancestry and explicitly rejects equality with the source root, avoiding string-prefix acceptance such as `/tmp-not-temporary`. Base rehearsal confinement and copy tests continue to cover read-only source SQLite backup and temp-only writes. Stub broker `launched` states remain dispatch evidence only; neither code nor report counts them as accepted dossier, DebateMap, or judgement artifacts.

The frozen simulation manifest hash is now `c4875b79c80cc05f7a238483200280c28cac2902aafcff5a9cd38e1acf8c8bec`. No production source, live database, governance record, model route, or service configuration was modified.

Verification:

```text
PYTHONPATH=src python3 -m unittest tests.test_activation_scenario_rehearsal tests.test_rehearse_deploy
```

Result: 103 tests passed in 0.773 seconds. `git diff --check` passed.

## Integration hardening

Shared CLI path validation now rejects report paths into any live/source root or exact source input, including symlink aliases, and rejects temporary roots overlapping those sources. Both ordinary and activation rehearsals apply it before their run starts. Hash-bound JSON is decoded from the same bytes that were hashed. SEC selection now rejects a dangling selector symlink instead of silently reverting to v1. **121 focused tests / 0.800s passed** across ordinary/activation rehearsal and SEC proposal/selection tests.
