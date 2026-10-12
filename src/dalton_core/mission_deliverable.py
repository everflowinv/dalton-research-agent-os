"""P10c: the mission's own documents, with every number traceable (vision v1.1).

Phase 9 froze what an Initial Screen contains (the Playbook's
``deliverable_templates.initial_screen``) and what it must satisfy to pass its
gate.  P9d filled the Ledger with Claims.  This module is where the two meet:
a versioned, append-only document per company, drafted from Claims the Ledger
already holds, and refused if it says a number it cannot trace.

Three rules the authority enforces rather than trusts:

- **Every number traces to a quantitative Claim.**  A section body may only
  carry a figure that appears in that section's ``numbers`` list bound to a
  quantitative claim version.  A period label (a year, a quarter, a fiscal
  year) is not a figure, the same distinction the drafting path already makes.
  An unsourced figure fails the publish; the drafter is expected to write
  "缺来源" instead of guessing.
- **Every cited Claim exists and is not retired.**  A section's ``claim_refs``
  must name live claim versions (P10b retirements are excluded), so a document
  can never rest on a Claim the system has already disowned.
- **The document binds the authorities it was written under.**  The mission
  version and the playbook version, both by hash: if either moves, the next
  version records the move instead of the text silently drifting.

Automation may publish only what the mission grants (``deliverable`` in
``autonomy.may_write``); a person may always publish.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator

from .store import DaltonStore, authorization_flag, authorized_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("mission_deliverable_schema.sql")
WRITE_SCOPE = "deliverable"
DELIVERABLE_KINDS: tuple[str, ...] = (
    "industry_framework", "initial_screen", "industry_model", "company_model",
    "forecast_lines", "investment_memo", "weekly_brief",
    # P14a: the short note a tracked event produces -- "ACN fell 4.2% today:
    # sector-wide, no company news; the nearest driver is X".  One chain per
    # company, gaining a version per note, so the company's running commentary
    # is replayable by version like every other output (ADR-0008).  It is a
    # deliverable rather than a new object precisely so that it inherits the
    # rule that a figure with no live Claim behind it is refused.
    "event_note",
    # P12d: the Deep Insight Gate's twelve answers, rendered for a reader.  The
    # record of truth is the gate authority's own chain -- that is what the
    # owner's decision binds by hash and what replays by version -- and this is
    # the reader's copy, published through the machinery every other document in
    # this system already appears in.  It is a deliverable rather than a second
    # object precisely so that it inherits the rule that a figure with no live
    # Claim behind it is refused.
    "deep_insight_gate",
    # P14f: the two windows of an earnings season.  One chain per company per
    # kind, gaining a version per occurrence, so "what did we say before the
    # print and what did we say after it" is a version walk like every other
    # output (ADR-0008).  Deliverables rather than new objects for the same
    # reason as the two above.
    "earnings_preview",
    "earnings_calibration",
)
#: The one ``change_reason`` a version zero may carry, and the only version it
#: may be carried on. Named here so the two rules read as one thing.
IMPORT_CHANGE_REASON = "imported_prior"


def deliverable_change_reasons() -> tuple[str, ...]:
    """ADR-0008's five, plus the one word only a deliverable chain can use.

    Deliberately *not* a widening of ``model_forecast_driver.CHANGE_REASONS``.
    Those five are reasons a version *changed*; ``imported_prior`` is a
    version that changed nothing, because it is a document that already
    existed being put at the head of the chain. A catalyst calendar or a
    tracking cadence can never produce it, so putting it in the shared tuple
    would only let those authorities accept a word that means nothing to them
    -- a closed vocabulary is worth having precisely because every word in it
    is reachable.
    """

    from .model_forecast_driver import CHANGE_REASONS

    return (*CHANGE_REASONS, IMPORT_CHANGE_REASON)
MAX_SECTIONS = 24
MAX_BODY_CHARS = 6000
MAX_SECTION_NUMBERS = 128
SECTION_NUMBERS_VALIDATION_CONTRACT = "section-numbers-capacity:128:v1"
GAP_MARKER = "缺来源"

# A year, quarter, half or fiscal-year label is a period, not a figure.  Same
# rule the extraction contract uses, so the two paths agree on what a number is.
_PERIOD_TOKEN_RE = re.compile(
    # ISO dates and the ".." ranges the Ledger prints for a reporting period,
    # which live were being read apart into "01" and "31".
    r"(?:19|20)\d{2}-\d{2}-\d{2}(?:\s*\.\.\s*(?:19|20)?\d{2}-\d{2}-\d{2})?"
    r"|(?:FY\s?)?(?:19|20)\d{2}(?:\s?[-–/]\s?(?:19|20)?\d{2})?(?:\s?(?:年|财年))?"
    r"|FY\s?\d{2}(?![0-9])"
    r"|Q[1-4]\s?(?:FY\s?)?(?:19|20)?\d{0,4}"
    r"|(?:19|20)\d{2}\s?Q[1-4]"
    r"|[1-4]Q(?:19|20)?\d{2}"
    r"|H[12]\s?(?:19|20)?\d{0,4}"
    r"|第?[一二三四1-4]季度"
    # Batch D: a rolling-window label is a period, not a figure -- 「30 日 ADV」,
    # 「52 周区间」, "30-day ADV". Narrow on purpose: only when a window noun
    # follows, so 「30 日内上涨 5%」's 30 is still a figure to source.
    r"|\d{1,3}\s?(?:日|天|周|个交易日)(?=\s?(?:ADV|均量|成交|区间|高点|低点|滚动|移动|平均|波动))"
    r"|\d{1,3}[-\s]?(?:day|week)(?=\s?(?:ADV|average|moving|volume|range|high|low))",
    re.IGNORECASE,
)
# The Playbook's rule is about *timely numbers*: a measurement that has to come
# from a filing or a tool result.  A bare small integer with no unit, percent,
# currency or separator is a threshold, a count or an ordinal ("book-to-bill
# 跌破 1", "两条线"), not a measurement, and requiring a Claim for it would
# empty the document without making it truer.
_BARE_SMALL_INTEGER = 12
# The longest rendering of one cited figure. A Claim's normalized_statement has
# no ceiling in its own contract, so this is generous on purpose: it exists to
# stop a single statement becoming a document, not to police wording. The
# document's real bound is MAX_SECTION_NUMBERS on a section's numbers.
MAX_NUMBER_TEXT = 1000
_VALUE_TOKEN_RE = re.compile(r"[$€£¥]\s?\d[\d,.]*|\d[\d,.]*\s?%|\d[\d,.]*")
_ISO_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2}-\d{2}-\d{2})(?!\d)")
_MONTH_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01]),?\s+(?:19|20)\d{2}(?!\d)",
    re.IGNORECASE,
)
_MONTH_DAY_RE = re.compile(
    r"(?<![A-Za-z])(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(0?[1-9]|[12]\d|3[01])(?!\d)",
    re.IGNORECASE,
)
_BARE_MONTH_DAY_RE = re.compile(
    _MONTH_DAY_RE.pattern + r"(?!\s*,?\s*(?:19|20)\d{2})",
    re.IGNORECASE,
)
# The day-first spelling the sell side prints in a period -- "23 Jun 2026" --
# which is how IBM's history_of_price_drivers was refused twice on 2026-09-27
# ($0.88 a round) for writing "6 月 23 日".  The month must be capitalised:
# "23 may" is more often a verb than a date.
_DAY_MONTH_RE = re.compile(
    r"(?<![\d.,/])(0?[1-9]|[12]\d|3[01])\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)(?![A-Za-z])(?:,?\s+((?:19|20)\d{2})(?!\d))?",
)
NUMBER_SOURCE_CONTRACT_VERSION = "number-source-contract:0.9"


def number_source_contract_fingerprint() -> str:
    """Identity for consumers whose terminal hold depends on number sourcing."""

    from .store import content_hash

    return content_hash({
        "version": NUMBER_SOURCE_CONTRACT_VERSION,
        "number_source_fields": ["text", "period"],
        # 0.8 (batch D): a material-pack number (``number_ref``) and a
        # 「我们的估计」 bound to a model assumption are admissible sources.
        "admissible_sources": ["claim", "cell", "passage", "derivation", "pack_number",
                               "our_estimate"],
        "window_labels": "n-day/week window before ADV/区间/均量/... is a period",
        "bound_period_equivalence": (
            "iso-date-to-english-month-date-or-exact-cited-month-day"
            "+cjk-and-slash-month-day+day-first-month-date"
            "+read-from-period-and-text"),
        "print_equivalence": (
            "percent-word+english-number-words-exact+currency-symbol-as-word"),
        "year_amount_disambiguation": "four-digit-year-with-explicit-money-or-percent-unit-is-a-value",
    })


def _kind_check_list() -> str:
    """The CHECK constraint's value list, generated from the vocabulary.

    So that the schema file, the rebuild and the Python vocabulary cannot
    disagree about which kinds exist -- which is exactly how a Core ends up
    refusing a kind with an ``IntegrityError`` instead of a readable message.
    """

    return ",".join(f"'{kind}'" for kind in DELIVERABLE_KINDS)


class MissionDeliverableError(RuntimeError):
    """Base error for the deliverable authority."""


class MissionDeliverableValidationError(MissionDeliverableError):
    """A closed field or argument is invalid."""


class MissionDeliverableConflict(MissionDeliverableError):
    """An append-only record was reused with different semantics, or a gate refused."""


class MissionDeliverableNotFound(MissionDeliverableError):
    """The named record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000, minimum: int = 1) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise MissionDeliverableValidationError(f"{name} must be text of {minimum}..{maximum} characters")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MissionDeliverableValidationError(f"{name} must be a SHA-256 hex digest")
    return value


# A drafted body may still carry a "C7" / "N1" citation tag; its digits are a
# reference, not a figure.  Live, they were the first thing the check flagged.
_CITATION_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CN]\d{1,3}(?![A-Za-z0-9])")


def value_tokens(text: str, *, include_small: bool = False) -> list[str]:
    """Every figure the text asserts, with period labels and citation tags removed."""

    without_tags = _CITATION_TAG_RE.sub(" ", text or "")
    def period_or_amount(match: re.Match[str]) -> str:
        # Only a standalone bare year is ambiguous. FY labels, full dates,
        # quarters and explicit 年/财年 remain periods, even next to money.
        if re.fullmatch(r"(?:19|20)\d{2}", match.group()):
            before = without_tags[:match.start()]
            after = without_tags[match.end():]
            if (re.search(r"[\d.,]$", before) or re.match(r"(?:\d|[.,]\d)", after)
                    or re.search(r"[$€£¥]\s*$", before)
                    or re.match(r"\s*(?:(?:万亿|十亿|百万|千万|亿|万|千)\s*)?(?:美元|人民币|欧元|英镑|日元|港元|USD\b|EUR\b|GBP\b|JPY\b|dollars?\b|euros?\b|%)", after, re.I)
                    or re.match(r"\s*(?:billion|million|trillion)\s+(?:USD\b|EUR\b|dollars?\b|euros?\b)", after, re.I)):
                return match.group()
        return " "
    without_periods = _PERIOD_TOKEN_RE.sub(period_or_amount, without_tags)
    tokens = []
    for match in _VALUE_TOKEN_RE.finditer(without_periods):
        token = match.group(0).strip()
        plain = token.rstrip(".")
        if not include_small and plain.isdigit() and int(plain) <= _BARE_SMALL_INTEGER:
            continue
        tokens.append(token)
    return tokens


def _normalise_number(token: str) -> str:
    return re.sub(r"[\s,]", "", token).rstrip(".")


def _normalise_date(value: str) -> str | None:
    compact = value.replace(",", "")
    for pattern in ("%b %d %Y", "%B %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(compact, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _source_fields(item: Mapping[str, Any]) -> tuple[str, str]:
    """The two fields of a cited row a date may be read from: period and text.

    A row states its date in either: "23 Jun 2026" in the period column, or
    "on 2026-06-23 the stock ..." in the statement.  Both are the row's own
    words, so both bind; nothing else does.
    """

    return str(item.get("period") or ""), str(item.get("text") or "")


def _month_number(name: str) -> str | None:
    try:
        return datetime.strptime(name[:3].title(), "%b").strftime("%m")
    except ValueError:
        return None


def _day_first_date(match: re.Match[str]) -> str | None:
    month = _month_number(match.group(2))
    if month is None or match.group(3) is None:
        return None
    try:
        return datetime(int(match.group(3)), int(month), int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def _bound_period_dates(numbers: Sequence[Mapping[str, Any]]) -> set[str]:
    """Every complete date a cited row names, in its period or its text."""

    dates: set[str] = set()
    for item in numbers:
        for field in _source_fields(item):
            for match in _ISO_DATE_RE.finditer(field):
                normalised = _normalise_date(match.group(1))
                if normalised is not None:
                    dates.add(normalised)
            for match in _MONTH_DATE_RE.finditer(field):
                normalised = _normalise_date(match.group(0))
                if normalised is not None:
                    dates.add(normalised)
            for match in _DAY_MONTH_RE.finditer(field):
                if (normalised := _day_first_date(match)) is not None:
                    dates.add(normalised)
    return dates


def _month_day(value: str) -> tuple[str, int] | None:
    match = _MONTH_DAY_RE.search(value)
    if match is None:
        return None
    month = _month_number(match.group(1))
    return None if month is None else (month, int(match.group(2)))


def _remove_bound_month_dates(
    body: str, numbers: Sequence[Mapping[str, Any]],
) -> str:
    """Remove only complete dates proved by a cited number's period.

    ``value_tokens`` still sees every unbound or differently dated expression.
    This is deliberately an equality check against authority material, not a
    blanket rule that dates are never figures.
    """

    bound_dates = _bound_period_dates(numbers)
    checked = _MONTH_DATE_RE.sub(
        lambda match: (" " * len(match.group(0)))
        if _normalise_date(match.group(0)) in bound_dates else match.group(0),
        body,
    )
    checked = _DAY_MONTH_RE.sub(
        lambda match: (" " * len(match.group(0)))
        if match.group(3) is not None and _day_first_date(match) in bound_dates
        else match.group(0),
        checked,
    )
    bound_month_days = _bound_month_day_keys(numbers)
    if not bound_month_days:
        return checked
    checked = _BARE_MONTH_DAY_RE.sub(
        lambda match: (" " * len(match.group(0)))
        if _month_day(match.group(0)) in bound_month_days else match.group(0),
        checked,
    )
    return _DAY_MONTH_RE.sub(
        lambda match: (" " * len(match.group(0)))
        if match.group(3) is None
        and (_month_number(match.group(2)), int(match.group(1))) in bound_month_days
        else match.group(0),
        checked,
    )


# The Ledger renders a percentage as "15.8 (percent)"; the Chinese prose the
# drafting prompt asks for prints the same figure as "15.8%".  Same digits,
# same unit, different token -- live on 2026-09-25 that refused CTSH's
# supply_and_cost round after round.  Only the unit word is read: the digits
# must still match exactly, and a bare cited number never sources a
# percentage.  (A figure rescaled or rounded for print -- "约 188.6 亿" for a
# filed 18857000000 -- is still a number the Ledger does not carry; see the
# golden ``unit-rewrite`` case.)
_PERCENT_WORD_RE = re.compile(
    r"(\d[\d,.]*)\s*\(?\s*(?:percent|per\s?cent|pct)\b\)?", re.IGNORECASE)
# A day written the way the Chinese prose writes it, and the "3/19" the sell
# side writes in a period label.  Both are dates only when a cited period
# names that same month and day; otherwise their digits stay figures.
_CJK_MONTH_DAY_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])\s*月\s*([12]\d|3[01]|0?[1-9])\s*[日号]")
_SLASH_MONTH_DAY_RE = re.compile(
    r"(?<![\d/.])(1[0-2]|0?[1-9])/([12]\d|3[01]|0?[1-9])(?![\d/])")


def _bound_month_day_keys(numbers: Sequence[Mapping[str, Any]]) -> set[tuple[str, int]]:
    """Every month and day a cited row names, in any of its spellings.

    Read from the row's period *and* its text: "Jun 23", "23 Jun 2026",
    "2026-06-23", "6/23", "6月23日".  Equality only -- the key is the exact
    (month, day), so a cited 23 June never sources a 24 June or a bare 23.
    """

    keys: set[tuple[str, int]] = set()
    for iso in _bound_period_dates(numbers):
        keys.add((iso[5:7], int(iso[8:10])))
    for item in numbers:
        for field in _source_fields(item):
            for match in _MONTH_DAY_RE.finditer(field):
                if (month := _month_number(match.group(1))) is not None:
                    keys.add((month, int(match.group(2))))
            for match in _DAY_MONTH_RE.finditer(field):
                if (month := _month_number(match.group(2))) is not None:
                    keys.add((month, int(match.group(1))))
            for pattern in (_CJK_MONTH_DAY_RE, _SLASH_MONTH_DAY_RE):
                for match in pattern.finditer(field):
                    keys.add((f"{int(match.group(1)):02d}", int(match.group(2))))
    return keys


def _remove_bound_numeric_month_days(
    body: str, numbers: Sequence[Mapping[str, Any]],
) -> str:
    keys = _bound_month_day_keys(numbers)
    if not keys:
        return body
    for pattern in (_CJK_MONTH_DAY_RE, _SLASH_MONTH_DAY_RE):
        body = pattern.sub(
            lambda match: (" " * len(match.group(0)))
            if (f"{int(match.group(1)):02d}", int(match.group(2))) in keys
            else match.group(0),
            body,
        )
    return body


# 2026-09-26: a cited row that spells its figure in English words -- "five
# thousand employees" -- carries that figure as surely as one printing 5000,
# and CTSH's dossier was refused round after round ($0.88 each) for writing it
# in digits.  The words are read as exactly one integer and only that integer:
# no rounding, no "about", no digits-with-scale ("5 thousand"), and only the
# plain spelling (see ``_english_words``), so "twenty five hundred" or "five
# five" stay unread rather than being guessed at.
_UNITS_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS_WORDS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty",
               "seventy", "eighty", "ninety")
_SCALE_WORDS = (("trillion", 10 ** 12), ("billion", 10 ** 9),
                ("million", 10 ** 6), ("thousand", 10 ** 3))
_NUMBER_WORD = (
    "(?:" + "|".join(sorted(
        [*_UNITS_WORDS, *filter(None, _TENS_WORDS), "hundred",
         *(name for name, _ in _SCALE_WORDS)], key=len, reverse=True)) + ")")
_ENGLISH_NUMBER_RE = re.compile(
    rf"(?<![A-Za-z-]){_NUMBER_WORD}(?:(?:\s+and\s+|\s+|-){_NUMBER_WORD})*"
    r"(?![A-Za-z-])(\s*\(?\s*(?:percent|per\s?cent|pct)\b\)?)?",
    re.IGNORECASE)


def _english_below_thousand(value: int) -> list[str]:
    words: list[str] = []
    hundreds, rest = divmod(value, 100)
    if hundreds:
        words += [_UNITS_WORDS[hundreds], "hundred"]
    if rest >= 20:
        tens, units = divmod(rest, 10)
        words.append(_TENS_WORDS[tens] + ("-" + _UNITS_WORDS[units] if units else ""))
    elif rest or not words:
        words.append(_UNITS_WORDS[rest])
    return words


def _english_words(value: int) -> str:
    """The one plain spelling of ``value``, e.g. ``five thousand two hundred``."""

    if value == 0:
        return "zero"
    words: list[str] = []
    for name, scale in _SCALE_WORDS:
        count, value = divmod(value, scale)
        if count:
            words += [*_english_below_thousand(count), name]
    if value:
        words += _english_below_thousand(value)
    return " ".join(words)


def _english_number(phrase: str) -> int | None:
    """The integer an English number phrase names exactly, or ``None``.

    Parsed leniently, then accepted only if the value spells back to the same
    words (``and`` and hyphens aside): a phrase that is not the plain spelling
    of one number is not a number this check will vouch for.
    """

    words = [word for word in re.split(r"[\s-]+", phrase.lower())
             if word and word != "and"]
    units = {word: index for index, word in enumerate(_UNITS_WORDS)}
    tens = {word: index * 10 for index, word in enumerate(_TENS_WORDS) if word}
    scales = dict(_SCALE_WORDS)
    total = current = 0
    for word in words:
        if word in units:
            current += units[word]
        elif word in tens:
            current += tens[word]
        elif word == "hundred":
            current *= 100
        elif word in scales:
            total += current * scales[word]
            current = 0
        else:
            return None
    value = total + current
    spelled = re.split(r"[\s-]+", _english_words(value))
    return value if spelled == words else None


def english_number_tokens(text: str) -> set[str]:
    """Normalised figures that English number words in ``text`` state exactly."""

    found: set[str] = set()
    for match in _ENGLISH_NUMBER_RE.finditer(text or ""):
        phrase = match.group(0)[: len(match.group(0)) - len(match.group(1) or "")]
        value = _english_number(phrase)
        if value is None:
            continue
        # As with digits: "25 percent" in a row sources both 25 and 25%.
        found.add(str(value))
        if match.group(1):
            found.add(f"{value}%")
    return found


# A cited row saying "$970 million" and Chinese prose saying "970 million 美元"
# state one figure: the digits are the row's, and the currency symbol has only
# moved into a word -- exactly what the drafting prompt asks for ("18857000000
# usd is written 18857000000 美元").  Live 2026-09-26..28 that refused DXC's
# supply_and_cost seven times over "970" (C1: "Adjusted EBIT was $970
# million").  Only the symbol is dropped: the digits must still match exactly,
# so a rescaled "970000000" or a rounded "9.7 亿" stays unsourced.
_CURRENCY_SYMBOLS = "$€£¥"


def unsourced_numbers(body: str, numbers: Sequence[Mapping[str, Any]]) -> list[str]:
    """Figures in the body that no supplied, Claim-bound number accounts for."""

    sourced = set()
    for item in numbers:
        text = str(item.get("text", ""))
        for token in value_tokens(text):
            normalised = _normalise_number(token)
            sourced.add(normalised)
            if normalised[:1] in _CURRENCY_SYMBOLS:
                sourced.add(normalised[1:])
        for match in _PERCENT_WORD_RE.finditer(text):
            sourced.add(_normalise_number(match.group(1)) + "%")
        sourced |= english_number_tokens(text)
    checked_body = _remove_bound_numeric_month_days(
        _remove_bound_month_dates(body, numbers), numbers)
    # A displayed equation is itself evidence only after replay against an
    # already-validated derivation. Its constants never source unrelated prose.
    equations = re.compile(r"(?<![\dA-Za-z])[\d(][\d.\s()+−\-×*/÷]*[\d)]")
    def proven_equation(match: re.Match[str]) -> str:
        expression = match.group().strip().replace('−','-').replace('×','*').replace('÷','/')
        if not any(op in expression for op in ('*','/')):
            return match.group()
        for item in numbers:
            derivation = item.get('derivation')
            if not isinstance(derivation, Mapping):
                continue
            result = str(derivation['result'])
            try:
                validate_derived_number({'text':result,'derivation':{'expression':expression,'result':result}},
                                        numbers, check_stray=False)
            except MissionDeliverableError:
                continue
            return ' ' * len(match.group())
        return match.group()
    checked_body = equations.sub(proven_equation, checked_body)
    def exact_value(token: str) -> tuple[str, bool, Decimal] | None:
        value = _normalise_number(token)
        currency = value[:1] if value[:1] in _CURRENCY_SYMBOLS else ''
        try:
            return currency, value.endswith('%'), Decimal(_bare(value))
        except InvalidOperation:
            return None
    exact_sources = {value for token in sourced if (value := exact_value(token)) is not None}
    return [
        token for token in value_tokens(checked_body)
        if _normalise_number(token) not in sourced and exact_value(token) not in exact_sources
    ]


# P12e / P14f: what a figure may cite when no Claim carries it.
#
# The rule this document layer was built on -- every timely number traces to a
# quantitative Claim -- was right about the failure it prevented and wrong
# about the world it assumed, which was one where every number a research
# output states has been read out of a source by an extractor.  Two layers now
# produce figures that are *computed*, deterministically, from things the Core
# already holds and can re-derive: P12e's cross-company comparison cells, each
# the arithmetic of filed statement lines, and P14f's forecast cells, each a
# frozen formula over a stored model version.  Neither has a Claim behind it
# and neither ever will; minting one would be manufacturing a quotation for a
# calculation.
#
# So a number entry cites a Claim *or* a cell, never both and never neither,
# and a cell is admitted only when it still resolves at publish time.  The
# discipline is unchanged in the only sense that matters: a figure in the prose
# that nothing in this Core can produce is still refused.
CELL_SOURCE_KINDS: tuple[str, ...] = (
    "statement_accession", "forecast_cell", "valuation_metric",
)
_CELL_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType({
    "statement_accession": frozenset({"kind", "ref", "accession"}),
    "forecast_cell": frozenset({"kind", "ref", "version_ref"}),
    # P11c-E: the third computed layer the note below anticipated. A multiple
    # is arithmetic over a price version and a filed line, with the formula
    # frozen into the snapshot beside it; there is no Claim behind it and
    # there never will be. ``ref`` is ``valuation-metric:<version>:<metric>``,
    # which is what the gate reader already mints.
    "valuation_metric": frozenset({"kind", "ref", "version_ref"}),
})


def validate_cell_citation(value: Any, name: str = "number.cell") -> dict[str, Any]:
    """One computed cell a figure may cite.  Shape only; resolving is separate."""

    if not isinstance(value, Mapping):
        raise MissionDeliverableValidationError(f"{name} must be an object")
    kind = value.get("kind")
    if kind not in CELL_SOURCE_KINDS:
        raise MissionDeliverableValidationError(
            f"{name}.kind must be one of {list(CELL_SOURCE_KINDS)}")
    expected = _CELL_FIELDS[kind]
    if set(value) != expected:
        raise MissionDeliverableValidationError(
            f"{name} for {kind} must be exactly {sorted(expected)}")
    wire = {"kind": kind, "ref": _text(value["ref"], f"{name}.ref", maximum=512)}
    if kind == "statement_accession":
        wire["accession"] = _text(value["accession"], f"{name}.accession", maximum=256)
    else:
        wire["version_ref"] = _text(value["version_ref"], f"{name}.version_ref",
                                    maximum=512)
    return wire


# ADR-0008's closed vocabulary.  Imported rather than restated so a sixth
# reason has one place to be added, and so the cockpit's label map, which
# already renders these five, cannot drift away from the authority that
# accepts them.
REVISION_FIELDS: frozenset[str] = frozenset({
    "change_reason", "evidence_refs", "reopen_ref",
})


def validate_revision(value: Any) -> dict[str, Any] | None:
    """Why a version exists, or ``None`` when nobody claimed a reason.

    ADR-0008: a version carries a ``change_reason`` from a closed vocabulary
    and the exact evidence refs that occasioned it, and "a version that cannot
    name what it learned is a rewrite".  So a reason with no refs is refused
    here rather than stored and disbelieved later.
    """

    if value is None:
        return None
    reasons = deliverable_change_reasons()

    if not isinstance(value, Mapping):
        raise MissionDeliverableValidationError("revision must be an object")
    unknown = sorted(set(value) - REVISION_FIELDS)
    if unknown:
        raise MissionDeliverableValidationError(
            f"revision has unknown fields: {', '.join(unknown)}"
        )
    reason = value.get("change_reason")
    if reason not in reasons:
        raise MissionDeliverableValidationError(
            f"change_reason must be one of {list(reasons)}"
        )
    refs = value.get("evidence_refs") or ()
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise MissionDeliverableValidationError("revision evidence_refs must be a list")
    cleaned = [_text(ref, "revision.evidence_refs[]", maximum=512) for ref in refs]
    if not cleaned:
        raise MissionDeliverableValidationError(
            "a revision must name the evidence that occasioned it (ADR-0008)"
        )
    reopen_ref = value.get("reopen_ref")
    if reopen_ref is not None:
        reopen_ref = _text(reopen_ref, "revision.reopen_ref", maximum=512)
    return {
        "change_reason": reason,
        "evidence_refs": list(dict.fromkeys(cleaned))[:40],
        "reopen_ref": reopen_ref,
    }


# ---------------------------------------------------------------------------
# Batch A: figures sourced from a cited passage, or derived from cited figures
# ---------------------------------------------------------------------------

#: Constants a derivation may use without a citation: a bare small integer
#: (the same threshold ``value_tokens`` ignores) and powers of ten (scaling and
#: percent conversion).
_DERIVATION_CONSTANTS = frozenset({"100", "1000", "10000", "1000000", "1000000000"})
_DERIVATION_RE = re.compile(r"^[0-9.+\-*/()\s]+$")
MAX_DERIVATION_CHARS = 200


def _sourced_value_set(text: str) -> set[str]:
    """Every normalised figure a text states, in the forms a body may print it."""

    found: set[str] = set()
    for token in value_tokens(text, include_small=True):
        normalised = _normalise_number(token)
        found.add(normalised)
        if normalised[:1] in _CURRENCY_SYMBOLS:
            found.add(normalised[1:])
    for match in _PERCENT_WORD_RE.finditer(text):
        found.add(_normalise_number(match.group(1)) + "%")
    found |= english_number_tokens(text)
    return found


def _bare(token: str) -> str:
    token = token.lstrip(_CURRENCY_SYMBOLS).rstrip("%").strip()
    return token


def validate_passage_number(item: Mapping[str, Any],
                            resolve_passage: Callable[[str], str | None] | None) -> dict[str, Any]:
    """A figure citing a ``doc-passage:`` ref: its values must be in the passage."""

    text = _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT)
    ref = _text(item["passage_ref"], "number.passage_ref", maximum=1024)
    if not ref.startswith("doc-passage:"):
        raise MissionDeliverableValidationError("number.passage_ref must be a doc-passage: ref")
    passage_text = resolve_passage(ref) if resolve_passage is not None else None
    if passage_text is None:
        raise MissionDeliverableConflict(
            "a figure cites a passage that does not resolve (or no longer hashes to its ref) "
            f"in this Core: {ref}")
    tokens = value_tokens(text, include_small=True)
    if not tokens:
        raise MissionDeliverableValidationError(
            "a passage-cited number entry must state the figure it cites")
    available = _sourced_value_set(passage_text)
    missing = [token for token in tokens if _normalise_number(token) not in available
               and _normalise_number(token).lstrip(_CURRENCY_SYMBOLS) not in available]
    if missing:
        raise MissionDeliverableConflict(
            f"a figure is not in the passage it cites: {missing[:3]} not found in {ref}")
    return {"text": text, "passage_ref": ref,
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120)}


def _evaluate(node: Any) -> float:
    import ast

    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise MissionDeliverableValidationError("a derivation divides by zero")
        return left / right
    raise MissionDeliverableValidationError("a derivation is arithmetic over numbers only (+ - * / and parentheses)")


def _bound_scale_constants(tree: Any, sourced: set[Decimal]) -> set[int]:
    """Powers of ten are operators on proved operands, never new facts.

    The expression's AST supplies the operation type. A scale is accepted
    only as a multiplication/division factor whose other branch recursively
    consists of cited inputs and valid arithmetic. Additive amounts and a
    bare scale-as-result remain subject to the source requirement.
    """
    import ast
    constants: set[int] = set()
    def scalar(node):
        return Decimal(str(node.value)) if isinstance(node, ast.Constant) and type(node.value) in (int,float) else None
    def scale(node):
        value=scalar(node)
        return value is not None and value in {Decimal(10) ** n for n in range(1,13)}
    def bound(node):
        if isinstance(node,ast.Expression):return bound(node.body)
        value=scalar(node)
        if value is not None:return value in sourced
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.USub,ast.UAdd)):return bound(node.operand)
        if not isinstance(node,ast.BinOp):return False
        if isinstance(node.op,(ast.Mult,ast.Div)):
            if scale(node.right) and bound(node.left):
                constants.add(id(node.right));return True
            if isinstance(node.op,ast.Mult) and scale(node.left) and bound(node.right):
                constants.add(id(node.left));return True
        if isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
            # A ratio minus one is the dimensionless growth/return operation.
            if (isinstance(node.op,ast.Sub) and scalar(node.right)==1
                    and isinstance(node.left,ast.BinOp) and isinstance(node.left.op,ast.Div)
                    and bound(node.left.left) and bound(node.left.right)):
                constants.add(id(node.right));return True
            return bound(node.left) and bound(node.right)
        return False
    bound(tree)
    return constants


def _without_verified_expression(text: str, expression: str) -> str:
    """Remove only the exact checked formula, including typographic operators.

    A prose claim that happens to contain 1 or 100 is still checked as a fact.
    """
    translated=text.translate(str.maketrans({'−':'-','×':'*','÷':'/','（':'(','）':')'}))
    pattern=r'\s*'.join(re.escape(char) for char in re.sub(r'\s+','',expression))
    return re.sub(r'(?<![\d.+*/(-])'+pattern+r'(?!\s*[\d.+*/)-])',' ',translated)


def validate_derived_number(item: Mapping[str, Any], cited: Sequence[Mapping[str, Any]], *, check_stray: bool = True) -> dict[str, Any]:
    """A figure computed from figures this section cites, re-computed here."""

    import ast

    text = _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT)
    derivation = item["derivation"]
    if not isinstance(derivation, Mapping) or set(derivation) != {"expression", "result"}:
        raise MissionDeliverableValidationError("number.derivation must be exactly {expression, result}")
    expression = _text(derivation["expression"], "number.derivation.expression", maximum=MAX_DERIVATION_CHARS)
    result = _text(str(derivation["result"]), "number.derivation.result", maximum=40)
    if not _DERIVATION_RE.fullmatch(expression):
        raise MissionDeliverableValidationError(
            "a derivation is arithmetic over plain numbers (no commas, units or names)")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", result):
        raise MissionDeliverableValidationError("number.derivation.result must be a plain number")
    sourced: set[str] = set()
    for entry in cited:
        for token in _sourced_value_set(str(entry.get("text", ""))):
            sourced.add(_bare(token))
        # A verified display retains its exact raw scalar too. Both may be
        # operands; arbitrary peer prose or an unchecked display is not proof.
        if entry.get('display') is not None and entry.get('source_text') is not None:
            display=validate_number_display(entry,str(entry['source_text']))
            sourced.add(_bare(display['source_value']))
    exact_operands = set()
    for token in sourced:
        try:
            exact_operands.add(Decimal(token))
        except InvalidOperation:
            continue
    try:
        tree=ast.parse(expression,mode='eval')
    except SyntaxError as exc:
        raise MissionDeliverableValidationError('a derivation must be a valid arithmetic expression') from exc
    scale_constants=_bound_scale_constants(tree,exact_operands)
    for node in ast.walk(tree):
        if not isinstance(node,ast.Constant) or type(node.value) not in (int,float):continue
        literal=ast.get_source_segment(expression,node)
        plain = literal.rstrip("0").rstrip(".") if "." in literal else literal
        if (literal in sourced or plain in sourced or Decimal(literal) in exact_operands or id(node) in scale_constants or literal in _DERIVATION_CONSTANTS
                or (literal.isdigit() and int(literal) <= _BARE_SMALL_INTEGER)):
            continue
        raise MissionDeliverableConflict(
            f"a derivation uses {literal}, which no other figure in this section cites")
    try:
        value = _evaluate(tree)
    except SyntaxError as exc:
        raise MissionDeliverableValidationError("a derivation must be a valid arithmetic expression") from exc
    decimals = len(result.split(".", 1)[1]) if "." in result else 0
    if abs(value - float(result)) > 0.5 * 10 ** -decimals + 1e-9:
        raise MissionDeliverableConflict(
            f"a derivation does not compute: {expression} = {value:.6g}, not {result}")
    stated = [_bare(_normalise_number(token)) for token in value_tokens(text, include_small=True)]
    if result.lstrip("-") not in stated:
        raise MissionDeliverableConflict(
            f"a derived number entry's text must state its result {result}")
    prose = [_bare(_normalise_number(token)) for token in
             value_tokens(_without_verified_expression(text,expression),include_small=True)]
    stray = [token for token in prose if token != result.lstrip("-") and token not in sourced]
    if stray and check_stray:
        raise MissionDeliverableConflict(
            f"a derived number entry states figures that are neither its result nor cited: {stray[:3]}")
    return {"text": text, "derivation": {"expression": expression, "result": result},
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120)}


#: The gap a figure in an imported prior document is recorded as. It is not a
#: refusal, because an import asserts nothing: the figure rule exists to stop
#: *this system* writing a number it cannot cite, and a v0 is a record of what
#: a document said, bound to that document by ref and hash.
IMPORTED_FIGURE_GAP = "上一版里的数字，未在本系统重新核对"


# ---------------------------------------------------------------------------
# Batch D: pack numbers, our estimates, gap items, contract-strict sections
# ---------------------------------------------------------------------------

#: Kinds published under the batch-D product contract: a failed (empty,
#: heading-only, gap-only) section refuses the version, 「材料缺少」 must bind a
#: MaterialRequest, and indirect placeholders are refused.
CONTRACT_STRICT_KINDS: tuple[str, ...] = ("initial_screen", "investment_memo")
#: Prefixes a 「我们的估计」 figure may bind: a model assumption, an ``O`` tag's
#: our-ref from a material pack, or a forecast cell of our own model.
ESTIMATE_REF_PREFIXES: tuple[str, ...] = ("assumption:", "our-ref:", "our:", "forecast-cell:",
                                          "forecast-model-version:", "house-view:",
                                          # batch C: our model's figures in the pack json
                                          "pack-json:/our_model")

NUMBER_DISPLAY_RULE_REF = "number-display:same-unit-declared-decimals-half-up:1"


def _display_tokens(text: str) -> list[str]:
    """Signed values for precision proofs; body token rules stay unchanged."""
    clean = _PERIOD_TOKEN_RE.sub(" ", _CITATION_TAG_RE.sub(" ", text or ""))
    return [_normalise_number(m.group()) for m in re.finditer(
        r"[$€£¥]?\s?-?\d[\d,]*(?:\.\d+)?\s?%?", clean)]


def number_display(source_text: str, display_value: str) -> dict[str, Any]:
    """Bind one same-unit rendering (one or two decimals) to an exact value."""
    shown=_normalise_number(display_value)
    if not re.fullmatch(r"[$€£¥]?-?\d+\.\d{1,2}%?",shown):
        raise MissionDeliverableConflict('a number display must use one or two decimals in the source unit')
    decimals=len(shown.rstrip('%').split('.')[1])
    currency=shown[:1] if shown[:1] in _CURRENCY_SYMBOLS else ''
    percent=shown.endswith('%')
    candidates=[]
    for token in _display_tokens(source_text):
        original=_normalise_number(token)
        if ((original[:1] if original[:1] in _CURRENCY_SYMBOLS else '')!=currency
                or original.endswith('%')!=percent):continue
        try:
            amount=Decimal(_bare(original))
            rendered=currency+format(amount.quantize(Decimal(1).scaleb(-decimals),rounding=ROUND_HALF_UP),'f')+('%' if percent else '')
        except InvalidOperation:continue
        if rendered==shown:candidates.append(original)
    candidates=sorted(set(candidates))
    if len(candidates)!=1:
        raise MissionDeliverableConflict('number display lacks one exact source value under the declared rounding rule')
    return {'rule_ref':NUMBER_DISPLAY_RULE_REF,'source_value':candidates[0],'display_value':shown,
            'decimal_places':decimals,'rounding':'ROUND_HALF_UP','source_text_hash':content_hash(source_text)}


def validate_number_display(item: Mapping[str,Any], source_text: str) -> dict[str,Any]:
    display=item.get('display')
    if not isinstance(display,Mapping) or dict(display)!=number_display(source_text,str(display.get('display_value') or '')):
        raise MissionDeliverableConflict('number display does not replay its exact source and rounding rule')
    tokens=_display_tokens(str(item['text']))
    shown=str(display['display_value'])
    if shown not in [_normalise_number(t) for t in tokens]:
        raise MissionDeliverableConflict('number text does not state its declared display value')
    available=set(_display_tokens(source_text))|{shown,shown.lstrip(_CURRENCY_SYMBOLS)}
    if any(_normalise_number(t) not in available and _normalise_number(t).lstrip(_CURRENCY_SYMBOLS) not in available for t in tokens):
        raise MissionDeliverableConflict('number display text contains an unsourced extra figure')
    return dict(display)


def validate_pack_number(item: Mapping[str, Any],
                         resolve_number: Callable[[str], str | None] | None) -> dict[str, Any]:
    """A figure citing a material pack ``[N…]`` number: values must be in its text."""

    text = _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT)
    ref = _text(item["number_ref"], "number.number_ref", maximum=1024)
    source_text = resolve_number(ref) if resolve_number is not None else None
    if source_text is None:
        raise MissionDeliverableConflict(
            f"a figure cites a pack number that does not resolve in this publish: {ref}")
    if item.get('display') is not None:
        display=validate_number_display(item,source_text)
        return {"text":text,"number_ref":ref,"source_text":_text(source_text,"number.source_text",maximum=MAX_NUMBER_TEXT),"display":display,
                "period":None if item.get("period") is None else _text(item["period"],"number.period",maximum=120)}
    tokens = value_tokens(text, include_small=True)
    if not tokens:
        raise MissionDeliverableValidationError("a pack-number entry must state the figure it cites")
    available = _sourced_value_set(source_text)
    missing = [token for token in tokens if _normalise_number(token) not in available
               and _normalise_number(token).lstrip(_CURRENCY_SYMBOLS) not in available]
    if missing:
        raise MissionDeliverableConflict(
            f"a figure is not in the pack number it cites: {missing[:3]} not found in {ref}")
    return {"text": text, "number_ref": ref, "source_text": _text(source_text, "number.source_text",
                                                                   maximum=MAX_NUMBER_TEXT),
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120)}


def validate_estimate_number(item: Mapping[str, Any]) -> dict[str, Any]:
    """A figure that is 「我们的估计」, bound to a model assumption ref."""

    text = _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT)
    estimate = item["estimate"]
    ref = estimate.get("assumption_ref") if isinstance(estimate, Mapping) else estimate
    ref = _text(ref, "number.estimate.assumption_ref", maximum=512)
    if not ref.startswith(ESTIMATE_REF_PREFIXES):
        raise MissionDeliverableValidationError(
            "a 「我们的估计」 figure must bind a model assumption ref "
            f"({', '.join(ESTIMATE_REF_PREFIXES)})")
    if not value_tokens(text, include_small=True):
        raise MissionDeliverableValidationError("an estimate entry must state its figure")
    return {"text": text, "estimate": {"assumption_ref": ref},
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120)}


def unlabelled_estimates(body: str, numbers: Sequence[Mapping[str, Any]]) -> list[str]:
    """Estimate figures written in a sentence that does not say 「我们的估计」."""

    from .product_contract import OUR_ESTIMATE_RE, sentences

    estimates: set[str] = set()
    others: set[str] = set()
    for item in numbers:
        target = estimates if item.get("estimate") else others
        for token in value_tokens(str(item.get("text") or "")):
            target.add(_normalise_number(token))
    only_ours = estimates - others
    stray: list[str] = []
    for sentence in sentences(body):
        labelled = bool(OUR_ESTIMATE_RE.search(sentence))
        for token in value_tokens(sentence):
            if _normalise_number(token) in only_ours and not labelled:
                stray.append(token)
    return stray


def contract_findings(title: str, body: str, gap_items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The batch-D prose rules a strict section must meet."""

    from .product_contract import material_missing_findings, placeholder_findings
    from .product_substance import FAILED_STATUSES, section_substance

    findings = [{**item, "section": title} for item in material_missing_findings(body, gap_items)]
    findings += [{**item, "section": title} for item in placeholder_findings(body)]
    verdict = section_substance({"title": title, "body": body, "gaps": []})
    if verdict["status"] in FAILED_STATUSES:
        findings.append({"code": f"section_{verdict['status']}", "section": title,
                         "reason": verdict["reason"]})
    return findings


def pack_passage_resolver(
    index_resolver: Callable[[str], str | None] | None,
    pack_passages: Mapping[str, str] | None,
) -> Callable[[str], str | None] | None:
    """The evidence index first; then a material pack's own passage text, but
    only when that text still hashes to the ref (``hash12`` is the SHA-256 of
    the exact cited text), so a pack cannot vouch for words it altered."""

    if not pack_passages:
        return index_resolver
    import hashlib

    from .evidence_index import PassageRefInvalid, parse_passage_ref

    def resolve(ref: str) -> str | None:
        found = index_resolver(ref) if index_resolver is not None else None
        if found is not None:
            return found
        text = pack_passages.get(ref)
        if text is None:
            return None
        try:
            parts = parse_passage_ref(ref)
        except PassageRefInvalid:
            return None
        digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]
        return str(text) if digest == parts["hash12"] else None

    return resolve


def validate_section(
    section: Mapping[str, Any],
    *,
    live_claim_refs: set[str] | None = None,
    resolve_cell: Callable[[Mapping[str, Any]], bool] | None = None,
    imported: bool = False,
    resolve_passage: Callable[[str], str | None] | None = None,
    resolve_number: Callable[[str], str | None] | None = None,
    resolve_request: Callable[[str], Mapping[str, Any] | None] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """One closed section: a title, a body or a gap, and traceable numbers.

    Batch A: a figure may also cite a ``doc-passage:`` ref (``passage_ref``):
    every value in its ``text`` must appear in the cited passage's own words
    (``resolve_passage`` returns that text, or ``None`` when the ref does not
    resolve or was tampered with -- then the figure is refused).  Or it may be
    a declared ``derivation`` -- ``{"expression", "result"}`` -- whose every
    operand is a figure another entry of this section cites and whose result
    re-computes, to the precision written, to the figure in ``text``.

    ``resolve_cell`` answers "does this computed cell still exist in the Core".
    ``None`` means nobody can answer, and a cell citation is then refused
    rather than believed -- the same posture ``live_claim_refs=None`` takes for
    a Claim in the shape-only checks the golden set runs.

    ``imported`` relaxes exactly one rule and no others: a figure in the body
    that no citation reaches becomes a recorded gap instead of a refusal. An
    imported v0 is a prior document, not an assertion this system is making,
    and the alternative -- stripping the numbers out of an old screen so it
    would pass -- would destroy the thing being imported. Every other check
    still applies: a v0 may not cite a Claim that does not exist, may not cite
    a cell that does not resolve, and may not cite both at once. What it may
    do is carry its own numbers uncited, which is what a document written
    somewhere else always does.
    """

    if not isinstance(section, Mapping):
        raise MissionDeliverableValidationError("section must be an object")
    allowed = {"title", "body", "claim_refs", "numbers", "gaps", "gap_items", "source_refs"}
    if set(section) - allowed or "title" not in section:
        raise MissionDeliverableValidationError("section has an invalid closed shape")
    title = _text(section["title"], "section.title", maximum=200)
    body = section.get("body") or ""
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        raise MissionDeliverableValidationError("section.body must be text under 6000 characters")
    numbers = section.get("numbers") or []
    if not isinstance(numbers, list) or len(numbers) > MAX_SECTION_NUMBERS:
        raise MissionDeliverableValidationError(
            f"section.numbers must be a list of at most {MAX_SECTION_NUMBERS} entries")
    checked: list[dict[str, Any]] = []
    derived: list[Mapping[str, Any]] = []
    for item in numbers:
        if not isinstance(item, Mapping) or set(item) - {
            "text", "claim_version_ref", "period", "cell", "passage_ref", "derivation",
            "number_ref", "source_text", "estimate", "metric", "unit", "display",
        } or "text" not in item:
            raise MissionDeliverableValidationError(
                "number entries need text and either claim_version_ref or cell")
        cites_claim = item.get("claim_version_ref") is not None
        cites_cell = item.get("cell") is not None
        cites_passage = item.get("passage_ref") is not None
        derives = item.get("derivation") is not None
        cites_pack = item.get("number_ref") is not None
        estimates = item.get("estimate") is not None
        if item.get("display") is not None and not (cites_pack or estimates):
            raise MissionDeliverableValidationError("display precision requires an exact held pack number source")
        if cites_claim + cites_cell + cites_passage + derives + cites_pack + estimates != 1:
            # Both or neither. Neither is the old refusal; both would let a
            # figure claim two provenances and be checked against the more
            # convenient one.
            raise MissionDeliverableValidationError(
                "a number cites exactly one of claim_version_ref and cell "
                "(or passage_ref, or a declared derivation)")
        meta = {key: _text(item[key], f"number.{key}", maximum=120)
                for key in ("metric", "unit") if item.get(key) is not None}
        if derives:
            derived.append(item)
            continue
        if cites_passage:
            checked.append({**validate_passage_number(item, resolve_passage), **meta})
            continue
        if cites_pack:
            checked.append({**validate_pack_number(item, resolve_number), **meta})
            continue
        if estimates:
            estimate=validate_estimate_number(item)
            if item.get('display') is not None:
                ref=estimate['estimate']['assumption_ref']
                source=resolve_number(ref) if resolve_number else None
                if source is None:raise MissionDeliverableConflict('displayed estimate lacks its exact held number source')
                estimate['display']=validate_number_display(item,source)
                estimate['source_text']=source
            checked.append({**estimate, **meta})
            continue
        if cites_cell:
            cell = validate_cell_citation(item["cell"])
            if resolve_cell is None or not resolve_cell(cell):
                raise MissionDeliverableConflict(
                    "a figure cites a computed cell that does not resolve in this "
                    f"Core: {cell['ref']}"
                )
            checked.append({
                "text": _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT),
                "cell": cell,
                "period": (None if item.get("period") is None
                           else _text(item["period"], "number.period", maximum=120)),
            })
            continue
        ref = _text(item["claim_version_ref"], "number.claim_version_ref", maximum=512)
        if live_claim_refs is not None and ref not in live_claim_refs:
            raise MissionDeliverableConflict(
                "a figure cites a Claim that is retired or does not exist: " + ref
            )
        checked.append({
            # P13ac: a number's text is the Claim's own normalized_statement,
            # and the Claim contract puts no ceiling on that. 200 was narrower
            # than what the system legitimately produces: the SEC lane writes
            # "EPAM SYSTEMS, INC. reported Revenue from Contract with Customer,
            # Excluding Assessed Tax of ..." at 205 characters, so every EPAM
            # figure failed here and the Initial Screen could not be published
            # at all -- permanently, because Claims are append-only and cannot
            # be shortened after the fact.
            #
            # Eliding instead of widening would be worse: unsourced_numbers
            # reads the figures back out of this text, and these statements put
            # the figure last, so a truncation would drop the number and then
            # report the body that cites it as unsourced.
            #
            # The document stays bounded by the 60-entry cap above; this bound
            # is only here so one statement cannot be a document.
            "text": _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT),
            "claim_version_ref": ref,
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120),
        })
    # Prove arithmetic before accepting any peer result in a range's wording.
    # A peer's unverified prose is never an operand source.
    pending=list(derived);proven=[]
    while pending:
        progress=False;last_error=None
        for item in list(pending):
            trusted=[*checked,*({'text':d['derivation']['result']} for d in proven)]
            try:proof=validate_derived_number(item,trusted,check_stray=False)
            except MissionDeliverableConflict as exc:
                if 'a derivation uses ' not in str(exc):raise
                last_error=exc;continue
            proven.append(proof);pending.remove(item);progress=True
        if not progress:raise last_error or MissionDeliverableConflict('derivation sources are unavailable or circular')
    trusted=[*checked,*({'text':d['derivation']['result']} for d in proven)]
    for item in derived:
        meta = {key: _text(item[key], f"number.{key}", maximum=120)
                for key in ("metric", "unit") if item.get(key) is not None}
        checked.append({**validate_derived_number(item, trusted), **meta})
    claim_refs = section.get("claim_refs") or []
    if not isinstance(claim_refs, list) or len(claim_refs) > 200 or any(
        not isinstance(ref, str) or not ref for ref in claim_refs
    ):
        raise MissionDeliverableValidationError("section.claim_refs must be a list of refs")
    if live_claim_refs is not None:
        missing = [ref for ref in claim_refs if ref not in live_claim_refs]
        if missing:
            raise MissionDeliverableConflict(
                "a section cites Claims that are retired or do not exist: " + ", ".join(missing[:3])
            )
    gaps = section.get("gaps") or []
    if not isinstance(gaps, list) or len(gaps) > 20 or any(
        not isinstance(gap, str) or not gap.strip() for gap in gaps
    ):
        raise MissionDeliverableValidationError("section.gaps must be a list of short strings")
    from .product_contract import ProductContractError, validate_gap_item

    raw_items = section.get("gap_items") or []
    if not isinstance(raw_items, list) or len(raw_items) > 20:
        raise MissionDeliverableValidationError("section.gap_items must be a list of at most 20")
    try:
        gap_items = [validate_gap_item(item, f"section「{title}」.gap_items[{index}]")
                     for index, item in enumerate(raw_items)]
    except ProductContractError as exc:
        raise MissionDeliverableValidationError(str(exc)) from exc
    from .material_request_citation import without_verified_request_tokens
    checked_body = without_verified_request_tokens(body, gap_items, resolve_request)
    stray = unsourced_numbers(checked_body, checked)
    if stray and not imported:
        raise MissionDeliverableConflict(
            f"section「{title}」carries figures with no source behind them: {stray[:5]}；"
            f"写 {GAP_MARKER} 而不是猜一个数字"
        )
    unlabelled = unlabelled_estimates(body, checked)
    if unlabelled and not imported:
        raise MissionDeliverableConflict(
            f"section「{title}」writes our estimate {unlabelled[:3]} without saying 「我们的估计」")
    if strict and not imported:
        problems = contract_findings(title, body, gap_items)
        if problems:
            raise MissionDeliverableConflict(
                f"section「{title}」breaks the product contract: "
                + "；".join(f"{item['code']}（{item.get('excerpt') or item.get('matched') or item.get('reason') or ''}）"
                           for item in problems[:3]))
    recorded = [gap.strip()[:300] for gap in gaps]
    if stray and imported:
        recorded.append(f"{IMPORTED_FIGURE_GAP}：{'、'.join(stray[:5])}")
    out = {
        "title": title, "body": body.strip(), "claim_refs": list(dict.fromkeys(claim_refs)),
        "numbers": checked, "gaps": recorded[:20],
    }
    if gap_items:
        out["gap_items"] = gap_items
    source_refs = section.get("source_refs") or []
    if not isinstance(source_refs, list) or len(source_refs) > 200:
        raise MissionDeliverableValidationError("section.source_refs must be a list of at most 200")
    if source_refs:
        # Batch D: what the section read and cited beyond figures -- passage,
        # pack number, our-view refs with their source kind and date -- so the
        # quality score can count source kinds and find the latest transcript
        # without the pack.
        checked_sources = []
        for index, item in enumerate(source_refs):
            if not isinstance(item, Mapping) or "ref" not in item or set(item) - {
                    "ref", "tag", "source_kind", "as_of", "company_ref", "title",
                    "company_refs", "doc_kind", "document_ref"}:
                raise MissionDeliverableValidationError(
                    f"section.source_refs[{index}] must be {{ref, tag?, source_kind?, as_of?, company_ref?, title?, company_refs?, doc_kind?, document_ref?}}")
            checked_source = {key: _text(str(item[key]), f"section.source_refs[{index}].{key}", maximum=1024)
                              for key in item if key not in {"company_refs", "doc_kind", "document_ref"}
                              and item[key] is not None and str(item[key]).strip()}
            for key in ("doc_kind", "document_ref"):
                if key in item:
                    checked_source[key] = _text(item[key], f"section.source_refs[{index}].{key}", maximum=1024)
            if "company_refs" in item:
                refs = item["company_refs"]
                # Resource bound only. These document associations neither
                # identify a sentence's subject nor authorize an issuer fact.
                if not isinstance(refs, list) or not 1 <= len(refs) <= 256:
                    raise MissionDeliverableValidationError(
                        f"section.source_refs[{index}].company_refs must be a list of 1..256 strings")
                checked_source["company_refs"] = [
                    _text(ref, f"section.source_refs[{index}].company_refs[{position}]", maximum=1024)
                    for position, ref in enumerate(refs)]
            checked_sources.append(checked_source)
        out["source_refs"] = checked_sources
    return out


class MissionDeliverableAuthority:
    """Append-only, hash-bound documents, refused when a number has no source."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_deliverable_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._widen_kind_check()
        self._admit_version_zero()

    def _admit_version_zero(self) -> None:
        """W3: admit v0 on a Core built when 1 was the floor.

        This and ``_widen_kind_check`` rebuild the same table, so each must
        write *both* constraints from the same source: the kind list from
        ``_kind_check_list()`` and the version floor as ``>= 0``. Restating
        either as a literal makes whichever migration runs second silently
        undo the other -- which is exactly what happened when this one shipped
        with the eight kinds it knew about frozen into it.

        Same shape and the same reason as ``_widen_kind_check`` above: the
        constraint lives in the table, ``CREATE TABLE IF NOT EXISTS`` does not
        revisit a table that exists, and an unmigrated Core would refuse the
        import with an ``IntegrityError`` rather than a sentence. Sentinelled
        on the constraint text itself, so this runs at most once.
        """

        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='mission_deliverable_versions'"
        ).fetchone()
        if row is None or "version_number >= 0" in (row["sql"] or ""):
            return
        if self.connection.in_transaction:
            raise MissionDeliverableConflict(
                "the deliverable version-zero migration requires no open transaction"
            )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS mission_deliverables_authorized_insert;
                DROP TRIGGER IF EXISTS mission_deliverables_no_update;
                DROP TRIGGER IF EXISTS mission_deliverables_no_delete;
                CREATE TABLE mission_deliverable_versions_v3 (
                    version_id TEXT PRIMARY KEY,
                    deliverable_ref TEXT NOT NULL,
                    version_number INTEGER NOT NULL CHECK(version_number >= 0),
                    prior_version_ref TEXT REFERENCES mission_deliverable_versions_v3(version_id),
                    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
                    mission_version_hash TEXT NOT NULL,
                    playbook_version_ref TEXT NOT NULL,
                    playbook_version_hash TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN (
                        __KINDS__
                    )),
                    subject_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deliverable_ref, version_number)
                );
                INSERT INTO mission_deliverable_versions_v3 SELECT * FROM mission_deliverable_versions;
                DROP TABLE mission_deliverable_versions;
                ALTER TABLE mission_deliverable_versions_v3 RENAME TO mission_deliverable_versions;
                CREATE INDEX IF NOT EXISTS idx_mission_deliverables_by_subject
                ON mission_deliverable_versions(mission_version_ref, subject_ref, kind, created_at);
                CREATE TRIGGER mission_deliverables_authorized_insert
                BEFORE INSERT ON mission_deliverable_versions
                WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
                    SELECT RAISE(ABORT, 'mission deliverable insert requires MissionDeliverableAuthority'); END;
                CREATE TRIGGER mission_deliverables_no_update
                BEFORE UPDATE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                CREATE TRIGGER mission_deliverables_no_delete
                BEFORE DELETE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                COMMIT;
                """.replace("__KINDS__", _kind_check_list())
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MissionDeliverableConflict(
                "the deliverable version-zero migration broke foreign keys"
            )

    def _widen_kind_check(self) -> None:
        """Admit a kind on a Core built before that kind existed.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that is already
        there, so a Core created under the seven-kind CHECK keeps refusing the
        eighth for ever -- with an ``IntegrityError`` from the constraint
        rather than the readable refusal the vocabulary check above gives.
        Live holds a handful of deliverables, so the rebuild is small; it
        follows ``DaltonStore._migrate_thesis_authority_columns`` exactly,
        including the foreign-key check afterwards, because a rebuild that
        silently orphaned the pointer would be worse than the constraint.

        P14a wrote this for ``event_note`` and keyed it on that one literal;
        P12d added ``deep_insight_gate`` and moved the sentinel to it.  A
        sentinel is one kind, and three slices adding kinds in the same week is
        how a Core ends up having had one migration and not the next: whichever
        sentinel it already carries, it is left alone.  So the condition is now
        *every* kind in :data:`DELIVERABLE_KINDS`, and the rebuilt CHECK is
        generated from that tuple rather than written out again.  Adding a kind
        is a line in one place.
        """

        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='mission_deliverable_versions'"
        ).fetchone()
        if row is None:
            return
        current = row["sql"] or ""
        if all(f"'{kind}'" in current for kind in DELIVERABLE_KINDS):
            return
        if self.connection.in_transaction:
            raise MissionDeliverableConflict(
                "the deliverable kind migration requires no open transaction"
            )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS mission_deliverables_authorized_insert;
                DROP TRIGGER IF EXISTS mission_deliverables_no_update;
                DROP TRIGGER IF EXISTS mission_deliverables_no_delete;
                CREATE TABLE mission_deliverable_versions_v2 (
                    version_id TEXT PRIMARY KEY,
                    deliverable_ref TEXT NOT NULL,
                    version_number INTEGER NOT NULL CHECK(version_number >= 0),
                    prior_version_ref TEXT REFERENCES mission_deliverable_versions_v2(version_id),
                    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
                    mission_version_hash TEXT NOT NULL,
                    playbook_version_ref TEXT NOT NULL,
                    playbook_version_hash TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN (
                        __KINDS__
                    )),
                    subject_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deliverable_ref, version_number)
                );
                INSERT INTO mission_deliverable_versions_v2 SELECT * FROM mission_deliverable_versions;
                DROP TABLE mission_deliverable_versions;
                ALTER TABLE mission_deliverable_versions_v2 RENAME TO mission_deliverable_versions;
                CREATE INDEX IF NOT EXISTS idx_mission_deliverables_by_subject
                ON mission_deliverable_versions(mission_version_ref, subject_ref, kind, created_at);
                CREATE TRIGGER mission_deliverables_authorized_insert
                BEFORE INSERT ON mission_deliverable_versions
                WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
                    SELECT RAISE(ABORT, 'mission deliverable insert requires MissionDeliverableAuthority'); END;
                CREATE TRIGGER mission_deliverables_no_update
                BEFORE UPDATE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                CREATE TRIGGER mission_deliverables_no_delete
                BEFORE DELETE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                COMMIT;
                """.replace("__KINDS__", _kind_check_list())
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MissionDeliverableConflict(
                "the deliverable kind migration broke foreign keys"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("MissionDeliverableAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads ---------------------------------------------------------------

    def live_claim_version_refs(self) -> set[str]:
        """Claim versions a document may cite: everything the Ledger holds, less retirements."""

        refs = {
            row["claim_version_id"]
            for row in self.connection.execute("SELECT claim_version_id FROM claim_versions").fetchall()
        }
        from .claim_retirement import retired_claim_version_refs

        # Retired less reinstated (2026-09-24).
        return refs - retired_claim_version_refs(self.connection)

    def cell_resolver(self) -> Callable[[Mapping[str, Any]], bool]:
        """Whether a computed cell a figure cites still exists in this Core.

        Two kinds, two reads, and both fail closed on a Core that has never
        opened the authority in question: an unresolvable cell is not a cell
        that has been shown to exist, and the whole reason a figure is allowed
        to cite one is that this Core can re-derive it.

        A statement accession is checked for existence and not for the cell's
        arithmetic. That is the right depth here: the arithmetic is frozen in
        ``industry_framework.build_comparison`` and re-run from the same filed
        lines every time the table is built, so what a document needs to know
        is that the filing behind the number is still held.
        """

        def resolve(cell: Mapping[str, Any]) -> bool:
            try:
                if cell["kind"] == "statement_accession":
                    row = self.connection.execute(
                        "SELECT 1 FROM coverage_mission_statement_filings "
                        "WHERE accession=? LIMIT 1", (cell["accession"],),
                    ).fetchone()
                    return row is not None
                if cell["kind"] == "valuation_metric":
                    # The version has to be held *and* still carry the metric
                    # the ref names. A snapshot whose inputs went missing
                    # publishes the metric as ``unavailable`` with no value,
                    # and a document citing it would be citing a blank.
                    row = self.connection.execute(
                        "SELECT record_json FROM valuation_snapshot_versions "
                        "WHERE version_id=?", (cell["version_ref"],),
                    ).fetchone()
                    if row is None:
                        return False
                    record = json.loads(row["record_json"])
                    wanted = str(cell["ref"]).rsplit(":", 1)[-1]
                    return any(
                        item.get("metric") == wanted
                        and item.get("status") == "available"
                        and item.get("value") is not None
                        for item in record.get("metrics") or ()
                    )
                row = self.connection.execute(
                    "SELECT record_json FROM forecast_model_versions WHERE version_id=?",
                    (cell["version_ref"],),
                ).fetchone()
                if row is None:
                    return False
                record = json.loads(row["record_json"])
                for line in record.get("results") or ():
                    for item in line.get("cells") or ():
                        if item.get("ref") == cell["ref"]:
                            return True
                return False
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                return False
            except (ValueError, KeyError, TypeError):
                return False

        return resolve

    def passage_resolver(self) -> Callable[[str], str | None] | None:
        """Batch A: ``doc-passage:`` ref -> the cited text, from the evidence index
        beside this Core (``document-index.sqlite``); ``None`` for an in-memory
        Core, so a passage citation there is refused rather than believed."""

        if self.store.path == ":memory:":
            return None
        from .evidence_index import passage_resolver

        return passage_resolver(Path(self.store.path).parent)

    def request_resolver(self, subject_ref: str):
        if self.store.path == ":memory:":
            return None
        from .material_request_citation import request_citation_resolver
        return request_citation_resolver(Path(self.store.path).parent, subject_ref)

    def latest(self, deliverable_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM mission_deliverable_pointer p JOIN mission_deliverable_versions v "
            "ON v.version_id=p.version_id WHERE p.deliverable_ref=?",
            (_text(deliverable_ref, "deliverable_ref", maximum=512),),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise MissionDeliverableConflict("mission deliverable authority drifted")
        return record

    def deliverables(
        self, mission_version_ref: str, *, kind: str | None = None, subject_ref: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT v.record_json AS record_json FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "WHERE v.mission_version_ref=?"
        )
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref", maximum=512)]
        if kind is not None:
            query += " AND v.kind=?"
            params.append(kind)
        if subject_ref is not None:
            query += " AND v.subject_ref=?"
            params.append(subject_ref)
        query += " ORDER BY v.created_at, v.version_id"
        return [json.loads(row["record_json"]) for row in self.connection.execute(query, params).fetchall()]

    # -- write ---------------------------------------------------------------

    def publish(
        self,
        *,
        kind: str,
        subject_ref: str,
        mission: Mapping[str, Any],
        playbook: Mapping[str, Any],
        template_ref: str,
        sections: Sequence[Mapping[str, Any]],
        summary: str,
        gaps: Sequence[str] = (),
        model_invocation_refs: Sequence[str] = (),
        actor_ref: str,
        idempotency_key: str | None = None,
        revision: Mapping[str, Any] | None = None,
        as_version_zero: bool = False,
        gate: Mapping[str, Any] | None = None,
        pack_numbers: Mapping[str, str] | None = None,
        pack_passages: Mapping[str, str] | None = None,
        contract: Mapping[str, Any] | None = None,
        material_pack: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store the next version of one deliverable chain.

        ``as_version_zero`` is W3's import: this document existed before the
        system did, so it goes in at 0 and the first thing the system writes
        goes in at 1 pointing back at it. It is refused on a chain that
        already has a version, because a chain can only start once, and it
        requires ``change_reason: imported_prior``.
        """

        if kind not in DELIVERABLE_KINDS:
            raise MissionDeliverableValidationError(f"kind must be one of {list(DELIVERABLE_KINDS)}")
        subject_ref = _text(subject_ref, "subject_ref", maximum=256)
        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not (actor_ref.startswith("human:") or actor_ref.startswith("automation:")):
            raise MissionDeliverableValidationError("actor_ref must be a human: or automation: principal")
        if actor_ref.startswith("automation:"):
            if actor_ref != mission["autonomy"]["automation_principal"]:
                raise MissionDeliverableConflict("automation actor is not the mission principal")
            if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
                raise MissionDeliverableConflict(
                    f"mission does not grant {WRITE_SCOPE} writes to automation"
                )
        if kind != "industry_framework" and not any(
            member["company_ref"] == subject_ref for member in mission["universe"]
        ) and subject_ref != mission["industry_ref"]:
            raise MissionDeliverableConflict("subject is not in the mission universe")
        if not isinstance(sections, Sequence) or not 1 <= len(sections) <= MAX_SECTIONS:
            raise MissionDeliverableValidationError(f"sections must be 1..{MAX_SECTIONS} entries")
        if as_version_zero:
            reason = (revision or {}).get("change_reason")
            if reason != IMPORT_CHANGE_REASON:
                raise MissionDeliverableValidationError(
                    f"a version zero must carry change_reason {IMPORT_CHANGE_REASON!r}: "
                    "it is not a revision of anything, it is where the chain starts"
                )
        elif (revision or {}).get("change_reason") == IMPORT_CHANGE_REASON:
            raise MissionDeliverableValidationError(
                f"{IMPORT_CHANGE_REASON!r} is only legal on an imported version zero"
            )
        live = self.live_claim_version_refs()
        resolve_cell = self.cell_resolver()
        resolve_passage = pack_passage_resolver(self.passage_resolver(), pack_passages)
        # Batch D: a version drafted under the product contract names it; the
        # strict prose rules (substance, 「材料缺少」 binding, placeholders) then
        # apply to every section, and a failed section refuses the version --
        # an empty section is never a publication channel (ap 1).
        strict = contract is not None and kind in CONTRACT_STRICT_KINDS and not as_version_zero
        numbers_by_ref = {str(key): str(value) for key, value in (pack_numbers or {}).items()}
        resolve_number = (numbers_by_ref.get if pack_numbers is not None else None)
        resolve_request = self.request_resolver(subject_ref)
        checked = [
            validate_section(section, live_claim_refs=live,
                             resolve_cell=resolve_cell, imported=as_version_zero,
                             resolve_passage=resolve_passage,
                             resolve_number=resolve_number, resolve_request=resolve_request, strict=strict)
            for section in sections
        ]
        if not any(section["body"] for section in checked):
            raise MissionDeliverableConflict("a deliverable with no written section is an empty shell")
        summary = _text(summary, "summary", maximum=2000)
        slug = subject_ref.rsplit(":", 1)[-1]
        deliverable_ref = f"mission-deliverable:{kind}:{slug}"
        record = {
            "schema_version": SCHEMA_VERSION,
            "deliverable_ref": deliverable_ref,
            "kind": kind,
            "subject_ref": subject_ref,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "playbook_version_ref": playbook["id"],
            "playbook_version_hash": playbook["content_hash"],
            "template_ref": _text(template_ref, "template_ref", maximum=256),
            "summary": summary,
            "sections": checked,
            "gaps": [str(gap).strip()[:300] for gap in gaps][:40],
            "model_invocation_refs": [str(ref)[:512] for ref in model_invocation_refs][:40],
            "actor_ref": actor_ref,
            "created_at": self.clock(),
            # P14d / ADR-0008: why this version exists, when it exists because
            # somebody decided it should.  ``None`` on a first draft and on the
            # ordinary "there are new Claims" redraft, which needs no reason
            # beyond the Claims; a value here means a human checkpoint was
            # passed and names the refs that occasioned it.
            "revision": validate_revision(revision),
        }
        if material_pack is not None:
            # Batch C's ``MaterialPack.record()``: what redraft_policy reads (its
            # watermark) to decide whether new material justifies a redraft.
            record["material_pack"] = json.loads(json.dumps(dict(material_pack), ensure_ascii=False,
                                                            default=str))
        if contract is not None:
            # Which contract, material pack and worksheet this version was
            # drafted from. In the body hash: a different pack is a different
            # document even when the prose happens to agree.
            record["contract"] = json.loads(json.dumps(dict(contract), ensure_ascii=False))
        if gate is not None and as_version_zero and gate.get("passed"):
            # A version zero is a document this system did not write, so no
            # reading of it can be a pass of this system's exit gate. A raise
            # rather than an assert: assertions vanish under -O, and this is
            # the one place a caller could quietly turn an imported document
            # into a passed screen.
            raise MissionDeliverableValidationError(
                "an imported version zero cannot carry a passed gate; its "
                "items are 'imported', not 'passed'"
            )
        if gate is not None:
            # P10c's exit-gate self-assessment, stored rather than left to
            # live as one line of prose in a stage record's rationale. Out of
            # the body hash below on purpose: a gate is a reading of a
            # document, not part of it, and re-reading it must not mint a
            # version.
            record["gate"] = json.loads(json.dumps(dict(gate), ensure_ascii=False))
        # What the document says, without its version number or timestamp: an
        # unchanged body is a duplicate, not a new version.
        #
        # ``revision`` is deliberately not in the body hash.  A re-issue that
        # produces the identical document is still a duplicate, however good
        # the reason was: ADR-0008 refuses "a rewrite of an unchanged world",
        # and a change_reason is not a change.
        record["body_hash"] = content_hash({
            key: record[key] for key in (
                "kind", "subject_ref", "mission_version_ref", "mission_version_hash",
                "playbook_version_ref", "playbook_version_hash", "template_ref",
                "summary", "sections", "gaps",
            ) + (("contract",) if "contract" in record else ())
            + (("material_pack",) if "material_pack" in record else ())
        })
        with self._transaction() as cur:
            pointer = cur.execute(
                "SELECT version_id, version_number, content_hash FROM mission_deliverable_pointer "
                "WHERE deliverable_ref=?", (deliverable_ref,),
            ).fetchone()
            if as_version_zero:
                version, prior = 0, None
            else:
                version = 1 if pointer is None else int(pointer["version_number"]) + 1
                prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = f"mission-deliverable-version:{content_hash({'ref': deliverable_ref, 'version': version})[:32]}"
            record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            if pointer is not None:
                existing = cur.execute(
                    "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?",
                    (pointer["version_id"],),
                ).fetchone()
                current = json.loads(existing["record_json"])
                if current.get("body_hash") == record["body_hash"]:
                    return {**current, "status": "duplicate"}
            if as_version_zero and pointer is not None:
                # Deliberately *after* the duplicate checks above and the
                # idempotency check below: re-running the import of a document
                # that is already the chain's v0 has to be a no-op, because a
                # lane that reads a corpus every tick will re-offer the same
                # screen for ever. What this refuses is the other case -- a
                # *different* prior document arriving at a chain that already
                # started -- and that one is a conflict, because a chain can
                # only begin once and the second document is evidence, not an
                # origin.
                seen_import = cur.execute(
                    "SELECT version_id FROM mission_deliverable_versions "
                    "WHERE deliverable_ref=? AND version_number=0", (deliverable_ref,),
                ).fetchone()
                if seen_import is not None and idempotency_key is not None:
                    existing = cur.execute(
                        "SELECT record_json FROM mission_deliverable_versions "
                        "WHERE version_id=? AND json_extract(record_json,'$.idempotency_key')=?",
                        (seen_import["version_id"],
                         _text(idempotency_key, "idempotency_key", maximum=512)),
                    ).fetchone()
                    if existing is not None:
                        return {**json.loads(existing["record_json"]),
                                "status": "duplicate"}
                raise MissionDeliverableConflict(
                    "this chain already has a version; a prior document can "
                    "only be imported into a chain that has not started"
                )
            if idempotency_key is not None:
                key = _text(idempotency_key, "idempotency_key", maximum=512)
                seen = cur.execute(
                    "SELECT version_id FROM mission_deliverable_versions WHERE deliverable_ref=? "
                    "AND json_extract(record_json,'$.idempotency_key')=?", (deliverable_ref, key),
                ).fetchone()
                if seen is not None:
                    existing = cur.execute(
                        "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?",
                        (seen["version_id"],),
                    ).fetchone()
                    return {**json.loads(existing["record_json"]), "status": "duplicate"}
                record["idempotency_key"] = key
                record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            cur.execute(
                "INSERT INTO mission_deliverable_versions(version_id,deliverable_ref,version_number,"
                "prior_version_ref,mission_version_ref,mission_version_hash,playbook_version_ref,"
                "playbook_version_hash,kind,subject_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], deliverable_ref, version, prior, mission["id"], mission["content_hash"],
                 playbook["id"], playbook["content_hash"], kind, subject_ref,
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO mission_deliverable_pointer(deliverable_ref,version_id,version_number,"
                    "content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (deliverable_ref, record["id"], version, record["content_hash"], record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE mission_deliverable_pointer SET version_id=?, version_number=?, "
                    "content_hash=?, updated_at=? WHERE deliverable_ref=?",
                    (record["id"], version, record["content_hash"], record["created_at"], deliverable_ref),
                )
        return {**record, "status": "fresh"}

    def publish_human_erratum(
        self, *, permission: Mapping[str, Any], mission: Mapping[str, Any],
        playbook: Mapping[str, Any], actor_ref: str,
    ) -> dict[str, Any]:
        """Append the exact five substitutions authorized by one erratum reopen."""
        proposal = permission.get("proposal") or {}
        erratum = proposal.get("erratum") or {}
        if (permission.get("verdict") != "approve"
                or permission.get("proposal_ref") != proposal.get("id")
                or permission.get("proposal_hash") != proposal.get("content_hash")
                or proposal.get("change_reason") != "human_revision"
                or proposal.get("flipped") != []):
            raise MissionDeliverableConflict("an exact approved human erratum is required")
        proposal_row = self.connection.execute(
            "SELECT record_json,content_hash FROM gate_reopen_proposals WHERE proposal_id=?",
            (proposal.get("id"),),).fetchone()
        decision_row = self.connection.execute(
            "SELECT record_json,content_hash FROM gate_reopen_decisions WHERE decision_id=? AND proposal_ref=?",
            (permission.get("id"),proposal.get("id")),).fetchone()
        if (proposal_row is None or proposal_row["content_hash"] != proposal.get("content_hash")
                or json.loads(proposal_row["record_json"]) != proposal
                or decision_row is None or decision_row["content_hash"] != permission.get("content_hash")
                or json.loads(decision_row["record_json"]) != {k:v for k,v in permission.items() if k != "proposal"}):
            raise MissionDeliverableConflict("erratum approval authority did not read back exactly")
        required = {"candidate_file_sha256","candidate_hash","source_version_ref",
            "source_version_hash","source_version_number","section_index","before_sha256",
            "after_sha256","substitutions","claim_authorities"}
        if set(erratum) != required:
            raise MissionDeliverableValidationError("erratum proof has an unexpected shape")
        if (proposal.get("mission_version_ref") != mission.get("id")
                or proposal.get("mission_version_hash") != mission.get("content_hash")):
            raise MissionDeliverableConflict("erratum mission binding failed")
        substitutions = erratum["substitutions"]
        if not isinstance(substitutions, list) or len(substitutions) != 5:
            raise MissionDeliverableValidationError("this erratum requires exactly five substitutions")
        for claim in erratum["claim_authorities"]:
            if not isinstance(claim, Mapping) or set(claim) != {"claim_version_ref","claim_content_hash"}:
                raise MissionDeliverableValidationError("erratum Claim proof is not closed")
            claim_row=self.connection.execute("SELECT content_hash FROM claim_versions WHERE claim_version_id=?",
                (claim["claim_version_ref"],)).fetchone()
            if claim_row is None or claim_row["content_hash"] != claim["claim_content_hash"]:
                raise MissionDeliverableConflict("erratum Claim authority changed")
        row = self.connection.execute(
            "SELECT * FROM mission_deliverable_versions WHERE version_id=?",
            (erratum["source_version_ref"],),
        ).fetchone()
        if (row is None or row["content_hash"] != erratum["source_version_hash"]
                or int(row["version_number"]) != erratum["source_version_number"]):
            raise MissionDeliverableConflict("erratum source version binding failed")
        source = json.loads(row["record_json"])
        index = erratum["section_index"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(source["sections"]):
            raise MissionDeliverableValidationError("erratum section index is invalid")
        before = source["sections"][index]["body"]
        if hashlib.sha256(before.encode()).hexdigest() != erratum["before_sha256"]:
            raise MissionDeliverableConflict("erratum before bytes changed")
        after = before
        for change in substitutions:
            if not isinstance(change, Mapping) or set(change) != {"before", "after"}:
                raise MissionDeliverableValidationError("erratum substitutions are not closed")
            old, new = change["before"], change["after"]
            if not isinstance(old, str) or not isinstance(new, str) or after.count(old) != 1:
                raise MissionDeliverableConflict("erratum substitution is not unique")
            after = after.replace(old, new, 1)
        if hashlib.sha256(after.encode()).hexdigest() != erratum["after_sha256"]:
            raise MissionDeliverableConflict("erratum after bytes do not match")
        sections = copy.deepcopy(source["sections"]); sections[index]["body"] = after
        latest = self.latest(row["deliverable_ref"])
        idem = "human-erratum:" + erratum["candidate_hash"]
        if latest is not None and latest["id"] != source["id"]:
            if (latest.get("prior_version_ref") == source["id"]
                    and latest.get("idempotency_key") == idem
                    and latest.get("version") == source["version"] + 1
                    and latest.get("sections") == sections
                    and latest.get("revision", {}).get("reopen_ref") == proposal["id"]):
                return {**latest, "status": "duplicate"}
            raise MissionDeliverableConflict("erratum source is no longer the chain head")
        if latest is None or latest["content_hash"] != source["content_hash"]:
            raise MissionDeliverableConflict("erratum source is no longer the chain head")
        revision = {"change_reason":"human_revision","reopen_ref":proposal["id"],
            "evidence_refs":[permission["id"],proposal["id"],source["id"],
                *[row["claim_version_ref"] for row in erratum["claim_authorities"]]]}
        result = self.publish(kind=source["kind"],subject_ref=source["subject_ref"],
            mission=mission,playbook=playbook,template_ref=source["template_ref"],
            sections=sections,summary=source["summary"],gaps=source["gaps"],
            model_invocation_refs=source["model_invocation_refs"],actor_ref=actor_ref,
            revision=revision,idempotency_key=idem)
        if result["version"] != source["version"] + 1 or result["sections"] != sections:
            raise MissionDeliverableConflict("erratum append did not preserve the source document")
        return result


__all__ = [
    "CELL_SOURCE_KINDS",
    "DELIVERABLE_KINDS",
    "GAP_MARKER",
    "IMPORTED_FIGURE_GAP",
    "IMPORT_CHANGE_REASON",
    "deliverable_change_reasons",
    "MissionDeliverableAuthority",
    "MissionDeliverableConflict",
    "MissionDeliverableError",
    "MissionDeliverableNotFound",
    "MissionDeliverableValidationError",
    "english_number_tokens",
    "number_source_contract_fingerprint",
    "REVISION_FIELDS",
    "WRITE_SCOPE",
    "unsourced_numbers",
    "validate_cell_citation",
    "validate_revision",
    "validate_section",
    "value_tokens",
    "CONTRACT_STRICT_KINDS",
    "ESTIMATE_REF_PREFIXES",
    "contract_findings",
    "unlabelled_estimates",
    "validate_estimate_number",
    "validate_pack_number",
    "pack_passage_resolver",
]
