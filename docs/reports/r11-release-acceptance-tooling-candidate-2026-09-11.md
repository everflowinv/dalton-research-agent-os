# R11 release acceptance tooling candidate — 2026-09-11

## Boundary

The R11 source freeze is
`/Users/everflow/Projects/dalton-foundation-followup-r11-acceptance-worktree`
at `62f35597b2ca3c57d02bc06e688a2484829c1309`. The full-suite, wheel,
rehearsal and final activated-configuration hashes are deliberately unresolved
in the committed template. The helper can produce only
`candidate_pending_owner_acceptance`; it has no accept, publish, install, live
mutation, or manifest replacement operation.

The current provider bridge receipt records OpenClaw configuration SHA-256
`249124e3d372aaab5479a272f4901046fb04aec3339d3276711c2068edcb402e`.
That is an audit fact, not an R11 constant. The R11 candidate must hash the
final copied OpenClaw configuration after all authorized activation work is
complete. The same final snapshot decides whether the model-config inventory
contains 14 or 15 files; the candidate validator compares the supplied count
to the actual snapshot and never assumes either stage.

The frozen wheel build now reports wheel SHA-256
`cc7c9d2033745d41f9491493ef9c91401ea18d9f27cb1bd5f2647de98cfefea6`;
its verification receipt at preparation time has SHA-256
`eaa82fd651c78339cec1e8b50a09869d8c6a38245b41c08a8a86a502d4ab694a`.
The template still leaves their packet-local paths and hashes unresolved until
the owner copies the final artifacts into one packet and binds those exact
packet bytes.

## R10a helper changes required for R11

The R10a process remains useful for full-suite receipt checking, wheel/source
byte matching, copied-state rehearsal, stopped deployment, installed-byte
verification and sustained health. Its private release helpers need these
bounded changes before R11 acceptance:

1. Replace the R10a `FINAL` block with the one clean R11 source commit and the
   new full-suite, wheel, rehearsal and helper hashes. Do not carry R10a
   acceptance hashes forward.
2. Build the preservation baseline from one final activated configuration
   snapshot. Bind the exact model-config mapping, service configuration,
   OpenClaw configuration, installed provider plugin tree, mission authority, web-v6 activation
   receipt and provider-bridge activation receipt. Do not verify the current
   plugin by replaying the old R9a broker-capacity receipt or old plugin bytes.
3. Derive the model-config count and semantic hash from that snapshot. Alpha
   activation may make the count 15; no helper may keep R10a's fixed stage-14
   assertion.
4. Remove the R10a stager's `failed_r10_sources`, `failed_r10a_sources` and
   archive-copy publication. Historical receipts may remain historical text,
   but deleted payload archives are not release inputs and must not be copied
   or recreated.
5. Bind only the newest verified retained SQLite snapshot through its small
   manifest and the new retention receipt. The candidate contains one
   `latest_backup.snapshot_id`; it contains no archive inventory.
6. In the stopped window, run the reviewed backup-retention config helper after
   the R11 package is installed and before the controller starts. It compares
   the service-config bytes to the staged precondition, asks the newly installed
   R11 `ServiceConfig` parser to validate the result, changes only
   `backup.keep_latest` to `3`, retains the file mode, and writes an exclusive
   pending receipt. The postdeploy verifier must bind that receipt and verify
   every other service-config value, the final model-config inventory and the
   mission authority stayed unchanged.
7. Keep manifest acceptance and deployment publication in the separately
   reviewed owner stager. The repository candidate tool must not be promoted
   into that role.

## Candidate artifacts

`deploy/release/r11-acceptance-inputs.template.json` contains the known source
identity and explicit nulls for every result that does not exist yet.
`scripts/prepare_release_acceptance_candidate.py` checks a clean exact source,
closed packet-local paths, regular-file hashes, the dynamic model-config
snapshot, the latest retained backup, and the exact service-config delta. It
writes its output with exclusive-create semantics.

`scripts/configure_backup_retention_stopped_window.py` is the only proposed
R11 configuration mutation. It neither owns the stopped window nor starts a
service. The final deployment wrapper must supply the SHA-256 of the config it
staged and must stop if those bytes drift.

`scripts/run_release_copied_state_rehearsal.py` loads the exact frozen
rehearsal implementation, copies SQLite through its read-only backup path, and
does not invoke the historical 12-to-14 setup option. It compares the copied
configuration to a final pre-stop snapshot, applies the retention-only service
candidate inside scratch state, runs the ordinary migrations/writer/tick, and
then revalidates the dynamic 14-or-15 model inventory and service config. It
does not open the stopped live ModelRouter, so it does not depend on cold WAL
sidecars. Its log, report and candidate-only binding are exclusive outputs;
the scratch tree remains for the owner's handle/process audit and cleanup.

No acceptance candidate was filled, no live file was read by these tools, and
no deployment or manifest transition was run in this worktree.
