# HK weekly buyback judgement and incremental grouping

Date: 2026-09-10  
Baseline: `309637f`  
Branch: `hk-weekly-judgement-incremental`

## Result

HKEX next-day buyback disclosures remain immutable daily `buyback_disclosure` events. The judgement scheduler now treats their payload `cluster_key` ISO week as the reasoning unit, scoped by company. An HK week remains raw and unjudged while that ISO week is open in `Asia/Hong_Kong`; it becomes eligible after the week closes. This is scheduler behavior only and does not change the live HK universe, tracking policy, acquisition cadence, or owner decisions D1–D9.

The first judgement for a closed week binds every daily event then present to the producer and verifier prompts. One event carries the paid judgement and the other newly unjudged rows receive zero-cost `grouped_judgement` aliases, preserving the existing cost and outcome de-duplication contract used by US monthly rows.

A late HK disclosure for an already judged closed week creates a new incremental judgement. Its prompt evidence expands to the complete company/week set, including previously judged daily rows, while aliases are written only for newly unjudged siblings. The model request identity contains a deterministic hash of the sorted full evidence set, so the additional day cannot replay the earlier partial-week WorkOrder. Different companies and different ISO weeks remain separate slots.

US issuer-purchases grouping remains keyed by accession and retains its existing monthly table wording and behavior. HK prompts name daily rows and include filed date, shares, average price, and aggregate total in both producer and verifier inputs.

## Validation

Focused validation ran 288 tests successfully:

`PYTHONPATH=$PWD/src python3 -m unittest tests.test_mission_event_judgement_lane tests.test_event_judgement tests.test_buyback_disclosure tests.test_hkex_filings_adapter tests.test_judgement_outcome`

The new real `run_judgement` regression proves that two current-week daily events make zero model calls, two closed-week days make one producer/verifier pair, and a late third day makes one new pair whose two prompts contain all three event refs and whose WorkOrder differs from the original. It also checks raw-event preservation, zero-cost alias attribution, total paid cost, and company/week isolation. `py_compile` and `git diff --check` passed for the changed modules.

The repository-wide suite remains owned by main integration.
