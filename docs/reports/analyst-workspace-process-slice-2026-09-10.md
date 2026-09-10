# Analyst workspace process slice

The macOS LaunchAgent renderer now accepts an optional validated workspace manifest and strict `space.lumos.dalton.workspace.<slug>` namespace. Legacy callers omit both and retain the original four labels and paths. Workspace agents inherit `DALTON_WORKSPACE_MANIFEST`; the renderer loads the hash-bound manifest and verifies that its state and service configuration paths are the paths being rendered. Writer lane children inherit the same environment without changing `HOME`.

`python -m dalton_core.workspace_process` provides `plan`, `install`, `start`, and `stop`. Planning validates the manifest, required executables in the immutable release, and local cockpit port before any mutation. Installation only renders workspace-owned plists and logs against the selected release; it does not create, upgrade, or modify a shared virtual environment. Start and stop address only the selected workspace's namespaced labels. The existing installer remains the backward-compatible legacy deployment path.

Tests render two workspaces from one release with distinct labels, state, configuration, sockets, and logs. A real two-process singleton test holds two independent controller locks, stops and restarts one, and verifies that the other PID and both token files remain unchanged. Focused validation passed 80 workspace, renderer, service, and lane-registry tests without live launchctl calls.

Fleet migration, live process changes, port publishing through Tailscale, policy approval, and release signing remain outside this slice.
