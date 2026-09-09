"""S3: the three crowd connectors' identity, governance, quota and grade.

The identity half is the usual: a template that renders from the frozen
definitions, a source hash shared inside a connector, and a schema hash bound
to one operation so that approving one does not widen into another.

The grade half is the part worth reading. These sources may never produce a
figure, and the way that is enforced is by *absence*: a document kind with no
entry in `document_figure_grade.GRADE_BY_SPEC` is a kind no figure may be read
from. The tests below pin that absence, so that adding a grade for a crowd
source later has to be a decision somebody makes on purpose.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.connector_governance import (
    EMPLOYEE_REVIEWS_KIND,
    GOVERNANCE_KIND_REGISTRY,
    XREACH_KINDS,
    XUEQIU_KINDS,
    ConnectorGovernance,
    build_governance_record,
)
from dalton_core.connector_inventory import (
    build_connector_inventory,
    load_packaged_connector_inventory,
)
from dalton_core.connector_quota_policy import governed_daily_quota
from dalton_core.document_figure_grade import (
    FigureGradeError,
    figure_worthy,
    grade_for,
    require_grade,
)
from dalton_core.employee_reviews_core import (
    OPERATION as BLIND_OPERATION,
    body_locked,
    employee_reviews_identity,
    employee_reviews_permissions,
)
from dalton_core.mission_crowd_source_lane import (
    CROWD_GRADE,
    CROWD_IMPORTANCE,
    CROWD_SPEC_REFS,
    admissible_as_sole_quantitative_source,
    crowd_qualify,
)
from dalton_core.xreach_core import (
    CREDENTIAL_SLOT_REFS,
    NOT_BUILT_OPERATIONS,
    xreach_identity,
    xreach_permissions,
)
from dalton_core.xueqiu_core import (
    CREDENTIALLED_OPERATIONS,
    CREDENTIAL_SLOT_REF,
    xueqiu_fallback_route,
    xueqiu_identity,
    xueqiu_permissions,
)

ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_DIR = ROOT / "deploy" / "connector-governance"


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.built = build_connector_inventory()["templates"]

    def test_the_three_crowd_templates_are_packaged(self):
        for slug in ("xueqiu-posts", "x-xreach-crowd", "employee-reviews"):
            self.assertIn(slug, self.built)
            self.assertIn(slug, load_packaged_connector_inventory()["templates"])

    def test_the_shadow_templates_they_descend_from_are_untouched(self):
        # The whole reason these are new connectors: the 2026-08-14 hashes the
        # owner has already seen must not move.
        self.assertEqual(
            self.built["xueqiu"]["content_hash"],
            "a811f9bd5c7b4086670b5539a7a6cc40911a86aa2a61a80ca0eed2d921050400",
        )
        self.assertEqual(
            self.built["x-xreach"]["content_hash"],
            "7bb63f16a3c71c668e0e0acb8390e8d9e18c0641609d3323dbf4c37ee34c413d",
        )

    def test_the_crowd_templates_reuse_the_shadow_targets(self):
        self.assertEqual(
            self.built["xueqiu-posts"]["transport"]["target_ref"],
            self.built["xueqiu"]["transport"]["target_ref"],
        )
        self.assertEqual(
            self.built["x-xreach-crowd"]["transport"]["target_ref"],
            self.built["x-xreach"]["transport"]["target_ref"],
        )
        self.assertEqual(
            self.built["xueqiu-posts"]["source_identity"]["source_ref"],
            self.built["xueqiu"]["source_identity"]["source_ref"],
        )

    def test_the_hot_rank_fallback_is_scoped_to_the_ranking_alone(self):
        routes = self.built["xueqiu-posts"]["route_restrictions"]["fallback_routes"]
        self.assertEqual([route["operation"] for route in routes], ["hot_rank"])
        self.assertEqual(
            routes[0]["target_ref"], "host-tool:cn-hk-findata-xq-hot-rank")
        self.assertIsNotNone(xueqiu_fallback_route("hot_rank"))
        self.assertIsNone(xueqiu_fallback_route("search_posts"))

    def test_x_semantic_search_is_not_an_operation_of_this_connector(self):
        operations = {item["operation"]
                      for item in self.built["x-xreach-crowd"]["operations"]}
        self.assertEqual(operations, {"user_timeline", "search", "thread"})
        self.assertNotIn(NOT_BUILT_OPERATIONS[0], operations)

    def test_the_search_ceiling_is_ranked_and_the_timeline_is_enumerated(self):
        ceilings = {item["operation"]: item["completeness_ceiling"]
                    for item in self.built["x-xreach-crowd"]["operations"]}
        self.assertEqual(ceilings["search"], "ranked")
        self.assertEqual(ceilings["user_timeline"], "enumerated")

    def test_blind_is_public_https_to_one_host_and_needs_no_credential(self):
        template = self.built["employee-reviews"]
        self.assertEqual(template["transport"]["kind"], "public_https")
        self.assertEqual(template["transport"]["allowed_hosts"], ["www.teamblind.com"])
        self.assertEqual(template["auth_boundary"]["mode"], "none")
        self.assertEqual(employee_reviews_permissions()["credential_slot_refs"], [])

    def test_the_body_lock_is_why_blind_is_only_partial(self):
        ceiling = self.built["employee-reviews"]["operations"][0]["completeness_ceiling"]
        self.assertEqual(ceiling, "partial")

    def test_the_source_method_may_differ_from_the_dalton_operation(self):
        methods = {item["operation"]: item["source_method"]
                   for item in self.built["x-xreach-crowd"]["operations"]}
        self.assertEqual(methods["user_timeline"], "tweets")
        methods = {item["operation"]: item["source_method"]
                   for item in self.built["xueqiu-posts"]["operations"]}
        self.assertEqual(methods["hot_rank"], "get_hot_stocks")


class IdentityTests(unittest.TestCase):
    def test_one_source_hash_and_one_schema_hash_per_operation(self):
        hashes = {op: xueqiu_identity(op)["schema_hash"]
                  for op in ("search_posts", "get_post", "hot_rank")}
        self.assertEqual(len(set(hashes.values())), 3)
        sources = {xueqiu_identity(op)["source_hash"] for op in hashes}
        self.assertEqual(len(sources), 1)

    def test_only_the_post_operations_require_the_cookie_slot(self):
        self.assertEqual(CREDENTIALLED_OPERATIONS, ("search_posts", "get_post"))
        self.assertTrue(xueqiu_identity("search_posts")["requires_credential_slot"])
        self.assertFalse(xueqiu_identity("hot_rank")["requires_credential_slot"])
        self.assertEqual(
            xueqiu_permissions()["credential_slot_refs"], [CREDENTIAL_SLOT_REF])

    def test_x_needs_both_cookie_slots_for_every_operation(self):
        self.assertEqual(len(CREDENTIAL_SLOT_REFS), 2)
        for operation in ("user_timeline", "search", "thread"):
            self.assertEqual(
                xreach_identity(operation)["credential_slot_refs"],
                list(CREDENTIAL_SLOT_REFS),
            )
        self.assertEqual(
            xreach_permissions()["credential_slot_refs"], list(CREDENTIAL_SLOT_REFS))

    def test_no_permission_declaration_carries_material(self):
        for permissions in (xueqiu_permissions(), xreach_permissions(),
                            employee_reviews_permissions()):
            for slot in permissions["credential_slot_refs"]:
                self.assertTrue(slot.startswith("credential-slot:"))
            self.assertFalse(permissions["core_db"])
            self.assertEqual(permissions["filesystem_write"], ["runner:raw-sink"])


class GovernanceTests(unittest.TestCase):
    def kinds(self) -> list[str]:
        return sorted(XUEQIU_KINDS | XREACH_KINDS | {EMPLOYEE_REVIEWS_KIND})

    def test_every_operation_has_its_own_registered_kind(self):
        self.assertEqual(len(self.kinds()), 7)
        for kind in self.kinds():
            self.assertIn(kind, GOVERNANCE_KIND_REGISTRY)

    def test_a_record_is_bound_to_one_operation_and_cannot_be_reused(self):
        records = {kind: build_governance_record(kind, approved_by="human:tester")
                   for kind in self.kinds()}
        schema_hashes = {record["expected_schema_hash"] for record in records.values()}
        self.assertEqual(len(schema_hashes), 7)

    def test_the_committed_records_are_proposed_and_still_describe_the_contract(self):
        for kind in self.kinds():
            path = GOVERNANCE_DIR / f"{kind}-v1.json"
            self.assertTrue(path.exists(), f"{kind} has no committed record")
            wire = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(wire["status"], "proposed", kind)
            governance = ConnectorGovernance(wire)
            self.assertFalse(governance.approved)
            rebuilt = build_governance_record(kind, approved_by=wire["approved_by"],
                                              effective_from=wire["effective_from"])
            self.assertEqual(wire, rebuilt, kind)


class QuotaTests(unittest.TestCase):
    def test_every_crowd_operation_declares_a_conservative_daily_ceiling(self):
        pairs = [
            ("xueqiu-posts", "search_posts"), ("xueqiu-posts", "get_post"),
            ("xueqiu-posts", "hot_rank"), ("x-xreach-crowd", "user_timeline"),
            ("x-xreach-crowd", "search"), ("x-xreach-crowd", "thread"),
            ("employee-reviews", "blind_reviews"),
        ]
        for slug, operation in pairs:
            quota = governed_daily_quota(slug, operation)
            self.assertEqual(quota["daily_unit_limit"], 50, f"{slug}/{operation}")
            self.assertGreaterEqual(quota["max_physical_calls_per_unit"], 1)


class GradeTests(unittest.TestCase):
    def test_no_figure_may_be_read_from_a_crowd_source(self):
        for spec_ref in CROWD_SPEC_REFS:
            self.assertIsNone(grade_for(spec_ref))
            self.assertFalse(figure_worthy(spec_ref))
            with self.assertRaises(FigureGradeError):
                require_grade(spec_ref)

    def test_the_crowd_grade_is_never_a_sole_quantitative_source(self):
        self.assertFalse(admissible_as_sole_quantitative_source(CROWD_GRADE))
        self.assertTrue(
            admissible_as_sole_quantitative_source("company-filed-document"))

    def test_the_crowd_importance_is_the_claim_index_bottom_tier(self):
        from dalton_core.claim_index_authority import IMPORTANCE_TIERS

        self.assertEqual(CROWD_IMPORTANCE, IMPORTANCE_TIERS[-1])

    def test_the_index_sorts_the_crowd_last_without_any_entry_being_added(self):
        """The bottom is where an unlisted source already lands.

        `claim_index_tagging` reaches importance through the discovery spec and
        then the connector's source type. The crowd is absent from both tables
        on purpose: absent means `other`, and `other` is the bottom. Adding an
        entry anywhere is what would raise it, so the tables are asserted empty
        of these rather than asserted to contain something.
        """

        from dalton_core.claim_index_tagging import (
            SOURCE_TYPE_IMPORTANCE,
            SPEC_IMPORTANCE,
        )

        for spec_ref in CROWD_SPEC_REFS:
            self.assertNotIn(spec_ref, SPEC_IMPORTANCE)
        for source_type in ("social_search", "social_enumeration"):
            self.assertNotIn(source_type, SOURCE_TYPE_IMPORTANCE)
            self.assertEqual(
                SOURCE_TYPE_IMPORTANCE.get(source_type, "other"), CROWD_IMPORTANCE)

    def test_a_crowd_statement_says_what_it_is(self):
        qualified = crowd_qualify("sentiment turned negative in March")
        self.assertTrue(qualified.startswith("sentiment turned negative in March,"))
        self.assertIn("anonymous", qualified)


class BodyLockTests(unittest.TestCase):
    def test_placeholder_prose_is_recognised_and_real_prose_is_not(self):
        self.assertTrue(body_locked("Lorem ipsum dolor sit amet, and so on"))
        self.assertTrue(body_locked("  lorem ipsum dolor sit amet  "))
        self.assertFalse(body_locked("great team, terrible hours"))
        self.assertFalse(body_locked(None))

    def test_the_blind_identity_names_one_operation(self):
        identity = employee_reviews_identity()
        self.assertEqual(identity["allowed_operations"], [BLIND_OPERATION])
        self.assertFalse(identity["requires_credential_slot"])
