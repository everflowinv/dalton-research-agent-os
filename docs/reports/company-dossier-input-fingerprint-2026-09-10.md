# CompanyDossier exact producer-input fingerprints — 2026-09-10

## Result

New dossier records use wire schema 0.2 and carry a closed map of one nullable
fingerprint per dossier unit. A fingerprint is frozen immediately before that
unit's model call, after material rows, prior body, guidance table, market-view
availability, and the classification produced earlier in the same run have
been resolved. It binds the exact prompt SHA plus the mission, Constitution,
and dossier-policy refs and hashes. It adds no model call.

This replaces the rejected whole-plan design. A three-unit tick assigns hashes
only to the three outputs it actually produced. A carried unit keeps no newly
claimed provenance: it is `null` unless this version actually redrafted it.
Consequently a first version whose other units are unavailable can be fresh,
while a later partial version carrying drafted units reports `unknown`. Any
known per-unit mismatch reports `stale` first. This is conservative and cannot
label inherited prose fresh using the wrong predecessor.

`reconstruct_dossier_input(connection, record, current_mission, policy)` is the
read-only audit entry point. It fixes historical prompt context to the
record's `prior_version_ref`, while binding governance to the supplied current
mission. It uses the same plan, material transformation, prior-body rendering,
profile-table rendering, and prompt builder as publication. A test opens a
real fixture database with `mode=ro`, `query_only`, and an authorizer denying
writes and DDL; reconstruction performs no denied operation.

The company model specification is not fingerprinted because it is not an
input to `draft_unit` or its prompt. Forecast rows derived elsewhere are bound
when they appear in the exact rendered material. Adding the model spec now
would create false stale results. If it later becomes a dossier producer
input, the shared builder must add it at that boundary.

## Compatibility and integrity

The migration adds nullable `input_fingerprints_json`. It also tolerates an
existing abandoned singular `input_fingerprint` column and leaves it intact.
Schema 0.1 JSON and its hashes remain unchanged and has `unknown` freshness.
The authority checks the plural column against record JSON, validates every
non-null SHA, and rejects record tampering.

`input_fingerprints` is explicitly excluded from the semantic body hash, so a
metadata-only difference cannot bypass the identical-body or no-new-evidence
publication gates. It remains covered by the immutable record content hash.
A duplicate publication returns the existing head and therefore cannot attach
new fingerprint metadata. Such a head remains honestly `unknown` or `stale`;
an append-only build receipt would be a separate future authority.

## Validation

```text
PYTHONPATH=src python3 -m unittest tests.test_company_dossier \
  tests.test_company_dossier_draft tests.test_dossier_lane
Ran 127 tests in 4.688s — OK
```

The tests include a real published version that reconstructs as fresh, an
uncited newly indexed row that makes it stale, a second partial publication
that remains unknown because it carries older drafted units, exact prompt
material sensitivity, legacy reads, tamper refusal, plural migration alongside
the abandoned singular column, and read-only reconstruction. No live state,
model configuration, deployment state, or network service was changed.

## Integration review

Read-only reconstruction now reuses the audit's existing `query_only` transaction without committing it, while the default Ledger snapshot API still rejects nesting in writable transactions. Forecast-cell material continues through the existing pure version validator instead of bypassing its integrity checks. Reconstruction mirrors the producer's prior-body handling for classification and variant calls.

A first partial dossier can still have draftable sections skipped by the run quota. Those sections now make whole-dossier freshness `unknown`; fresh inputs for the three produced sections do not qualify the remaining sections. The child reports recomputed freshness, and the readiness CLI consumes per-unit hashes in one read-only snapshot. Mixed typed citations remain separately validated. **108 focused tests / 7.861s passed**, including real full and partial publications and end-to-end read-only audit reconstruction.

Carried sections without an exact per-unit producer predecessor remain conservative `unknown`. No metadata-only duplicate receipt has been introduced; a duplicate cannot silently claim updated provenance.
