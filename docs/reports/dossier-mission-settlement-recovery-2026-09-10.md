# Dossier permission settlement across mission changes

Live v14 signing exposed a coordinator bug: a child launched against v13 was settled after the mission pointer advanced. The coordinator recomputed the permission key at settlement and recorded the old refusal against v14, so an authorized lane stayed blocked. The evidence-only ticket digest could also reuse an old result when authorization changed.

The coordinator now binds evidence plus mission/policy/config state into the signature before launch. The child ticket and all settlement outcomes retain that captured identity. A new control state gets a different ticket digest; the old refusal is retired from the active failure view without erasing historical events. The v2 key namespace also retires previously persisted, unbound permission entries once after upgrade so the already-poisoned live v14 item can recover. The child continues checking actual authorization before spending or publishing.

Validation: 64 dossier and whole-ledger failure tests passed (6.817s), including mission update between launch and settlement, distinct run digests, a current refusal still holding, and old permission event retention after upgrade. No live failure ledger or mission was edited by this repair.
