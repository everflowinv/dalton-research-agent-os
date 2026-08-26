from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.alphaengine_core_acquisition import (
    StaticConnectorGovernance,
    build_governance_record as build_alphaengine_governance_record,
)
from dalton_core.connector_governance import (
    ConnectorGovernance,
    ConnectorGovernanceError,
    build_governance_record,
    load_connector_governance,
)
from dalton_core.connector_governance_cli import (
    approve_governance_record,
    main as governance_main,
)
from dalton_core.research_plan_executor import sec_connector_identity
from dalton_core.sec_authority_harness import PUBLIC_PERMISSIONS
from dalton_core.store import canonical_json, content_hash
from dalton_core.connector_inventory import load_packaged_connector_inventory


OWNER = "human:lumos"


class ConnectorGovernanceTests(unittest.TestCase):
    def test_alpha_builder_is_byte_identical_and_legacy_file_loads(self) -> None:
        legacy = build_alphaengine_governance_record(
            approved_by=OWNER, status="proposed", effective_from="2026-08-26T00:00:00+00:00"
        )
        generic = build_governance_record(
            "alphaengine-get-document",
            approved_by=OWNER, status="proposed", effective_from="2026-08-26T00:00:00+00:00"
        )
        self.assertEqual(canonical_json(generic), canonical_json(legacy))

        path = Path(__file__).parents[1] / "deploy/connector-governance/alphaengine-get-document-v1.json"
        loaded = load_connector_governance(path)
        self.assertEqual(loaded.kind, "alphaengine-get-document")
        self.assertEqual(loaded.content_hash, json.loads(path.read_text())["content_hash"])
        self.assertEqual(StaticConnectorGovernance.load(path).content_hash, loaded.content_hash)

    def test_alpha_approval_and_policy_remain_compatible(self) -> None:
        governance = StaticConnectorGovernance(
            build_alphaengine_governance_record(approved_by=OWNER, status="approved")
        )
        query = {
            "capability_id": governance.capability_id,
            "source_ref": "artifact:connector-profile-template:alphaengine:0.1",
            "source_hash": governance.wire["expected_source_hash"],
            "schema_hash": governance.wire["expected_schema_hash"],
        }
        receipt = governance.approval(query)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["approved_by"], OWNER)
        self.assertEqual(governance.policy({"policy_ref": governance.policy_ref})["max_lease_seconds"], 120)

    def test_sec_record_identity_permissions_and_authorities(self) -> None:
        inventory = load_packaged_connector_inventory()
        identity = sec_connector_identity(inventory["templates"]["sec"], "get_company_facts")
        proposed = build_governance_record(
            "sec-company-facts", approved_by=OWNER,
            effective_from="2026-08-27T00:00:00+00:00",
        )
        self.assertEqual(proposed["expected_source_hash"], identity["source_hash"])
        self.assertEqual(proposed["expected_schema_hash"], identity["schema_hash"])
        self.assertEqual(proposed["allowed_permissions"], PUBLIC_PERMISSIONS)
        governance = ConnectorGovernance(proposed)
        self.assertEqual(governance.kind, "sec-company-facts")
        permissions = governance.allowed_permissions
        permissions["network"] = False
        self.assertTrue(governance.allowed_permissions["network"])
        with self.assertRaises(ConnectorGovernanceError):
            governance.approval({"capability_id": governance.capability_id})
        with self.assertRaises(ConnectorGovernanceError):
            governance.policy({"policy_ref": governance.policy_ref})

        approved = ConnectorGovernance(
            build_governance_record(
                "sec-company-facts", approved_by=OWNER, status="approved",
                effective_from="2026-08-27T00:00:00+00:00",
            )
        )
        query = {
            "capability_id": approved.capability_id,
            "source_ref": "artifact:connector-profile-template:sec:0.1",
            "source_hash": identity["source_hash"],
            "schema_hash": identity["schema_hash"],
        }
        receipt = approved.approval(query)
        self.assertEqual(
            receipt["fixture_manifest_hash"],
            inventory["templates"]["sec"]["fixture_manifest_hash"],
        )
        self.assertEqual(receipt["capability_id"], "capability:dalton:connector:sec-edgar")
        policy = approved.policy({"policy_ref": approved.policy_ref})
        self.assertEqual(policy["allowed_permissions"], PUBLIC_PERMISSIONS)
        self.assertEqual(approved.policy_hash(), policy["content_hash"])

    def test_unknown_capability_is_rejected(self) -> None:
        record = build_governance_record("sec-company-facts", approved_by=OWNER)
        body = {**record, "capability_id": "capability:unknown:connector"}
        body["content_hash"] = content_hash({k: v for k, v in body.items() if k != "content_hash"})
        with self.assertRaises(ConnectorGovernanceError):
            ConnectorGovernance(body)

    def test_sec_cli_propose_show_approve_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sec-company-facts-v1.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    governance_main([
                        "propose", "--kind", "sec-company-facts", "--path", str(path),
                        "--approved-by", OWNER,
                        "--effective-from", "2026-08-27T00:00:00+00:00",
                    ]),
                    0,
                )
                self.assertEqual(
                    governance_main(["show", "--path", str(path)]), 0
                )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            proposed = json.loads(path.read_text())
            self.assertEqual(proposed["status"], "proposed")
            self.assertIn('"status": "proposed"', output.getvalue())
            # The inactive governance object fails closed; the owner CLI is
            # the explicit operation that turns this proposal into approved.
            self.assertEqual(proposed["status"], "proposed")
            self.assertEqual(
                json.loads(path.read_text())["status"], "proposed"
            )
            self.assertEqual(
                governance_main(["approve", "--path", str(path), "--approved-by", OWNER]), 0
            )
            approved = json.loads(path.read_text())
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approve_governance_record(path, approved_by=OWNER), approved)
            self.assertEqual(
                governance_main(["show", "--path", str(path)]), 0
            )
            # propose is create-only and must not replace an existing record.
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    governance_main([
                        "propose", "--kind", "sec-company-facts", "--path", str(path),
                        "--approved-by", OWNER,
                    ]),
                    1,
                )
