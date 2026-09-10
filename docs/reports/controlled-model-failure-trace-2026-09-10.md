# Controlled model failure trace

This change gives future event-judgement and company-dossier child runs a bounded link from a surfaced model transport failure to the Scheduler authorities that already govern controlled redrive.

`CockpitModelError.failure_trace` is present only after Scheduler has a formal failed result. It contains the purpose and unchanged base request ID, the WorkOrder reference and immutable WorkOrder hash, and the formal result-envelope hash. The values are read back from Scheduler authority after completion; prompts, model output, credentials, and error payloads are excluded. Calls that fail before a formal result exists carry no trace and cannot become eligible through this contract.

Event judgement and dossier drafting preserve these records in the owner-only child `summary.json` as `failed_model_traces`. Existing content validation refusals do not gain a trace. Existing summaries without the field remain valid and provide no recovery binding. The base request ID, prompt, WorkOrder identity, admission, settlement, and controlled-redrive suffix behavior are unchanged, so replay first consults the old formal result and only an exact approved controlled-redrive record can create the single suffix-bound recovery call.

This commit deliberately does not infer a lane-to-WorkOrder relationship for historical tickets. Per-company/group scheduling and held-lane consumption can use this trace in a later integration step; absence or hash drift must remain a hold.

Validation: 157 focused model/event/dossier unit tests passed before two nonexistent test-module names were reported by the test loader. The corrected focused lane/CLI selection ran 118 tests successfully. Python compilation and `git diff --check` passed.
