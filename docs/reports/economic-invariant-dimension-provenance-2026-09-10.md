# Economic invariant dimension provenance — 2026-09-10

## Result

The segment-sum invariant no longer has to choose between flattening a
multi-axis XBRL context and disabling every segment check.  Fresh SEC statement
observations now count the complete non-empty `dim_*` context columns exposed by
edgartools `with_dimensions()`.  The nullable count is preserved through the
statement authority.  Only a proven count of one is eligible for the existing
known-additive-axis check.

Legacy rows and parser rows that expose only the projected axis/member remain
`NULL`.  They are reported as not checked; the code does not infer a count from
the projection.  A two-axis context remains two-axis after storage and cannot
be flattened into a geographic or product sum.

The connector output contract now requires nullable `dimension_count`, so its
schema hash changed.  A proposed v3 governance record is included and the lane
names it explicitly.  It is not approved by this change.  Until an owner
approves that exact record, new network runs correctly remain governance
blocked; no prior approval is treated as covering the wider output.

## Reader behavior

The Cockpit now reads the economic-invariant report already bound into a
published forecast model's filing proof.  A published model with unavailable
checks displays the invariant names and evidence reasons.  A fully checked
model stays quiet, and an invariant failure retains the existing “not
published” treatment.  Models without a filing proof do not get invented
check results.

## Replay boundary

Each filing record now hashes the complete normalized line list in
`statement_lines_hash`; each line stores the dimension count, and the filing
continues to cite the immutable raw parser artifact.  That gives a reader the
normalized proof, its source artifact, and a stable hash without rewriting old
statement rows.

## Validation

Focused tests cover complete one- and two-axis parser contexts, projected-only
legacy contexts, authority round-trip, multi-axis exclusion from segment sums,
the changed connector contract/governance binding, and Cockpit failure versus
not-checked display.  The final affected run passed 254 tests.

