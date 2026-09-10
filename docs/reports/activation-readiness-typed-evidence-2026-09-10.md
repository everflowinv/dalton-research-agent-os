# Typed dossier evidence audit

The dossier contract permits ClaimVersions, filed statement/document figures, and forecast cells. Treating every evidence reference as a ClaimVersion falsely invalidated a dossier with legitimate filed numbers. The read-only audit now resolves Claims and figures in their respective immutable tables. An unversioned forecast-cell reference remains explicitly unverifiable (`valid: null`) because its reference does not identify an immutable model version; absence of such a binding is not silently accepted or reported as a missing Claim.

Regression: 11 activation-readiness tests passed (0.137s), including mixed Claim/statement/document citations, an unversioned forecast cell, and a missing figure. This is a wave-4 change, not part of the frozen d21de7d full-suite run. Next: integrate exact dossier input and DebateMap mission bindings with their read-only reconstruction checks.
