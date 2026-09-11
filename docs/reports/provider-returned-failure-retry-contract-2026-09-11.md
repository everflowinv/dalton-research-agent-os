# Returned provider failure retry contract

This slice adds an opt-in, closed retry policy to the routed transcript worker. Existing WorkOrders and workers omit `provider_retry` and retain their prior bytes and behavior.

A retry is authorized only by an exact adapter-authenticated `provider_completed_failure` dispatch proof, an exact allowlisted code, an executed broker response hash, and an invocation whose completion and result references agree. Unknown telemetry remains fully represented by normal conservative accounting; it is not confused with an unknown send/completion state. Exceptions, post-send timeouts, protocol failures, content refusals, arbitrary codes, and replay-only responses remain terminal.

Each physical retry is a new Scheduler attempt and ModelRouter decision, producing a new adapter invocation and accounting entry. Retry state and excluded profiles live in the immutable retryable ResultEnvelope, so worker restart does not reset the bound. The selected profile is required to remain identical during the configured same-profile retry count. Once exhausted, that profile is excluded and the next declared purpose-chain profile is selected on a later Scheduler attempt. This also gives budget-specialized workers a distinct `(work, attempt, phase)` admission boundary for every physical call.

The broker response adds a closed `dispatchProof` only to local capacity failures known to precede provider execution. The adapter validates that proof and exposes the consumer-facing `openclaw-model-adapter` authority. An error code alone never grants zero-cost retry authority. Pre-proof journal responses remain readable but carry no proof.

This first integration covers `RoutedTranscriptPolishModelWorker`. Other workers must opt in and bind the same policy into their WorkOrder identity before using the shared contract.

## Declared tier order on paid retries

The paid-retry path calls `ModelRouter.route` directly because every physical
provider call must have its own Scheduler attempt. It now passes the registered
tier for the worker purpose, matching the tier that `execute_chain` derives on
the initial non-retry path. The router therefore keeps the declared order for
the first call, the bounded same-profile retry, and the later fallback after
that profile is excluded. A missing purpose-to-tier registration fails closed.

The router's authorization and ordering rules remain separate. A tier chain
orders candidates that pass the policy filters; it does not authorize a profile
outside `filters.allowed_profile_ids`. Only an explicit purpose override has
the existing, deliberate exception to a legacy profile pin. The regression
fixture consequently authorizes both profiles and proves the declared sequence
`zz-preferred`, `zz-preferred`, then `test-transcript`, even though the policy's
global ascending preference would choose `test-transcript` first.

Verification covered the 64 contract/router/worker tests in
`test_provider_retry`, `test_model_router`, `test_model_fallback_chain`, and
`test_transcript_polish_model_worker`, plus all four real host bridge patch
tests. The transcript suite includes the real Unix broker returned-failure and
capacity-proof cases. All 68 tests passed.

## Production host boundary audit

The declaration for OpenClaw 2026.9.3 describes `llm.complete` as a success-only `Promise<LlmCompleteResult>`, but that declaration was not the complete runtime boundary. Inspection of the installed implementation found that `@openclaw/ai` projects returned provider errors into a terminal assistant result with `stopReason: "error"`, a structured numeric HTTP `errorCode`, and usage. The plugin LLM facade then discarded that structure by treating the empty content as an ordinary completion. This report corrects the earlier declaration-only conclusion.

The reviewed host patch preserves only an exact 429 or 5xx numeric-string status from that returned error result. It emits a versioned `provider_completed_failure` union with the already prepared provider/model/agent attribution and normalized raw usage. The broker independently validates the closed union and attribution and maps it to a closed dispatch proof. Error strings, thrown exceptions, DNS/timeout failures, 401/403 responses, malformed status values and all other host failures remain ineligible. They keep the conservative terminal/unknown accounting path.

The patch closes this boundary without interpreting exception messages. Its
installer must remain bound to the reviewed OpenClaw version and exact bundle
anchors alongside the existing controlled-transport patch. Although the host
normalizes available raw usage, the broker deliberately does not publish that
telemetry on a failed response under the current protocol. Dalton therefore
settles each returned provider failure at the full reservation before admitting
the next physical attempt. A future protocol may expose trusted failure usage,
but this retry contract does not assume it.
