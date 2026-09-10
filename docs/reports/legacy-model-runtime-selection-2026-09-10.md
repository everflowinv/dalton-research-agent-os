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

- `bounded_planner.config.planner_routing_policy_ref` for `plan`;
- `agenda.config.routing_policy_ref` for `agenda_planning`;
- `thesis_impact.config.assessment_routing_policy_ref`;
- `thesis_impact.config.verifier_routing_policy_ref`.

Each resident entry now also receives the selected catalog profile's public
`credential_slot_ref` in its real slot field (the bounded planner uses
`planner_credential_slot_refs`; the others use `credential_slot_refs`).  Prior
slots remain present.  Thus selecting a model on another provider does not
leave the new policy unusable behind the old provider's single credential
slot.  No credential value is read or copied.

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

An additional route test begins with an OpenAI-only configuration, selects a
Claude profile outside that legacy pin, reads the updated credential slots,
and asks the real router for a decision.  It selects the Claude gateway.  An
absent-purpose route remains on the legacy profile through the companion
router regression.  The thesis same-family verifier regression now also
asserts that only the assessment budget admission exists: the verifier is
rejected before budget reservation and before a second broker call.

## Read-only live-copy rehearsal

The current live router was opened with SQLite `mode=ro`, backed up to a fresh
temporary directory, synchronized there from the public OpenClaw catalog, and
paired with temporary copies of the three installed model configurations and
`service.json`.  Every router path used by the four resident sections pointed
at the temporary database.  No broker or model was called.

On separate fresh copies, `plan`, `thesis_impact_assessment`, and
`thesis_impact_verifier` published successfully, changed only temporary files,
added the selected provider's slot, and reported `requires_restart=true`.
`agenda_planning` correctly refused before writing because the copied live
agenda pin is `model-routing-policy-version:dalton-openclaw:2` while `:3` is
current.  Hashes of every temporary config remained unchanged on that failed
attempt.  The live activation packet must first align that stale agenda pin
with the current policy head; the setter does not silently skip it.

The public semantic difference is limited to the allowed profile set. Version
2 allows DeepSeek Flash, GPT-5.6 Sol/Terra/Luna, and Claude Fable/Opus. Version
3 retains those entries and adds Claude Sonnet, five Gemini profiles, Qwen
3.8 Max, three Qwen/GLM profiles, GPT-5.5, DeepSeek Pro, five Grok profiles,
and OpenRouter OX Alpha. Adapter constraints, empty provider/family filters,
text modality, verification independence capabilities, and ordering are
unchanged. Neither version has a purpose override.

After the final router chain was applied, 70 focused selection and verifier
tests passed. The local review branch lacks the later budget-reservation fix,
so its broad run cannot represent the frozen integration tree. Running the
previously failing expensive-fallback regression against frozen integration
source `61f56759c5e249be150623844df3ee4550a5ee65` passed (1 test). There is no
remaining frozen-code blocker from that observation.
