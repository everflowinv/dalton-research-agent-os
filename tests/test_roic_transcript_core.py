"""P13ah: roic.ai transcripts, split the way every library here is.

The Playbook asks for four quarters of calls. AlphaEngine carries them and is
capped at 130 calls a day; live, CTSH sat at one call of four with that cap
exhausted and its Initial Screen could not be rewritten. One source being busy
stopped a company, so the same requirement now has a second, independent way to
be met.

Listing what exists and reading one transcript are different permissions, and a
schema hash binds one operation, so one record cannot cover both.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector_governance import (
    ConnectorGovernance,
    build_governance_record,
    governance_kind_for_capability,
)
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.roic_transcript_core import (
    GET_KIND,
    GET_OPERATION,
    LIST_KIND,
    LIST_OPERATION,
    RoicTranscriptError,
    build_roic_governance_record,
    roic_identity,
    roic_permissions,
    roic_schema_hash,
    roic_source_hash,
)

REPO = Path(__file__).resolve().parents[1]
GOVERNANCE_DIR = REPO / "deploy" / "connector-governance"


class IdentityTests(unittest.TestCase):
    def test_the_two_operations_do_not_share_a_schema_hash(self):
        self.assertNotEqual(roic_schema_hash(LIST_OPERATION),
                            roic_schema_hash(GET_OPERATION))

    def test_one_roic_is_one_source(self):
        self.assertEqual(roic_identity(LIST_OPERATION)["source_hash"],
                         roic_identity(GET_OPERATION)["source_hash"])

    def test_it_is_not_the_same_source_as_alphaengine(self):
        # Both carry transcripts; they are not the same source and must not
        # share a source hash, or one approval would describe the other.
        from dalton_core.alphaengine_core_acquisition import alphaengine_source_hash

        self.assertNotEqual(roic_source_hash(), alphaengine_source_hash())

    def test_an_identity_admits_exactly_one_operation(self):
        for operation in (LIST_OPERATION, GET_OPERATION):
            identity = roic_identity(operation)
            self.assertEqual(identity["allowed_operations"], [operation])

    def test_an_operation_nobody_froze_is_refused(self):
        with self.assertRaises(RoicTranscriptError):
            roic_identity("delete_everything")

    def test_the_capabilities_map_back_to_their_kinds(self):
        self.assertEqual(
            governance_kind_for_capability(roic_identity(LIST_OPERATION)["capability_id"]),
            LIST_KIND)
        self.assertEqual(
            governance_kind_for_capability(roic_identity(GET_OPERATION)["capability_id"]),
            GET_KIND)


class PermissionTests(unittest.TestCase):
    def test_it_is_credential_free_public_web_to_one_host(self):
        permissions = roic_permissions()
        self.assertEqual(permissions["credential_slot_refs"], [])
        self.assertIs(permissions["core_db"], False)
        self.assertIs(permissions["network"], True)
        template = load_packaged_connector_inventory()["templates"]["roic-transcript"]
        self.assertEqual(template["transport"]["allowed_hosts"], ["www.roic.ai"])
        self.assertEqual(template["transport"]["host_policy"], "literal_allowlist")

    def test_mutating_a_returned_permission_set_cannot_reach_the_next_caller(self):
        first = roic_permissions()
        first["network"] = False
        self.assertIs(roic_permissions()["network"], True)


class GovernanceTests(unittest.TestCase):
    def test_a_record_is_proposed_and_needs_a_human(self):
        self.assertEqual(
            build_roic_governance_record(operation=LIST_OPERATION,
                                         approved_by="human:lumos")["status"],
            "proposed")
        with self.assertRaises(RoicTranscriptError):
            build_roic_governance_record(operation=LIST_OPERATION,
                                         approved_by="automation:dalton")

    def test_each_record_binds_its_own_operation(self):
        for operation in (LIST_OPERATION, GET_OPERATION):
            record = build_roic_governance_record(operation=operation,
                                                  approved_by="human:lumos")
            self.assertEqual(record["expected_schema_hash"], roic_schema_hash(operation))

    def test_the_shared_builder_produces_the_same_record(self):
        direct = build_roic_governance_record(
            operation=GET_OPERATION, approved_by="human:lumos",
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        shared = build_governance_record(
            GET_KIND, approved_by="human:lumos", status="proposed",
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        self.assertEqual(direct, shared)


class ShippedRecordTests(unittest.TestCase):
    def records(self):
        for kind, operation in ((LIST_KIND, LIST_OPERATION), (GET_KIND, GET_OPERATION)):
            path = GOVERNANCE_DIR / f"{kind}-v1.json"
            self.assertTrue(path.is_file(), f"{path} is not shipped")
            yield operation, json.loads(path.read_text(encoding="utf-8"))

    def test_the_shipped_records_are_proposed_not_approved(self):
        for _operation, record in self.records():
            self.assertEqual(record["status"], "proposed")

    def test_the_shipped_records_match_the_packaged_template(self):
        for operation, record in self.records():
            self.assertEqual(record["expected_schema_hash"], roic_schema_hash(operation))
            self.assertEqual(record["expected_source_hash"], roic_source_hash())

    def test_the_shipped_records_load_and_are_not_yet_usable(self):
        for _operation, record in self.records():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(record, handle)
                path = handle.name
            try:
                self.assertFalse(ConnectorGovernance.load(path).approved)
            finally:
                Path(path).unlink()

    def test_the_installer_seeds_both(self):
        install = (REPO / "deploy" / "macos" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("roic-list-transcripts", install)
        self.assertIn("roic-get-transcript", install)


if __name__ == "__main__":
    unittest.main()
