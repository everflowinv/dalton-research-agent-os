"""Bounded, source-neutral retrieval targets for material acquisition.

These describe passages needed for research. They do not certify a numeric
answer or replace the directed-reader's immutable document registration.
"""
from __future__ import annotations
from collections.abc import Mapping
import re
from urllib.parse import urlparse
from .store import content_hash

RULE_REF = 'material-answer-retrieval:0.2'
class MaterialRetrievalError(ValueError):
    pass

FORMS = {'10-K': 'sec_10k', '10-Q': 'sec_10q', '8-K': 'sec_8k'}
# Only exact lexical aliases: accounting concepts with different definitions
# (e.g. operating income/margin, bookings/book-to-bill) are never merged.
# Exact domain labels only; this is not a general question translator.
# Untyped questions remain partial even when all detected labels are found,
# because causal/compound answer obligations may remain unrepresented.
_LABEL_TERMS = (
    ('营业成本', 'cost of revenues'), ('销售及管理费用', 'selling general administrative'),
    ('营业利润率', 'operating margin'), ('一次性项目', 'one-time items'),
    ('汇率', 'foreign exchange'), ('指引', 'guidance'), ('员工人数', 'employees'),
    ('利用率', 'utilization'), ('新签订单', 'new bookings'),
)
_ALIASES = (
    ('headcount', 'employees'),
    ('utilization', 'utilisation'),
    ('cost of revenues', 'cost of revenue'),
    ('selling general administrative', 'selling general and administrative', 'SG&A'),
)

def _plain(text):
    return ' '.join(str(text or '').split())

def filing_kinds(request: Mapping) -> list[str]:
    """Explicit requested forms only, independently of target translation."""
    need = request.get('need') or {}
    typed = (request.get('acceptance') or {}).get('retrieval')
    question = str(need.get('question') or '')
    wanted = list(need.get('doc_kinds') or [])
    if isinstance(typed, Mapping):
        question += ' ' + str(typed.get('wants') or '')
        wanted.extend(typed.get('doc_kinds') or [])
    forms = re.findall(r'(?<![A-Za-z0-9])(?:10[- ]?[KQ]|8[- ]?K)(?:/A)?(?![A-Za-z0-9])', question, re.I)
    normalize = {'10K': 'sec_10k', '10Q': 'sec_10q', '8K': 'sec_8k'}
    return list(dict.fromkeys([k for k in wanted if k in FORMS.values()] +
        [normalize[re.sub(r'/A$', '', f.upper()).replace(' ', '').replace('-', '')] for f in forms]))


def contract(request: Mapping) -> dict | None:
    need = request.get('need') or {}
    if need.get('kind') != 'answer': return None
    acceptance = request.get('acceptance') or {}
    typed = acceptance.get('retrieval')
    raw = str(need.get('question') or '')
    chunks = re.split(r'\s+-- (wants|terms):\s*', raw)
    question = chunks[0]
    extras = dict(zip(chunks[1::2], chunks[2::2]))
    if isinstance(typed, Mapping):
        if (set(typed) - {'rule_ref', 'query_terms', 'wants', 'doc_kinds', 'selected_document'}
                or not {'rule_ref', 'query_terms', 'wants', 'doc_kinds'} <= set(typed)
                or typed.get('rule_ref') != RULE_REF):
            raise MaterialRetrievalError('unsupported structured material retrieval contract')
        terms = typed.get('query_terms') or []
        wants = typed.get('wants') or ''
        kinds = typed.get('doc_kinds') or []
    else:
        terms = [t.strip() for t in extras.get('terms', '').split(',') if t.strip()]
        wants = extras.get('wants', '')
        kinds = need.get('doc_kinds') or []
    scope_complete = bool(terms)
    if not terms:
        terms = [term for label, term in _LABEL_TERMS if label in question]
    if (not isinstance(terms, list) or not terms or len(terms) > 12
            or any(not isinstance(t, str) or not t.strip() or len(t) > 160 for t in terms)):
        if typed is not None or extras.get('terms'):
            raise MaterialRetrievalError('material retrieval target list is invalid or exceeds its finite bound')
        return None
    required = filing_kinds(request)
    selected = typed.get('selected_document') if isinstance(typed, Mapping) else None
    if selected is not None and (not isinstance(selected, Mapping)
            or set(selected) != {'document_ref', 'document_version_hash'}
            or not isinstance(selected.get('document_ref'), str) or not selected['document_ref']
            or re.fullmatch(r'[0-9a-f]{64}', str(selected.get('document_version_hash'))) is None):
        raise MaterialRetrievalError('selected original document identity is invalid')
    # Filing date cannot stand in for a fiscal period. Unbound textual period
    # requirements stay partial until the normal reader proves the requested
    # period. An index text hash is likewise not a registration-version hash.
    period_unproved = bool(re.search(r'\bFY\s*\d{2,4}\b|\bQ[1-4]\b|20\d{2}|第[一二三四1234]季度|[一二三四]季度',
                                    question + ' ' + str(wants), re.I))
    groups = []
    for term in terms:
        word = _plain(term)
        aliases = next((a for a in _ALIASES if word.casefold() in {x.casefold() for x in a}), (word,))
        key = aliases[0].casefold()
        if not any(g['target'] == key for g in groups):
            groups.append({'target': key, 'terms': list(aliases)})
    body = {'rule_ref': RULE_REF, 'question': question, 'wants': str(wants),
            'groups': groups, 'doc_kinds': required,
            'scope_complete': (scope_complete and len(required) <= 1 and not period_unproved and selected is None
                               and not any(_plain(t).casefold() in {'people', 'workforce', 'talent'} for t in terms)),
            'period_unproved': period_unproved, 'selected_document': dict(selected) if selected else None}
    return {**body, 'content_hash': content_hash(body)}

def retrieval_fields(inquiry: Mapping) -> dict | None:
    directed = inquiry.get('directed_document')
    if not isinstance(directed, Mapping): return None
    terms = directed.get('query_terms') or []
    if not isinstance(terms, list) or not terms or len(terms) > 12: return None
    draft = {'need': {'kind': 'answer', 'question': inquiry.get('question') or ''},
             'acceptance': {'retrieval': {'rule_ref': RULE_REF, 'query_terms': list(terms),
                            'wants': inquiry.get('wants') or '', 'doc_kinds': []}}}
    value = contract(draft)
    if value is None: return None
    result = {'rule_ref': RULE_REF, 'query_terms': list(terms),
              'wants': inquiry.get('wants') or '', 'doc_kinds': value['doc_kinds']}
    if directed.get('document_ref') is not None or directed.get('document_version_hash') is not None:
        result['selected_document'] = {k: directed.get(k) for k in ('document_ref', 'document_version_hash')}
        # Validate without changing the immutable selected version identity.
        contract({'need': draft['need'], 'acceptance': {'retrieval': result}})
    return result

def phrase_matches(text: str, phrase: str) -> bool:
    # Allow punctuation/"and" in source labels; never let a lone generic token
    # stand in for a declared multiword target.
    words = re.findall(r'[A-Za-z0-9]+|[一-鿿]+', phrase.casefold())
    if not words: return False
    pattern = r'(?<!\w)' + r'(?:[\W_]+(?:and[\W_]+)?)'.join(re.escape(w) for w in words) + r'(?!\w)'
    return re.search(pattern, str(text or ''), re.I) is not None

def issuer_matches(item: Mapping, company: str | None, identity: Mapping) -> bool:
    if company is None: return True
    if company not in (item.get('filed_company_refs') or []): return False
    # SEC archive paths carry the issuer's CIK independently of associations.
    cik = re.fullmatch(r'company:sec-cik:(\d+)', company)
    url = urlparse(str(item.get('url') or ''))
    if cik and item.get('kind') in FORMS.values():
        match = re.search(r'/Archives/edgar/data/(\d+)/', url.path, re.I)
        return (url.hostname in {'www.sec.gov', 'sec.gov'} and match is not None
                and int(match.group(1)) == int(cik.group(1)))
    # Non-SEC company fixtures/issuer materials must also name their own
    # issuer in title/house; filed/listed associations alone are insufficient.
    names = [identity.get('name'), identity.get('ticker'), *(identity.get('aliases') or [])]
    title = str(item.get('title') or '') + ' ' + str(item.get('house') or '')
    return any(isinstance(n, str) and n and phrase_matches(title, n) for n in names)
