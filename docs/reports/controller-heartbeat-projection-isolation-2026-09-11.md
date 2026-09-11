# Controller heartbeat projection isolation

On 2026-09-11 the R6 controller remained alive but one health check observed a
99.79-second-old heartbeat. A three-second sample of that same process showed
the main thread in TLS connect, handshake, and read calls made by the static
dashboard publisher. The preceding projection also took about 44.27 seconds
before publication began. Both operations ran before `run_once()` wrote its
heartbeat, so either could exceed the existing health freshness contract.
Historical ENOSPC lines in the shared stderr log were not evidence for this
incident; the current process had no recorded error and the volume had 3.1 GiB
available.

The repair runs one projection at a time in a dedicated worker. After the
controller observes its completion, it applies the returned watermark and the
captured pre-build source signature, then starts one static render/publish
worker. No new projection starts while that publisher is active. If authority
bytes changed during either operation, comparison with the captured signature
causes the next eligible tick to build a new projection. A failed old plugin
retry cannot start while a projection is writing its database.

The controller thread continues lease sweeping and heartbeat publication while
these workers run. A previously successful dashboard remains `ready` while a
replacement is built; the completed refresh changes it to `error` if it fails.
Initial startup keeps the plugin `pending` during projection and `running`
during its first render/publish; it becomes `ready` only after completion. Projection state and its error are now present in the heartbeat, and
a projection error also supplies the top-level `last_error` for a degraded
heartbeat.

`daltond --once` explicitly waits for projection, rendering, and publication,
preserving its complete-artifact output and exit behavior. Service shutdown
waits for running projection and plugin workers, matching the prior synchronous
completion boundary and preventing work from continuing after the service
context closes. The default health freshness limit remains 30 seconds; the
repair does not expand it.

Focused coverage is in `tests.test_service.ServiceTests`:

- `test_blocked_projection_does_not_block_heartbeat`
- `test_blocked_dashboard_publish_does_not_block_heartbeat`
- `test_projection_blocks_retry_of_old_failed_plugin`
- `test_failed_refresh_keeps_ready_until_failure_is_observed`
- `test_one_cycle_sweeps_projects_and_renders_without_an_llm`

The complete `tests.test_service` module passed 49 tests. Tests use local
blocking functions and temporary databases; they do not make external calls.
The private process sample and its receipt are retained under
`private/acceptance/r6-controller-health-20260911T0229Z` in the owner activation
packet.
