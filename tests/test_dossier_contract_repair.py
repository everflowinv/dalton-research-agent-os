"""WP-C1-2 on the dossier lane: one repair per unit, and a readable budget.

Live evidence, from
``~/Library/Application Support/Dalton/state/dalton-core/company-dossier-runs/``:

* ``d9e9d69f231b39097628d750`` (2026-09-16T16:18Z) -- ``all attempted dossier
  units were refused: industry_classification: slots[0] has keys ['refs',
  'sentences', 'slot_id']; ...; demand_drivers: demand_drivers.slots[2] writes
  4 sentences; the cap is 3; segments_and_mix: slots[0] has keys ['sentences',
  'slot_id', 'unknown']``
* ``7a612fcf494e2039b8daba7a`` (09-16T10:48Z) -- ``... slots[0] has keys
  ['counterexample', 'sentences', 'slot_id'] ...; segments_and_mix.slots[0]
  writes 5 sentences; the cap is 3``
* ``060be12c83781d26639258b9`` -- ``kpi_dictionary: the reply has keys
  ['slots']; the contract is ['gaps', 'slots']`` in ``refused[]``
* ``527a8e03055fc29b8f1807f3`` (09-16T07:32Z) and seven more -- every attempted
  unit ``{"reason": "run cost bound reached"}``, ``status: "succeeded"``,
  ``failure_reason: null``: a run that did nothing and said nothing about why.

Not one of these was a judgement about the evidence.
"""

from __future__ import annotations

import json
import re
import unittest

from dalton_core.company_dossier import (
    CLASSIFICATION_UNIT, MAX_GAPS, MAX_SENTENCE_CHARS, SLOT_SENTENCE_CAP,
)
from dalton_core.company_dossier_draft import (
    DossierDraftRefused, draft_contract_fingerprint, parse_unit_output,
    unit_contract, unit_contract_reminder,
)
from dalton_core.draft_contract_repair import (
    Contract, check_contract, stray_tail_fragment,
)
from tests import test_dossier_lane as _lane

ACN = _lane.ACN


def material_rows():
    return [{"tag": "C1", "ref": "claim-version:1", "text": "一行材料",
             "period": None, "importance": "filing", "kind": "claim"}]


STRUCTURE = [{"slot_id": "state", "prompt": "State it"},
             {"slot_id": "why", "prompt": "Say why"}]


class UnitContractTests(unittest.TestCase):
    def contract(self, unit="business_model"):
        return unit_contract(unit, structure=STRUCTURE, material=material_rows())

    def test_a_slot_with_both_sentences_and_unknown_is_named(self):
        found = check_contract(
            {"slots": [{"slot_id": "state", "unknown": "x",
                        "sentences": [{"text": "t", "refs": ["C1"]}]}],
             "gaps": []}, self.contract())
        self.assertEqual([item.rule for item in found], ["shape"])

    def test_a_stray_slot_key_is_named_with_both_allowed_shapes(self):
        for stray in ("refs", "counterexample"):
            found = check_contract(
                {"slots": [{"slot_id": "state", stray: ["C1"],
                            "sentences": [{"text": "t", "refs": ["C1"]}]}],
                 "gaps": []}, self.contract())
            self.assertEqual([item.rule for item in found], ["shape"], stray)
            self.assertIn(stray, found[0].detail)

    def test_a_missing_gaps_key_is_named(self):
        found = check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [{"text": "t", "refs": ["C1"]}]}]},
            self.contract())
        self.assertEqual([item.rule for item in found], ["keys"])
        self.assertIn("gaps", found[0].detail)

    def test_a_fourth_and_a_fifth_sentence_are_both_named(self):
        for count in (4, 5):
            found = check_contract(
                {"slots": [{"slot_id": "state",
                            "sentences": [{"text": "t", "refs": ["C1"]}] * count}],
                 "gaps": []}, self.contract())
            self.assertEqual([item.rule for item in found], ["max_items"])
            self.assertIn(f"{count} items", found[0].detail)
            self.assertIn(f"the cap is {SLOT_SENTENCE_CAP}", found[0].detail)

    def test_a_tag_that_was_not_shown_is_named_with_the_shown_tags(self):
        found = check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [{"text": "t", "refs": ["C7"]}]}],
             "gaps": []}, self.contract())
        self.assertEqual([item.rule for item in found], ["allowed_refs"])
        self.assertIn("C1", found[0].detail)

    def test_the_classification_unit_owns_a_third_key_and_an_enumeration(self):
        contract = self.contract(CLASSIFICATION_UNIT)
        found = check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [{"text": "t", "refs": ["C1"]}]}],
             "gaps": [], "classification": "not_a_classification"},
            contract)
        self.assertEqual([item.rule for item in found], ["enum"])
        self.assertEqual(
            check_contract({"slots": [], "gaps": []}, contract)[0].rule, "keys")

    def test_a_reply_that_holds_the_contract_has_nothing_to_say(self):
        self.assertEqual(check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [{"text": "t", "refs": ["C1"]}]},
                       {"slot_id": "why", "unknown": "材料没有回答"}],
             "gaps": []}, self.contract()), [])

    def test_the_reminder_states_every_bound_the_rules_use(self):
        reminder = unit_contract_reminder("business_model", structure=STRUCTURE)
        self.assertIn("'state', 'why'", reminder.replace('"', "'"))
        self.assertIn(str(SLOT_SENTENCE_CAP), reminder)
        self.assertIn(str(MAX_SENTENCE_CHARS), reminder)
        self.assertIn(str(MAX_GAPS), reminder)
        self.assertIn("Never both", reminder)


# EPAM's demand_drivers gap, versions 7-9 of company:sec-cik:0001352010,
# carried the drafter's abandoned next sentence into the published file.
EPAM_GAP = ("缺少托管服务合同占比、年度合同金额、平均合同期限和续约率数据，"
            "无法评估其平抑收入波动的能力。中")


class StrayTailTests(unittest.TestCase):
    def test_the_live_epam_gap_is_caught(self):
        self.assertEqual(stray_tail_fragment(EPAM_GAP), "中")

    def test_one_or_two_characters_after_final_punctuation_are_a_fragment(self):
        for text, fragment in (
                ("能力。中", "中"), ("能力！中国", "中国"), ("能力？中 ", "中"),
                ("能力。 中", "中"), ("他说“到此为止。”中", "中"),
                ("能力）。中", "中"), ("能力.中", "中")):
            self.assertEqual(stray_tail_fragment(text), fragment, text)

    def test_what_is_not_this_defect_is_left_alone(self):
        for text in (
                "无法评估其平抑收入波动的能力。",  # clean
                "能力。中国市场",                   # a tail of three is a sentence
                "收入下降；待查",                   # ； is not sentence-final
                "收入下降……待查",                   # nor is an ellipsis
                "来自U.S.市场",                     # an abbreviation, not a stop
                "升级到v2.中",                      # a version string
                "The capability. 中",               # half-width stop after Latin
                "能力。3", "能力。Q3", "。中", "中", "", None, 7):
            self.assertIsNone(stray_tail_fragment(text), repr(text))

    def test_the_unit_contract_names_every_path_that_carries_one(self):
        contract = unit_contract("business_model", structure=STRUCTURE,
                                 material=material_rows())
        found = check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [{"text": "一句话。中", "refs": ["C1"]}]},
                       {"slot_id": "why", "unknown": "材料没有回答。中国"}],
             "gaps": ["干净的缺口。", EPAM_GAP]}, contract)
        self.assertEqual(
            [(item.path, item.rule) for item in found],
            [("slots[0].sentences[0].text", "clean_tail"),
             ("slots[1].unknown", "clean_tail"),
             ("gaps[1]", "clean_tail")])
        self.assertIn("'中'", found[-1].detail)

    def test_a_contract_that_does_not_ask_keeps_its_fingerprint(self):
        # Other lanes share the evaluator; their contracts must not move.
        from dalton_core.store import content_hash

        plain = Contract(name="x", max_chars={"a": 30})
        self.assertEqual(plain.fingerprint(), content_hash({
            "name": "x", "keys": {}, "shapes": {}, "max_items": {},
            "min_items": {}, "max_chars": {"a": 30}, "enums": {},
            "allowed_ref_paths": [], "nonempty": [],
        }))
        self.assertNotEqual(
            plain.fingerprint(),
            Contract(name="x", max_chars={"a": 30}, clean_tails=("a",)).fingerprint())
        self.assertEqual(check_contract({"a": "能力。中"}, plain), [])

    def test_the_parser_refuses_it_so_the_repair_path_sees_it(self):
        reply = json.dumps({
            "slots": [{"slot_id": "state",
                       "sentences": [{"text": "一句话。", "refs": ["C1"]}]},
                      {"slot_id": "why", "unknown": "材料没有回答"}],
            "gaps": [EPAM_GAP]}, ensure_ascii=False)
        with self.assertRaises(DossierDraftRefused) as caught:
            parse_unit_output(reply, unit="business_model", structure=STRUCTURE,
                              material=material_rows())
        self.assertIn("gaps[0]", str(caught.exception))
        clean = reply.replace("能力。中", "能力。")
        parsed = parse_unit_output(clean, unit="business_model",
                                   structure=STRUCTURE, material=material_rows())
        self.assertEqual(parsed["gaps"][-1][-3:], "能力。")

    def test_an_all_unknown_reply_is_checked_too(self):
        reply = json.dumps({
            "slots": [{"slot_id": "state", "unknown": "材料没有回答。中"},
                      {"slot_id": "why", "unknown": "材料没有回答"}],
            "gaps": []}, ensure_ascii=False)
        with self.assertRaises(DossierDraftRefused):
            parse_unit_output(reply, unit="business_model", structure=STRUCTURE,
                              material=material_rows())

    def test_the_rule_is_part_of_the_reminder_and_the_fingerprint(self):
        self.assertIn("final punctuation",
                      unit_contract_reminder("business_model", structure=STRUCTURE))
        self.assertIsInstance(draft_contract_fingerprint(), str)


class RepairingModel(_lane.FakeModel):
    """Breaks the shape once per unit, then answers correctly."""

    def __init__(self, breakage, **kwargs):
        super().__init__(**kwargs)
        self.breakage = breakage
        self.broken: set[str] = set()
        self.repair_prompts: list[str] = []

    def call(self, *, purpose, request_id, prompt, mission):
        if prompt.startswith("Your previous reply broke"):
            self.repair_prompts.append(prompt)
            self.prompts.append(prompt)
            # Answer the repair with the shape the original prompt asked for:
            # re-derive it from the *first* prompt for this unit.
            return super().call(purpose=purpose, request_id=request_id,
                                prompt=self.pending, mission=mission)
        if prompt.startswith("You are an independent verifier"):
            return super().call(purpose=purpose, request_id=request_id,
                                prompt=prompt, mission=mission)
        unit = re.search(r"^Part: (\S+)", prompt, flags=re.MULTILINE)
        name = unit.group(1) if unit else "?"
        if name not in self.broken:
            self.broken.add(name)
            self.pending = prompt
            self.prompts.append(prompt)
            return self._envelope(self.breakage(prompt))
        return super().call(purpose=purpose, request_id=request_id,
                            prompt=prompt, mission=mission)


def both_keys(prompt):
    slots = re.findall(r"^  (\S+)\t", prompt, flags=re.MULTILINE)
    return json.dumps({
        "slots": [{"slot_id": slots[0], "unknown": "x",
                   "sentences": [{"text": "一句话。", "refs": ["C1"]}]}],
        "gaps": [],
    }, ensure_ascii=False)


def stray_gap(prompt):
    slots = re.findall(r"^  (\S+)\t", prompt, flags=re.MULTILINE)
    tag = "C1" if "\nC1\t" in prompt else "N1"
    payload = {
        "slots": [{"slot_id": slots[0],
                   "sentences": [{"text": "这一节的判断由所引材料支撑。", "refs": [tag]}]}]
        + [{"slot_id": slot, "unknown": "材料没有回答这一点"} for slot in slots[1:]],
        "gaps": [EPAM_GAP],
    }
    if "Part: industry_classification" in prompt:
        payload["classification"] = "contract_compounder"
    return json.dumps(payload, ensure_ascii=False)


class DossierRepairTests(unittest.TestCase):
    def setUp(self):
        self.harness = _lane.Harness()
        self.addCleanup(self.harness.close)

    def test_a_broken_shape_is_repaired_and_the_unit_is_published(self):
        summary = self.harness.run(
            model_factory=lambda: RepairingModel(both_keys), max_units=2)
        self.assertEqual(summary["status"], "succeeded")
        self.assertIn(summary["dossier_status"],
                      {"published", "partial_published"})
        self.assertTrue(summary["units_drafted"])
        repaired = [row for row in summary["contract_repair"]
                    if row["status"] == "repaired"]
        self.assertTrue(repaired)
        self.assertEqual(repaired[0]["repair_attempts"], 1)
        self.assertTrue(repaired[0]["violations"])

    def test_a_stray_tail_is_repaired_not_published(self):
        model = None

        def factory():
            nonlocal model
            model = RepairingModel(stray_gap)
            return model

        summary = self.harness.run(model_factory=factory, max_units=2)
        self.assertEqual(summary["status"], "succeeded")
        repaired = [row for row in summary["contract_repair"]
                    if row["status"] == "repaired"]
        self.assertTrue(repaired)
        rules = {item["rule"] for row in repaired for item in row["violations"]}
        self.assertIn("clean_tail", rules)
        self.assertTrue(model.repair_prompts)
        self.assertIn("stray fragment", model.repair_prompts[0])
        self.assertNotIn("能力。中", json.dumps(
            summary, ensure_ascii=False))

    def test_a_reply_that_holds_the_contract_buys_no_repair(self):
        summary = self.harness.run(
            model_factory=lambda: _lane.FakeModel(), max_units=1)
        self.assertTrue(summary["contract_repair"])
        for row in summary["contract_repair"]:
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["repair_attempts"], 0)

    def test_the_budget_says_which_units_it_gave_up_and_what_is_left(self):
        # ``run cost bound reached`` eight times on 2026-09-16, in a summary
        # whose ``failure_reason`` was null.
        self.harness.model_config.write_text(
            json.dumps({"run_budget": {"max_cost_usd": 0.000001}}), encoding="utf-8")
        summary = self.harness.run(max_units=12)
        budget = summary["budget"]
        self.assertEqual(budget["spent_micros"], 0)
        self.assertTrue(budget["units_skipped_for_cost"])
        self.assertIn("成本上限", budget["reason"])
        self.assertEqual(
            sorted(budget["units_skipped_for_cost"]),
            sorted(row["unit"] for row in summary["refused"]
                   if row["reason"] == "run cost bound reached"))

    def test_a_run_that_pays_for_everything_reports_no_skips(self):
        summary = self.harness.run(max_units=2)
        self.assertEqual(summary["budget"]["units_skipped_for_cost"], [])
        self.assertIsNone(summary["budget"]["reason"])
        self.assertGreater(summary["budget"]["spent_micros"], 0)


class ContractRepairProvenanceTests(unittest.TestCase):
    """The formal replay of a unit repaired for a broken shape.

    Live EPAM (``company:sec-cik:0001352010``) from 2026-09-18, last
    ``dfc89e7d18b54eb2692dd867`` on 09-25: ``catalyst_calendar`` wrote four
    sentences in one slot.  The drafting loop addressed its repair on the
    deterministic finding *and* the parser's own message (two lines, with
    different words); the replay rebuilt the list without the parser's line,
    so the repair's content-addressed id never matched and every run -- each a
    free replay of the cached reply -- refused with "producer repair identity
    drifted".
    """

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from dalton_core.company_dossier_draft import (
            build_unit_prompt, build_verifier_prompt, citable_context, draft_hash,
            verifier_prompt_contract_fingerprint,
        )
        from dalton_core.draft_contract_repair import (
            build_repair_prompt, repair_request_id, violations_of,
        )
        from dalton_core.store import canonical_json, content_hash
        from tests.test_company_dossier_draft import (
            STRUCTURE as DRAFT_STRUCTURE, material, one_sentence, reply,
        )

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scheduler_path = Path(self.temp.name) / "scheduler.sqlite"
        self.router_path = Path(self.temp.name) / "router.sqlite"
        scheduler = sqlite3.connect(self.scheduler_path)
        scheduler.executescript(
            "CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,"
            "work_order_json TEXT,work_order_hash TEXT);"
            "CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT "
            "PRIMARY KEY,work_order_id TEXT,attempt_number INTEGER,"
            "result_envelope_json TEXT,result_envelope_hash TEXT,outcome TEXT,"
            "content_hash TEXT,created_at TEXT);")
        router = sqlite3.connect(self.router_path)
        router.execute(
            "CREATE TABLE model_route_decisions(decision_id TEXT PRIMARY KEY,"
            "work_order_id TEXT,work_order_hash TEXT,outcome TEXT,"
            "decision_json TEXT,decision_hash TEXT)")

        unit = "demand_drivers"
        rows = list(material())
        company = {"company_ref": "company:epam", "ticker": "EPAM"}
        parse_input = {"structure": list(DRAFT_STRUCTURE), "material": rows,
                       "prior_body": "", "profile": None, "profile_table": "",
                       "market_view_available": False, "classification": None}
        over_cap = {"slot_id": "causal_chain:0", "sentences": [
            {"text": f"第{index}句由所引材料支撑。", "refs": ["C1"]}
            for index in range(1, SLOT_SENTENCE_CAP + 2)]}
        parent_text = reply([over_cap, {"slot_id": "causal_chain:1", "unknown": "x"}])
        repaired_text = reply([one_sentence("causal_chain:0", ["C1"]),
                               {"slot_id": "causal_chain:1", "unknown": "x"}])
        with self.assertRaises(DossierDraftRefused) as caught:
            parse_unit_output(parent_text, unit=unit, structure=DRAFT_STRUCTURE,
                              material=rows)
        block = parse_unit_output(repaired_text, unit=unit,
                                  structure=DRAFT_STRUCTURE, material=rows)
        contract = unit_contract(unit, structure=DRAFT_STRUCTURE, material=rows)
        # Exactly what ``run_with_contract_repair`` addresses the repair on.
        self.violations = violations_of(parent_text, contract,
                                        parse_error=caught.exception)
        draft_prompt = build_unit_prompt(unit=unit, structure=DRAFT_STRUCTURE,
                                         material=rows, company=company)
        producer_input = {
            "unit": unit, "company": company,
            "prompt_sha": content_hash({"prompt": draft_prompt}),
            "mission": {"ref": "mission:v14", "hash": "c" * 64},
            "parse_input": parse_input,
        }
        parent_request = content_hash({
            "unit": unit, "company": company["company_ref"],
            "prompt_sha": content_hash(draft_prompt)})[:32]
        repair_prompt = build_repair_prompt(
            original_prompt=draft_prompt, reply_text=parent_text,
            violations=self.violations,
            contract_reminder=unit_contract_reminder(unit, structure=DRAFT_STRUCTURE),
            context=citable_context(rows))
        repair_request = repair_request_id(
            parent_request, contract_name=contract.name, violations=self.violations)
        blocks = {unit: block}
        digest = draft_hash(blocks)
        verifier_prompt = build_verifier_prompt(blocks, company=company)
        verifier_request = (f"verify-{digest[:24]}-"
                            f"{verifier_prompt_contract_fingerprint()[:16]}")

        def record_call(name, *, prompt, text, request_id, purpose,
                        producer_routes=()):
            work_ref, route_ref = f"work:{name}", f"route-decision:{name}"
            result_ref, invocation_ref = f"result:{name}", f"invocation:{name}"
            work_request = request_id
            if producer_routes:
                work_request += ":producer:" + content_hash(
                    sorted(set(producer_routes)))[:16]
            work = {"id": work_ref, "question": prompt, "metadata": {
                "purpose": purpose, "request_id": work_request,
                "mission_version_ref": producer_input["mission"]["ref"],
                "mission_version_hash": producer_input["mission"]["hash"],
                "producer_route_decision_refs": list(producer_routes)}}
            work_hash = content_hash(work)
            envelope = {"id": result_ref, "work_order_ref": work_ref,
                        "invocation_ref": invocation_ref, "status": "succeeded",
                        "outputs": {"text": text},
                        "metadata": {"route_decision_ref": route_ref}}
            decision = {"id": route_ref}
            decision["content_hash"] = content_hash(decision)
            created = "2026-09-25T12:33:29+00:00"
            envelope_hash = content_hash(envelope)
            receipt = content_hash({
                "result_envelope_id": result_ref, "work_order_id": work_ref,
                "attempt_number": 1, "result_envelope_hash": envelope_hash,
                "outcome": "succeeded", "created_at": created})
            scheduler.execute("INSERT INTO scheduler_work_orders VALUES(?,?,?)",
                              (work_ref, canonical_json(work), work_hash))
            scheduler.execute(
                "INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?,?,?,?,?)",
                (result_ref, work_ref, 1, canonical_json(envelope), envelope_hash,
                 "succeeded", receipt, created))
            router.execute("INSERT INTO model_route_decisions VALUES(?,?,?,?,?,?)",
                           (route_ref, work_ref, work_hash, "selected",
                            canonical_json(decision), decision["content_hash"]))
            return route_ref, {
                "work_order_ref": work_ref, "result_envelope_ref": result_ref,
                "invocation_ref": invocation_ref, "route_decision_ref": route_ref,
                "request_id": request_id, "prompt_hash": content_hash(prompt)}

        parent_route, parent_call = record_call(
            "parent", prompt=draft_prompt, text=parent_text,
            request_id=parent_request, purpose="dossier")
        repair_route, repair_call = record_call(
            "repair", prompt=repair_prompt, text=repaired_text,
            request_id=repair_request, purpose="dossier")
        _, verifier_call = record_call(
            "verifier", prompt=verifier_prompt,
            text='{"verdict":"pass","findings":[]}',
            request_id=verifier_request, purpose="dossier_verifier",
            producer_routes=[parent_route, repair_route])
        scheduler.commit(); router.commit(); scheduler.close(); router.close()
        self.unit, self.blocks = unit, blocks
        self.provenance = {unit: {
            "producer_input": producer_input,
            "producer_prior_version_ref": None,
            "verified_draft_hash": digest,
            "producer": repair_call,
            "producer_repair": parent_call,
            "verifier": verifier_call,
        }}

    def validate(self, *, current=True):
        from dalton_core.company_dossier_cli import validate_formal_unit_provenance

        validate_formal_unit_provenance(
            self.provenance, mission_ref="mission:v14", current_prior_ref=None,
            current_units={self.unit} if current else set(),
            current_blocks=self.blocks if current else None,
            scheduler_db=self.scheduler_path, router_db=self.router_path)

    def test_the_parser_and_the_contract_say_it_in_different_words(self):
        self.assertEqual([item.rule for item in self.violations],
                         ["max_items", "validator"])

    def test_a_repair_addressed_on_the_parser_message_resolves(self):
        self.validate()
        # And the same unit carried forward into a later version.
        self.validate(current=False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
