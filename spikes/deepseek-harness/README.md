# DeepSeek Harness handshake spike

This optional probe pins the official Python SDK/runtime 0.1.0rc6 and measures
the credential-free stdio JSON-RPC `initialize` handshake. It sends no prompt
and makes no model request.

The probe verifies the SDK/subprocess seam only. DeepSeek Harness does not
natively consume `WorkOrder` or emit `ResultEnvelope`; a future adapter would
still have to project the prompt, collect session events and usage, and return
the same strict process frame used by the native and Pi workers.

The official runtime remains a developer preview and must not receive Dalton's
DB path or authority credentials.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
runtime_root=$(mktemp -d)
.venv/bin/python handshake.py "$runtime_root"
```
