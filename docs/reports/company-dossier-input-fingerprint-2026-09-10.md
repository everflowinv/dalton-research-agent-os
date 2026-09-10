# CompanyDossier exact producer input fingerprint — 2026-09-10

## Boundary and prior-head rule

The dossier now freezes one canonical producer-input object before the first
draft call. It contains the company identity; every planned unit's exact
structure and bounded material rows; claim-version and claim-index attributes;
numeric/forecast rows; the current company-model specification; the prior
classification context; guidance profile; and exact constitution and dossier
policy refs/hashes. `build_dossier_input` is pure and exported so an audit can
rebuild the same object, while `dossier_input_fingerprint` supplies its hash.
No additional model call is introduced.

The prior input is the record named by the produced version's
`prior_version_ref`, including its content hash and classification. A freshness
audit must rebuild against that predecessor rather than pass the newly
published dossier as `prior`; using the new head would make publication itself
change the input and mark every new version stale immediately.

The input is captured once before drafting. It is not re-read after model
calls, so a concurrent authority change cannot be falsely attributed to the
output that was produced from the earlier prompt.

## Compatibility and authority

New records use dossier wire schema 0.2 and carry `input_fingerprint`; the
fingerprint participates in the record content hash. The append-only table has
a nullable `input_fingerprint` column, added in place when an existing 0.1
database is opened. Existing 0.1 record JSON, body hashes, and content hashes
remain byte-compatible and read with freshness `unknown`.

The authority checks the column against the wire value and validates the
fingerprint as SHA-256. `input_freshness(version_ref, current_fingerprint)`
returns `fresh`, `stale`, or `unknown`. The CLI reports the state for a newly
published version. Changing prompt-visible material that the draft did not
ultimately cite still changes the fingerprint, closing the cited-ref/row-count
blind spot.

## Validation

Focused tests cover legacy unknown, new fresh/stale states, fingerprint
tampering, uncited prompt-material changes, authority publication, drafting,
and the dossier lane:

```text
PYTHONPATH=src python3 -m unittest tests.test_company_dossier \
  tests.test_company_dossier_draft tests.test_dossier_lane
Ran 124 tests in 4.292s — OK
```

No live database, model configuration, deployment state, or network service
was changed.
