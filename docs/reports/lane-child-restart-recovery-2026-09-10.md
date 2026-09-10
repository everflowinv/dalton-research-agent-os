# Lane child restart recovery — 2026-09-10

`LaneChildLauncher` previously enforced one child per lane only through an
in-memory `Popen`. After a writer crash, a new launcher could overwrite the
same digest's ticket and log or start a different digest beside the surviving
child.

New tickets persist the exact child argv. Before spawning, the launcher scans
running tickets and verifies both PID liveness and process identity. Linux
uses `/proc/<pid>/cmdline`; macOS uses the BSD `ps` argv rendering, including
the framework Python executable normalization used by spawned Python children.
A matching same-digest request adopts and returns the existing ticket without
opening its log. A matching different digest raises the existing
`LaneChildConflict`. A dead PID or a live PID running another command marks the
stale ticket orphaned and does not block new work.

`status()` applies the same identity check to an adopted ticket. It therefore
cannot remain `running` forever if its original child exits and the operating
system reuses that PID for another command. Legacy tickets without recorded
argv retain their prior PID-liveness behavior because they contain no sound
process identity to compare.

The ticket remains the authority after adoption; no process handle is invented
and `close()` therefore never signals a child the restarted launcher did not
create. Tickets written before the argv field existed retain the prior orphan
handling. Managed deployment drains those legacy children before replacing the
runtime.

```text
PYTHONPATH=src python3 -m unittest tests.test_lane_child_launcher
Ran 18 tests in 0.231s — OK
```

The regressions use real sleeping subprocesses and verify same-digest adoption,
unchanged log content, different-digest exclusion, normal/dead completion
recovery, and rejection of a reused PID whose command does not match. No live
lane or model process was touched.
