"""S4: the China / Hong Kong fundamentals identity, and what it refuses.

Dalton could read American filings and American prices and nothing at all about
a company listed in Shanghai, Shenzhen or Hong Kong. Six operations, six
capabilities, six approvals: reading one company's income statement and reading
the whole market's margin balance are not the same permission, and a schema
hash binds exactly one operation so one record cannot quietly cover another.

The pinned CNINFO hashes are here rather than in the inventory tests because
this is the change that could have moved them. Adding a connector rewrites
``index.json``; if it rewrites anything else, the announcement connector that
is already live stops matching its own approval.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from dalton_core.cn_hk_findata_core import (
    ADAPTER_LIBRARY,
    ADAPTER_LIBRARY_VERSION,
    CALIBER_NOTES,
    CAPABILITY_BY_OPERATION,
    CnHkFinDataError,
    FUNCTIONS_BY_OPERATION,
    HOSTS_BY_OPERATION,
    KIND_BY_OPERATION,
    NO_BATCH_PROBE_HOSTS,
    OPERATIONS,
    REFUSED_VENDOR_ROUTES,
    VENDORS_BY_OPERATION,
    build_cn_hk_findata_governance_record,
    cn_hk_findata_adapter_hash,
    cn_hk_findata_contract,
    cn_hk_findata_identity,
    cn_hk_findata_output_schema,
    cn_hk_findata_schema_hash,
    cn_hk_findata_source_hash,
    invocation_ref,
    refused_vendor_route,
)
from dalton_core.connector_governance import (
    ConnectorGovernance,
    build_governance_record,
    governance_kind_for_capability,
)
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_quota_policy import governed_daily_quota

REPO = Path(__file__).resolve().parents[1]
GOVERNANCE_DIR = REPO / "deploy" / "connector-governance"
OWNER = "human:lumos"

# The announcement connector as it stood before this one existed. Typed once,
# here, so that a change which moves them has to move this line too.
CNINFO_PINNED = {
    "profile_template_hash":
        "7fd228dddee5cd74d70cd79b11e4bed3a61539f37ca5860427b899e0196a9ae7",
    "fixture_manifest_hash":
        "1d383fdae0494d324f1740613912ff09d5e1bee17bc4cbc9909b8cbfd6bdfc8e",
    "proposal_manifest_hash":
        "4c53d26e9e6ba7f24e9dda663461bea494bdf3c0ccc6a9b12fd68ca7ed788897",
}


class IdentityTests(unittest.TestCase):
    def test_every_operation_has_its_own_schema_hash(self):
        hashes = {op: cn_hk_findata_schema_hash(op) for op in OPERATIONS}
        self.assertEqual(len(set(hashes.values())), len(OPERATIONS), hashes)

    def test_the_six_share_one_source_hash(self):
        self.assertEqual(
            {cn_hk_findata_identity(op)["source_hash"] for op in OPERATIONS},
            {cn_hk_findata_source_hash()},
        )

    def test_an_operation_nobody_froze_is_refused(self):
        with self.assertRaises(CnHkFinDataError):
            cn_hk_findata_schema_hash("fund_flow_individual")

    def test_the_adapter_hash_names_the_library_and_its_version(self):
        first = cn_hk_findata_adapter_hash("buybacks")
        import dalton_core.cn_hk_findata_core as core

        original = core.ADAPTER_LIBRARY_VERSION
        try:
            core.ADAPTER_LIBRARY_VERSION = "1.99.0"
            self.assertNotEqual(first, cn_hk_findata_adapter_hash("buybacks"))
        finally:
            core.ADAPTER_LIBRARY_VERSION = original
        self.assertEqual(first, cn_hk_findata_adapter_hash("buybacks"))

    def test_the_library_is_akshare(self):
        self.assertEqual(ADAPTER_LIBRARY, "akshare")
        self.assertRegex(ADAPTER_LIBRARY_VERSION, r"^\d+\.\d+\.\d+$")

    def test_the_operation_set_is_the_one_that_was_approved(self):
        self.assertEqual(
            set(OPERATIONS),
            {
                "financial_statements", "shareholders", "buybacks",
                "margin_balance", "northbound_flow", "ah_premium",
            },
        )

    def test_an_invocation_ref_changes_when_the_bytes_change(self):
        common = dict(
            operation="buybacks", governance_ref="connector-governance:x:v1",
            governance_hash="a" * 64, parameters={"a_ticker": "600519"},
        )
        first = invocation_ref(artifact_hash="b" * 64, **common)
        second = invocation_ref(artifact_hash="c" * 64, **common)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("connector-invocation:cn-hk-findata:"))


class HostTests(unittest.TestCase):
    def profile(self):
        return load_packaged_connector_inventory()["templates"]["cn-hk-findata"]

    def test_every_operation_host_is_on_the_template_allowlist(self):
        allowed = set(self.profile()["transport"]["allowed_hosts"])
        for operation in OPERATIONS:
            for host in HOSTS_BY_OPERATION[operation]:
                self.assertIn(host, allowed, f"{operation} reaches {host}")

    def test_the_allowlist_has_nothing_no_operation_reaches(self):
        reached = {host for operation in OPERATIONS
                   for host in HOSTS_BY_OPERATION[operation]}
        self.assertEqual(set(self.profile()["transport"]["allowed_hosts"]), reached)

    def test_the_tencent_ah_host_is_not_reachable_at_all(self):
        # It is a real alternative and it returns the H-share quote with no
        # premium in it. Declaring the host would make answering the wrong
        # question a one-line change.
        self.assertNotIn(
            "stock.gtimg.cn", self.profile()["transport"]["allowed_hosts"])
        route = refused_vendor_route("ah_premium", "tencent")
        self.assertIsNotNone(route)
        self.assertIn("比价", route["reason"])

    def test_the_natural_language_router_is_a_forbidden_route(self):
        forbidden = self.profile()["route_restrictions"]["forbidden_target_refs"]
        self.assertIn("route:cn-hk-findata-nl-router", forbidden)
        self.assertIn("route:eastmoney-push2his-batch-probe", forbidden)
        self.assertIn("route:tencent-ah-quote-list", forbidden)

    def test_no_operation_declares_a_permitted_fallback(self):
        # The skill has twelve vendor fallbacks and not one of them is for
        # these six. A row from 同花顺 or 新浪 is a different 口径 and cannot
        # enter under an approval that names 东财.
        self.assertEqual(
            self.profile()["route_restrictions"]["fallback_routes"], [])
        for operation in OPERATIONS:
            self.assertLessEqual(len(VENDORS_BY_OPERATION[operation]), 2)

    def test_the_one_quote_cluster_host_is_marked_do_not_probe(self):
        self.assertEqual(HOSTS_BY_OPERATION["ah_premium"], ("push2.eastmoney.com",))
        self.assertIn("push2.eastmoney.com", NO_BATCH_PROBE_HOSTS)

    def test_the_vendor_enum_in_the_contract_matches_the_identity(self):
        for operation in OPERATIONS:
            schema = cn_hk_findata_output_schema(operation)
            rows = schema["properties"].get("lines") or schema["properties"].get("rows")
            if rows is None:
                rows = schema["properties"]["top_holders"]
            enum = rows["items"]["properties"]["source_vendor"]["enum"]
            self.assertEqual(tuple(enum), VENDORS_BY_OPERATION[operation])

    def test_every_operation_row_carries_the_three_provenance_fields(self):
        for operation in OPERATIONS:
            schema = cn_hk_findata_output_schema(operation)
            blocks = [
                value for key, value in schema["properties"].items()
                if key in {"lines", "rows", "top_holders", "holder_counts"}
            ]
            self.assertTrue(blocks, operation)
            for block in blocks:
                required = set(block["items"]["required"])
                self.assertLessEqual(
                    {"source_vendor", "fallback_used", "caliber_note"}, required,
                    f"{operation} row is missing a provenance field")

    def test_every_frozen_function_is_named(self):
        for operation in OPERATIONS:
            self.assertTrue(FUNCTIONS_BY_OPERATION[operation], operation)
            for function in FUNCTIONS_BY_OPERATION[operation]:
                self.assertTrue(function.startswith("stock_"), function)

    def test_the_refused_routes_all_carry_a_reason(self):
        for route in REFUSED_VENDOR_ROUTES:
            self.assertIn(route["operation"], OPERATIONS)
            self.assertNotIn(route["vendor"], VENDORS_BY_OPERATION[route["operation"]])
            self.assertGreater(len(route["reason"]), 20)


class QuotaTests(unittest.TestCase):
    def test_every_operation_declares_a_daily_ceiling(self):
        for operation in OPERATIONS:
            quota = governed_daily_quota("cn-hk-findata", operation)
            self.assertGreaterEqual(quota["daily_unit_limit"], 1)
            self.assertGreaterEqual(quota["max_physical_calls_per_unit"], 1)

    def test_the_quote_cluster_operation_has_the_smallest_allowance(self):
        # 「不要批量探测东财」 expressed as arithmetic: four a day against the
        # host that went quiet in August 2026 cannot look like probing.
        ah = governed_daily_quota("cn-hk-findata", "ah_premium")
        self.assertLessEqual(ah["daily_unit_limit"], 4)
        for operation in OPERATIONS:
            quota = governed_daily_quota("cn-hk-findata", operation)
            self.assertLessEqual(quota["daily_unit_limit"], 40, operation)


class ShippedRecordTests(unittest.TestCase):
    def test_all_six_records_ship_and_match_the_builder(self):
        for operation in OPERATIONS:
            kind = KIND_BY_OPERATION[operation]
            path = GOVERNANCE_DIR / f"{kind}-v1.json"
            shipped = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                shipped,
                build_cn_hk_findata_governance_record(
                    operation=operation, approved_by=OWNER, status="proposed"),
                f"{kind} on disk differs from what the identity builds",
            )

    def test_every_shipped_record_is_proposed_and_loads(self):
        for operation in OPERATIONS:
            path = GOVERNANCE_DIR / f"{KIND_BY_OPERATION[operation]}-v1.json"
            governance = ConnectorGovernance.load(path)
            self.assertFalse(governance.approved)
            self.assertEqual(
                governance.capability_id, CAPABILITY_BY_OPERATION[operation])

    def test_the_registry_maps_each_capability_back_to_its_own_kind(self):
        for operation in OPERATIONS:
            self.assertEqual(
                governance_kind_for_capability(CAPABILITY_BY_OPERATION[operation]),
                KIND_BY_OPERATION[operation],
            )

    def test_the_generic_builder_reaches_the_same_identity(self):
        for operation in OPERATIONS:
            record = build_governance_record(
                KIND_BY_OPERATION[operation], approved_by=OWNER)
            self.assertEqual(
                record["expected_schema_hash"],
                cn_hk_findata_schema_hash(operation))

    def test_a_record_cannot_be_approved_by_something_other_than_a_person(self):
        with self.assertRaises(CnHkFinDataError):
            build_cn_hk_findata_governance_record(
                operation="buybacks", approved_by="core:automation")


class PackagedInventoryTests(unittest.TestCase):
    def test_the_packaged_inventory_matches_the_frozen_definitions(self):
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "build_connector_inventory.py"),
             "--check"],
            capture_output=True, text=True, cwd=str(REPO),
            env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_adding_this_connector_did_not_move_the_cninfo_hashes(self):
        index = load_packaged_connector_inventory()["index"]
        entry = next(item for item in index["profiles"]
                     if item["connector_ref"] == "connector:cninfo-announcements")
        for field, value in CNINFO_PINNED.items():
            self.assertEqual(entry[field], value, field)

    def test_the_template_is_public_https_with_no_credential(self):
        template, _ = cn_hk_findata_contract("buybacks")
        self.assertEqual(template["transport"]["kind"], "public_https")
        self.assertEqual(template["auth_boundary"]["mode"], "none")
        self.assertEqual(
            template["auth_boundary"]["credential_material"], "forbidden")

    def test_only_the_margin_operation_claims_to_be_enumerated(self):
        template, _ = cn_hk_findata_contract("buybacks")
        ceilings = {item["operation"]: item["completeness_ceiling"]
                    for item in template["operations"]}
        self.assertEqual(ceilings["margin_balance"], "enumerated")
        for operation in OPERATIONS:
            if operation != "margin_balance":
                self.assertEqual(ceilings[operation], "partial", operation)

    def test_the_caliber_notes_say_something(self):
        for key, note in CALIBER_NOTES.items():
            self.assertGreater(len(note), 10, key)


if __name__ == "__main__":
    unittest.main()
