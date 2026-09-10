# Investment Memo Cockpit approval closure

Date: 2026-09-10

The writer decision path already enforces the full authority chain. It resolves
the exact current MissionDeliverable head, active mission and Playbook hashes;
validates the closed four-group, twelve-question gate; resolves five immutable
Scheduler WorkOrders and successful formal ResultEnvelopes; checks their Router
decisions, invocation refs, mission bindings and producer-route binding; proves
the verifier used a different served model family; and compares the verifier's
formal JSON result with the memo body hash and gate verdict. The stage write
then requires a human principal and a reason. Real authority tests cover
approve, reject, retry after an interrupted active-stage write, old rejection
followed by a new head, and old approval followed by a legal reopen and new
head.

The remaining defect was in the Cockpit read path. It validated only the JSON
gate before displaying approval actions. A memo with absent formal work could
therefore look decidable and then be refused correctly by the writer.

`investment_memo_evidence.replay_memo_model_evidence` now performs that replay
using query-only Scheduler access and a read-only ModelRouter. Cockpit exposes
approve/reject only after this replay succeeds. Its API item includes the four
producer groups, twelve structured questions, four checks, gaps, current input
bindings, verified body hash, and the formal producer/verifier evidence state.
It explicitly marks that a human signature is required. No automated path can
submit the decision, and the writer remains the final authority check.

The shared HTML was intentionally not changed in this slice. The API fields are
ready for the pending visual redesign without creating a second decision
route.

Validation:

- `PYTHONPATH=src python3 -m unittest tests.test_investment_memo_decision_real tests.test_investment_memo_decision tests.test_cockpit_plane`
  — 22 tests passed, one existing skip.

