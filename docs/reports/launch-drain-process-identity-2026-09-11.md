# Launch drain process identity repair

## Problem

`launch_drain.running_tickets()` treated any non-zombie process at a recorded
PID as the lane child. A stale research-plan ticket therefore treated a later
macOS `com.apple.geod` process that reused the PID as live and held deployment
until timeout.

## Contract

The drain remains read-only. It does not update tickets, signal processes, or
infer completion. For a running ticket:

- a dead or zombie PID is finished for drain purposes;
- a recorded command or process start time that definitively differs means the
  PID was reused and does not block;
- matching command identity blocks the drain;
- unavailable OS identity evidence remains conservative and blocks;
- legacy tickets without recorded command identity use a timezone-aware start
  time when available; otherwise they retain their prior liveness behavior.

Linux identity uses `/proc/<pid>/cmdline` and `/proc` start ticks. macOS uses
`ps -ww ... command` and `ps ... lstart`. Launchers capture `started_at`
before filesystem preparation and write the ticket after `Popen`, so process
birth must fall between `started_at` and the ticket file mtime, with one second
of BSD `ps` precision allowance. This preserves a genuinely slow launch while
rejecting a later PID reuse. Missing or malformed interval evidence remains
blocking. Ambiguous BSD rendering, including paths or arguments with spaces,
also remains blocking.

## Verification

`PYTHONPATH=src python3 -m unittest tests.test_launch_drain tests.test_lane_child_launcher tests.test_installer_controller_preflight tests.test_mission_sec_quarters`

53 tests pass. Coverage includes a real child, an exited zombie, a live reused
PID with different argv, matching argv with older and newer process starts,
the start-time precision boundary, a ten-second pre-Popen delay, Python
interpreter aliases, ambiguous paths with spaces, identity lookup failure,
timeout behavior, and read-only ticket preservation.

Ticket JSON and filesystem metadata come from one opened descriptor. `fstat`
before and after the read must agree on device, inode, size, mtime, and ctime;
otherwise the interval is unknown and the drain waits. This prevents pairing
an old ticket payload with metadata from a replacement generation.
