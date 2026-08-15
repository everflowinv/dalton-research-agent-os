from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector import ConnectorValidationError, validate_connector_proposal_manifest
from dalton_core.connector_inventory import (
    ConnectorInventoryError,
    PROFILE_DEFINITIONS,
    build_connector_inventory,
    load_connector_proposal_package,
    load_packaged_connector_inventory,
    validate_connector_fixture_manifest,
    validate_connector_inventory_index,
    validate_connector_profile_template,
)
from dalton_core.store import canonical_json, content_hash
from tests.test_connector import assert_wire_schema


ROOT = Path(__file__).parents[1]


def rehash(wire: dict) -> dict:
    result = copy.deepcopy(wire)
    result.pop("content_hash", None)
    result["content_hash"] = content_hash(result)
    return result


def synthetic_proposal_package() -> dict[str, dict]:
    slug = "synthetic-prices"
    created_at = "2026-08-15T04:00:00.000000+00:00"
    connector_ref = f"connector:{slug}"
    profile_ref = f"connector-profile-template:{slug}:0.1"
    fixture_ref = f"connector-fixture-manifest:{slug}:0.1"
    target_ref = f"transport:{slug}:0.1"
    operation = "get_quote"
    input_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "minLength": 1}},
    }
    output_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["source_record_refs", "next_cursor", "provider_status"],
        "properties": {
            "source_record_refs": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "next_cursor": {"type": ["string", "null"]},
            "provider_status": {"type": "integer", "minimum": 100},
        },
    }
    input_ref = f"schema:connector-proposal:{slug}:{operation}:input:0.1"
    output_ref = f"schema:connector-proposal:{slug}:{operation}:output:0.1"
    fixture_cases = []
    for scenario in (
        "success", "empty", "partial", "schema_drift",
        "rate_limited", "timeout", "malformed",
    ):
        succeeded = scenario in {"success", "empty", "partial"}
        outcome = (
            "succeeded" if succeeded else
            "rate_limited" if scenario == "rate_limited" else
            "timeout" if scenario == "timeout" else "failed"
        )
        source_status = {
            "success": "complete", "empty": "empty", "partial": "partial",
        }.get(scenario)
        completeness = {
            "success": "enumerated", "empty": "enumerated", "partial": "partial",
        }.get(scenario)
        fixture_cases.append(
            {
                "case_ref": f"fixture:{slug}:{operation}:{scenario}:0.1",
                "scenario": scenario,
                "operation": operation,
                "outcome": outcome,
                "provider_status": (
                    None if scenario == "timeout" else
                    429 if scenario == "rate_limited" else 200
                ),
                "source_status": source_status,
                "completeness": completeness,
                "page": None,
                "next_cursor": None,
                "source_record_refs": (
                    [f"record:{slug}:synthetic:1"]
                    if scenario in {"success", "partial"} else []
                ),
                "raw_payload_hash": (
                    content_hash(
                        {
                            "synthetic": True, "connector": slug,
                            "operation": operation, "scenario": scenario,
                        }
                    )
                    if succeeded else None
                ),
                "error_code": None if succeeded else scenario,
            }
        )
    fixture = rehash(
        {
            "schema_version": "0.1", "id": fixture_ref,
            "created_at": created_at, "connector_template_ref": profile_ref,
            "recording_boundary": "public_provider", "authenticated": False,
            "synthetic": True,
            "operations": [{"operation": operation, "pagination_mode": "none"}],
            "cases": fixture_cases,
        }
    )
    target_hash = content_hash(
        {
            "kind": "public_https", "target_ref": target_ref,
            "host_policy": "literal_allowlist",
            "allowed_hosts": ["prices.example.com"],
        }
    )
    operation_wire = {
        "operation": operation, "source_method": operation,
        "input_schema_ref": input_ref, "input_schema_hash": content_hash(input_schema),
        "output_schema_ref": output_ref, "output_schema_hash": content_hash(output_schema),
        "completeness_ceiling": "enumerated",
        "pagination": {
            "mode": "none", "cursor_field": None,
            "bounded_window_required": False, "max_pages": 1,
        },
        "side_effects": ["read:recorded-fixture"],
    }
    profile = rehash(
        {
            "schema_version": "0.1", "id": profile_ref,
            "created_at": created_at, "connector_ref": connector_ref,
            "source_identity": {
                "source_ref": f"source:{slug}", "source_type": "market_data",
                "source_version": "proposal-0.1",
            },
            "transport": {
                "kind": "public_https", "target_ref": target_ref,
                "target_hash": target_hash, "host_policy": "literal_allowlist",
                "allowed_hosts": ["prices.example.com"],
            },
            "auth_boundary": {
                "mode": "none", "owner": "none",
                "credential_material": "forbidden", "use_time_authority": "none",
            },
            "route_restrictions": {
                "allowed_target_refs": [target_ref],
                "forbidden_target_refs": ["route:private-network"],
                "fallback_routes": [], "provenance_label_required": True,
            },
            "schema_documents": [
                {
                    "schema_ref": input_ref, "schema_hash": content_hash(input_schema),
                    "document": input_schema,
                },
                {
                    "schema_ref": output_ref, "schema_hash": content_hash(output_schema),
                    "document": output_schema,
                },
            ],
            "operations": [operation_wire],
            "fixture_manifest_ref": fixture_ref,
            "fixture_manifest_hash": fixture["content_hash"],
            "readiness": {
                "level": "inventory_connected", "lease_eligible": False,
                "live_execution_allowed": False,
                "required_gate": "killable_total_deadline_public_transport",
            },
        }
    )
    proposal = rehash(
        {
            "schema_version": "0.2",
            "id": f"connector-proposal-manifest:{slug}:0.2",
            "created_at": created_at,
            "capability_proposal_ref": f"capability-proposal:connector:{slug}:0.1",
            "connector_ref": connector_ref,
            "source_identity": {
                "source": f"source:{slug}", "adapter": target_ref,
                "source_version": "proposal-0.1", "adapter_version": "proposal-0.1",
            },
            "adapter_package_ref": f"inventory-artifact:adapter-contract:{slug}:0.1",
            "adapter_source_hash": content_hash(
                {"target_ref": target_ref, "operations": [operation]}
            ),
            "profile_template_ref": profile_ref,
            "profile_template_hash": profile["content_hash"],
            "operations": [
                {
                    "operation": operation,
                    "input_schema_ref": input_ref,
                    "input_schema_hash": content_hash(input_schema),
                    "output_schema_ref": output_ref,
                    "output_schema_hash": content_hash(output_schema),
                    "completeness": "enumerated", "pagination": "none",
                    "side_effects": ["read:recorded-fixture"],
                }
            ],
            "fixture_manifest_ref": fixture_ref,
            "fixture_manifest_hash": fixture["content_hash"],
            "offline_attestation_policy_ref": "policy:connector-inventory-offline:0.1",
            "requested_canary": None,
            "promotion_policy_ref": "policy:connector-promotion:0.1",
            "builder_ref": "builder:test-connector-proposal:0.1",
            "transport_kind": "public_https",
            "transport_target_ref": target_ref,
            "transport_target_hash": target_hash,
            "auth_boundary": profile["auth_boundary"],
            "inventory_state": "proposal_only",
        }
    )
    return {"profile": profile, "fixture": fixture, "proposal": proposal}


def write_proposal_package(root: Path, package: dict[str, dict]) -> None:
    for name in ("profile", "fixture", "proposal"):
        (root / f"{name}.json").write_text(
            canonical_json(package[name]), encoding="utf-8"
        )


def rebind_proposal_package(package: dict[str, dict]) -> dict[str, dict]:
    """Recompute every declared graph hash after a hostile package mutation."""

    package = copy.deepcopy(package)
    profile = package["profile"]
    fixture = package["fixture"]
    proposal = package["proposal"]
    slug = profile["id"].removeprefix(
        "connector-profile-template:"
    ).removesuffix(":0.1")

    for case in fixture["cases"]:
        if case["outcome"] == "succeeded":
            case["raw_payload_hash"] = content_hash(
                {
                    "synthetic": True,
                    "connector": slug,
                    "operation": case["operation"],
                    "scenario": case["scenario"],
                }
            )
    fixture = rehash(fixture)

    documents = {}
    for document in profile["schema_documents"]:
        document["schema_hash"] = content_hash(document["document"])
        documents[document["schema_ref"]] = document
    for operation in profile["operations"]:
        operation["input_schema_hash"] = documents[
            operation["input_schema_ref"]
        ]["schema_hash"]
        operation["output_schema_hash"] = documents[
            operation["output_schema_ref"]
        ]["schema_hash"]
    profile["transport"]["target_hash"] = content_hash(
        {
            "kind": profile["transport"]["kind"],
            "target_ref": profile["transport"]["target_ref"],
            "host_policy": profile["transport"]["host_policy"],
            "allowed_hosts": profile["transport"]["allowed_hosts"],
        }
    )
    profile["fixture_manifest_ref"] = fixture["id"]
    profile["fixture_manifest_hash"] = fixture["content_hash"]
    profile = rehash(profile)

    proposal["connector_ref"] = profile["connector_ref"]
    proposal["source_identity"]["source"] = profile["source_identity"]["source_ref"]
    proposal["source_identity"]["source_version"] = profile["source_identity"][
        "source_version"
    ]
    proposal["source_identity"]["adapter"] = profile["transport"]["target_ref"]
    proposal["adapter_source_hash"] = content_hash(
        {
            "target_ref": profile["transport"]["target_ref"],
            "operations": [
                operation["operation"] for operation in profile["operations"]
            ],
        }
    )
    proposal["profile_template_ref"] = profile["id"]
    proposal["profile_template_hash"] = profile["content_hash"]
    proposal["fixture_manifest_ref"] = fixture["id"]
    proposal["fixture_manifest_hash"] = fixture["content_hash"]
    proposal["transport_kind"] = profile["transport"]["kind"]
    proposal["transport_target_ref"] = profile["transport"]["target_ref"]
    proposal["transport_target_hash"] = profile["transport"]["target_hash"]
    proposal["auth_boundary"] = profile["auth_boundary"]
    proposal["operations"] = [
        {
            "operation": operation["operation"],
            "input_schema_ref": operation["input_schema_ref"],
            "input_schema_hash": operation["input_schema_hash"],
            "output_schema_ref": operation["output_schema_ref"],
            "output_schema_hash": operation["output_schema_hash"],
            "completeness": operation["completeness_ceiling"],
            "pagination": operation["pagination"]["mode"],
            "side_effects": operation["side_effects"],
        }
        for operation in profile["operations"]
    ]
    proposal = rehash(proposal)
    return {"profile": profile, "fixture": fixture, "proposal": proposal}


class ConnectorInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.built = build_connector_inventory()

    def test_packaged_inventory_is_exactly_the_deterministic_build(self) -> None:
        packaged = load_packaged_connector_inventory()
        self.assertEqual(canonical_json(packaged["index"]), canonical_json(self.built["index"]))
        self.assertEqual(packaged["templates"], self.built["templates"])
        self.assertEqual(packaged["fixtures"], self.built["fixtures"])
        self.assertEqual(packaged["proposals"], self.built["proposals"])
        assert_wire_schema(self, "connector-inventory-index.schema.json", packaged["index"])
        for profile in packaged["templates"].values():
            assert_wire_schema(self, "connector-profile-template.schema.json", profile)
        for fixture in packaged["fixtures"].values():
            assert_wire_schema(self, "connector-fixture-manifest.schema.json", fixture)
        for proposal in packaged["proposals"].values():
            # The proposal schema keeps the 0.1 root-required field set for
            # wire compatibility; its conditional 0.2 fields are enforced by
            # the executable validator.
            self.assertEqual(
                validate_connector_proposal_manifest(proposal), proposal
            )

    def test_extensible_proposal_package_accepts_a_new_offline_connector(self) -> None:
        frozen_before = load_packaged_connector_inventory()
        package = synthetic_proposal_package()
        self.assertNotIn(
            package["profile"]["connector_ref"],
            {definition["connector_ref"] for definition in PROFILE_DEFINITIONS},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_proposal_package(root, package)
            loaded = load_connector_proposal_package(root)

        self.assertEqual(loaded, package)
        self.assertEqual(
            loaded["profile"]["readiness"],
            {
                "level": "inventory_connected",
                "lease_eligible": False,
                "live_execution_allowed": False,
                "required_gate": "killable_total_deadline_public_transport",
            },
        )
        self.assertEqual(load_packaged_connector_inventory(), frozen_before)

    def test_extensible_proposal_package_fails_closed_on_escalation_and_forks(self) -> None:
        def assert_rejected(
            package: dict[str, dict], *, extra_file: bool = False
        ) -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_proposal_package(root, package)
                if extra_file:
                    (root / "README.json").write_text("{}", encoding="utf-8")
                with self.assertRaises(ConnectorInventoryError):
                    load_connector_proposal_package(root)

        live = synthetic_proposal_package()
        live["profile"]["readiness"]["lease_eligible"] = True
        live["profile"] = rehash(live["profile"])
        live["proposal"]["profile_template_hash"] = live["profile"]["content_hash"]
        live["proposal"] = rehash(live["proposal"])
        assert_rejected(live)

        schema_fork = synthetic_proposal_package()
        schema_fork["profile"]["operations"][0]["input_schema_hash"] = "0" * 64
        schema_fork["profile"] = rehash(schema_fork["profile"])
        schema_fork["proposal"]["profile_template_hash"] = schema_fork["profile"][
            "content_hash"
        ]
        schema_fork["proposal"] = rehash(schema_fork["proposal"])
        assert_rejected(schema_fork)

        real_fixture = synthetic_proposal_package()
        real_fixture["fixture"]["synthetic"] = False
        real_fixture["fixture"] = rehash(real_fixture["fixture"])
        real_fixture["profile"]["fixture_manifest_hash"] = real_fixture["fixture"][
            "content_hash"
        ]
        real_fixture["profile"] = rehash(real_fixture["profile"])
        real_fixture["proposal"]["fixture_manifest_hash"] = real_fixture["fixture"][
            "content_hash"
        ]
        real_fixture["proposal"]["profile_template_hash"] = real_fixture["profile"][
            "content_hash"
        ]
        real_fixture["proposal"] = rehash(real_fixture["proposal"])
        assert_rejected(real_fixture)

        unsafe = synthetic_proposal_package()
        unsafe["proposal"]["builder_ref"] = "api_key:secret-material"
        unsafe["proposal"] = rehash(unsafe["proposal"])
        assert_rejected(unsafe)

        graph_fork = synthetic_proposal_package()
        graph_fork["proposal"]["source_identity"]["adapter"] = "transport:fork:0.1"
        graph_fork["proposal"] = rehash(graph_fork["proposal"])
        assert_rejected(graph_fork)

        assert_rejected(synthetic_proposal_package(), extra_file=True)

    def test_extensible_proposal_package_recursively_validates_operation_schemas(self) -> None:
        mutations = (
            lambda document: document["properties"]["symbol"].update(
                {"type": "banana"}
            ),
            lambda document: document["properties"].update(
                {
                    "nested": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {},
                    }
                }
            ),
            lambda document: document["properties"].update(
                {"values": {"type": "array"}}
            ),
            lambda document: document["properties"]["symbol"].update(
                {"description": "unreviewed keyword"}
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                package = synthetic_proposal_package()
                mutate(package["profile"]["schema_documents"][0]["document"])
                package = rebind_proposal_package(package)
                root = Path(directory)
                write_proposal_package(root, package)
                with self.assertRaises(ConnectorInventoryError):
                    load_connector_proposal_package(root)

    def test_extensible_proposal_package_reserves_frozen_identity_and_authority(self) -> None:
        def assert_rejected(package: dict[str, dict]) -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_proposal_package(root, rebind_proposal_package(package))
                with self.assertRaises(ConnectorInventoryError):
                    load_connector_proposal_package(root)

        frozen = json.loads(
            canonical_json(synthetic_proposal_package()).replace(
                "synthetic-prices", "cninfo"
            )
        )
        frozen["profile"]["connector_ref"] = "connector:cninfo-announcements"
        frozen["proposal"]["connector_ref"] = "connector:cninfo-announcements"
        assert_rejected(frozen)

        adapter_version = synthetic_proposal_package()
        adapter_version["proposal"]["source_identity"]["adapter_version"] = "forked-9.9"
        assert_rejected(adapter_version)

        missing_gate = synthetic_proposal_package()
        missing_gate["profile"]["readiness"]["required_gate"] = "none"
        assert_rejected(missing_gate)

    def test_extensible_proposal_package_member_boundary_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_proposal_package(root, synthetic_proposal_package())
            (root / "fixture.json").unlink()
            with self.assertRaises(ConnectorInventoryError):
                load_connector_proposal_package(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_proposal_package(root, synthetic_proposal_package())
            (root / "proposal.json").unlink()
            (root / "proposal.json").symlink_to(root / "profile.json")
            with self.assertRaises(ConnectorInventoryError):
                load_connector_proposal_package(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_proposal_package(root, synthetic_proposal_package())
            (root / "fixture.json").write_text(
                "{" + "x" * 1_000_001 + "}", encoding="utf-8"
            )
            with self.assertRaises(ConnectorInventoryError):
                load_connector_proposal_package(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_proposal_package(root, synthetic_proposal_package())
            (root / "proposal.json").write_text(
                '{"schema_version":"0.2","schema_version":"0.2"}',
                encoding="utf-8",
            )
            with self.assertRaises(ConnectorInventoryError):
                load_connector_proposal_package(root)

    def test_inventory_has_exactly_ten_distinct_profiles_and_required_splits(self) -> None:
        profiles = self.built["templates"]
        self.assertEqual(
            set(profiles),
            {
                "cninfo", "sec", "alphaengine", "x-xreach", "x-x-search",
                "reddit-last30days", "guidepoint", "gemini-web-search",
                "web-fetch", "xueqiu",
            },
        )
        refs = {profile["connector_ref"] for profile in profiles.values()}
        self.assertEqual(len(refs), 10)
        self.assertNotEqual(
            profiles["x-xreach"]["connector_ref"], profiles["x-x-search"]["connector_ref"]
        )
        self.assertNotEqual(
            profiles["gemini-web-search"]["connector_ref"], profiles["web-fetch"]["connector_ref"]
        )

    def test_xueqiu_route_auth_operations_and_fallback_provenance_are_frozen(self) -> None:
        profile = self.built["templates"]["xueqiu"]
        self.assertEqual(profile["transport"]["kind"], "host_tool")
        self.assertEqual(
            profile["transport"]["target_ref"], "host-tool:agent-reach-xueqiu-channel"
        )
        self.assertEqual(profile["auth_boundary"]["mode"], "host_owned")
        self.assertEqual(
            {operation["operation"] for operation in profile["operations"]},
            {"get_stock_quote", "get_hot_posts", "get_hot_stocks", "search_stock"},
        )
        completeness = {
            item["operation"]: item["completeness_ceiling"]
            for item in profile["operations"]
        }
        self.assertEqual(completeness["get_stock_quote"], "enumerated")
        self.assertEqual(completeness["get_hot_posts"], "ranked")
        self.assertEqual(
            profile["route_restrictions"]["fallback_routes"],
            [{
                "operation": "get_hot_stocks",
                "target_ref": "host-tool:cn-hk-findata-xq-hot-rank",
                "source_ref": "source:xueqiu",
                "adapter_ref": "adapter:cn-hk-findata:xq-hot-rank",
                "provenance_label": "xueqiu_hot_stock_rank_fallback",
            }],
        )
        serialized = canonical_json(self.built).lower()
        self.assertNotIn("xq_a_token", serialized)
        self.assertNotIn("cookie:", serialized)

    def test_transport_auth_and_readiness_never_fabricate_runner_authority(self) -> None:
        public = {"cninfo", "sec", "web-fetch"}
        for slug, profile in self.built["templates"].items():
            with self.subTest(slug=slug):
                readiness = profile["readiness"]
                self.assertEqual(readiness["level"], "inventory_connected")
                self.assertFalse(readiness["lease_eligible"])
                self.assertFalse(readiness["live_execution_allowed"])
                proposal = self.built["proposals"][slug]
                self.assertIsNone(proposal["requested_canary"])
                self.assertEqual(proposal["inventory_state"], "proposal_only")
                if slug in public:
                    self.assertEqual(profile["transport"]["kind"], "public_https")
                    self.assertEqual(profile["auth_boundary"]["mode"], "none")
                    if slug == "web-fetch":
                        self.assertEqual(
                            profile["transport"]["host_policy"], "per_call_authority"
                        )
                        self.assertFalse(profile["transport"]["allowed_hosts"])
                    else:
                        self.assertEqual(
                            profile["transport"]["host_policy"], "literal_allowlist"
                        )
                        self.assertTrue(profile["transport"]["allowed_hosts"])
                else:
                    self.assertFalse(profile["transport"]["allowed_hosts"])

    def test_operation_schemas_and_fixture_hashes_are_exactly_bound(self) -> None:
        for slug, profile in self.built["templates"].items():
            documents = {
                document["schema_ref"]: document for document in profile["schema_documents"]
            }
            fixture = self.built["fixtures"][slug]
            self.assertEqual(profile["fixture_manifest_hash"], fixture["content_hash"])
            for operation in profile["operations"]:
                with self.subTest(slug=slug, operation=operation["operation"]):
                    self.assertEqual(
                        operation["input_schema_hash"],
                        documents[operation["input_schema_ref"]]["schema_hash"],
                    )
                    self.assertEqual(
                        operation["output_schema_hash"],
                        documents[operation["output_schema_ref"]]["schema_hash"],
                    )
                    self.assertFalse(
                        documents[operation["input_schema_ref"]]["document"]["additionalProperties"]
                    )

    def test_fixture_matrix_has_common_and_authenticated_failure_cases(self) -> None:
        common = {
            "success", "empty", "partial", "schema_drift",
            "rate_limited", "timeout", "malformed",
        }
        for slug, fixture in self.built["fixtures"].items():
            modes = {
                item["operation"]: item["pagination_mode"]
                for item in fixture["operations"]
            }
            for operation, mode in modes.items():
                scenarios = {
                    case["scenario"] for case in fixture["cases"]
                    if case["operation"] == operation
                }
                self.assertTrue(common.issubset(scenarios), (slug, operation))
                self.assertEqual("pagination" in scenarios, mode != "none")
                if fixture["authenticated"]:
                    self.assertTrue(
                        {"permission_denied", "revoked"}.issubset(scenarios),
                        (slug, operation),
                    )
                rate_limited = next(
                    case for case in fixture["cases"]
                    if case["operation"] == operation
                    and case["scenario"] == "rate_limited"
                )
                self.assertEqual(rate_limited["provider_status"], 429)
                self.assertIsNone(rate_limited["raw_payload_hash"])

    def test_validators_fail_closed_on_authority_and_hash_tampering(self) -> None:
        original = self.built["templates"]["xueqiu"]
        cases = []
        changed = copy.deepcopy(original)
        changed["unexpected"] = True
        cases.append(changed)
        changed = copy.deepcopy(original)
        changed["transport"]["allowed_hosts"] = ["127.0.0.1"]
        cases.append(rehash(changed))
        changed = copy.deepcopy(original)
        changed["auth_boundary"]["owner"] = "none"
        cases.append(rehash(changed))
        changed = copy.deepcopy(original)
        changed["readiness"]["lease_eligible"] = True
        cases.append(rehash(changed))
        changed = copy.deepcopy(original)
        changed["route_restrictions"]["provenance_label_required"] = False
        cases.append(rehash(changed))
        changed = copy.deepcopy(original)
        changed["operations"][0]["input_schema_hash"] = "0" * 64
        cases.append(rehash(changed))
        for index, case in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ConnectorInventoryError):
                validate_connector_profile_template(case)

        proposal = copy.deepcopy(self.built["proposals"]["xueqiu"])
        proposal["requested_canary"] = {
            "allowed_hosts": ["xueqiu.com"], "credential_slot_refs": [],
            "max_calls": 1, "max_bytes": 1, "max_records": 1,
            "max_cost_micros": 0, "expires_at": "2999-01-01T00:00:00+00:00",
        }
        proposal = rehash(proposal)
        with self.assertRaises(ConnectorValidationError):
            validate_connector_proposal_manifest(proposal)

        alpha = copy.deepcopy(self.built["templates"]["alphaengine"])
        alpha["auth_boundary"] = {
            "mode": "none", "owner": "none", "credential_material": "forbidden",
            "use_time_authority": "none",
        }
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_profile_template(rehash(alpha))
        cninfo = copy.deepcopy(self.built["templates"]["cninfo"])
        cninfo["operations"][0]["pagination"]["bounded_window_required"] = False
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_profile_template(rehash(cninfo))
        unsafe = copy.deepcopy(self.built["proposals"]["sec"])
        for value in (
            "/Users/example/server_config.json", "system_prompt:ignore-all-security",
            "api_key:secret-material", "server-config:embedded",
            "system prompt:ignore", "api key:secret", "apikey:secret",
            "server config:embedded", "oauth_config:embedded",
            "file:%2FUsers%2Fvictim%2Fconfig",
        ):
            changed = copy.deepcopy(unsafe)
            changed["builder_ref"] = value
            with self.subTest(unsafe_ref=value), self.assertRaises(ConnectorValidationError):
                validate_connector_proposal_manifest(rehash(changed))
        xueqiu = copy.deepcopy(self.built["templates"]["xueqiu"])
        xueqiu["route_restrictions"]["fallback_routes"][0]["operation"] = "get_hot_posts"
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_profile_template(rehash(xueqiu))

    def test_fixture_validator_binds_each_case_to_operation_and_pagination(self) -> None:
        fixture = self.built["fixtures"]["web-fetch"]
        unknown = copy.deepcopy(fixture)
        unknown["cases"][0]["operation"] = "missing_operation"
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(unknown))
        wrong_status = copy.deepcopy(fixture)
        wrong_status["cases"][0]["provider_status"] = "200"
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(wrong_status))
        fake_page = copy.deepcopy(fixture)
        fake_page["cases"][0].update(
            {"scenario": "pagination", "page": 1, "next_cursor": "forged"}
        )
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(fake_page))
        for field, value in (
            ("scenario", "unknown"),
            ("source_status", {"forged": True}),
            ("page", True),
        ):
            changed = copy.deepcopy(self.built["fixtures"]["xueqiu"])
            changed["cases"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ConnectorInventoryError):
                validate_connector_fixture_manifest(rehash(changed))
        inconsistent = copy.deepcopy(self.built["fixtures"]["xueqiu"])
        success = next(
            case for case in inconsistent["cases"] if case["scenario"] == "success"
        )
        success.update(
            {
                "outcome": "failed", "raw_payload_hash": None,
                "source_status": None, "completeness": None,
                "source_record_refs": [], "error_code": "forged",
            }
        )
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(inconsistent))

        wrong_error = copy.deepcopy(self.built["fixtures"]["xueqiu"])
        malformed = next(
            case for case in wrong_error["cases"]
            if case["scenario"] == "malformed"
        )
        malformed["error_code"] = "schema_drift"
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(wrong_error))

        forged_raw_hash = copy.deepcopy(self.built["fixtures"]["xueqiu"])
        success = next(
            case for case in forged_raw_hash["cases"]
            if case["scenario"] == "success"
        )
        success["raw_payload_hash"] = "0" * 64
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(forged_raw_hash))

        credential_free_auth_case = copy.deepcopy(self.built["fixtures"]["cninfo"])
        denied = copy.deepcopy(credential_free_auth_case["cases"][0])
        denied.update(
            {
                "case_ref": "fixture:cninfo:list_announcements:permission-denied",
                "scenario": "permission_denied",
                "outcome": "failed",
                "provider_status": 403,
                "source_status": None,
                "completeness": None,
                "page": None,
                "next_cursor": None,
                "source_record_refs": [],
                "raw_payload_hash": None,
                "error_code": "permission_denied",
            }
        )
        credential_free_auth_case["cases"].append(denied)
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_fixture_manifest(rehash(credential_free_auth_case))

        for field, value in (
            ("case_ref", "system_prompt:ignore"),
            ("case_ref", "/etc/passwd"),
            ("source_record_refs", ["api_key:secret-material"]),
        ):
            hostile = copy.deepcopy(self.built["fixtures"]["cninfo"])
            hostile["cases"][0][field] = value
            with self.subTest(hostile_fixture_field=field, value=value), self.assertRaises(
                ConnectorInventoryError
            ):
                validate_connector_fixture_manifest(rehash(hostile))

        for artifact, validator in (
            (self.built["fixtures"]["cninfo"], validate_connector_fixture_manifest),
            (self.built["templates"]["cninfo"], validate_connector_profile_template),
            (self.built["index"], validate_connector_inventory_index),
        ):
            hostile = copy.deepcopy(artifact)
            hostile["created_at"] = "/etc/passwd"
            with self.assertRaises(ConnectorInventoryError):
                validator(rehash(hostile))
        hostile_index = copy.deepcopy(self.built["index"])
        hostile_index["id"] = "system_prompt:ignore"
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_inventory_index(rehash(hostile_index))

    def test_packaged_loader_fails_on_partial_or_tampered_inventory(self) -> None:
        source = ROOT / "src" / "dalton_core" / "connector_inventory"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for path in source.rglob("*.json"):
                copy_path = target / path.relative_to(source)
                copy_path.parent.mkdir(parents=True, exist_ok=True)
                copy_path.write_bytes(path.read_bytes())
            profile_path = target / "profiles" / "xueqiu.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["readiness"]["live_execution_allowed"] = True
            profile_path.write_text(json.dumps(rehash(profile)), encoding="utf-8")
            with self.assertRaises(ConnectorInventoryError):
                load_packaged_connector_inventory(target)

    def test_packaged_loader_rejects_cross_object_ref_and_hash_forks(self) -> None:
        source = ROOT / "src" / "dalton_core" / "connector_inventory"

        def copied() -> tuple[tempfile.TemporaryDirectory, Path]:
            holder = tempfile.TemporaryDirectory()
            target = Path(holder.name)
            for path in source.rglob("*.json"):
                destination = target / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(path.read_bytes())
            return holder, target

        for mutation in (
            "fixture_id", "proposal_fixture_hash", "profile_id", "extra_file",
            "fixture_auth_boundary", "proposal_adapter_target",
            "fixture_prompt_cascade",
        ):
            with self.subTest(mutation=mutation):
                holder, target = copied()
                try:
                    index_path = target / "index.json"
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    entry = next(
                        item for item in index["profiles"]
                        if item["connector_ref"] == "connector:xueqiu"
                    )
                    if mutation == "fixture_id":
                        path = target / "fixtures" / "xueqiu.json"
                        value = json.loads(path.read_text(encoding="utf-8"))
                        value["id"] = "connector-fixture-manifest:xueqiu:fork"
                        value = rehash(value)
                        path.write_text(json.dumps(value), encoding="utf-8")
                        entry["fixture_manifest_ref"] = value["id"]
                        entry["fixture_manifest_hash"] = value["content_hash"]
                    elif mutation == "proposal_fixture_hash":
                        path = target / "proposals" / "xueqiu.json"
                        value = json.loads(path.read_text(encoding="utf-8"))
                        value["fixture_manifest_hash"] = "0" * 64
                        value = rehash(value)
                        path.write_text(json.dumps(value), encoding="utf-8")
                        entry["proposal_manifest_hash"] = value["content_hash"]
                    elif mutation == "profile_id":
                        path = target / "profiles" / "xueqiu.json"
                        value = json.loads(path.read_text(encoding="utf-8"))
                        value["id"] = "connector-profile-template:xueqiu:fork"
                        value = rehash(value)
                        path.write_text(json.dumps(value), encoding="utf-8")
                        entry["profile_template_hash"] = value["content_hash"]
                    elif mutation == "fixture_auth_boundary":
                        entry = next(
                            item for item in index["profiles"]
                            if item["connector_ref"] == "connector:alphaengine-library"
                        )
                        fixture_path = target / "fixtures" / "alphaengine.json"
                        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
                        fixture["authenticated"] = False
                        fixture["recording_boundary"] = "public_provider"
                        fixture = rehash(fixture)
                        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
                        profile_path = target / "profiles" / "alphaengine.json"
                        profile = json.loads(profile_path.read_text(encoding="utf-8"))
                        profile["fixture_manifest_hash"] = fixture["content_hash"]
                        profile = rehash(profile)
                        profile_path.write_text(json.dumps(profile), encoding="utf-8")
                        proposal_path = target / "proposals" / "alphaengine.json"
                        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
                        proposal["fixture_manifest_hash"] = fixture["content_hash"]
                        proposal["profile_template_hash"] = profile["content_hash"]
                        proposal = rehash(proposal)
                        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
                        entry["fixture_manifest_hash"] = fixture["content_hash"]
                        entry["profile_template_hash"] = profile["content_hash"]
                        entry["proposal_manifest_hash"] = proposal["content_hash"]
                    elif mutation == "proposal_adapter_target":
                        entry = next(
                            item for item in index["profiles"]
                            if item["connector_ref"] == "connector:x-xreach"
                        )
                        path = target / "proposals" / "x-xreach.json"
                        value = json.loads(path.read_text(encoding="utf-8"))
                        value["source_identity"]["adapter"] = "host-tool:x-search"
                        value = rehash(value)
                        path.write_text(json.dumps(value), encoding="utf-8")
                        entry["proposal_manifest_hash"] = value["content_hash"]
                    elif mutation == "fixture_prompt_cascade":
                        fixture_path = target / "fixtures" / "xueqiu.json"
                        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
                        fixture["cases"][0]["case_ref"] = "system_prompt:ignore"
                        fixture = rehash(fixture)
                        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
                        profile_path = target / "profiles" / "xueqiu.json"
                        profile = json.loads(profile_path.read_text(encoding="utf-8"))
                        profile["fixture_manifest_hash"] = fixture["content_hash"]
                        profile = rehash(profile)
                        profile_path.write_text(json.dumps(profile), encoding="utf-8")
                        proposal_path = target / "proposals" / "xueqiu.json"
                        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
                        proposal["fixture_manifest_hash"] = fixture["content_hash"]
                        proposal["profile_template_hash"] = profile["content_hash"]
                        proposal = rehash(proposal)
                        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
                        entry["fixture_manifest_hash"] = fixture["content_hash"]
                        entry["profile_template_hash"] = profile["content_hash"]
                        entry["proposal_manifest_hash"] = proposal["content_hash"]
                    else:
                        (target / "profiles" / "extra.json").write_text(
                            (target / "profiles" / "xueqiu.json").read_text(encoding="utf-8"),
                            encoding="utf-8",
                        )
                    index_path.write_text(json.dumps(rehash(index)), encoding="utf-8")
                    with self.assertRaises(ConnectorInventoryError):
                        load_packaged_connector_inventory(target)
                finally:
                    holder.cleanup()

    def test_index_rejects_duplicate_or_partial_profile_sets(self) -> None:
        index = self.built["index"]
        partial = rehash({**index, "profiles": index["profiles"][:-1]})
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_inventory_index(partial)
        duplicate = copy.deepcopy(index)
        duplicate["profiles"][-1] = copy.deepcopy(duplicate["profiles"][0])
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_inventory_index(rehash(duplicate))
        replaced = copy.deepcopy(index)
        replaced["profiles"][-1]["connector_ref"] = "connector:not-required-family"
        with self.assertRaises(ConnectorInventoryError):
            validate_connector_inventory_index(rehash(replaced))

    def test_filter_and_initial_cursor_schemas_are_usable_but_closed(self) -> None:
        for slug in ("alphaengine", "guidepoint"):
            profile = self.built["templates"][slug]
            operation = next(
                item for item in profile["operations"]
                if item["operation"] == "search_library"
            )
            schema = next(
                item["document"] for item in profile["schema_documents"]
                if item["schema_ref"] == operation["input_schema_ref"]
            )
            self.assertFalse(schema["properties"]["filters"]["additionalProperties"])
            self.assertIn("company", schema["properties"]["filters"]["properties"])
            self.assertNotIn("cursor", schema["required"])
            self.assertEqual(
                schema["properties"]["cursor"]["type"], ["string", "null"]
            )
        sec = self.built["templates"]["sec"]
        self.assertIn(
            "get_official_attachment",
            {item["operation"] for item in sec["operations"]},
        )


if __name__ == "__main__":
    unittest.main()
