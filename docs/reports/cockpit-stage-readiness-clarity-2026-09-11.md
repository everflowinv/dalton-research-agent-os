# Cockpit stage and source-readiness clarity

The overview previously placed a historical stage decision beside a current
Initial Screen checklist without naming the two time bases. A company could
therefore read as “已通过” while the adjacent checklist showed missing
material, implying either that the gate was fabricated or that a later stage
had become invalid.

The API now returns `gate_decision` and `source_readiness` separately. The
former is the folded, immutable stage history; the latter is today's Initial
Screen source checklist. The journey label combines them only while the
company has not moved beyond Initial Screen. Later stages retain their actual
historical decision while company detail still shows the current source state.
No authority decision, checklist count, reopening rule, or stage transition is
changed.

Validation covered passed-with-gaps, passed-and-ready, failed, not-entered, and
later-stage cases; the Cockpit overview integration test also checks the new
fields. Eighteen focused Python tests passed (one existing skip), and the
embedded browser JavaScript passed `node --check`.


Integration refinement: the checklist always distinguishes acquired document count from read count, including when acquisition is complete (for example EPAM annual acquired 1/read 0). Quantitative periods use quarter units. Entered-but-undecided stages say “已进入，等待裁决”; no historical decision is invented. Combined writer/discovery/fetch/Cockpit validation: 60 tests passed with one existing skip; embedded JavaScript syntax passes.
