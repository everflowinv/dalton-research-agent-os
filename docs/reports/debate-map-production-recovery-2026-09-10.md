# DebateMap production recovery

Date: 2026-09-10

This audit read the live state and Scheduler databases in SQLite read-only mode
and projected the broker journal without prompts, response text, or credentials.
It made no model call and wrote no live state.

Two failures had different causes:

- EPAM run `35e68bb79c5f4f0777a4b8e9` received a top-level object containing
  `debates`, `evidence_limits`, and `template_coverage`. The parser correctly
  refused it because the producer contract permits exactly `debates`. The two
  commentary fields cannot be accepted selectively without weakening the
  closed boundary around an otherwise authoritative map.
- CTSH run `4e8a19d55aeb45339bc65aa4` replayed the earlier formal WorkOrder
  `work:cockpit-debate_map-0fc6ace9f1823190f1676514cfa68365`. Its Scheduler
  result is terminal `failed` with `MODEL_CHAIN_EXHAUSTED`; the selected Astra
  chain link records `unclassified_failure`. The generic
  `the model call did not succeed` message came from the replay reader and hid
  that formal error code. This was not a parser rejection.

The producer output boundary is now a named, hashed contract. The prompt shows
that version and hash beside the response instructions and explicitly forbids
the two live commentary keys and every other sibling of `debates`. The parser
remains closed and still rejects the full response if any extra key appears.
Both the prompt-derived WorkOrder and the persistent debate-lane business key
include the repaired contract identity. The same evidence and mission can
therefore run once after this repair rather than remaining behind the prior
terminal hold; unchanged repaired inputs remain idempotent.

`CockpitModel` replay failures now retain the formal ResultEnvelope error code,
so a future summary reports `MODEL_CHAIN_EXHAUSTED` instead of discarding the
only persisted diagnosis. It does not expose broker response content.

Validation:

- `PYTHONPATH=src python3 -m unittest tests.test_debate_map_draft tests.test_debate_map_lane tests.test_cockpit_model_fallback`
  — 93 passed.

The repair does not reinterpret either failed output as a DebateMap and does
not guarantee that an unavailable model provider will recover. It makes the
corrected producer contract eligible once and preserves the concrete formal
failure when the provider chain still cannot serve it.
