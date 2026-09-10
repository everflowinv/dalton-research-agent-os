# Investment Memo producer — 2026-09-10

## Delivered boundary

This slice adds the automated candidate producer. It does not add a new document or decision authority. A successful run publishes `kind=investment_memo` through `MissionDeliverableAuthority`; the separate human decision and stage transition remain Slice C.

The lane becomes eligible only when the folded mission ladder says the company's next stage is `investment_memo`, the mission grants `deliverable`, the memo is a human checkpoint, and the current input includes an owner-approved Deep Insight Gate plus dossier, industry framework, forecast model, sensitivity, and valuation records. Current Claims, DebateMap, price, consensus, catalyst, events and judgements, reflections, and prior model records join the frozen input when present. Every row contributes an exact ref/hash binding. An unchanged binding returns `nothing_new` before a model is constructed.

The bound Playbook supplies two distinct sets:

- its 12 `deliverable_templates.investment_memo` section titles;
- its 12 `key_questions`, assigned stable `memo_q01`…`memo_q12` refs.

Four producer calls draft the prescribed section/question groups. They receive the complete frozen input without arbitrary slicing. Output parsing is closed-shape and refuses reordered/missing sections, changed questions, non-Claim section references, unknown input references, and unbound numbers. `MissionDeliverable.validate_section` then applies its live Claim, computed-cell, and unsourced-number rules before verification.

One verifier call reads the complete assembled memo, all 12 structured answers, and the complete cited input. `independent_model_call` supplies every producer route before routing or budget admission. A reject, malformed response, unknown answer, failed deterministic check, or inability to reserve the configured four producer calls plus verifier publishes nothing.

## Durable verification contract

`investment_memo_contract.py` is shared with the human decision slice. The stored gate binds the memo sections, summary, mission/playbook/template, all questions, and every input ref/hash. It requires:

- exact bound Playbook question refs and text;
- four fixed passing checks (`key_questions_complete`, `variant_consensus`, `anti_thesis`, `risk_reward`);
- four fixed, unique producer groups with unique WorkOrder and RouteDecision refs;
- producer and verifier result-envelope and invocation refs;
- a distinct verifier WorkOrder/route, `pass`, no findings, and the exact canonical material hash.

The five WorkOrder refs are also stored in the existing `model_invocation_refs` field. CockpitModel returns these fields only after a formal successful scheduler result. Slice C can re-read the named result envelope, parse its closed verifier output, and compare its `verified_body_hash` before a human stage decision.

## Configuration and deployment

Purposes `investment_memo` and `investment_memo_verifier` are registered as brain and verifier tiers and mapped to the existing dossier producer/verifier configuration pair. Defaults are centrally configurable: each call is 120,000 input tokens, 6,000 output tokens, $1, and 300 seconds; the atomic run is four units and $5. The run refuses when the configured cap cannot reserve all five calls.

The lane registry is the deployment wiring used by `macos_launchagent.lane_argv`. It emits both memo arguments only when both existing dossier configuration files are installed, including their supported legacy fallback paths. A half-installed pair leaves the lane absent.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_investment_memo_contract tests.test_investment_memo_draft tests.test_investment_memo_lane tests.test_lane_registry tests.test_model_selection tests.test_model_fallback_chain tests.test_call_budget tests.test_packaging tests.test_service`

Result: 205 tests passed. The focused tests cover complete untrimmed prompt material, strict references, body/input tampering, false values such as `None`, wrong Playbook questions, duplicate provenance, four producer calls followed by one verifier, verifier route propagation, zero publication on refusal, paired launcher arguments, central purpose selection, and package data.

No live files, signatures, model calls, or network requests were made.
