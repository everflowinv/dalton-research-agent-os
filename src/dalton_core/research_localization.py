"""Hash-bound, rebuildable Chinese display attachments for research products.

The attachment is presentation data.  It never replaces the research product,
its approval state, source refs, figures or version identity.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .cockpit_model import register_purpose
from .final_text_contract import FINAL_TEXT_RULES_VERSION, final_text_instructions
from .numeric_display import format_display_number

SCHEMA_VERSION = "research-localization:0.1"
TARGET_LOCALE = "zh-CN"
VERIFIER_PURPOSE = register_purpose("research_localization_verifier")
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
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
_CHINESE_CALENDAR_QUARTER = re.compile(
    r"(?<!\d)(\d{4})\s*\u5e74\s*\u7b2c?\s*([\u4e00\u4e8c\u4e09\u56db1-4])\s*\u5b63\u5ea6"
)
_OPAQUE_ID = re.compile(
    r"\b(?:claim|claim-version|dossier|dossier-version|memo|memo-version|"
    r"debate|debate-map|forecast-model-version|company-model-spec|mission|"
    r"mission-version|thesis|thesis-version|event|document|document-version)"
    r":[A-Za-z0-9:._-]+|\b[0-9a-f]{64}\b|(?<![A-Za-z0-9])[ST][0-9]+(?![A-Za-z0-9])|\bcausal_chain:[0-9]+\b|因果链[0-9]+",
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

    def range_key(match: re.Match[str]) -> tuple[str, str] | None:
        quarter = boundaries.get(match.groups()[1:])
        return (match.group(1), quarter) if quarter else None

    source_text = "\n".join(source_values)
    target_text = "\n".join(target_values)
    source_counts = Counter(key for match in _CALENDAR_QUARTER_RANGE.finditer(source_text)
                            if (key := range_key(match)) is not None)
    target_counts = Counter(
        (match.group(1), names.get(match.group(2), match.group(2)))
        for match in _CHINESE_CALENDAR_QUARTER.finditer(target_text)
    )
    # If the target retained the explicit range, ordinary token comparison
    # already proves its boundaries.  Pair only ranges actually replaced by a
    # quarter name, so a redundant display label cannot mask added/changed
    # dates elsewhere in the section.
    target_ranges = Counter(key for match in _CALENDAR_QUARTER_RANGE.finditer(target_text)
                            if (key := range_key(match)) is not None)
    paired = (source_counts - target_ranges) & target_counts

    def normalize(text: str) -> str:
        def replace_range(match: re.Match[str]) -> str:
            key = range_key(match)
            if key is not None and paired[key]:
                return f" CALQ{key[0]}X{key[1]} "
            return match.group(0)

        def replace_name(match: re.Match[str]) -> str:
            key = (match.group(1), names.get(match.group(2), match.group(2)))
            if paired[key]:
                return f" CALQ{key[0]}X{key[1]} "
            return match.group(0)

        normalized = _CALENDAR_QUARTER_RANGE.sub(replace_range, text)
        normalized = _CHINESE_CALENDAR_QUARTER.sub(replace_name, normalized)
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
    # Large 亿 amounts remain faithful at either one or two decimal places.
    # This accepts 180.4 and 180.44 for 18,044,066,000, never 180.5.
    if kind == "amount_usd" and abs(number) >= Decimal("10000000000"):
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
    added -= aliases

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
    return sorted(missing_counter.elements()), sorted(added.elements())


def build_prompt(product: Mapping[str, Any]) -> str:
    """Build a translation/edit prompt without granting new research work."""

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
        "Return raw JSON only: "
        '{"verdict":"pass|reject","faithful":true,"no_new_facts":true,'
        '"meaning_preserved":true,"findings":["具体问题"]}',
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
            or candidate["rules_version"] != FINAL_TEXT_RULES_VERSION):
        raise ResearchLocalizationError("localization identity is unsupported")
    source = candidate["source"]
    if source != {"kind": product.get("kind"), "version_ref": product.get("version_ref"),
                   "content_hash": source_content_hash(product)}:
        raise ResearchLocalizationError("localization source identity mismatch")
    # Rebuild from its own display fields to replay all hashes and verifier gates.
    localized = {"sections": [{"index": x["index"], "title": x["title"],
                                "body": x["body"], "gaps": x["gaps"]}
                               for x in candidate["sections"]]}
    if build_localization(product, localized, candidate["verifier"]) != dict(candidate):
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
           "source_content_hash"]
