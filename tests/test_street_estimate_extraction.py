"""P11b: reading page one of a broker note, and refusing to read most of them.

Every text below is written for this file. Real research notes are licensed
material and none of them is committed here; the layouts are the three the live
spool actually holds -- a labelled masthead, a revision block with the old
target above the new one, and a sentence that says the whole thing in nine
words -- reproduced closely enough to be the same parsing problem.
"""

from __future__ import annotations

import hashlib
import unittest

from dalton_core.street_estimate import BASIS, TARGET_METRIC, validate_estimate
from dalton_core.street_estimate_extraction import (
    OUTPUT_SCHEMA,
    PAGE_ONE_CHARS,
    StreetEstimateExtractionError,
    TASK_HASH,
    build_prompt,
    build_request,
    extract,
    find_broker,
    find_rating,
    find_targets,
    verify_estimate_table,
    worthy_spec,
)

ACN = "company:sec-cik:0001467373"

# A labelled masthead: the layout TD Securities and Wells Fargo use.
MASTHEAD = (
    "Computer Services & IT Consulting\n"
    "Accenture PLC\n"
    "TD SECURITIES (USA) LLC\n"
    "QUICK TAKE: COMPANY UPDATE\n"
    "September 2, 2026\n"
    "Price: $186.53 (09/01/2026)\n"
    "Price Target: $173.00 (Prior $151.00)\n"
    "HOLD (2)\n"
    "Model Update\n"
    "Our view: bookings were softer than we modelled and we take our estimates down.\n"
)
# A revision block: the old target printed above the new one, with the prose
# form of the same statement further down. This is the layout that silently
# yields the wrong number to a label-first match.
REVISION = (
    "Update\n"
    "Guidance cut highlights weakness in North America.\n"
    "EPAM Systems Inc\n"
    "From\nTo\nPrice Target\n$100.00\n$97.00\n"
    "Stock down; the second-half ramp comes out of the model. "
    "Remain EW, PT to $97.\n"
    "A Bank & Co. LLC\n"
)
# Nine words in the middle of a paragraph: the layout RBC uses.
PROSE = (
    "EQUITY RESEARCH\nAugust 19, 2026\nIBM\n"
    "Thoughts from our meetings with management\n"
    "Our view: the strategic outlook was unchanged and the near-term headwinds "
    "look like timing rather than demand. Maintain our OP rating and a price "
    "target of $270.\n"
)


QUOTE_CHARS = 1200


def quotes(text):
    """The same 1,200-character windows ``document_extraction`` hands out."""

    rows = []
    for start in range(0, max(len(text), 1), QUOTE_CHARS):
        chunk = text[start:start + QUOTE_CHARS]
        if not chunk:
            break
        rows.append({
            "quote_id": "quote:%d:%d:%s" % (
                start, start + len(chunk),
                hashlib.sha256(chunk.encode()).hexdigest()[:16]),
            "raw_text": chunk,
        })
    return rows


def context(text, **overrides):
    value = {
        "company_ref": ACN,
        "document_ref": "alphaengine-doc:1",
        "spec_ref": "sell-side-reports",
        "source_manifest_hash": "9" * 64,
        "published_on": "2026-09-02",
        "subject_names": ["Accenture", "ACN"],
        "sources": ["TD Securities (USA) LLC"],
        "analysts": ["A. Analyst"],
        "document_companies": ["Accenture PLC"],
        "quotes": quotes(text),
    }
    value.update(overrides)
    return value


class MastheadTests(unittest.TestCase):
    def test_the_labelled_layout_yields_the_new_target_not_the_prior_one(self):
        result = extract(context(MASTHEAD))
        self.assertIsNone(result["refusal"])
        estimate = result["estimate"]
        self.assertEqual(estimate["target_price"]["value"], "173.00")
        self.assertEqual(estimate["target_price"]["currency"], "USD")
        self.assertEqual(result["pattern"], "labelled")

    def test_the_figure_is_verified_against_the_quote_it_cites(self):
        estimate = extract(context(MASTHEAD))["estimate"]
        figure = estimate["figures"][0]
        self.assertEqual(figure["metric_ref"], TARGET_METRIC)
        self.assertEqual(figure["basis"], BASIS)
        self.assertEqual(figure["as_reported_label"], "Price Target")
        self.assertIn("Price Target: $173.00", figure["citation_text"])
        # And it survives the authority's own re-validation.
        validate_estimate(estimate)

    def test_the_rating_comes_out_of_the_houses_own_scale(self):
        estimate = extract(context(
            MASTHEAD, sources=["TD Securities (USA) LLC"]
        ))["estimate"]
        self.assertEqual(estimate["broker"], "td")
        self.assertEqual(estimate["rating"]["code"], "hold")
        self.assertEqual(estimate["rating"]["as_named"], "HOLD")
        self.assertEqual(estimate["rating"]["scale"], "buy-hold-sell")

    def test_the_house_comes_from_the_acquisition_metadata_when_it_can(self):
        estimate = extract(context(
            MASTHEAD, sources=["Wells Fargo Securities LLC"]
        ))["estimate"]
        self.assertEqual(estimate["broker"], "wells-fargo")
        self.assertEqual(estimate["broker_basis"], "document_metadata")

    def test_the_body_text_names_the_house_when_the_metadata_does_not(self):
        broker, named, basis = find_broker(
            [], quotes("TD SECURITIES (USA) LLC\nPrice Target: $11.00\n")
        )
        self.assertEqual((broker, basis), ("td", "page_text"))
        self.assertIsNotNone(named)


class RevisionTests(unittest.TestCase):
    def test_the_stranded_label_never_yields_the_superseded_target(self):
        # "Price Target" with both numbers on their own lines matches nothing,
        # because the label and the figure are not on one line. What is left is
        # the prose form, whose only label is "PT" -- so the note is refused.
        # The point of the test is the number that is *not* stored: $100.00 is
        # the target this house has just moved away from.
        result = extract(context(
            REVISION, subject_names=["EPAM"], document_companies=["EPAM Systems, Inc."],
            sources=["Morgan Stanley & Co. LLC"],
        ))
        self.assertEqual(result["refusal"], "label_does_not_name_a_line")
        self.assertIsNone(result["estimate"])

    def test_a_house_this_system_does_not_know_is_not_attributable(self):
        text = (
            "Accenture PLC\nA Bank Nobody Named LLP\n"
            "Price Target: $173.00\nBUY\n"
        )
        result = extract(context(text, sources=["A Bank Nobody Named LLP"]))
        self.assertEqual(result["refusal"], "broker_unknown")

    def test_two_live_targets_on_one_page_are_refused(self):
        # A page saying two things is not saying one, and a refusal is the only
        # answer that cannot be quietly wrong.
        text = (
            "Accenture PLC\nTD SECURITIES (USA) LLC\n"
            "Price Target: $173.00\nPrice Target: $210.00\n"
        )
        result = extract(context(text))
        self.assertEqual(result["refusal"], "ambiguous_target")
        self.assertEqual(result["values"], ["173.00", "210.00"])

    def test_the_masthead_wins_over_a_number_mentioned_in_the_prose(self):
        # A note that carries a labelled target and also discusses a bull case
        # is saying one thing in its masthead. Reading both would find a
        # disagreement that the page does not have.
        text = (
            "Accenture PLC\nTD SECURITIES (USA) LLC\n"
            "Price Target: $173.00\nHOLD (2)\n"
            "We also flag a bull-case price target of $210.00 on the shares.\n"
        )
        result = extract(context(text))
        self.assertIsNone(result["refusal"])
        self.assertEqual(result["estimate"]["target_price"]["value"], "173.00")

    def test_a_prior_target_is_marked_rather_than_counted(self):
        # Two labelled targets, the first introduced as the one being replaced.
        # Without the mark this page would look like it says two things and be
        # refused; with it, it says one.
        text = (
            "Accenture PLC\nTD SECURITIES (USA) LLC\n"
            "Prior\nPrice Target: $151.00\nPrice Target: $173.00\n"
        )
        rows = find_targets(quotes(text))
        self.assertEqual({row["value"] for row in rows}, {"151.00", "173.00"})
        live = [row for row in rows if not row["superseded"]]
        self.assertEqual({row["value"] for row in live}, {"173.00"})
        self.assertIsNone(extract(context(text))["refusal"])


class ProseTests(unittest.TestCase):
    def test_a_target_written_in_a_sentence_is_read(self):
        result = extract(context(
            PROSE, subject_names=["IBM"], document_companies=["IBM"],
            sources=["RBC Capital Markets"], published_on="2026-08-19",
        ))
        self.assertIsNone(result["refusal"])
        estimate = result["estimate"]
        self.assertEqual(estimate["target_price"]["value"], "270")
        self.assertEqual(estimate["broker"], "rbc")
        self.assertEqual(estimate["rating"]["code"], "buy")
        self.assertEqual(estimate["rating"]["as_named"], "OP")

    def test_a_bare_PT_is_refused_rather_than_relabelled(self):
        # The note wrote "$270 PT" and nothing longer. The verifier requires a
        # label that names a line; handing it "price target" instead would be
        # handing it words the document did not print.
        text = PROSE.replace("a price target of $270", "$270 PT")
        result = extract(context(
            text, subject_names=["IBM"], document_companies=["IBM"],
            sources=["RBC Capital Markets"],
        ))
        self.assertEqual(result["refusal"], "label_does_not_name_a_line")
        self.assertEqual(result["label"], "PT")

    def test_an_abbreviation_needs_a_cue_to_be_a_rating(self):
        # "UP" is Underperform at RBC and the most common two-letter string in
        # English. Without a rating word beside it, it is not a rating.
        self.assertIsNone(find_rating(
            quotes("Shares are UP 12% since the print and we see more to come."),
            broker="rbc",
        ))
        self.assertEqual(
            find_rating(quotes("We reiterate our UP rating."), broker="rbc")["code"],
            "sell",
        )

    def test_a_rating_word_the_house_cannot_say_is_not_its_rating(self):
        self.assertIsNone(find_rating(
            quotes("Peers are rated Overweight elsewhere on the street."),
            broker="rbc",
        ))


class RefusalTests(unittest.TestCase):
    def test_a_note_naming_several_companies_is_refused_whole(self):
        result = extract(context(
            MASTHEAD,
            document_companies=["Accenture PLC", "Cognizant Technology Solutions"],
        ))
        self.assertEqual(result["refusal"], "multi_company_report")
        self.assertEqual(result["company_count"], 2)

    def test_a_note_that_never_names_the_company_is_not_about_it(self):
        result = extract(context(MASTHEAD, subject_names=["Cognizant"]))
        self.assertEqual(result["refusal"], "subject_not_named")

    def test_a_document_of_another_kind_is_not_read(self):
        result = extract(context(MASTHEAD, spec_ref="earnings-call-transcripts"))
        self.assertEqual(result["refusal"], "not_sell_side")
        self.assertFalse(worthy_spec("earnings-call-transcripts"))

    def test_a_note_with_no_target_is_a_refusal_with_a_name(self):
        text = "Accenture PLC\nTD SECURITIES (USA) LLC\nOur view: bookings were soft.\n"
        result = extract(context(text, sources=["TD Cowen"]))
        self.assertEqual(result["refusal"], "no_target_price")

    def test_a_target_with_no_currency_symbol_is_not_a_price(self):
        text = (
            "Accenture PLC\nTD SECURITIES (USA) LLC\n"
            "Our price target moves to 173.00 on the revised model.\n"
        )
        result = extract(context(text, sources=["TD Cowen"]))
        self.assertEqual(result["refusal"], "no_target_price")

    def test_a_target_below_the_masthead_is_out_of_the_page_one_window(self):
        text = "filler. " * ((PAGE_ONE_CHARS // 8) + 400) + "Price Target: $173.00\n"
        result = extract(context(
            "Accenture PLC\nTD SECURITIES (USA) LLC\n" + text, sources=["TD Cowen"]
        ))
        self.assertEqual(result["refusal"], "no_target_price")

    def test_a_context_missing_a_field_is_a_programming_error_not_a_refusal(self):
        broken = context(MASTHEAD)
        broken.pop("quotes")
        with self.assertRaises(StreetEstimateExtractionError):
            extract(broken)


class HorizonTests(unittest.TestCase):
    def test_a_dated_target_keeps_the_date_the_house_underwrote(self):
        text = (
            "EPAM Systems Inc\nJ.P. Morgan\n"
            "Price Target (Dec-27):$120.00\nPrior (Dec-26):$142.00\n"
        )
        result = extract(context(
            text, subject_names=["EPAM"], document_companies=["EPAM Systems, Inc."],
            sources=["J.P. Morgan"],
        ))
        self.assertIsNone(result["refusal"])
        self.assertEqual(result["estimate"]["target_price"]["horizon"], "Dec-27")
        self.assertIn("Dec-27", result["estimate"]["figures"][0]["period"])

    def test_an_undated_target_is_dated_from_the_note(self):
        estimate = extract(context(MASTHEAD))["estimate"]
        self.assertIsNone(estimate["target_price"]["horizon"])
        self.assertEqual(estimate["figures"][0]["period"], "12 months from 2026-09-02")


class ModelSurfaceTests(unittest.TestCase):
    """The estimates table is the part a model is worth paying for."""

    # Deliberately not the aligned grid a real note prints. A row of
    # space-separated columns defeats ``document_numeric_claim.numbers_in``,
    # whose number pattern runs across the spaces and reads one row as a single
    # twenty-digit number -- which is a real finding about that table layout and
    # is written up in the report rather than papered over here.
    TABLE_QUOTE = (
        "Revenue ($M), fiscal year ending December\n"
        "FY2027E revenue of $20,200\n"
        "Prior estimate: $20,000\n"
    )

    def request(self):
        return build_request({
            "company_ref": ACN, "document_ref": "alphaengine-doc:1",
            "broker": "guggenheim",
            "quotes": [{"quote_id": "q", "raw_text": self.TABLE_QUOTE}],
        })

    def test_the_task_identity_is_frozen_with_its_schema(self):
        self.assertEqual(self.request()["task_hash"], TASK_HASH)
        self.assertIn("superseded", OUTPUT_SCHEMA["properties"]["estimates"]
                      ["items"]["properties"])

    def test_the_prompt_asks_for_the_brokers_own_numbers(self):
        prompt = build_prompt(self.request())
        self.assertIn("broker's own forward revenue", prompt)
        self.assertIn("never the company's reported results", prompt)
        self.assertIn("superseded=true", prompt)

    def test_a_superseded_row_is_recorded_as_such_and_not_verified_as_current(self):
        verified = verify_estimate_table({
            "schema_version": "0.1",
            "estimates": [
                {"quote_id": "q", "metric_ref": "metric:revenue-estimate",
                 "as_reported_label": "revenue", "period": "FY2027E",
                 "value": "20200", "unit": "currency", "currency": "USD",
                 "scale": "million", "superseded": False},
                {"quote_id": "q", "metric_ref": "metric:revenue-estimate",
                 "as_reported_label": "Prior estimate", "period": "FY2027E",
                 "value": "20000", "unit": "currency", "currency": "USD",
                 "scale": "million", "superseded": True},
            ],
        }, quotes={"q": self.TABLE_QUOTE}, subject_as_named="Accenture")
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["value"], "20200")

    def test_one_bad_row_refuses_the_whole_table(self):
        # A wrong column is every number real and every one filed against the
        # wrong year. That is evidence about the reading, not about the row.
        with self.assertRaises(StreetEstimateExtractionError) as caught:
            verify_estimate_table({
                "schema_version": "0.1",
                "estimates": [
                    {"quote_id": "q", "metric_ref": "metric:revenue-estimate",
                     "as_reported_label": "revenue", "period": "FY2027E",
                     "value": "20200", "unit": "currency", "currency": "USD",
                     "scale": "million", "superseded": False},
                    {"quote_id": "q", "metric_ref": "metric:revenue-estimate",
                     "as_reported_label": "revenue", "period": "FY2028E",
                     "value": "99999", "unit": "currency", "currency": "USD",
                     "scale": "million", "superseded": False},
                ],
            }, quotes={"q": self.TABLE_QUOTE}, subject_as_named="Accenture")
        self.assertIn("refused whole", str(caught.exception))

    def test_a_response_of_another_shape_is_refused(self):
        with self.assertRaises(StreetEstimateExtractionError):
            verify_estimate_table(
                {"schema_version": "0.2", "estimates": []},
                quotes={}, subject_as_named="Accenture",
            )


if __name__ == "__main__":
    unittest.main()
