# Pi agent-core conformance spike

This optional spike pins `@earendil-works/pi-agent-core` 0.84.1 and runs a
local fake provider. It does not call a model endpoint or use an API key. The
fake provider emits a tool call, Pi executes the deterministic formatter tool,
and the worker returns Dalton's strict `ModelInvocation + ResultEnvelope`
process frame.

It is deliberately outside the `dalton_core` Python package. Pi is a candidate
worker loop, not a Core dependency or authority store.

```bash
npm ci --ignore-scripts
PYTHONPATH=../../src python3 run_conformance.py
```

For the accepted spike, the Python `ProcessRuntimeAdapter` launches this worker
with a temporary cwd and a scrubbed environment. Pi still has no native
filesystem/process/network/credential sandbox; production execution of unknown
capability code requires a different OS identity, container/VM, or equivalent
service boundary.
