# Verifier selection reinstall audit

Date: 2026-09-10

This offline review checked whether the eleven current product verifier purpose selections survive the setup calls repeated by `deploy/macos/install.sh`. It did not copy the live router, call a model, alter live configuration, or include the separately held thesis-impact verifier policy.

The install script delegates each configured role to `research_planner_setup.install`, which calls `ensure_planner_policy` for the same policy id. `ensure_planner_policy` reads the latest immutable policy version and carries its complete `purpose_overrides` mapping into any appended setup version. When the setup content is already identical, it returns the current version unchanged. Model configuration files receive the returned latest policy ref.

An offline Router regression published explicit `profile:gemini-3-8-flash` selections for:

- `debate_map_verifier`
- `conviction_call_verifier`
- `event_judgement_verifier`
- `thesis_reflection_verifier`
- `zero_base_review_verifier`
- `dossier_verifier`
- `deep_insight_gate_verifier`
- `industry_framework_verifier`
- `investment_memo_verifier`
- `earnings_preview_verifier`
- `earnings_calibration_verifier`

It then simulated a setup contract change through `ensure_planner_policy`, forcing a new immutable policy version. The new version pointed to the prior selected version, retained all eleven override wires byte-for-byte, and resolved every purpose to the selected Gemini profile. The existing single-purpose reinstall regression also covers the no-loss behavior independently.

Result: no reinstall blocker found in this path. This proves authority/config preservation in a temporary Router fixture. It does not assert that a live broker currently offers the profile, that its provider controls remain eligible, or that a model call will succeed; those are runtime catalog and admission checks.
