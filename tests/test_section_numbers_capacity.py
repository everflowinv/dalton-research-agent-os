"""Finite citation capacity; unchanged financial checks and paid replay identity."""
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core.mission_deliverable import (
    MAX_SECTION_NUMBERS, MissionDeliverableValidationError, MissionDeliverableConflict,
    pack_passage_resolver, validate_section,
)
from dalton_core.mission_investment_memo_lane import company_signature
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from tests import test_investment_memo_lane as lane_fixture
from tests import test_investment_memo_timeout_recovery as replay_fixture


def section(count, company="company:one"):
    entries = [{"text": f"${1000 + i}", "number_ref": f"pack_number:{company}:{i}"}
               for i in range(count)]
    return {"title": company + " revenue evidence", "body": "；".join(n["text"] for n in entries),
            "claim_refs": [], "numbers": entries, "gaps": []}


def validate(value):
    sources = {n["number_ref"]: n["text"] for n in value["numbers"] if n.get("number_ref")}
    return validate_section(value, live_claim_refs=set(), resolve_number=sources.get, strict=True)


class CitationCapacityTests(unittest.TestCase):
    def test_old_and_expanded_capacity_preserve_every_bound_number_for_two_companies(self):
        for company in ("company:first", "company:second"):
            for count in (60, 61, 82, 128):
                value = section(count, company)
                checked = validate(value)
                self.assertEqual(checked["body"], value["body"])
                self.assertEqual(checked["numbers"], [{**n, "source_text": n["text"], "period": None} for n in value["numbers"]])
                self.assertEqual(len(checked["numbers"]), count)
        self.assertEqual(MAX_SECTION_NUMBERS, 128)

    def test_129_entries_and_open_shape_still_refuse(self):
        with self.assertRaisesRegex(MissionDeliverableValidationError, "at most 128"):
            validate(section(129))
        value = section(82)
        value["numbers"] = {"source": "not a list"}
        with self.assertRaisesRegex(MissionDeliverableValidationError, "at most 128"):
            validate_section(value, strict=True)

    def test_missing_source_and_wrong_value_still_refuse_above_old_capacity(self):
        value = section(82)
        sources = {n["number_ref"]: n["text"] for n in value["numbers"]}
        sources.pop(value["numbers"][-1]["number_ref"])
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(value, resolve_number=sources.get, live_claim_refs=set(), strict=True)
        sources[value["numbers"][-1]["number_ref"]] = "$9000"
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(value, resolve_number=sources.get, live_claim_refs=set(), strict=True)

    def test_wrong_arithmetic_and_body_limit_still_refuse(self):
        value = section(82)
        value["numbers"][-1] = {"text": "$11", "derivation": {"expression": "3*4", "result": "11"}}
        with self.assertRaises(MissionDeliverableConflict):
            validate(value)
        value = section(82)
        value["body"] = "x" * 6001
        with self.assertRaisesRegex(MissionDeliverableValidationError, "6000"):
            validate(value)

    def test_passage_hash_and_wrong_quoted_value_still_refuse(self):
        value = section(82)
        original = "$1081"
        ref = "doc-passage:alphaengine-doc:999999:0-5:" + hashlib.sha256(original.encode()).hexdigest()[:12]
        value["numbers"][-1] = {"text": "$9999", "passage_ref": ref}
        sources = {n["number_ref"]: n["text"] for n in value["numbers"] if n.get("number_ref")}
        for raw in (original, "$9999"):
            with self.subTest(raw=raw), self.assertRaises(MissionDeliverableConflict):
                validate_section(value, live_claim_refs=set(), resolve_number=sources.get,
                                 resolve_passage=pack_passage_resolver(None, {ref: raw}), strict=True)


class PaidReplayIdentityTests(unittest.TestCase):
    def test_validation_epoch_wakes_lane_but_reuses_success_and_failed_repair_without_new_fees(self):
        case = replay_fixture.MemoTimeoutRecoveryTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        frozen = lane_fixture.MemoFairnessTests.frozen("company:first", "a" * 64)
        launcher = SimpleNamespace(model_config_path=case.root / "none.json",
                                   verifier_model_config_path=case.root / "none-verifier.json")
        for company in ("company:first", "company:second"):
            kwargs = case.kw(2, company)
            case.adapter.script = {}
            paid = case.model.call(**kwargs)
            repair = {**kwargs, "request_id": kwargs["request_id"] + "-semantic-repair",
                      "prompt": kwargs["prompt"] + "\nRepair the previous group"}
            case.adapter.script = {"profile:gpt-6-astra": {"code": "TIMEOUT", "message": "host completion exceeded request timeout"}}
            with self.assertRaises(CockpitModelError):
                case.model.call(**repair)
            fee_rows = case.fee_rows()
            calls = len(case.adapter.served)
            with Scheduler(case.model.scheduler_db) as scheduler:
                work_rows = [tuple(r) for r in scheduler.connection.execute(
                    "SELECT work_order_id,work_order_hash,work_order_json FROM scheduler_work_orders ORDER BY rowid")]
            with patch("dalton_core.mission_deliverable.SECTION_NUMBERS_VALIDATION_CONTRACT", "section-numbers-capacity:60:legacy"):
                old_signature = company_signature(frozen, launcher)
            self.assertNotEqual(old_signature, company_signature(frozen, launcher))
            case.adapter.script = {}
            free = case.model.call(**kwargs)
            self.assertTrue(free["replayed"])
            self.assertEqual(free["cost_micros"], 0)
            self.assertEqual(free["work_order_ref"], paid["work_order_ref"])
            self.assertEqual(free["result_envelope_ref"], paid["result_envelope_ref"])
            with self.assertRaises(CockpitModelError):
                case.model.call(**repair)
            self.assertEqual(len(case.adapter.served), calls)
            self.assertEqual(case.fee_rows(), fee_rows)
            with Scheduler(case.model.scheduler_db) as scheduler:
                self.assertEqual([tuple(r) for r in scheduler.connection.execute(
                    "SELECT work_order_id,work_order_hash,work_order_json FROM scheduler_work_orders ORDER BY rowid")], work_rows)
