"""A finite source-bound edit of an estimate annotation, never its prose."""
from decimal import Decimal, InvalidOperation
import re
from .store import content_hash

CONTRACT_REF = 'initial-screen-estimate-citation-paths:0.1'
ERROR = 'an estimate entry must state its figure'
_METRICS = {
    'revenue': {'revenue', 'Revenue', '收入', '营业收入', '营收'},
    'eps': {'eps', 'EPS', '每股收益', '稀释每股收益', '摊薄每股收益'},
}


def _source(number, pack):
    from .product_contract import tag_entry
    from .mission_deliverable import value_tokens, MAX_NUMBER_TEXT
    ref = number.get('estimate')
    # These are exact tag spellings, not a general assumption-ref rewrite.
    if not isinstance(ref, str):
        raise ValueError('estimate repair original reference is not a tag')
    matched = re.fullmatch(r'(?:assumption:)?(N[1-9][0-9]*)', ref)
    if matched is None:
        raise ValueError('estimate repair has no exact original pack tag')
    tag = matched[1]
    entry = tag_entry(pack, tag)
    if (not entry or entry.get('kind') != 'number' or entry.get('source_kind') != 'ours'
            or entry.get('ref') != 'pack-json:' + entry.get('pointer', '')):
        raise ValueError('estimate repair tag is not a held our-model numeric source')
    pointer = entry['pointer']
    match = re.fullmatch(r'/our_model/years/(0|[1-9][0-9]*)/(revenue|eps)/value', pointer)
    if match is None:
        raise ValueError('estimate repair numeric source path is unsupported')
    data = pack.get('json', {})
    ours = data.get('our_model', {})
    try:
        period = ours['years'][int(match[1])]
        leaf = period[match[2]]
        raw_value = leaf['value']
        value = Decimal(str(raw_value))
    except (KeyError, IndexError, TypeError, InvalidOperation):
        raise ValueError('estimate repair lacks its exact available numeric leaf') from None
    if (data.get('company_ref') != pack.get('company_ref') or ours.get('status') != 'available'
            or leaf.get('status') != 'available' or leaf.get('method') != 'annual_projection' or not value.is_finite()
            or str(raw_value) != str(entry.get('value'))
            or period.get('label') != entry.get('period') or number.get('period') != period.get('label')
            or number.get('metric') not in _METRICS[match[2]]
            or not str(ours.get('model', {}).get('version_ref', '')).startswith('forecast-model-version:')):
        raise ValueError('estimate repair company/metric/period/value/model binding differs')
    native = leaf.get('unit')
    if match[2] == 'revenue':
        currency = {'usd': '美元', 'eur': '欧元', 'gbp': '英镑', 'jpy': '日元', 'cny': '人民币'}.get(native)
        scale = {'÷1e9': '十亿', '÷1e6': '百万', '÷1e3': '千'}.get(entry.get('transform'))
        if currency is None or scale is None or number.get('unit') != scale + currency:
            raise ValueError('estimate repair source unit/scale differs')
    elif native != 'usd_per_share' or number.get('unit') not in {'每股美元', '美元/股', 'USD/share', 'usd_per_share'}:
        raise ValueError('estimate repair per-share source unit differs')
    text = number.get('text')
    display = str(entry.get('text') or '')
    if (not isinstance(text, str) or not text or value_tokens(text, include_small=True)
            or not display or not value_tokens(display, include_small=True)):
        raise ValueError('estimate repair is not a missing-figure annotation with a held source figure')
    replacement = text + '（源数值：' + display + '）'
    if len(replacement.strip()) > MAX_NUMBER_TEXT:
        raise ValueError('estimate repair exact source annotation exceeds the existing text bound')
    # The full original rendered source spelling includes its value and scale.
    # No Decimal rounding, guessed residual, new number or body edit occurs.
    return {'contract_ref': CONTRACT_REF, 'tag': tag, 'source_ref': entry['ref'],
        'source_entry_hash': content_hash(entry), 'source_leaf_hash': content_hash(leaf),
        'company_ref': pack['company_ref'], 'model_ref': ours['model']['version_ref'],
        'pointer': pointer, 'source_value': str(raw_value), 'source_unit': native,
        'period': period['label'], 'replacement_estimate': tag,
        'replacement_text': replacement}


def plan(section, pack):
    """One exact failing annotation only; ambiguous/missing sources refuse."""
    from .mission_deliverable import value_tokens
    candidates = [(i, n) for i, n in enumerate(section.get('numbers', []))
                  if isinstance(n, dict) and n.get('estimate') is not None
                  and not value_tokens(str(n.get('text') or ''), include_small=True)]
    if len(candidates) != 1:
        raise ValueError('estimate repair missing-figure target is ambiguous')
    index, number = candidates[0]
    return {str(index): _source(number, pack)}


def assert_edit(old, changed, proof, pack):
    if _source(old, pack) != proof:
        raise ValueError('estimate repair source proof changed')
    expected = dict(old, estimate=proof['replacement_estimate'], text=proof['replacement_text'])
    if changed != expected:
        raise ValueError('estimate repair changed an unapproved field/value/source')
