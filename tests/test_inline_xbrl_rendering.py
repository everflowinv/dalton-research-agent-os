"""2026-10-09: an inline-XBRL filing renders as the document a reader sees.

Live, two directed reads of EPAM's 10-K for its segment and vertical revenue
returned "只有 XBRL 元素名称、没有数值": the generic page renderer kept the
hidden ``ix:header`` (contexts, units, hidden facts -- 429 ``us-gaap:`` and 375
``epam:`` names in the first block) and split every table cell into its own
block.  The fixture below has the shape of that filing, anonymised and cut
down: a hidden header naming the segment axis and members, then the vertical
revenue table as EPAM lays it out (``$`` and ``%`` in their own cells,
self-closing spacer cells, ``ix:nonFraction`` values).
"""

from __future__ import annotations

import hashlib
import unittest

from dalton_core.public_web_extraction_source import (
    INLINE_XBRL_RENDERER,
    is_inline_xbrl,
    render_public_web_text,
    render_public_web_text_candidates,
    renderer_is_legacy_for_inline_xbrl,
)

CELL = '<td style="padding:2px"><span>{}</span></td>'
SPACER = '<td colspan="3" style="padding:0 1pt"/>'


def _row(label: str, *cells: str) -> str:
    return "<tr>" + CELL.format(label) + "".join(
        SPACER if cell == "" else CELL.format(cell) for cell in cells) + "</tr>"


FILING = (
    '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><head><title>issuer-20251231</title></head>'
    '<body><div style="display:none"><ix:header><ix:hidden>'
    '<ix:nonNumeric contextRef="c-1" name="dei:EntityCentralIndexKey">0009999999</ix:nonNumeric>'
    '<ix:nonNumeric contextRef="c-1" name="dei:DocumentFiscalPeriodFocus">FY</ix:nonNumeric>'
    '</ix:hidden><ix:resources>'
    '<xbrli:context id="c-9"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0009999999</xbrli:identifier>'
    '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
    'issuer:AmericasSegmentMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>'
    '<xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>'
    '</xbrli:period></xbrli:context>'
    '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
    '</ix:resources></ix:header></div>'
    '<div><span>FORM 10-K</span></div>'
    '<div><span>The following table presents our revenues by vertical for the periods indicated:</span></div>'
    '<table><tr><td colspan="3"/><td><span>2025</span></td><td/><td><span>2024</span></td></tr>'
    + _row("Financial Services", "$",
           '<ix:nonFraction unitRef="usd" contextRef="c-9" decimals="-3" '
           'name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" scale="3">1,316,487</ix:nonFraction>&#160;',
           "", "24.1&#160;", "%", "", "$", "1,022,617&#160;", "", "21.6&#160;", "%")
    + _row("Americas segment", "$", "(1,234", ")", "", "4.0", "%")
    + '</table><p>Revenues by Business Segment are discussed below.</p></body></html>'
).encode("utf-8")

PAGE = b"<html><body><p>Plain page</p><table><tr><td>A</td><td>1</td></tr></table></body></html>"


class InlineXbrlRenderingTests(unittest.TestCase):
    def test_the_hidden_header_is_not_text(self) -> None:
        rendered = render_public_web_text(FILING, raw_media_type="text/html")
        self.assertEqual(rendered["renderer"], INLINE_XBRL_RENDERER)
        self.assertNotIn("us-gaap:", rendered["text"])
        self.assertNotIn("AmericasSegmentMember", rendered["text"])
        self.assertNotIn("0009999999", rendered["text"])
        self.assertIn("FORM 10-K", rendered["text"])

    def test_a_table_row_is_one_line_with_its_values(self) -> None:
        text = render_public_web_text(FILING, raw_media_type="text/html")["text"]
        self.assertIn("Financial Services | $1,316,487 | 24.1% | $1,022,617 | 21.6%", text)
        self.assertIn("Americas segment | $(1,234) | 4.0%", text)
        self.assertIn("2025 | 2024", text)
        self.assertIn("Revenues by Business Segment are discussed below.", text)

    def test_the_legacy_rendering_is_still_reproducible(self) -> None:
        legacy = render_public_web_text(FILING, raw_media_type="text/html", legacy=True)
        self.assertEqual(legacy["renderer"], "html-visible-blocks:0.1")
        # Exactly what was hashed before: the header text first, cells apart.
        self.assertIn("issuer:AmericasSegmentMember", legacy["text"])
        self.assertIn("Financial Services\n\n$\n\n1,316,487", legacy["text"])
        candidates = render_public_web_text_candidates(FILING, raw_media_type="text/html")
        self.assertEqual([item["renderer"] for item in candidates],
                         [INLINE_XBRL_RENDERER, "html-visible-blocks:0.1"])
        digest = hashlib.sha256(legacy["text"].encode("utf-8")).hexdigest()
        self.assertIn(digest, {hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
                               for item in candidates})

    def test_an_ordinary_page_renders_exactly_as_before(self) -> None:
        self.assertFalse(is_inline_xbrl(PAGE.decode()))
        current = render_public_web_text(PAGE, raw_media_type="text/html")
        legacy = render_public_web_text(PAGE, raw_media_type="text/html", legacy=True)
        self.assertEqual(current, legacy)
        self.assertEqual(current["text"], "Plain page\n\nA\n\n1")
        self.assertEqual(len(render_public_web_text_candidates(PAGE, raw_media_type="text/html")), 1)

    def test_which_stored_renderers_need_the_legacy_rendering(self) -> None:
        self.assertTrue(renderer_is_legacy_for_inline_xbrl("html-visible-blocks:0.1"))
        self.assertTrue(renderer_is_legacy_for_inline_xbrl("gzip:0.1|html-visible-blocks:0.1"))
        self.assertFalse(renderer_is_legacy_for_inline_xbrl(INLINE_XBRL_RENDERER))
        self.assertFalse(renderer_is_legacy_for_inline_xbrl(None))


    def test_a_directed_read_registered_before_the_change_re_reads_legacy(self) -> None:
        from dalton_core.document_research import _registered_under_legacy_rendering

        def registration(renderer):
            return {"normalized_text": {"renderer_ref": renderer}}

        self.assertTrue(_registered_under_legacy_rendering(
            registration("html-visible-blocks:0.1"), registration(INLINE_XBRL_RENDERER)))
        self.assertFalse(_registered_under_legacy_rendering(
            registration(INLINE_XBRL_RENDERER), registration(INLINE_XBRL_RENDERER)))
        self.assertFalse(_registered_under_legacy_rendering(
            registration("alphaengine-paged-text:0.1"), registration(INLINE_XBRL_RENDERER)))

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
