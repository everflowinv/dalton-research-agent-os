# Cockpit model metadata declarations — 2026-09-10

## Outcome

The owner can now declare a live model profile's family and capabilities from
the Cockpit model page. The action is a human-only writer operation. Its input
contains only `profile_id`, `family`, and `capabilities`; the writer resolves
the current live profile and binds the declaration to its actual provider and
model. A caller cannot supply or spoof that route identity.

Declarations live in Dalton's append-only model-router authority. They do not
modify OpenClaw's host configuration and require no gateway reload. The catalog
sync overlays a declaration only when profile id, provider, and model all still
match, so a changed alias route cannot inherit an old family assertion.

The model page shows public family and capability metadata. An
`unclassified:*` family has a clear note that it cannot perform independent
verification. After publishing a declaration, the page explains that the next
catalog sync applies it; until then verification continues to fail closed.

## Governance and failure behavior

- The operation is present only in the human-governance allowlist and the
  authenticated actor is injected by the writer protocol.
- Retired and unknown profiles are refused.
- Empty capability declarations are rejected before reaching the writer.
- The writer derives declaration version, prior reference, readable identity,
  provider, model, actor, and timestamp from current state.
- Repeating an identical derived declaration identity is idempotent in the
  authority; changed content under the same identity conflicts.

## Validation

- Model selection, governance, Cockpit, and HTTP action tests: 78 passed.
- Tests exercise human-only registration, closed operation fields, exact live
  route and actor binding, missing-profile refusal, Cockpit writer dispatch,
  and invalid-input rejection before dispatch.
- Extracted inline JavaScript passed `node --check`.
- `git diff --check` passed.

No host configuration, live Core, broker, network, or model was called.
