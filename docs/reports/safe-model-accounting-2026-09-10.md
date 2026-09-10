# Safe model transport accounting — 2026-09-10

## Result

The broker adapter now distinguishes a request that provably never reached `sendall` from a request whose completion is unknown. Socket-path validation and connect failures raise `BrokerDefinitelyNotSent`; timeout, protocol, connection loss after connect, and host completion failure remain uncertain. Cockpit fallback is permitted only for the former. An uncertain call halts the chain and conservatively settles the full reserved ceiling instead of recording zero cost.

`OpenClawModelAdapter.execute(..., before_send=callback)` provides an admission seam after a successful Unix-socket connection and before any request byte. Shared host capacity remains reserved during connect, is marked dispatched only after the callback accepts, and an undispatched reservation is cancelled at zero cost on connect or callback failure. Once dispatch begins, transport/protocol uncertainty retains the conservative shared reservation.

## Compatibility and limits

Legacy callers may omit `before_send`. Replay never invokes it. Broker `BUSY` remains a broker-local zero-cost retry result. This change does not infer actual provider cost after missing telemetry; it retains the already bounded maximum until authoritative reconciliation.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_openclaw_model_adapter tests.test_cockpit_model_fallback tests.test_shared_capacity tests.test_model_fallback_chain tests.test_cockpit_provider_boundary`

Result: 124 tests passed. A ResourceWarning-strict test also proves callback refusal closes the connected socket before sending.
