# Cockpit actual model bindings — 2026-09-10

## Problem

The model page previously read one `CockpitConfig.model_config_path` policy and
used it for every purpose. On an installation with separate extraction,
planner, dossier producer, and dossier verifier pins, every row could therefore
claim to run the extraction policy.

## Change

The read-only page now resolves each purpose from the configuration its runtime
consumer reads. This includes the registered per-lane files, dossier and
verifier compatibility fallbacks, and the exact nested planner, agenda, and
thesis-impact pins in `service.json`. Ask, goal, and steer continue to use the
Cockpit's injected config path. Purposes whose model config is supplied only as
a dynamic launch argument are shown as unconfigured when the running Cockpit
cannot observe that argument; the page does not substitute another policy.

For old policies without fallback chains or purpose overrides, the page shows
their direct `filters.allowed_profile_ids` pin. It no longer advertises the
global tier chain that the legacy call does not consume. Each row exposes its
configuration source and exact policy version. Resident service pins are
marked as requiring a restart after a change.

Available-model rows now show the immutable profile ID alongside the current
`provider/model` route. This lets the owner check which route a metadata
declaration would bind before submitting the profile ID.

No live configuration, routing policy, model catalog, or broker file was read
or written while implementing this change.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_model_selection tests.test_cockpit_plane tests.test_cockpit_int2`
passed **128 tests** with one existing skipped integration test. The multi-pin
page test creates four distinct policies and verifies that ask, planner,
dossier producer, and dossier verifier report their own policy, source, and
direct model chain. It also verifies that an unobservable dynamic debate-map
binding is reported as unconfigured.

The JavaScript extracted from `cockpit_control.html` passed `node --check`.
