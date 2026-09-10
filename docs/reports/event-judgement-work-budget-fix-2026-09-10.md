# Event judgement work-budget repair — 2026-09-10

## Failure

The first live post-authorization event batch considered eight events and
refused all eight before any model invocation. The route snapshots rejected
both brain profiles with `work_order_cost_budget_exceeded`: the lane declared a
60,000-token input reservation and 1,500-token output reservation under an
owner per-call cap of `$0.10`, although its prompt builder already limited the
actual prompt to 24,000 characters.

## Repair

The owner cap remains `$0.10`. Event prompts now have a 5,000-byte UTF-8 bound,
which is the same input bound declared by the WorkOrder. When context exceeds
the bound, the prompt retains complete context/evidence lines and the complete
final output contract; it marks omitted rows explicitly. It never slices a
UTF-8 character or evidence row. The closed event response is limited to 700
output tokens. At the current checked-in catalog prices, the worst-case brain
reservation is below the owner cap.

For the maximum 5,000-byte input and 700-token output declaration, the fixture
rate cards produce these conservative reservations: GPT-6 Astra `$0.085000`,
Claude Fable 5.1 `$0.085000`, ZAI GLM 5.3 `$0.010080`, and Gemini 3.5 Flash Lite
`$0.003250`.

The same bounds apply to judge, verifier, reflection, and reflection-verifier
calls. This keeps the sibling verifier feasible without weakening its family
independence check.

## Evidence

`PYTHONPATH=src python3 -m unittest tests.test_event_route_budget tests.test_event_judgement`
passed 87 tests in 4.373 seconds.

The route test synchronizes the current catalog fixture into a real
`ModelRouter`, publishes real brain and verifier policies, resolves their
credential slots, and routes maximum-sized event WorkOrders without invoking a
broker. It proves:

- a current priced brain profile is admitted below `$0.10`;
- a verifier is admitted below `$0.10`; and
- when the producer family is Anthropic, the Anthropic verifier is skipped and
  the ZAI verifier is selected.

No model, network, deployment, or live authority was called or changed.
