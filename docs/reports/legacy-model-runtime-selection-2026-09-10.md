# Legacy model runtime Cockpit selection — 2026-09-10

## Runtime gaps closed

Four routed runtimes were outside effective per-stage selection even though the
router supported purpose overrides.

- Document extraction inherited the transcript worker's route call without a
  purpose. It now passes `document_extraction` and is classified as cheap.
- Thesis impact chose assessment and verifier policy refs from the resident
  `service.json` and routed without a phase purpose. It now passes
  `thesis_impact_assessment` or `thesis_impact_verifier`. A legacy single-pin
  policy remains required in automatic mode; an exact phase override admits
  the purpose-aware selection while the router still performs producer-family
  independence before the budget reservation and broker call.
- The retired Agenda plane, if explicitly re-enabled under ADR-0009, now passes
  `agenda_planning` rather than ignoring a selection.
- `LLMResearchPlannerWorker` uses the existing `plan` purpose. No duplicate
  research-planner purpose was introduced.

## Resident configuration pins

`set_model_selection` still discovers all registered model JSON files. It now
also discovers the conventional deployed `config/service.json` beside a Dalton
state directory and, for the relevant purpose only, includes the exact nested
resident pin:

- `bounded_planner.config.routing_policy_ref` for `plan`;
- `agenda.config.routing_policy_ref` for `agenda_planning`;
- `thesis_impact.config.assessment_routing_policy_ref`;
- `thesis_impact.config.verifier_routing_policy_ref`.

The service document joins the existing staged write/rollback set, so a late
replacement failure cannot leave model files and service pins on different
policy versions. The result names the nested pin and returns
`requires_restart=true`. These long-lived objects parse service configuration
at construction and have no safe per-tick config reload; claiming immediate
activation would be false. File-backed lane configs continue to report no
restart required.

Explicit selection is purpose-scoped. The companion router change must let the
explicit purpose chain supersede only `allowed_profile_ids` for that route;
an absent-purpose route must retain the legacy single pin. Other provider,
family, adapter, modality, capacity, availability, credential, and budget
filters remain effective.

## Validation

```text
PYTHONPATH=src python3 -m unittest tests.test_model_selection \
  tests.test_model_fallback_chain tests.test_document_extraction \
  tests.test_transcript_polish_model_worker \
  tests.test_llm_research_planner_worker tests.test_agenda_coordinator \
  tests.test_thesis_impact_control tests.test_thesis_impact_production
Ran 161 tests in 39.213s — OK
```

The focused service test uses the deployed directory topology, publishes an
explicit `plan` choice, verifies both the ordinary model config and the nested
bounded-planner pin move atomically, and verifies the restart requirement is
reported. No service was restarted and no live configuration or model was
called.
