from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.capability_catalog import (
    CapabilityCatalog,
    CapabilityNotFound,
    StaleCatalog,
    canonical_hash,
)
from dalton_core.openclaw_metadata import (
    MetadataConflict,
    MetadataValidationError,
    OpenClawMetadataImporter,
)


NOW = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def none_permissions() -> dict:
    return {
        "risk_class": "none",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": [],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": [],
    }


def mcp_permissions() -> dict:
    return {
        "risk_class": "medium",
        "network": True,
        "filesystem_read": [],
        "filesystem_write": [],
        "credential_slot_refs": ["credential-slot:alphaengine"],
        "core_db": False,
        "side_effects": ["read:authenticated-library"],
    }


class FakeAuthorities:
    def approval(self, query: dict) -> dict:
        suffix = canonical_hash(
            {"capability_id": query["capability_id"], "source_hash": query["source_hash"]}
        )[:16]
        wire = {
            "schema_version": "0.1",
            "approval_ref": f"approval:{suffix}",
            "capability_id": query["capability_id"],
            "registry_revision_ref": f"capability-version:{suffix}",
            "artifact_ref": query["source_ref"],
            "artifact_hash": query["source_hash"],
            "schema_hash": query["schema_hash"],
            "fixture_manifest_hash": "4" * 64,
            "attestation_ref": f"attestation:{suffix}",
            "attestation_hash": "5" * 64,
            "decision_ref": f"capability-decision:{suffix}",
            "decision": "approve",
            "approved_by": "human:lumos",
            "approved_permissions": copy.deepcopy(query["requested_permissions"]),
            "active": True,
            "effective_from": "2026-08-14T17:00:00+00:00",
            "effective_until": None,
        }
        wire["receipt_hash"] = canonical_hash(wire)
        return wire


def hashed_item(wire: dict) -> dict:
    result = copy.deepcopy(wire)
    result["metadata_hash"] = canonical_hash(result)
    return result


def make_snapshot(
    *,
    snapshot_id: str = "openclaw-snapshot:one",
    skill_hash: str = "1" * 64,
    skill_description: str = "Read SEC filings through a governed workflow.",
    input_schema: dict | None = None,
    include_skill: bool = True,
    include_tool: bool = True,
    skills_complete: bool = True,
    complete_servers: list[str] | None = None,
) -> dict:
    input_schema = input_schema or {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {"query": {"type": "string"}},
    }
    output_schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {"content": {"type": "array"}},
    }
    skills = []
    if include_skill:
        skills.append(
            hashed_item(
                {
                    "name": "findata-analyst",
                    "label": "Findata analyst",
                    "description": skill_description,
                    "source": "openclaw-workspace",
                    "source_version": skill_hash[:16],
                    "eligible": True,
                    "disabled": False,
                    "model_visible": True,
                    "user_invocable": True,
                    "command_visible": True,
                    "instruction_ref": "openclaw-skill:findata-analyst",
                    "instruction_hash": skill_hash,
                }
            )
        )
    tools = []
    if include_tool:
        tools.append(
            hashed_item(
                {
                    "server_name": "alphaengine",
                    "safe_server_name": "alphaengine",
                    "tool_name": "search_library",
                    "title": "Search library",
                    "description": "Search the authenticated research library.",
                    "source_version": "catalog-7",
                    "execution_mode": "sequential",
                    "input_schema_ref": "openclaw-mcp-schema:alphaengine/search_library/input",
                    "input_schema": input_schema,
                    "input_schema_hash": canonical_hash(input_schema),
                    "output_schema_ref": "openclaw-mcp-schema:call-tool-result/0.1",
                    "output_schema": output_schema,
                    "output_schema_hash": canonical_hash(output_schema),
                }
            )
        )
    wire = {
        "schema_version": "0.1",
        "id": snapshot_id,
        "created_at": NOW.isoformat(timespec="microseconds"),
        "producer": {
            "openclaw_version": "2026.7.1",
            "catalog_generation": snapshot_id.rsplit(":", 1)[-1],
        },
        "scope": {
            "skills_complete": skills_complete,
            "mcp_servers_complete": (
                ["alphaengine"] if complete_servers is None else complete_servers
            ),
        },
        "skills": skills,
        "mcp_tools": tools,
    }
    wire["content_hash"] = canonical_hash(wire)
    return wire


class OpenClawMetadataImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authorities = FakeAuthorities()
        self.catalog = CapabilityCatalog(
            clock=lambda: NOW,
            approval_resolver=self.authorities.approval,
        )
        self.importer = OpenClawMetadataImporter(self.catalog, clock=lambda: NOW)

    def tearDown(self) -> None:
        self.catalog.close()

    def test_imports_metadata_and_schema_without_publishing_or_instruction_body(self) -> None:
        snapshot = make_snapshot()
        result = self.importer.import_snapshot(snapshot)
        self.assertEqual("fresh", result["write_status"])
        self.assertEqual(0, result["catalog_epoch"])
        self.assertEqual(2, len(result["events"]))
        self.assertEqual(
            1,
            self.catalog._conn.execute(
                "SELECT import_generation FROM external_capability_import_state "
                "WHERE singleton=1"
            ).fetchone()[0],
        )
        self.assertEqual([], self.catalog.search("filing", visibility_scopes=["research"]))

        skill = self.catalog.get_external_metadata(
            "capability:skill:openclaw:findata-analyst"
        )
        self.assertNotIn("instruction", skill)
        self.assertEqual("openclaw-skill:findata-analyst", skill["source"]["source_ref"])
        tool = self.catalog.get_external_metadata(
            "capability:mcp:alphaengine:search_library"
        )
        self.assertNotIn("input_schema", tool)
        loaded = self.catalog.load_external_schema(
            "openclaw-mcp-schema:alphaengine/search_library/input",
            snapshot["mcp_tools"][0]["input_schema_hash"],
        )
        self.assertEqual(snapshot["mcp_tools"][0]["input_schema"], loaded)

        duplicate = self.importer.import_snapshot(snapshot)
        self.assertEqual("duplicate", duplicate["write_status"])
        self.assertEqual(0, self.catalog.epoch)
        self.assertEqual(
            1,
            self.catalog._conn.execute(
                "SELECT import_generation FROM external_capability_import_state "
                "WHERE singleton=1"
            ).fetchone()[0],
        )

    def test_builds_governed_descriptor_proposals_and_publishes_only_with_approval(self) -> None:
        self.importer.import_snapshot(make_snapshot())
        skill_spec = self.importer.build_descriptor_spec(
            "capability:skill:openclaw:findata-analyst",
            version=1,
            permissions=none_permissions(),
            policy_ref="policy:capability-v1",
            visibility_scopes=["research"],
        )
        tool_spec = self.importer.build_descriptor_spec(
            "capability:mcp:alphaengine:search_library",
            version=1,
            permissions=mcp_permissions(),
            policy_ref="policy:capability-v1",
            visibility_scopes=["research"],
            state="auth_required",
        )
        with self.assertRaises(StaleCatalog):
            self.catalog.publish({**skill_spec, "summary": "caller-injected summary"})
        source_bypass = copy.deepcopy(tool_spec)
        source_bypass["source"]["type"] = "plugin"
        with self.assertRaises(StaleCatalog):
            self.catalog.publish(source_bypass)
        skill = self.catalog.publish(skill_spec)
        tool = self.catalog.publish(tool_spec)
        self.assertEqual(2, self.catalog.epoch)
        self.assertEqual("instruction_load", skill.contract.mode)
        self.assertEqual("typed_call", tool.contract.mode)
        described = self.catalog.describe(
            tool.id, visibility_scopes=["research"], catalog_epoch=self.catalog.epoch
        )
        self.assertNotIn("input_schema", described.to_dict())

    def test_drift_bumps_epoch_withdraws_descriptor_and_rejects_stale_proposal(self) -> None:
        first = make_snapshot()
        self.importer.import_snapshot(first)
        stale_spec = self.importer.build_descriptor_spec(
            "capability:skill:openclaw:findata-analyst",
            version=1,
            permissions=none_permissions(),
            policy_ref="policy:capability-v1",
            visibility_scopes=["research"],
        )
        self.catalog.publish(stale_spec)
        self.assertEqual(1, self.catalog.epoch)

        second = make_snapshot(
            snapshot_id="openclaw-snapshot:two",
            skill_hash="9" * 64,
            skill_description="Read SEC filings with a revised instruction contract.",
        )
        result = self.importer.import_snapshot(second)
        self.assertEqual(2, result["catalog_epoch"])
        with self.assertRaises(CapabilityNotFound):
            self.catalog.describe(
                "capability:skill:openclaw:findata-analyst",
                visibility_scopes=["research"],
                catalog_epoch=2,
            )
        with self.assertRaises(StaleCatalog):
            self.catalog.publish(stale_spec)

        replacement = self.importer.build_descriptor_spec(
            "capability:skill:openclaw:findata-analyst",
            version=2,
            permissions=none_permissions(),
            policy_ref="policy:capability-v1",
            visibility_scopes=["research"],
        )
        published = self.catalog.publish(replacement)
        self.assertEqual(2, published.version)
        self.assertEqual(3, self.catalog.epoch)

    def test_first_snapshot_invalidates_preexisting_unobserved_external_descriptor(self) -> None:
        legacy = {
            "schema_version": "0.1",
            "id": "capability:skill:openclaw:findata-analyst",
            "version": 1,
            "created_at": NOW.isoformat(timespec="microseconds"),
            "kind": "instruction",
            "name": "findata-analyst",
            "label": "Legacy metadata",
            "summary": "An unobserved legacy descriptor.",
            "aliases": [],
            "tags": ["legacy"],
            "intent_examples": [],
            "source": {
                "type": "skill",
                "namespace": "openclaw",
                "source_ref": "openclaw-skill:findata-analyst",
                "source_version": "1111111111111111",
            },
            "contract": {
                "mode": "instruction_load",
                "input_schema_ref": None,
                "output_schema_ref": None,
                "instruction_ref": "openclaw-skill:findata-analyst",
                "adapter_ref": "adapter:openclaw:skill-loader:0.1",
            },
            "permissions": none_permissions(),
            "eligibility": {
                "state": "ready",
                "visibility_scopes": ["research"],
                "policy_ref": "policy:capability-v1",
                "valid_until": None,
            },
            "source_hash": "1" * 64,
            "schema_hash": canonical_hash(
                {
                    "contract": {
                        "mode": "instruction_load",
                        "input_schema_ref": None,
                        "output_schema_ref": None,
                        "instruction_ref": "openclaw-skill:findata-analyst",
                        "adapter_ref": "adapter:openclaw:skill-loader:0.1",
                    },
                    "instruction_hash": "1" * 64,
                }
            ),
        }
        self.catalog.publish(legacy)
        result = self.importer.import_snapshot(make_snapshot())
        self.assertEqual(2, result["catalog_epoch"])
        self.assertTrue(
            any(
                event["capability_id"] == legacy["id"]
                and event["action"] == "changed"
                for event in result["events"]
            )
        )
        with self.assertRaises(CapabilityNotFound):
            self.catalog.describe(
                legacy["id"], visibility_scopes=["research"], catalog_epoch=2
            )

    def test_complete_scope_removes_but_partial_scope_preserves(self) -> None:
        self.importer.import_snapshot(make_snapshot())
        skill_spec = self.importer.build_descriptor_spec(
            "capability:skill:openclaw:findata-analyst",
            version=1,
            permissions=none_permissions(),
            policy_ref="policy:capability-v1",
            visibility_scopes=["research"],
        )
        skill = self.catalog.publish(skill_spec)
        partial = make_snapshot(
            snapshot_id="openclaw-snapshot:partial",
            include_skill=False,
            skills_complete=False,
        )
        self.importer.import_snapshot(partial)
        self.catalog.describe(
            skill.id, visibility_scopes=["research"], catalog_epoch=self.catalog.epoch
        )
        complete = make_snapshot(
            snapshot_id="openclaw-snapshot:complete",
            include_skill=False,
            skills_complete=True,
        )
        result = self.importer.import_snapshot(complete)
        self.assertTrue(any(event["action"] == "removed" for event in result["events"]))
        with self.assertRaises(CapabilityNotFound):
            self.catalog.get_external_metadata(skill.id)
        with self.assertRaises(StaleCatalog):
            self.catalog.publish(skill_spec)

    def test_disabled_import_cannot_be_published_ready_by_bypassing_builder(self) -> None:
        snapshot = make_snapshot()
        snapshot["skills"][0]["disabled"] = True
        snapshot["skills"][0]["metadata_hash"] = canonical_hash(
            {
                key: value
                for key, value in snapshot["skills"][0].items()
                if key != "metadata_hash"
            }
        )
        snapshot["content_hash"] = canonical_hash(
            {key: value for key, value in snapshot.items() if key != "content_hash"}
        )
        self.importer.import_snapshot(snapshot)
        unavailable = self.importer.build_descriptor_spec(
            "capability:skill:openclaw:findata-analyst",
            version=1,
            permissions=none_permissions(),
            policy_ref="policy:capability-v1",
            visibility_scopes=["research"],
        )
        self.assertEqual("unavailable", unavailable["eligibility"]["state"])
        bypass = copy.deepcopy(unavailable)
        bypass["eligibility"]["state"] = "ready"
        with self.assertRaises(StaleCatalog):
            self.catalog.publish(bypass)

    def test_rejects_prompt_paths_credentials_and_hash_tampering_without_writes(self) -> None:
        hostile = make_snapshot()
        hostile["skills"][0]["instruction"] = "do everything"
        hostile["content_hash"] = canonical_hash(
            {key: value for key, value in hostile.items() if key != "content_hash"}
        )
        with self.assertRaises(MetadataValidationError):
            self.importer.import_snapshot(hostile)
        self.assertEqual(0, self.catalog.epoch)

        path_leak = make_snapshot(snapshot_id="openclaw-snapshot:path")
        path_leak["skills"][0]["instruction_ref"] = "file:/private/skill.md"
        path_leak["skills"][0]["metadata_hash"] = canonical_hash(
            {k: v for k, v in path_leak["skills"][0].items() if k != "metadata_hash"}
        )
        path_leak["content_hash"] = canonical_hash(
            {key: value for key, value in path_leak.items() if key != "content_hash"}
        )
        with self.assertRaises(MetadataValidationError):
            self.importer.import_snapshot(path_leak)

        secret_schema = {
            "type": "object",
            "properties": {"api_key": {"type": "string"}},
        }
        with self.assertRaises(MetadataValidationError):
            self.importer.import_snapshot(
                make_snapshot(snapshot_id="openclaw-snapshot:secret", input_schema=secret_schema)
            )

        tampered = make_snapshot(snapshot_id="openclaw-snapshot:tampered")
        tampered["mcp_tools"][0]["description"] = "changed after hashing"
        tampered["content_hash"] = canonical_hash(
            {key: value for key, value in tampered.items() if key != "content_hash"}
        )
        with self.assertRaises(MetadataConflict):
            self.importer.import_snapshot(tampered)
        self.assertEqual(0, self.catalog.epoch)

    def test_contract_schema_is_closed_and_parseable(self) -> None:
        schema = json.loads(
            (CONTRACTS / "openclaw-capability-snapshot.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(make_snapshot()))


if __name__ == "__main__":
    unittest.main()
