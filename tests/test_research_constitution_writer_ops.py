"""Research constitution writer ops: human-governed publish and read paths."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    WriterServer,
)


OWNER = "human:coverage-owner"
INDUSTRY_REF = "industry:us-it-services"
INDUSTRY = "industry:us-it-services"
GOVERNANCE_TOKEN = "governance-test-token"
CORE_TOKEN = "core-test-token"
WORKER_TOKEN = "worker-test-token"


def method_payload() -> dict:
    return {
        "question_admission": ["A question must change a Thesis or driver view."],
        "causal_chain": ["Bookings lead revenue by two to four quarters."],
        "source_standards": {
            "hierarchy": ["SEC filings are the primary numeric authority."],
            "conflict_adjudication": ["GAAP filing numbers override transcripts."],
            "minimum_independent_sources": 1,
        },
        "falsification": {
            "required_falsifier_searches": ["Search for bookings contraction."],
            "alternative_explanations": ["FX translation masquerading as inflection."],
        },
        "materiality": ["State magnitude in revenue-growth points."],
        "lifecycle": {
            "continue_when": ["The brief changes a Thesis or driver view."],
            "refresh_when": ["Evidence exceeds its freshness window."],
            "stop_when": ["All falsifiers resolved."],
            "escalate_when": ["A major Thesis change is proposed."],
        },
        "output_rubric": {
            "criteria": ["State what changed and its impact."],
            "good_samples": [],
            "bad_samples": [],
        },
    }


class ConstitutionWriterHarness:
    def __init__(self, root: Path):
        self.socket = str(root / "writer.sock")
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal(
                "coverage-governance", GOVERNANCE_TOKEN, HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref=OWNER,
            ),
            "worker": Principal(
                "worker", WORKER_TOKEN,
                frozenset({"stage_change", "propose_model_input"}),
            ),
            "automation-governance": Principal(
                "automation-governance", "automation-token", HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref="system:planner",
            ),
        }
        self.server = WriterServer(root / "core.sqlite", self.socket, principals)
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.governance = WriterClient(self.socket, GOVERNANCE_TOKEN)
        self.core = WriterClient(self.socket, CORE_TOKEN)
        self.worker = WriterClient(self.socket, WORKER_TOKEN)

    def bootstrap(self) -> dict:
        mandate = self.governance.call("create_mandate", {
            "mandate_ref": "mandate:us-it-services", "actor_ref": OWNER,
            "objective": "Establish US IT Services coverage.",
            "scope_refs": [INDUSTRY], "constraints": {}, "success_criteria": {},
            "effective_from": "2026-08-23T00:00:00+00:00", "effective_until": None,
        })
        pack = self.governance.call("register_driver_pack", {
            "driver_pack_ref": "driver-pack:us-it-services", "actor_ref": OWNER,
            "industry_ref": INDUSTRY, "title": "US IT Services Driver Pack",
            "drivers": [{
                "driver_ref": "driver:d", "label": "D", "mechanism": "m",
                "metric_refs": ["metric:x"],
            }],
            "metric_specs": [{
                "metric_ref": "metric:x", "label": "X", "definition": "d", "unit": "USD",
                "periodicity": "quarterly", "preferred_source_refs": ["source:sec-edgar"],
                "verification_kind": "numeric", "caveats": [],
            }],
            "thesis_templates": [{
                "template_ref": "template:x", "statement": "s", "mechanism": "m",
                "driver_refs": ["driver:d"], "implied_expectation": "e",
                "falsifier_refs": ["falsifier:x"],
            }],
            "version_id": "driver-pack-version:us-it-services:1", "prior_version_ref": None,
            "idempotency_key": "driver-pack:us-it-services:1",
        })
        policy = self.core.call("active_policy", {})
        return {"mandate": mandate, "pack": pack, "policy": policy}

    def params(self, state: dict) -> dict:
        return {
            "constitution_ref": "constitution:us-it-services",
            "industry_ref": INDUSTRY,
            "title": "US IT Services Research Constitution v1",
            "bindings": {
                "mandate_version": {
                    "ref": state["mandate"]["id"],
                    "hash": state["mandate"]["content_hash"],
                },
                "driver_pack_version": {
                    "ref": state["pack"]["id"],
                    "hash": state["pack"]["content_hash"],
                },
                "governance_policy_version": {
                    "ref": state["policy"]["policy_version_id"],
                    "hash": state["policy"]["content_hash"],
                },
                "doctrine_pack_version": None,
                "weekly_brief_plan": None,
            },
            "method": method_payload(),
            "actor_ref": OWNER,
            "version_id": "constitution-version:us-it-services:1",
            "prior_version_ref": None,
            "idempotency_key": "constitution:us-it-services:1",
        }

    def close(self) -> None:
        self.server.stop()
        self.thread.join(timeout=10)


class ResearchConstitutionWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.h = ConstitutionWriterHarness(Path(root.name))
        self.addCleanup(self.h.close)
        self.state = self.h.bootstrap()

    def test_governance_publishes_reads_and_replays(self) -> None:
        published = self.h.governance.call(
            "publish_research_constitution", self.h.params(self.state)
        )
        self.assertEqual("fresh", published["status"])
        replay = self.h.governance.call(
            "publish_research_constitution", self.h.params(self.state)
        )
        self.assertEqual("duplicate", replay["status"])
        active = self.h.governance.call("get_active_research_constitution", {
            "constitution_ref": "constitution:us-it-services",
        })
        self.assertEqual(published["id"], active["id"])
        reread = self.h.governance.call("get_research_constitution", {
            "version_id": published["id"],
        })
        self.assertEqual(published["content_hash"], reread["content_hash"])
        report = self.h.governance.call("research_constitution_report", {})
        self.assertEqual(1, report["constitution_count"])
        self.assertEqual(1, report["version_count"])

    def test_only_human_principals_may_publish(self) -> None:
        params = self.h.params(self.state)
        with self.assertRaises(RemoteAuthorizationError):
            self.h.worker.call("publish_research_constitution", params)
        params_with_system_actor = dict(params)
        params_with_system_actor["actor_ref"] = "system:planner"
        with self.assertRaises(RemoteAuthorizationError):
            self.h.core.call("publish_research_constitution", params_with_system_actor)
        automation = WriterClient(self.h.socket, "automation-token")
        with self.assertRaises(RemoteAuthorizationError):
            automation.call("publish_research_constitution", params)
        with self.assertRaises(RemoteAuthorizationError):
            self.h.worker.call("research_constitution_report", {})

    def test_unknown_parameters_are_rejected(self) -> None:
        params = dict(self.h.params(self.state))
        params["surprise"] = True
        with self.assertRaises(Exception):
            self.h.governance.call("publish_research_constitution", params)


class CompanyResearchViewWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.h = ConstitutionWriterHarness(Path(root.name))
        self.addCleanup(self.h.close)

    def test_read_ops_project_views_and_queries(self) -> None:
        view = self.h.governance.call("company_research_view", {
            "company_ref": "company:sec-cik:0001467373",
        })
        self.assertEqual("company_research_view", view["projection_kind"])
        self.assertEqual("insufficient", view["thesis"]["status"])
        self.assertEqual([], view["claims"])
        replay = self.h.governance.call("company_research_view", {
            "company_ref": "company:sec-cik:0001467373",
        })
        self.assertEqual(view["content_hash"], replay["content_hash"])
        query = self.h.governance.call("company_research_query", {
            "company_ref": "company:sec-cik:0001467373",
        })
        self.assertEqual("company_research_query", query["projection_kind"])
        self.assertEqual([], query["claims"])
        with self.assertRaises(RemoteAuthorizationError):
            self.h.worker.call("company_research_view", {
                "company_ref": "company:sec-cik:0001467373",
            })
        with self.assertRaises(Exception):
            self.h.governance.call("company_research_query", {"status": "bogus"})


class BoundedPlannerWriterOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.h = ConstitutionWriterHarness(Path(root.name))
        self.addCleanup(self.h.close)

    def test_question_template_and_loop_admission_require_human(self) -> None:
        mandate = self.h.governance.call("create_mandate", {
            "mandate_ref": "mandate:p8c", "actor_ref": OWNER,
            "objective": "Admit the standing demand question.",
            "scope_refs": [INDUSTRY_REF], "constraints": {}, "success_criteria": {},
            "effective_from": "2026-08-23T00:00:00+00:00", "effective_until": None,
        })
        question = self.h.governance.call("record_backlog_question", {
            "mandate_version_ref": mandate["id"], "company_ref": INDUSTRY_REF,
            "question": "Has US IT services demand bottomed?",
            "answer_criteria": "Same-filing quarterly revenue comparisons across the lane.",
            "source_refs": ["source:sec-edgar"], "actor_ref": OWNER,
            "idempotency_key": "question:p8c:1",
        })
        self.assertIn("question_version_ref", question)
        template = self.h.governance.call("publish_probe_template", {
            "template_ref": "probe-template:sec-revenue-growth:v1",
            "capability_ref": "capability:sec-read-only",
            "operation": "get_company_facts",
            "runtime_profile_ref": "runtime:sec-read-only:0.1",
            "parameter_contract": {
                "allowed_fields": ["source_ref", "locator", "query_terms"],
                "required_fields": ["source_ref", "locator", "query_terms"],
                "constants": {"source_ref": "source:sec-edgar"},
            },
            "output_contract_ref": "schema:bounded-planner-probe-output:0.1",
            "verifier_ref": "verifier:source-level-coverage:0.1",
            "permission_scope": "public_sec_read",
            "declared_side_effects": ["read:public-http"],
            "cost": {"cost_units": 1, "max_attempts": 2, "max_seconds": 120},
            "actor_ref": OWNER, "prior_version_ref": None,
        })
        self.assertEqual("fresh", template["status"])
        loop = self.h.governance.call("create_bounded_planner_loop", {
            "loop_ref": "bounded-loop:us-it-services-demand:v1",
            "question_version_ref": question["question_version_ref"],
            "template_bindings": [{
                "coverage_item_ref": "coverage:revenue-growth",
                "template_version_ref": template["id"],
                "parameters": {
                    "source_ref": "source:sec-edgar",
                    "locator": "company-facts/CIK0001467373",
                    "query_terms": ["Revenues", "10-Q"],
                },
            }],
            "required_coverage_items": ["coverage:revenue-growth"],
            "budget": {"max_rounds": 2, "max_cost_units": 2, "max_seconds": 300},
            "actor_ref": OWNER, "prior_version_ref": None,
        })
        self.assertEqual("fresh", loop["status"])
        reread = self.h.governance.call("bounded_planner_loop", {
            "version_ref": loop["id"],
        })
        self.assertEqual(loop["content_hash"], reread["content_hash"])
        template_reread = self.h.governance.call("bounded_probe_template", {
            "version_ref": template["id"],
        })
        self.assertEqual(template["content_hash"], template_reread["content_hash"])
        with self.assertRaises(RemoteAuthorizationError):
            self.h.worker.call("publish_probe_template", {
                "template_ref": "probe-template:x", "actor_ref": OWNER,
            })
        with self.assertRaises(RemoteAuthorizationError):
            self.h.core.call("create_bounded_planner_loop", {
                "loop_ref": "loop:x", "actor_ref": "system:planner",
            })


if __name__ == "__main__":
    unittest.main()
