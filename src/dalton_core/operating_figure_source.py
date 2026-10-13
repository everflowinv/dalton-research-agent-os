"""Append-only raw-column evidence for new operating Figures.

The ordinary Figure bytes and citation remain unchanged. This adjunct exists
only when that Figure needs a native column/clause proof beyond the displayed
quote. It cannot authorize a model call. The canonical numeric receipt reader
still replays all original model costs, source permissions and full stages.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from .store import canonical_json,content_hash
from .operating_driver_units import CONTRACT_REF,FIGURE_VERIFIER_REF
from .operating_source_measurements import registered_measurements,verify_native_measurement

TABLE='mission_document_operating_figure_sources'
RULE_REF='operating-figure-original-source:0.1'


def _registry(connection):
    from .financial_note_context import _read_config
    from .document_research import build_document_research_registry
    from .connector_authority_port import ReadOnlyConnectorReceiptReader
    from .public_web_fetch_launcher import ReadOnlyPublicWebFetchManifestReader
    from .raw_spool import RawSpoolReader
    root=next(row[2] for row in connection.execute('PRAGMA database_list') if row[1]=='main')
    if not root:
        raise ValueError('operating source replay needs its exact persistent Core')
    state=Path(root).parent
    config,_=_read_config(state/'document-research-config.json')
    refs=[r for r in config['enabled_sources'] if r in {'source:sec-edgar','source:public-web'}]
    if 'source:web-search' in config['enabled_sources'] and 'source:public-web' not in refs:
        refs.append('source:public-web')
    if not refs:
        raise ValueError('operating original source is no longer enabled')
    return build_document_research_registry(core=SimpleNamespace(connection=connection),state_dir=state,
        spool=RawSpoolReader(config['spool_dir']),receipt_reader=ReadOnlyConnectorReceiptReader(connection),
        policy=config['policy'],feed_launchers={},alphaengine_launcher=None,
        public_web_launcher=ReadOnlyPublicWebFetchManifestReader(state_dir=state),
        public_web_source_refs=refs,source_reading_limits=config['source_reading_limits'])


def _stages(scheduler,works):
    result=[]
    for index,work in enumerate(works[:3]):
        held=scheduler.work_order_authority(work['id'])
        formal=scheduler.formal_result(work['id'])
        if (held is None or formal is None or held['work_order']!=work
                or held['work_order_hash']!=content_hash(work) or formal['terminal_state']!='succeeded'):
            raise ValueError('operating Figure lacks all three original succeeded stages')
        result.append({'work_order_ref':work['id'],'work_order_hash':content_hash(work),
            'formal_hash':content_hash(formal),'stage_index':index})
    return result


def _admission(connection,ref,expected_hash):
    row=connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(ref,)).fetchone()
    if row is None:
        raise ValueError('operating Figure original admission is absent')
    admission=json.loads(row['record_json'])
    if (canonical_json(admission)!=row['record_json'] or admission['content_hash']!=row['content_hash']
            or content_hash({k:v for k,v in admission.items() if k!='content_hash'})!=row['content_hash']
            or row['content_hash']!=expected_hash or admission['id']!=ref
            or admission['numeric_authority']['contract_ref']!=CONTRACT_REF):
        raise ValueError('operating Figure original admission identity/hash differs')
    return admission


def _metadata(connection,admission,figure):
    from .directed_numeric_executor import _admitted_source_binding
    source=_admitted_source_binding(connection,admission)
    expected={'company_ref':admission['company_ref'],'review_ref':source['review_id'],
        'document_ref':source['document_ref'],'source_manifest_hash':source['source_manifest_hash'],
        'source_grade':source['source_grade'],'observed_by':admission['actor_ref'],'verified_by':FIGURE_VERIFIER_REF}
    if any(figure.get(k)!=v for k,v in expected.items()):
        raise ValueError('operating Figure source/review/actor metadata differs from its actual admission')
    return source


def record(executor,admission,works,figure,search_proof):
    prior=executor.connection.execute('SELECT figure_ref FROM '+TABLE+' WHERE figure_ref=?',(figure['figure_id'],)).fetchone()
    if prior is not None:
        # An independently admitted read cannot rewrite an earlier Figure's
        # source proof. The ordinary figure-support comparison still checks
        # the current target against that exact existing measurement.
        read(executor.connection,figure,registry=executor.registry)
        return
    from .directed_numeric_output import quotes
    if quotes(search_proof).get(figure['quote_id']) != figure['citation_text']:
        return
    native=works[1]['metadata'].get('operating_source_proof')
    from .claim_index_figures import _numeric_candidate
    candidate=_numeric_candidate(figure)
    if native is None or not verify_native_measurement(candidate,figure['citation_text'],native):
        return
    # The actual stage owner has already completed the full paid/formal/fee
    # proof; repeating it at this append boundary protects any future caller.
    from .directed_numeric_executor import _execution_proof
    proof,_,_,accounting=_execution_proof(executor,admission,works)
    if proof!=search_proof:
        raise ValueError('operating Figure does not bind its exact source stage')
    _metadata(executor.connection,admission,figure)
    body={'rule_ref':RULE_REF,'figure_ref':figure['figure_id'],'figure_hash':figure['content_hash'],
        'admission_ref':admission['id'],'admission_hash':admission['content_hash'],
        'registration':admission['request']['registration'],'search_proof':proof,
        'native_proof':native,'stages':_stages(executor.scheduler,works),
        'accounting_proofs':accounting,'created_at':figure['created_at']}
    value={**body,'content_hash':content_hash(body)}
    with executor.authority._transaction() as cur:
        row=cur.execute('SELECT record_json,content_hash FROM '+TABLE+' WHERE figure_ref=?',(figure['figure_id'],)).fetchone()
        if row is None:
            cur.execute('INSERT INTO '+TABLE+'(figure_ref,admission_ref,record_json,content_hash) VALUES(?,?,?,?)',
                (figure['figure_id'],admission['id'],canonical_json(value),value['content_hash']))
        elif row['record_json']!=canonical_json(value) or row['content_hash']!=value['content_hash']:
            raise ValueError('operating Figure source evidence is immutable and differs')
        read(executor.connection,figure,registry=executor.registry)


def read(connection,figure,*,registry=None):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() is None:
        return None
    row=connection.execute('SELECT * FROM '+TABLE+' WHERE figure_ref=?',(figure['figure_id'],)).fetchone()
    if row is None:
        return None
    value=json.loads(row['record_json'])
    if (canonical_json(value)!=row['record_json'] or value['content_hash']!=row['content_hash']
            or content_hash({k:v for k,v in value.items() if k!='content_hash'})!=row['content_hash']
            or value['rule_ref']!=RULE_REF or value['figure_ref']!=figure['figure_id']
            or value['figure_hash']!=figure['content_hash'] or value['created_at']!=figure['created_at']
            or value['admission_ref']!=row['admission_ref']):
        raise ValueError('operating Figure source adjunct hash/identity differs')
    admission=_admission(connection,value['admission_ref'],value['admission_hash'])
    _metadata(connection,admission,figure)
    from .scheduler import Scheduler
    scheduler=object.__new__(Scheduler);scheduler.connection=connection
    works=[]
    for stage in value['stages']:
        held=scheduler.work_order_authority(stage['work_order_ref'])
        if held is None:
            raise ValueError('operating Figure original stage is missing')
        works.append(held['work_order'])
    if len(works)!=3 or _stages(scheduler,works)!=value['stages']:
        raise ValueError('operating Figure original stages no longer match')
    if (any(w['metadata'].get('mission_document_research_admission_ref')!=admission['id'] for w in works)
            or value['registration']!=admission['request']['registration']):
        raise ValueError('operating Figure source stages belong to another admission')
    # Bind this Figure to the actual accepted reader/verifier answers, not
    # merely three succeeded Works bearing this admission's name.
    from .annual_report_qualitative import validate_model_proof
    from .directed_numeric_output import parse_draft,parse_verifier,candidate_ref
    from .model_router import independent_families
    draft=validate_model_proof(scheduler.formal_result(works[1]['id'])['result_envelope']['outputs'],
        stage='qualitative_model_draft',work=works[1])
    verifier=validate_model_proof(scheduler.formal_result(works[2]['id'])['result_envelope']['outputs'],
        stage='independent_qualitative_verifier',work=works[2])
    parsed=parse_draft(canonical_json(draft['output']),metadata=works[1]['metadata'])
    checked=parse_verifier(canonical_json(verifier['output']),metadata=works[2]['metadata'])
    from .claim_index_figures import _numeric_candidate
    expected=_numeric_candidate(figure)
    votes={r['target_ref']:r['verdict'] for r in checked['targets']}
    from .document_numeric_claim import validate_numeric_candidate
    compare={k:v for k,v in expected.items() if k!='subject_as_named'}
    # The legacy nineteen-column Figure stores no subject_as_named field;
    # issuer attribution is authenticated by its original admission/source,
    # not fabricated into the Figure during replay.
    accepted=[r for r in parsed['targets'] if r['status']=='supported'
              and {k:v for k,v in validate_numeric_candidate(r['figure'],numeric_contract_ref=CONTRACT_REF).items()
                   if k!='subject_as_named'}==compare and votes.get(r['target_ref'])=='accept']
    if (len(accepted)!=1 or checked['verdict']!='pass'
            or checked['candidate_ref']!=candidate_ref(parsed)
            or not independent_families(verifier['model_family'],draft['model_family'])
            or len(value['accounting_proofs'])!=2
            or any(p['content_hash']!=content_hash({k:v for k,v in p.items() if k!='content_hash'})
                   for p in value['accounting_proofs'])):
        raise ValueError('operating Figure is not the exact independently accepted original answer')
    registry=_registry(connection) if registry is None else registry
    proof=registry.verify_search_proof(value['search_proof'])
    if proof['request']!=admission['request']:
        raise ValueError('operating Figure registered source changed')
    native=registered_measurements(registry,value['registration'],purpose=admission['request']['purpose'],search_proof=proof,
                periods=list(dict.fromkeys(t['period'] for t in admission['numeric_authority']['targets'])))
    if native!=value['native_proof']:
        raise ValueError('operating Figure raw columns/unit/period evidence changed')
    from .claim_index_figures import _numeric_candidate
    if not verify_native_measurement(_numeric_candidate(figure),figure['citation_text'],native):
        raise ValueError('operating Figure lacks its exact raw amount/column identity')
    return native
