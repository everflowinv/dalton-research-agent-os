# Dossier retired-Claim prefilter — 2026-09-10

`company_dossier_cli.claim_material` now removes Claims with an append-only
`claim_retirement_decisions.decision = retired` record before priority sorting,
prompt quota slicing, staleness planning, and any model call. Historical dossier
versions and Claim rows remain unchanged and resolvable. The final
`unresolved_refs` check remains as a second defense against a retirement that
appears after input planning or against a carried section.

The compatibility behavior is explicit: a Core without the retirement table is
read exactly as before; live canonical Claims remain eligible; and a company
whose only new material is retired is idle and spends zero model calls.

Focused validation:

```
PYTHONPATH=src python3 -m unittest tests.test_dossier_lane
Ran 67 tests in 7.488s — OK
```

## Issuer lineage boundary

The existing issuer helpers validate acquisition/extraction metadata. The
Dossier Claim projection does not currently expose the immutable document and
issuer-position proof needed to re-run that test. Reconstructing it inside this
small read function would create a second, partial lineage resolver and could
incorrectly remove legitimate target-company conference or comparative
research. This patch therefore does not infer or automatically retire Claims.

A separate unsigned owner-review manifest was prepared outside the repository
for the 19 live Claims already proven by the prior read-only lineage audit to
come from a different issuer (EPAM 6, IBM 8, DXC 5), including the two exact IBM
Initial Screen citations. It is review evidence only; it performs no decision,
retraction, stage transition, or live write. Until an owner decides those exact
hashes, affected product acceptance remains a human checkpoint.
