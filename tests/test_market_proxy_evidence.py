"""W4: ``market_proxy`` as an evidence kind, and the ``proxy_gap`` it must carry.

Chem retrospective §7.2. A spread, a list price or a futures continuation is a
real public number and it is *not* the company's realised figure. Recording one
beside a filed cell without saying how far apart they are is how a model comes
to assert the company earned the spread.
"""

import unittest

from dalton_core.claim_index_authority import (
    EVIDENCE_KINDS,
    EVIDENCE_KIND_DEFINITIONS,
    IMPORTANCE_TIERS,
    MARKET_PROXY,
)
from dalton_core.model_forecast_driver import (
    MAX_PROXY_GAP_CHARS,
    PROXY_GAP_REQUIRED,
    REF_KINDS,
    ForecastModelValidationError,
    _normalize_ref,
    market_proxy_ref,
    proxy_gaps,
)

GAP = ("MDI--benzene is a chemical margin, not a plant cash margin: it excludes "
       "utilities, freight and the discount on contract volumes")


def ref(**kwargs):
    base = {"kind": "prior_period", "ref": "x", "concept": None,
            "period_end": None, "accession": None}
    base.update(kwargs)
    return base


class VocabularyTests(unittest.TestCase):
    def test_market_proxy_is_an_evidence_kind_with_a_definition(self):
        self.assertIn(MARKET_PROXY, EVIDENCE_KINDS)
        self.assertIn("distance", EVIDENCE_KIND_DEFINITIONS[MARKET_PROXY])

    def test_it_is_not_a_source_tier(self):
        # ``importance`` says who said it; the evidence kind says what it is.
        # Folding the two would make "a price agency published this" mean the
        # same thing as "the company filed this".
        self.assertNotIn(MARKET_PROXY, IMPORTANCE_TIERS)

    def test_a_forecast_assumption_may_cite_one(self):
        self.assertIn(MARKET_PROXY, REF_KINDS)
        self.assertEqual(PROXY_GAP_REQUIRED, frozenset({MARKET_PROXY}))


class ProxyGapTests(unittest.TestCase):
    def test_a_proxy_with_its_gap_is_accepted_and_keeps_the_sentence(self):
        wire = _normalize_ref(
            market_proxy_ref("proxy:mdi-benzene-spread", proxy_gap=GAP,
                             period_end="2026-06-30"),
            "refs[0]")
        self.assertEqual(wire["kind"], MARKET_PROXY)
        self.assertEqual(wire["proxy_gap"], GAP)
        self.assertEqual(wire["period_end"], "2026-06-30")

    def test_a_proxy_without_a_gap_is_refused_with_a_reason(self):
        with self.assertRaises(ForecastModelValidationError) as caught:
            _normalize_ref(ref(kind=MARKET_PROXY, ref="proxy:spread"), "refs[0]")
        self.assertIn("how far it sits", str(caught.exception))

    def test_an_empty_or_blank_gap_is_not_a_gap(self):
        for value in ("", "   ", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(ForecastModelValidationError):
                    _normalize_ref(
                        ref(kind=MARKET_PROXY, ref="proxy:spread", proxy_gap=value),
                        "refs[0]")

    def test_the_gap_is_stripped_and_bounded(self):
        wire = _normalize_ref(
            ref(kind=MARKET_PROXY, ref="proxy:spread", proxy_gap=f"  {GAP}  "),
            "refs[0]")
        self.assertEqual(wire["proxy_gap"], GAP)
        with self.assertRaises(ForecastModelValidationError) as caught:
            _normalize_ref(
                ref(kind=MARKET_PROXY, ref="proxy:spread",
                    proxy_gap="x" * (MAX_PROXY_GAP_CHARS + 1)),
                "refs[0]")
        self.assertIn("longer than", str(caught.exception))

    def test_a_proxy_must_name_the_series_it_cites(self):
        with self.assertRaises(ForecastModelValidationError) as caught:
            _normalize_ref(
                ref(kind=MARKET_PROXY, ref=None, proxy_gap=GAP), "refs[0]")
        self.assertIn("name the series", str(caught.exception))

    def test_the_refusal_is_whole_and_never_repaired(self):
        # No default sentence is written for a proxy that arrived without one:
        # the field carries the one judgement this module is not entitled to
        # make.
        with self.assertRaises(ForecastModelValidationError):
            _normalize_ref(ref(kind=MARKET_PROXY, ref="proxy:spread"), "refs[0]")

    def test_a_gap_on_anything_else_is_refused(self):
        with self.assertRaises(ForecastModelValidationError) as caught:
            _normalize_ref(ref(kind="claim", ref="claim-version:1", proxy_gap=GAP),
                           "refs[0]")
        self.assertIn("cannot carry a proxy_gap", str(caught.exception))

    def test_a_ref_that_is_not_a_proxy_keeps_the_bytes_it_always_had(self):
        # The field is absent rather than null on every other kind, so no model
        # already recorded gets a new content hash for a field it never used.
        self.assertNotIn("proxy_gap", _normalize_ref(ref(), "refs[0]"))

    def test_proxy_gaps_reads_the_sentences_off_an_assumption(self):
        assumption = {"refs": [
            ref(),
            {**market_proxy_ref("proxy:a", proxy_gap="one"), "kind": MARKET_PROXY},
            {**market_proxy_ref("proxy:b", proxy_gap="two"), "kind": MARKET_PROXY},
        ]}
        self.assertEqual(proxy_gaps(assumption), ["one", "two"])
        self.assertEqual(proxy_gaps({}), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
