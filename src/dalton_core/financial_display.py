"""One server-side reading of financial figures and filed concepts for people.

2026-10-08 inspection (DEFECT-04/05): the cockpit's 产物 / 证据 / 结论 views
and the HTML export printed filed amounts as bare integers
(``USD 200606000000``, ``17727871000``, ``USD 716924 million``) and named
SEC concepts by their XBRL id (``us-gaap:RevenueFromContractWith...``).

Everything here is display only: it is applied to a copy, *after* any
localization lookup (whose receipts hash the stored text), and the stored
text is kept beside it for 技术详情.  The amount rules are batch v's
(``numeric_display``: 亿美元 from 1 亿 up, else 万美元; about four
significant digits), so every surface reads one scale.

* ``financial_display_text`` -- free prose: base-USD and scaled-USD amounts
  in Chinese units on Chinese lines, thousands separators on English lines
  and on bare long integers, XBRL ids replaced by Chinese account names,
  ``accession`` by 申报编号, ISO date ranges in Chinese.
* ``filing_figure_display`` -- the two generated filed-figure sentences
  (English statement-line source, Chinese XBRL-row claim) as one short
  Chinese line, or ``None``.
* ``source_display_label`` -- one cited source's label for 来源.
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping

from .numeric_display import (
    _DECIMAL_TOKEN, _HAN, _format_unquoted_usd, format_display_number,
    format_prose_date_ranges, transform_unquoted_prose,
)

_SCALES = {"thousand": Decimal(1000), "million": Decimal(1000000),
           "billion": Decimal(1000000000), "trillion": Decimal(1000000000000)}
_SCALED_USD = re.compile(
    rf"(?i)\bUSD\s+(?P<number>{_DECIMAL_TOKEN})\s+(?P<scale>thousand|million|billion|trillion)\b"
    r"(?:\s*\((?:currency|usd)\))?")
_XBRL_ID = re.compile(r"(?<![\w:])(?P<concept>(?:us-gaap|dei|ifrs-full|srt):[A-Z][A-Za-z0-9]+)")
_XBRL_ROW = re.compile(
    r"XBRL 行 (?P<concept>(?:us-gaap|dei|ifrs-full|srt):[A-Z][A-Za-z0-9]+)")
_XBRL_PAREN = re.compile(
    r"\s*[（(](?:us-gaap|dei|ifrs-full|srt):[A-Z][A-Za-z0-9]+[)）]")
# A bare integer of seven or more digits that is not part of an identifier,
# a filing number (0001018724-26-000026), a date or a decimal.
_BARE_LONG = re.compile(r"(?<![\w.,:/#\-])(?P<number>-?\d{7,})(?![\w\-]|[.,]\d)")
_EN_USD = re.compile(
    rf"(?i)(?P<prefix>\bUSD\s+)(?P<a>{_DECIMAL_TOKEN})(?![\d,.]*\d)"
    rf"|(?<![\w.,])(?P<b>{_DECIMAL_TOKEN})(?P<suffix>\s*USD\b)")


def concept_label(concept: Any) -> str | None:
    """A filed concept's Chinese account name, or ``None``."""

    from .cockpit_model_display import concept_account_label

    return concept_account_label(concept)


def _grouped(number: str) -> str:
    text = number.replace(",", "")
    sign = "-" if text.startswith("-") else ""
    whole, _, fraction = text.lstrip("+-").partition(".")
    grouped = f"{int(whole):,}"
    return sign + grouped + (f".{fraction}" if fraction else "")


def _scaled_usd(match: re.Match[str]) -> str:
    number = Decimal(match.group("number").replace(",", ""))
    return format_display_number(number * _SCALES[match.group("scale").lower()],
                                 kind="amount_usd")


def _concept_words(match: re.Match[str]) -> str:
    return concept_label(match.group("concept")) or "对应申报科目"


def _chinese_part(text: str) -> str:
    text = _SCALED_USD.sub(_scaled_usd, text)
    text = _format_unquoted_usd(text)
    text = _XBRL_ROW.sub(lambda m: f"XBRL 科目「{_concept_words(m)}」", text)
    text = _XBRL_PAREN.sub("", text)
    text = _XBRL_ID.sub(_concept_words, text)
    text = re.sub(r"accession (?=\d{10}-\d{2}-\d{6})", "申报编号 ", text)
    return _BARE_LONG.sub(lambda m: _grouped(m.group("number")), text)


def _english_part(text: str) -> str:
    def scaled(match: re.Match[str]) -> str:
        return f"USD {_grouped(match.group('number'))} {match.group('scale').lower()}"

    text = _SCALED_USD.sub(scaled, text)
    text = _XBRL_PAREN.sub("", text)

    def usd(match: re.Match[str]) -> str:
        if match.group("a") is not None:
            return match.group("prefix") + _grouped(match.group("a"))
        return _grouped(match.group("b")) + match.group("suffix")

    text = _EN_USD.sub(usd, text)
    # Batch ah (financial review 2.3): no raw ``us-gaap:`` id on an English
    # line either -- the concept in words (its Chinese name when registered).
    text = _XBRL_ID.sub(lambda m: concept_label(m["concept"]) or humanized_concept(m["concept"]) or m["concept"], text)
    return _BARE_LONG.sub(lambda m: _grouped(m.group("number")), text)


def financial_display_text(value: Any) -> str:
    """Amounts, concept ids and date ranges in ``value`` made readable.

    Quoted text (Markdown quotes, ``"…"``, ``“…”``) is left byte-for-byte as
    written; it may be a source's own words.
    """

    text = "" if value is None else str(value)
    if not text:
        return text
    lines = []
    for line in text.splitlines(keepends=True):
        if _HAN.search(line):
            line = format_prose_date_ranges(transform_unquoted_prose(line, _chinese_part))
        else:
            line = transform_unquoted_prose(line, _english_part)
        lines.append(line)
    return "".join(lines)


def _period_text(start: str, end: str) -> str:
    from .cockpit_plane import claim_period_display_label

    return claim_period_display_label(f"{start}..{end}") or f"{start} 至 {end}"


def _amount(value: str, currency: str | None, unit: str | None = None) -> str:
    from .numeric_display import format_typed_value

    unit_name = (unit or "").lower()
    if (currency or "").upper() == "USD" or unit_name == "usd":
        return format_typed_value(value, unit="usd", scale="one", currency="USD")
    if unit_name == "shares":
        from .return_attribution import share_count_text

        return share_count_text(value) or value
    shown = _grouped(value) if re.fullmatch(r"-?\d+(?:\.\d+)?", value) else value
    return " ".join(part for part in (shown, currency or unit) if part)


_EN_FIGURE = re.compile(
    r"^(?P<label>.+?) \((?P<concept>(?:us-gaap|dei|ifrs-full|srt):[A-Za-z0-9]+)\) for "
    r"(?P<start>\d{4}-\d{2}-\d{2})\.\.(?P<end>\d{4}-\d{2}-\d{2}) was "
    r"(?P<value>-?\d+(?:\.\d+)?) (?P<unit>[A-Za-z/]+), as filed in (?P<accession>[\d-]+)$")
_CN_FIGURE = re.compile(
    r"^(?P<filer>.+?)在(?P<form>[0-9A-Za-z/\-]+)（accession (?P<accession>[\d-]+)）中列报的"
    r"(?P<metric>[^，（]+)(?:（分部：(?P<segment>[^）]+)）)?，期间 "
    r"(?P<start>\d{4}-\d{2}-\d{2})\.\.(?P<end>\d{4}-\d{2}-\d{2})，为 "
    r"(?P<currency>[A-Z]{3}) (?P<value>-?\d+(?:\.\d+)?)。该数字直接取自申报的 XBRL 行 "
    r"(?P<concept>[\w-]+:[A-Za-z0-9]+)（[^）]*?第 \d+ 行）[，,]?未经任何换算。?$")
_QUARTER_COMPARISON = re.compile(
    r"^(?P<entity>.+?) reported (?P<label>.+?) of (?P<unit>[A-Z]{3}) "
    r"(?P<value>-?\d+(?:\.\d+)?) for (?P<start>\d{4}-\d{2}-\d{2})\.\."
    r"(?P<end>\d{4}-\d{2}-\d{2}), (?P<direction>up|down) "
    r"(?P<growth>\d+(?:\.\d+)?)% year over year from (?P=unit) "
    r"(?P<prior>-?\d+(?:\.\d+)?) in the comparable quarter\.$")


def quarter_comparison_display(text: Any) -> str | None:
    """Display a complete canonical comparison claim, without rewriting it."""
    raw = str(text or "").strip()
    found = _QUARTER_COMPARISON.fullmatch(raw)
    if found:
        from .cockpit_model_display import LINE_LABELS
        name = LINE_LABELS.get(found["label"].casefold(), found["label"])
        direction = "增长" if found["direction"] == "up" else "下降"
        return (f"{found['entity']}：{name}（{_period_text(found['start'], found['end'])}）"
                f"为 {_amount(found['value'], found['unit'])}，同比{direction} {found['growth']}%；"
                f"上年同期为 {_amount(found['prior'], found['unit'])}。")
    return None


def filing_figure_display(text: Any) -> str | None:
    """The generated filed-figure sentence as one short Chinese line, or ``None``.

    ``Revenues (us-gaap:Revenues) for 2026-01-01..2026-06-30 was 10894000000
    usd, as filed in 0001058290-26-000031`` reads
    「营业收入（2026年1月1日至2026年6月30日）：108.9 亿美元 · SEC 申报原值」.
    """
    raw = str(text or "").strip()
    found = _EN_FIGURE.match(raw)
    if found:
        from .cockpit_model_display import LINE_LABELS

        name = (concept_label(found["concept"])
                or LINE_LABELS.get(found["label"].strip().casefold())
                or found["label"].strip())
        return (f"{name}（{_period_text(found['start'], found['end'])}）："
                f"{_amount(found['value'], None, found['unit'])} · SEC 申报原值")
    found = _CN_FIGURE.match(raw)
    if found:
        segment = f"（分部：{found['segment']}）" if found["segment"] else ""
        return (f"{found['metric']}{segment}（{_period_text(found['start'], found['end'])}）："
                f"{_amount(found['value'], found['currency'])} · {found['form']} 申报原值")
    return None


def statement_display(text: Any) -> str | None:
    """A stored statement as shown, or ``None`` when nothing changes."""

    raw = "" if text is None else str(text)
    shown = filing_figure_display(raw) or financial_display_text(raw)
    return shown if shown != raw else None


def source_display_label(source: Any) -> str | None:
    """One cited source (a ref or a source row) for 来源; ``None`` = keep."""

    if not isinstance(source, Mapping):
        return None
    text = source.get("display_label") or source.get("label") or source.get("text")
    if not isinstance(text, str) or not text:
        return None
    # Batch am: a derived ratio's machine precision (「0.577264（即 57.73%）」)
    # reads as a person writes it.
    from .numeric_display import format_prose_machine_numbers

    shown = statement_display(text)
    return format_prose_machine_numbers(shown) if isinstance(shown, str) and shown else shown


# ---------------------------------------------------------------------------
# batch ah (2026-10-09, three analyst UX reviews): names on the financial grid
# and the company header.  Display only -- applied to copies after every
# localization lookup; nothing here is hashed or stored.
# ---------------------------------------------------------------------------

#: Registered names for the tickers the workspaces cover today.  A member's own
#: ``name`` (the SEC resolver's) always wins; this only fills a gap so that a
#: header never falls back to a bare CIK (financial review 2.3, MSFT
#: ``0000789019``).
COMPANY_DISPLAY_NAMES = {
    "ACN": "Accenture", "CTSH": "Cognizant", "EPAM": "EPAM", "IBM": "IBM",
    "DXC": "DXC Technology", "AMZN": "Amazon", "GOOGL": "Alphabet", "GOOG": "Alphabet",
    "META": "Meta Platforms", "MSFT": "Microsoft",
}
UNNAMED_COMPANY = "未登记代码的公司"
_CIK_ONLY = re.compile(r"^\d{6,10}$")


def company_display_name(member: Mapping[str, Any] | None, company_ref: Any = None) -> str:
    """``Accenture ACN``: name then ticker, the ticker once when that is all
    there is -- never a CIK (``company:sec-cik:0000789019``)."""

    member = member or {}
    ticker = str(member.get("ticker") or "").strip()
    name = str(member.get("name") or "").strip()
    if not name or _CIK_ONLY.fullmatch(name):
        name = COMPANY_DISPLAY_NAMES.get(ticker.upper(), "")
    if ticker and name and name.upper() != ticker.upper():
        return f"{name} {ticker}"
    if ticker:
        return ticker
    if name:
        return name
    ref = str(company_ref or "")
    tail = ref.rsplit(":", 1)[-1] if ref else ""
    if ref.startswith("company:ticker:") and tail:
        return tail.upper()
    return UNNAMED_COMPANY


_FILLER_LINE = re.compile(r"^本行为(?P<name>[^（(\n]+?)\s*[（(][^）)\n]*[）)]\s*科目[。.]?\s*(?P<rest>.*)$")  # term-ok: the pattern that removes the filler
_FILLER_NAMES = {"收入": "营业收入"}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def strip_label_filler(text: Any) -> str:
    """A line label a translator turned into a sentence, back to the name.

    「本行为收入（Revenues）科目。」 -> 「营业收入」;
    「本行为稀释每股收益（Diluted EPS）科目，为单独标注的小值指标。」 -> 「稀释每股收益」.
    The drafting contract (final_text_contract, batch am) forbids such labels;
    this keeps text stored before that rule readable.  A trailing clause after
    「科目，」 is dropped: a label carries no explanation.
    """

    value = " ".join(str(text or "").split())
    found = _FILLER_LINE.match(value)
    if not found:
        return value
    name = found["name"].strip()
    rest = found["rest"].strip()
    if rest.startswith(("，", ",", "为")):
        rest = ""
    return (_FILLER_NAMES.get(name, name) + (f" {rest}" if rest else "")).strip()


def humanized_concept(concept: Any) -> str | None:
    """``us-gaap:CostOfGoodsAndServicesSold`` -> ``Cost Of Goods And Services
    Sold``: the filed concept in words when no Chinese name is registered."""

    text = str(concept or "").strip()
    found = _XBRL_ID.fullmatch(text) or re.fullmatch(r"(?:[a-z-]+:)?(?P<concept>[A-Z][A-Za-z0-9]+)", text)
    if not found:
        return None
    local = text.split(":", 1)[-1]
    return " ".join(_CAMEL.split(local)).strip() or None


def model_line_display(label: Any, concept: Any = None) -> str:
    """One financial-grid row name: Chinese where known, a humanized English
    concept otherwise; never a generated explanation sentence
    (「本行为收入（Revenues）科目。」) and never a raw ``us-gaap:`` id."""

    text = " ".join(str(label or "").split())
    text = strip_label_filler(text)
    if _XBRL_ID.fullmatch(text):
        return concept_label(text) or humanized_concept(text) or "未命名科目"
    if _XBRL_ID.search(text):
        text = _XBRL_PAREN.sub("", text)
        text = _XBRL_ID.sub(lambda m: concept_label(m["concept"]) or humanized_concept(m["concept"]) or "申报科目", text)
    if text:
        return text
    return concept_label(concept) or humanized_concept(concept) or "未命名科目"


#: The money rule of every table the company page draws (MUSE 10.2): one unit
#: per table, chosen from the table's largest amount by the batch-v scale
#: (亿美元 from 1 亿 up, else 万美元), printed once in the header row --
#: never per cell, never as an 18-digit float.
def money_unit_header(unit: Any) -> str:
    text = str(unit or "").strip()
    return f"金额：{text}" if text else "金额：美元"


__all__ = ["COMPANY_DISPLAY_NAMES", "company_display_name", "concept_label",
           "filing_figure_display", "financial_display_text", "humanized_concept",
           "model_line_display", "money_unit_header", "source_display_label", "strip_label_filler",
           "statement_display"]
