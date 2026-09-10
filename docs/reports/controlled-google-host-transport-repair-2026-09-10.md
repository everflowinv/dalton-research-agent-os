# Controlled Google host transport repair

OpenClaw 2026.9.3 attached its custom Google stream transport to the prepared
Gemini model. `completeWithPreparedSimpleCompletionModel` preferred that bound
transport over the native `@openclaw/ai` transport. The bound transport made a
generation request without calling `countTokens` or producing the required
provider-control proof. The host then rejected its own result with “provider
controls were not enforced,” which the broker safely reduced to
`HOST_COMPLETION_FAILED`.

`integrations/openclaw_host_patches/patch_controlled_completion_transport.py`
changes only the transport choice. A completion carrying `providerControls`
uses an unbound copy with OpenClaw's default `@openclaw/ai` runtime, whose native Google transport
enforces the hash-bound schema, provider token count, input/output/total limits,
worst-case cost, pinned output allowance, and proof. Ordinary completions retain
the pre-bound transport. No proof check or budget is relaxed.

The patch refuses every OpenClaw version except `2026.9.3`, requires exactly one
known source anchor, is idempotent, and supports `--check`. Its behavioral test
places the network counter in the pre-bound transport itself. It proves that a
controlled call never invokes that dependency, that a refused admission stops
after the count boundary, and that an admitted call produces a proof before one
generation.

Deployment uses the existing managed patch runner after review:

1. Copy the patch script into `~/.openclaw/workspace/patch/`.
2. Add it to the OpenClaw 2026.9 `PATCH_NAMES` and `PATCH_SCRIPTS` arrays in
   `patch/apply_all.sh`, immediately after `patch_openclaw_202609_llm.py`.
3. Run it first with `--openclaw-root <managed-openclaw-root> --check`; a missing
   patch must exit nonzero.
4. Apply through `patch/apply_all.sh` with restart deferred, run the source tests
   plus the patch runner's local-provider transport test, then request the usual
   safe gateway restart only after active work drains.
5. After restart, run `apply_all.sh --check` before any paid canary. A canary is
   separate authorization and must retain the exact verifier controls.

This commit does not copy the mutable patch runner into the repository and does
not alter the managed OpenClaw installation. The repository artifact is the
reviewable source of the new patch; the runner registration remains an explicit
deployment step because that workspace contains unrelated operator state.
