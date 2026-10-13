"""Bounded acquired-original evidence for quality, never a financial authority."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import content_hash

CONTRACT = 'initial-quality-acquired-source-scope:1'
CALL_RE = re.compile(r'(?:welcome\s+to.{0,180}(?:financial results|earnings).{0,70}(?:conference call|teleconference)|欢迎.{0,100}(?:财报|业绩).{0,30}(?:电话会|会议))', re.I | re.S)
MANAGEMENT_RE = re.compile(r'(?:investor relations|\bCEO\b|\bCFO\b|chief executive|chief financial|投资者关系|首席执行官|首席财务官)', re.I)
THIRD_PARTY_RE = re.compile(r'(?:发言人\s*(?:专家|分析师)|专家[：:]|分析师[：:]|数据交流|Sales Commentary Only|Not Research)', re.I)
COMPARISON_RE = re.compile(r'(?:相比|比较|对比|对照|参照|参考|相对|竞争|行业|同业|同行|vs\.?|versus|compar|compet|industry|hyperscaler)', re.I)
_MONTHS = {name:i for i,name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'),1)}


def named(text, names):
    from .document_subject import _mentions
    return sorted(ref for ref, aliases in names.items() if _mentions(text, aliases))


def call_role(proof, subject):
    """A title, requested issuer or generic transcript kind cannot prove a call."""
    source = proof['quality_source']; header = source['header']; names = source['names']
    if source['header_hash'] != content_hash(header):
        raise ValueError('quality original header hash differs')
    match = CALL_RE.search(header)
    if match and MANAGEMENT_RE.search(header) and named(match.group(), names) == [subject]:
        # Date is taken from the original event declaration, never upload date.
        date = re.search(r'(?:as of today|today is|dated|held on)[,\s]*([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+(20\d{2})', header, re.I)
        if date and date[1].title() in _MONTHS:
            from datetime import date as Date
            event_date = Date(int(date[3]), _MONTHS[date[1].title()], int(date[2])).isoformat()
            return {'role':'issuer_earnings_call', 'event_date':event_date,
                    'document_ref':proof['document_ref'], 'document_hash':proof['document_sha256'],
                    'header_hash':source['header_hash']}
        return {'role':'unverified', 'reason':'earnings_call_event_date_unproved'}
    if THIRD_PARTY_RE.search(header):
        return {'role':'third_party_discussion', 'header_hash':source['header_hash']}
    return {'role':'unverified', 'reason':'earnings_call_activity_or_issuer_unproved'}


def source_context(state_dir, art):
    """Exact spans + one held header per transcript; a single bounded RO child."""
    from .planner_paid_output_recovery import _authority_connection
    from .evidence_index import _PASSAGE_COLUMNS, _passage_view
    from .initial_screen_failure_feedback import _checked_originals
    state = Path(state_dir)
    inputs = ((art.get('contract') or {}).get('pack') or {}).get('inputs') or []
    refs = {str(item['ref']) for section in art['sections'] for item in section.get('source_refs') or ()
            if str(item.get('ref') or '').startswith('doc-passage:')}
    transcripts = {str(item['ref']):item for item in inputs if item.get('doc_kind') == 'transcript'}
    if len(transcripts)>16 or len(refs)>48:
        raise ValueError('quality original scope exceeds bound')
    headers = {}
    with _authority_connection(state/'document-index.sqlite') as index:
        for doc, item in sorted(transcripts.items()):
            row = index.execute(f'SELECT {_PASSAGE_COLUMNS} FROM evidence_passages p JOIN evidence_documents d ON d.doc_id=p.doc_id WHERE d.document_ref=? AND d.text_sha256=? ORDER BY p.ordinal LIMIT 1',
                                (doc,item.get('sha256'))).fetchone()
            if row is None: raise ValueError('quality held transcript original is unavailable')
            head = _passage_view(index,row)
            refs.add(head['passage_ref']); headers[doc] = head['passage_ref']
    proofs = _checked_originals(state, refs, include_quality_metadata=True)
    # Reject using a different original than the frozen pack, including cited spans.
    expected = {str(item['ref']):item.get('sha256') for item in inputs if item.get('kind')=='document'}
    for proof in proofs.values():
        if proof['document_ref'] in expected and proof['document_sha256'] != expected[proof['document_ref']]:
            raise ValueError('quality source differs from frozen pack')
    roles = {doc:call_role(proofs[ref],art['subject_ref']) for doc,ref in headers.items()}
    return {'contract_ref':CONTRACT,'proofs':proofs,'call_roles':roles,
            'content_hash':content_hash({'proofs':proofs,'call_roles':roles})}


def issuer_finding(section, item, subject, evidence):
    """Check the claimed figure's own sentence; whole-document peer lists are not claims."""
    proof = evidence['proofs'].get(item.get('ref'))
    if proof is None: return 'issuer_binding_unverified'
    names = proof['quality_source']['names']; text = proof['text']; mentioned = set(named(text,names))
    bound = [number for number in section.get('numbers') or () if number.get('passage_ref') == item.get('ref')]
    if bound:
        # The full financial validator already proves the cited amount. Here
        # inspect the original quotation's subject, without reinterpreting its
        # display units or comparing translated digits.
        from .product_contract import passage_quote_matches
        header_names = set(named(proof['quality_source']['header'],names))
        for number in bound:
            quote = str(number.get('text') or '')
            declared = named(str(number.get('metric') or ''),names)
            claimed = declared[0] if len(declared)==1 else subject
            if claimed != subject and claimed not in named(section['body'],names):
                return 'numeric_issuer_binding_unverified'
            if not passage_quote_matches(quote,text):
                return 'numeric_issuer_binding_unverified'
            if mentioned <= {claimed}:
                if claimed in mentioned or header_names == {claimed} or call_role(proof,claimed)['role']=='issuer_earnings_call':
                    continue
                return 'numeric_issuer_binding_unverified'
            # The exact amount-bearing quote must say whose amount it is, or
            # its nearest explicit original subject must be the target. A
            # document-wide requested issuer cannot override a foreign claim.
            quote_names = set(named(quote,names))
            if (len(quote_names)>1 and claimed in quote_names
                    and quote_names <= set(named(section['body'],names))
                    and COMPARISON_RE.search(section['body'])):
                # The exact quotation is an explicitly named peer comparison,
                # not a scalar projected from a different issuer's sentence.
                continue
            scan = re.sub(r'\s+', ' ', re.sub(r'(?<=\w)-\r?\n[ \t]*(?=\w)', '-', text)).strip()
            shown = re.sub(r'\s+', ' ', quote).strip()
            at = scan.find(shown)
            if at < 0: return 'numeric_issuer_binding_unverified'
            before = scan[max(0,at-1024):at+len(shown)]
            hits=[]
            for ref,aliases in names.items():
                for alias in aliases:
                    for match in re.finditer(r'(?<![A-Za-z0-9])'+re.escape(alias)+r'(?![A-Za-z0-9])' if re.search(r'[A-Za-z]',alias) else re.escape(alias),before,re.I):
                        hits.append((match.start(),ref))
            if not hits or max(hits)[1] != claimed:
                return 'numeric_issuer_binding_unverified'
        return None
    # Qualitative comparison is allowed only when the cited original and body
    # both explicitly discuss the covered companies; this never certifies a value.
    header_names = set(named(proof['quality_source']['header'],names))
    if subject in mentioned or (not mentioned and (header_names == {subject} or call_role(proof,subject)['role']=='issuer_earnings_call')):
        return None
    body_names = set(named(section['body'],names))
    if mentioned & body_names and COMPARISON_RE.search(section['body']) and COMPARISON_RE.search(text):
        return None
    return 'issuer_binding_unverified'
