"""P12b: twenty claims, and exactly what the rules are supposed to say about them.

The fixture is the point of this file. Live there are 2,170 Claims with an
empty aspect, no date, no importance and no grouping, and the blueprint's
acceptance test is a person checking twenty of them. So twenty are written down
here with the answer beside each one -- what section it belongs in, how much its
source is worth, what date it is about, and which of them are the same fact --
and the rules have to produce that table exactly.

Four of the twenty exist because of something that actually happened:

* three Accenture claims are one quarter's revenue growth arriving from three
  documents, which is the ACN Initial Screen complaint verbatim;
* one DXC personnel item is from 2023 and has to come out dated 2023, not
  dated "when we fetched it";
* one claim's subject is the industry rather than a company, so no model is
  asked what section of a company dossier it belongs in;
* one measure is not in the rule table at all, and has to come out ``other``
  rather than be guessed at.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.claim_aspect_vocabulary import (
    ASPECTS,
    DEFINITIONS,
    ClaimAspectError,
    require_aspect,
    vocabulary_table,
)
from dalton_core.claim_index_tagging import (
    ClaimIndexTaggingRefused,
    ProvenanceResolver,
    aspects_from_response,
    build_batch,
    build_prompt,
    dedupe_group_key,
    fold,
    period_as_of,
    quantitative_aspect,
    rule_tags,
)

ACN = "company:sec-cik:0001467373"
DXC = "company:sec-cik:001688568"
EPAM = "company:sec-cik:0001352010"
INDUSTRY = "industry:us-it-services"

FILING = {"importance": "filing", "importance_basis": "discovery_spec:annual-report-10k",
          "published_at": None, "retrieved_at": "2026-09-01T00:00:00+00:00"}
CALL = {"importance": "management_statement",
        "importance_basis": "discovery_spec:earnings-call-transcripts",
        "published_at": None, "retrieved_at": "2026-09-02T00:00:00+00:00"}
BROKER = {"importance": "sell_side",
          "importance_basis": "discovery_spec:sell-side-reports",
          "published_at": None, "retrieved_at": "2026-09-03T00:00:00+00:00"}
NEWS = {"importance": "news", "importance_basis": "discovery_spec:management-changes",
        "published_at": None, "retrieved_at": "2026-09-04T00:00:00+00:00"}
STALE_NEWS = {**NEWS, "published_at": "2023-06-14T09:00:00+00:00"}


def claim(**overrides):
    base = {
        "subject_ref": ACN, "metric_or_aspect": "demand environment",
        "period": "current", "claim_kind": "qualitative", "unit": None,
        "normalized_statement": "Demand is stable.",
    }
    base.update(overrides)
    return base


# (name, claim, provenance, expected aspect, importance, as_of, as_of_basis)
FIXTURE = [
    # -- filed numbers ----------------------------------------------------
    ("acn-revenue-q3", claim(
        metric_or_aspect="quarterly_revenue_yoy_growth", claim_kind="quantitative",
        unit="percent", period="2026-03-01..2026-05-31",
        normalized_statement="Accenture reported revenues for 2026-03-01..2026-05-31."),
     FILING, "segments_and_mix", "filing", "2026-05-31", "period_end"),
    ("acn-revenue-q3-again", claim(
        metric_or_aspect="quarterly_revenue_yoy_growth", claim_kind="quantitative",
        unit="percent", period="2026-03-01..2026-05-31",
        normalized_statement="Accenture reported revenues, per the press release."),
     CALL, "segments_and_mix", "management_statement", "2026-05-31", "period_end"),
    ("acn-revenue-q3-third-time", claim(
        metric_or_aspect="quarterly_revenue_yoy_growth", claim_kind="quantitative",
        unit="percent", period="2026-03-01..2026-05-31",
        normalized_statement="Accenture revenue growth, as written up by the broker."),
     BROKER, "segments_and_mix", "sell_side", "2026-05-31", "period_end"),
    ("acn-margin", claim(
        metric_or_aspect="operating margin", claim_kind="quantitative",
        unit="percent", period="FY2025",
        normalized_statement="Operating margin was 15.6 percent."),
     FILING, "supply_and_cost", "filing", "2025-12-31", "period_label"),
    ("acn-guidance", claim(
        metric_or_aspect="revenue guidance", claim_kind="quantitative",
        unit="percent", period="FY2026",
        normalized_statement="Guided to 5 to 7 percent growth."),
     CALL, "guidance_style", "management_statement", "2026-12-31", "period_label"),
    ("epam-buyback", claim(
        subject_ref=EPAM, metric_or_aspect="share repurchase", claim_kind="quantitative",
        unit="currency", period="Q2 2026",
        normalized_statement="Repurchased 100 million of stock."),
     FILING, "management_and_capital_allocation", "filing", "2026-06-30", "period_label"),
    ("epam-headcount", claim(
        subject_ref=EPAM, metric_or_aspect="billable headcount", claim_kind="quantitative",
        unit="count", period="2026-06-30",
        normalized_statement="Billable headcount was 55,000."),
     FILING, "supply_and_cost", "filing", "2026-06-30", "period_label"),
    ("epam-pipeline", claim(
        subject_ref=EPAM, metric_or_aspect="pipeline coverage", claim_kind="quantitative",
        unit="ratio", period="H1 2026",
        normalized_statement="Pipeline coverage was 2.1x."),
     BROKER, "demand_drivers", "sell_side", "2026-06-30", "period_label"),
    ("dxc-mystery-number", claim(
        subject_ref=DXC, metric_or_aspect="widget quotient", claim_kind="quantitative",
        unit="ratio", period="2026",
        normalized_statement="The widget quotient was 4."),
     FILING, "other", "filing", "2026-12-31", "period_label"),
    # -- prose the model has to file --------------------------------------
    ("acn-demand", claim(), CALL, None, "management_statement",
     "2026-09-02", "evidence_retrieved_at"),
    ("acn-competition", claim(
        metric_or_aspect="competitive positioning",
        normalized_statement="Wins against the Indian heritage vendors are up."),
     BROKER, None, "sell_side", "2026-09-03", "evidence_retrieved_at"),
    ("acn-ai", claim(
        metric_or_aspect="AI demand outlook", period="Q2 2026",
        normalized_statement="Generative AI bookings doubled year over year."),
     CALL, None, "management_statement", "2026-06-30", "period_label"),
    ("epam-attrition", claim(
        subject_ref=EPAM, metric_or_aspect="attrition",
        normalized_statement="Voluntary attrition remains below pre-pandemic levels."),
     CALL, None, "management_statement", "2026-09-02", "evidence_retrieved_at"),
    ("epam-note", claim(
        subject_ref=EPAM, metric_or_aspect="demand outlook",
        normalized_statement="The broker sees discretionary spend recovering."),
     BROKER, None, "sell_side", "2026-09-03", "evidence_retrieved_at"),
    ("dxc-personnel-2023", claim(
        subject_ref=DXC, metric_or_aspect="management changes",
        normalized_statement="DXC named a new chief delivery officer."),
     STALE_NEWS, None, "news", "2023-06-14", "document_published_at"),
    ("dxc-press", claim(
        subject_ref=DXC, metric_or_aspect="contract news",
        normalized_statement="DXC signed a renewal with a European bank."),
     NEWS, None, "news", "2026-09-04", "evidence_retrieved_at"),
    # Same subject, same sentence, from two documents: one fact.
    ("acn-echo-one", claim(
        metric_or_aspect="demand environment",
        normalized_statement="Clients are prioritising reinvention programmes."),
     CALL, None, "management_statement", "2026-09-02", "evidence_retrieved_at"),
    ("acn-echo-two", claim(
        metric_or_aspect="Demand environment",
        normalized_statement="Clients are  prioritising reinvention programmes. "),
     NEWS, None, "news", "2026-09-04", "evidence_retrieved_at"),
    # An industry claim: the dossier has no section for it and no model is asked.
    ("industry-supply", claim(
        subject_ref=INDUSTRY, metric_or_aspect="offshore capacity",
        normalized_statement="Offshore delivery capacity grew 6 percent."),
     BROKER, "industry", "sell_side", "2026-09-03", "evidence_retrieved_at"),
    ("no-provenance", claim(
        subject_ref=DXC, metric_or_aspect="something",
        normalized_statement="A claim with no evidence relation at all."),
     {"importance": "other", "importance_basis": "no evidence relation",
      "published_at": None, "retrieved_at": None},
     None, "other", None, "unknown"),
]


class AspectVocabularyTests(unittest.TestCase):
    def test_the_words_are_the_dossier_sections_plus_industry_and_other(self):
        # P12a groups a company's file by this list, so a word here that is not
        # a dossier section is a section nobody will ever assemble.
        self.assertEqual(ASPECTS[:10], (
            "business_model", "segments_and_mix", "demand_drivers",
            "supply_and_cost", "competitive_position",
            "management_and_capital_allocation", "guidance_style",
            "kpi_dictionary", "catalyst_calendar", "history_of_price_drivers",
        ))
        self.assertEqual(ASPECTS[10:], ("industry", "other"))

    def test_every_word_carries_the_line_the_prompt_shows(self):
        self.assertEqual(set(DEFINITIONS), set(ASPECTS))
        table = vocabulary_table().splitlines()
        self.assertEqual(len(table), len(ASPECTS))
        for line, word in zip(table, ASPECTS):
            self.assertEqual(line.split("\t")[0], word)
            self.assertEqual(line.split("\t")[1], DEFINITIONS[word])

    def test_a_word_outside_the_list_is_refused_by_name(self):
        with self.assertRaises(ClaimAspectError) as caught:
            require_aspect("moat")
        self.assertIn("business_model", str(caught.exception))


class PeriodTests(unittest.TestCase):
    def test_the_shapes_a_period_actually_takes_here(self):
        self.assertEqual(period_as_of("2026-03-01..2026-05-31"),
                         ("2026-05-31", "period_end"))
        self.assertEqual(period_as_of("Q2 2026"), ("2026-06-30", "period_label"))
        self.assertEqual(period_as_of("2Q26"), ("2026-06-30", "period_label"))
        self.assertEqual(period_as_of("fiscal 2025"), ("2025-12-31", "period_label"))
        self.assertEqual(period_as_of("2026-06"), ("2026-06-30", "period_label"))
        self.assertEqual(period_as_of({
            "kind": "fiscal_quarter", "label": "FY2026Q3",
            "start": "2026-03-01T00:00:00+00:00", "end": "2026-05-31T23:59:59+00:00",
        }), ("2026-05-31", "period_end"))

    def test_a_period_that_places_a_figure_nowhere_gets_no_date(self):
        # Live, 309 claims say "current" and 107 say "not specified". Inventing
        # a date for them would make every one of them look like today's news.
        for label in ("current", "not specified", "ongoing", "当前",
                      "current fiscal year", "revenue in 2025 and 2026"):
            self.assertEqual(period_as_of(label), (None, "unknown"), label)


class RuleTaggingTests(unittest.TestCase):
    def tags(self, name):
        for item in FIXTURE:
            if item[0] == name:
                return rule_tags(item[1], item[2])
        raise AssertionError(name)

    def test_every_fixture_claim_gets_exactly_the_expected_tags(self):
        for name, body, provenance, aspect, importance, as_of, basis in FIXTURE:
            with self.subTest(name):
                tags = rule_tags(body, provenance)
                self.assertEqual(tags["aspect"], aspect)
                self.assertEqual(tags["importance"], importance)
                self.assertEqual(tags["as_of"], as_of)
                self.assertEqual(tags["as_of_basis"], basis)

    def test_a_quantitative_aspect_comes_from_the_measure_and_never_a_model(self):
        for name in ("acn-revenue-q3", "acn-margin", "acn-guidance",
                     "epam-buyback", "epam-headcount", "epam-pipeline"):
            self.assertEqual(self.tags(name)["aspect_source"], "rule", name)

    def test_a_measure_the_rules_do_not_know_is_other_rather_than_guessed(self):
        self.assertEqual(quantitative_aspect("widget quotient"), "other")
        self.assertEqual(self.tags("dxc-mystery-number")["aspect"], "other")

    def test_an_industry_claim_is_filed_as_industry_without_asking(self):
        tags = self.tags("industry-supply")
        self.assertEqual((tags["aspect"], tags["aspect_source"]), ("industry", "rule"))

    def test_only_qualitative_company_prose_is_left_for_a_model(self):
        left = [name for name, body, prov, aspect, *_ in FIXTURE if aspect is None]
        self.assertEqual(len(left), 10)
        for name in left:
            self.assertIsNone(self.tags(name)["aspect_source"])

    def test_the_2023_personnel_item_is_dated_2023_not_dated_today(self):
        # The whole reason as_of exists: this was fetched in 2026 and a lane
        # that dated it by retrieval would put a three-year-old personnel note
        # at the top of DXC's file.
        tags = self.tags("dxc-personnel-2023")
        self.assertEqual((tags["as_of"], tags["as_of_basis"]),
                         ("2023-06-14", "document_published_at"))

    def test_the_rule_tagger_hash_moves_when_a_rule_moves(self):
        from dalton_core.claim_index_tagging import RULE_TAGGER_HASH
        from dalton_core.store import content_hash

        self.assertNotEqual(RULE_TAGGER_HASH, content_hash({"tagger": "rule:other"}))
        self.assertEqual(len(RULE_TAGGER_HASH), 64)


class DedupeTests(unittest.TestCase):
    def key(self, name):
        for item in FIXTURE:
            if item[0] == name:
                return dedupe_group_key(item[1], as_of=item[5])
        raise AssertionError(name)

    def test_one_quarters_revenue_from_three_documents_is_one_group(self):
        keys = {self.key(name) for name in
                ("acn-revenue-q3", "acn-revenue-q3-again", "acn-revenue-q3-third-time")}
        self.assertEqual(len(keys), 1)

    def test_a_different_quarter_is_a_different_group(self):
        other = dedupe_group_key(claim(
            metric_or_aspect="quarterly_revenue_yoy_growth", claim_kind="quantitative",
            unit="percent", period="2025-12-01..2026-02-28",
        ), as_of="2026-02-28")
        self.assertNotEqual(other, self.key("acn-revenue-q3"))

    def test_a_different_unit_is_a_different_group(self):
        other = dedupe_group_key(claim(
            metric_or_aspect="quarterly_revenue_yoy_growth", claim_kind="quantitative",
            unit="currency", period="2026-03-01..2026-05-31",
        ), as_of="2026-05-31")
        self.assertNotEqual(other, self.key("acn-revenue-q3"))

    def test_the_same_sentence_typed_twice_is_one_group(self):
        self.assertEqual(self.key("acn-echo-one"), self.key("acn-echo-two"))

    def test_the_same_sentence_about_another_company_is_not(self):
        mine = dedupe_group_key(claim(
            subject_ref=EPAM,
            normalized_statement="Clients are prioritising reinvention programmes."),
            as_of=None)
        self.assertNotEqual(mine, self.key("acn-echo-one"))

    def test_two_different_sentences_stay_two_groups(self):
        # No similarity model: "demand is stable" and "demand is steady" are
        # two claims here, and that is the conservative error on purpose.
        self.assertNotEqual(
            dedupe_group_key(claim(normalized_statement="Demand is stable."), as_of=None),
            dedupe_group_key(claim(normalized_statement="Demand is steady."), as_of=None),
        )

    def test_folding_is_case_and_whitespace_and_nothing_else(self):
        self.assertEqual(fold(" Demand   Environment "), "demand environment")
        self.assertEqual(fold("2026-03-01..2026-05-31"), "2026-03-01..2026-05-31")


class ProvenanceResolverTests(unittest.TestCase):
    """The chain from a claim to the discovery spec that found its document."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE evidence_versions(evidence_version_id TEXT PRIMARY KEY,
                evidence_json TEXT NOT NULL);
            CREATE TABLE evidence_relations(claim_version_id TEXT,
                evidence_version_id TEXT);
            CREATE TABLE transcript_correction_set_versions(version_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL);
            CREATE TABLE coverage_mission_discovered_documents(document_ref TEXT,
                discovery_ref TEXT);
            CREATE TABLE coverage_mission_source_discoveries(record_id TEXT PRIMARY KEY,
                spec_ref TEXT);
        """)

    def add(self, claim_version_id, *, source_type, document_ref=None,
            correction_ref="correction:1", retrieved_at="2026-09-05T00:00:00+00:00"):
        lineage = ["source:alphaengine", "envelope:1", "artifact:1"]
        if document_ref is not None:
            lineage.append(correction_ref)
            self.connection.execute(
                "INSERT OR REPLACE INTO transcript_correction_set_versions VALUES(?,?)",
                (correction_ref, json.dumps({"document_ref": document_ref})),
            )
        evidence_id = f"evidence:{claim_version_id}"
        self.connection.execute(
            "INSERT INTO evidence_versions VALUES(?,?)",
            (evidence_id, json.dumps({
                "source_type": source_type, "source_lineage": lineage,
                "retrieved_at": retrieved_at, "source_envelope_ref": "envelope:1",
            })),
        )
        self.connection.execute(
            "INSERT INTO evidence_relations VALUES(?,?)",
            (claim_version_id, evidence_id),
        )

    def discovery(self, document_ref, spec_ref, discovery_ref="discovery:1"):
        self.connection.execute(
            "INSERT OR REPLACE INTO coverage_mission_source_discoveries VALUES(?,?)",
            (discovery_ref, spec_ref),
        )
        self.connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?)",
            (document_ref, discovery_ref),
        )

    def test_the_discovery_spec_decides_and_two_alphaengine_docs_differ(self):
        # Both arrive through the same connector as authenticated_transcript.
        # Only the spec says one is management speaking and one is a broker.
        self.discovery("alphaengine-doc:1", "earnings-call-transcripts", "discovery:call")
        self.discovery("alphaengine-doc:2", "sell-side-reports", "discovery:note")
        self.add("claim:call", source_type="authenticated_transcript",
                 document_ref="alphaengine-doc:1", correction_ref="correction:call")
        self.add("claim:note", source_type="authenticated_transcript",
                 document_ref="alphaengine-doc:2", correction_ref="correction:note")
        resolver = ProvenanceResolver(self.connection)
        self.assertEqual(resolver.resolve("claim:call")["importance"],
                         "management_statement")
        self.assertEqual(resolver.resolve("claim:note")["importance"], "sell_side")

    def test_a_web_page_is_matched_on_the_url_digest_the_two_refs_share(self):
        digest = "a" * 64
        self.discovery(f"public-web-url:sha256:{digest}", "industry-demand")
        self.add("claim:web", source_type="public_web",
                 document_ref=f"public-web-document:url-sha256:{digest}:body-sha256:{'b' * 64}")
        resolver = ProvenanceResolver(self.connection)
        resolved = resolver.resolve("claim:web")
        self.assertEqual(resolved["importance"], "news")
        self.assertEqual(resolved["importance_basis"], "discovery_spec:industry-demand")

    def test_without_a_discovery_the_source_type_answers_and_says_so(self):
        self.add("claim:filing", source_type="official_filing")
        resolver = ProvenanceResolver(self.connection)
        resolved = resolver.resolve("claim:filing")
        self.assertEqual(resolved["importance"], "filing")
        self.assertEqual(resolved["importance_basis"],
                         "evidence_source_type:official_filing")

    def test_a_claim_with_no_evidence_at_all_is_other(self):
        resolver = ProvenanceResolver(self.connection)
        self.assertEqual(resolver.resolve("claim:orphan"), {
            "importance": "other", "importance_basis": "no evidence relation",
            "document_ref": None, "spec_ref": None,
            "published_at": None, "retrieved_at": None,
        })

    def test_two_documents_behind_one_claim_take_the_stronger(self):
        self.discovery("alphaengine-doc:1", "earnings-call-transcripts", "discovery:call")
        self.add("claim:both", source_type="public_web")
        self.connection.execute(
            "INSERT INTO evidence_versions VALUES(?,?)",
            ("evidence:second", json.dumps({
                "source_type": "authenticated_transcript",
                "source_lineage": ["source:alphaengine", "correction:call"],
                "retrieved_at": "2026-09-06T00:00:00+00:00",
                "source_envelope_ref": "envelope:1",
            })),
        )
        self.connection.execute(
            "INSERT INTO transcript_correction_set_versions VALUES(?,?)",
            ("correction:call", json.dumps(
                {"document_ref": "alphaengine-doc:1"})),
        )
        self.connection.execute(
            "INSERT INTO evidence_relations VALUES(?,?)", ("claim:both", "evidence:second"))
        resolver = ProvenanceResolver(self.connection)
        self.assertEqual(resolver.resolve("claim:both")["importance"],
                         "management_statement")


class ModelTaggingTests(unittest.TestCase):
    def rows(self, count=3):
        return [
            {"claim_version_ref": f"claim-version:{index}",
             "subject_ref": ACN, "metric_or_aspect": "demand environment",
             "period": "current",
             "normalized_statement": f"Statement number {index}."}
            for index in range(1, count + 1)
        ]

    def test_the_batch_is_bounded_by_count_and_by_prompt_size(self):
        batch = build_batch(self.rows(60), max_claims=5)
        self.assertEqual(len(batch), 5)
        self.assertEqual([item["row_id"] for item in batch], ["1", "2", "3", "4", "5"])
        tiny = build_batch(self.rows(60), max_prompt_bytes=1)
        self.assertEqual(len(tiny), 1)

    def test_the_prompt_carries_the_vocabulary_and_a_row_per_claim(self):
        batch = self.rows(3)
        prompt = build_prompt(build_batch(batch))
        for word in ASPECTS:
            self.assertIn(word, prompt)
        self.assertIn("Statement number 3.", prompt)
        # A table, not JSON: the same content as objects is several times the
        # bytes and the router reserves against prompt size.
        self.assertNotIn('{"claim_version_ref"', prompt)

    def test_a_canned_table_assigns_every_row_by_claim_version(self):
        batch = build_batch(self.rows(3))
        assigned = aspects_from_response(
            batch, "1\tdemand_drivers\n2\tcompetitive_position\n3\tother\n")
        self.assertEqual(assigned, {
            "claim-version:1": "demand_drivers",
            "claim-version:2": "competitive_position",
            "claim-version:3": "other",
        })

    def test_a_fenced_reply_is_still_read(self):
        batch = build_batch(self.rows(1))
        self.assertEqual(
            aspects_from_response(batch, "```\n1\tguidance_style\n```"),
            {"claim-version:1": "guidance_style"},
        )

    def test_a_word_outside_the_vocabulary_refuses_the_whole_batch(self):
        batch = build_batch(self.rows(3))
        with self.assertRaises(ClaimIndexTaggingRefused) as caught:
            aspects_from_response(
                batch, "1\tdemand_drivers\n2\tmoat_and_pricing\n3\tother\n")
        self.assertIn("moat_and_pricing", str(caught.exception))

    def test_a_row_that_was_never_shown_refuses_the_whole_batch(self):
        # Not "drop row 9 and keep the rest": a reply naming a row that does
        # not exist was not produced from the table, so nothing in it is kept.
        batch = build_batch(self.rows(2))
        with self.assertRaises(ClaimIndexTaggingRefused):
            aspects_from_response(batch, "1\tdemand_drivers\n9\tother\n")

    def test_a_row_left_unanswered_refuses_the_batch(self):
        batch = build_batch(self.rows(3))
        with self.assertRaises(ClaimIndexTaggingRefused) as caught:
            aspects_from_response(batch, "1\tdemand_drivers\n2\tother\n")
        self.assertIn("3", str(caught.exception))

    def test_the_same_row_answered_twice_refuses_the_batch(self):
        batch = build_batch(self.rows(2))
        with self.assertRaises(ClaimIndexTaggingRefused):
            aspects_from_response(
                batch, "1\tdemand_drivers\n1\tother\n2\tother\n")

    def test_prose_around_the_table_refuses_it(self):
        batch = build_batch(self.rows(1))
        with self.assertRaises(ClaimIndexTaggingRefused):
            aspects_from_response(
                batch, "Here is my answer:\n1\tdemand_drivers\n")

    def test_an_empty_reply_is_a_refusal_not_an_empty_answer(self):
        batch = build_batch(self.rows(1))
        with self.assertRaises(ClaimIndexTaggingRefused):
            aspects_from_response(batch, "   ")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
