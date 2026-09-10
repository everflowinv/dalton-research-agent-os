# Event verifier contract-violation audit — 2026-09-10

## Finding

The seven verifier failures in run
`event-judgement-runs/3df55e69c7d6d1733a181a32` happened before any
broker request. They are deterministic Dalton admission failures, not Claude
content refusals and not malformed verifier answers.

Each persisted verifier WorkOrder has purpose `event_judgement_verifier` and a
route constrained by a producer family. Its metadata contains only
`control_plane`, `purpose`, `request_id`, and `mission_version_ref`.
`OpenClawModelAdapter._required_provider_controls()` requires every independent
verifier WorkOrder to carry `verifier_output_schema_version`. It raises
`ModelAdmissionError("independent verifier WorkOrder lacks the required output
schema version")` before `_exchange()`. The fallback classifier correctly maps
that admission exception to `contract_violation`; the chain then records one
unserved `profile:claude-fable-5-1` link and reports
`MODEL_CHAIN_EXHAUSTED` to Scheduler.

The evidence across the read-only live stores is consistent:

- all seven producer WorkOrders have successful formal results and broker
  journal records;
- all seven paired verifier WorkOrders have failed formal results and an
  unserved route-chain link with `skip_reason=contract_violation`;
- none of the seven verifier WorkOrder IDs occurs anywhere in the current
  1,000-record broker journal;
- the failure follows directly by passing the persisted WorkOrder and route
  shape to the adapter's pure provider-control admission function.

The first producer failure in the same run is separate. Its Astra broker record
has `ok=false`, `code=INVALID_HOST_RESULT`, and `message="host returned invalid
text"`. That call reached the broker; the seven verifier calls did not.

## Root cause and safe repair boundary

`CockpitModel.call()` knows that a call is an independent verifier from its
nonempty `producer_route_decision_refs`, but `build_work()` does not bind a
provider output contract into the WorkOrder metadata. Adding only the missing
version field is unsafe: the adapter currently selects
`thesis-impact-verifier-provider-output-v0.2.schema.json` for every verifier
without an explicit binding mode. That schema requires thesis assessment
fields and is not the Event Judgement verifier's output contract.

The repair therefore needs an Event Judgement provider-output schema and a
closed, trusted schema selector carried in immutable WorkOrder metadata. The
adapter must resolve only packaged allowlisted schemas, never a caller path or
arbitrary JSON. Work identity must include the selected contract metadata so a
repaired call cannot collide with the failed historical WorkOrder. A stub
transport regression should prove:

1. the event verifier reaches `_exchange()` with its exact packaged structured
   output controls;
2. the broker response matching that schema is accepted and the lane's
   semantic verifier parser still validates it;
3. a missing, unknown, or thesis schema selection fails before transport;
4. producer calls remain on the legacy no-provider-controls path;
5. no budget settlement is charged for admission failures.

No gate was weakened and no model, broker, live database, or live file was
called or modified during this audit.
