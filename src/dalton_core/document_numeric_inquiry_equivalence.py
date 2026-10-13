"""New admission matching; historical v1 permission proofs keep their reader.

Typed numeric requirements are executable scopes, not numbers mentioned in a
question. A calendar date or a recognition horizon cannot make two metrics
equivalent. No answer values or new execution permission are created here.
"""
from collections.abc import Mapping
import re

from .document_inquiry_equivalence import (
    InquirySignature, inquiry_signature_for, signatures_equivalent,
)
from .directed_numeric_research import normalize_targets
from .store import canonical_json

RULE_REF = 'mission-document-research:admission-equivalence:v2'
_DATE = re.compile(
    r'(?<![0-9])(?:19|20)[0-9]{2}\s*(?:年\s*(?:0?[1-9]|1[0-2])\s*月\s*'
    r'(?:0?[1-9]|[12][0-9]|3[01])\s*[日号]|[-/](?:0?[1-9]|1[0-2])[-/]'
    r'(?:0?[1-9]|[12][0-9]|3[01]))(?![0-9])')
_MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
_ENGLISH_DATE = re.compile(
    r'(?i)(?<![A-Za-z0-9])(?:' + _MONTH + r'\s+(?:[1-9]|[12][0-9]|3[01]),?\s+(?:19|20)[0-9]{2}'
    r'|(?:[1-9]|[12][0-9]|3[01])\s+' + _MONTH + r'\s+(?:19|20)[0-9]{2})(?![0-9])')


def _source(strategy):
    ref, digest = strategy.get('document_ref'), strategy.get('document_version_hash')
    if (not isinstance(ref, str) or not ref.strip() or not isinstance(digest, str)
            or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
        raise ValueError('inquiry equivalence needs an exact document version')
    return ref, digest


def _scope(strategy):
    # Definitions contain qualifiers (consolidated/segment, gross/net, etc.).
    # Only the local target handle and ordering may change without changing the
    # requested financial fact. Unknown fields and forged definition hashes fail.
    rows = [{k: t[k] for k in ('metric_ref', 'metric_definition_hash', 'period',
                              'unit', 'currency', 'basis')}
            for t in normalize_targets(strategy['numeric_targets'], contract_ref=(strategy['strategy_version'] if strategy.get('strategy_version') == 'directed-document-operating-numeric:0.1' else 'directed-document-numeric:0.1'))]
    return tuple(sorted(canonical_json(row) for row in rows))


def _optional_source(strategy):
    """Untyped history may have no original binding; it proves no equivalence."""
    try:
        return _source(strategy)
    except ValueError:
        return None


def admission_match(company_ref, inquiry, other):
    """Return a proved matching scope, or None; never a shared-number shortcut.

    The caller derives the new inquiry from a formal plan and strongly reads
    a matched historical admission. Immutable v1 revision/upgrade proof readers
    are deliberately separate from this new-write decision.
    """
    if inquiry.get('company_ref', company_ref) != company_ref or other.get('company_ref', company_ref) != company_ref:
        return None
    left = inquiry.get('directed_document') or {}
    right = other.get('directed_document') or {}
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise ValueError('inquiry strategy is not an object')
    typed_left, typed_right = 'numeric_targets' in left, 'numeric_targets' in right
    a, b = inquiry_signature_for(company_ref, inquiry), inquiry_signature_for(company_ref, other)
    if typed_left or typed_right:
        lscope = _scope(left) if typed_left else None
        rscope = _scope(right) if typed_right else None
        lsource = _source(left) if typed_left else _optional_source(left)
        rsource = _source(right) if typed_right else _optional_source(right)
        if lsource is None or rsource is None or lsource != rsource:
            return None
        if typed_left and typed_right:
            if (left.get('strategy_version') == right.get('strategy_version')
                    and a.target == b.target and lscope == rscope):
                return 'numeric_targets'
            return None
        # A prior qualitative read can require a numeric upgrade only when it
        # asked the exact question from the same original. Shared dates, amounts
        # and horizons are insufficient, and do not borrow its paid authority.
        return 'question' if a.target == b.target and a.text and a.text == b.text else None
    from .document_inquiry_equivalence import question_figures
    # Keep original question text for exact equality; mask only date tokens in
    # the new-write figure comparison, leaving v1 functions and replay untouched.
    a = InquirySignature(a.company_ref, a.text,
        question_figures(_ENGLISH_DATE.sub(' ', _DATE.sub(' ', str(inquiry.get('question') or '')))), a.target)
    b = InquirySignature(b.company_ref, b.text,
        question_figures(_ENGLISH_DATE.sub(' ', _DATE.sub(' ', str(other.get('question') or '')))), b.target)
    return signatures_equivalent(a, b)
