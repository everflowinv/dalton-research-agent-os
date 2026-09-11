# R14c1 runtime acceptance and remaining recovery blocker — 2026-09-11

Snapshot time: 2026-09-11T20:40:19.414398+00:00. R14c1 was installed at source `5048af26554dd379a16f4ef5d595dc75d8664b1d`; its separate deployment-health process subsequently passed 45/45 samples. This product audit is read-only and made no source, network, model, or live-state writes.

The normal planner attempted one postdeploy Work. Its 249,689-byte governed prompt fit the reviewed 250,000-byte prompt limit, but the authoritative day budget rejected the reservation before broker I/O: committed 99,756,798 micros plus requested 2,696,890 micros exceeded the 100,000,000-micro cap. The Work has zero budget admissions, zero settlements, zero usage entries, and `invocation:not-started`; therefore it is a budget wait, not a paid call or model failure.

The stored plan produced two directed-document admissions. Admission is not completion. Both exact registered-document searches completed locally without model budget. The DXC draft was then atomically rejected before send by the exhausted day budget; the EPAM draft was only Scheduler-pending at the snapshot. Across both admissions there were **0 proven paid sends, 0 outcomes, and 0 canonical promotions**.

The DXC observation exposes a product blocker. It recorded `retry_at=2026-09-12T00:00:00Z`, but the generic recovery window ended at `2026-09-11T22:35:12.704127Z`; the executor therefore stored `status=stopped` and `fresh_work_recovery_deadline_exceeded`. The lane converts that state to a permanent `recovery_required` hold with no retry time, so the admission cannot resume when the daily budget resets. This is a code defect in the cross-UTC day-budget recovery calculation. It does not justify increasing the budget or replaying the failed Work.

The extraction fix behaved as designed. The exact zero-length view and two invalid-UTF-8 views were dismissed as durably unreadable without any read-complete Claim. The two views lacking a completed acquisition/file remain open. Subsequent extraction children continued fresh numeric and discovery work, so the retained views did not monopolize the whole extraction cycle.

The first postdeploy conviction run did not repeat the former 300-character output refusal. Its verifier rejected the market view because cited rows did not support it, and the lane persisted a content refusal bound to the new task, verifier contract, evidence fingerprint, and model config.

The v1 helper queried mission-document Scheduler rows in the separate service scheduler database, while production stores those Works in Core. Its raw evidence remains immutable. The Core-backed supplement and this v2 report supersede only v1's generic lifecycle classification.

## Exact runtime acceptance

The release passed 7,488 tests (zero failures/errors, one skip), 602 runtime-file verification and the 14-step/42-tick/zero-escape rehearsal. Deployment completed at 20:30:46 UTC; same controller PID 49496 passed 45/45 health samples over 671.048 seconds. Post-observation installation verification passed and the release pointer is published verified. Seventeen model configs, service/OpenClaw bytes and mission v14 were preserved with zero configuration/service/external mutations. This runtime acceptance does not close the product blocker above.

| Evidence | SHA-256 |
| --- | --- |
| Manifest | `812504290ce16410fa19ac6ddbea26205050e63c6a51a7763857e166b5cc04d6` |
| Deployment | `5065b99c34eb4bff3a059844e6990f1a587c991a382bd892a4599b64c89e2937` |
| Installed files | `a14bcc79a65dc24970e3adde4783dfb8d950d7926ae2e490a202eb461530166d` |
| Sustained health | `ca80ecd01d8a67478ae47e1f55099fa4aa7079ee76d1863f09995797394ffb28` |
| Finalization | `0693bdedfd876c14bb1ebf229fc07b24bc516db6569d6f19d6e52bdceeb6e73c` |
| Publication | `d574a9b2eef15695424223a09b32b51e41a495ea2271528faf2bbed80087d43f` |
| Product report v2 | `d856c85b8e5b07103ec647b930003dcad52b7859641d9319416587cd8da74ad2` |
| Product receipt v2 | `04f40654afa4646da5a292943988191201051ae9dbb2e135cd44dddf1fa07c61` |

## Next development

Fix only proven pre-send daily-budget recovery so its configured active recovery window starts at the exact next eligible UTC boundary. Preserve bounded attempts, fresh-Work requirements and accounting evidence. The repair must also recover already stopped affected records through an append-only, verifiable transition; repairing only newly created jobs would leave the observed DXC record permanently blocked. Ordinary unknown-outcome recovery keeps its existing limits.

R15 remains parallel and undeployed: company-specific cash-flow declarations must feed exact selected filing concepts, preserve old model replay, reject incompatible units/signs and export OCF/CapEx/FCF into the already accepted AMZN-style geometry. New cash-flow cells need independent recalculation against internal quarterly and annual authority.

After R14c1 publication, 44 SQLite files in completed R14b/R14c rehearsal scratch roots were verified and removed, freeing 2,540,101,632 bytes. All rollback backups, the current R14c1 rehearsal and every non-SQLite evidence file remain. Private cleanup receipt SHA `f133ff46d57cd80d760cb02d8a9b9aa3d3889fd15f5bb61dd8c316a2909d71bd`.

## 21:07 UTC recovery repair accepted in integration

Author chain `2fcd46c4` → `f385fb85` → `5ef9f5e2` is integrated as `435ee790` → `0a50a59f` → `805c8d3f`. This is not yet deployed. Proven atomic day-budget refusals receive one bounded active recovery window after their first exact UTC eligibility boundary; later refusals cannot slide the deadline forward. Ordinary recovery and maximum fresh-Work counts retain their existing policy. Old stopped records can re-enter only when the stored proof/times match the exact old cross-day shape, including equality at midnight. Before eligibility the lane waits; after the extended deadline it remains terminal. The expiry uses a distinct observation reason, preserving the old row without an identity/hash collision.

Independent review reproduced and rejected the first implementation at 02:01: the lane incorrectly resumed and the executor reported `stored observation drifted`. The corrected implementation passed 43 root document/lane tests in 27.854 seconds, plus the final equal-midnight boundary test in 1.328 seconds. Author coverage includes 181 related tests on the initial implementation and 48 focused tests after the expiry repair. Original and new terminal observations coexist; repeat late execution sends zero model requests and creates no fresh Work. The integration source/test files match the reviewed author bytes.
