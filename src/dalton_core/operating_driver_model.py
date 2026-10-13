"""Forecast source replay for explicit canonical operating-driver choices."""
from __future__ import annotations
import copy
from pathlib import Path
from .store import canonical_json
from .operating_driver_context import OperatingDriverReadContext, metric_history, selected_context

BASIS_REF = 'forecast-canonical-operating-source:0.1'
STATUS = 'verified_reported'


def driver(*, declared, series):
    if (declared.get('basis_concept') is not None or series['status'] != STATUS
            or declared['basis_operating_metric_ref'] != series['metric_ref']
            or declared['basis_operating_context_hash'] != series['context_hash']
            or declared['unit'] != series['unit'] or not series['history']):
        raise ValueError('economic operating driver differs from its selected reported source')
    ref='economic:'+declared['ref']
    return {'ref':ref,'kind':'statement_line','label':declared['label'],
        'concept':series['metric_ref'],'statement':None,'unit':series['unit'],'status':STATUS,
        'role':None,'spec_rows':[declared['ref']],'note':'Independently verified reported operating metric, not GAAP.',
        'history':copy.deepcopy(series['history']), 'structure_line_ref':ref,
        'forecast_method':'quarterly_growth','forecast_base_ref':None,
        'operating_source':{'contract_ref':BASIS_REF,'metric_ref':series['metric_ref'],
            'context_hash':series['context_hash'],'evidence_refs':copy.deepcopy(series['evidence_refs']),
            'context_evidence_refs':copy.deepcopy(series['context_evidence_refs'])}}


def validate_driver(value):
    source=value.get('operating_source')
    if not isinstance(source,dict) or set(source)!={'contract_ref','metric_ref','context_hash','evidence_refs','context_evidence_refs'}:
        raise ValueError('operating forecast driver has no complete explicit source binding')
    if (source['contract_ref'] != BASIS_REF or value['concept'] != source['metric_ref']
            or value['status'] != STATUS or value['statement'] is not None or value['role'] is not None
            or not source['evidence_refs']):
        raise ValueError('operating forecast driver masquerades as a filed statement')
    from .store import content_hash
    import re
    if re.fullmatch('[0-9a-f]{64}',source['context_hash']) is None:
        raise ValueError('operating forecast source context hash differs')
    from .operating_driver_context import MAX_RECORDS
    for field, fields in (('evidence_refs', {'ref', 'hash'}),
                          ('context_evidence_refs', {'ref', 'hash', 'admission_ref'})):
        records = source[field]
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
            raise ValueError('operating forecast source bindings exceed their bounded record set')
        refs = set()
        for record in records:
            if (not isinstance(record, dict) or set(record) != fields
                    or not isinstance(record['ref'], str)
                    or re.fullmatch(r'operating-driver-evidence:[0-9a-f]{32}', record['ref']) is None
                    or record['ref'] in refs or not isinstance(record['hash'], str)
                    or re.fullmatch('[0-9a-f]{64}', record['hash']) is None
                    or ('admission_ref' in fields and (not isinstance(record['admission_ref'], str)
                        or not 0 < len(record['admission_ref']) <= 240))):
                raise ValueError('operating forecast original record/admission bindings differ')
            refs.add(record['ref'])
    context_index = {r['ref']: r['hash'] for r in source['context_evidence_refs']}
    if any(context_index.get(r['ref']) != r['hash'] for r in source['evidence_refs']):
        raise ValueError('operating forecast metric records are outside their exact original context')
    return copy.deepcopy(value)


def verify_model_sources(owner, wire):
    operating=[d for d in wire['drivers'] if 'operating_source' in d]
    if not operating:
        return
    core=next(row[2] for row in owner.connection.execute('PRAGMA database_list').fetchall() if row[1]=='main')
    for item in operating:
        validate_driver(item)
        source=item['operating_source']
        with OperatingDriverReadContext(store=owner.store,state_dir=Path(core).parent) as reader:
            context=reader.projection(wire['company_ref'],evidence_bindings=source['context_evidence_refs'])
        selected=selected_context(context,source['context_evidence_refs'],source['context_hash'])
        series=metric_history(selected,company_ref=wire['company_ref'],metric_ref=source['metric_ref'],expected_hash=source['context_hash'])
        if (canonical_json(series['history']) != canonical_json(item['history'])
                or series['unit'] != item['unit'] or series['evidence_refs'] != source['evidence_refs']):
            raise ValueError('operating forecast history differs from its complete canonical/source/fee authority')
