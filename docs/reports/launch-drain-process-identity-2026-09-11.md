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
`ps -ww ... command` and `ps ... lstart`. A process may precede its ticket by
up to three seconds because the ticket is written after `Popen` and BSD `ps`
reports whole seconds. A process born after the ticket is rejected. Ambiguous
BSD rendering, including paths or arguments with spaces, remains blocking.

## Verification

`PYTHONPATH=src python3 -m unittest tests.test_launch_drain tests.test_lane_child_launcher`

37 tests pass. Coverage includes a real child, an exited zombie, a live reused
PID with different argv, matching argv with older and newer process starts,
the start-time precision boundary, Python interpreter aliases, ambiguous paths
with spaces, identity lookup failure, timeout behavior, and read-only ticket
preservation.
