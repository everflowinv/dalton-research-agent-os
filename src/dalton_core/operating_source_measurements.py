"""Source-native unit/duration bindings for the new operating read contract.

The extra proof is distinct from the quote. Native quotations stay byte
identical; raw table columns or explicitly named narrative clauses certify
which amount carries which duration and physical unit. A model and its
independent verifier still decide whether that native line answers the exact
admitted economic definition. These readers own no admission or paid slot.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal
from .store import content_hash
from .operating_source_columns import table_measurements, RULE_REF as COLUMN_RULE

RULE_REF = 'operating-native-measurement-proof:0.1'
MAX_RECORDS = 128


def _unit(text, *, currency=None, dollar_declared=False):
    lower = text.lower()
    denom = re.search(r'\bper\s+(?:(metric|short|product|nutrient)\s+)?(tons?|tonnes?|mmbtu)\b', lower)
    if denom:
        qualifier, word = denom.groups()
        if currency not in {'USD', 'CAD', 'EUR', 'GBP'} or not dollar_declared:
            return None
        return ('currency_per_mmbtu' if word == 'mmbtu' else
                'currency_per_metric_ton' if qualifier == 'metric' or word.startswith('tonne') else
                'currency_per_short_ton' if qualifier == 'short' else 'currency_per_ton'), currency
    quantity = re.search(r'\b(?:(metric|short)\s+)?(tons?|tonnes?)\b', lower)
    if quantity:
        qualifier, word = quantity.groups()
        return ('metric_tons' if qualifier == 'metric' or word.startswith('tonne') else
                'short_tons' if qualifier == 'short' else 'tons'), None
    if re.search(r'\bpercent(?:age)?\b|%', text):
        return 'percent', None
    return None


def _scale(text):
    if re.search(r'\(\s*000s\s*\)', text, re.I):
        return 'thousand'
    found = set(re.findall(r'\b(thousand|million|billion|trillion)s?\b', text, re.I))
    if len(found) > 1:
        raise ValueError('operating amount has ambiguous native multipliers')
    return next(iter(found)).lower() if found else None


def _narrative_records(text, *, columns, quotes):
    """Only exact calendar-quarter clauses under an explicit calendar FY.

    A fiscal quarter label alone does not establish Gregorian duration. This
    rule requires the same raw filing's namespace-bound DEI year-end Dec31.
    Table amounts instead use their explicit month/end/year columns.
    """
    if not columns['calendar_fiscal_year']:
        return []
    currency = columns['report_currency']['currency']
    records = []
    quarter = re.compile(r'\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(?:of\s+)?(20\d{2})\b', re.I)
    counts = {'first':1,'1st':1,'second':2,'2nd':2,'third':3,'3rd':3,'fourth':4,'4th':4}
    amount = re.compile(r'(?<![\w.])(?:(USD|CAD|EUR|GBP|\$)\s*)?([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*'
        r'(?:(thousand|million|billion|trillion)\s+)?((?:per\s+(?:(?:metric|short|product|nutrient)\s+)?(?:ton|tonne|MMBtu))|(?:(?:metric|short)\s+)?(?:tons?|tonnes?))\b',re.I)
    for q in quotes:
        excerpt = q['excerpt']
        # Do not pick a period from another paragraph or a comparative value
        # from its neighbour. Comparison clauses inherit the stated metric,
        # but must themselves explicitly state their period.
        for para in re.finditer(r'[^\n]+(?:\n(?!\n)[^\n]+)*', excerpt):
            paragraph = para.group()
            sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', paragraph)
            clauses = [(sentence, words) for sentence in sentences for words in
                re.split(r'\bcompared\s+to\b|\bfrom\b|\bversus\b', sentence)]
            for sentence, words in clauses:
                periods = list(quarter.finditer(words))
                if len(periods) != 1:
                    continue
                window = periods[0]
                n, year = counts[window[1].lower()], int(window[2])
                start = date(year,(n-1)*3+1,1)
                end_month = n*3
                import calendar
                end = date(year,end_month,calendar.monthrange(year,end_month)[1])
                for number in amount.finditer(words):
                    named_currency, value, scale, named_unit = number.groups()
                    # A named foreign denomination defeats inherited report
                    # currency; a dollar sign is admitted only by the explicit
                    # same-original namespace-expanded single-currency rule.
                    money = named_currency.upper() if named_currency and named_currency != '$' else currency
                    if named_currency == '$' and re.search(r'\b(?:Canadian|Australian|Hong Kong|Singapore)\s+dollars?\b',words,re.I):
                        continue
                    unit = _unit(named_unit,currency=money,dollar_declared=bool(named_currency))
                    if unit is None:
                        continue
                    record = {'kind':'explicit-calendar-quarter-narrative',
                        'quote_id':q['quote_id'], 'quote_sha256':q['excerpt_sha256'],
                        'source_start':q['source_start']+para.start(),
                        'source_end':q['source_start']+para.end(),
                        'native_paragraph':paragraph,'native_sentence':sentence,'native_clause':words,
                        'literal_value':number[2], 'value':format(Decimal(value.replace(',','')),'f'),
                        'period':str(start)+'/'+str(end),'unit':unit[0],'currency':unit[1],
                        'scale':scale.lower() if scale else None,
                        'raw_sha256':columns['raw_sha256'],
                        'source_rule_hash':columns['content_hash'],
                        'report_currency_hash':columns['report_currency']['content_hash']}
                    records.append({**record,'content_hash':content_hash(record)})
    return records


def native_measurements(raw, *, original_text, matches, periods, query_terms):
    columns = table_measurements(raw,original_text=original_text)
    quote_rows = []
    for q in matches:
        if (q['source_start'] < 0 or q['source_end'] > len(original_text)
                or original_text[q['source_start']:q['source_end']] != q['excerpt']
                or hashlib.sha256(q['excerpt'].encode()).hexdigest() != q['excerpt_sha256']):
            raise ValueError('operating native proof lost its exact registered source span')
        quote_rows.append({**q,'quote_id':f"quote:{q['source_start']}:{q['source_end']}:{q['excerpt_sha256'][:16]}"})
    currency = columns['report_currency']['currency']
    records=[]
    for row in columns['measurements']:
        named = row['native_label']+' '+(row['unit_caption'] or '')
        dollars = '$' in row['native_row'] or any(re.search(r'\bdollars\b',s,re.I) for s in row['table_captions'])
        # An explicit unit in the physical row supersedes a preceding
        # subsection caption; the caption cannot relabel later tonnage as
        # gas prices merely because both share the same reporting table.
        unit = _unit(row['native_label'],currency=currency,dollar_declared=dollars)
        if unit is None:
            unit = _unit(row['unit_caption'] or '',currency=currency,dollar_declared=dollars)
        if unit is None:
            continue
        if unit[0].startswith('currency_per_'):
            denomination=' '.join([row['native_label'], row['unit_caption'] or '', *row['table_captions']])
            named=set(re.findall(r'\b(USD|CAD|EUR|GBP)\b',denomination))
            for word,code in [('Canadian','CAD'),('Australian','AUD'),('Singapore','SGD'),('Hong Kong','HKD')]:
                if re.search(r'\b'+word+r'\s+dollars?\b',denomination,re.I): named.add(code)
            if named and named != {currency}:
                continue
        # A per-unit amount is not the table's monetary millions. Its native
        # row/caption declares the denominator; explicit table exceptions
        # preserve the written per-unit number rather than scaling revenue.
        scale = _scale(row['native_label'])
        # A shared monetary multiplier is not silently discarded. A row's
        # per-unit annotation overrides it only when the original caption
        # explicitly excepts per-ton/per-MMBtu data.
        if unit[0].startswith('currency_per_') and scale is None:
            captions=' '.join(row['table_captions'])
            if _scale(captions) is not None and re.search(
                    r'\bexcept\b[^.;]{0,120}\bper[- ](?:ton|MMBtu)\b',captions,re.I) is None:
                continue
        for q in quote_rows:
            if not (q['source_start'] <= row['row_source_start'] and q['source_end'] >= row['row_source_end']):
                continue
            record = {'kind':'exact-raw-table-column','quote_id':q['quote_id'],
                'quote_sha256':q['excerpt_sha256'],'source_start':row['row_source_start'],
                'source_end':row['row_source_end'],'native_row':row['native_row'],
                'native_label':row['native_label'],'literal_value':row['literal_value'],
                'value':row['value'],'period':row['period'],'unit':unit[0],'currency':unit[1],
                'scale':scale,'raw_sha256':columns['raw_sha256'],
                'source_rule_hash':columns['content_hash'],
                'report_currency_hash':columns['report_currency']['content_hash'],
                'table_index':row['table_index'],'table_hash':row['table_hash'],
                'column_start':row['column_start'],'column_end':row['column_end'],
                'cell_hash':row['cell_hash'],'amount_cells':row['amount_cells'],
                'duration_header':row['duration_header'],'year_header':row['year_header']}
            records.append({**record,'content_hash':content_hash(record)})
    records += _narrative_records(original_text,columns=columns,quotes=quote_rows)
    # Only source-native candidates in the admitted reporting windows enter
    # this operation's proof. Lexical terms retrieve candidates; they certify
    # no economic equivalence. Retain every matching occurrence and reject
    # overflow instead of silently truncating conflicting rows.
    if (not isinstance(periods, (list, tuple)) or not 1 <= len(periods) <= 16
            or len(set(periods)) != len(periods)
            or any(not isinstance(p, str) or len(p) != 21 for p in periods)
            or not isinstance(query_terms, list) or not 1 <= len(query_terms) <= 16
            or any(not isinstance(t, str) or not 0 < len(t) <= 160 for t in query_terms)):
        raise ValueError('operating native proof lacks bounded request windows/terms')
    records = [r for r in records if r['period'] in periods and any(
        term.lower() in r.get('native_label', r.get('native_sentence', '')).lower()
        for term in query_terms)]
    records = list({r['content_hash']: r for r in records}.values())
    if len(records)>MAX_RECORDS:
        raise ValueError('operating native proof exceeds its bounded candidate measurements')
    body={'rule_ref':RULE_REF,'raw_sha256':columns['raw_sha256'],
        'text_sha256':columns['text_sha256'],'column_rule_ref':COLUMN_RULE,
        'column_proof_hash':columns['content_hash'],'report_currency':columns['report_currency'],
        'request_periods':list(periods),'request_query_terms':query_terms,
        'measurements':sorted(records,key=lambda r:(r['quote_id'],r['source_start'],r['period'],r['value'],r['content_hash']))}
    return {**body,'content_hash':content_hash(body)}


def registered_measurements(registry, registration, *, purpose, search_proof, periods):
    actual,text=registry._reresolve(registration,purpose=purpose)
    if actual!=registration or search_proof['request']['registration']!=registration:
        raise ValueError('operating native source differs from registered search authority')
    adapter=registry.adapters.get(registration['source_ref'])
    if adapter is None and registry.acquired_fetched_adapter is not None:
        acquired=registry.acquired_fetched_adapter
        alias=acquired.source_aliases.get(registration['source_ref'],registration['source_ref'])
        adapter=acquired.adapters.get(alias)
    if adapter is None or not hasattr(adapter,'spool'):
        return None
    bodies=[]
    source=registration['raw_source']
    if source['status']!='bound' or not source['content_hashes']:
        return None
    for sha in source['content_hashes']:
        raw=adapter.spool.read_object(sha)
        if hashlib.sha256(raw).hexdigest()!=sha:
            raise ValueError('operating native source raw hash drifted')
        # Only complete XHTML sources opt into the raw-column contract.
        # Simple synthetic/plain text sources retain literal quote checks.
        try:
            proof=native_measurements(raw,original_text=text,matches=search_proof['matches'],
                periods=periods,query_terms=search_proof['request']['query_terms'])
        except __import__('xml.etree.ElementTree',fromlist=['ParseError']).ParseError:
            continue
        except ValueError as exc:
            if str(exc) != 'operating source columns require a complete XHTML original':
                raise
            continue
        bodies.append(proof)
    if not bodies:
        return None
    body={'rule_ref':RULE_REF,'registration_ref':registration['id'],
        'registration_hash':registration['content_hash'],'manifest_hash':registration['manifest_hash'],
        'search_proof_hash':search_proof['content_hash'],'sources':bodies}
    return {**body,'content_hash':content_hash(body)}


def matching_measurements(proof, *, period, terms):
    if proof is None:
        return []
    if (not isinstance(proof,dict) or set(proof)!={'rule_ref','registration_ref','registration_hash','manifest_hash','search_proof_hash','sources','content_hash'}
            or proof.get('rule_ref')!=RULE_REF or proof['content_hash']!=content_hash({k:v for k,v in proof.items() if k!='content_hash'})
            or not isinstance(proof['sources'],list) or not 1<=len(proof['sources'])<=16):
        raise ValueError('operating native proof hash or contract differs')
    for source in proof['sources']:
        fields={'rule_ref','raw_sha256','text_sha256','column_rule_ref','column_proof_hash','report_currency','request_periods','request_query_terms','measurements','content_hash'}
        if (not isinstance(source,dict) or set(source)!=fields or source['rule_ref']!=RULE_REF
                or source['column_rule_ref']!=COLUMN_RULE
                or source['content_hash']!=content_hash({k:v for k,v in source.items() if k!='content_hash'})
                or not isinstance(source['measurements'],list) or len(source['measurements'])>MAX_RECORDS):
            raise ValueError('operating native source proof shape/hash differs')
        money=source['report_currency']
        if (not isinstance(money,dict) or set(money)!={'rule_ref','raw_sha256','currency','definitions','content_hash'}
                or money['raw_sha256']!=source['raw_sha256']
                or money['content_hash']!=content_hash({k:v for k,v in money.items() if k!='content_hash'})):
            raise ValueError('operating native currency proof hash/original differs')
        for row in source['measurements']:
            if (not isinstance(row,dict) or row['raw_sha256']!=source['raw_sha256']
                    or row['source_rule_hash']!=source['column_proof_hash']
                    or row['report_currency_hash']!=money['content_hash']
                    or row['period'] not in source['request_periods']
                    or row['content_hash']!=content_hash({k:v for k,v in row.items() if k!='content_hash'})):
                raise ValueError('operating native measurement hash/source/window differs')
    return [record for source in proof['sources'] for record in source['measurements']
        if record['period']==period and any(term.lower() in record.get('native_label',record.get('native_sentence','')).lower() for term in terms)]


def verify_native_measurement(candidate, quote, proof):
    """No positive result unless the exact original amount/period/unit closes."""
    if proof is None:
        return False
    if not any(source['measurements'] for source in proof.get('sources', [])):
        return False
    rows=matching_measurements(proof,period=candidate['period'],terms=[candidate['as_reported_label']])
    found=[]
    for row in rows:
        if (row['quote_id']==candidate['quote_id'] and row['quote_sha256']==hashlib.sha256(quote.encode()).hexdigest()
                and row['value']==candidate['value'] and row['unit']==candidate['unit']
                and row['currency']==candidate['currency'] and row['scale']==candidate['scale']):
            native=row.get('native_row',row.get('native_paragraph'))
            if native in quote and row['content_hash']==content_hash({k:v for k,v in row.items() if k!='content_hash'}):
                found.append(row)
    if not found:
        raise ValueError('operating value does not match its exact raw source column or explicit period clause')
    # Multiple occurrences of the same row/value in a repeated disclosure are
    # acceptable only as the exact same typed measure, never a different unit
    # or reporting window borrowed from another row/table.
    return True


def prompt_measurements(proof, targets):
    """Explicit native evidence metadata, kept separate from all quotations."""
    windows = {t['period'] for t in targets}
    fields = ('kind', 'quote_id', 'native_label', 'native_sentence', 'native_clause', 'literal_value',
              'value', 'period', 'unit', 'currency', 'scale', 'table_index',
              'column_start', 'column_end', 'duration_header', 'year_header',
              'content_hash')
    return {'rule_ref': RULE_REF, 'source_proof_hash': proof['content_hash'],
        'instruction': 'These are raw source column/clause identities, not assertions that a native metric answers the admitted definition. Preserve the original quote. Select only the exact company, scope and native label that answers the requested economic meaning; otherwise return unavailable. Do not mix segment and consolidated, product and nutrient, quarterly and cumulative, or actual and variance columns.',
        'native_measurements': _prompt_rows(proof,targets,fields)}


def _prompt_rows(proof, targets, fields):
    allowed={(t['period'],t['unit'],t['currency']) for t in targets}
    unique={}
    for source in proof['sources']:
        for row in source['measurements']:
            if (row['period'],row['unit'],row['currency']) not in allowed:
                continue
            # Overlapping registered excerpts can repeat the identical raw
            # cell/clause. Show one quote identity for that exact occurrence;
            # keep all distinct tables, cells, denominations and values.
            key=(row['kind'],row['raw_sha256'],row.get('table_hash'),row.get('cell_hash'),
                 row.get('native_clause'),row['source_start'],row['source_end'],row['period'],
                 row['value'],row['unit'],row['currency'],row['scale'])
            unique.setdefault(key,{k:row[k] for k in fields if k in row})
    return list(unique.values())
