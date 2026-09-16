"""WP-C1-1: a carried-forward unit's binding is not this run's to refuse.

The live failure this reproduces, from
``~/Library/Application Support/Dalton/state/dalton-core/company-dossier-runs/``:

* ``a5c4cacf77d37a3dd23105fa``  (2026-09-16T16:13:15Z)
* ``5955c4ddd44e776b678f0e16``  (2026-09-16T14:44:22Z)
* ``b54c8983fcc656b66fefea57``  (2026-09-16T13:39:12Z)

-- 25 runs on 09-14 and 09-16, all ``company:sec-cik:0001467373``, all with::

    "status": "failed",
    "failure_reason": "unexpected ValueError:
        unit_provenance.business_model.producer input binding drifted",
    "units_drafted": ["industry_classification"]

and a ``run.log`` whose whole content is a traceback through
``company_dossier.py:1479 publish_verified`` into
``company_dossier_cli.py:1238``.

``business_model`` was **not** drafted in those runs.  It was published in
version 5 and carried forward untouched ever since.  Replaying it read its
recorded ``producer_input``, rebuilt the prompt with today's
``final_text_instructions()`` and four retained legacy renderings, found none
of them equal to the WorkOrder's question, and threw -- taking the units the
run *had* drafted with it, four times a day, permanently, because
``publish_verified`` requires carried-forward provenance to be byte-identical
to its predecessor's and there is therefore no version ACN can ever publish.

Verified against the live authorities read-only on 2026-09-16: the recorded
prompt hash *is* the hash of the WorkOrder's question, and the request id *is*
derived from that hash.  Only the re-render disagrees, and it disagrees in the
shared house-style block, not in any evidence.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.company_dossier import UnitProvenanceDrift
from dalton_core.company_dossier_cli import validate_formal_unit_provenance
from dalton_core.store import canonical_json, content_hash
# Imported as a module rather than as a name: unittest collects every TestCase
# in a test module's namespace, and importing the class would re-run the whole
# provenance suite under a second name.
from tests import test_dossier_unit_provenance as _provenance_fixtures


class BindingDriftRecoveryTests(unittest.TestCase):
    """The formal-authority fixture, reused; the drift cases, added."""

    setUp = _provenance_fixtures.DossierUnitProvenanceTests.setUp
    _call = _provenance_fixtures.DossierUnitProvenanceTests._call

    def _retire_the_prompt_template(self):
        """Make the recorded prompt one no builder in the tree reproduces.

        Exactly what an edit to ``final_text_instructions()`` does on the fifth
        occasion: the WorkOrder still carries the bytes the model was shown,
        the hash still matches, and nothing in the source tree can re-render
        them.
        """

        scheduler = sqlite3.connect(self.scheduler_path)
        router = sqlite3.connect(self.router_path)
        scheduler.execute("DELETE FROM scheduler_result_envelopes")
        scheduler.execute("DELETE FROM scheduler_work_orders")
        router.execute("DELETE FROM model_route_decisions")
        self.producer_prompt = (
            self.producer_prompt
            + "\n(house style wording that was retired five releases ago)")
        self.producer_input["prompt_sha"] = content_hash(
            {"prompt": self.producer_prompt})
        producer_route = self._call(scheduler, router, "producer", [])
        self._call(scheduler, router, "verifier", [producer_route])
        scheduler.commit(); router.commit(); scheduler.close(); router.close()
        item = self.provenance[self.unit]
        item["producer_input"] = self.producer_input
        item["input_fingerprint"] = content_hash(self.producer_input)
        item["producer"] = self.calls["producer"]
        item["verifier"] = self.calls["verifier"]

    def test_a_carried_forward_unit_is_bound_by_its_hash_not_by_a_re_render(self):
        self._retire_the_prompt_template()
        # ``current_units`` is what ``publish_verified`` passes: the units this
        # run changed.  This one is not among them.
        validate_formal_unit_provenance(
            self.provenance, mission_ref="mission:v14", current_prior_ref=None,
            current_units=set(),
            scheduler_db=self.scheduler_path, router_db=self.router_path,
        )

    def test_a_unit_drafted_on_this_run_still_refuses_closed(self):
        self._retire_the_prompt_template()
        with self.assertRaises(UnitProvenanceDrift) as caught:
            validate_formal_unit_provenance(
                self.provenance, mission_ref="mission:v14", current_prior_ref=None,
                current_units={self.unit},
                scheduler_db=self.scheduler_path, router_db=self.router_path,
            )
        self.assertEqual(caught.exception.unit, self.unit)
        self.assertFalse(caught.exception.carry_forward)
        self.assertIn("producer input binding drifted", str(caught.exception))

    def test_the_refusal_is_a_dossier_error_a_run_can_catch(self):
        # ``run_dossier`` catches ``CompanyDossierError`` and writes a summary;
        # anything else re-raises and the child dies with a traceback and no
        # readable reason.  That is what turned a stale template into 25 lost
        # runs.
        from dalton_core.company_dossier import (
            CompanyDossierError, CompanyDossierValidationError)

        self.assertTrue(issubclass(UnitProvenanceDrift, CompanyDossierError))
        self.assertTrue(issubclass(UnitProvenanceDrift, CompanyDossierValidationError))
        self.assertTrue(issubclass(UnitProvenanceDrift, ValueError))

    def test_an_unresolvable_carried_forward_unit_is_recorded_not_raised(self):
        # The general case: the call itself no longer resolves.  A caller that
        # asks to hear about it gets the unit named and keeps publishing.
        scheduler = sqlite3.connect(self.scheduler_path)
        scheduler.execute("DELETE FROM scheduler_work_orders WHERE work_order_id=?",
                          ("work:producer",))
        scheduler.commit(); scheduler.close()
        drift: list[dict[str, str]] = []
        validate_formal_unit_provenance(
            self.provenance, mission_ref="mission:v14", current_prior_ref=None,
            current_units=set(), carry_forward_drift=drift,
            scheduler_db=self.scheduler_path, router_db=self.router_path,
        )
        self.assertEqual([item["unit"] for item in drift], [self.unit])
        self.assertIn("binding_drift", drift[0]["reason"])
        self.assertIn("does not resolve", drift[0]["reason"])

    def test_without_the_collector_it_still_refuses(self):
        # Opt-in, not a silent downgrade: a caller that does not ask to hear
        # about carried-forward drift gets the refusal it always got.
        scheduler = sqlite3.connect(self.scheduler_path)
        scheduler.execute("DELETE FROM scheduler_work_orders WHERE work_order_id=?",
                          ("work:producer",))
        scheduler.commit(); scheduler.close()
        with self.assertRaises(UnitProvenanceDrift) as caught:
            validate_formal_unit_provenance(
                self.provenance, mission_ref="mission:v14", current_prior_ref=None,
                current_units=set(),
                scheduler_db=self.scheduler_path, router_db=self.router_path,
            )
        self.assertTrue(caught.exception.carry_forward)

    def test_a_unit_drafted_now_is_never_recorded_and_stepped_over(self):
        scheduler = sqlite3.connect(self.scheduler_path)
        scheduler.execute("DELETE FROM scheduler_work_orders WHERE work_order_id=?",
                          ("work:producer",))
        scheduler.commit(); scheduler.close()
        drift: list[dict[str, str]] = []
        with self.assertRaises(UnitProvenanceDrift):
            validate_formal_unit_provenance(
                self.provenance, mission_ref="mission:v14", current_prior_ref=None,
                current_units={self.unit}, carry_forward_drift=drift,
                scheduler_db=self.scheduler_path, router_db=self.router_path,
            )
        self.assertEqual(drift, [])

    def test_a_forged_prompt_is_still_refused_for_a_carried_forward_unit(self):
        # The relaxation is exactly "we no longer require today's template to
        # reproduce yesterday's bytes".  Everything that binds the call to the
        # unit still binds: change the recorded prompt hash and it refuses.
        scheduler = sqlite3.connect(self.scheduler_path)
        row = scheduler.execute(
            "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
            ("work:producer",)).fetchone()
        work = json.loads(row[0])
        work["question"] = work["question"] + " (tampered)"
        scheduler.execute(
            "UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=? "
            "WHERE work_order_id=?",
            (canonical_json(work), content_hash(work), "work:producer"))
        scheduler.commit(); scheduler.close()
        with self.assertRaises(UnitProvenanceDrift):
            validate_formal_unit_provenance(
                self.provenance, mission_ref="mission:v14", current_prior_ref=None,
                current_units=set(),
                scheduler_db=self.scheduler_path, router_db=self.router_path,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RepairedUnitProvenanceTests(unittest.TestCase):
    """WP-C1-2: a unit repaired once still replays end to end.

    The repair reply is the one that became the unit, so ``producer`` names the
    repair call.  What makes that checkable rather than asserted is
    ``producer_repair``: the parent call it repaired, resolved against the same
    authorities, whose own recorded reply plus the deterministic contract
    rebuild the exact repair prompt.
    """

    setUp = _provenance_fixtures.DossierUnitProvenanceTests.setUp
    _call = _provenance_fixtures.DossierUnitProvenanceTests._call

    def build_repair_chain(self, *, broken_text=None):
        from dalton_core.company_dossier_draft import (
            unit_contract, unit_contract_reminder, citable_context)
        from dalton_core.draft_contract_repair import (
            build_repair_prompt, repair_request_id, violations_of)
        from tests.test_company_dossier_draft import STRUCTURE, material

        rows = material()
        broken = broken_text if broken_text is not None else json.dumps({
            "slots": [{"slot_id": STRUCTURE[0]["slot_id"], "unknown": "x",
                       "sentences": [{"text": "一句话。", "refs": ["C1"]}]}],
            "gaps": [],
        }, ensure_ascii=False)
        contract = unit_contract(self.unit, structure=STRUCTURE, material=rows)
        violations = violations_of(broken, contract)
        self.assertTrue(violations)
        repair_prompt = build_repair_prompt(
            original_prompt=self.producer_prompt, reply_text=broken,
            violations=violations,
            contract_reminder=unit_contract_reminder(self.unit, structure=STRUCTURE),
            context=citable_context(rows))
        repair_request = repair_request_id(
            self.calls["producer"]["request_id"], contract_name=contract.name,
            violations=violations)

        scheduler = sqlite3.connect(self.scheduler_path)
        router = sqlite3.connect(self.router_path)
        scheduler.execute("DELETE FROM scheduler_result_envelopes")
        scheduler.execute("DELETE FROM scheduler_work_orders")
        router.execute("DELETE FROM model_route_decisions")
        # The parent: the drafting prompt, and the reply that broke.
        original_text, self.producer_text = self.producer_text, broken
        parent_route = self._call(scheduler, router, "producer_repair", [])
        # The repair: its own prompt, its own identity, the accepted reply.
        self.producer_text = original_text
        held_prompt, self.producer_prompt = self.producer_prompt, repair_prompt
        producer_route = self._call(scheduler, router, "producer", [])
        self.producer_prompt = held_prompt
        self._call(scheduler, router, "verifier", [producer_route])
        # The real call carries its repair identity in the WorkOrder, because
        # that is the id ``model.call`` was given.
        row = scheduler.execute(
            "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
            ("work:producer",)).fetchone()
        work = json.loads(row[0])
        work["metadata"]["request_id"] = repair_request
        work_hash = content_hash(work)
        scheduler.execute(
            "UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=? "
            "WHERE work_order_id=?", (canonical_json(work), work_hash, "work:producer"))
        router.execute(
            "UPDATE model_route_decisions SET work_order_hash=? WHERE work_order_id=?",
            (work_hash, "work:producer"))
        scheduler.commit(); router.commit(); scheduler.close(); router.close()

        item = self.provenance[self.unit]
        item["producer"] = {**self.calls["producer"], "request_id": repair_request}
        item["producer_repair"] = self.calls["producer_repair"]
        item["verifier"] = self.calls["verifier"]
        # The recorded drafting prompt belongs to the parent, not the repair.
        self.producer_input["prompt_sha"] = content_hash(
            {"prompt": held_prompt})
        item["producer_input"] = self.producer_input
        item["input_fingerprint"] = content_hash(self.producer_input)
        return item

    def test_a_repaired_unit_replays_through_its_parent(self):
        self.build_repair_chain()
        validate_formal_unit_provenance(
            self.provenance, mission_ref="mission:v14", current_prior_ref=None,
            current_units={self.unit},
            scheduler_db=self.scheduler_path, router_db=self.router_path,
        )

    def test_a_repair_of_a_reply_that_broke_no_rule_is_refused(self):
        # A repair is only authorized by a violation.  Without one there is
        # nothing to rebuild the prompt from, and a second paid call has no
        # reason to exist.
        with self.assertRaises(AssertionError):
            # The helper itself asserts the reply really did break something.
            self.build_repair_chain(broken_text=self.producer_text)

    def test_a_forged_repair_prompt_is_refused(self):
        self.build_repair_chain()
        scheduler = sqlite3.connect(self.scheduler_path)
        row = scheduler.execute(
            "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
            ("work:producer",)).fetchone()
        work = json.loads(row[0])
        work["question"] = work["question"] + "\nand one more instruction"
        scheduler.execute(
            "UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=? "
            "WHERE work_order_id=?",
            (canonical_json(work), content_hash(work), "work:producer"))
        scheduler.commit(); scheduler.close()
        with self.assertRaises(UnitProvenanceDrift):
            validate_formal_unit_provenance(
                self.provenance, mission_ref="mission:v14", current_prior_ref=None,
                current_units={self.unit},
                scheduler_db=self.scheduler_path, router_db=self.router_path,
            )

    def test_the_repair_must_be_a_different_call_from_what_it_repaired(self):
        from dalton_core.company_dossier import _unit_provenance
        from dalton_core.company_dossier import UNITS

        item = self.build_repair_chain()
        item["producer_repair"] = dict(item["producer"])
        candidate = {unit: None for unit in UNITS}
        candidate[self.unit] = item
        with self.assertRaisesRegex(Exception, "must be a different call"):
            _unit_provenance(candidate)
