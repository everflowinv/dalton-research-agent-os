# V14 product-recovery frozen acceptance — 2026-09-10

This acceptance is bound to frozen source commit
`38721e4413341e8ec8c75ea185413a69845a0eb6` in
`/Users/everflow/Projects/dalton-v14-product-recovery-acceptance-worktree`.
The frozen checkout and live Dalton root were read only throughout the checks.

## Wheel

The PEP 517 wheel build completed successfully with:

```text
python3 -m pip wheel . --no-deps --wheel-dir /tmp/dalton-v14-recovery-wheel
```

Artifact:

```text
/tmp/dalton-v14-recovery-wheel/dalton_core-0.1.0.dev0-py3-none-any.whl
sha256 df29656980b085b6edd67263dad74aeeca30d39ddc939ffbf2e3a81cc180f71d
```

`/tmp/dalton-v14-verify-wheel.py` passed. Its exact inventory comparison found
367 Python, 67 SQL, 3 HTML, and 81 JSON files, with no missing or extra files,
and every packaged byte matched the corresponding frozen source byte. It
extracted three nonempty inline HTML scripts and all three passed `node
--check`.

Both budget catalogs are present and byte-identical to frozen source:

- `call_budget_defaults.json`: 3,797 bytes, SHA-256
  `d821ed4238c438fca4a1fa27efeda1aef8517e61cbfaad0d15ce534df0ebe32a`
- `run_budget_defaults.json`: 376 bytes, SHA-256
  `5f316798fedc0498fbe39f4b956b24b31960ca97f1eacc0e1e1bd947ac39941c`

## Read-only live-state rehearsal

The first invocation, without a source-tree `PYTHONPATH`, copied live state
read only and then stopped with `ModuleNotFoundError: dalton_core`. This was an
invocation-environment failure rather than a frozen-source result. The final
rehearsal used the frozen `src` explicitly and a second new temp root:

```text
PYTHONPATH=/Users/everflow/Projects/dalton-v14-product-recovery-acceptance-worktree/src \
python3 scripts/rehearse_deploy.py \
  --live-root '/Users/everflow/Library/Application Support/Dalton' \
  --temp-root /tmp/dalton-v14-recovery-rehearsal-source \
  --report /tmp/dalton-v14-recovery-rehearsal-report.md
```

The final command exited 0. It copied 34 live items (845 MB) into the temp
root, confined all generated state there, bootstrapped the copied Core, applied
67/67 schemas, synchronized the copied model catalog, rendered three plists,
confirmed 22 mission write grants and 10 checkpoints with none missing, bound
the temporary writer socket, and completed one controller tick with 38 ledger
entries and zero escaped paths. The current signed V14 mission was retained;
the rehearsal did not simulate or sign a mission and made no paid or network
business calls.

The report records operational findings rather than hiding them. The current
verifier phase pin names retired `profile:gemini-3-7-flash` and will refuse
until the owner repoints it to the verifier tier chain. Optional company-wiki,
crowd-tool, and prior-research governance gates remain shut; four explicitly
non-seeded tools remain absent. The live install has 15/16 lane switch files,
with `catalog_sync` absent. Existing pre-pool admissions remain classified as
unpooled until settlement. Rendered plist differences reflect the frozen
installer command set and interpreter paths. Full detail is preserved in
`/tmp/dalton-v14-recovery-rehearsal-report.md`.

This is packaging and isolated deployment-rehearsal evidence. It does not
claim live deployment or product-output acceptance.
