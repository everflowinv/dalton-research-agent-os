# Wave 4 release wheel validation — 2026-09-10

Frozen source commit:
`79e0ef7ca9348484bc549cdc36b3846bd404f7b3`

The wheel was built from the frozen release-acceptance worktree with:

```text
python3 -m pip wheel --no-deps --wheel-dir /tmp/dalton-wave4-release-wheel .
```

Artifact:

```text
dalton_core-0.1.0.dev0-py3-none-any.whl
SHA-256 99ba69af9960bc9e115842ff2f21c86802abf396dc802f35a85fecd4df4afec1
```

The wheel contains 363 Python files and 67 SQL files, exactly 430 combined.
Every packaged Python and SQL member was compared byte-for-byte with its
source under `src/dalton_core`; none was missing or different.

`cockpit_control.html` and `dashboard.html` were also compared byte-for-byte
with the frozen source. The package additionally contains
`cockpit_control_legacy.html`; it was checked as well and matched. Inline
JavaScript was extracted from all three packaged/source-identical pages and
passed `node --check` for each file.

The build wrote only beneath `/tmp/dalton-wave4-release-wheel` and the normal
pip cache. It did not modify the frozen worktree or live Dalton state.
