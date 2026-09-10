# Event judgement work-budget repair — 2026-09-10

## Failure

The first live post-authorization event batch was rejected before broker
invocation because the lane's old `$0.10` code default could not admit its
declared context at current catalog prices. The owner subsequently authorized
a `$1.00` per-call default while retaining the `$100` daily budget.

## Repair

Event calls now default to 60,000 UTF-8 input bytes, 1,500 output tokens,
`$1.00`, and 180 seconds. Model configuration can override the general call
budget and each of the four event purposes independently. Effective limits
govern prompt admission, WorkOrders, pool reservation, and the event contract
fingerprint.

Prompts retain the complete primary event and filing/month or HK/week group.
Verifier prompts also retain the complete producer draft, full event group,
canonical cited evidence records, and closed contract. Missing cited records
and prompts over the configured byte bound are refused before a call. All four
helpers turn prompt-construction failures into ordinary refused results, so a
single event cannot abort its batch and already incurred producer spend can be
settled.

The request namespace hashes the effective input, output, cost, and timeout
limits with the prompt-contract version, so a budget change produces distinct
work identity even when prompt text is unchanged.

## Evidence

`PYTHONPATH=src python3 -m unittest tests.test_call_budget tests.test_cockpit_plane tests.test_event_judgement tests.test_event_route_budget tests.test_mission_event_judgement_lane`
passed 155 tests in 23.559 seconds, with one existing optional test skipped.

The route test synchronizes the current catalog fixture into a real
`ModelRouter`, publishes real brain and verifier policies, resolves their
credential slots, and routes maximum-sized event WorkOrders without invoking a
broker. It proves:

- a current priced brain profile is admitted below `$1.00`;
- a verifier is admitted below `$1.00`;
- when the producer family is Anthropic, the Anthropic verifier is skipped and
  the ZAI verifier is selected.

No model, network, deployment, or live authority was called or changed.
