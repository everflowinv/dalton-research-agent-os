"""W4: the derived insider context -- arithmetic over filings, no model call."""

from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path

from dalton_core.event_judgement import (
    DERIVED_CONTEXT_KINDS,
    allowed_refs,
    build_judge_prompt,
    derived_context,
)
from dalton_core.insider_context import (
    CONSIDERATIONS,
    InsiderContextError,
    build_insider_context,
    percent_of_holding,
    plan_word,
    prompt_block,
    transaction_flags,
    usd_value,
)
from dalton_core.research_event import (
    PAYLOAD_FIELDS,
    ResearchEventAuthority,
    ResearchEventValidationError,
    record_event,
    validate_payload,
)
from dalton_core.sec_ownership_adapter import parse_form4, rule_10b5_1_checkbox
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec-buyback"
#: The two real filings captured for this slice; see the fixture MANIFEST.
PLAN_FILING = FIXTURES / "form4-10b5-1-nvda-0001696841-26-000010.xml"
NO_PLAN_FILING = FIXTURES / "form4-no-plan-acn-0001487630-26-000018.xml"
#: A synthetic Form 4 written before the 2023 amendments existed: schema X0508
#: has no ``aff10b5One`` element at all.
LEGACY_FILING = FIXTURES.parent / "sec-ownership" / "form4-sale.xml"

OWNER_CIK = "0001900001"
OTHER_CIK = "0001900002"


def insider_payload(**overrides):
    payload = {
        "accession": "0001467373-26-000100",
        "form": "4",
        "owner_name": "Testperson A",
        "owner_cik": OWNER_CIK,
        "role": "officer:Chief Financial Officer",
        "transaction_code": "S",
        "transaction_meaning": "open-market sale",
        "transaction_date": "2026-08-12",
        "security_title": "Class A Ordinary Shares",
        "shares": "4250",
        "price_per_share": "318.4200",
        "acquired_disposed": "D",
        "shares_owned_following": "18600",
        "direct_or_indirect": "D",
        "issuer_name": "ACCENTURE PLC",
        "plan_10b5_1": None,
        "footnotes_hash": None,
        "invocation_ref": "connector-invocation:sec-ownership:" + "a" * 32,
        "artifact_hash": "b" * 64,
        "event_key": "key-1",
    }
    payload.update(overrides)
    return payload


def event(payload, *, event_id="research-event:insider-1", company_ref=ACN,
          kind="insider_transaction"):
    return {
        "id": event_id,
        "company_ref": company_ref,
        "kind": kind,
        "occurred_at": f"{payload.get('transaction_date') or '2026-08-12'}"
                       "T00:00:00+00:00",
        "evidence_tier": "primary_filing",
        "source_refs": [f"sec:filing:{payload['accession']}"],
        "payload": payload,
    }


class CheckboxTests(unittest.TestCase):
    """The Rule 10b5-1 box, verbatim, in all three of its states."""

    def test_a_ticked_box_is_read_as_ticked(self):
        self.assertIs(
            rule_10b5_1_checkbox(PLAN_FILING.read_text(encoding="utf-8")), True
        )

    def test_an_unticked_box_is_read_as_unticked(self):
        self.assertIs(
            rule_10b5_1_checkbox(NO_PLAN_FILING.read_text(encoding="utf-8")), False
        )

    def test_a_form_without_the_element_is_unknown_and_not_false(self):
        # The whole point of the third state: schema X0508 predates the box, and
        # calling its absence False would report every pre-2023 sale as one the
        # executive timed themselves.
        self.assertIsNone(
            rule_10b5_1_checkbox(LEGACY_FILING.read_text(encoding="utf-8"))
        )
        self.assertEqual(plan_word(None), "unknown")
        self.assertEqual(plan_word(False), "no")
        self.assertEqual(plan_word(True), "yes")

    def test_two_elements_that_disagree_say_nothing(self):
        text = (
            "<?xml version='1.0'?><ownershipDocument>"
            "<documentType>4</documentType>"
            "<aff10b5One>1</aff10b5One><aff10b5One>0</aff10b5One>"
            "</ownershipDocument>"
        )
        self.assertIsNone(rule_10b5_1_checkbox(text))

    def test_the_connector_wire_is_not_widened_by_this(self):
        # The frozen output contract's shape is hashed into the approval the
        # running writer holds for form4_transactions, so the checkbox lives
        # beside the wire and not in it.
        raw = PLAN_FILING.read_bytes()
        wire = parse_form4(
            raw.decode("utf-8"),
            accession="0001696841-26-000010",
            artifact_hash=hashlib.sha256(raw).hexdigest(),
        )
        self.assertNotIn("rule_10b5_1_checkbox", wire)
        self.assertNotIn("plan_10b5_1", wire)


class PayloadContractTests(unittest.TestCase):
    def test_the_insider_payload_carries_the_checkbox_and_the_footnote_hash(self):
        fields = PAYLOAD_FIELDS["insider_transaction"]
        self.assertIn("plan_10b5_1", fields)
        self.assertIn("footnotes_hash", fields)

    def test_a_field_nobody_declared_is_still_refused(self):
        with self.assertRaises(ResearchEventValidationError):
            validate_payload(
                "insider_transaction", insider_payload(plan_reason="he wanted to")
            )

    def test_the_checkbox_survives_the_payload_validator_as_a_boolean(self):
        body = validate_payload("insider_transaction", insider_payload(plan_10b5_1=True))
        self.assertIs(body["plan_10b5_1"], True)
        body = validate_payload("insider_transaction", insider_payload())
        self.assertIsNone(body["plan_10b5_1"])


class ArithmeticTests(unittest.TestCase):
    def test_size_is_measured_against_the_holding_after_the_trade(self):
        self.assertEqual(percent_of_holding("4250", "18600"), "22.85")

    def test_a_holding_of_nothing_is_not_a_division(self):
        self.assertIsNone(percent_of_holding("4250", "0"))
        self.assertIsNone(percent_of_holding(None, "18600"))

    def test_a_filed_comma_is_not_a_different_number(self):
        self.assertEqual(percent_of_holding("4,250", "18,600"), "22.85")

    def test_value_is_shares_times_price_and_a_grant_is_worth_zero(self):
        self.assertEqual(usd_value("4250", "318.4200"), "1353285.00")
        self.assertEqual(usd_value("1200", "0"), "0.00")

    def test_a_row_with_no_price_has_no_value_rather_than_a_zero_one(self):
        self.assertIsNone(usd_value("1200", None))

    def test_the_editorial_flags_name_mechanics_rather_than_decisions(self):
        self.assertEqual(
            transaction_flags(insider_payload(transaction_code="F", price_per_share="0")),
            ["tax_withholding"],
        )
        self.assertEqual(
            sorted(transaction_flags(
                insider_payload(transaction_code="A", price_per_share="0")
            )),
            ["no_consideration", "routine_award"],
        )
        self.assertEqual(transaction_flags(insider_payload()), [])


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)

    def context(self, subject=None, others=(), connection=None):
        return build_insider_context(
            subject or event(insider_payload()),
            company_events=others,
            connection=connection,
        )

    def test_a_kind_this_does_not_read_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(InsiderContextError):
            build_insider_context(event(insider_payload(), kind="filing"))

    def test_the_subject_row_carries_the_derived_figures(self):
        subject = self.context()["subject"]
        self.assertEqual(subject["percent_of_holding_following"], "22.85")
        self.assertEqual(subject["usd_value"], "1353285.00")
        self.assertEqual(subject["direction"], "disposal")
        self.assertEqual(subject["plan_10b5_1"], "unknown")

    def test_the_trailing_window_covers_the_same_owner_and_every_insider(self):
        others = [
            event(
                insider_payload(
                    transaction_date="2026-07-15", shares="1000",
                    price_per_share="300", event_key="key-2",
                    accession="0001467373-26-000101",
                ),
                event_id="research-event:insider-2",
            ),
            event(
                insider_payload(
                    owner_name="Testperson B", owner_cik=OTHER_CIK,
                    transaction_date="2026-08-10", shares="500",
                    price_per_share="310", event_key="key-3",
                    accession="0001467373-26-000102",
                ),
                event_id="research-event:insider-3",
            ),
            # Outside ninety days: present in the ledger, absent from the window.
            event(
                insider_payload(
                    transaction_date="2026-01-02", shares="99999",
                    price_per_share="200", event_key="key-4",
                    accession="0001467373-26-000103",
                ),
                event_id="research-event:insider-4",
            ),
        ]
        found = self.context(others=others)
        self.assertEqual(found["owner_trailing"]["disposals"], 2)
        self.assertEqual(found["owner_trailing"]["disposal_shares"], "5250")
        self.assertEqual(found["issuer_trailing"]["disposals"], 3)
        self.assertEqual(found["issuer_trailing"]["disposal_shares"], "5750")
        self.assertEqual(found["ledger_earliest"], "2026-01-02")

    def test_withholding_and_awards_are_counted_apart_from_selling(self):
        others = [
            event(
                insider_payload(
                    transaction_code="F", acquired_disposed="D", shares="900",
                    price_per_share="0", transaction_date="2026-08-11",
                    event_key="key-5", accession="0001467373-26-000104",
                ),
                event_id="research-event:insider-5",
            ),
            event(
                insider_payload(
                    transaction_code="A", acquired_disposed="A", shares="1200",
                    price_per_share="0", transaction_date="2026-08-11",
                    event_key="key-6", accession="0001467373-26-000105",
                ),
                event_id="research-event:insider-6",
            ),
        ]
        owner = self.context(others=others)["owner_trailing"]
        self.assertEqual(owner["disposals"], 1)
        self.assertEqual(owner["disposal_shares"], "4250")
        self.assertEqual(owner["withheld_shares"], "900")
        self.assertEqual(owner["no_consideration_shares"], "1200")

    def test_a_window_with_a_priceless_row_reports_its_dollars_as_a_floor(self):
        others = [
            event(
                insider_payload(
                    shares="100", price_per_share=None, transaction_date="2026-08-11",
                    event_key="key-7", accession="0001467373-26-000106",
                ),
                event_id="research-event:insider-7",
            ),
        ]
        self.assertTrue(self.context(others=others)["owner_trailing"]["usd_is_a_floor"])

    def test_an_exercise_and_a_sale_on_one_day_are_reported_as_one_act(self):
        others = [
            event(
                insider_payload(
                    transaction_code="M", acquired_disposed="A", shares="1200",
                    price_per_share="0", transaction_date="2026-08-12",
                    event_key="key-8", accession="0001467373-26-000107",
                ),
                event_id="research-event:insider-8",
            ),
        ]
        pairs = self.context(others=others)["exercise_and_sell"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["exercised_shares"], "1200")
        self.assertEqual(pairs[0]["sold_shares"], "4250")
        self.assertEqual(pairs[0]["cash_realised_usd"], "1353285.00")

    def test_insiders_trading_in_the_same_week_are_counted(self):
        others = [
            event(
                insider_payload(
                    owner_name="Testperson B", owner_cik=OTHER_CIK,
                    transaction_date="2026-08-10", event_key="key-9",
                    accession="0001467373-26-000108",
                ),
                event_id="research-event:insider-9",
            ),
            event(
                insider_payload(
                    owner_name="Testperson C", owner_cik="0001900003",
                    transaction_date="2026-05-10", event_key="key-10",
                    accession="0001467373-26-000109",
                ),
                event_id="research-event:insider-10",
            ),
        ]
        found = self.context(others=others)
        self.assertEqual(found["cluster_owner_count"], 2)
        self.assertEqual(found["cluster_owners"], ["Testperson A", "Testperson B"])

    # -- anticipation ------------------------------------------------------

    def form144(self, *, person="Testperson A", event_date="2026-06-30"):
        return {
            "id": "research-event:notice-1",
            "company_ref": ACN,
            "kind": "ownership_change",
            "occurred_at": f"{event_date}T00:00:00+00:00",
            "evidence_tier": "primary_filing",
            "source_refs": ["sec:filing:0001467373-26-000200"],
            "payload": {
                "accession": "0001467373-26-000200",
                "form": "144",
                "reporting_person": person,
                "person_cik": None,
                "person_type": "planned sale by Officer",
                "aggregate_shares": "5000",
                "event_date": event_date,
                "security_class": "Class A Ordinary Shares",
                "is_amendment": False,
            },
        }

    def test_a_form_144_by_the_same_person_anticipates_the_sale(self):
        found = self.context(others=[self.form144()])
        self.assertEqual(found["anticipated"], "true")
        self.assertEqual(found["anticipation"][0]["kind"], "form_144")
        self.assertIn("research-event:notice-1", found["refs"])

    def test_a_form_144_by_somebody_else_does_not(self):
        found = self.context(others=[self.form144(person="Somebody Else")])
        self.assertNotEqual(found["anticipated"], "true")

    def test_a_form_144_from_before_the_window_does_not(self):
        found = self.context(others=[self.form144(event_date="2025-01-01")])
        self.assertNotEqual(found["anticipated"], "true")

    def test_with_no_ledger_to_search_the_answer_is_unknown_and_not_false(self):
        found = self.context()
        self.assertEqual(found["anticipated"], "unknown")
        self.assertIn("no ledger", found["anticipated_reason"])

    def test_thin_capital_allocation_coverage_is_unknown(self):
        self._claims([("claim-1", "Bookings grew 12% year on year in the third quarter.")])
        found = self.context(connection=self.connection)
        self.assertEqual(found["anticipated"], "unknown")
        self.assertIn("coverage_thin", found["anticipated_reason"])

    def test_five_capital_allocation_claims_can_support_false(self):
        self._claims([
            (f"claim-{index}", f"Capital allocation observation {index} has no sale plan.")
            for index in range(5)
        ])
        found = self.context(connection=self.connection)
        self.assertEqual(found["anticipated"], "false")
        self.assertIn("searched 5 Claims", found["anticipated_reason"])

    def test_a_claim_that_expects_a_sale_answers_true_with_its_ref(self):
        self._claims([
            ("claim-2",
             "The CFO adopted a Rule 10b5-1 trading plan in May covering 24,000 shares."),
        ])
        found = self.context(connection=self.connection)
        self.assertEqual(found["anticipated"], "true")
        self.assertEqual(found["anticipation"][0]["ref"], "claim-2")
        self.assertIn("claim-2", found["refs"])

    def _claims(self, rows):
        self.connection.execute(
            "CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY, "
            "claim_ref TEXT, claim_json TEXT, created_at TEXT)"
        )
        import json

        for ref, statement in rows:
            self.connection.execute(
                "INSERT INTO claim_versions VALUES(?,?,?,?)",
                (ref, ref, json.dumps({"subject_ref": ACN, "statement": statement,
                                       "aspect": "management_and_capital_allocation"}),
                 "2026-08-01T00:00:00+00:00"),
            )


class PromptTests(unittest.TestCase):
    def test_the_block_prints_the_figures_the_owner_asked_for(self):
        text = "\n".join(prompt_block(build_insider_context(event(insider_payload()))))
        for needle in ("22.85%", "1353285.00", "Rule 10b5-1 plan box on the form: unknown",
                       "did anything anticipate this sale"):
            self.assertIn(needle, text)

    def test_an_unknown_checkbox_is_explained_rather_than_left_to_be_misread(self):
        text = "\n".join(prompt_block(build_insider_context(event(insider_payload()))))
        self.assertIn("do not read it as an unplanned sale", text)

    def test_the_owners_reading_is_printed_as_considerations_not_rules(self):
        text = "\n".join(prompt_block(build_insider_context(event(insider_payload()))))
        self.assertIn("considerations, not rules; you decide", text)
        self.assertIn(CONSIDERATIONS[1][:40], text)

    def test_nothing_in_the_block_decides_anything(self):
        # The five words stay the judgement lane's. A context that told the
        # brain what to conclude would be a rule wearing a table's clothes.
        text = "\n".join(prompt_block(build_insider_context(event(insider_payload()))))
        for word in ("NO_CHANGE", "THESIS_WEAKENED", "THESIS_BROKEN",
                     "THESIS_STRENGTHENED", "NEW_THESIS"):
            self.assertNotIn(word, text)


class JudgementWiringTests(P14aHarness):
    """The block reaches the prompt, and its refs become citable."""

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)

    def record(self, payload, **kwargs):
        return record_event(
            self.events, company_ref=ACN, kind="insider_transaction",
            occurred_at=f"{payload['transaction_date']}T00:00:00+00:00",
            source_refs=[f"sec:filing:{payload['accession']}"],
            payload=payload, mission=self.mission, actor_ref=AUTOMATION, **kwargs,
        )

    def test_an_insider_transaction_is_a_kind_that_carries_a_derived_block(self):
        self.assertIn("insider_transaction", DERIVED_CONTEXT_KINDS)
        self.assertIn("buyback_disclosure", DERIVED_CONTEXT_KINDS)

    def test_the_block_is_built_from_the_ledger_and_reaches_the_prompt(self):
        older = self.record(insider_payload(
            transaction_date="2026-07-15", shares="1000", price_per_share="300",
            event_key="key-older", accession="0001467373-26-000111",
        ))
        subject = self.record(insider_payload(plan_10b5_1=True))
        found = derived_context(
            subject, connection=self.store.connection,
            recent_events=self.events.events(company_ref=ACN, limit=40),
        )
        self.assertEqual(found["context"]["owner_trailing"]["disposals"], 2)
        self.assertIn(older["id"], found["refs"])
        context = {
            "event": subject, "company_ref": ACN, "ticker": "ACN",
            "derived_lines": found["lines"], "derived_refs": found["refs"],
        }
        prompt = build_judge_prompt(context)
        self.assertIn("Derived insider context", prompt)
        self.assertIn("Rule 10b5-1 plan box on the form: yes", prompt)
        self.assertIn(older["id"], allowed_refs(context))

    def test_a_derived_block_that_cannot_be_built_does_not_stop_the_judgement(self):
        # A judgement that could not be made because a *derived comparison*
        # failed would be the arithmetic deciding what gets judged. So the
        # block goes missing, with its reason, and the event is still judged on
        # its payload.
        subject = self.record(insider_payload())
        closed = sqlite3.connect(":memory:")
        closed.close()
        broken = derived_context(subject, connection=closed)
        self.assertIsNone(broken["context"])
        self.assertEqual(broken["refs"], [])
        self.assertIn("could not be built", "\n".join(broken["lines"]))

    def test_a_kind_with_no_derived_block_gets_none(self):
        self.assertIsNone(
            derived_context({"kind": "news", "company_ref": ACN},
                            connection=self.store.connection)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
