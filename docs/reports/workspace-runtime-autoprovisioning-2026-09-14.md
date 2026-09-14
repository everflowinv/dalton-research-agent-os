# Independent research environments

Date: 2026-09-14

## Owner workflow

Open **研究环境**, enter a name, and choose **新建空白研究环境**. Creation installs the existing Dalton research engine and its local configuration. Enter a research goal, review the proposed companies, questions, sources and budget, then confirm publication. No per-environment model or connector setup is required.

Switching environments navigates to another Cockpit URL. It does not send a stop, pause or restart operation. Writer and controller processes continue when their page is closed. A failed creation can be retried with the same request ID; an environment that already began research is returned without resetting its state.

## Isolation and reuse

| Resource | Ownership |
| --- | --- |
| Core, scheduler, projection, model routing decisions, budget usage, Cockpit journal | Separate SQLite files per environment |
| Goals, companies, documents, claims, tasks, deliverables, approvals | Separate environment state; matching company IDs do not merge records |
| Writer token, writer socket, controller lock, logs, backups, task directories | Separate paths and process namespaces |
| Model broker and connector services, account connection references | Existing shared infrastructure |
| Provider capacity ledger, when explicitly configured | Shared account capacity; environment call and mission budget ledgers remain local |
| Dalton code and dependency packages | Shared immutable release |
| Model profiles, routing policies, research playbook and generic method defaults | Re-declared locally from pinned templates; no historic research or approval events copied |

The owner-authorized creation workflow registers local use of already connected sources. It does not enable unrelated external integrations or copy outbound message destinations. Mission publication remains an explicit confirmation in the new environment. Ambiguous goals remain reviewable drafts and identify the missing scope.

Budget usage stays local. Model call-cost ceilings use one host-owned `shared_call_budget_policy_path`, with a default of USD 1 and optional per-purpose overrides. Existing workspaces read the policy on every call, and templates pin the same policy path for future workspaces. An owner edit from Cockpit uses the host manager with compare-and-swap and a revision receipt; research writers only read the shared file. Token, timeout and per-run settings remain local. Shared provider rate limits still apply. Existing shared capacity policy bindings can be configured by the host operator and are propagated into new manifests; automatic creation does not create unlimited quotas or promise fair scheduling across provider accounts.

## Automatic setup chain

`workspace_manager` validates the owner-only manager configuration and exact template hashes, reserves a unique UUID/port, then runs:

1. `workspace_creation.create_blank_workspace`: local schema, token and namespace initialization.
2. `workspace_control_setup.configure_workspace_control`: base owner identity, local control configuration and bootstrap.
3. `workspace_model_setup.install_runtime_template`: the 21 existing model role configurations, local router declarations and budget policy declarations; broker socket/key remain shared references.
4. `workspace_runtime_setup.install`: generic research playbook, method foundation, tracking defaults, output policy placeholders and local connector governance.
5. `workspace_service_setup.install_service_template`: planner policy bound to the installed local model configuration, extraction settings, research review and document reading; incremental bootstrap adds the required managed principals while preserving existing token values.
6. Namespaced LaunchAgent installation, startup, Cockpit identity/blank-state checks, authenticated writer read RPC, controller heartbeat/tick checks, and a private Tailscale Serve route.

Initial goal planning uses a separately bounded workspace setup context. Confirmation travels through the normal writer governance operation `publish_first_workspace_mission`, which checks its own workspace and local foundation before creating the mandate, driver pack, constitution and coverage mission. Publication also binds local discovery selectors and dossier/framework policies to the new mission. SEC issuer resolution receives the configured operator identity explicitly. Subsequent execution uses the existing controller and research lanes.

## Host preparation

This is performed once by the operator, not by the person creating each environment. Export the current host model and operating definitions with:

```sh
python -m dalton_core.workspace_model_setup export --source-state <HOST_STATE> --output <MODEL_TEMPLATE>
python -m dalton_core.workspace_service_setup export --source-config <HOST_SERVICE_JSON> --output <SERVICE_TEMPLATE>
```

The owner-only manager configuration includes `runtime_templates.model` and `runtime_templates.service`, each containing an absolute `path` and the SHA-256 of that file. Model/service broker references must match. Source catalog and template references are pinned into the new manifest; request payloads cannot choose filesystem paths or commands.

Build the release once with an offline dependency wheelhouse and dependency lock. Pass both `--wheelhouse` and `--dependency-lock` to `dalton_core.workspace_release`. Schema 0.2 release identity includes the Dalton wheel hash and dependency lock hash; use the returned `release_ref` and `release_path`, rather than constructing a release reference from the Dalton wheel alone. Existing schema 0.1 releases remain readable. Never run pip inside an already inventoried release.

Use a short fleet root on macOS, such as `~/.dalton`, because Unix socket paths are length limited. Each environment gets its own process namespace and port. The manager adds one Tailscale Serve route and checks that existing routes were preserved.

## Verification

`scripts/run_workspace_runtime_acceptance.py` starts two real writer/controller process sets with independent state and the same test ticker. It uses the production LaunchAgent renderer and verifies research stage records, successful discovery tickets and discovered source documents in both databases, continuing A ticks while B is created/running, and rejection of cross-environment tokens, child paths and mission publication. Source discovery uses a local rehearsal subprocess with no external provider calls; deployment checks installed configurations, catalog synchronization and live processes. Provider connectivity is observed through governed mission runs.

Focused regressions cover template tampering, path escape, namespace mismatches, first-goal planning/publication/replay, blank-state behavior, release inventory and frontend JavaScript. Browser and installed-release results are recorded in the deployment receipt, separately from this source-level procedure.

## Retrieval and progress diagnostics

A discovered document is a candidate, not an acquired or fully read document. Check company/type/period relevance before spending retrieval calls; paginate provider bodies completely before marking acquisition complete. Use the OpenClaw scripts AlphaEngine MCP, with the company query and provider category (for example `Accenture` and `meeting_minutes`), rather than the retired CLI. Expired provider cursors require a fresh first-page request.

A scheduler tick alone does not prove forward progress. Inspect acquisition tickets, extraction failure reasons and read-completion proofs against the actual company checklist. Long connector work must yield the single writer thread within the caller budget and settle durable child tickets on later ticks. Budget changes must validate the complete mandate/constitution/mission authority chain as well as the paid-call ledger.

## Budget updates and recovery

A Cockpit research-budget save calls `set_research_budget_authority_chain`. It validates the expected mission hash and pool totals before updating the policy, mandate, constitution, mission, paid ledger policy and registered model/service bindings. Already spent amounts remain chargeable after the policy transition. These are recoverable, idempotent updates across stores, not a single database transaction; a retry reuses matching authority versions and requires the current mission hash. The file-binding helper restores prior bindings if a file write fails.

The shared single-call cost policy has a different scope: one host policy feeds every existing environment and future template. Per-environment daily budgets and expenditure remain independent. Local model cost fields are compatibility fallbacks; the shared resolver determines the effective per-call ceiling.

## Observed research progress on 2026-09-14

The active IT-services mission is version 19. Governed OpenClaw AlphaEngine retrieval and automatic authority reconciliation brought the earnings-call checklist to 4/4 for ACN, CTSH, EPAM and IBM. IBM's missing three quarters were fetched as complete two-page bodies; the normal controller subsequently registered the acquired mission rows and their review records without a manual tick or database edit. A provider search hit alone is not counted as acquired.

At this checkpoint, the current mission's newly acquired annual reports and transcripts still awaited complete reading proofs; the autonomous extraction lane was running. Historical mission-family proofs (for example IBM's 11 broker documents) are distinct from the active mission's newly completed readings. The `awaiting_human_extraction` storage state is also consumed by the automatic extraction lane; it does not by itself mean an operator must click to start reading.
