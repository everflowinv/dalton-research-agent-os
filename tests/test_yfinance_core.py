"""P11a: the Yahoo Finance identity, split the way every library here is.

Dalton could see filings and could not see the market, and the whole valuation
branch of the Initial Screen was fail-closed behind that. Yahoo is the owner's
choice: free, already backing three of their own tools, and a decision that can
be revisited once the market layer has earned a paid vendor.

Prices and analyst estimates are two operations, two capabilities and two
approvals -- a market print and an analyst's opinion are not the same kind of
thing, and a schema hash binds one operation, so one record cannot cover both.
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
from dalton_core.connector_quota_policy import governed_daily_quota
from dalton_core.yfinance_core import (
    ADAPTER_LIBRARY,
    ANALYST_ESTIMATES_KIND,
    ANALYST_ESTIMATES_OPERATION,
    CALENDAR_OPERATION,
    DAILY_PRICES_KIND,
    DAILY_PRICES_OPERATION,
    YFinanceError,
    build_yfinance_governance_record,
    invocation_ref,
    yfinance_adapter_hash,
    yfinance_identity,
    yfinance_output_schema,
    yfinance_permissions,
    yfinance_schema_hash,
    yfinance_source_hash,
)

REPO = Path(__file__).resolve().parents[1]
GOVERNANCE_DIR = REPO / "deploy" / "connector-governance"
OWNER = "human:lumos"


class IdentityTests(unittest.TestCase):
    def test_the_two_operations_do_not_share_a_schema_hash(self):
        self.assertNotEqual(
            yfinance_schema_hash(DAILY_PRICES_OPERATION),
            yfinance_schema_hash(ANALYST_ESTIMATES_OPERATION),
        )

    def test_one_yahoo_is_one_source(self):
        self.assertEqual(
            yfinance_identity(DAILY_PRICES_OPERATION)["source_hash"],
            yfinance_identity(ANALYST_ESTIMATES_OPERATION)["source_hash"],
        )

    def test_it_is_not_the_same_source_as_sec(self):
        # Both carry numbers about the same company. They are not the same
        # source, and must not share a source hash, or one approval would
        # describe the other.
        from dalton_core.sec_financials_core import sec_financials_source_hash

        self.assertNotEqual(yfinance_source_hash(), sec_financials_source_hash())

    def test_the_adapter_hash_names_the_library(self):
        # Swapping the scraper is a different capability, because the scraper
        # is the part the owner is being asked to trust. The hash moves when
        # the library name does, and the two operations do not share one.
        from dalton_core.store import content_hash
        from dalton_core.yfinance_core import yfinance_contract

        template, _ = yfinance_contract(DAILY_PRICES_OPERATION)
        expected = content_hash({
            "target_ref": template["transport"]["target_ref"],
            "source": template["source_identity"]["source_ref"],
            "operation": DAILY_PRICES_OPERATION,
            "library": ADAPTER_LIBRARY,
        })
        self.assertEqual(yfinance_adapter_hash(DAILY_PRICES_OPERATION), expected)
        moved = content_hash({
            "target_ref": template["transport"]["target_ref"],
            "source": template["source_identity"]["source_ref"],
            "operation": DAILY_PRICES_OPERATION,
            "library": "some-other-scraper",
        })
        self.assertNotEqual(expected, moved)
        self.assertNotEqual(
            yfinance_adapter_hash(DAILY_PRICES_OPERATION),
            yfinance_adapter_hash(ANALYST_ESTIMATES_OPERATION),
        )

    def test_an_identity_admits_exactly_one_operation(self):
        for operation in (DAILY_PRICES_OPERATION, ANALYST_ESTIMATES_OPERATION):
            self.assertEqual(
                yfinance_identity(operation)["allowed_operations"], [operation]
            )

    def test_an_operation_nobody_froze_is_refused(self):
        with self.assertRaises(YFinanceError):
            yfinance_identity("get_financial_statements")

    def test_the_capabilities_map_back_to_their_kinds(self):
        self.assertEqual(
            governance_kind_for_capability(
                yfinance_identity(DAILY_PRICES_OPERATION)["capability_id"]),
            DAILY_PRICES_KIND)
        self.assertEqual(
            governance_kind_for_capability(
                yfinance_identity(ANALYST_ESTIMATES_OPERATION)["capability_id"]),
            ANALYST_ESTIMATES_KIND)


class ContractTests(unittest.TestCase):
    def test_the_template_reaches_only_yahoo_without_a_credential(self):
        template = load_packaged_connector_inventory()["templates"]["yfinance"]
        self.assertEqual(template["transport"]["kind"], "public_https")
        self.assertEqual(template["transport"]["host_policy"], "literal_allowlist")
        self.assertEqual(
            template["transport"]["allowed_hosts"],
            ["query1.finance.yahoo.com", "query2.finance.yahoo.com"],
        )
        self.assertEqual(template["auth_boundary"]["mode"], "none")
        self.assertEqual(template["source_identity"]["source_ref"], "source:yahoo-finance")

    def test_the_price_contract_keeps_close_and_adj_close_apart(self):
        # The lesson the owner's other tooling paid for: one column cannot be
        # both, and a reader holding one must be able to tell which.
        schema = yfinance_output_schema(DAILY_PRICES_OPERATION)
        bar = schema["properties"]["bars"]["items"]["properties"]
        self.assertIn("close", bar)
        self.assertIn("adj_close", bar)
        self.assertNotEqual(bar["close"], bar.get("adjusted"))

    def test_the_price_contract_forbids_an_adjusted_run(self):
        schema = yfinance_output_schema(DAILY_PRICES_OPERATION)
        self.assertEqual(schema["properties"]["auto_adjust"]["enum"], [False])

    def test_share_count_and_market_cap_are_separately_dated(self):
        schema = yfinance_output_schema(DAILY_PRICES_OPERATION)
        observation = schema["properties"]["observations"]["items"]
        self.assertEqual(
            observation["properties"]["observation"]["enum"],
            ["shares_outstanding", "market_cap"],
        )
        self.assertIn("as_of", observation["required"])
        # And they are not fields on a bar: Yahoo cannot say what the share
        # count was on a Tuesday in 2024.
        self.assertNotIn(
            "shares_outstanding",
            schema["properties"]["bars"]["items"]["properties"],
        )

    def test_the_estimates_contract_carries_targets_ratings_eps_and_revenue(self):
        schema = yfinance_output_schema(ANALYST_ESTIMATES_OPERATION)
        self.assertEqual(
            sorted(schema["properties"]["price_target"]["required"]),
            ["current", "high", "low", "mean", "median", "number_of_analysts"],
        )
        for block in ("recommendations", "eps_estimates", "revenue_estimates"):
            self.assertIn(block, schema["properties"])

    def test_the_connector_cannot_serve_a_financial_statement(self):
        # Yahoo has them; this connector does not expose them, because a
        # scraped copy of a filed figure is a worse number wearing the same
        # clothes.
        template = load_packaged_connector_inventory()["templates"]["yfinance"]
        operations = {item["operation"] for item in template["operations"]}
        # C1 added `calendar`. The point of this assertion is unchanged: the
        # set is closed and no statement operation is in it, so widening it
        # has to be a deliberate edit here rather than something that happens.
        self.assertEqual(operations, {
            DAILY_PRICES_OPERATION, ANALYST_ESTIMATES_OPERATION, CALENDAR_OPERATION,
        })


class PermissionAndQuotaTests(unittest.TestCase):
    def test_it_is_credential_free_public_web(self):
        permissions = yfinance_permissions()
        self.assertEqual(permissions["credential_slot_refs"], [])
        self.assertIs(permissions["core_db"], False)
        self.assertIs(permissions["network"], True)

    def test_mutating_a_returned_permission_set_cannot_reach_the_next_caller(self):
        first = yfinance_permissions()
        first["network"] = False
        self.assertIs(yfinance_permissions()["network"], True)

    def test_the_quota_is_modest_because_the_source_never_agreed_to_serve_us(self):
        quota = governed_daily_quota("yfinance", DAILY_PRICES_OPERATION)
        self.assertEqual(quota["quota_unit"], "search")
        self.assertLessEqual(quota["daily_unit_limit"], 200)
        # Two physical calls per unit: the download, and the metadata read
        # that carries the share count.
        self.assertEqual(quota["max_physical_calls_per_unit"], 2)
        estimates = governed_daily_quota("yfinance", ANALYST_ESTIMATES_OPERATION)
        self.assertLess(
            estimates["daily_unit_limit"], quota["daily_unit_limit"]
        )


class GovernanceTests(unittest.TestCase):
    def test_a_record_is_proposed_and_needs_a_human(self):
        self.assertEqual(
            build_yfinance_governance_record(
                operation=DAILY_PRICES_OPERATION, approved_by=OWNER)["status"],
            "proposed")
        with self.assertRaises(YFinanceError):
            build_yfinance_governance_record(
                operation=DAILY_PRICES_OPERATION, approved_by="automation:dalton")

    def test_each_record_binds_its_own_operation(self):
        for operation in (DAILY_PRICES_OPERATION, ANALYST_ESTIMATES_OPERATION):
            record = build_yfinance_governance_record(
                operation=operation, approved_by=OWNER)
            self.assertEqual(
                record["expected_schema_hash"], yfinance_schema_hash(operation))

    def test_the_shared_builder_produces_the_same_record(self):
        direct = build_yfinance_governance_record(
            operation=ANALYST_ESTIMATES_OPERATION, approved_by=OWNER,
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        shared = build_governance_record(
            ANALYST_ESTIMATES_KIND, approved_by=OWNER, status="proposed",
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        self.assertEqual(direct, shared)


class ShippedRecordTests(unittest.TestCase):
    def records(self):
        for kind, operation in (
            (DAILY_PRICES_KIND, DAILY_PRICES_OPERATION),
            (ANALYST_ESTIMATES_KIND, ANALYST_ESTIMATES_OPERATION),
        ):
            path = GOVERNANCE_DIR / f"{kind}-v1.json"
            self.assertTrue(path.is_file(), f"{path} is not shipped")
            yield operation, json.loads(path.read_text(encoding="utf-8"))

    def test_the_shipped_records_are_proposed_not_approved(self):
        for _operation, record in self.records():
            self.assertEqual(record["status"], "proposed")

    def test_the_shipped_records_match_the_packaged_template(self):
        for operation, record in self.records():
            self.assertEqual(
                record["expected_schema_hash"], yfinance_schema_hash(operation))
            self.assertEqual(record["expected_source_hash"], yfinance_source_hash())

    def test_the_shipped_records_load_and_are_not_yet_usable(self):
        for _operation, record in self.records():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(record, handle)
                path = handle.name
            try:
                self.assertFalse(ConnectorGovernance.load(path).approved)
            finally:
                Path(path).unlink()


class InvocationRefTests(unittest.TestCase):
    def parameters(self, **overrides):
        base = {"ticker": "ACN", "start": "2026-09-01", "end": "2026-09-05"}
        base.update(overrides)
        return base

    def call(self, **overrides):
        request = {
            "operation": DAILY_PRICES_OPERATION,
            "governance_ref": "connector-governance:yfinance-daily-prices:v1",
            "governance_hash": "a" * 64,
            "parameters": self.parameters(),
            "artifact_hash": "b" * 64,
        }
        request.update(overrides)
        return invocation_ref(**request)

    def test_the_same_call_with_the_same_bytes_is_the_same_invocation(self):
        self.assertEqual(self.call(), self.call())

    def test_different_bytes_are_a_different_invocation(self):
        # A restatement, a split, a Yahoo correction: the window is the same
        # and what came back is not, which is exactly the distinction the
        # version chain needs to be able to make.
        self.assertNotEqual(self.call(), self.call(artifact_hash="c" * 64))

    def test_a_different_window_is_a_different_invocation(self):
        self.assertNotEqual(
            self.call(), self.call(parameters=self.parameters(end="2026-09-06")))

    def test_a_different_approval_is_a_different_invocation(self):
        self.assertNotEqual(self.call(), self.call(governance_hash="d" * 64))


if __name__ == "__main__":
    unittest.main()
