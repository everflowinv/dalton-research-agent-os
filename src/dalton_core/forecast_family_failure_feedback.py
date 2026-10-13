"""Source-rebuilt partition diagnostics for ordinary planning, never authority."""
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import re

from .store import content_hash

CONTRACT_REF = 'forecast-family-failure-feedback:0.1'
MAX_ITEMS = 2
MAX_ALTERNATIVES = 4
MAX_SUMMARY_BYTES = 256_000
_POINTER = re.compile(r'^family-diagnostics:([a-f0-9]{24}):([a-f0-9]{64}):([a-f0-9]{64})(?:;|$)')


def _source_rows(rows, entry):
    key = (*entry['source_identity'], entry['concept'], entry['period_start'], entry['period_end'])
    return [r for r in rows if (r.get('ingest_id'), r.get('statement_context_ref'), r.get('statement'),
        r.get('concept'), r.get('period_start'), r.get('period_end')) == key]


def _family(record, rows, entry):
    from .forecast_source_family_partition import partitions
    axis, fixed = entry['axis'], entry['fixed_dimensions']
    peers = [r for r in rows if set(r.get('dimensions') or {}) == {axis, *fixed}
             and all(r['dimensions'].get(a) == m for a, m in fixed.items())]
    members = sorted({r['dimensions'][axis] for r in peers})
    if not members:
        raise ValueError('family feedback has no source members')
    # The temporary driver supplies the exact requested view, not permission to
    # adopt it or a claim that the model has already consumed an alternative.
    view = {'drivers': [{'ref': 'source-view', 'concept': entry['concept'],
             'statement': entry['source_identity'][2], 'statement_identity': {
             'statement': entry['source_identity'][2], 'dimensions': {axis: members[0], **fixed}}}]}
    f = {k: entry[k] for k in ('concept', 'period_start', 'period_end', 'axis', 'fixed_dimensions')}
    f.update(ingest_id=entry['source_identity'][0], statement_context_ref=entry['source_identity'][1],
             statement=entry['source_identity'][2], driver_ref='source-view', members=members)
    return partitions(view, rows, [f])[0]


def _detail(record, rows, entry):
    from .economic_invariants import ADDITIVE_SEGMENT_AXES, CONSOLIDATION_ITEMS_AXES, OPERATING_SEGMENT_MEMBERS
    from .forecast_consumed_source_groups import _wire
    if entry.get('group', {}).get('bridge') or entry.get('group', {}).get('reconciling_items'):
        return None
    source = _source_rows(rows, entry)
    actual = _family(record, source, entry)
    group = actual['group']
    # Complex reconciliations remain with their existing invariant diagnostic;
    # this contract does not strip a bridge to claim a flat partition failure.
    if group.get('bridge') or group.get('reconciling_items'):
        return None
    total = Decimal(str(group['total']))
    parts = sum((Decimal(str(p['value'])) for p in group['parts']), Decimal(0))
    if parts == total and len(group['parts']) >= 2:
        return None
    alternatives, seen = [], set()
    for row in source:
        dims = row.get('dimensions') or {}
        for axis in dims:
            fixed = {a: m for a, m in dims.items() if a != axis}
            key = (axis, tuple(sorted(fixed.items())))
            if (axis not in ADDITIVE_SEGMENT_AXES or key in seen
                    or key == (entry['axis'], tuple(sorted(entry['fixed_dimensions'].items())))
                    or not all(a in CONSOLIDATION_ITEMS_AXES and m in OPERATING_SEGMENT_MEMBERS
                               for a, m in fixed.items())):
                continue
            seen.add(key)
            choice = _family(record, source, {**entry, 'axis': axis, 'fixed_dimensions': fixed})
            g = choice['group']
            if (len(g['parts']) >= 2 and not g.get('bridge') and not g.get('reconciling_items')
                    and sum((Decimal(str(p['value'])) for p in g['parts']), Decimal(0)) == Decimal(str(g['total']))
                    and len({str(o['unit']).casefold() for o in choice['origins']}) == 1):
                alternatives.append({k: choice[k] for k in ('axis', 'fixed_dimensions', 'origins', 'group')})
    alternatives.sort(key=str)
    drivers = {d['ref']: d for d in record.get('drivers') or []}
    return {'source_identity': entry['source_identity'], 'concept': entry['concept'],
        'period_start': entry['period_start'], 'period_end': entry['period_end'],
        'axis': entry['axis'], 'fixed_dimensions': entry['fixed_dimensions'],
        'driver_identities': [{'driver_ref': ref, 'statement_identity': drivers[ref]['statement_identity']}
                              for ref in entry['driver_refs']],
        'source_rows_hash': content_hash(_wire(source)), 'group': group, 'origins': actual['origins'],
        'member_sum': format(parts, 'f'), 'reported_total': format(total, 'f'),
        'difference': format(total-parts, 'f'),
        'same_source_complete_choices': alternatives[:MAX_ALTERNATIVES],
        'omitted_choice_count': max(0, len(alternatives)-MAX_ALTERNATIVES),
        'omitted_choices_hash': content_hash(alternatives[MAX_ALTERNATIVES:]),
        'next_action': ('Select a source-bound complete identity explicitly and rebuild every driver/history; '
                        'never splice members across fixed dimensions or invent a reconciliation.'
                        if alternatives else 'Obtain exact missing family members/reconciliation for this source and period; no residual may be invented.')}


def build(record, rows, scope):
    from .forecast_execution_statement_scope import RULE_REF
    if scope is None or scope.get('rule_ref') != RULE_REF:
        return None
    from .economic_invariants import _segment_sum, FAIL
    details, other_failures = [], []
    for entry in scope['arithmetic_partitions']:
        item = _detail(record, rows, entry)
        if item is not None:
            details.append(item)
        elif _segment_sum([entry['group']]).status == FAIL:
            other_failures.append(entry['content_hash'])
    if not details:
        return None
    body = {'contract_ref': CONTRACT_REF, 'company_ref': record['company_ref'],
        'mission_version_ref': record['mission_version_ref'], 'spec_ref': record['spec_ref'],
        'spec_hash': record['spec_hash'], 'inputs_hash': record['inputs_hash'],
        'scope_hash': scope['content_hash'], 'source_groups_hash': scope['source_groups_hash'],
        'items': details[:MAX_ITEMS], 'total_count': len(details),
        'omitted_count': max(0,len(details)-MAX_ITEMS), 'omitted_items_hash': content_hash(details[MAX_ITEMS:]),
        'checked_family_count': len(scope['arithmetic_partitions']),
        'other_invariant_failure_count': len(other_failures), 'other_invariant_failures_hash': content_hash(other_failures),
        'financial_evidence': False, 'retry_authority': False}
    return {**body, 'content_hash': content_hash(body)}


def pointer(state_dir, ticket_ref, summary):
    """A hash pointer fits the unchanged 500-character append-only ledger."""
    detail = summary.get('family_partition_failures')
    if not isinstance(detail, Mapping) or detail.get('contract_ref') != CONTRACT_REF:
        return None
    if not re.fullmatch('model-forecast-run:[a-f0-9]{24}', ticket_ref):
        return None
    ticket_ref = ticket_ref.split(':', 1)[1]
    path = Path(state_dir)/'model-forecast-runs'/ticket_ref/'summary.json'
    raw = path.read_bytes()
    if len(raw)>MAX_SUMMARY_BYTES or json.loads(raw) != summary:
        raise ValueError('family feedback summary differs from the settled child')
    return f"family-diagnostics:{ticket_ref}:{hashlib.sha256(raw).hexdigest()}:{detail['content_hash']}"


def read(connection, *, state_dir, company_ref, mission, event):
    """Rebuild exact facts under current spec/context. A file hash is not enough."""
    match = _POINTER.match(event['reason'])
    if match is None or mission is None:
        return None
    ticket, summary_hash, detail_hash = match.groups()
    path = Path(state_dir)/'model-forecast-runs'/ticket/'summary.json'
    if path.stat().st_size > MAX_SUMMARY_BYTES:
        raise ValueError('family feedback summary exceeds bound')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=summary_hash:
        raise ValueError('family feedback original summary drifted')
    summary = json.loads(raw); detail = summary['family_partition_failures']
    if (detail.get('contract_ref')!=CONTRACT_REF or detail.get('content_hash')!=detail_hash
            or content_hash({k:v for k,v in detail.items()if k!='content_hash'})!=detail_hash
            or detail.get('company_ref')!=company_ref or summary.get('company_ref')!=company_ref
            or summary.get('forecast_status')!='unavailable:economic_invariants'
            or detail.get('mission_version_ref')!=mission['id']
            or detail.get('financial_evidence') is not False or detail.get('retry_authority') is not False
            or not 0<len(detail['items'])<=MAX_ITEMS):
        raise ValueError('family feedback summary/current mission identity differs')
    from .coverage_mission import CoverageMissionAuthority
    owner=CoverageMissionAuthority.__new__(CoverageMissionAuthority);owner.connection=connection
    held=owner.mission(mission['id'])
    if held!=mission or company_ref not in {e['company_ref'] for e in held['universe']}:
        raise ValueError('family feedback mission authority or company universe differs')
    active=connection.execute('SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?',
                              (held['mission_ref'],)).fetchone()
    if active is None or active[0]!=mission['id']:
        return None
    current=owner.latest_company_model_spec(company_ref)
    if current is None or current['spec_id']!=detail['spec_ref'] or current['content_hash']!=detail['spec_hash']:
        return None  # A later specification supersedes the old operational gap.
    if summary.get('spec_ref')!=current['spec_id']:
        raise ValueError('family feedback child spec differs')
    if not event['item_key'].startswith(company_ref+'|'+str(summary['digest'])+'|'):
        raise ValueError('family feedback event belongs to another input')
    from .statement_context import _latest, statement_rows
    from .company_model_annual_projection import verify_statement_filing
    from .forecast_consumed_source_groups import _wire
    declarations = {('fcf-input:' if d['ref'].startswith('fcf-') else 'economic:')+d['ref']:d
                    for d in current.get('revenue_drivers') or []}
    for item in detail['items']:
        ingest, context, statement = item['source_identity']
        if _latest(connection, ingest)!=context:
            return None
        filing=connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?',(ingest,)).fetchone()
        if filing is None or filing['company_ref']!=company_ref:
            raise ValueError('family feedback source belongs to another issuer')
        verify_statement_filing(connection,dict(filing),allow_legacy_metadata=True)
        source = _source_rows(statement_rows(connection,ingest,version_id=context),item)
        if content_hash(_wire(source))!=item['source_rows_hash']:
            raise ValueError('family feedback complete original source rows differ')
        drivers=[]
        for bound in item['driver_identities']:
            declaration=declarations.get(bound['driver_ref'])
            if (declaration is None or declaration.get('basis_concept')!=item['concept']
                    or declaration.get('basis_identity')!=bound['statement_identity']):
                raise ValueError('family feedback current declared driver differs')
            drivers.append({'ref':bound['driver_ref'],'statement_identity':bound['statement_identity']})
        rebuilt=_detail({'drivers':drivers},source,{**item,'driver_refs':[d['ref']for d in drivers]})
        if rebuilt!=item:
            raise ValueError('family feedback amounts/origins/independent choices do not replay')
    return detail
