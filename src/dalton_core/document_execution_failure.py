"""Strong read-only execution failures; never new retry or answer authority."""
from collections.abc import Mapping
from .store import content_hash

OUTCOME = 'document_execution_failed'


def failure_from_observations(connection, admission, observations):
    from .directed_numeric_recovery import read_contract_retry_links
    from .mission_document_research_executor import _formal_hash, _terminal_model_failure
    from .scheduler import Scheduler
    from .contracts import ResultEnvelope
    selected = [item for item in observations if item.get('admission_ref') == admission['id']
                and item.get('outcome') == 'recovery_required']
    if not selected:
        return None
    ordinal = {'qualitative_model_draft': 2, 'independent_qualitative_verifier': 3}
    selected = [item for item in selected if item.get('stage') in ordinal]
    if not selected:
        return None
    stage = max(ordinal[item['stage']] for item in selected)
    rows = [item for item in selected if ordinal[item['stage']] == stage]
    links = read_contract_retry_links(connection, admission, stage)
    ref = links[-1]['recovery_work_order_ref'] if links else rows[-1]['work_order_ref']
    scheduler = object.__new__(Scheduler)
    scheduler.connection = connection
    stored = scheduler.work_order_authority(ref)
    matching = [item for item in rows if item['work_order_ref'] == ref]
    observed = max(matching or rows, key=lambda item: (
        str((item.get('recovery') or {}).get('day') or ''), item['created_at'], item['id']))
    state = 'recovery_pending'
    error_code = error_message = None
    formal_ref = formal_hash = result_ref = result_hash = None
    proof_work = stored
    if stored is not None:
        work = stored['work_order']
        if (work['metadata'].get('mission_document_research_admission_ref') != admission['id']
                or work['metadata'].get('mission_document_research_admission_hash') != admission['content_hash']
                or ordinal.get(work['metadata'].get('stage')) != stage):
            raise ValueError('document failure Work ownership differs')
        formal = scheduler.formal_result(ref)
        if formal is not None and formal['terminal_state'] == 'succeeded':
            return None  # This stage recovered; old failed observations are history.
        formal = _terminal_model_failure(scheduler, work)
        if formal is not None:
            state = 'execution_failed'
    else:
        formal = None
    if formal is None:
        # The authorized child is ready/leased or not yet enqueued. Retain
        # its parent's exact failure cause; do not label that child as failed.
        proof_work = scheduler.work_order_authority(observed['work_order_ref'])
        if proof_work is None:
            raise ValueError('document failure parent Work unavailable')
        formal = _terminal_model_failure(scheduler, proof_work['work_order'])
    if formal is None:
        raise ValueError('document failure terminal proof unavailable')
    envelope = ResultEnvelope.from_dict(formal['result_envelope']).to_dict()
    if (formal.get('content_hash', _formal_hash(formal)) != _formal_hash(formal)
            or envelope['id'] != formal['result_envelope_id']
            or envelope['work_order_ref'] != proof_work['work_order']['id']
            or content_hash(envelope) != formal['result_envelope_hash']):
        raise ValueError('document failure formal authority differs')
    error_code = (envelope.get('error') or {}).get('code')
    diagnostic = (envelope.get('metadata') or {}).get('rejected_model_output') or {}
    error_message = (diagnostic.get('error_message') if diagnostic.get('diagnostic_only') is True
                     else (envelope.get('error') or {}).get('message'))
    formal_ref = formal.get('id', formal.get('result_record_id'))
    formal_hash = _formal_hash(formal)
    result_ref, result_hash = envelope['id'], formal['result_envelope_hash']
    recovery = observed['recovery'] if matching else {'status': 'admitted', 'reason': 'existing_recovery_link'}
    identity = {'admission_ref': admission['id'], 'admission_hash': admission['content_hash'],
        'work_order_ref': ref, 'work_order_hash': None if stored is None else stored['work_order_hash'],
        'failed_work_order_ref': proof_work['work_order']['id'],
        'failed_work_order_hash': proof_work['work_order_hash'],
        'formal_result_ref': formal_ref, 'formal_result_hash': formal_hash,
        'result_envelope_ref': result_ref, 'result_envelope_hash': result_hash,
        'observation_ref': observed['id'], 'observation_hash': observed['content_hash'],
        'recovery_link_ref': links[-1]['id'] if links else None,
        'recovery_link_hash': links[-1]['content_hash'] if links else None}
    body = {'id': 'document-execution-failure:' + content_hash(identity)[:32], **identity,
        'mission_version_ref': admission['mission_version_ref'], 'company_ref': admission['company_ref'],
        'document_ref': admission['request']['registration']['document_ref'],
        'document_version_hash': admission['request']['registration']['content_hash'],
        'question': admission['planner_inquiry']['question'], 'outcome': OUTCOME,
        'stage': observed['stage'], 'execution_state': state, 'error_code': error_code,
        'error_message': str(error_message)[:180] if error_message else None,
        'error_message_hash': content_hash(error_message) if error_message else None,
        'recovery': {key: recovery.get(key) for key in ('status', 'reason', 'retry_at', 'deadline')},
        'created_at': formal['created_at'],
        'meaning': 'Execution failed or awaits its existing recovery; no answer or source absence is established. Do not buy a duplicate inquiry.'}
    return {**body, 'content_hash': content_hash(body)}


def read_execution_failure(connection, admission_ref):
    from .mission_document_research import MissionDocumentResearchAuthority
    from .mission_document_research_executor import read_mission_document_research_observations
    row = connection.execute('SELECT * FROM mission_document_research_admissions WHERE admission_id=?',
                             (admission_ref,)).fetchone()
    if row is None:
        raise ValueError('document failure admission unavailable')
    admission = MissionDocumentResearchAuthority._read_row(None, row)
    observations = read_mission_document_research_observations(connection, admission_ref=admission_ref)
    return failure_from_observations(connection, admission, observations)


def compact_failures(feedback):
    rows = sorted((item for item in feedback if item.get('outcome') == OUTCOME),
                  key=lambda item: (item['created_at'], item['id']), reverse=True)
    fields = ('admission_ref', 'admission_hash', 'document_ref', 'document_version_hash',
              'work_order_ref', 'work_order_hash', 'failed_work_order_ref', 'failed_work_order_hash',
              'formal_result_hash', 'result_envelope_hash',
              'question', 'stage', 'execution_state', 'error_code', 'error_message',
              'error_message_hash', 'recovery', 'content_hash')
    return {'failures': [{key: item[key] for key in fields} for item in rows[:3]],
            'omitted_count': len(rows[3:]), 'omitted_hash': content_hash(rows[3:])}
