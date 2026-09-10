# Cockpit browser review — in progress, 2026-09-10

The owner requested a systematic page/interaction review alongside renewed onboarding/vision gap analysis. The runtime recovery/budget release is frozen separately at `38721e4`; the UI fixes below belong to the next batch.

## Reproduced findings

1. At a 1200px desktop viewport the actual live overview has a 2282px document width. The main grid takes 1280px in addition to the sidebar, and long company/figure references overflow their columns. The right side is clipped. A separate Sol agent is repairing responsive sizing, local table scrolling and narrow-screen layout without hiding overflow at the body.
2. `loadApprovals()` replaced every approval card every 30 seconds. An unsubmitted rationale vanished on the next refresh, and a pending action could be recreated as enabled. The failed-request handler also re-enabled actions explicitly disabled by the server.
3. On this machine the configured Tailscale hostname failed DNS resolution during this review; the local authenticated service is healthy. Browser inspection uses a temporary loopback GET-only bridge to the owner's configured local Cockpit identity. The bridge refuses all POSTs. This is local UI verification, not proof of remote Tailscale access.

## Approval fix and validation

The page now retains unchanged cards by exact kind/ref/hash/evaluation identity. It reconciles their order without replacing their DOM nodes, retains a rationale when details of the same version change, and does not carry it into a different version. Pending cards remain disabled through refresh. Failed submissions restore each action's original disabled state. A request sequence prevents an older GET response from overwriting a newer list.

Real Chromium checks passed all six cases: draft/focus/selection preservation, pending refresh, disabled action preservation after failure, same-version detail changes, new-version draft separation, and out-of-order GET results. JavaScript syntax also passed. The reproducible browser check is `scripts/check_cockpit_approval_refresh.js`, for `playwright-cli run-code --filename`; it replaces page-local read/write functions with fixtures, restores them afterwards, and sends no live decision.

Next: finish responsive browser checks at desktop/mobile widths, inspect all five views and model/budget dialogs after the recovery deployment, and fix further reproducible behavior. Real mission/approval signatures remain with the owner.
