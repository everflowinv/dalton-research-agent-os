# F15 governance refusal classification

Date: 2026-09-10  
Branch: `resume-permission-fix`  
Baseline: `d6c2d38`

## Result

The shared lane failure contract now has a fourth `not_permitted` class. Document-extraction reasons beginning with the known mission-grant or active-policy governance gates enter this class. They consume no transient retry count, never enter dependency parking, and never become content-terminal.

`LaneFailureBudget` persists `not_permitted` and `permission_ok` events in the append-only operations ledger. Its replay restores pending authorization after a writer restart. The ledger projection and cockpit expose these records through a separate `permission_items` / `permission_count` bucket with a Chinese authorization label.

The document extraction coordinator wires the actual child `stop_reason` producer to this contract. An unchanged governance refusal returns `ungranted` without launching another child, even after elapsed time or coordinator restart. It records the relevant configuration-file signature with the settled ticket; a changed model or governance file clears the authorization hold and admits a new run. Queue states such as `nothing_to_draft` keep their existing idle behavior, and non-governance gates keep their existing bounded hold.

## Verification

`PYTHONPATH=$PWD/src python3 -m unittest tests.test_lane_failure_classes tests.test_cockpit_ops_panels tests.test_document_extraction_launcher`

Result: 72 tests passed in 6.129 seconds.

The focused tests distinguish governance refusal from drained work, verify zero retry-budget use, verify the separate cockpit bucket, preserve the hold across coordinator restart, and resume after a governance file changes.

## Limits

Configuration change detection covers the model configuration and the two governance files already passed to the extraction launcher. A permission source stored elsewhere must update one of these installed files to signal the coordinator, which is the current deployment path for grant and policy changes.
