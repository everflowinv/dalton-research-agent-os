# R18 broker stopped-window readiness (read-only)

This review inspected the existing release helpers and the completed R17a
packet without changing live configuration, plugins, services, state, or
provider traffic. R17a used `successor-config-transition-0.4`, which proves
exact preservation of the OpenClaw configuration and reports zero external
mutations. That receipt cannot prove installation of a new broker artifact.

## Existing closed deployment contract

`execute_successor_stopped_window_candidate.py` requires one accepted candidate
manifest to bind the clean frozen source commit, wheel, full-suite evidence,
copied-state rehearsal, transition manifest, live snapshots, and the exact
rehearsed `scripts/` inventory through `successor_ops_binding`. Execution also
requires the manifest's explicit SHA-256. The existing owner authorization can
therefore be carried forward operationally without requesting it again, while
the R18 invocation must still name the newly accepted R18 manifest hash; an R17a
hash does not authorize different bytes.

After live preflight and disk preflight, the outer R11a shell stops the Dalton
services, proves singleton/drain completion, and creates a fresh owner-only
rollback directory. It copies and hashes the venv, complete config directory,
selected state metadata, and LaunchAgent plists; then it creates a database
snapshot and verifies a restore. These steps are the required backup boundary
before any broker or runtime mutation.

`openclaw_broker_stopped_window.apply_reviewed_transition` adds a second closed
boundary while Dalton is stopped. It requires:

- a clean source checkout at the transition's exact commit;
- exact reviewed OpenClaw before bytes and an exclusive operation lock;
- no loaded Dalton services, no active child tickets, and an exact hash-bound
  historical unresolved broker journal;
- managed plugin source and packet copies with identical closed tree inventories,
  hashes, commit, destination, plugin id, and version;
- a gateway stop followed by repeat config/journal/stopped-state checks;
- inode-and-byte compare-and-swap of reviewed OpenClaw bytes, gateway restart,
  both broker sockets ready, and exact loaded plugin root/version/status checks.

Its receipt binds the source commit, transition content hash, before/after
OpenClaw hashes, unresolved journal hash/count, managed plugin trees and loaded
versions, before/after gateway identities, zero model calls, and explicit
`retry_authorized=false` / `refund_authorized=false`. Failure recovery and
rollback restore only reviewed bytes and refuse journal or concurrent config
drift.

## Concrete R18 gap

The stopped-window helper delegates transition validation to
`expected_openclaw_frame_transition_state`, whose current `0.3` schema permits
at most one managed plugin and validates it specifically as
`dalton-openclaw-web-search-broker` from
`integrations/openclaw-web-search-broker`. The R17a/R18 `0.4` pure-preserve
schema permits no external mutation at all. Consequently, neither current path
can truthfully prove installation of the production team's repo-owned model
broker required-control change. A pure-preserve runtime receipt may accompany
the release only as proof that unrelated OpenClaw/config authority was retained.

Before freezing R18, the production broker change needs reviewed preparation,
execution, rollback, and tests that extend the closed managed-artifact contract
to the model broker (or introduce an equivalently strict dedicated contract).
That contract must bind the model broker's repo source path, closed tree hash,
source commit, staged packet copy, exact destination, identity/version, and the
post-restart truthful required-control capability. It must also preserve the
existing unresolved-journal, no-retry/no-refund, socket readiness, CAS, and
rollback guarantees. A loose file copy or a capability inferred only from the
Dalton wheel is insufficient.

## Minimum stopped-window sequence

1. Freeze a clean R18 source containing the intended Dalton changes and the
   reviewed broker-control implementation. Build the wheel, run the full suite,
   and produce a copied-state rehearsal with an ops binding from the exact
   execution checkout.
2. Snapshot current model, service, OpenClaw, provider-plugin, mission, and other
   retained authorities. Stage a closed model-broker artifact in the private
   packet. Prepare the broker transition from those exact before bytes and the
   exact unresolved journal. If OpenClaw config bytes need no semantic change,
   the broker artifact operation must still be represented as a real managed
   artifact mutation; it cannot be hidden under pure-preserve.
3. Publish an accepted R18 candidate manifest that binds all artifacts and their
   hashes. Record the explicit R18 manifest hash used by the executor. This is a
   concrete binding of the existing deployment authorization, not a request for
   duplicate authorization.
4. Run read-only live preflight and disk-capacity preflight. Refuse any source,
   config, plugin, authority, journal, service, child-ticket, or ops-helper drift.
5. Enter one stopped window: capture initially loaded services, stop all Dalton
   labels, prove singleton/drain, then create and verify the fresh rollback copy
   and database restore.
6. While Dalton remains stopped, apply the reviewed broker artifact/config
   transition: lock, recheck, stop the gateway, recheck, install exact reviewed
   bytes by CAS/closed-tree operation, restart the gateway, require both sockets,
   and verify exact loaded model-broker identity/version/capability. Write the
   broker transition receipt before proceeding.
7. Install the reviewed Dalton wheel, apply the separate pure-preserve runtime
   transition, render/install exact LaunchAgents, and restart the previously
   loaded Dalton service set.
8. Verify wheel/runtime byte equality, all preserved configuration and authority
   hashes, broker artifact/tree/config/journal identity, service health, and zero
   deployment model calls. Then perform the normal bounded runtime and product
   acceptance observation. Do not interpret the deployment as permission to
   retry the historical IBM work.
9. On failure after broker mutation, stop/drain Dalton, run the hash-bound broker
   rollback first, then use the outer verified database/runtime/config/plist
   rollback and restore the captured service set. Preserve any concurrent
   unknown database and refuse unsafe owner-state/config drift as the existing
   shell does.

## Evidence required for acceptance

The private R18 packet should retain the accepted manifest and explicit hash;
clean source/ops bindings; wheel, full-suite and wheel-verification receipts;
copied-state rehearsal; exact before/after snapshots; closed model-broker source
and staged tree manifests; broker journal authority; fresh rollback inventory,
hashes, database snapshot and verified restore; broker apply/rollback-capable
receipt; successor transition receipt; installed-byte verification; gateway and
broker socket/plugin/capability proof; preserved authority hashes; health
samples; and product acceptance. Every broker receipt should continue to state
zero model calls and no retry/refund authorization.
