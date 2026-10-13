"""Pure, bounded column identity from a verified original reporting table.

A candidate row does not establish the model metric's meaning. It retains
native labels and table identity so the reader and independent verifier can
make that decision. Reporting periods are bound to the amount's actual
row/colspan coordinates; a year elsewhere in a citation proves nothing.
No value, period, unit or wording is inserted into the original quotation.
"""
from __future__ import annotations

import calendar
import gzip
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

from .store import content_hash
from .public_web_extraction_source import _normalize_inline
from .pure_source_parse_cache import cache_byte_parser

RULE_REF = 'operating-source-table-columns:0.1'
CURRENCY_RULE_REF = 'operating-same-original-report-currency:0.1'
MAX_BYTES = 16 * 1024 * 1024
MAX_ELEMENTS = 300000
MAX_TABLES = 4096
MAX_ROWS = 1000
MAX_COLUMNS = 256
MAX_CELLS = 100000
MAX_TOTAL_CELLS = 500000
MAX_MEASUREMENTS = 4096
HTML = '{http://www.w3.org/1999/xhtml}'
_DURATION = re.compile(r'\b(three|six|nine|twelve|3|6|9|12)\s+months?\s+ended\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?$', re.I)
_COUNTS = {'three': 3, 'six': 6, 'nine': 9, 'twelve': 12}
_DECIMAL = r'\d+(?:,\d{3})*(?:\.\d+)?'
# Accounting parentheses are one negative sign. A signed inner number or
# unmatched parenthesis is not a second independently interpretable sign.
_NUMBER = re.compile(r'(?:[-+]?' + _DECIMAL + r'|\(' + _DECIMAL + r'\))$')
_AMOUNT = re.compile(r'\$?(?:([-+]?' + _DECIMAL + r')|\(\$?(' + _DECIMAL + r')\))$')


def _amounts(cells):
    """Close a written amount across its real adjacent symbol cells.

    The full participating span must later belong to one exact year/window.
    We never read a positive numeric core after dropping split parentheses,
    and never negate an already signed parenthesized inner amount twice.
    """
    def prefix(text):
        return bool(text and re.fullmatch(r'[()$+\-]+',text))
    def suffix(text):
        return bool(text.startswith(')') and re.fullmatch(r'[()$+\-]+',text))
    used = set()
    for index, cell in enumerate(cells):
        if index in used or not re.search(r'\d', cell['text']):
            continue
        start, end = index, index + 1
        while start and start - 1 not in used and prefix(cells[start - 1]['text']):
            start -= 1
        while end < len(cells) and suffix(cells[end]['text']):
            end += 1
        participants = cells[start:end]
        used.update(range(start, end))
        literal = ''.join(c['text'] for c in participants)
        match = _AMOUNT.fullmatch(literal)
        if match is None:
            continue
        positive, parentheses = match.groups()
        value = Decimal((positive or parentheses).replace(',', ''))
        if parentheses is not None:
            value = -value
        yield cell, participants, literal, value


def _text(node):
    return _normalize_inline(' '.join(node.itertext()))


def _row_text(cells):
    merged = []
    for cell in cells:
        text = cell['text']
        if not text:
            continue
        if merged and (text in {'%', ')', ')%'} or merged[-1] in {'$', '(', '($', '$('}):
            merged[-1] += text
        else:
            merged.append(text)
    return ' | '.join(merged)


def _period(duration, year):
    match = _DURATION.search(duration)
    if match is None or re.fullmatch(r'20\d{2}', year) is None:
        return None
    count, month, day = match.groups()
    months = _COUNTS[count.lower()] if count.lower() in _COUNTS else int(count)
    ending = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date()
    if ending.day != calendar.monthrange(ending.year, ending.month)[1]:
        return None
    index = ending.year * 12 + ending.month - months
    beginning = date(index // 12, index % 12 + 1, 1)
    return str(beginning) + '/' + str(ending)


def _rows(table):
    rows = []
    for child in table:
        if child.tag in {HTML + name for name in ('thead', 'tbody', 'tfoot')}:
            rows.extend(row for row in child if row.tag == HTML + 'tr')
        elif child.tag == HTML + 'tr':
            rows.append(child)
    if len(rows) > MAX_ROWS:
        raise ValueError('operating source table exceeds its bounded row grid')
    return rows


def _grid(table):
    result, occupied, total = [], {}, 0
    for row_index, row in enumerate(_rows(table)):
        cells, column = [], 0
        for cell in row:
            if cell.tag not in {HTML + 'td', HTML + 'th'}:
                continue
            # Nested tables cannot lend a label or numeric cell to their
            # enclosing layout table. They are parsed separately below.
            if any(node.tag == HTML + 'table' for node in cell.iter()):
                raise ValueError('nested operating table has no single row/column identity')
            while (row_index, column) in occupied:
                column += 1
            width = cell.get('colspan', '1')
            height = cell.get('rowspan', '1')
            if re.fullmatch(r'[1-9]\d{0,3}', width) is None or re.fullmatch(r'[1-9]\d{0,3}', height) is None:
                raise ValueError('operating source has an invalid explicit cell span')
            width, height = int(width), int(height)
            if width > MAX_COLUMNS or height > MAX_ROWS or column + width > MAX_COLUMNS or row_index + height > MAX_ROWS:
                raise ValueError('operating source table exceeds its bounded column grid')
            text = _text(cell)
            if len(text) > 4096:
                raise ValueError('operating source cell exceeds its bounded literal text')
            item = {'column_start': column, 'column_end': column + width, 'text': text,
                    'row': row_index, 'rowspan': height, 'cell_hash': content_hash(ET.tostring(cell, encoding='unicode'))}
            total += width * height
            if total > MAX_CELLS:
                raise ValueError('operating source exceeds its bounded occupied cells')
            for r in range(row_index, row_index + height):
                for c in range(column, column + width):
                    if (r, c) in occupied:
                        raise ValueError('operating source cell spans overlap')
                    occupied[r, c] = item
            cells.append(item)
            column += width
        result.append(cells)
    return result


@cache_byte_parser('operating-source-column-pure-parse:0.1', byte_arguments=('raw',))
def table_measurements(raw: bytes, *, original_text: str) -> dict:
    """Parse only amount/column candidates, never choose an economic metric.

    The caller must already have reverified the registration, complete raw
    SHA and rendering. Matching the exact rendered table back into that text
    closes the source offsets used by normal registered excerpts.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES or not isinstance(original_text, str) or len(original_text.encode()) > MAX_BYTES:
        raise ValueError('operating source exceeds its bounded original bytes/text')
    body = raw
    if raw.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            body = stream.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES or re.search(br'<!ENTITY\b', body, re.I):
        raise ValueError('operating source exceeds decompressed bytes or declares XML entities')
    scopes, stack, pending, root = {}, [], [], None
    for event, node in ET.iterparse(io.BytesIO(body), events=('start', 'end', 'start-ns')):
        if event == 'start-ns':
            pending.append(node)
        elif event == 'start':
            scope = dict(stack[-1]) if stack else {}
            scope.update(pending); pending.clear(); stack.append(scope)
            scopes[id(node)] = scope
            if root is None:
                root = node
            if len(stack) > 256 or len(scopes) > MAX_ELEMENTS:
                raise ValueError('operating source exceeds its bounded XML depth/elements')
        else:
            stack.pop()
    elements = list(root.iter())
    if root.tag != HTML + 'html':
        raise ValueError('operating source columns require a complete XHTML original')
    tables = [node for node in elements if node.tag == HTML + 'table']
    if len(tables) > MAX_TABLES:
        raise ValueError('operating source exceeds its bounded tables')
    measurements, after, occupied_cells = [], 0, 0
    for table_index, table in enumerate(tables):
        try:
            grid = _grid(table)
        except ValueError as exc:
            # A nested layout table is not an amount source; its independent
            # leaf tables still retain their own identities. Invalid spans in
            # any leaf table are an explicit rejection, not a guessed layout.
            if str(exc) != 'nested operating table has no single row/column identity':
                raise
            continue
        occupied_cells += sum((cell['column_end']-cell['column_start'])*cell['rowspan'] for row in grid for cell in row)
        if occupied_cells > MAX_TOTAL_CELLS:
            raise ValueError('operating original exceeds its bounded total occupied cells')
        lines = [_row_text(row) for row in grid]
        rendered = '\n\n'.join(line for line in lines if line)
        if not rendered:
            continue
        source_start = original_text.find(rendered, after)
        if source_start < 0:
            # Non-visible or differently rendered tables cannot certify an
            # amount against the registered quote. Do not synthesize a quote.
            continue
        after = source_start + len(rendered)
        table_hash = hashlib.sha256(ET.tostring(table)).hexdigest()
        headers, years, local_unit, line_position = [], [], None, source_start
        table_captions = [cell['text'] for row in grid[:8] for cell in row
                          if re.search(r'\bdollars\b|\bUSD\b|\bCAD\b|\bEUR\b|\bGBP\b', cell['text'], re.I)]
        for row_index, cells in enumerate(grid):
            row_text = lines[row_index]
            if not row_text:
                continue
            row_start = original_text.find(row_text, line_position, source_start + len(rendered))
            if row_start < 0:
                raise ValueError('operating rendered row has no exact original span')
            line_position = row_start + len(row_text)
            for cell in cells:
                if _DURATION.search(cell['text']):
                    headers.append(cell)
                if re.fullmatch(r'20\d{2}', cell['text']):
                    years.append(cell)
            label = next((cell['text'] for cell in cells if cell['text']), '')
            if not label or _DURATION.search(label) or re.fullmatch(r'20\d{2}', label):
                continue
            # Section units are explicit native captions. They are retained
            # rather than flattened into every row or inferred from a label.
            if not any(_NUMBER.fullmatch(c['text']) for c in cells):
                local_unit = label if re.search(r'\bper\s+(?:MMBtu|(?:metric\s+|short\s+)?ton)\b', label, re.I) else None
            for cell, amount_cells, literal, value in _amounts(cells):
                column_start = amount_cells[0]['column_start']
                column_end = amount_cells[-1]['column_end']
                matching_years = [y for y in years if y['row'] < row_index
                    and y['column_start'] <= column_start and y['column_end'] >= column_end]
                matching_headers = [h for h in headers if h['row'] < row_index
                    and h['column_start'] <= column_start and h['column_end'] >= column_end]
                # Current, prior and variance columns do not share identity.
                # Multiple header blocks are refused instead of choosing the
                # closest year/date from unrelated text.
                if len(matching_years) != 1 or len(matching_headers) != 1:
                    continue
                window = _period(matching_headers[0]['text'], matching_years[0]['text'])
                if window is None:
                    continue
                record = {'table_index': table_index, 'table_hash': table_hash,
                    'table_source_start': source_start, 'table_source_end': source_start + len(rendered),
                    'row': row_index, 'row_source_start': row_start, 'row_source_end': row_start + len(row_text),
                    'native_label': label, 'native_row': row_text,
                    'literal_value': literal, 'value': format(value, 'f'),
                    'column_start': column_start, 'column_end': column_end,
                    'cell_hash': cell['cell_hash'], 'period': window,
                    'amount_cells': amount_cells,
                    'duration_header': matching_headers[0], 'year_header': matching_years[0],
                    'unit_caption': local_unit, 'table_captions': table_captions}
                measurements.append({**record, 'content_hash': content_hash(record)})
                if len(measurements) > MAX_MEASUREMENTS:
                    raise ValueError('operating source exceeds its bounded column measurements')
    from .sec_filing_units import parse_filing_units, NUMERIC_RULE_REF, _expanded, INLINE
    units = parse_filing_units(raw, rule_ref=NUMERIC_RULE_REF)['units']
    # Count every namespace-expanded ISO currency in an actual XBRL unit,
    # including compound physical denominators unsupported by the legacy
    # monetary classifier. A second currency must not disappear just because
    # that particular ratio was not a currency-per-share unit.
    definitions=[];currencies=set()
    for unit_node in elements:
        if unit_node.tag!='{http://www.xbrl.org/2003/instance}unit':
            continue
        declared=set()
        for measure in unit_node.iter('{http://www.xbrl.org/2003/instance}measure'):
            expanded=_expanded(_text(measure),scopes[id(measure)])
            if expanded.startswith('{http://www.xbrl.org/2003/iso4217}'):
                code=expanded.split('}',1)[1]
                if re.fullmatch('[A-Z]{3}',code) is None:
                    raise ValueError('operating source ISO currency measure is malformed')
                declared.add(code)
        if declared:
            currencies.update(declared)
            original=units[unit_node.get('id')]
            definitions.append({'unit_ref':unit_node.get('id'),'definition_sha256':original['definition_sha256'],
                'expanded_currency_measures':sorted(declared)})
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    currency_proof = {'rule_ref': CURRENCY_RULE_REF, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
        'currency': currency if currency in {'USD', 'CAD', 'EUR', 'GBP'} else None,
        'definitions': definitions}
    fiscal_ends = []
    for node in elements:
        if node.tag not in {'{' + ns + '}nonNumeric' for ns in INLINE} or not node.get('name'):
            continue
        name = _expanded(node.get('name'), scopes[id(node)])
        ns, local = name[1:].split('}', 1)
        if local == 'CurrentFiscalYearEndDate' and re.fullmatch(r'https?://xbrl\.sec\.gov/dei/20\d{2}', ns):
            fiscal_ends.append(_text(node))
    proof = {'rule_ref': RULE_REF, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
             'text_sha256': hashlib.sha256(original_text.encode()).hexdigest(), 'measurements': measurements,
             'report_currency': {**currency_proof, 'content_hash': content_hash(currency_proof)},
             'calendar_fiscal_year': bool(fiscal_ends) and set(fiscal_ends) <= {'December 31', '--12-31'}}
    return {**proof, 'content_hash': content_hash(proof)}
