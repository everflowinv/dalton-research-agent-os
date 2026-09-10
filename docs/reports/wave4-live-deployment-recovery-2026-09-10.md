# Wave 4 live deployment recovery — 2026-09-10

The owner attempted the reviewed deploy/sign command. Deployment installed the accepted runtime and 67 schemas, then failed before catalog synchronization: extraction's cheap tier required `profile:zai-glm-5-3-flash`, which was absent in the old router. The subsequent `&& sign-mission.zsh` did not execute.

## Cause and fix

The actual shell installer configured extraction before synchronizing the dynamic broker catalog. Earlier activation rehearsals synchronized first, so they did not cover this production ordering. `67373c0ccb85a645e2d3ceed82dbfcf1f880a8d5` moves catalog sync before extraction. Shell behavioral regressions cover a stale router, synchronization failure stopping before extraction, and offline setup. Combined shell/extraction/deployment tests: 11 passed on development Python (1.647s), and 11 passed on installed Python 3.14 (1.297s).

All 433 Python/SQL/HTML runtime files remain byte-identical to the accepted `79e0ef7` runtime, whose 6,248-test result remains explicitly bound to that baseline. Only installer order and tests/docs changed; this report does not relabel the baseline full-suite run as a run of the new commit.

A read-only live-router backup confirmed that current broker synchronization adds the missing ZAI profile and every required tier member. Brain 2/2, cheap 3/3 and verifier 3/3 profiles resolved credential slots; a second synchronization was a no-op. No static profile seeding was restored.

## Recovery performed

The assistant retried the owner's already-authorized deployment with the corrected frozen installer and original accepted runtime bytes. Catalog synchronization, all role setup, plist installation and service startup completed. A second verified database backup was retained in the private packet (`deploy-backup-20260910T153709Z`); the owner's original backup remains `deploy-backup-20260910T153217Z`.

Final health initially failed because an unmanaged old controller (PID 56558, started September 9) was still running with the exact same service config and overwrote the new controller's heartbeat. It was outside launchctl's current job, so bootout had not stopped it. After verifying the exact process arguments and distinguishing it from managed PID 11278, the old process received SIGTERM; after it remained alive, only that verified old process was terminated. No database or artifact was deleted. The old controller's presence means the backup validation establishes each SQLite restore, not a globally quiescent multi-database snapshot.

Health then passed with current controller PID 11278: writer/control sockets ready, controller running, heartbeat fresh, plugins ready. The corrected archive is retained at the private packet's `.release-source.cPBnug`; the pointer was recorded only after source/runtime identity and final health were verified. The installer process itself had returned nonzero during stale-heartbeat checks; service health was recovered and independently rechecked afterward.

The private postdeploy check at `20260910T154026Z` confirms all 433 installed Python/SQL/HTML files match archive and reviewed wheel. Mission v13 remains active with its reviewed hash; v14 is pending the owner's signature. All 15 requested dossier/DebateMap/event-judgement products are still missing. There are 25 configured purposes, one disabled workflow referencing a retired profile, and six dynamically unconfigured purposes. Catalog sync configuration is now installed.

## Next step

The owner runs only `sign-mission.zsh`; another deployment is unnecessary for this recovery. Then verify real products and current routing after the new mission is active. DeepSeek lineage declaration and the disabled thesis-impact verifier selection remain separate owner decisions.

Engineering follow-up from the observed orphan: add a lifetime controller ownership lock plus exact same-config legacy process detection before backup/runtime replacement. Current `service.py` has no singleton guard, `install.sh` stops only launchctl-owned jobs, and `launch_drain.py` checks child tickets. A lock alone cannot detect a pre-upgrade daemon. The guard should refuse conflicting residents before side effects and must not kill processes based on a heartbeat PID alone.
