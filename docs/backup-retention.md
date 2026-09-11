# SQLite backup retention

Dalton backup retention is disabled unless the owner adds `keep_latest` to the
enabled `backup` block in `service.json`. The current three-snapshot policy is:

```json
{
  "backup": {
    "enabled": true,
    "root": "/absolute/state/dalton-core/backups",
    "interval_seconds": 86400,
    "keep_latest": 3
  }
}
```

Retention runs only after a new SQLite snapshot completes its existing backup
and integrity checks. Before deleting anything, it checks every completed
snapshot against its manifest and SHA-256 values, then runs SQLite
`PRAGMA integrity_check` on every database in the newest three. If three good
snapshots cannot be proved, it deletes nothing. Dot-prefixed work directories,
symlinks, malformed manifests, files with hash drift, and directories with
unmanifested content are retained and reported as skipped. Eligible older
snapshots are deleted directly; no archive copy is created.

Completed snapshots made from WAL-mode authorities may contain SQLite's
unmanifested `-wal` and `-shm` sidecars. Retention accepts only the closed safe
case: both files are present, owner-only and stable; the WAL is empty; and the
SHM has SQLite's bounded 32 KiB page shape. Integrity checks open the main file
with `immutable=1`, so they cannot create or consume sidecars. A non-empty WAL
holds the snapshot and prevents deletion.

Restore verification holds a shared lock on the snapshot manifest. Retention
must obtain an exclusive lock before deletion and, where `lsof` is available,
also refuses a snapshot opened by another process. It rechecks the full
directory inventory and all retained file signatures immediately before each
deletion. This keeps a restore, a changed retained copy, or a late unknown file
from being removed.

The heartbeat `backup.last_retention` record and the `dalton-backup prune` JSON
output include retained snapshot checks, skipped paths, deleted snapshot IDs,
deleted count, and logical bytes removed.
