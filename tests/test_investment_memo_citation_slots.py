"""Batch t: a memo citation in the wrong slot is named, explained and repaired once.

Live 2026-10-07 (ACN, CTSH, MSCI, IBM): every draft cited refs that *were* in the
frozen material -- the valuation snapshot, the approved gate, event judgements,
forecast cells -- in ``claim_refs`` / ``numbers[].claim_version_ref``, where
MissionDeliverable admits only Claims.  The prompt had offered one flat
"Allowed refs" list, and the refusal said "outside the frozen input".
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from dalton_core.investment_memo_cli import run_memo
from dalton_core.investment_memo_draft import (
    GROUPS, InvestmentMemoCitationError, build_group_prompt, citation_index, parse_group_output,
)
from dalton_core.investment_memo_material import bound_material, compact_text, ROW_CAPS
from dalton_core.store import content_hash
from tests.test_investment_memo_draft import QUESTIONS, TITLES, FakeModel, group_payload

SNAPSHOT = "valuation-snapshot-version:22edfd5a0d8d4764c993047d8abf0da1"
CLAIM = "claim-version:" + "1" * 64
CELL = "result:revenue@2027-05-31:estimate"
MODEL = "forecast-model-version:ff98:7"
METRIC = f"valuation-metric:{SNAPSHOT}:trailing_pe"

MATERIAL = [
    {"ref": CLAIM, "hash": "c" * 64, "kind": "claim", "text": "{}"},
    {"ref": CELL, "hash": "d" * 64, "kind": "forecast_cell", "version_ref": MODEL, "text": "{}"},
    {"ref": METRIC, "hash": "e" * 64, "kind": "valuation_metric", "version_ref": SNAPSHOT,
     "text": "{}"},
    {"ref": SNAPSHOT, "hash": "f" * 64, "kind": "valuation_snapshot_versions", "text": "{}"},
    {"ref": "deep-insight-gate-version:acn:2", "hash": "a" * 64,
     "kind": "deep_insight_gate_versions", "text": "{}"},
]
ALLOWED = {row["ref"] for row in MATERIAL}
Q1 = [{"question_ref": "memo_q01", "question": QUESTIONS[0]}]


def payload():
    value = group_payload(TITLES[:4], Q1)
    for section in value["sections"]:
        section["claim_refs"] = [CLAIM]
    value["key_questions"][0]["refs"] = [CLAIM]
    return value


class CitationSlotTests(unittest.TestCase):
    def test_acn_shape_names_the_misplaced_snapshot_ref(self):
        value = payload()
        value["sections"][0]["claim_refs"] = [SNAPSHOT]
        with self.assertRaises(InvestmentMemoCitationError) as caught:
            parse_group_output(value, section_titles=TITLES[:4], questions=Q1,
                               allowed_refs=ALLOWED, material=MATERIAL)
        self.assertIn(SNAPSHOT, str(caught.exception))
        self.assertIn("valuation_snapshot_versions", str(caught.exception))
        self.assertNotIn("not in the frozen material", str(caught.exception))
        self.assertEqual(caught.exception.refs, [SNAPSHOT])

    def test_hallucinated_ref_is_named_as_not_frozen(self):
        value = payload()
        value["key_questions"][0]["refs"] = ["claim-version:invented"]
        with self.assertRaises(InvestmentMemoCitationError) as caught:
            parse_group_output(value, section_titles=TITLES[:4], questions=Q1,
                               allowed_refs=ALLOWED, material=MATERIAL)
        self.assertIn("claim-version:invented", str(caught.exception))
        self.assertIn("not in the frozen material", str(caught.exception))

    def test_cell_named_in_claim_slot_becomes_the_frozen_cell(self):
        value = payload()
        value["sections"][1]["numbers"] = [
            {"text": "190亿", "claim_version_ref": CELL, "period": "FY27"},
            {"text": "15.2x", "cell": {"kind": "valuation_metric", "ref": METRIC,
                                       "version_ref": "wrong"}, "period": "now"}]
        parsed = parse_group_output(value, section_titles=TITLES[:4], questions=Q1,
                                    allowed_refs=ALLOWED, material=MATERIAL)
        numbers = parsed["sections"][1]["numbers"]
        self.assertEqual(numbers[0]["cell"], {"kind": "forecast_cell", "ref": CELL,
                                              "version_ref": MODEL})
        self.assertNotIn("claim_version_ref", numbers[0])
        self.assertEqual(numbers[1]["cell"]["version_ref"], SNAPSHOT)

    def test_prompt_separates_claims_cells_and_context(self):
        prompt = build_group_prompt(group="view", section_titles=TITLES[:1], questions=Q1,
                                    material=MATERIAL, company={"ticker": "ACN"})
        lines = {line.split(":", 1)[0]: line for line in prompt.splitlines() if ": " in line}
        self.assertEqual(json.loads(lines["Claim refs"].split(": ", 1)[1]), [CLAIM])
        cells = json.loads(lines["Computed cells"].split(": ", 1)[1])
        self.assertIn({"kind": "forecast_cell", "ref": CELL, "version_ref": MODEL}, cells)
        self.assertIn(SNAPSHOT, json.loads(lines["Context refs"].split(": ", 1)[1]))
        self.assertNotIn("Allowed refs", prompt)

    def _frozen(self):
        mission = {"id": "mission:1", "content_hash": "a" * 64,
                   "autonomy": {"automation_principal": "automation:mission"}}
        playbook = {"id": "playbook:1", "content_hash": "b" * 64,
                    "deliverable_templates": {"investment_memo": TITLES}, "key_questions": QUESTIONS}
        return {"status": "ready", "mission": mission, "playbook": playbook,
                "company": {"company_ref": "company:ACN", "ticker": "ACN"}, "material": MATERIAL,
                "input_bindings": [{"ref": r["ref"], "hash": r["hash"], "kind": r["kind"]}
                                   for r in MATERIAL]}

    def _responses(self, *, first_bad: bool):
        out = []
        for _, sections, questions in GROUPS:
            value = group_payload([TITLES[i] for i in sections],
                                  [{"question_ref": f"memo_q{i+1:02d}", "question": QUESTIONS[i]}
                                   for i in questions])
            for section in value["sections"]:
                section["claim_refs"] = [CLAIM]
            for answer in value["key_questions"]:
                answer["refs"] = [CLAIM, SNAPSHOT]
            out.append(value)
        if first_bad:
            bad = json.loads(json.dumps(out[0]))
            bad["sections"][0]["claim_refs"] = [SNAPSHOT]
            out.insert(0, bad)
        return out

    def test_without_repair_budget_the_refusal_names_the_ref(self):
        result = run_memo(store=SimpleNamespace(connection=None), frozen=self._frozen(),
                          model=FakeModel(self._responses(first_bad=True)), verifier_model=FakeModel([]))
        self.assertEqual(result["memo_status"], "draft_refused")
        self.assertIn(SNAPSHOT, result["reason"])
        self.assertEqual(result["offending_refs"], [SNAPSHOT])

    @patch("dalton_core.investment_memo_cli.MissionDeliverableAuthority")
    def test_one_repair_names_the_ref_and_the_memo_publishes(self, authority_type):
        producer = FakeModel(self._responses(first_bad=True))
        verifier = FakeModel([], "verifier")

        def bound_call(**kwargs):
            contract = json.loads(next(line for line in kwargs["prompt"].splitlines()
                                       if line.startswith('{"finding_codes"')))
            verifier.responses = iter([{**contract, "verdict": "pass", "finding_codes": []}])
            return FakeModel.call(verifier, **kwargs)
        verifier.call = bound_call
        authority_type.return_value.publish.return_value = {
            "status": "fresh", "id": "memo:v1", "content_hash": "f" * 64}
        result = run_memo(store=SimpleNamespace(connection=None), frozen=self._frozen(), model=producer,
                          verifier_model=verifier, max_repairs=1)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(producer.calls), 5)
        self.assertIn(SNAPSHOT, producer.calls[1]["prompt"].split("Offending refs:", 1)[1])
        self.assertNotEqual(producer.calls[0]["request_id"], producer.calls[1]["request_id"])

    def test_only_one_repair_per_run(self):
        responses = self._responses(first_bad=True)
        responses.insert(1, responses[0])  # the redo is wrong again
        producer = FakeModel(responses)
        result = run_memo(store=SimpleNamespace(connection=None), frozen=self._frozen(), model=producer,
                          verifier_model=FakeModel([]), max_repairs=1)
        self.assertEqual(result["memo_status"], "draft_refused")
        self.assertEqual(len(producer.calls), 2)
        self.assertEqual(result["repairs"][0]["refs"], [SNAPSHOT])


class MaterialKeepsCitableAuthorityTests(unittest.TestCase):
    def test_cells_keep_their_version_and_dossier_prose_survives_provenance_bulk(self):
        def row(ref, kind, record, **extra):
            return {"ref": ref, "hash": content_hash(record), "kind": kind,
                    "text": json.dumps(record, ensure_ascii=False), **extra}
        dossier = {"id": "dossier:v58", "unit_provenance": {"x": ["p" * 400] * 600},
                   "sections": [{"aspect": f"a{n}", "slots": [{"sentences": [
                       {"text": f"判断{n}：托管服务带动收入", "refs": ["claim-version:" + "9" * 64] * 5}]}]}
                       for n in range(10)]}
        rows = [row("dossier:v58", "company_dossier_versions", dossier),
                row(CELL, "forecast_cell", {"ref": CELL, "value": "1", "kind": "estimate",
                                            "period": {"end": "2027-05-31", "calendar": "x"}},
                    version_ref=MODEL),
                row(METRIC, "valuation_metric", {"metric": "trailing_pe", "value": "15.2",
                                                 "status": "available"}, version_ref=SNAPSHOT)]
        bounded = {r["ref"]: r for r in bound_material(rows, company_ref="company:ACN")}
        self.assertIn("托管服务带动收入", bounded["dossier:v58"]["text"])
        self.assertNotIn("unit_provenance", bounded["dossier:v58"]["text"])
        self.assertEqual(bounded[CELL]["version_ref"], MODEL)
        self.assertEqual(json.loads(bounded[CELL]["text"])["period"], "2027-05-31")
        self.assertEqual(bounded[METRIC]["version_ref"], SNAPSHOT)
        self.assertEqual(citation_index(list(bounded.values()))["cells"][METRIC]["version_ref"],
                         SNAPSHOT)

    def test_oversized_row_keeps_some_substance_not_only_ids(self):
        record = {"id": "x", "small": {"thesis": "核心判断"}, "huge": ["y" * 50] * 5000}
        text = compact_text(record, 400)
        self.assertIn("核心判断", text)
        self.assertLessEqual(len(text.encode("utf-8")), 400)


class SignatureTests(unittest.TestCase):
    def test_drafting_contract_change_releases_the_held_company(self):
        from dalton_core import mission_investment_memo_lane as lane
        from tests.test_investment_memo_lane import InvestmentMemoLaneTests  # noqa: F401
        frozen = {"company": {"company_ref": "company:ACN"},
                  "mission": {"id": "m", "content_hash": "a"}, "playbook": {"id": "p", "content_hash": "b"},
                  "input_bindings": []}

        class Launcher:
            model_config_path = verifier_model_config_path = "/nonexistent"
        before = lane.company_signature(frozen, Launcher())
        with patch("dalton_core.investment_memo_draft.MEMO_PROMPT_CONTRACT_VERSION", "other"):
            after = lane.company_signature(frozen, Launcher())
        self.assertNotEqual(before, after)
        self.assertTrue(after.startswith("company:ACN|"))


if __name__ == "__main__":
    unittest.main()
