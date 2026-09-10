# Investment Memo scheduling fairness — 2026-09-10

The memo lane previously computed one signature from global table counts and
checked its hold before the child selected a company. A deterministic rejection
for the first company could therefore stop every later company, while unrelated
company activity could release the rejected input.

The coordinator now walks the active mission universe in its declared order and
calls the existing read-only `collect_frozen_input` path for each exact company.
That path retains the folded stage gate, signed memo/human-checkpoint grant,
bound playbook, approved Deep Insight Gate, and required dossier, forecast,
sensitivity, valuation, and framework checks. Only a `ready` company enters the
failure ledger or starts the child. Its signature covers the exact frozen input
bindings, current mission and playbook refs/hashes, and both model configuration
file hashes. The existing launcher passes `--company-ref`, so the child cannot
silently select another company.

A content refusal remains terminal for that exact company input. Other eligible
companies are still considered in the same tick. A broker-capacity refusal is
classified from the verifier's actual reason and retains the shared bounded
dependency-probe behavior. Missing prerequisites are reported as skipped and do
not consume retries or model budget. The launcher still provides the single
child slot.

Validation:

```text
PYTHONPATH=src:. python3 -m unittest \
  tests.test_investment_memo_lane tests.test_investment_memo_draft \
  tests.test_investment_memo_contract tests.test_investment_memo_decision \
  tests.test_investment_memo_decision_real tests.test_f14_whole_ledger_lanes -v
```

All 38 tests passed. The focused cases cover first-company content rejection,
second-company progress, all-held quiet behavior, persisted hold after restart,
company-scoped input changes, missing prerequisites, and recoverable broker
capacity. No live state, service, broker, or model call was used.
