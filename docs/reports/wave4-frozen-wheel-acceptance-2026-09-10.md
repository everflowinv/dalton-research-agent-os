+# Wave 4 frozen wheel acceptance — 2026-09-10

Frozen source: `61f56759c5e249be150623844df3ee4550a5ee65` in the detached acceptance worktree. The source worktree remained clean before and after the build.

`python3 -m build` was unavailable because the environment does not install the `build` module. The equivalent isolated PEP 517 build used `python3 -m pip wheel . --no-deps --wheel-dir /tmp/dalton-wave4-wheel`.

Artifact:

- `/tmp/dalton-wave4-wheel/dalton_core-0.1.0.dev0-py3-none-any.whl`
- size: 3,373,565 bytes
- SHA-256: `097f7554c7b8ce8f489c844c6db73e55c2ce790269d2812c7d2d414f03b563ec`

Every packaged Python and SQL entry was compared byte-for-byte with its corresponding path under frozen `src/`: 430 files total, comprising 363 Python files and 67 SQL files. Missing source counterparts: 0. Byte mismatches: 0.

The single inline script from frozen `src/dalton_core/cockpit_control.html` was extracted to `/tmp/dalton-wave4-cockpit-scripts.js` (58,142 bytes). `node --check` completed successfully.

This acceptance performed a local wheel build and static byte/syntax checks only. It made no broker call, model call, network fetch, budget admission, host configuration change, deployment, signature, or live authority write.

