"""WP-C2 on the dossier lane: a verdict a model can act on, and a cooldown.

Live evidence, from
``/Volumes/EveSSD/Dalton/legacy-state/dalton-core/company-dossier-runs/``:

* Accenture (``company:sec-cik:0001467373``) -- 50 summaries with
  ``dossier_status: "verification_failed"``, the last of them
  ``d4a6ab2cfacbdde1e11ee2fc`` (2026-09-18T05:13Z), every one carrying the same
  single finding: ``{"unit": "demand_drivers", "code": "unsupported_sentence",
  "detail": "The statement claiming that consulting work is increasingly
  incorporated into large managed services projects is not supported by the
  cited evidence for that point."}``.  ``contract_repair`` on every one of them
  is three rows of ``{"status": "ok", "repair_attempts": 0}``: the shape was
  never the problem, so the one repair mechanism the lane had never fired.  The
  run relaunched roughly every five minutes, at four-plus paid calls each.
* Cognizant (``company:sec-cik:0001058290``) -- 27 summaries with
  ``dossier_status: "constitution_refused"``; ``d86211b5a03b5b83f768e4be``
  (2026-09-17T12:13Z) carries three ``investment_conclusion`` findings, all
  against ``history_of_price_drivers``, which that same run had just drafted.

Two things are tested here.  That a finding now reaches the model that wrote
the sentence, once; and that a company whose repaired draft is still rejected
stops being relaunched for a fixed period **however much the evidence moves**,
because it was the evidence signature moving that made the old hold useless.
"""

from __future__ import annotations

import json
import re
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from dalton_core import mission_dossier_lane as lane_module
from dalton_core.company_dossier_cli import (
    MAX_FINDINGS_REPAIR_ROUNDS,
    findings_repair_targets,
)
from tests import test_dossier_lane as _lane

ACN = _lane.ACN
FINDINGS_HEAD = "An independent check read your previous reply"


class VerifierRejectsOnceModel(_lane.FakeModel):
    """Rejects one unit on the first verdict, then passes.

    The repair answers with the reply it is repairing.  That is deliberate: the
    test is about whether a finding reaches the model with the unit's own reply
    and whether what comes back is re-verified and published, not about a fake
    model's prose.
    """

    def __init__(self, *, repairable=True, **kwargs):
        super().__init__(**kwargs)
        self.repairable = repairable
        self.unit: str | None = None
        self.verdicts = 0
        self.repair_prompts: list[str] = []
        self.last_payload: dict[str, str] = {}

    def call(self, *, purpose, request_id, prompt, mission):
        if prompt.startswith("You are an independent verifier"):
            self.prompts.append(prompt)
            self.verdicts += 1
            # Reject the first unit the draft actually carries, read out of the
            # verifier's own prompt rather than agreed in advance.
            self.unit = re.findall(r"^## (\S+)", prompt, flags=re.MULTILINE)[0]
            rejected = self.verdicts == 1 or not self.repairable
            body = {
                "verdict": "reject" if rejected else "pass",
                "findings": ([{"unit": self.unit, "code": "unsupported_sentence",
                               "detail": "the cited row does not carry this claim"}]
                             if rejected else []),
            }
            return self._envelope(json.dumps(body))
        if prompt.startswith(FINDINGS_HEAD):
            self.repair_prompts.append(prompt)
            self.prompts.append(prompt)
            return self._envelope(self.last_payload.get(self.unit or "", "{}"))
        result = super().call(purpose=purpose, request_id=request_id,
                              prompt=prompt, mission=mission)
        found = re.search(r"^Part: (\S+)", prompt, flags=re.MULTILINE)
        if found is not None:
            self.last_payload[found.group(1)] = result["text"]
        return result


class ConclusionThenCleanModel(_lane.FakeModel):
    """Writes an investment conclusion once, and removes it when told to.

    CTSH, 2026-09-17: three ``investment_conclusion`` findings against a
    section that run had just drafted, and the whole run refused without the
    model ever being shown which words were the problem.
    """

    def __init__(self, *, repairable=True, **kwargs):
        super().__init__(**kwargs)
        self.repairable = repairable
        self.repair_prompts: list[str] = []
        self.clean: dict[str, str] = {}

    def call(self, *, purpose, request_id, prompt, mission):
        if prompt.startswith(FINDINGS_HEAD):
            self.repair_prompts.append(prompt)
            self.prompts.append(prompt)
            if not self.repairable:
                return self._envelope(next(iter(self.dirty.values())))
            return self._envelope(next(iter(self.clean.values())))
        result = super().call(purpose=purpose, request_id=request_id,
                              prompt=prompt, mission=mission)
        found = re.search(r"^Part: (\S+)", prompt, flags=re.MULTILINE)
        if found is None or prompt.startswith("You are an independent verifier"):
            return result
        self.clean[found.group(1)] = result["text"]
        payload = json.loads(result["text"])
        for slot in payload["slots"]:
            if "sentences" in slot:
                slot["sentences"][0]["text"] += "我们给出买入评级。"
        text = json.dumps(payload, ensure_ascii=False)
        self.dirty = getattr(self, "dirty", {})
        self.dirty[found.group(1)] = text
        return self._envelope(text)


class TargetTests(unittest.TestCase):
    def test_only_a_unit_this_run_drafted_can_be_repaired(self):
        # A finding about a section carried forward has no drafting call behind
        # it on this run, so there is nothing to hand back to a model.
        targets = findings_repair_targets(
            [{"unit": "demand_drivers", "code": "unsupported_sentence",
              "detail": "x"},
             {"unit": "business_model", "code": "unsupported_sentence",
              "detail": "carried forward"}],
            {"demand_drivers": {}}, key="unit")
        self.assertEqual(list(targets), ["demand_drivers"])

    def test_the_output_rubric_speaks_of_sections_not_units(self):
        targets = findings_repair_targets(
            [{"code": "investment_conclusion", "criterion_index": 2,
              "section": "history_of_price_drivers", "phrase": "买入"}],
            {"history_of_price_drivers": {}}, key="section")
        self.assertEqual(list(targets), ["history_of_price_drivers"])
        self.assertEqual(targets["history_of_price_drivers"][0]["phrase"], "买入")

    def test_one_round_is_one_round(self):
        self.assertEqual(MAX_FINDINGS_REPAIR_ROUNDS, 1)


class VerificationRepairTests(unittest.TestCase):
    def setUp(self):
        self.harness = _lane.Harness()
        self.addCleanup(self.harness.close)

    def test_a_rejected_unit_is_repaired_once_and_the_file_publishes(self):
        model = VerifierRejectsOnceModel()
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _Shared(model, route="route:verify"),
            max_units=2)
        self.assertEqual(summary["status"], "succeeded")
        self.assertIn(summary["dossier_status"],
                      {"published", "partial_published"})
        self.assertEqual(summary["findings_repair_rounds"], 1)
        self.assertEqual(len(summary["findings_repair"]), 1)
        row = summary["findings_repair"][0]
        self.assertEqual(row["kind"], "verification")
        self.assertEqual(row["status"], "repaired")
        self.assertEqual(row["repair_attempts"], 1)
        self.assertEqual(row["findings"][0]["code"], "unsupported_sentence")
        # One repair call, and the verifier was asked again about the body that
        # is actually published.
        self.assertEqual(len(model.repair_prompts), 1)
        self.assertEqual(model.verdicts, 2)

    def test_the_repair_prompt_carries_the_finding_and_the_reply(self):
        model = VerifierRejectsOnceModel()
        self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _Shared(model, route="route:verify"),
            max_units=2)
        prompt = model.repair_prompts[0]
        self.assertIn("the cited row does not carry this claim", prompt)
        self.assertIn("unsupported_sentence", prompt)
        self.assertIn("YOUR PREVIOUS REPLY:", prompt)
        self.assertIn("CITABLE ROW TAGS", prompt)

    def test_a_repair_that_does_not_help_refuses_once_and_stops(self):
        model = VerifierRejectsOnceModel(repairable=False)
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _Shared(model, route="route:verify"),
            max_units=2)
        self.assertEqual(summary["dossier_status"], "verification_failed")
        self.assertEqual(summary["findings_repair_rounds"], 1)
        self.assertEqual(len(model.repair_prompts), 1)
        self.assertIn("after one repair call", summary["failure_reason"])
        # Two verdicts and one repair: the second refusal is final for this run.
        self.assertEqual(model.verdicts, 2)

    def test_a_verifier_that_never_ran_buys_no_repair(self):
        class Unverified(_lane.FakeModel):
            def call(self, *, purpose, request_id, prompt, mission):
                if prompt.startswith("You are an independent verifier"):
                    return self._envelope("not json at all")
                return super().call(purpose=purpose, request_id=request_id,
                                    prompt=prompt, mission=mission)

        summary = self.harness.run(
            verifier_model_factory=lambda: Unverified(route="route:verify"),
            max_units=1)
        self.assertEqual(summary["dossier_status"], "verification_failed")
        self.assertEqual(summary["findings_repair"], [])
        self.assertEqual(summary["findings_repair_rounds"], 0)

    def test_a_run_with_no_room_left_refuses_the_repair_unmade(self):
        self.harness.model_config.write_text(
            json.dumps({"run_budget": {"max_cost_usd": 0.0000035}}),
            encoding="utf-8")
        model = VerifierRejectsOnceModel(repairable=False)
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _Shared(model, route="route:verify"),
            max_units=1)
        rows = [row for row in summary["findings_repair"]
                if row["status"] == "budget_refused"]
        if rows:
            self.assertIn("micros left", rows[0]["reason"])
            self.assertEqual(model.repair_prompts, [])


class OutputRubricRepairTests(unittest.TestCase):
    def setUp(self):
        self.harness = _lane.Harness()
        # Bind the fixture constitution's one criterion to the check the live
        # refusals were against: a dossier is a file, not a call.
        policy = json.loads(self.harness.policy_path.read_text(encoding="utf-8"))
        policy["output_rubric_bindings"][0]["check"] = "no_investment_conclusion"
        self.harness.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        self.addCleanup(self.harness.close)

    def test_a_conclusion_the_constitution_forbids_is_removed_once(self):
        model = ConclusionThenCleanModel()
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _lane.FakeModel(route="route:verify"),
            max_units=1)
        self.assertEqual(summary["findings_repair_rounds"], 1)
        row = summary["findings_repair"][0]
        self.assertEqual(row["kind"], "output_rubric")
        self.assertEqual(row["status"], "repaired")
        self.assertEqual(row["findings"][0]["code"], "investment_conclusion")
        self.assertEqual(summary["output_rubric_findings"], [])
        self.assertIn(summary["dossier_status"],
                      {"published", "partial_published"})
        self.assertEqual(len(model.repair_prompts), 1)
        self.assertIn("买入", model.repair_prompts[0])

    def test_a_repair_that_keeps_the_conclusion_refuses_once(self):
        model = ConclusionThenCleanModel(repairable=False)
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _lane.FakeModel(route="route:verify"),
            max_units=1)
        self.assertEqual(summary["dossier_status"], "constitution_refused")
        self.assertEqual(summary["findings_repair_rounds"], 1)
        self.assertEqual(len(model.repair_prompts), 1)
        self.assertIn("after one repair call", summary["failure_reason"])


class _Shared:
    """A verifier handle onto one model object, on the verifier's own route.

    The drafting and verifying calls in these fixtures have to agree about what
    was rejected, which they cannot do if they are two objects; independence is
    decided from the route decision, so the route is what differs.
    """

    def __init__(self, model, route="route:verify"):
        self.model = model
        self.route = route

    def call(self, **kwargs):
        result = self.model.call(**kwargs)
        return {**result, "route_decision_ref": self.route}

    def budget_for(self, purpose):
        return {"max_cost_usd": 1.0}


class CooldownTests(unittest.TestCase):
    """A content refusal now holds the company, not the evidence signature.

    The old hold was recorded against the run's signature, and the signature is
    a digest of the company's evidence.  The quantitative-claim promotion lane
    admits new Claims every tick, so every tick produced a *new* key, the hold
    never matched it, and Accenture was relaunched fifty times against the same
    unsupported sentence.
    """

    class Launcher:
        def __init__(self, summary, ticket_status="failed"):
            self.started: list[str] = []
            self.started_companies: list[str | None] = []
            self.summary = summary
            self.ticket_status = ticket_status

        def capacity_probe_interval_seconds(self):
            return None

        def start(self, *, signature, company_ref=None, controlled_reentry=None):
            self.started.append(signature)
            self.started_companies.append(company_ref)
            return {"id": f"company-dossier-run:{len(self.started)}",
                    "signature": signature, "company_ref": company_ref}

        def status(self, ticket_ref):
            return {"id": ticket_ref, "status": self.ticket_status,
                    "signature": self.started[-1],
                    "company_ref": self.started_companies[-1],
                    "summary": self.summary}

    class Clock:
        def __init__(self):
            self.now = datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

    def setUp(self):
        self.harness = _lane.Harness()
        self.addCleanup(self.harness.close)
        self.connection = self.harness.store.connection
        self.clock = self.Clock()
        self.claims = 0

    def coordinator(self, launcher):
        return lane_module.MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher,
            companies=lambda: [ACN], failure_clock=self.clock)

    def refused(self, status="verification_failed"):
        return self.Launcher({
            "company_ref": ACN, "dossier_status": status, "status": "failed",
            "failure_reason": "the cited row does not carry this claim",
            "verification": {"status": "verified", "verdict": "reject"},
        })

    def new_evidence(self):
        """What the promotion lane does every tick: one more Claim."""

        self.claims += 1
        self.harness.tag(f"new-{self.claims}", "demand_drivers",
                         statement=f"又一条新材料 {self.claims}。")

    def refuse_once(self, launcher):
        coordinator = self.coordinator(launcher)
        coordinator.dispatch_once()
        settled = coordinator.dispatch_once()["settled"]
        return coordinator, settled

    def test_a_content_refusal_holds_the_company_for_the_cooldown(self):
        coordinator, settled = self.refuse_once(self.refused())
        self.assertEqual(settled["dossier_status"], "verification_failed")
        held = coordinator.company_cooldown(ACN)
        self.assertIsNotNone(held)
        self.assertIn("verification_failed", held["reason"])
        self.assertEqual(held["seconds"],
                         lane_module.CONTENT_REFUSAL_COOLDOWN_SECONDS)
        self.assertEqual(settled["cooldown"]["until"], held["until"])

    def test_new_evidence_during_the_cooldown_does_not_restart_or_lift_it(self):
        launcher = self.refused()
        coordinator, _ = self.refuse_once(launcher)
        until = coordinator.company_cooldown(ACN)["until"]
        launched = len(launcher.started)
        for _ in range(3):
            self.clock.now += timedelta(minutes=5)
            self.new_evidence()
            result = coordinator.dispatch_once()
            self.assertEqual(result["status"], "held")
            self.assertIn(ACN, result["cooling_down"])
            self.assertEqual(coordinator.company_cooldown(ACN)["until"], until)
        # The whole point: three ticks, three different evidence signatures,
        # no child.
        self.assertEqual(len(launcher.started), launched)

    def test_the_cooldown_expires_on_its_own_and_the_lane_tries_again(self):
        launcher = self.refused("constitution_refused")
        coordinator, _ = self.refuse_once(launcher)
        launched = len(launcher.started)
        self.clock.now += timedelta(
            seconds=lane_module.CONTENT_REFUSAL_COOLDOWN_SECONDS + 1)
        self.new_evidence()
        self.assertIsNone(coordinator.company_cooldown(ACN))
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(len(launcher.started), launched + 1)

    def test_a_reviewed_contract_change_lifts_it_at_once(self):
        # A cooldown is not a suspension: a refusal is a statement made under
        # the drafting and verifying contracts of the day, and a deploy that
        # moves one of them makes the statement void.
        launcher = self.refused()
        coordinator, _ = self.refuse_once(launcher)
        self.assertIsNotNone(coordinator.company_cooldown(ACN))
        with unittest.mock.patch(
                "dalton_core.company_dossier_draft.draft_contract_fingerprint",
                return_value="f" * 64):
            self.assertIsNone(coordinator.company_cooldown(ACN))

    def test_a_published_run_ends_it(self):
        launcher = self.refused()
        coordinator, _ = self.refuse_once(launcher)
        self.assertIsNotNone(coordinator.company_cooldown(ACN))
        launcher.ticket_status = "succeeded"
        launcher.summary = {"company_ref": ACN, "dossier_status": "published",
                            "status": "succeeded"}
        coordinator._open = "company-dossier-run:1"
        coordinator._settle_open()
        self.assertIsNone(coordinator.company_cooldown(ACN))



class FindingsRepairProvenanceTests(unittest.TestCase):
    """The formal replay of a unit that was repaired against findings.

    A contract repair is re-derivable end to end: the parent's reply broke a
    deterministic rule, so the violation list and therefore the repair prompt
    follow from the record.  A *findings* repair cannot be: the parent's reply
    is well formed, and what was wrong with it is a sentence an independent
    verifier wrote.  So the findings are carried in the record and everything
    else is still rebuilt -- the reply the model was shown, the prompt around
    it, and the repair's content-addressed identity.
    """

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from dalton_core.company_dossier_draft import (
            build_unit_prompt, build_verifier_prompt, citable_context, draft_hash,
            parse_unit_output, unit_contract_reminder, unit_reply_wire,
            verifier_prompt_contract_fingerprint, FINDINGS_REMINDER_LINES,
        )
        from dalton_core.company_dossier_cli import findings_repair_contract_name
        from dalton_core.draft_contract_repair import (
            build_findings_repair_prompt, contract_reminder_lines,
            repair_request_id, violations_from_findings,
        )
        from dalton_core.store import canonical_json, content_hash
        from tests.test_company_dossier_draft import (
            STRUCTURE, material, one_sentence, reply,
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
        company = {"company_ref": "company:acn", "ticker": "ACN"}
        parse_input = {"structure": list(STRUCTURE), "material": rows,
                       "prior_body": "", "profile": None, "profile_table": "",
                       "market_view_available": True, "classification": None}
        parent_text = reply([one_sentence("causal_chain:0", ["C1"]),
                             {"slot_id": "causal_chain:1", "unknown": "x"}])
        repaired_text = reply([one_sentence("causal_chain:0", ["C1"]),
                               {"slot_id": "causal_chain:1", "unknown": "y"}])
        parent_block = parse_unit_output(parent_text, unit=unit,
                                         structure=STRUCTURE, material=rows)
        block = parse_unit_output(repaired_text, unit=unit, structure=STRUCTURE,
                                  material=rows)
        draft_prompt = build_unit_prompt(unit=unit, structure=STRUCTURE,
                                         material=rows, company=company)
        producer_input = {
            "unit": unit, "company": company,
            "prompt_sha": content_hash({"prompt": draft_prompt}),
            "mission": {"ref": "mission:v14", "hash": "c" * 64},
            "constitution": {"ref": "constitution-version:us-it-services:1",
                             "hash": "c" * 64},
            "policy": {"ref": "dossier-policy:test:v1",
                       "hash": "ef91d84cfaf1c2c67d2482c4fe97ba8bd4ef373e9db5"
                               "03cc543654b8d3246cf5"},
            "parse_input": parse_input,
        }
        self.findings = [{"code": "unsupported_sentence", "detail":
                          "the cited row does not carry this claim",
                          "unit": unit}]
        violations = violations_from_findings(self.findings)
        parent_request = content_hash({
            "unit": unit, "company": company["company_ref"],
            "prompt_sha": content_hash(draft_prompt)})[:32]
        repair_prompt = build_findings_repair_prompt(
            original_prompt=draft_prompt,
            reply_text=json.dumps(
                unit_reply_wire(parent_block, unit=unit, material=rows),
                ensure_ascii=False, sort_keys=True),
            findings=violations,
            contract_reminder=(contract_reminder_lines(FINDINGS_REMINDER_LINES)
                               + "\n" + unit_contract_reminder(
                                   unit, structure=STRUCTURE)),
            context=citable_context(rows))
        repair_request = repair_request_id(
            parent_request,
            contract_name=findings_repair_contract_name("verification", unit),
            violations=violations)
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
            created = "2026-09-18T00:00:00+00:00"
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
            "input_fingerprint": content_hash(producer_input),
            "producer_input": producer_input,
            "producer_prior_version_ref": None,
            "resolved_classification": None,
            "verified_draft_hash": digest,
            "producer": repair_call,
            "producer_repair": parent_call,
            "producer_repair_findings": {"kind": "verification",
                                         "findings": self.findings},
            "verifier": verifier_call,
        }}

    def validate(self, provenance=None):
        from dalton_core.company_dossier_cli import validate_formal_unit_provenance

        validate_formal_unit_provenance(
            provenance or self.provenance, mission_ref="mission:v14",
            current_prior_ref=None, current_units={self.unit},
            current_blocks=self.blocks,
            scheduler_db=self.scheduler_path, router_db=self.router_path)

    def test_a_findings_repair_resolves_end_to_end(self):
        self.validate()

    def test_the_findings_are_what_the_repair_prompt_is_addressed_on(self):
        # Change one word of the recorded finding and the repair's identity no
        # longer follows from it, so the chain refuses.
        bad = json.loads(json.dumps(self.provenance))
        bad[self.unit]["producer_repair_findings"]["findings"][0]["detail"] = "other"
        with self.assertRaisesRegex(ValueError, "findings repair identity drifted"):
            self.validate(bad)

    def test_a_repair_that_claims_the_wrong_kind_refuses(self):
        bad = json.loads(json.dumps(self.provenance))
        bad[self.unit]["producer_repair_findings"]["kind"] = "output_rubric"
        with self.assertRaisesRegex(ValueError, "findings repair identity drifted"):
            self.validate(bad)

    def test_the_closed_shape_refuses_findings_without_a_parent_call(self):
        from dalton_core.company_dossier import (
            CompanyDossierValidationError, UNITS, _unit_provenance,
        )

        wire = {unit: None for unit in UNITS}
        item = json.loads(json.dumps(self.provenance[self.unit]))
        item.pop("producer_repair")
        wire[self.unit] = item
        with self.assertRaises(CompanyDossierValidationError):
            _unit_provenance(wire)

    def test_the_closed_shape_bounds_what_a_finding_may_carry(self):
        from dalton_core.company_dossier import (
            CompanyDossierValidationError, MAX_REPAIR_FINDINGS, UNITS,
            _unit_provenance,
        )

        for findings in ([], [{}], [{"detail": "x" * 501}],
                         [{"detail": "x"}] * (MAX_REPAIR_FINDINGS + 1),
                         [{"detail": {"nested": 1}}]):
            wire = {unit: None for unit in UNITS}
            item = json.loads(json.dumps(self.provenance[self.unit]))
            item["producer_repair_findings"] = {"kind": "verification",
                                                "findings": findings}
            wire[self.unit] = item
            with self.assertRaises(CompanyDossierValidationError):
                _unit_provenance(wire)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
