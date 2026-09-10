# Cockpit browser review — in progress, 2026-09-10

The owner requested a systematic page/interaction review alongside renewed onboarding/vision gap analysis. The runtime recovery/budget release is frozen separately at `38721e4`; the UI fixes below belong to the next batch.

## Reproduced findings

1. At a 1200px desktop viewport the actual live overview has a 2282px document width. The main grid takes 1280px in addition to the sidebar, and long company/figure references overflow their columns. The right side is clipped. A Sol agent prepared a responsive repair (`43d5be0`), but the owner subsequently deferred visual changes to a complete final redesign. That isolated commit is retained and is not merged or deployed.
2. `loadApprovals()` replaced every approval card every 30 seconds. An unsubmitted rationale vanished on the next refresh, and a pending action could be recreated as enabled. The failed-request handler also re-enabled actions explicitly disabled by the server.
3. On this machine the configured Tailscale hostname failed DNS resolution during this review; the local authenticated service is healthy. Browser inspection uses a temporary loopback GET-only bridge to the owner's configured local Cockpit identity. The bridge refuses all POSTs. This is local UI verification, not proof of remote Tailscale access.

## Approval fix and validation

The page now retains unchanged cards by exact kind/ref/hash/evaluation identity. It reconciles their order without replacing their DOM nodes, retains a rationale when details of the same version change, and does not carry it into a different version. Pending cards remain disabled through refresh. Failed submissions restore each action's original disabled state. A request sequence prevents an older GET response from overwriting a newer list.

Real Chromium checks passed all six cases: draft/focus/selection preservation, pending refresh, disabled action preservation after failure, same-version detail changes, new-version draft separation, and out-of-order GET results. JavaScript syntax also passed. The reproducible browser check is `scripts/check_cockpit_approval_refresh.js`, for `playwright-cli run-code --filename`; it replaces page-local read/write functions with fixtures, restores them afterwards, and sends no live decision.

Next: inspect functional behavior in all five views and model/budget dialogs after the recovery deployment. Real mission/approval signatures remain with the owner. Visual changes are deferred as described below.

## Latest owner direction: comprehensive design last

The owner explicitly requested that incremental visual fixes stop for now. After functional development and deployment/acceptance, redesign the complete Cockpit, including its page logic if useful. The requested direction is a polished, expressive, fancy light interface inspired by Apple, with the quality of a leading web designer. Research workflow must be clear and creatively presented: the objective, stages, current understanding, evidence, blockers, decisions and next actions should form a coherent product rather than a stack of operational tables.

This is a final whole-product design phase, not permission to conceal research failures or invent completed outputs. Functional repairs such as preserving rationale drafts continue now. The responsive patch and browser measurements are retained as diagnostic evidence for the later redesign, not as the visual direction to ship.

## Ask continuity repair

The question submitter retained a reference to its answer node, while returning to the Ask tab unconditionally replaced that node with history. The eventual answer then rendered into a detached node. A page refresh also showed a persisted running job without resuming polling.

History refresh now preserves the active submission and rejects late history responses while a new answer is running. Persisted running history resumes bounded polling while the Ask view is visible. Browser checks passed navigation during an active answer, visible completion, resumed history polling, and eventual result display. The page-local fixture check is `scripts/check_cockpit_ask_recovery.js`; no model or live POST was called.

## Source-base readiness projection correction

A checklist containing only `not_planned` or `source_unavailable` gaps previously reported `source_base_ready=true`, because readiness excluded those blocked categories. Both company and industry projections now require every required item to be complete. Existing separate `gaps` and `blocked_on` lists remain explanatory, and `research_state.ready_for_next_stage` no longer counts blocked companies as ready. Twenty-four mission-stage tests pass, including disconnected-source and unplanned-industry read projections followed by actual acquired-document completion. This change is after frozen `a108f0e` and is not yet deployed. The first verification command also named a nonexistent `tests.test_research_state`; the corrected module-only invocation passes.
