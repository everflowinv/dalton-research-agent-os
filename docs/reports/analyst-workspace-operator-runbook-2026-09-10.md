# Analyst workspace operator runbook

Date: 2026-09-10

This runbook describes the workspace tooling in the accepted source tree. It is an operator procedure, not a deployment receipt. Current acceptance used temporary directories and deterministic subprocess/transport stubs. It did not create or run a real multi-workspace fleet, contact model or data providers, publish Tailscale routes, or sign a mission.

## Preconditions and placeholders

Run every command from a trusted shell with the accepted Dalton wheel and all of its dependencies already present locally. The immutable release installer invokes pip with `--no-index --find-links <wheel-directory>`; therefore `<WHEELHOUSE>` must contain the Dalton wheel and every required dependency wheel. Do not download dependencies during this procedure.

Replace every angle-bracketed value below. Do not reuse another workspace's UUID, port, state directory, token file, mission, or signed capacity policy.

```sh
HOST_ROOT=<OWNER_LOCAL_FLEET_ROOT>
WHEELHOUSE=<OFFLINE_WHEELHOUSE>
WHEEL="$WHEELHOUSE/<DALTON_WHEEL_FILENAME>.whl"
WHEEL_SHA256=<LOWERCASE_WHEEL_SHA256>
PYTHON=<TRUSTED_BOOTSTRAP_PYTHON>
LAUNCH_AGENTS_DIR=<OWNER_LAUNCH_AGENTS_DIRECTORY>
OWNER_LOGIN=<OWNER_LOGIN>
TAILSCALE_HOST=<WORKSPACE_TAILSCALE_HOST>
TAILSCALE_EXECUTABLE=<ABSOLUTE_TAILSCALE_EXECUTABLE>
UID_VALUE=<OWNER_NUMERIC_UID>
```

Verify the digest before installation:

```sh
shasum -a 256 "$WHEEL"
```

The output must equal `<WHEEL_SHA256>` exactly.

## Install one shared immutable release

```sh
"$PYTHON" -m dalton_core.workspace_release \
  --host-root "$HOST_ROOT" \
  --wheel "$WHEEL" \
  --wheel-sha256 "$WHEEL_SHA256"
```

Record `release_path` and `release_ref` from the JSON result:

```sh
RELEASE_PATH=<RETURNED_RELEASE_PATH>
RELEASE_REF="release:sha256:$WHEEL_SHA256"
RELEASE_PYTHON="$RELEASE_PATH/bin/python"
```

The installer creates a content-addressed virtual environment, verifies the installed `dalton_core` payload byte-for-byte against the wheel, and writes an integrity inventory. Workspaces reference this shared release read-only; operators must not edit files under `RELEASE_PATH`.

## Prepare explicit shared provider capacity bindings

Each model-provider account shared by multiple workspaces needs an owner-published capacity policy and a binding JSON file. The binding is not inferred from a credential name. Its closed shape is:

```json
{
  "database": "<HOST_ROOT>/fleet-capacity/<MODEL_CAPACITY_DB_FILENAME>",
  "policy_ref": "<EXACT_ACTIVE_POLICY_REF>",
  "policy_hash": "<EXACT_ACTIVE_POLICY_SHA256>",
  "provider": "<PROVIDER_ID>",
  "credential_slot_ref": "<CREDENTIAL_SLOT_REF>",
  "scope_ref": "<OWNER_DECLARED_SCOPE_REF>",
  "account_ref": "<OWNER_DECLARED_ACCOUNT_REF>"
}
```

Create one file per provider/account binding, mode `0600`, outside every workspace root. A workspace may repeat `--shared-model-capacity-binding` for multiple providers. Two workspaces that consume the same provider account must point to the same capacity database and owner-declared account scope, with exact active policy refs and hashes. Do not copy credential tokens into this file.

Connector capacity is also explicit in the workspace manifest (`shared_connector_capacity`). The current `dalton-workspace create` CLI does not expose a connector-binding flag. Do not hand-edit the hashed manifest. Until an operator-facing creator supports that field, create only workspaces whose connector capacity needs are represented by the approved creation workflow; otherwise stop and record that the workspace is not ready for governed connector dispatch.

## Create two isolated workspaces

Choose unique slugs and unused loopback ports. The creation authority generates a new canonical UUID for each workspace:

```sh
WS_A_PORT=<UNUSED_LOOPBACK_PORT_A>
WS_B_PORT=<UNUSED_LOOPBACK_PORT_B>
BINDING_A=<MODEL_CAPACITY_BINDING_JSON_A>
BINDING_B=<MODEL_CAPACITY_BINDING_JSON_B>
```

The CLI currently generates a UUID itself. Omit any attempt to force or copy one; record the returned UUID for each workspace.

```sh
"$RELEASE_PATH/bin/dalton-workspace" create \
  --host-root "$HOST_ROOT" \
  --slug <WORKSPACE_SLUG_A> \
  --cockpit-port "$WS_A_PORT" \
  --release-ref "$RELEASE_REF" \
  --release-path "$RELEASE_PATH" \
  --shared-readonly-path "$RELEASE_PATH" \
  --shared-model-capacity-binding "$BINDING_A"

"$RELEASE_PATH/bin/dalton-workspace" create \
  --host-root "$HOST_ROOT" \
  --slug <WORKSPACE_SLUG_B> \
  --cockpit-port "$WS_B_PORT" \
  --release-ref "$RELEASE_REF" \
  --release-path "$RELEASE_PATH" \
  --shared-readonly-path "$RELEASE_PATH" \
  --shared-model-capacity-binding "$BINDING_B"
```

Record each returned `manifest`, `workspace_id`, and `content_hash`. Confirm the IDs, ports, workspace roots, and derived database paths differ:

```sh
MANIFEST_A="$HOST_ROOT/workspaces/<WORKSPACE_SLUG_A>/workspace.json"
MANIFEST_B="$HOST_ROOT/workspaces/<WORKSPACE_SLUG_B>/workspace.json"
"$RELEASE_PATH/bin/dalton-workspace" validate --manifest "$MANIFEST_A"
"$RELEASE_PATH/bin/dalton-workspace" validate --manifest "$MANIFEST_B"
```

## Bootstrap each workspace

Manifest paths determine the canonical state and config paths:

```sh
STATE_A="$HOST_ROOT/workspaces/<WORKSPACE_SLUG_A>/state/dalton-core"
CONFIG_A="$HOST_ROOT/workspaces/<WORKSPACE_SLUG_A>/config/service.json"
STATE_B="$HOST_ROOT/workspaces/<WORKSPACE_SLUG_B>/state/dalton-core"
CONFIG_B="$HOST_ROOT/workspaces/<WORKSPACE_SLUG_B>/config/service.json"

DALTON_WORKSPACE_MANIFEST="$MANIFEST_A" \
  "$RELEASE_PATH/bin/dalton-bootstrap" \
  --state-dir "$STATE_A" --config "$CONFIG_A" --workspace-manifest "$MANIFEST_A"

DALTON_WORKSPACE_MANIFEST="$MANIFEST_B" \
  "$RELEASE_PATH/bin/dalton-bootstrap" \
  --state-dir "$STATE_B" --config "$CONFIG_B" --workspace-manifest "$MANIFEST_B"
```

Bootstrap creates separate Core, Scheduler, projection and model-router databases plus separate writer sockets and token files. It does not authorize research, copy a prior mission, or create a signed mission for the new workspace.

## Configure each owner's Cockpit

This is an explicit local configuration write. Use the owner identity and host allocated to that workspace:

```sh
DALTON_WORKSPACE_MANIFEST="$MANIFEST_A" \
  "$RELEASE_PYTHON" -m dalton_core.workspace_control_setup \
  --manifest "$MANIFEST_A" --owner-login "$OWNER_LOGIN" \
  --tailscale-host <TAILSCALE_HOST_A> \
  --tailscale-executable "$TAILSCALE_EXECUTABLE"

DALTON_WORKSPACE_MANIFEST="$MANIFEST_B" \
  "$RELEASE_PYTHON" -m dalton_core.workspace_control_setup \
  --manifest "$MANIFEST_B" --owner-login "$OWNER_LOGIN" \
  --tailscale-host <TAILSCALE_HOST_B> \
  --tailscale-executable "$TAILSCALE_EXECUTABLE"
```

The result says `tailscale_published: false`; configuration alone does not publish a route. A new workspace remains `awaiting_mission` until its owner follows the normal governance process and signs a workspace-specific mission. Never copy a token file or signed mission from another workspace.

## Plan, install, start, and stop

Plan is read-only and checks the release, required executables, manifest, and port availability:

```sh
DALTON_WORKSPACE_MANIFEST="$MANIFEST_A" \
  "$RELEASE_PYTHON" -m dalton_core.workspace_process plan \
  --manifest "$MANIFEST_A" --launch-agents-dir "$LAUNCH_AGENTS_DIR"
```

Review the returned UUID, manifest hash, release ref, port, labels, and plist destination. Then install namespaced LaunchAgent files:

```sh
DALTON_WORKSPACE_MANIFEST="$MANIFEST_A" \
  "$RELEASE_PYTHON" -m dalton_core.workspace_process install \
  --manifest "$MANIFEST_A" --launch-agents-dir "$LAUNCH_AGENTS_DIR"
```

Start only after the workspace-specific governance, provider bindings, and configuration have been reviewed:

```sh
DALTON_WORKSPACE_MANIFEST="$MANIFEST_A" \
  "$RELEASE_PYTHON" -m dalton_core.workspace_process start \
  --manifest "$MANIFEST_A" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --uid "$UID_VALUE"
```

Stop drains lanes before unloading the writer:

```sh
DALTON_WORKSPACE_MANIFEST="$MANIFEST_A" \
  "$RELEASE_PYTHON" -m dalton_core.workspace_process stop \
  --manifest "$MANIFEST_A" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --uid "$UID_VALUE"
```

Repeat the plan/install/start sequence for workspace B with `MANIFEST_B`. Do not reuse A's manifest environment while operating B.

## Read-only fleet inventory

```sh
"$RELEASE_PYTHON" -m dalton_core.workspace_fleet --host-root "$HOST_ROOT"
```

The inventory validates each registered manifest, rejects duplicate UUIDs or ports, checks service binding when configured, and reports capacity configuration state. It is an inventory, not evidence that products were generated or that external providers succeeded.

## Legacy installation boundary

There is no in-place migration of the legacy live runtime. The only legacy command is a dry-run plan:

```sh
"$RELEASE_PATH/bin/dalton-workspace" dry-run-migrate \
  --host-root "$HOST_ROOT" \
  --legacy-state-dir <LEGACY_STATE_DIR> \
  --legacy-config <LEGACY_SERVICE_CONFIG> \
  --slug <PROPOSED_NEW_SLUG> \
  --cockpit-port <PROPOSED_NEW_PORT> \
  --release-ref "$RELEASE_REF" \
  --release-path "$RELEASE_PATH"
```

It performs no writes and describes a future stop, copy, verify, namespaced install, and owner-acceptance process. Do not treat it as authorization to migrate, overwrite, or remove legacy live state.

## Source verification performed for this runbook

The command shapes above were checked against `workspace_release.py`, `workspace.py`, `bootstrap.py`, `workspace_control_setup.py`, `workspace_process.py`, `workspace_fleet.py`, and the console-script entries in `pyproject.toml`. Help inspection is read-only; no workspace, release, LaunchAgent, mission, token, or live database was created or changed while preparing this document.
