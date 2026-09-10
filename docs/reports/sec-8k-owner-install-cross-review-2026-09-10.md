# SEC 8-K owner-install helper cross-review — 2026-09-10

The final helper binds every signing input twice: command-line SHA-256 checks
protect source bytes, while validated canonical hashes bind the active plan,
candidate, proposed-to-approved selector, active mission, and governance record
into the immutable approval receipt. An existing receipt is accepted only when
its own content hash is valid, its actor remains `human:*`, and reconstructing
the receipt from the current packet yields the identical record. A newly
rehashed candidate and selector therefore cannot reuse an earlier signature.

Prepare remains read-only, including when an existing receipt is present.
Repeated apply preserves the original approval timestamp and returns
`identical` for all three targets. All target conflicts and symlinks are
preflighted before writes. The review found one remaining partial-install path:
a filesystem failure or concurrent conflict during the second or third atomic
link left outputs created earlier in that invocation. The helper now tracks
only files it created and removes those exact bytes on failure; it never removes
an identical file that existed before the run.

Focused validation uses the real proposal validators and a real temporary Core
mission authority. It covers source-byte/hash tampering, changed preserved
scope, selector traversal and schema drift, conflicting targets, existing
receipt prepare/apply, a different signing packet against an existing receipt,
late filesystem failure rollback, repeated apply, and unchanged source-Core
bytes.

```text
PYTHONPATH=src python3 -m unittest tests.test_sec_8k_owner_install
Ran 9 tests in 0.711s — OK
```

No live artifact was signed, installed, replaced, or read for writing.
