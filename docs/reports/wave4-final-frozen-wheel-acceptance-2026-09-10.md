# Wave 4 final frozen-wheel acceptance — 2026-09-10

## Frozen input

- Checkout: `/Users/everflow/Projects/dalton-wave4-final-acceptance-worktree`
- Commit: `f1d61b8ad7c0d9a94830cead535570d6b0a5e7a5`
- The checkout was clean before and after acceptance. No source file, worktree,
  live state, or deployment state was changed.

## Wheel result

- Wheel: `/tmp/dalton-wave4-final-wheel/dalton_core-0.1.0.dev0-py3-none-any.whl`
- SHA-256: `c30b46645f3aab29659cd856ceb0cc9d03090c34f11eb587a33f3ae24f4a23bb`
- Build: `python3 -m pip wheel --no-deps`
- Result: PASS

Every packaged `dalton_core/**/*.py` and `dalton_core/**/*.sql` member was
compared byte for byte with the corresponding file under the frozen checkout's
`src/` directory. The source and wheel each contained 430 such files. There
were no missing, extra, or changed files.

The machine-readable comparison result is beside the wheel at
`/tmp/dalton-wave4-final-wheel/verification.json`.

## Cockpit JavaScript

All inline JavaScript blocks from both packaged-source Cockpit pages were
extracted and checked with `node --check`:

- `cockpit_control.html`: PASS (1 inline script)
- `cockpit_control_legacy.html`: PASS (1 inline script)

Total: 2 of 2 inline scripts passed.

This acceptance did not run the full Python suite; the root integration process
ran that suite independently against the same frozen commit.
