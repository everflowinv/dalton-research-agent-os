# Verifier route independence cross-review — 2026-09-10

## Finding and repair

The routing implementation resolves every producer route before budget
admission, fails closed for missing/rejected/unresolved routes, includes the
producer set in work identity, filters all producer families across fallback
links, and checks the whole family set again before a single-pin call is
charged. The signature-based compatibility path only omits the new keyword for
legacy callables whose declared signature does not accept it; it does not catch
or retry errors raised by the call.

One functional blocker remained. `industry_framework_cli` and
`deep_insight_gate_cli` use their own verifier functions, although their
independence predicates are shared with the dossier. The original change
updated the dossier verifier and its company-dossier caller only. The two other
production callers therefore supplied an empty producer list and were refused
before verification on every successful draft.

The repair wires `independent_model_call` into both lane-specific verifier
functions and passes every route accumulated by their draft loops. Missing
routes continue to fail closed. Existing post-call independence checks remain
as defense in depth.

## Validation

The combined focused suite covered the router/fallback implementation and all
six verifier flows, including the two repaired lanes:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_cockpit_model_fallback tests.test_model_fallback_chain \
  tests.test_company_dossier_draft tests.test_zero_base_review \
  tests.test_event_judgement tests.test_earnings_season_cli \
  tests.test_deep_insight_gate_lane tests.test_industry_framework \
  tests.test_industry_framework_draft tests.test_industry_framework_lane
Ran 421 tests in 10.362s — OK
```

No live configuration, routing policy, broker, deployment, or network state was
modified.
