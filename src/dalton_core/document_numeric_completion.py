"""Read a completed numeric settlement solely to retire a stale lane hold.

This is an operations projection, not financial admission or a new execution
permission. Incomplete targets and inconsistent immutable records stay held.
"""
import json
import sqlite3
from .store import canonical_json, content_hash
from .directed_numeric_research import DirectedNumericError


def completed_numeric_admission(connection, admission_ref):
    from .scheduler import Scheduler
    from .mission_document_research_executor import _formal_hash
    try:
        row=connection.execute('SELECT n.record_json,n.content_hash,a.record_json AS admission_json,'
            'a.content_hash AS admission_hash FROM mission_document_numeric_results n '
            'JOIN mission_document_research_admissions a ON a.admission_id=n.admission_ref '
            'WHERE n.admission_ref=?',(admission_ref,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return False
        raise
    if row is None:return False
    result=json.loads(row['record_json']);admission=json.loads(row['admission_json'])
    for wire,raw,digest in ((result,row['record_json'],row['content_hash']),
                            (admission,row['admission_json'],row['admission_hash'])):
        if canonical_json(wire)!=raw or wire['content_hash']!=digest or content_hash(
                {k:v for k,v in wire.items() if k!='content_hash'})!=digest:
            raise DirectedNumericError('numeric lane completion immutable hash differs')
    if (admission['id']!=admission_ref or result['admission_ref']!=admission_ref
            or result['admission_hash']!=admission['content_hash']
            or result['numeric_authority']!=admission['numeric_authority']):
        raise DirectedNumericError('numeric lane completion parent binding differs')
    scheduler=object.__new__(Scheduler);scheduler.connection=connection
    authority=scheduler.work_order_authority(result['work_order_ref'])
    formal=scheduler.formal_result(result['work_order_ref'])
    if formal is None:return False
    if (authority is None or authority['work_order_hash']!=result['work_order_hash']
            or authority['work_order']['metadata'].get('mission_document_research_admission_ref')!=admission_ref
            or authority['work_order']['metadata'].get('mission_document_research_admission_hash')!=admission['content_hash']
            or formal['terminal_state']!='succeeded' or formal['content_hash']!=_formal_hash(formal)
            or content_hash(formal['result_envelope'])!=formal['result_envelope_hash']
            or formal['result_envelope']['work_order_ref']!=result['work_order_ref']
            or formal['result_envelope']['outputs']!=result):
        raise DirectedNumericError('numeric lane completion exact formal differs')
    from .directed_numeric_measure_reconciliation import feedback_supplement
    supplement=feedback_supplement(connection,admission,result)
    if supplement is not None:result=supplement
    if result['status']!='completed' or result['pending_targets']:return False
    expected=[t['target_ref'] for t in admission['numeric_authority']['targets']]
    completed=[t['target_ref'] for t in result['completed_targets']]
    if (not expected or len(set(expected))!=len(expected)
            or len(set(completed))!=len(completed) or set(completed)!=set(expected)):
        raise DirectedNumericError('numeric lane completion does not cover every admitted target')
    return True
