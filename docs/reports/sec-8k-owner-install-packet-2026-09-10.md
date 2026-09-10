# SEC 8-K owner install packet — 2026-09-10

Added a two-stage helper for the already-reviewed SEC 8-K proposal. `prepare` reads and validates the live active mission and v1 plan, candidate, and proposed selector, then prints exact refs, hashes, target paths, target states, preserved company scope/budget, and the sole appended 8-K spec. It writes nothing. `apply` additionally requires an explicit `human:` actor, `--execute`, and `--service-stopped-ack`.

The helper binds the candidate and selector file SHA-256 values, the active plan's original content hash, the selector's candidate ref/hash, and the current read-only live mission universe. It refuses any candidate scope/budget/company change, selector drift, stale active plan, tampered input, or existing different target. Each target is published through a temporary fsynced mode-0600 file and an atomic no-overwrite hard link. An interrupted two-file operation is safely resumable: identical content is idempotent and a different file is never replaced.

Installing these files is not deployment or product acceptance. The approved selector affects the next LaunchAgent render; `deploy/macos/install.sh` and post-start artifact acceptance remain separate. The helper performs no network or model call.

The private owner packet at `/Users/everflow/Projects/dalton-owner-activation-20260910` now contains `sec-8k-selector.proposed.json`, SHA-256 `f622300a3a21b050f5daa075f2a614b168f5c4e5bbd1cc3bed427645e909b088`, plus exact prepare/apply commands in its README and bindings in its manifest. Nothing was applied to live. A real prepare run against live v13 succeeded and reported both targets absent, v1 plan hash `27a19e8b04aad10aa6d60fa6dab5a0f50be72c5b4c891c995e61f1d425ad6ac7`, candidate v2 hash `a660a9e1b863ffe1a9afe526c5005710d0f7d82f907caf60a41b1a14c9e7b388`, and only the reviewed 8-K spec delta.

Verification:

```text
PYTHONPATH=src python3 -m unittest tests.test_sec_8k_owner_install tests.test_sec_8k_discovery_proposal
```

Result: 8 tests passed. Tests cover dry-run non-writing behavior, explicit owner gates, idempotent apply in a temporary directory, tamper/stale rejection, scope mutation rejection, and refusal to overwrite a different target. `git diff --check` passed.
