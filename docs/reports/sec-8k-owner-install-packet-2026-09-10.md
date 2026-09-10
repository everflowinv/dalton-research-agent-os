# SEC 8-K owner install packet — 2026-09-10

Added a two-stage helper for the already-reviewed SEC 8-K proposal. `prepare` reads and validates the live active mission and v1 plan, candidate, and proposed selector, then prints exact refs, hashes, target paths, target states, preserved company scope/budget, and the sole appended 8-K spec. It writes nothing. `apply` additionally requires an explicit `human:` actor, `--execute`, and `--service-stopped-ack`.

The helper reads every supplied artifact once and binds the SHA-256 check and JSON decode to those same bytes. It binds candidate, selector, active plan, current mission, and packaged SEC governance. The proposal builder's scope and capability checks are reused, and the real mission authorization path is exercised on a temporary SQLite backup. It also requires the exact next plan version and the renderer's closed selector schema/source contract, with a plain filename that cannot escape the target directory.

Apply persists an owner approval receipt between candidate publication and selector publication, so the selector is the final enabling file. Each file uses a temporary fsynced mode-0600 file and an atomic no-overwrite hard link. This is deliberately not a three-file transaction: a crash can leave a candidate or receipt without a selector. Rerunning is safe; an identical receipt reuses its original approval timestamp and different existing content is never replaced.

Installing these files is not deployment or product acceptance. The approved selector affects the next LaunchAgent render; `deploy/macos/install.sh` and post-start artifact acceptance remain separate. The helper performs no network or model call.

The private packet command and manifest now include the active-plan byte hash and packaged governance artifact/hash. A real prepare against the current live Core completed read-only with candidate, approval receipt, and selector targets all absent. Nothing was applied to live.

Verification:

```text
PYTHONPATH=src python3 -m unittest tests.test_sec_8k_owner_install tests.test_sec_8k_discovery_proposal
```

Result: 11 tests passed. Tests cover dry-run non-writing behavior, explicit owner gates, idempotent apply and receipt reuse, symlink/path collision rejection, tamper/stale rejection, scope mutation rejection, and a real temporary Core mission/governance authorization path whose source database bytes remain unchanged. `git diff --check` passed.
