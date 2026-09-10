"""S5: the IR-page watcher -- declared pages only, one event per diff, offline.

No test here starts changedetection.io or reaches any address.  The child is
driven through its fixture mode, and the sweep is exercised against a fake
runner so that "what the lane does with a diff" is a question about this slice
rather than about the host-tool runner S1 already tests.
"""

from __future__ import annotations

import argparse
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.authority_resolver import _schema_matches
from dalton_core.connector_governance import (
    GOVERNANCE_KIND_REGISTRY,
    ConnectorGovernance,
    build_governance_record,
)
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_quota_policy import governed_daily_quota
from dalton_core.ir_page_watch_cli import (
    IrPageWatchRunError,
    run as run_child,
)
from dalton_core.ir_page_watch_core import (
    CAPABILITY_BY_OPERATION,
    DEFAULT_BASE_URL,
    DIFF_OPERATION,
    KIND_BY_OPERATION,
    LIST_OPERATION,
    OPERATIONS,
    IrPageWatchError,
    changed_at_rfc3339,
    declared_page,
    declared_watches,
    diff_hash,
    event_key,
    excerpt_of,
    host_is_declared,
    ir_page_change_payload,
    ir_page_watch_identity,
    ir_page_watch_output_schema,
    ir_page_watch_permissions,
    ir_page_watch_schema_hash,
    line_change_counts,
    load_ir_page_declaration,
    normalise_url,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ir-page-watch"
DECLARATION = ROOT / "deploy" / "phase9" / "p9-us-it-services-ir-pages-v1.json"
ACN = "company:sec-cik:0001467373"
ACN_NEWS = "https://investor.accenture.com/news-and-events/news-releases"


def args(operation: str, **overrides: object) -> argparse.Namespace:
    values = {
        "operation": operation,
        "declaration": str(DECLARATION),
        "base_url": DEFAULT_BASE_URL,
        "timeout": 5.0,
        "since": None,
        "watch_id": None,
        "output_dir": None,
        "fixture_file": str(FIXTURES / "watches.json"),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = load_packaged_connector_inventory()["templates"]["ir-page-watch"]

    def test_the_profile_is_a_host_tool_that_reaches_no_host(self) -> None:
        self.assertEqual(self.template["transport"]["kind"], "host_tool")
        self.assertEqual(self.template["transport"]["allowed_hosts"], [])
        self.assertEqual(self.template["auth_boundary"]["mode"], "none")
        self.assertEqual(
            self.template["auth_boundary"]["credential_material"], "forbidden"
        )

    def test_creating_a_watch_is_a_forbidden_route(self) -> None:
        # An operation that could add a watch would be an operation that could
        # point Dalton at any page on the internet.
        self.assertEqual(
            set(self.template["route_restrictions"]["forbidden_target_refs"]),
            {"route:changedetection-create-watch", "route:changedetection-fetch-now"},
        )

    def test_two_operations_with_two_schema_hashes(self) -> None:
        self.assertEqual(
            {item["operation"] for item in self.template["operations"]}, set(OPERATIONS)
        )
        self.assertNotEqual(
            ir_page_watch_schema_hash(LIST_OPERATION),
            ir_page_watch_schema_hash(DIFF_OPERATION),
        )
        self.assertNotEqual(
            CAPABILITY_BY_OPERATION[LIST_OPERATION],
            CAPABILITY_BY_OPERATION[DIFF_OPERATION],
        )

    def test_the_connector_declares_no_credential_and_no_network(self) -> None:
        permissions = ir_page_watch_permissions()
        self.assertFalse(permissions["network"])
        self.assertEqual(permissions["credential_slot_refs"], [])
        self.assertEqual(permissions["filesystem_write"], ["runner:raw-sink"])

    def test_both_operations_are_registered_governance_kinds(self) -> None:
        for operation, kind in KIND_BY_OPERATION.items():
            self.assertIn(kind, GOVERNANCE_KIND_REGISTRY, operation)
            record = build_governance_record(kind, approved_by="human:lumos")
            self.assertEqual(record["status"], "proposed")
            self.assertEqual(
                record["expected_schema_hash"], ir_page_watch_schema_hash(operation)
            )

    def test_the_shipped_records_are_proposed_and_load(self) -> None:
        root = ROOT / "deploy" / "connector-governance"
        for kind in KIND_BY_OPERATION.values():
            record = ConnectorGovernance.load(root / f"{kind}-v1.json")
            self.assertEqual(record.wire["status"], "proposed")
            self.assertFalse(record.approved)

    def test_both_operations_have_a_governed_quota(self) -> None:
        for operation in OPERATIONS:
            quota = governed_daily_quota("ir-page-watch", operation)
            self.assertGreaterEqual(quota["daily_unit_limit"], 1)

    def test_the_identity_names_the_operation_it_covers(self) -> None:
        identity = ir_page_watch_identity(DIFF_OPERATION)
        self.assertEqual(identity["allowed_operations"], [DIFF_OPERATION])
        with self.assertRaises(IrPageWatchError):
            ir_page_watch_identity("delete_everything")


class DeclarationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declaration = load_ir_page_declaration(DECLARATION)

    def test_every_covered_company_declares_at_least_one_page(self) -> None:
        companies = {page["company_ref"] for page in self.declaration["pages"].values()}
        self.assertEqual(len(companies), 5)
        self.assertIn(ACN, companies)

    def test_a_trailing_slash_is_the_same_page(self) -> None:
        self.assertEqual(normalise_url(ACN_NEWS + "/"), ACN_NEWS)
        self.assertIsNotNone(declared_page(self.declaration, ACN_NEWS + "/"))

    def test_a_query_string_is_a_different_page(self) -> None:
        self.assertNotEqual(normalise_url(ACN_NEWS + "?year=2026"), ACN_NEWS)
        self.assertIsNone(declared_page(self.declaration, ACN_NEWS + "?year=2026"))

    def test_an_undeclared_host_is_not_declared(self) -> None:
        self.assertFalse(host_is_declared(self.declaration, "https://example.com/x"))
        self.assertTrue(host_is_declared(self.declaration, ACN_NEWS))

    def test_a_page_declared_twice_is_refused(self) -> None:
        import tempfile

        wire = json.loads(DECLARATION.read_text(encoding="utf-8"))
        wire["companies"][1]["pages"][0]["url"] = ACN_NEWS
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(wire, handle)
            path = handle.name
        self.addCleanup(Path(path).unlink)
        with self.assertRaises(IrPageWatchError) as caught:
            load_ir_page_declaration(path)
        self.assertIn("declared twice", str(caught.exception))

    def test_a_missing_declaration_is_a_refusal_not_an_empty_allowlist(self) -> None:
        with self.assertRaises(IrPageWatchError):
            load_ir_page_declaration("/nonexistent/ir-pages.json")


class ChildTests(unittest.TestCase):
    def test_listing_marks_declared_and_counts_the_rest(self) -> None:
        wire = run_child(args(LIST_OPERATION))
        _schema_matches(wire, ir_page_watch_output_schema(LIST_OPERATION), "output")
        by_id = {row["watch_id"]: row for row in wire["watches"]}
        self.assertTrue(by_id["3f2a-acn-news"]["declared"])
        self.assertEqual(by_id["3f2a-acn-news"]["company_ref"], ACN)
        # Somebody else's watch on this shared tool: listed, so an operator
        # sees it, and never read.
        self.assertFalse(by_id["cc90-someone-else"]["declared"])
        self.assertIsNone(by_id["cc90-someone-else"]["company_ref"])
        self.assertEqual(wire["undeclared_count"], 1)

    def test_a_diff_names_the_page_the_bytes_and_what_moved(self) -> None:
        wire = run_child(args(
            DIFF_OPERATION, watch_id="3f2a-acn-news",
            fixture_file=str(FIXTURES / "acn-diff.json"),
        ))
        _schema_matches(wire, ir_page_watch_output_schema(DIFF_OPERATION), "output")
        self.assertEqual(wire["company_ref"], ACN)
        self.assertEqual(wire["url"], ACN_NEWS)
        self.assertEqual(wire["added_line_count"], 1)
        self.assertEqual(wire["removed_line_count"], 1)
        self.assertIn("managed services agreement", wire["excerpt"])
        self.assertEqual(len(wire["diff_hash"]), 64)

    def test_an_undeclared_watch_is_refused(self) -> None:
        with self.assertRaises(IrPageWatchRunError) as caught:
            run_child(args(
                DIFF_OPERATION, watch_id="cc90-someone-else",
                fixture_file=str(FIXTURES / "undeclared-diff.json"),
            ))
        self.assertIn("not a declared IR page", str(caught.exception))

    def test_the_child_writes_the_page_it_hashed(self, ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as target:
            wire = run_child(args(
                DIFF_OPERATION, watch_id="3f2a-acn-news",
                fixture_file=str(FIXTURES / "acn-diff.json"), output_dir=target,
            ))
            written = list(Path(target).glob("snapshot-*.txt"))
            self.assertEqual(len(written), 1)
            import hashlib

            self.assertEqual(
                hashlib.sha256(written[0].read_bytes()).hexdigest(),
                wire["current_snapshot_hash"],
            )

    def test_a_remote_address_is_not_a_loopback_one(self) -> None:
        with self.assertRaises(IrPageWatchRunError) as caught:
            run_child(args(
                LIST_OPERATION, fixture_file=None,
                base_url="http://evil.example.com:5055",
            ))
        self.assertIn("loopback", str(caught.exception))


class DiffIdentityTests(unittest.TestCase):
    def test_one_event_per_url_and_diff_hash(self) -> None:
        one = diff_hash(
            url=ACN_NEWS, previous_snapshot_hash="a" * 64,
            current_snapshot_hash="b" * 64,
        )
        # The same pair of snapshots, read again tomorrow, is the same change.
        self.assertEqual(
            one,
            diff_hash(
                url=ACN_NEWS + "/", previous_snapshot_hash="a" * 64,
                current_snapshot_hash="b" * 64,
            ),
        )
        self.assertNotEqual(
            one,
            diff_hash(
                url=ACN_NEWS, previous_snapshot_hash="a" * 64,
                current_snapshot_hash="c" * 64,
            ),
        )
        self.assertEqual(event_key(ACN_NEWS, one), event_key(ACN_NEWS + "/", one))

    def test_a_first_snapshot_has_no_previous_and_still_names_itself(self) -> None:
        self.assertEqual(len(diff_hash(
            url=ACN_NEWS, previous_snapshot_hash=None, current_snapshot_hash="b" * 64,
        )), 64)

    def test_line_counts_are_a_multiset_difference(self) -> None:
        self.assertEqual(line_change_counts("a\nb\n", "b\nc\n"), (1, 1))
        self.assertEqual(line_change_counts(None, "a\nb\n"), (2, 0))
        self.assertEqual(line_change_counts("a\n", "a\n"), (0, 0))

    def test_an_excerpt_is_the_new_lines_or_nothing(self) -> None:
        self.assertEqual(excerpt_of("a\n", "a\nb\n"), "b")
        self.assertIsNone(excerpt_of("a\n", "a\n"))

    def test_the_change_stamp_is_utc_or_the_sweep_clock(self) -> None:
        fallback = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
        self.assertEqual(
            changed_at_rfc3339("1788900000", fallback=fallback),
            datetime.fromtimestamp(1788900000, tz=timezone.utc).isoformat(
                timespec="microseconds"
            ),
        )
        # A naive stamp is not silently read as UTC.
        self.assertEqual(
            changed_at_rfc3339("2026-09-08T10:00:00", fallback=fallback),
            fallback.isoformat(timespec="microseconds"),
        )
        self.assertEqual(
            changed_at_rfc3339(None, fallback=fallback),
            fallback.isoformat(timespec="microseconds"),
        )

    def test_the_payload_is_what_the_event_ledger_declares(self) -> None:
        from dalton_core.research_event import PAYLOAD_FIELDS, validate_payload

        wire = run_child(args(
            DIFF_OPERATION, watch_id="3f2a-acn-news",
            fixture_file=str(FIXTURES / "acn-diff.json"),
        ))
        payload = ir_page_change_payload(
            wire, artifact_hash="d" * 64, invocation_ref="connector-invocation:x",
        )
        self.assertEqual(set(payload) - PAYLOAD_FIELDS["ir_page_change"], set())
        validate_payload("ir_page_change", payload)

    def test_declared_watches_keeps_the_declaration_order(self) -> None:
        declaration = load_ir_page_declaration(DECLARATION)
        wire = run_child(args(LIST_OPERATION))
        found = declared_watches(declaration, wire["watches"])
        self.assertEqual(len(found), 2)
        self.assertTrue(all(row["declared"] for row in found))


if __name__ == "__main__":
    unittest.main()
