"""Hash-bound, rebuildable Chinese display attachments for research products.

The attachment is presentation data.  It never replaces the research product,
its approval state, source refs, figures or version identity.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .cockpit_model import register_purpose
from .final_text_contract import FINAL_TEXT_RULES_VERSION, final_text_instructions
from .numeric_display import format_display_number

SCHEMA_VERSION = "research-localization:0.1"
TARGET_LOCALE = "zh-CN"
LEGACY_FINAL_TEXT_RULES_VERSIONS = frozenset({"simplified-chinese-research-prose:0.2"})
VERIFIER_PURPOSE = register_purpose("research_localization_verifier")
# A sign only where a sign can stand: "mid-2026" and "2025-2026" carry a
# hyphen, not a negative 2026 (2026-10-09, a UI batch blocked six times on
# 「2026 年中」 losing a "-2026" it never had).
_NUMBER = re.compile(r"(?:(?<![A-Za-z0-9])[+-])?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_ISO_YEAR_MONTH = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?![-\d])")
_SAME_YEAR_ISO_RANGE = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})\s*(?:\.\.|至|to)\s*\1-(\d{2})-(\d{2})(?!\d)",
    re.IGNORECASE,
)
_CALENDAR_QUARTER_RANGE = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})\s*(?:\.\.|\u81f3|to)\s*\1-(\d{2})-(\d{2})(?!\d)",
    re.IGNORECASE,
)
_CHINESE_CALENDAR_QUARTER_RANGE = re.compile(
    r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*"
    r"(?:至|到)\s*(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_CHINESE_CALENDAR_QUARTER = re.compile(
    r"(?<!\d)(\d{4})\s*\u5e74\s*\u7b2c?\s*([\u4e00\u4e8c\u4e09\u56db1-4])\s*\u5b63\u5ea6"
)
_LATIN_CALENDAR_QUARTER = re.compile(
    r"(?<!\d)(\d{4})\s*\u5e74?\s*Q\s*([1-4])(?!\d)", re.IGNORECASE
)
_OPAQUE_ID = re.compile(
    r"\b(?:claim|claim-version|dossier|dossier-version|memo|memo-version|"
    r"debate|debate-map|forecast-model-version|company-model-spec|mission|"
    r"mission-version|thesis|thesis-version|event|document|document-version)"
    r":[A-Za-z0-9:._-]+|\b[0-9a-f]{64}\b|(?<![A-Za-z0-9])[ST][0-9]+(?![A-Za-z0-9])|\bcausal_chain:[0-9]+\b|因果链[0-9]+"
    # 2026-10-08 (batch w): an SEC accession number is a source locator, not
    # a value -- the drafting prompt says not to reproduce source refs, and on
    # 10-07 most UI-text drafts that obeyed failed here on ``1467373``,
    # ``-24`` and ``-196``.  (The XBRL row, "第 275 行", is handled in
    # ``_consume_unit_conversions``: it may be dropped, not changed.)
    r"|(?<![0-9])[0-9]{10}-[0-9]{2}-[0-9]{6}(?![0-9])"
    # ... and so is a filing's form name: "10-Q" carries no value.
    r"|(?<![A-Za-z0-9])(?:10-K|10-Q|8-K|20-F|6-K|40-F)(?:/A)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_HAN = re.compile(r"[\u3400-\u9fff]")
_ENGLISH_NUMBER_VALUES = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
    "january": "1", "february": "2", "march": "3", "april": "4",
    "may": "5", "june": "6", "july": "7", "august": "8",
    "september": "9", "october": "10", "november": "11", "december": "12",
}
_ENGLISH_NUMBER = re.compile(
    r"(?<![A-Za-z])(" + "|".join(_ENGLISH_NUMBER_VALUES) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


class ResearchLocalizationError(ValueError):
    """A display attachment is malformed or no longer matches its source."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def source_content_hash(product: Mapping[str, Any]) -> str:
    return _hash(product)


def _text_hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _numbers(*values: str) -> list[str]:
    return sorted(_canonical_number(token) for value in values
                  for token in _NUMBER.findall(_numeric_text(value)))


def _numeric_text(value: str) -> str:
    """Remove opaque IDs and make ISO date separators unambiguously non-signs."""

    cleaned = _OPAQUE_ID.sub("", value)
    cleaned = re.sub(r"(?i)(?<![A-Za-z])pre-(20\d{2})", r"pre \1", cleaned)
    # 2026-10-08: "mid-2026" / "late-2025" is a year, not minus 2026.
    cleaned = re.sub(r"(?i)(?<![A-Za-z])(mid|early|late|end|year|fy)-(\d)", r"\1 \2", cleaned)
    cleaned = _SAME_YEAR_ISO_RANGE.sub(r"\1 \2 \3 \4 \5", cleaned)
    cleaned = _ISO_DATE.sub(r"\1 \2 \3", cleaned)
    cleaned = _ISO_YEAR_MONTH.sub(r"\1 \2", cleaned)
    return re.sub(r"(?<!\d)(\d{1,2})-(\d{1,2})(?=\s*月)", r"\1 \2", cleaned)



def _normalize_equivalent_calendar_quarters(
    source_values: Sequence[str], target_values: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Pair exact ISO calendar-quarter ranges with exact Chinese quarter names."""

    boundaries = {
        ("01", "01", "03", "31"): "1",
        ("04", "01", "06", "30"): "2",
        ("07", "01", "09", "30"): "3",
        ("10", "01", "12", "31"): "4",
    }
    names = {"一": "1", "二": "2", "三": "3", "四": "4"}

    def iso_range_key(match: re.Match[str]) -> tuple[str, str] | None:
        quarter = boundaries.get(match.groups()[1:])
        return (match.group(1), quarter) if quarter else None

    def chinese_range_key(match: re.Match[str]) -> tuple[str, str] | None:
        year, start_month, start_day, end_year, end_month, end_day = match.groups()
        if end_year is not None and end_year != year:
            return None
        quarter = boundaries.get((start_month.zfill(2), start_day.zfill(2),
                                  end_month.zfill(2), end_day.zfill(2)))
        return (year, quarter) if quarter else None

    def range_counts(text: str) -> Counter[tuple[str, str]]:
        keys = [iso_range_key(match) for match in _CALENDAR_QUARTER_RANGE.finditer(text)]
        keys.extend(chinese_range_key(match)
                    for match in _CHINESE_CALENDAR_QUARTER_RANGE.finditer(text))
        return Counter(key for key in keys if key is not None)

    source_text = "\n".join(source_values)
    target_text = "\n".join(target_values)
    source_counts = range_counts(source_text)
    target_counts = Counter(
        (match.group(1), names.get(match.group(2), match.group(2)))
        for match in _CHINESE_CALENDAR_QUARTER.finditer(target_text)
    )
    target_counts.update(match.groups()
                         for match in _LATIN_CALENDAR_QUARTER.finditer(target_text))
    # An exact retained range proves the boundaries independently, so its
    # matching quarter title is a redundant display label.  Normalizing both
    # spellings also handles a range replaced by the title.  A changed or
    # incomplete range is not counted here and therefore remains visible to
    # ordinary numeric-token comparison.
    paired = source_counts & target_counts

    def normalize(text: str) -> str:
        def replace_range(match: re.Match[str]) -> str:
            key = iso_range_key(match)
            if key is not None and paired[key]:
                return f" CALQ{key[0]}X{key[1]} "
            return match.group(0)

        def replace_chinese_range(match: re.Match[str]) -> str:
            key = chinese_range_key(match)
            if key is not None and paired[key]:
                return f" CALQ{key[0]}X{key[1]} "
            return match.group(0)

        def replace_name(match: re.Match[str]) -> str:
            key = (match.group(1), names.get(match.group(2), match.group(2)))
            if paired[key]:
                return f" CALQ{key[0]}X{key[1]} "
            return match.group(0)

        normalized = _CALENDAR_QUARTER_RANGE.sub(replace_range, text)
        normalized = _CHINESE_CALENDAR_QUARTER_RANGE.sub(replace_chinese_range, normalized)
        normalized = _CHINESE_CALENDAR_QUARTER.sub(replace_name, normalized)
        normalized = _LATIN_CALENDAR_QUARTER.sub(replace_name, normalized)
        # A source may redundantly state “2026 Q2 (2026-04-01 to
        # 2026-06-30)”.  Once both spellings have proved the same exact
        # identity, retaining that identity once is sufficient, just as for
        # an explicitly consolidated repeated YYYYQn period below.
        seen: set[tuple[str, str]] = set()
        def collapse(match: re.Match[str]) -> str:
            key = (match.group(1), match.group(2))
            if key in seen:
                return " "
            seen.add(key)
            return f" {key[0]} {key[1]} "
        return re.sub(r"CALQ(20\d{2})X([1-4])", collapse, normalized)

    return [normalize(source_text)], [normalize(target_text)]


def _canonical_number(token: str) -> str:
    suffix = "%" if token.endswith("%") else ""
    value = token[:-1] if suffix else token
    value = value.replace(",", "")
    if "." not in value:
        sign = value[:1] if value[:1] in "+-" else ""
        digits = value[1:] if sign else value
        value = sign + (digits.lstrip("0") or "0")
    return value + suffix


def _display_variants(token: str, kind: str) -> list[str]:
    rendered = format_display_number(token, kind=kind)  # type: ignore[arg-type]
    variants = [rendered]
    trimmed = re.sub(r"(?<=\d)\.0+(?=\s|%|$)|(?<=\.\d)0+(?=\s|%|$)", "", rendered)
    if trimmed != rendered:
        variants.append(trimmed)
    number = Decimal(token.replace(",", ""))
    # 亿 amounts remain faithful at either one or two decimal places.
    # This accepts 180.4 and 180.44 for 18,044,066,000, never 180.5.
    if kind == "amount_usd" and abs(number) >= Decimal("100000000"):
        scaled = number / Decimal("100000000")
        for quantum in (Decimal("0.1"), Decimal("0.01")):
            shown = scaled.quantize(quantum, rounding=ROUND_HALF_UP)
            variants.append(f"{shown} 亿美元")
    return variants


def _number_differences(source_values: Sequence[str], target_values: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return lost Arabic tokens and genuinely new target Arabic tokens.

    English number words and month names may become Arabic digits in otherwise
    Chinese prose. They may explain only an added target token; an Arabic token
    present in the source must still survive exactly.
    """

    source_values, target_values = _normalize_equivalent_calendar_quarters(
        source_values, target_values)
    source_numbers = Counter(_numbers(*source_values))
    target_numbers = Counter(_numbers(*target_values))
    missing_counter = source_numbers - target_numbers
    added = target_numbers - source_numbers
    # A parenthetical display aid may repeat a value already present in the
    # source. It cannot introduce a value absent from the source.
    for token in tuple(added):
        if token in source_numbers:
            del added[token]
    aliases: Counter[str] = Counter()
    for value in source_values:
        cleaned = _OPAQUE_ID.sub("", value)
        aliases.update(_ENGLISH_NUMBER_VALUES[m.group(1).lower()]
                       for m in _ENGLISH_NUMBER.finditer(cleaned))
    # Written Chinese counts/months may become Arabic digits in a language
    # revision (低于一 → 低于 1, 十二月 → 12 月). The semantic verifier still
    # checks what the count refers to; this is only a deterministic precheck.
    written = {"零":"0","〇":"0","一":"1","二":"2","两":"2","三":"3",
               "四":"4","五":"5","六":"6","七":"7","八":"8","九":"9",
               "十":"10","十一":"11","十二":"12"}
    for value in source_values:
        for match in re.finditer(r"[零〇一二两三四五六七八九十]+", _OPAQUE_ID.sub("",value)):
            if match.group() in written:
                aliases[written[match.group()]] += 1
        # “两成五” is the conventional Chinese expression for 25%.
        chinese_digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                          "四": 4, "五": 5, "六": 6, "七": 7,
                          "八": 8, "九": 9}
        for match in re.finditer(r"([一二两三四五六七八九])成([一二三四五六七八九])?", value):
            whole = chinese_digits[match.group(1)] * 10
            fraction = chinese_digits.get(match.group(2) or "零", 0)
            aliases[f"{whole + fraction}%"] += 1
        # FY26 and fiscal 2026 are two spellings of the same fiscal year.
        for match in re.finditer(r"(?<![A-Za-z])FY\s*([0-9]{2})(?![0-9])", value, re.I):
            short = str(int(match.group(1)))
            full = str(2000 + int(match.group(1)))
            if missing_counter[short] and (added[full] or re.search(
                    rf"(?<!\d){full}\s*财年", " ".join(target_values))):
                missing_counter[short] -= 1
                if added[full]: added[full] -= 1
                # Once FYnn has proved the corresponding full fiscal year,
                # that same display spelling may appear in both the heading
                # and body without inventing another period.
                aliases[full] += 1
            else:
                aliases[full] += 1
        for match in re.finditer(r"(?<![A-Za-z])(?:FY|fiscal(?:\s+year)?)\s*(20[0-9]{2})(?![0-9])", value, re.I):
            aliases[str(int(match.group(1)) % 100)] += 1
        for match in re.finditer(r"(?<![A-Za-z0-9])C([1-4])Q([0-9]{2})(?![0-9])", value, re.I):
            quarter = match.group(1)
            short = str(int(match.group(2)))
            full = str(2000 + int(match.group(2)))
            if missing_counter[short] and added[full]:
                missing_counter[short] -= 1
                added[full] -= 1
            else:
                aliases[full] += 1
            if missing_counter[quarter] and re.search(
                    rf"第?[一二三四]\s*(?:个\s*)?季度", " ".join(target_values)):
                expected = {"1":"一","2":"二","3":"三","4":"四"}[quarter]
                if re.search(rf"第?{expected}\s*(?:个\s*)?季度", " ".join(target_values)):
                    missing_counter[quarter] -= 1
        if re.search(r"(?<![A-Za-z])LTM(?![A-Za-z])", value, re.I):
            aliases["12"] += 1
        for sentence in re.split(r"[.!?。！？;；]", value):
            if (re.search(r"\bbook[- ]to[- ]bill\b", sentence, re.I)
                    and re.search(r"\babove[- ]parity\b", sentence, re.I)):
                aliases["1"] += 1
    # A proved spelling alias may be repeated in a heading and its body,
    # just like an Arabic source value above. Repetition adds no new value;
    # the independent verifier still checks each value's economic meaning.
    added = Counter({token: count for token, count in added.items()
                     if not aliases[token]})

    source_joined_raw = " ".join(source_values)
    target_joined_raw = " ".join(target_values)
    quarter_names = {"1": "一", "2": "二", "3": "三", "4": "四"}
    for match in re.finditer(r"(?<![A-Za-z0-9])Q([1-4])(?![0-9])", source_joined_raw, re.I):
        quarter = match.group(1)
        if missing_counter[quarter] and re.search(
                rf"第?{quarter_names[quarter]}季度", target_joined_raw):
            missing_counter[quarter] -= 1
    # A shared period may be stated once before a consolidated peer list.
    # Only collapse repetitions of the exact same YYYYQn composite retained
    # at least once in the target.
    period = re.compile(r"(?<!\d)(20\d{2})\s*Q([1-4])(?!\d)", re.I)
    source_periods = Counter(period.findall(source_joined_raw))
    target_periods = Counter(period.findall(target_joined_raw))
    for (year, quarter), count in source_periods.items():
        collapsed = count - target_periods[(year, quarter)]
        if target_periods[(year, quarter)] and collapsed > 0:
            missing_counter[year] -= min(collapsed, missing_counter[year])
            missing_counter[quarter] -= min(collapsed, missing_counter[quarter])
    chinese_digits = {1:"一",2:"二",3:"三",4:"四",5:"五",6:"六",
                      7:"七",8:"八",9:"九",10:"十",11:"十一",12:"十二"}
    for match in re.finditer(r"(?<!\d)([1-9]|1[0-2])\s*(?:个\s*)?(季度|quarters?|个月|months?)",
                             source_joined_raw, re.I):
        number = int(match.group(1)); unit = match.group(2).lower()
        target_unit = r"(?:个\s*)?季度" if "quarter" in unit or unit == "季度" else r"(?:个\s*)?月"
        if missing_counter[str(number)] and re.search(
                rf"{chinese_digits[number]}\s*{target_unit}", target_joined_raw):
            missing_counter[str(number)] -= 1
    for match in re.finditer(
            r"(?<!\d)([1-9]|1[0-2])\s*(?:至|到|[-–—])\s*([1-9]|1[0-2])\s*(?:个\s*)?(季度|quarters?)",
            source_joined_raw, re.I):
        low, high = int(match.group(1)), int(match.group(2))
        target_pattern = rf"{chinese_digits[low]}\s*(?:至|到|[-–—])\s*{chinese_digits[high]}\s*(?:个\s*)?季度"
        if re.search(target_pattern, target_joined_raw):
            for number in (low, high):
                if missing_counter[str(number)]: missing_counter[str(number)] -= 1
    for match in re.finditer(r"(?<!\d)([1-9]|1[0-2])\s*个\s*未来\s*季度", source_joined_raw):
        number = int(match.group(1))
        if (missing_counter[str(number)] and re.search(
                rf"未来\s*{chinese_digits[number]}\s*个?\s*季度", target_joined_raw)):
            missing_counter[str(number)] -= 1
    # Repeating the same calendar year for each date in a list is optional
    # when the target retains that exact year and all month/day values remain.
    date_year = re.compile(r"(?<!\d)(20\d{2})(?=-\d{2}(?:-\d{2})?|\s*年)")
    source_years = Counter(date_year.findall(source_joined_raw))
    target_years = Counter(date_year.findall(target_joined_raw))
    for year, count in source_years.items():
        if target_years[year] and count > target_years[year]:
            missing_counter[year] -= min(count - target_years[year], missing_counter[year])

    source_joined = " ".join(_numeric_text(value) for value in source_values)
    source_joined = _NUMBER.sub(lambda match: _canonical_number(match.group()), source_joined)
    target_compact = re.sub(r"\s+", "", " ".join(target_values))
    # Explicit $/USD magnitudes are source amounts, not dimensionless values.
    # Convert the exact magnitude to base USD before applying display rounding.
    magnitude_rows: list[tuple[str, Decimal]] = []
    for value in source_values:
        for match in re.finditer(
                r"(?:\$|USD\s*)([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
                r"(million|billion)\b", value, re.I):
            token = _canonical_number(match.group(1))
            factor = Decimal("1000000") if match.group(2).lower() == "million" else Decimal("1000000000")
            magnitude_rows.append((token, Decimal(token) * factor))
    for token, base_amount in magnitude_rows:
        for rendered in _display_variants(str(base_amount), "amount_usd"):
            rendered_numbers = _numbers(rendered)
            if (rendered.replace(" ", "") in target_compact
                    and all(added[number] for number in rendered_numbers)):
                if missing_counter[token]:
                    missing_counter[token] -= 1
                for number in rendered_numbers:
                    added[number] -= 1
                break
    for value in source_values:
        for match in re.finditer(r"\$\s*(0\.\d+)(?!\d)", value):
            token = _canonical_number(match.group(1))
            cents = (Decimal(token) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            rendered = f"{cents}美分"
            rendered_numbers = _numbers(rendered)
            if (rendered in target_compact and all(added[number] for number in rendered_numbers)):
                if missing_counter[token]: missing_counter[token] -= 1
                for number in rendered_numbers: added[number] -= 1
    # Consume only a formatting result computed from the exact missing source
    # token and an explicit source unit. Unlabelled numbers remain strict.
    for token in list(source_numbers.elements()):
        numeric_token = token[:-1] if token.endswith("%") else token
        escaped = re.escape(numeric_token)
        kinds: list[str] = []
        if (re.search(rf"(?<![\d.]){escaped}\s*(?:USD(?![A-Za-z])|美元)", source_joined, re.I)
                or re.search(rf"(?<![A-Za-z])USD\s*{escaped}(?![\d.])", source_joined, re.I)):
            kinds.append("amount_usd")
        if re.search(rf"(?<![\d.]){escaped}\s*%", source_joined):
            kinds.append("percent")
        if re.search(rf"(?:EPS|每股收益)[^\d]{{0,20}}{escaped}|{escaped}[^\d]{{0,12}}(?:EPS|每股)",
                     source_joined, re.I):
            kinds.append("eps")
        if re.search(rf"ARPU[^\d]{{0,20}}{escaped}|{escaped}[^\d]{{0,12}}ARPU",
                     source_joined, re.I):
            kinds.append("arpu")
        for kind in kinds:
            for rendered in _display_variants(numeric_token, kind):
                rendered_compact = rendered.replace(" ", "")
                rendered_numbers = _numbers(rendered)
                if (rendered_compact in target_compact
                        and all(added[number] for number in rendered_numbers)):
                    if missing_counter[token]:
                        missing_counter[token] -= 1
                    for number in rendered_numbers:
                        added[number] -= 1
                    break
            else:
                continue
            break
    for value, count in _written_small_counts(target_joined_raw).items():
        if missing_counter[value]:
            missing_counter[value] -= min(count, missing_counter[value])
    _consume_unit_conversions(source_values, target_values, missing_counter, added)
    _consume_display_rounding(missing_counter, added)
    return sorted(missing_counter.elements()), sorted(added.elements())


#: 2026-10-08 (batch w).  Scale words a display may state right after a
#: number.  Longest first, so 万亿 is not read as 万 and 十亿 not as 亿.
_SCALE_WORDS: tuple[tuple[str, Decimal], ...] = (
    ("万亿", Decimal("1000000000000")), ("千亿", Decimal("100000000000")),
    ("百亿", Decimal("10000000000")), ("十亿", Decimal("1000000000")),
    ("千万", Decimal("10000000")), ("百万", Decimal("1000000")),
    ("亿", Decimal("100000000")), ("万", Decimal("10000")),
    ("trillion", Decimal("1000000000000")), ("billion", Decimal("1000000000")),
    ("million", Decimal("1000000")), ("thousand", Decimal("1000")),
    ("bn", Decimal("1000000000")), ("mn", Decimal("1000000")),
)
_SCALED_NUMBER = re.compile(
    r"(?<![\d.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*("
    + "|".join(re.escape(word) for word, _ in _SCALE_WORDS)
    + r")(?![A-Za-z])", re.IGNORECASE)
_SCALE_OF = {word.lower(): factor for word, factor in _SCALE_WORDS}
_RESTATED_VALUE = re.compile(
    r"\bwas\b[^。]*?\((?:currency|percent|per\s*cent|ratio|number|count|shares?)\)",
    re.IGNORECASE)
_DECLARED_UNIT = re.compile(
    r"单位\s*[:：为是]?\s*(万亿|十亿|百万|亿|万)"
    r"|[（(]\s*(万亿|十亿|百万|亿|万)\s*(?:美元|元|人民币)\s*[）)]")
#: A rounded display must keep at least this many significant digits: 1.05 亿
#: for 104,863,000 is a display, 1 亿 is a different number.
_MIN_CONVERSION_SIGNIFICANT_DIGITS = 3


def _scaled_occurrences(values: Sequence[str]) -> list[tuple[str, Decimal, Decimal]]:
    """``(token, scaled value, scale)`` for each number followed by a scale word."""

    found = []
    for value in values:
        for match in _SCALED_NUMBER.finditer(_numeric_text(value)):
            token = _canonical_number(match.group(1))
            try:
                number = Decimal(token)
            except InvalidOperation:
                continue
            scale = _SCALE_OF[match.group(2).lower()]
            found.append((token, number * scale, scale))
    return found


def _faithful_conversion(exact: Decimal, shown_token: str, scale: Decimal) -> bool:
    """Whether ``shown_token`` x ``scale`` is ``exact`` written in that unit.

    Exactly equal, or ``exact / scale`` rounded half-up to the shown number's
    own decimals with at least three significant digits kept.
    """

    try:
        shown = Decimal(shown_token)
    except InvalidOperation:
        return False
    if shown * scale == exact:
        return True
    decimals = len(shown_token.lstrip("+-").partition(".")[2])
    if decimals > 8:
        return False
    digits = shown_token.lstrip("+-").replace(".", "").lstrip("0")
    if len(digits) < _MIN_CONVERSION_SIGNIFICANT_DIGITS:
        return False
    places = Decimal(1).scaleb(-decimals)
    return (exact / scale).quantize(places, rounding=ROUND_HALF_UP) == shown


def _consume_unit_conversions(source_values: Sequence[str], target_values: Sequence[str],
                              missing: Counter[str], added: Counter[str]) -> None:
    """Pair a source value with the same value restated in a stated unit.

    2026-10-08, live: 158 localizations on 10-07 and 34 blocked UI-text
    batches failed on ``104863000`` shown as ``1.04863 亿美元``, or
    ``2707409432.42`` as ``27.07 亿``.  A missing source number is accounted
    for by one *added* target number only when the target names the unit right
    after it (万, 亿, 百万, 十亿, million, billion ...) and that number times
    the unit is the source value -- exactly, or rounded half-up to the shown
    decimals with three or more significant digits.  A source number that
    itself carries a scale word is compared at its full value.  The unit must
    be stated: a bare ``1.04863`` stays a changed number, and so does a
    different one (``1.06 亿``).  One to one; the verifier still reads it.
    """

    joined_source = " ".join(_numeric_text(value) for value in source_values)
    # The XBRL row a figure was read from ("XBRL 行 ...（Americas，第 322 行）")
    # is a locator like the accession number: a draft may leave it out.
    if "XBRL" in joined_source:
        for match in re.finditer(r"第\s*([0-9]+)\s*行", joined_source):
            token = _canonical_number(match.group(1))
            if missing[token] > 0:
                missing[token] -= 1
    # "7.7 (percent)" is 7.7%: the source names the unit in words.
    for token in sorted(missing):
        if token.endswith("%") or not added[token + "%"]:
            continue
        if re.search(rf"(?<![\d.]){re.escape(token)}\s*\(?\s*(?:percent|per\s*cent|pct)\b",
                     joined_source, re.I):
            take = min(missing[token], added[token + "%"])
            missing[token] -= take
            added[token + "%"] -= take
    # A scaled target number may also be one the source happens to contain
    # elsewhere ("3 亿美元" beside "2023-03-31"): it is free for pairing when
    # the target holds it more often than the source does.
    source_count = Counter(_numbers(*source_values))
    target_count = Counter(_numbers(*target_values))
    spare = Counter({token: target_count[token] - source_count[token]
                     for token in target_count
                     if target_count[token] > source_count[token] and not added[token]})

    def available(token: str) -> bool:
        return added[token] > 0 or spare[token] > 0

    def take(token: str) -> None:
        if added[token] > 0:
            added[token] -= 1
        else:
            spare[token] -= 1

    targets = [row for row in _scaled_occurrences(target_values) if available(row[0])]
    # A table or list that declares its unit once ("收入（单位：亿美元）",
    # "营业收入（亿美元）") states it for its bare numbers too.
    declared = _DECLARED_UNIT.search(" ".join(target_values))
    if declared is not None:
        scale = _SCALE_OF[(declared.group(1) or declared.group(2)).lower()]
        scaled_tokens = {row[0] for row in targets}
        for token in sorted(added):
            if token.endswith("%") or token in scaled_tokens or added[token] <= 0:
                continue
            try:
                number = Decimal(token)
            except InvalidOperation:
                continue
            targets.extend([(token, number * scale, scale)] * added[token])
    source_scaled: dict[str, list[Decimal]] = {}
    for token, value, _scale in _scaled_occurrences(source_values):
        source_scaled.setdefault(token, []).append(value)
    converted: set[str] = set()
    for token in sorted(missing.elements()):
        if token.endswith("%") or missing[token] <= 0:
            continue
        try:
            plain = Decimal(token)
        except InvalidOperation:
            continue
        candidates = [*source_scaled.get(token, ()), plain]
        for index, (shown, _value, scale) in enumerate(targets):
            if not available(shown):
                continue
            if any(_faithful_conversion(exact, shown, scale) for exact in candidates):
                missing[token] -= 1
                take(shown)
                converted.add(token)
                targets.pop(index)
                break
    # A restatement next to a value the target kept ("65543000美元（约合
    # 6554.3万美元）") adds a display, not a number: it may stand for any
    # source value it faithfully converts, missing or not.
    if any(count > 0 for count in added.values()):
        values = []
        for token in _numbers(*source_values):
            if token.endswith("%"):
                continue
            try:
                values.append(Decimal(token))
            except InvalidOperation:
                continue
        values.extend(value for rows in source_scaled.values() for value in rows)
        for shown, _value, scale in targets:
            if added[shown] > 0 and any(
                    _faithful_conversion(exact, shown, scale) for exact in values):
                added[shown] -= 1
    # One generated sentence that restates its own value ("$1.5 billion ...
    # was USD 1.5 billion (currency), as published ...") may say it once,
    # provided the target keeps it.
    kept = Counter(_numbers(*target_values))
    shown_scaled = _scaled_occurrences(target_values)
    worded = re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)\s*\(?\s*(?:percent|per\s*cent|pct)\b\)?",
                    r"\1% (percent)", joined_source, flags=re.I)

    def represented(token: str) -> bool:
        if kept[token] or token in converted:
            return True
        try:
            exact = [Decimal(token), *source_scaled.get(token, ())]
        except InvalidOperation:
            return False
        return any(_faithful_conversion(value, shown, scale)
                   for shown, _value, scale in shown_scaled for value in exact)

    for token in sorted(missing):
        if missing[token] <= 0:
            continue
        # "11.2% ... was 11.2 (percent)" states one value twice.
        forms = [form for form in (token, token + "%")
                 if (form == token or not token.endswith("%")) and represented(form)]
        for sentence in re.split(r"(?<!\d)[.!?](?!\d)|[。！？；;\n]", worded):
            if missing[token] <= 0:
                break
            # Only the generated restatement "<quote> was <value> (<unit>), as
            # published ..." -- two different facts that share a value must
            # both survive.
            if not _RESTATED_VALUE.search(sentence):
                continue
            numbers = Counter(_numbers(sentence))
            repeats = max((numbers[form] for form in forms), default=0)
            if repeats > 1:
                missing[token] -= min(missing[token], repeats - 1)
    for counter in (missing, added):
        for key in [key for key, count in counter.items() if count <= 0]:
            del counter[key]


#: A source decimal with at least this many fractional digits is a machine
#: value (a float written out whole), not a figure anyone chose to state.
_MACHINE_PRECISION_DIGITS = 4


def _consume_display_rounding(missing: Counter[str], added: Counter[str]) -> None:
    """Pair a machine-precision source value with its display rounding.

    2026-10-07, live: the DXC dossier v21 never published because a unit
    carried ``2707409432.42083940`` and every draft showed it as
    ``2707409432.42`` -- exactly the "normal display rounding" the drafting
    prompt allows, refused by this check.  A source token with four or more
    decimals may be matched by one target token that is that value rounded
    half-up to the target's own zero to two decimals.  Nothing else changes:
    every other value still has to survive exactly, the pairing is one to one,
    and the semantic verifier still reads the sentence.
    """

    for token in sorted(missing.elements()):
        if token.endswith("%") or not missing[token]:
            continue
        fraction = token.lstrip("+-").partition(".")[2]
        if len(fraction) < _MACHINE_PRECISION_DIGITS:
            continue
        try:
            exact = Decimal(token)
        except InvalidOperation:
            continue
        for candidate in sorted(added.elements()):
            if candidate.endswith("%") or not added[candidate]:
                continue
            shown_fraction = candidate.lstrip("+-").partition(".")[2]
            if len(shown_fraction) > 2:
                continue
            try:
                shown = Decimal(candidate)
            except InvalidOperation:
                continue
            places = Decimal(1).scaleb(-len(shown_fraction))
            if exact.quantize(places, rounding=ROUND_HALF_UP) == shown:
                missing[token] -= 1
                added[candidate] -= 1
                break
    for counter in (missing, added):
        for key in [key for key, count in counter.items() if count <= 0]:
            del counter[key]


# Small counts a Chinese draft may write out: "4 家" -> "四家".  Live
# 2026-09-25 ACN and CTSH NO_CHANGE judgements failed their only draft on
# ``missing_tokens ["4"]`` for exactly this.  The rule is deliberately narrow:
#
# * values 0-10 (零/〇, 一 ... 十, 两 for 2) and 11-19 (十一 ... 十九) only --
#   no 二十, 百, 千, 万, no decimals and no percentages (四成 is 40%, and a
#   "4%" token is never consumed by this);
# * the written numeral must stand alone (no Chinese numeral or digit on
#   either side, so 二十四 does not yield 四 and 十四 does not yield 十) and
#   be a count: followed by a measure word, or preceded by 第;
# * it only accounts for a *missing* source integer; it never licenses an
#   added one, and each written occurrence accounts for one token.
#
# The independent verifier still checks what each count refers to.
_WRITTEN_SMALL = {
    "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3",
    "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
    "十": "10", **{"十" + digit: str(10 + int(value)) for digit, value in (
        ("一", "1"), ("二", "2"), ("三", "3"), ("四", "4"), ("五", "5"),
        ("六", "6"), ("七", "7"), ("八", "8"), ("九", "9"))},
}
_MEASURE_WORDS = "家个只支项条次名位年季月周天倍类种笔份款轮期段节处步组批件页章"
_WRITTEN_SMALL_COUNT = re.compile(
    r"(?<![零〇一二两三四五六七八九十百千万亿0-9])"
    r"(?:(?<=第)(十[一二三四五六七八九]|[零〇一二两三四五六七八九十])"
    r"|(十[一二三四五六七八九]|[零〇一二两三四五六七八九十])(?=\s*(?:个\s*)?[" + _MEASURE_WORDS + r"]))"
    r"(?![零〇一二两三四五六七八九十百千万亿0-9])"
)


def _written_small_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for match in _WRITTEN_SMALL_COUNT.finditer(_OPAQUE_ID.sub("", text)):
        counts[_WRITTEN_SMALL[match.group(1) or match.group(2)]] += 1
    return counts


def localization_blockers(product: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Refuse missing research chapters without mistaking typed UI fields for chapters.

    Empty bodies and placeholders still fail. Only the trusted projector's
    closed field contract exempts labels, formal absence states and gap notes
    from the narrative substance check; required research sections stay strict.
    """

    from .product_substance import publication_blockers

    sections = list(product.get("sections") or [])
    blockers = [{"code": "empty_source_section", "title": str(row.get("title") or "")}
                for row in sections if isinstance(row, Mapping) and not str(row.get("body") or "").strip()]
    from .research_source_projection import legacy_projection, section_role
    try:
        legacy_projection(product)  # Closed marker/state/hash proof, not body self-declaration.
        for row in sections:
            if not isinstance(row,Mapping): continue
            role = section_role(product,row)
            found = publication_blockers([row])
            if role != "narrative":
                # Labels, exact formal absence states and source gap annotations
                # are not required research chapters. Still reject placeholders.
                found = [item for item in found if item["code"] not in {"empty","heading_only","gap_only"}]
            blockers += [item for item in found if item["code"] != "empty"]
    except ValueError as exc:
        blockers.append({"code":"unproved_projection","title":"正文","reason":str(exc)})
    if not sections:
        blockers.append({"code":"missing_section","title":"正文"})
    return blockers


def build_prompt(product: Mapping[str, Any]) -> str:
    """Build a translation/edit prompt without granting new research work."""

    blockers = localization_blockers(product)
    if blockers:
        raise ResearchLocalizationError(
            "a product with a failed section is not localised: "
            + "；".join(f"{item['title']}（{item['code']}）" for item in blockers[:5]))
    sections = [
        {"index": i, "title": str(row.get("title") or ""),
         "body": str(row.get("body") or ""),
         "gaps": [str(x) for x in row.get("gaps") or []]}
        for i, row in enumerate(product.get("sections") or [])
    ]
    shape = {"sections": [
        {"index": row["index"], "title": "通顺的简体中文标题",
         "body": "忠实、简洁的简体中文正文", "gaps": ["真正影响判断的缺口"]}
        for row in sections
    ]}
    return "\n".join((
        "Translate and edit an existing research product for Chinese display. This is a faithful "
        "presentation pass, not new research.",
        *final_text_instructions(),
        "Keep exactly one output section for each input section, in the same order and with the "
        "same index. Keep the same gaps array length; do not manufacture new gaps. "
        "You may merge repetitive defensive sentences inside a section and repair "
        "awkward wording, but retain every uncertainty that could change the judgement.",
        "Preserve every authoritative value. You may format an explicit USD amount into 万美元 or "
        "亿美元 with normal display rounding, a percentage to one decimal place, and an explicitly "
        "labelled EPS or ARPU to two decimal places. Do not change the underlying value or unit. "
        "English number words and month "
        "names may be translated as Chinese written numerals (for example, one→一 and "
        "December→十二月); do not introduce Arabic digits for them unless needed. Do not "
        "translate, summarize or reproduce "
        "source refs; they remain attached from the source product outside this response.",
        "Return raw JSON only with exactly the requested shape.",
        "SOURCE PRODUCT SHA-256: " + source_content_hash(product),
        "SOURCE SECTIONS: " + json.dumps(sections, ensure_ascii=False, separators=(",", ":")),
        "OUTPUT SHAPE: " + json.dumps(shape, ensure_ascii=False, separators=(",", ":")),
    ))


def build_verifier_prompt(product: Mapping[str, Any], localized: Mapping[str, Any]) -> str:
    source = {
        "kind": product.get("kind"), "version_ref": product.get("version_ref"),
        "content_hash": source_content_hash(product),
        "sections": [{"index": index, "title": row.get("title") or "",
                      "body": row.get("body") or "", "gaps": row.get("gaps") or []}
                     for index, row in enumerate(product.get("sections") or [])],
    }
    return "\n".join((
        "You are an independent verifier of a Chinese presentation attachment for an existing "
        "research product.",
        "Check faithful meaning, exact section order, preservation of "
        "all decision-relevant uncertainties and financial values (allow faithful USD 万/亿 unit "
        "conversion and display rounding, percentages to one decimal, EPS/ARPU to two decimals), "
        "and absence of new "
        "facts, sources, approvals or actions. Proper nouns and verbatim quotations may remain in "
        "their source language.",
        "A normalized_statement, generated evidence sentence, structured summary or metric label "
        "is system-authored prose rather than a verbatim quotation. Reject a localization that "
        "leaves it untranslated or labels it as an original quotation unless the source structure "
        "explicitly marks a direct quotation.",
        "Return raw JSON only: "
        '{"verdict":"pass|reject","faithful":true,"no_new_facts":true,'
        '"meaning_preserved":true,"findings":[{"section_index":0,"source_quote":"原文中的唯一原句",'
        '"candidate_quote":"译稿中的唯一被拒句","code":"changed_fact|changed_number|new_fact|missing_fact","reason":"具体差异"}]}',
        "For pass, findings must be empty. A rejection must anchor every finding to exact unique "
        "body quotations and a section index. Only new_fact may have an empty source_quote; only "
        "missing_fact may have an empty candidate_quote. Unanchored criticism cannot authorize repair.",
        "Source refs, numbers, status, version and approval remain bound by the source product "
        "outside this presentation attachment. Display unit conversion does not change the original "
        "numeric data. This verifier checks semantic fidelity, not sentence-level language style.",
        "SOURCE: " + json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "LOCALIZED: " + json.dumps(localized, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))


def _english_display_allowed(product: Mapping[str, Any]) -> bool:
    kind = str(product.get("kind") or "").lower()
    media = str(product.get("media_type") or "").lower()
    return (kind in {"excel", "xlsx", "workbook", "model_excel"}
            or "spreadsheet" in media or "excel" in media or "xlsx" in media)


def validate_localized_text(product: Mapping[str, Any], localized: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cheap structural and numeric preservation checks to run before verification."""

    sections = product.get("sections") or []
    rows = localized.get("sections") if isinstance(localized, Mapping) else None
    if not isinstance(rows, list) or len(rows) != len(sections):
        raise ResearchLocalizationError("localized sections must match source section count")
    checked = []
    for index, (source, row) in enumerate(zip(sections, rows)):
        if not isinstance(source, Mapping) or not isinstance(row, Mapping):
            raise ResearchLocalizationError("sections must be objects")
        if set(row) != {"index", "title", "body", "gaps"} or row["index"] != index:
            raise ResearchLocalizationError("localized section shape or order is invalid")
        if not isinstance(row["title"], str) or not row["title"].strip() or not isinstance(row["body"], str):
            raise ResearchLocalizationError("localized title/body must be text")
        if not isinstance(row["gaps"], list) or any(not isinstance(x, str) for x in row["gaps"]):
            raise ResearchLocalizationError("localized gaps must be text")
        source_text = (str(source.get("title") or ""), str(source.get("body") or ""),
                       *(str(x) for x in source.get("gaps") or []))
        target_text = (row["title"], row["body"], *(str(x) for x in row["gaps"]))
        missing, added = _number_differences(source_text, target_text)
        if missing or added:
            detail = {"section_index": index, "missing_tokens": missing,
                      "added_tokens": added}
            raise ResearchLocalizationError(
                "localized section changed financial number tokens: "
                + json.dumps(detail, ensure_ascii=False, sort_keys=True)
            )
        if (not _english_display_allowed(product)
                and any(value.strip() for value in source_text)
                and not _HAN.search(" ".join(target_text))):
            raise ResearchLocalizationError(
                f"localized section {index} has no Simplified Chinese presentation text"
            )
        checked.append({
            "index": index,
            "source_title_sha256": _text_hash(source_text[0]),
            "source_body_sha256": _text_hash(source_text[1]),
            "source_gaps_sha256": _hash(list(source.get("gaps") or [])),
            "title": row["title"].strip(), "body": row["body"].strip(),
            "gaps": [x.strip() for x in row["gaps"]],
        })
    return checked


def truncated_copy_pairs(bodies: Sequence[str]) -> list[tuple[int, int]]:
    """``(copy, original)`` index pairs where one body repeats or cuts another."""

    from .final_surface_products import MIN_TRUNCATED_COPY_CHARS, truncated_copy_of

    pairs = []
    for index, body in enumerate(bodies):
        # A short line repeated ("尚未形成判断" under four debate positions)
        # is a label, not a copied paragraph.
        if len(body.strip()) < MIN_TRUNCATED_COPY_CHARS:
            continue
        for other, original in enumerate(bodies):
            if other != index and truncated_copy_of(body, original):
                # An exact duplicate is reported once, against the earlier one.
                if len(body.strip()) == len(original.strip()) and other > index:
                    continue
                pairs.append((index, other))
                break
    return pairs


def validate_no_truncated_copies(product: Mapping[str, Any],
                                 localized: Mapping[str, Any] | None = None) -> None:
    """Refuse sections that are copies, or truncated copies, of each other.

    Deterministic and free, and run where money is about to be spent: before
    a source is sent for drafting, and on each draft before it is verified.
    The verifier cannot catch a duplicate the source itself carries -- it is
    asked whether the translation is faithful, and a faithful translation of a
    repeated paragraph repeats it (live 2026-09-24, 15 of 17 judgements).

    With ``localized`` the check is on the draft, and a pair is only refused
    when the corresponding *source* sections are not already such a pair, so
    that a legitimately repeated short line cannot make a product untranslatable.
    Offsets are relative: ``localized`` covers the same sections as ``product``.
    """

    sources = [str((section or {}).get("body") or "") for section in product.get("sections") or []]
    source_pairs = set(truncated_copy_pairs(sources))
    if localized is None:
        if source_pairs:
            copy, original = sorted(source_pairs)[0]
            raise ResearchLocalizationError(
                f"source section {copy} is a copy or truncated copy of section {original}; "
                "the product must be rebuilt without the duplicate before it is localized")
        return
    rows = localized.get("sections") if isinstance(localized, Mapping) else None
    bodies = [str((row or {}).get("body") or "") for row in rows or []]
    for copy, original in truncated_copy_pairs(bodies):
        if (copy, original) in source_pairs or (original, copy) in source_pairs:
            continue
        raise ResearchLocalizationError(
            f"localized section {copy} is a copy or truncated copy of localized section "
            f"{original}; translate each source section on its own")


def build_localization(product: Mapping[str, Any], localized: Mapping[str, Any],
                       verifier: Mapping[str, Any]) -> dict[str, Any]:
    checked = validate_localized_text(product, localized)
    expected_verifier = {"verdict", "faithful", "no_new_facts", "meaning_preserved", "findings"}
    if (not isinstance(verifier, Mapping) or set(verifier) != expected_verifier
            or verifier.get("verdict") != "pass"
            or any(verifier.get(key) is not True for key in
                   ("faithful", "no_new_facts", "meaning_preserved"))
            or not isinstance(verifier.get("findings"), list)
            or verifier.get("findings")):
        raise ResearchLocalizationError("independent localization verifier did not pass cleanly")
    candidate = {
        "schema_version": SCHEMA_VERSION, "status": "localized",
        "target_locale": TARGET_LOCALE, "rules_version": FINAL_TEXT_RULES_VERSION,
        "source": {
            "kind": product.get("kind"), "version_ref": product.get("version_ref"),
            "content_hash": source_content_hash(product),
        },
        "sections": checked,
        "verifier": dict(verifier),
    }
    candidate["content_hash"] = _hash(candidate)
    return candidate


def validate_localization(product: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise ResearchLocalizationError("localization must be an object")
    body = dict(candidate)
    digest = body.pop("content_hash", None)
    if digest != _hash(body):
        raise ResearchLocalizationError("localization content hash mismatch")
    if set(candidate) != {"schema_version", "status", "target_locale", "rules_version",
                          "source", "sections", "verifier", "content_hash"}:
        raise ResearchLocalizationError("localization has an open or incomplete shape")
    if (candidate["schema_version"] != SCHEMA_VERSION or candidate["status"] != "localized"
            or candidate["target_locale"] != TARGET_LOCALE
            or candidate["rules_version"] not in ({FINAL_TEXT_RULES_VERSION} | LEGACY_FINAL_TEXT_RULES_VERSIONS)):
        raise ResearchLocalizationError("localization identity is unsupported")
    source = candidate["source"]
    if source != {"kind": product.get("kind"), "version_ref": product.get("version_ref"),
                   "content_hash": source_content_hash(product)}:
        raise ResearchLocalizationError("localization source identity mismatch")
    # Rebuild from its own display fields to replay all hashes and verifier gates.
    localized = {"sections": [{"index": x["index"], "title": x["title"],
                                "body": x["body"], "gaps": x["gaps"]}
                               for x in candidate["sections"]]}
    replay = build_localization(product, localized, candidate["verifier"])
    if candidate["rules_version"] in LEGACY_FINAL_TEXT_RULES_VERSIONS:
        replay["rules_version"] = candidate["rules_version"]
        unhashed = dict(replay); unhashed.pop("content_hash")
        replay["content_hash"] = _hash(unhashed)
    if replay != dict(candidate):
        raise ResearchLocalizationError("localization does not replay exactly")
    return dict(candidate)


def select_localized(product: Mapping[str, Any], candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    """Overlay display prose while retaining every authoritative product field."""

    if candidate is None:
        return copy.deepcopy(dict(product))
    valid = validate_localization(product, candidate)
    result = copy.deepcopy(dict(product))
    for source, translated in zip(result.get("sections") or [], valid["sections"]):
        source["title"] = translated["title"]
        source["body"] = translated["body"]
        source["gaps"] = list(translated["gaps"])
    result["localization"] = {
        "schema_version": valid["schema_version"], "target_locale": valid["target_locale"],
        "rules_version": valid["rules_version"], "source_content_hash": valid["source"]["content_hash"],
        "content_hash": valid["content_hash"],
    }
    return result


__all__ = ["ResearchLocalizationError", "SCHEMA_VERSION", "TARGET_LOCALE", "VERIFIER_PURPOSE",
           "build_prompt", "build_verifier_prompt", "build_localization",
           "validate_localized_text", "validate_localization", "select_localized",
           "truncated_copy_pairs", "validate_no_truncated_copies",
           "source_content_hash"]
