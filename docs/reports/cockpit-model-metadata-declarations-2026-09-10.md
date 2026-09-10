# Cockpit model metadata declarations — 2026-09-10

## Outcome

The owner can now declare a live model profile's family and capabilities from
the Cockpit model page. The action is a human-only writer operation. The page
submits the exact profile-version ref and hash it displayed; the writer checks
both while reading the current profile, then binds the declaration to its
actual provider and model. A caller cannot supply or spoof provider/model.

Declarations live in Dalton's append-only model-router authority. They do not
modify OpenClaw's host configuration and require no gateway reload. The catalog
sync overlays a declaration only when profile id, provider, and model all still
match, so a changed alias route cannot inherit an old family assertion.

The model page shows public family and capability metadata. An
`unclassified:*` family has a clear note that it cannot perform independent
verification. After publishing a declaration, the writer immediately runs the
configured catalog sync, reading the host and writing only Dalton. It reports
`applied` when the routable profile is updated or `pending_catalog_sync` when
this Core has no catalog lane configured.

The model selector also renders the writer's real `requires_restart` and
`reload_note` result. It no longer claims “next call” activation when a legacy
service pin was saved but still needs its service restarted.

## Governance and failure behavior

- The operation is present only in the human-governance allowlist and the
  authenticated actor is injected by the writer protocol.
- Retired and unknown profiles are refused.
- A stale displayed profile version or hash is refused without a declaration.
- Empty capability declarations are rejected before reaching the writer.
- The writer derives declaration version, prior reference, readable identity,
  provider, model, actor, and timestamp from current state.
- Repeating the same semantic declaration reuses the latest record rather than
  appending an unlimited chain.

## Validation

- 108 focused model-selection, catalog, router, governance, and HTTP tests
  passed.
- Tests exercise human-only registration, closed operation fields, exact live
  route and actor binding, stale/missing-profile refusal, semantic duplicate
  reuse, immediate catalog application, a subsequent no-broker route selecting
  the updated version, Cockpit writer dispatch, and invalid-input rejection.
- Extracted inline JavaScript passed `node --check`.
- `git diff --check` passed.

No host configuration, live Core, broker, network, or model was called.
