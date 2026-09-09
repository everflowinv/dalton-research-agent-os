"""P13ae: Guidepoint's identity, and why it is two approvals rather than one.

Guidepoint is the expert-network library the Deep Insight Gate asks for:
filings say what a company reported, transcripts of operators say why. The
connector template has been packaged since the inventory was built; what was
missing is an identity bound to one operation and a record an owner can sign.

Reading the index and reading a transcript are different permissions. P9d-1
split AlphaEngine for that reason and P10e split SEC for the same one: a schema
hash binds a single operation, so one record cannot be reused for the other
without silently widening what was approved.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.connector_governance import (
    ConnectorGovernance,
    build_governance_record,
    governance_kind_for_capability,
)
from dalton_core.guidepoint_core import (
    CREDENTIAL_SLOT_REF,
    SEARCH_KIND,
    SEARCH_OPERATION,
    TRANSCRIPT_KIND,
    TRANSCRIPT_OPERATION,
    GuidepointError,
    build_guidepoint_governance_record,
    guidepoint_identity,
    guidepoint_permissions,
    guidepoint_schema_hash,
    guidepoint_source_hash,
)

REPO = Path(__file__).resolve().parents[1]
GOVERNANCE_DIR = REPO / "deploy" / "connector-governance"


class IdentityTests(unittest.TestCase):
    def test_the_two_operations_do_not_share_a_schema_hash(self):
        # This is the whole reason there are two records.
        self.assertNotEqual(guidepoint_schema_hash(SEARCH_OPERATION),
                            guidepoint_schema_hash(TRANSCRIPT_OPERATION))

    def test_the_source_is_the_same_source(self):
        # One Guidepoint, so the source hash is shared -- exactly as the two
        # SEC capabilities share theirs.
        identities = [guidepoint_identity(op)
                      for op in (SEARCH_OPERATION, TRANSCRIPT_OPERATION)]
        self.assertEqual(identities[0]["source_hash"], identities[1]["source_hash"])
        self.assertEqual(identities[0]["source_hash"], guidepoint_source_hash())

    def test_an_identity_admits_exactly_one_operation(self):
        for operation in (SEARCH_OPERATION, TRANSCRIPT_OPERATION):
            identity = guidepoint_identity(operation)
            self.assertEqual(identity["allowed_operations"], [operation])
            self.assertEqual(list(identity["input_schema_refs"]), [operation])
            self.assertEqual(list(identity["output_schema_refs"]), [operation])

    def test_an_operation_nobody_froze_is_refused(self):
        with self.assertRaises(GuidepointError):
            guidepoint_identity("delete_everything")
        with self.assertRaises(GuidepointError):
            guidepoint_schema_hash("register_event")

    def test_the_capability_ids_are_distinct_and_map_back_to_their_kinds(self):
        search = guidepoint_identity(SEARCH_OPERATION)["capability_id"]
        transcript = guidepoint_identity(TRANSCRIPT_OPERATION)["capability_id"]
        self.assertNotEqual(search, transcript)
        self.assertEqual(governance_kind_for_capability(search), SEARCH_KIND)
        self.assertEqual(governance_kind_for_capability(transcript), TRANSCRIPT_KIND)


class PermissionTests(unittest.TestCase):
    def test_the_capability_is_credential_free_and_reaches_no_network_of_its_own(self):
        # The transport is host-owned MCP and the template forbids credential
        # material; this names a slot, never a secret.
        permissions = guidepoint_permissions()
        self.assertIs(permissions["network"], False)
        self.assertIs(permissions["core_db"], False)
        self.assertEqual(permissions["credential_slot_refs"], [CREDENTIAL_SLOT_REF])
        self.assertEqual(permissions["filesystem_read"], [])
        # The only write is the raw sink, where bytes are hashed before use.
        self.assertEqual(permissions["filesystem_write"], ["runner:raw-sink"])

    def test_the_packaged_template_forbids_credential_material(self):
        from dalton_core.connector_inventory import load_packaged_connector_inventory

        template = load_packaged_connector_inventory()["templates"]["guidepoint"]
        self.assertEqual(template["auth_boundary"]["credential_material"], "forbidden")
        self.assertEqual(template["auth_boundary"]["mode"], "host_owned")

    def test_mutating_a_returned_permission_set_cannot_reach_the_next_caller(self):
        first = guidepoint_permissions()
        first["network"] = True
        self.assertIs(guidepoint_permissions()["network"], False)


class GovernanceRecordTests(unittest.TestCase):
    def test_a_record_is_proposed_unless_somebody_says_otherwise(self):
        record = build_guidepoint_governance_record(
            operation=SEARCH_OPERATION, approved_by="human:lumos")
        self.assertEqual(record["status"], "proposed")

    def test_a_record_must_name_a_human_principal(self):
        with self.assertRaises(GuidepointError):
            build_guidepoint_governance_record(
                operation=SEARCH_OPERATION, approved_by="automation:dalton")

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(GuidepointError):
            build_guidepoint_governance_record(
                operation=SEARCH_OPERATION, approved_by="human:lumos", status="fine")

    def test_the_record_binds_its_own_operation_s_schema(self):
        for operation in (SEARCH_OPERATION, TRANSCRIPT_OPERATION):
            record = build_guidepoint_governance_record(
                operation=operation, approved_by="human:lumos")
            self.assertEqual(record["expected_schema_hash"],
                             guidepoint_schema_hash(operation))

    def test_the_shared_builder_produces_the_same_record(self):
        # The lane loads records through the shared machinery, so the kind has
        # to be reachable from there and not only from this module.
        direct = build_guidepoint_governance_record(
            operation=TRANSCRIPT_OPERATION, approved_by="human:lumos",
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        shared = build_governance_record(
            TRANSCRIPT_KIND, approved_by="human:lumos", status="proposed",
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        self.assertEqual(direct, shared)


class ShippedRecordTests(unittest.TestCase):
    """What the installer seeds, and what it must not be."""

    def records(self):
        for kind in (SEARCH_KIND, TRANSCRIPT_KIND):
            path = GOVERNANCE_DIR / f"{kind}-v1.json"
            with self.subTest(kind=kind):
                self.assertTrue(path.is_file(), f"{path} is not shipped")
            yield kind, json.loads(path.read_text(encoding="utf-8"))

    def test_the_shipped_records_are_proposed_not_approved(self):
        # Shipping an approved record would be signing for the owner.
        for _kind, record in self.records():
            self.assertEqual(record["status"], "proposed")

    def test_the_shipped_records_match_the_packaged_template(self):
        # A record whose hashes drifted from the template is one the lane will
        # refuse at load time, which is a bad thing to discover on the machine.
        for kind, record in self.records():
            operation = (SEARCH_OPERATION if kind == SEARCH_KIND
                         else TRANSCRIPT_OPERATION)
            self.assertEqual(record["expected_schema_hash"],
                             guidepoint_schema_hash(operation))
            self.assertEqual(record["expected_source_hash"], guidepoint_source_hash())

    def test_the_shipped_records_load_and_are_not_yet_usable(self):
        import tempfile

        for _kind, record in self.records():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(record, handle)
                path = handle.name
            try:
                governance = ConnectorGovernance.load(path)
                self.assertFalse(governance.approved)
            finally:
                Path(path).unlink()

    def test_the_installer_seeds_both(self):
        install = (REPO / "deploy" / "macos" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("guidepoint-search-library", install)
        self.assertIn("guidepoint-get-transcript", install)


if __name__ == "__main__":
    unittest.main()
