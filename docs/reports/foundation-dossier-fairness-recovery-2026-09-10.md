# Company dossier scheduling fairness recovery — 2026-09-10

## Problem and result

The dossier coordinator previously held one global Claim-ledger signature.
When the child repeatedly selected the first stale company and its content was
rejected, that terminal record prevented every other screened company from
running. An unrelated Claim anywhere also released the global hold and paid to
retry the same rejected company.

The coordinator now enumerates the existing folded Initial Screen results in
mission order. Each company gets an input fingerprint from the same bounded
canonical-Claim and numeric-material readers that build the dossier prompt,
plus its document figures, current forecast and dossier heads, and the dossier
draft and verifier contracts. This includes Claim retirement and canonical
selection changes, filed statement rows, forecast cells, guidance material,
and the derived guidance-profile inputs without using global table counts.
Mission, governance, policy, and model configuration bindings remain part of
the permission key.
The first company whose exact key is eligible is passed to the existing child
`--company-ref` filter. Held companies are skipped without weakening their
terminal or dependency classification.

The child still owns planning, stale-unit selection, retired-Claim filtering,
verification, and publication. The coordinator does not infer that a company
needs a particular section.

## Verification

```text
PYTHONPATH=src:. python3 -m unittest tests.test_dossier_lane -v
PYTHONPATH=src:. python3 -m unittest tests.test_f14_whole_ledger_lanes -v
```

Results: 72 dossier tests and 8 shared-ledger tests passed. New regressions
prove a terminal rejection for the first company permits the second company to
launch with unchanged inputs. They also prove Claim, retirement, statement,
forecast, and document-figure changes move only the affected company's
fingerprint.

No live state, service, broker, or model call was used.
