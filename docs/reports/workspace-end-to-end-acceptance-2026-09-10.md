# Workspace end-to-end acceptance

The acceptance test installs one locally built fixture wheel through the immutable release authority, then creates and bootstraps two workspaces with distinct UUIDs, ports, state directories, sockets, and token files. Each workspace explicitly configures its loopback Cockpit for a test owner. No Tailscale route is published.

Three deterministic processes per workspace are launched from the final content-addressed release path with the validated workspace manifest environment. The control fixture serves a real loopback HTTP response containing its workspace UUID and the honest blank state `awaiting_mission`. Both workspaces respond independently. Stopping writer, controller, and control for workspace A and restarting its controller leaves workspace B's controller PID and token bytes unchanged. A complete post-run release receipt verification confirms that process execution did not change the shared release.

This is a temporary-directory acceptance with deterministic transport stubs. It proves the release, workspace, bootstrap, control configuration, process isolation, HTTP, stop/restart, and immutable-file boundaries. It does not create a live fleet, install LaunchAgents, publish Tailscale mappings, sign governance, or claim that external connectors or models ran.
