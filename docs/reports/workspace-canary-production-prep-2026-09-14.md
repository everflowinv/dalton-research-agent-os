# Workspace canary production preparation

This procedure prepares one empty, isolated workspace without reading from or writing to the legacy Dalton state. The preparation script is read-only: it verifies the release wheel hash, root separation, current port availability, and LaunchAgent label availability, then prints the exact commands for review. It does not create the fleet root, install a release, create a workspace, load services, or change Tailscale.

Use `/Users/everflow/.dalton` as the fleet root. The short path is required because Darwin Unix-domain socket paths may contain at most 103 bytes; the manager's real `ws-<24 hex>` slug produces a 96-byte canonical writer socket path under this root. The legacy root `/Users/everflow/Library/Application Support/Dalton` must remain outside every workspace and must not appear in `shared_readonly_paths`.

The R25f core has no required third-party runtime dependencies. Its packet's single wheel is therefore a complete offline wheelhouse for the core workspace, Cockpit, writer, and controller. Optional connector extras are deliberately absent. A connector that needs an optional parser must remain refused until its reviewed wheels and configuration are added to a later immutable release.

Generate the review plan:

```sh
SOURCE=/Users/everflow/Projects/dalton-workspace-canary-prep
PACKET=/Users/everflow/Projects/dalton-owner-activation-20260910/foundation-r25f-release
BOOTSTRAP_PYTHON="/Users/everflow/Library/Application Support/Dalton/runtime/venv/bin/python"

"$BOOTSTRAP_PYTHON" "$SOURCE/scripts/prepare_workspace_canary.py" \
  --host-root /Users/everflow/.dalton \
  --wheel "$PACKET/dalton_core-0.1.0.dev0-py3-none-any.whl" \
  --wheel-sha256 6da4cb84caeca9dc7738c5157428f493b02f411bf972c3bbc724304854a15f65 \
  --bootstrap-python "$BOOTSTRAP_PYTHON" \
  --slug blank-canary --port 8794 \
  --owner-login '<TAILSCALE_LOGIN>' \
  --tailscale-host everflowdemac-mini.taild2c767.ts.net \
  --tailscale-executable /opt/homebrew/bin/tailscale \
  --launch-agents-dir /Users/everflow/Library/LaunchAgents \
  --uid "$(id -u)"
```

Add only reviewed external broker socket/key paths with repeated `--shared-readonly-path`. Add owner-published model and connector capacity binding files with the corresponding repeated options. A capacity binding shares quota authority; it does not copy credentials, provider configuration, connector governance, missions, approvals, or research records.

Review and execute the emitted phases one at a time. Re-run the process `plan` immediately before install and start because port availability is time-sensitive. Before publishing HTTPS, fetch the Cockpit on loopback and verify that its workspace UUID, manifest hash, release ref, and `awaiting_mission` state match the new manifest. Save `tailscale serve status --json`, publish the new port, and confirm that all pre-existing handlers are byte-for-byte unchanged and the new handler proxies only to `127.0.0.1:8794`.

The expected URL is `https://everflowdemac-mini.taild2c767.ts.net:8794/`. This uses the node's existing MagicDNS name and a unique HTTPS port. No workspace-specific DNS name is created by the current tooling.

## Mission activation gap

The empty Cockpit is a status and control surface once a mission exists. Its `awaiting_mission` presentation hides the normal goal content, and the workspace setup path does not create or sign a first mission. The source tree has internal mission creation authority and test helpers, but no workspace-scoped owner workflow that composes, reviews, and signs the first mission from the empty Cockpit. Starting the services can prove isolation and reachability, but it cannot make the workspace research-ready.

Treat first-mission provisioning as a production gate. Add a workspace-bound owner command or Cockpit onboarding flow that accepts a reviewed mission document, writes it through the workspace writer authority, records the owner signature/approval, and proves the active mission belongs to the workspace UUID. Until that exists and is accepted, expose the canary only for empty-state QA and do not represent it as an operational research environment.
