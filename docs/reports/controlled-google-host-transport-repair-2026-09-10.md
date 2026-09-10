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
of each known source anchor, rejects partial and duplicate patched states, checks
the candidate with `node --check`, replaces the bundle atomically, and is
idempotent. Its acceptance test copies the installed OpenClaw module and replaces
only its imports with explicit local seams. Node's permission model denies all
network access. The test proves that controlled calls skip the bound transport,
a refused admission stops after counting, an admitted call produces proof before
one generation, and an ordinary call still uses the bound custom transport. The
installed `@openclaw/ai` local-server suite separately proves Google countTokens,
schema refusal before generation, cost/token admission, and proof with zero paid
calls.

Deployment uses the existing managed patch runner after review:

1. Copy the patch script into `~/.openclaw/workspace/patch/`.
2. Add it to the OpenClaw 2026.9 `PATCH_NAMES` and `PATCH_SCRIPTS` arrays in
   `patch/apply_all.sh`, immediately after `patch_openclaw_202609_llm.py`.
3. Run it first with `--openclaw-root <managed-openclaw-root> --check`; a missing
   patch must exit nonzero.
4. Apply through `patch/apply_all.sh` with restart deferred, run the source tests
   plus the patch runner's local-provider transport test, then request the usual
   safe gateway restart only after active work drains.
5. After the eventual deferred restart, run `apply_all.sh --check`. No paid
   canary is needed for this repair; the hermetic copied-module and local-provider
   lifecycle tests are the acceptance evidence.

This commit does not copy the mutable patch runner into the repository and does
not alter the managed OpenClaw installation. The repository artifact is the
reviewable source of the new patch; the runner registration remains an explicit
deployment step because that workspace contains unrelated operator state.

The operational owner packet (kept outside version control) contains the
bounded registration and hash-checked rollback scripts. The single diagnostic
request incident is recorded there without credentials, request headers,
response content, or mutable configuration.
