"""P12a: the file is a chain, its shape is not the model's, and a rewrite is refused.

Four things carry this authority and everything else follows from them.

A version that cites nothing the current one did not is a ``duplicate``, not a
version (ADR-0008). That is the whole guard against restatement drift: a
dossier that cannot name what it learned has learned nothing, however different
the prose.

``demand_drivers`` and ``supply_and_cost`` take their slots from the
Constitution's causal chain, and a chain the policy has not mapped makes those
sections *unavailable* rather than free-form. A model that got to choose its
own structure would have replaced the methodology with its own and the version
chain would record the swap as prose.

A section that cannot be filled says why, from a closed list, and carries no
text at all. Padding is the failure mode of every file-shaped document.

And the Constitution's ``output_rubric`` is a published standard, so a
criterion the policy neither binds to a check nor declares inapplicable is
itself a finding: reading the criteria you happen to understand is not reading
the standard.
"""

from __future__ import annotations

import unittest

from dalton_core.company_dossier import (
    CLASSIFICATION_SLOTS,
    SECTIONS,
    UNITS,
    VARIANT_SLOTS,
    CompanyDossierAuthority,
    CompanyDossierConflict,
    CompanyDossierValidationError,
    DossierStructureUnmapped,
    causal_chain_hash,
    dossier_artefact,
    evidence_scope,
    load_policy,
    new_refs,
    output_rubric_findings,
    policy_hash,
    section_body,
    section_slots,
    unit_slots,
    validate_dossier_version,
    validate_policy,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_claim_index_entries import ACN, LedgerFixture

ACTOR = "automation:coverage-mission"
CHAIN = [
    "Enterprise IT budgets react to macro confidence with a one-to-two-quarter lag.",
    "New bookings lead revenue by two to four quarters.",
    "Delivery economics transmit demand into operating margin.",
]
CRITERIA = [
    "Every number in an output traces to a formal EvidenceVersion.",
    "A good research output reduces the open question set or sharpens a falsifier.",
]


def constitution(chain=CHAIN, criteria=CRITERIA):
    return {
        "id": "constitution-version:us-it-services:1",
        "constitution_ref": "constitution:us-it-services",
        "content_hash": "c" * 64,
        "method": {
            "causal_chain": list(chain),
            "output_rubric": {"criteria": list(criteria), "good_samples": [],
                              "bad_samples": []},
        },
    }


def policy(chain=CHAIN, criteria=CRITERIA, sections=("demand_drivers", "demand_drivers",
                                                     "supply_and_cost")):
    return validate_policy({
        "schema_version": "0.1",
        "policy_ref": "dossier-policy:test:v1",
        "causal_chain_maps": [{
            "constitution_ref": "constitution:us-it-services",
            "causal_chain_hash": causal_chain_hash(chain),
            "sections": list(sections),
            "note": "test",
        }],
        "output_rubric_bindings": [
            {"criterion_hash": content_hash(criteria[0]),
             "check": "numbers_trace_to_refs", "reason": ""},
            {"criterion_hash": content_hash(criteria[1]),
             "check": "not_a_restatement", "reason": ""},
        ],
    })


def source(ref, text="收入为 USD 5", kind="claim", period="2026-05-31"):
    return {"kind": kind, "ref": ref, "text": text, "period": period}


def drafted(aspect, ref, text="这一节的判断由引用支撑。", *, structure=None,
            source_text="收入为 USD 5"):
    ids = list(structure or [aspect])
    return {
        "aspect": aspect, "status": "drafted", "reason": None,
        "structure": ids,
        "slots": [{"slot_id": ids[0],
                   "sentences": [{"text": text, "refs": [ref]}]}]
        + [{"slot_id": item, "unknown": "材料没有回答这一点"} for item in ids[1:]],
        "sources": [source(ref, source_text)],
        "gaps": [], "profile": None,
    }


def unavailable(aspect, reason="no_canonical_claims"):
    return {"aspect": aspect, "status": "unavailable", "reason": reason,
            "structure": [], "slots": [], "sources": [], "gaps": [], "profile": None}


def classification(ref=None, word="contract_compounder"):
    if ref is None:
        return {"classification": "insufficient_evidence",
                "slots": [{"slot_id": slot, "unknown": "未起草"}
                          for slot in CLASSIFICATION_SLOTS],
                "sources": [], "gaps": []}
    return {
        "classification": word,
        "slots": [{"slot_id": slot,
                   "sentences": [{"text": f"{slot} 的理由。", "refs": [ref]}]}
                  for slot in CLASSIFICATION_SLOTS],
        "sources": [source(ref, "合同期限为五年")],
        "gaps": [],
    }


def variant(ref=None, available=False):
    if ref is None:
        return {"status": "unavailable", "reason": "not_drafted_this_run",
                "market_view_available": False, "market_view_reason": None,
                "structure": [], "slots": [], "sources": [], "gaps": []}
    ids = [slot for slot in VARIANT_SLOTS if available or slot != "market_view"]
    return {
        "status": "drafted", "reason": None, "market_view_available": available,
        "market_view_reason": None if available else "没有卖方或共识材料",
        "structure": ids,
        "slots": [{"slot_id": slot,
                   "sentences": [{"text": f"{slot} 的一句话。", "refs": [ref]}]}
                  for slot in ids],
        "sources": [source(ref, "卖方给出的目标区间")],
        "gaps": [],
    }


def bindings():
    return {
        "constitution_version": {"ref": "constitution-version:us-it-services:1",
                                 "hash": "c" * 64},
        "playbook_version": {"ref": "playbook-version:1", "hash": "d" * 64},
        "mission_version_ref": "mission-version:1",
        "policy_ref": "dossier-policy:test:v1",
        "policy_hash": policy_hash(policy()),
        "causal_chain_hash": causal_chain_hash(CHAIN),
        "rubric_ref": "rubric:company-dossier",
        "rubric_hash": "e" * 64,
    }


def body(*, drafted_sections, classification_block=None, variant_block=None,
         change_reason="evidence_thicker", evidence=None, company_ref=ACN,
         prior_ref=None):
    sections = []
    for aspect in SECTIONS:
        sections.append(drafted_sections.get(aspect) or unavailable(aspect))
    refs = evidence if evidence is not None else [
        row for section in sections for row in section["sources"]
    ][:1]
    record = {
        "company_ref": company_ref,
        "sections": sections,
        "industry_classification": classification_block or classification(),
        "variant_view": variant_block or variant(),
        "bindings": bindings(),
        "actor_ref": ACTOR,
        "change_reason": change_reason,
        "evidence_refs": refs,
        "decision": None,
    }
    if prior_ref is not None:
        record["computed_from_version_ref"] = prior_ref
    return record


class StructureTests(unittest.TestCase):
    def test_eight_sections_take_their_slot_from_the_aspect_definition(self):
        slots = section_slots("business_model")
        self.assertEqual([slot["slot_id"] for slot in slots], ["business_model"])
        self.assertIn("earns money", slots[0]["prompt"])

    def test_the_two_constitution_sections_take_their_slots_from_the_chain(self):
        demand = section_slots("demand_drivers", constitution=constitution(),
                               policy=policy())
        supply = section_slots("supply_and_cost", constitution=constitution(),
                               policy=policy())
        self.assertEqual([slot["slot_id"] for slot in demand],
                         ["causal_chain:0", "causal_chain:1"])
        self.assertEqual([slot["prompt"] for slot in demand], CHAIN[:2])
        self.assertEqual([slot["slot_id"] for slot in supply], ["causal_chain:2"])

    def test_an_unmapped_chain_refuses_rather_than_improvising(self):
        moved = CHAIN + ["Cash conversion is a trailing confirmation."]
        with self.assertRaises(DossierStructureUnmapped):
            section_slots("demand_drivers", constitution=constitution(moved),
                          policy=policy())

    def test_the_variant_view_drops_its_market_slot_when_nothing_shows_one(self):
        with_market = unit_slots("variant_view", market_view_available=True)
        without = unit_slots("variant_view", market_view_available=False)
        self.assertIn("market_view", [slot["slot_id"] for slot in with_market])
        self.assertNotIn("market_view", [slot["slot_id"] for slot in without])

    def test_the_shipped_policy_maps_the_live_constitutions_chain(self):
        import json
        from pathlib import Path

        manifest = json.loads(
            Path("deploy/phase8/p8a-us-it-services-bootstrap-v1.json").read_text(
                encoding="utf-8"))
        live = manifest["constitution"]["method"]["causal_chain"]
        shipped = load_policy()
        entry = next(item for item in shipped["causal_chain_maps"]
                     if item["causal_chain_hash"] == causal_chain_hash(live))
        self.assertEqual(len(entry["sections"]), len(live))
        self.assertEqual(set(entry["sections"]), {"demand_drivers", "supply_and_cost"})


class SectionContractTests(unittest.TestCase):
    def test_a_sentence_must_cite_something_that_was_shown(self):
        section = drafted("business_model", "claim-version:a")
        section["slots"][0]["sentences"][0]["refs"] = ["claim-version:never-shown"]
        with self.assertRaises(CompanyDossierValidationError) as caught:
            validate_dossier_version  # imported for the module, checked below
            from dalton_core.company_dossier import validate_section

            validate_section(section, "sections[0]")
        self.assertIn("not among the material shown", str(caught.exception))

    def test_a_slot_the_structure_did_not_ask_for_is_refused(self):
        from dalton_core.company_dossier import validate_section

        section = drafted("business_model", "claim-version:a")
        section["slots"][0]["slot_id"] = "a_slot_i_made_up"
        with self.assertRaises(CompanyDossierValidationError) as caught:
            validate_section(section, "sections[0]")
        self.assertIn("not the model's to choose", str(caught.exception))

    def test_a_missing_slot_is_refused_rather_than_left_blank(self):
        from dalton_core.company_dossier import validate_section

        section = drafted("demand_drivers", "claim-version:a",
                          structure=["causal_chain:0", "causal_chain:1"])
        section["slots"] = section["slots"][:1]
        with self.assertRaises(CompanyDossierValidationError) as caught:
            validate_section(section, "sections[2]")
        self.assertIn("every slot", str(caught.exception))

    def test_a_slot_may_be_explicitly_unknown(self):
        from dalton_core.company_dossier import validate_section

        section = drafted("demand_drivers", "claim-version:a",
                          structure=["causal_chain:0", "causal_chain:1"])
        checked = validate_section(section, "sections[2]")
        self.assertEqual(checked["slots"][1], {"slot_id": "causal_chain:1",
                                               "unknown": "材料没有回答这一点"})

    def test_the_sentence_cap_is_enforced_per_slot(self):
        from dalton_core.company_dossier import SLOT_SENTENCE_CAP, validate_section

        section = drafted("business_model", "claim-version:a")
        section["slots"][0]["sentences"] = [
            {"text": f"第 {index} 句。", "refs": ["claim-version:a"]}
            for index in range(SLOT_SENTENCE_CAP + 1)
        ]
        with self.assertRaises(CompanyDossierValidationError) as caught:
            validate_section(section, "sections[0]")
        self.assertIn("the cap is", str(caught.exception))

    def test_a_source_nobody_cites_is_refused(self):
        from dalton_core.company_dossier import validate_section

        section = drafted("business_model", "claim-version:a")
        section["sources"].append(source("claim-version:b", "另一条"))
        with self.assertRaises(CompanyDossierValidationError) as caught:
            validate_section(section, "sections[0]")
        self.assertIn("no sentence cites", str(caught.exception))

    def test_an_unavailable_section_carries_no_prose_and_a_closed_reason(self):
        from dalton_core.company_dossier import validate_section

        section = unavailable("catalyst_calendar", "no_catalyst_calendar_authority")
        checked = validate_section(section, "sections[8]")
        self.assertEqual(section_body(checked), "")
        section["reason"] = "we were busy"
        with self.assertRaises(CompanyDossierValidationError):
            validate_section(section, "sections[8]")

    def test_a_section_whose_every_slot_is_unknown_is_unavailable_not_drafted(self):
        from dalton_core.company_dossier import validate_section

        section = drafted("business_model", "claim-version:a")
        section["slots"] = [{"slot_id": "business_model", "unknown": "没有材料"}]
        with self.assertRaises(CompanyDossierValidationError) as caught:
            validate_section(section, "sections[0]")
        self.assertIn("unavailable, not drafted", str(caught.exception))


class ProseTests(unittest.TestCase):
    def test_a_citation_tag_in_the_prose_is_refused_by_the_authority(self):
        from dalton_core.company_dossier import validate_section

        # The rule the drafting prompt states, enforced where it cannot be
        # skipped: a rule that lives only in a prompt is a rule the next
        # drafter will not have read, and the published Initial Screens are
        # what happens then.
        for text in ("C3显示公司通过两类合同赚钱。", "见 N12。", "两类合同（C7）。"):
            section = drafted("business_model", "claim-version:a", text)
            with self.assertRaises(CompanyDossierValidationError) as caught:
                validate_section(section, "sections[0]")
            self.assertIn("into the prose", str(caught.exception))

    def test_ordinary_prose_that_merely_contains_a_letter_and_a_digit_is_fine(self):
        from dalton_core.company_dossier import validate_section

        for text in ("公司的 CN2 专线业务在扩张。", "毛利率为 C 类合同拖累。",
                     "the ACN3000 platform was retired."):
            validate_section(drafted("business_model", "claim-version:a", text),
                             "sections[0]")

    def test_chinese_sentences_run_together_and_english_ones_do_not(self):
        chinese = {"slots": [{"slot_id": "s", "sentences": [
            {"text": "第一句。", "refs": ["r"]}, {"text": "第二句。", "refs": ["r"]}]}]}
        english = {"slots": [{"slot_id": "s", "sentences": [
            {"text": "The first.", "refs": ["r"]},
            {"text": "The second.", "refs": ["r"]}]}]}
        self.assertEqual(section_body(chinese), "第一句。第二句。")
        self.assertEqual(section_body(english), "The first. The second.")


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.authority = CompanyDossierAuthority(self.fixture.store)

    def publish(self, **kwargs):
        return self.authority.publish(body(**kwargs))

    def test_plural_migration_coexists_with_the_abandoned_singular_column(self):
        connection = self.fixture.store.connection
        connection.execute(
            "ALTER TABLE company_dossier_versions ADD COLUMN input_fingerprint TEXT")
        connection.execute(
            "ALTER TABLE company_dossier_versions DROP COLUMN input_fingerprints_json")
        CompanyDossierAuthority(self.fixture.store)
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(company_dossier_versions)").fetchall()}
        self.assertIn("input_fingerprint", columns)
        self.assertIn("input_fingerprints_json", columns)

    def test_the_first_version_is_a_chain_of_one_and_reads_back(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(first["version"], 1)
        self.assertIsNone(first["prior_version_ref"])
        again = self.authority.dossier(first["id"])
        self.assertEqual(again["content_hash"], first["content_hash"])
        self.assertEqual(self.authority.latest(ACN)["id"], first["id"])

    def test_input_fingerprint_has_fresh_stale_and_legacy_unknown_states(self):
        legacy = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        self.assertEqual(self.authority.input_freshness(
            legacy["id"], {unit: "a" * 64 for unit in UNITS}), "unknown")
        candidate = body(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a"),
            "segments_and_mix": drafted("segments_and_mix", "claim-version:b")},
            prior_ref=legacy["id"])
        candidate["input_fingerprints"] = {unit: "d" * 64 for unit in UNITS}
        second = self.authority.publish(candidate)
        self.assertEqual(second["schema_version"], "0.2")
        self.assertEqual(self.authority.input_freshness(
            second["id"], {unit: "d" * 64 for unit in UNITS}), "fresh")
        self.assertEqual(self.authority.input_freshness(
            second["id"], {unit: "e" * 64 for unit in UNITS}), "stale")

    def test_input_fingerprint_is_bound_by_the_record_hash(self):
        candidate = body(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        candidate["input_fingerprints"] = {unit: "d" * 64 for unit in UNITS}
        published = self.authority.publish(candidate)
        forged = {key: value for key, value in published.items() if key != "status"}
        forged["input_fingerprints"]["business_model"] = "e" * 64
        with self.assertRaises(CompanyDossierConflict):
            validate_dossier_version(forged)

    def test_a_version_citing_nothing_new_is_a_duplicate(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        rewrite = self.authority.publish(body(
            drafted_sections={"business_model": drafted(
                "business_model", "claim-version:a", "换一种说法，但没有新证据。")},
            prior_ref=first["id"]))
        self.assertEqual(rewrite["status"], "duplicate")
        self.assertEqual(rewrite["duplicate_reason"], "no_new_evidence")
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_an_identical_body_is_a_duplicate_for_a_different_reason(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        same = self.authority.publish(body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            prior_ref=first["id"]))
        self.assertEqual(same["duplicate_reason"], "identical_body")

    def test_one_new_ref_is_enough_to_be_a_version(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        second = self.authority.publish(body(
            drafted_sections={
                "business_model": drafted("business_model", "claim-version:a"),
                "segments_and_mix": drafted("segments_and_mix", "claim-version:b"),
            },
            prior_ref=first["id"]))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        self.assertEqual(new_refs(second, first), ["claim-version:b"])

    def test_a_version_must_say_why_it_exists(self):
        with self.assertRaises(CompanyDossierValidationError):
            self.publish(drafted_sections={
                "business_model": drafted("business_model", "claim-version:a")},
                change_reason="because_i_felt_like_it")
        with self.assertRaises(CompanyDossierValidationError):
            self.authority.publish(body(
                drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
                evidence=[]))

    def test_a_body_computed_from_a_stale_head_is_refused(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        self.authority.publish(body(
            drafted_sections={
                "business_model": drafted("business_model", "claim-version:a"),
                "segments_and_mix": drafted("segments_and_mix", "claim-version:b")},
            prior_ref=first["id"]))
        with self.assertRaises(CompanyDossierConflict):
            self.authority.publish(body(
                drafted_sections={
                    "business_model": drafted("business_model", "claim-version:a"),
                    "kpi_dictionary": drafted("kpi_dictionary", "claim-version:c")},
                prior_ref=first["id"]))

    def test_versions_are_immutable_at_the_database(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        with self.assertRaises(Exception):
            self.fixture.store.connection.execute(
                "UPDATE company_dossier_versions SET content_hash='x' WHERE version_id=?",
                (first["id"],))
        with self.assertRaises(Exception):
            self.fixture.store.connection.execute(
                "DELETE FROM company_dossier_versions WHERE version_id=?", (first["id"],))

    def test_an_unauthorised_writer_cannot_insert(self):
        with self.assertRaises(Exception):
            self.fixture.store.connection.execute(
                "INSERT INTO company_dossier_versions(version_id,dossier_ref,"
                "version_number,prior_version_id,company_ref,change_reason,body_hash,"
                "evidence_scope_hash,record_json,content_hash,actor_ref,created_at) "
                "VALUES('v','d',1,NULL,'c','evidence_thicker','h','s','{}','h','a','t')")

    def test_the_chain_replays_one_section(self):
        first = self.publish(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a", "第一版。")})
        self.authority.publish(body(
            drafted_sections={"business_model": drafted(
                "business_model", "claim-version:b", "第二版，新证据推进了判断。")},
            prior_ref=first["id"]))
        replay = self.authority.replay_section(ACN, "business_model")
        self.assertEqual([item["version"] for item in replay], [1, 2])
        self.assertEqual(replay[0]["body"], "第一版。")
        self.assertEqual(replay[1]["refs"], ["claim-version:b"])
        # And a section nobody drafted replays as unavailable rather than absent.
        calendar = self.authority.replay_section(ACN, "catalyst_calendar")
        self.assertEqual([item["status"] for item in calendar],
                         ["unavailable", "unavailable"])


class VariantViewPublishTests(unittest.TestCase):
    """The test that should have caught it.

    ``validate_variant_view``'s drafted branch dropped ``gaps`` from what it
    returned while its own closed-shape check required it. Nothing noticed,
    because every other test either published a dossier whose variant view was
    ``unavailable`` (that branch kept the field) or ran the validator without
    publishing. Between those two shapes lay the only path a real dossier
    takes: draft the variant view, then publish it -- and it could not, ever,
    for any company, with a message about hashes that named no field.

    So the assertions here are on the *shape being preserved*, not on the
    symptom: a validator that normalises a record is only safe if what it
    returns validates to itself.
    """

    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.authority = CompanyDossierAuthority(self.fixture.store)

    def test_a_dossier_with_a_drafted_variant_view_publishes(self):
        block = variant("claim-version:v")
        block["gaps"] = ["缺共识区间：没有卖方模型可以对照"]
        published = self.authority.publish(body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            variant_block=block))
        self.assertEqual(published["status"], "fresh")
        stored = self.authority.dossier(published["id"])
        self.assertEqual(stored["variant_view"]["status"], "drafted")
        self.assertEqual(stored["variant_view"]["gaps"], block["gaps"])
        self.assertEqual([slot["slot_id"] for slot in stored["variant_view"]["slots"]],
                         [slot for slot in VARIANT_SLOTS if slot != "market_view"])

    def test_a_variant_view_with_a_market_view_publishes_too(self):
        published = self.authority.publish(body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            variant_block=variant("claim-version:v", available=True)))
        self.assertEqual(published["status"], "fresh")
        stored = self.authority.dossier(published["id"])
        self.assertIn("market_view",
                      [slot["slot_id"] for slot in stored["variant_view"]["slots"]])

    def test_a_drafted_classification_publishes(self):
        published = self.authority.publish(body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            classification_block=classification("claim-version:c")))
        self.assertEqual(published["status"], "fresh")
        stored = self.authority.dossier(published["id"])
        self.assertEqual(stored["industry_classification"]["classification"],
                         "contract_compounder")

    def test_every_validator_returns_something_that_validates_to_itself(self):
        # The general form of the defect. A validator normalises a record and
        # the authority hashes what it returned; if the two disagree about the
        # field set, the hash of the body and the body's own hash differ, and
        # the error names a hash rather than the field that went missing.
        from dalton_core.company_dossier import (
            validate_classification, validate_section, validate_variant_view,
        )

        def section_of(value):
            return validate_section(value, "sections[0]")

        cases = [
            (section_of, drafted("business_model", "claim-version:a")),
            (section_of, unavailable("kpi_dictionary")),
            (validate_classification, classification("claim-version:c")),
            (validate_classification, classification()),
            (validate_variant_view, variant("claim-version:v")),
            (validate_variant_view, variant("claim-version:v", available=True)),
            (validate_variant_view, variant()),
        ]
        for validator, value in cases:
            once = validator(value)
            self.assertEqual(validator(once), once,
                             getattr(validator, "__name__", str(validator)))


class OutputRubricTests(unittest.TestCase):
    def test_a_criterion_the_policy_never_mentions_is_a_finding(self):
        record = body(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        extra = CRITERIA + ["Thesis status is 'insufficient' unless a ThesisVersion exists."]
        findings = output_rubric_findings(
            record, constitution=constitution(criteria=extra), policy=policy())
        self.assertEqual([item["code"] for item in findings],
                         ["unmapped_output_rubric_criterion"])

    def test_a_figure_the_cited_row_does_not_carry_is_a_finding(self):
        section = drafted("business_model", "claim-version:a",
                          "本季收入为 USD 7，同比增 12%。", source_text="收入为 USD 5")
        record = body(drafted_sections={"business_model": section})
        findings = output_rubric_findings(record, constitution=constitution(),
                                          policy=policy())
        self.assertTrue(findings)
        self.assertEqual({item["code"] for item in findings}, {"number_without_source"})

    def test_a_number_copied_verbatim_from_its_row_is_not(self):
        section = drafted("business_model", "claim-version:a",
                          "本季收入为 USD 5。", source_text="收入为 USD 5")
        record = body(drafted_sections={"business_model": section})
        self.assertEqual(
            output_rubric_findings(record, constitution=constitution(), policy=policy()),
            [])

    def test_the_variant_view_is_read_by_the_output_rubric_too(self):
        # 目标价 and 低估 live in our_view if they live anywhere, and a standard
        # applied to nine tenths of a document is not applied.
        criteria = CRITERIA + ["outputs never auto-generate investment conclusions"]
        mapped = validate_policy({
            **policy(),
            "output_rubric_bindings": policy()["output_rubric_bindings"] + [{
                "criterion_hash": content_hash(criteria[2]),
                "check": "no_investment_conclusion", "reason": ""}],
        })
        block = variant("claim-version:v")
        block["slots"][0]["sentences"][0]["text"] = "我们认为市场给的目标价太低。"
        record = body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            variant_block=block)
        findings = output_rubric_findings(
            record, constitution=constitution(criteria=criteria), policy=mapped)
        self.assertEqual([item["section"] for item in findings], ["variant_view"])

    def test_a_number_in_the_classification_is_traced_like_any_other(self):
        block = classification("claim-version:c")
        block["slots"][0]["sentences"][0]["text"] = "合同期限中位数为 7.5 年。"
        record = body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            classification_block=block)
        findings = output_rubric_findings(record, constitution=constitution(),
                                          policy=policy())
        self.assertEqual([item["section"] for item in findings],
                         ["industry_classification"])

    def test_an_investment_conclusion_is_a_finding(self):
        criteria = CRITERIA + ["outputs never auto-generate investment conclusions"]
        mapped = validate_policy({
            **policy(),
            "output_rubric_bindings": policy()["output_rubric_bindings"] + [{
                "criterion_hash": content_hash(criteria[2]),
                "check": "no_investment_conclusion", "reason": ""}],
        })
        section = drafted("business_model", "claim-version:a", "我们认为估值低估。")
        record = body(drafted_sections={"business_model": section})
        findings = output_rubric_findings(record, constitution=constitution(criteria=criteria),
                                          policy=mapped)
        self.assertEqual([item["code"] for item in findings], ["investment_conclusion"])


class QualityArtefactTests(unittest.TestCase):
    def test_the_dossier_scores_against_q1s_rubric_with_the_aspect_titles(self):
        from dalton_core.research_quality_rubrics import rubric
        from dalton_core.research_quality_score import run_deterministic

        record = body(drafted_sections={
            "business_model": drafted("business_model", "claim-version:a")})
        art = dossier_artefact({**record, "id": "company-dossier-draft:1",
                                "content_hash": "f" * 64, "dossier_ref": "d"})
        # The ten sections, and then the two blocks: the rubric grades the
        # whole document, and a version whose only new evidence is in the
        # classification has learned something.
        self.assertEqual([section["title"] for section in art["sections"]],
                         list(SECTIONS) + ["industry_classification", "variant_view"])
        result = run_deterministic(art, rubric("company_dossier"))
        # Nine unavailable sections carry neither body nor gap, which the
        # section-coverage check calls a shell; that is what they are, and the
        # honest reason is in ``reason``. The two hard checks pass.
        self.assertEqual(
            {name for name in result["failed_checks"]}
            & {"numbers_without_refs", "new_version_cites_new_refs"}, set())

    def test_evidence_scope_is_every_ref_the_version_rests_on(self):
        record = body(
            drafted_sections={"business_model": drafted("business_model", "claim-version:a")},
            classification_block=classification("claim-version:c"),
            variant_block=variant("claim-version:v"))
        self.assertEqual(evidence_scope(record),
                         ["claim-version:a", "claim-version:c", "claim-version:v"])


if __name__ == "__main__":
    unittest.main()
