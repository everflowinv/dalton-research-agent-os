"""Explicit source quantities for governed operating-driver numeric reads.

This is a new numeric contract.  It does not widen the frozen 0.1 Figure
vocabulary, convert tons to counts, or turn a disclosed price into revenue.
Only the unit literally declared beside the amount (or its named table row)
is admissible.  Dimensional relationships remain a model decision.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any

CONTRACT_REF = 'directed-document-operating-numeric:0.1'
FIGURE_VERIFIER_REF = 'verifier:document-operating-figure-citation-units:0.1'
QUANTITY_UNITS = ('tons', 'metric_tons', 'short_tons')
PRICE_UNITS = ('currency_per_ton', 'currency_per_metric_ton', 'currency_per_short_ton', 'currency_per_mmbtu')
UNITS = (*QUANTITY_UNITS, *PRICE_UNITS)
_UNIT_WORDS = {
    'tons': r'tons?',
    'metric_tons': r'metric\s+(?:tons?|tonnes?)',
    'short_tons': r'short\s+tons?',
    'currency_per_ton': r'(?:per\s+ton|/\s*ton)',
    'currency_per_metric_ton': r'(?:per\s+metric\s+(?:ton|tonne)|/\s*metric\s+(?:ton|tonne))',
    'currency_per_short_ton': r'(?:per\s+short\s+ton|/\s*short\s+ton)',
    'currency_per_mmbtu': r'(?:per\s+MMBtu|/\s*MMBtu)',
}
_AMOUNT = r'[-+]?\d+(?:,\d{3})*(?:\.\d+)?'
_CURRENCY_WORDS = {'USD': r'(?:USD|US\s*dollars?|U\.S\.\s*dollars?)',
                   'CAD': r'(?:CAD|Canadian\s+dollars?)',
                   'EUR': r'(?:EUR|euros?)', 'GBP': r'(?:GBP|pounds?\s+sterling)'}


def monetary(unit: str) -> bool:
    return unit == 'currency' or unit in PRICE_UNITS


def verify_reported_unit(value: str, quote: str, *, unit: str,
                         currency: Any, scale: Any, label: str) -> None:
    """Prove the exact named unit, without inference from the metric name.

    An unqualified ``ton`` stays unqualified; explicit metric/short tons
    cannot satisfy it.  Price currencies must be named, not guessed from a
    dollar sign.  Existing scale verification remains separately required.
    """
    if unit not in UNITS:
        return
    text = unicodedata.normalize('NFKC', quote).replace('−', '-')
    declared = _UNIT_WORDS[unit]
    if unit in ('tons', 'currency_per_ton') and re.search(
            r'\b(?:metric|short)\s+(?:tons?|tonnes?)\b', text, re.I):
        raise ValueError('unqualified tons cannot borrow an explicitly qualified unit')
    if unit in QUANTITY_UNITS and currency is not None:
        raise ValueError('a physical quantity must not carry currency')
    if unit in PRICE_UNITS:
        words = _CURRENCY_WORDS.get(currency)
        if words is None or re.search(r'(?<!\w)' + words + r'(?!\w)', text, re.I) is None:
            raise ValueError('unit price needs an explicit supported source currency')
    if re.search(r'(?<!\w)' + declared + r'(?!\w)', text, re.I) is None:
        raise ValueError('operating quantity citation does not declare its exact unit')
    # The value must be associated with that unit, rather than borrowing an
    # unrelated sentence's tonnage.  Named tabular rows may declare the unit
    # in their own label and carry the value in that same physical row.
    target = Decimal(value)
    linked = False
    # Rendered HTML tables may preserve separators while flattening line breaks.
    # Match only this exact label's unit annotation and its immediately
    # following amount; unrelated rows cannot lend currency or a multiplier.
    for cell in re.finditer(re.escape(label) + r'\s*\(([A-Za-z\s./]+)\)\s*\|\s*(' + _AMOUNT + r')(?![\d.])', text, re.I):
        annotation, amount = cell.groups()
        if Decimal(amount.replace(',', '')) != target or re.search(declared, annotation, re.I) is None:
            continue
        if unit in PRICE_UNITS and re.search(_CURRENCY_WORDS[currency],annotation,re.I) is None:
            continue
        qualifier = re.search(r'\b(thousand|million|billion|trillion)s?\b',annotation,re.I)
        expected_scale = qualifier.group(1).lower() if qualifier else None
        if scale != expected_scale:
            raise ValueError('operating amount multiplier differs from its exact source cell')
        linked = True
    for row in text.splitlines():
        if re.search(declared, row, re.I) is None:
            continue
        for match in re.finditer(_AMOUNT, row):
            if Decimal(match.group().replace(',', '')) != target:
                continue
            suffix = row[match.end():match.end() + 80]
            adjacency = re.match(r'\s*(?:(?:thousand|million|billion)\s+)?' + declared,
                                 suffix, re.I) is not None
            head = re.split(r'[\t|]', row, maxsplit=1)[0].strip()
            named_row = bool(('\t' in row or '|' in row) and re.fullmatch(
                re.escape(label) + r'\s*\([A-Za-z\s./]+\)', head, re.I)
                and re.search(declared, head, re.I))
            if unit in PRICE_UNITS:
                words = _CURRENCY_WORDS[currency]
                adjacency = bool(adjacency and re.search(
                    words + r'\s*$', row[:match.start()], re.I)) or bool(re.match(
                    r'\s*' + words + r'\s*' + declared, suffix, re.I))
                named_row = bool(named_row and re.search(words, row, re.I))
            if adjacency or named_row:
                declared_scale = re.search(
                    r'\b(thousand|million|billion|trillion)s?\s+(?:of\s+)?' + declared,
                    row, re.I)
                expected_scale = declared_scale.group(1).lower() if declared_scale else None
                if scale != expected_scale:
                    raise ValueError('operating amount multiplier differs from its exact source row')
                linked = True
    if not linked:
        raise ValueError('operating amount does not bind its reported physical unit')



def verify_reported_period(period: str, quote: str) -> None:
    """This contract uses exact disclosed duration bounds, not filing dates."""
    from datetime import date
    parts = period.split('/')
    if len(parts) != 2:
        raise ValueError('operating numeric period requires an explicit start/end duration')
    start,end = (date.fromisoformat(p) for p in parts)
    if start > end:
        raise ValueError('operating numeric duration is reversed')
    # A source must actually name the duration, not simply share the year.
    # Multiple date windows in one quotation are not uniquely associated
    # with an amount under this initial conservative contract.
    dates = set(re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',quote))
    if dates == set(parts) and period in quote:
        return
    # Exact calendar-duration wording is an independent declaration of the
    # start. It is not the filing's report_date or a same-year inference.
    import calendar
    from datetime import datetime
    text=unicodedata.normalize('NFKC',quote)
    named=[]
    expression=r'\b(three|six|nine|twelve|3|6|9|12)\s+months?\s+ended\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b'
    counts={'three':3,'six':6,'nine':9,'twelve':12}
    for match in re.finditer(expression,text,re.I):
        duration,month,day,year=match.groups()
        length=counts[duration.lower()] if duration.lower() in counts else int(duration)
        ending=datetime.strptime(f'{month} {day} {year}','%B %d %Y').date()
        if ending.day!=calendar.monthrange(ending.year,ending.month)[1]:
            raise ValueError('operating duration does not name a complete calendar reporting period')
        beginning_index=ending.year*12+ending.month-length
        beginning=date(beginning_index//12,beginning_index%12+1,1)
        named.append((beginning,ending))
    if len(named)!=1 or named[0]!=(start,end) or dates-set(parts):
        raise ValueError('operating numeric source does not uniquely state its exact duration')
