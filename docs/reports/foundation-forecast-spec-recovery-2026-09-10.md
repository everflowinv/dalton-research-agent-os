# Foundation forecast specification recovery — 2026-09-10

The model-specification contract now participates in forecast work identity.
When the current verified specification has a different `spec_id` from the
latest forecast model version, the forecast lane derives a new digest from the
new specification, its content hash, the unchanged filed inputs, and the
frozen generator and formula hashes. A terminal failure recorded for the old
digest therefore cannot suppress this work.

The result is a new specification-bound **version in the existing company
model chain**. It does not create a separate `model_ref`. Publication binds the
candidate to the latest version through `source_version_ref`, so a concurrent
head change fails with the existing lost-update check. The prior model and its
forecast-line records remain immutable; the appended version names the prior
version and the new specification. Repeating the same current specification
and filed inputs produces no further version. Ordinary same-spec filing updates
continue through the existing actualisation path and do not re-forecast future
quarters.

Focused validation ran 117 tests across company model forecasting, the mission
forecast lane, forecast model authority, and forecast reconciliation. The
tests cover an old invariant refusal followed by a new-spec digest, immutable
old model and forecast-line rows, append-only version linkage, same-identity
idempotence, authority head compare-and-set behavior, and unchanged human
checkpoint behavior.

Code integration is complete. Live acceptance remains pending as part of the
foundation closure deployment; this review performed no live writes or model
calls.
