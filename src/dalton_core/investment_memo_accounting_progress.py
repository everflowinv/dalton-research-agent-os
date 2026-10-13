"""Free paging successors, bound to immutable Scheduler accounting progress."""
from __future__ import annotations

from pathlib import Path
import hashlib
from .store import content_hash
from .investment_memo_macro_accounting import CONTRACT_REF, SCOPES, PAGES, SCANS, _body


def resume_checkpoint(summary, ticket, scheduler_db):
    """Validate the exact pending run; return current progress, never paid authority."""
    from .planner_paid_output_recovery import _authority_connection
    if (not isinstance(summary, dict) or summary.get('memo_status')!='macro_accounting_pending'
            or ticket.get('status')=='running'
            or summary.get('memo_input_signature')!=ticket.get('signature')):
        raise ValueError('memo pending run signature or terminal ticket differs')
    progress=summary.get('macro_progress')
    if not isinstance(progress,dict):
        raise ValueError('memo pending run has no accounting checkpoint')
    body={k:v for k,v in progress.items() if k!='content_hash'}
    if (content_hash(body)!=progress.get('content_hash') or body.get('contract_ref')!=CONTRACT_REF
            or body.get('company_ref')!=ticket.get('company_ref')
            or body.get('table') not in (PAGES,SCANS)):
        raise ValueError('memo pending accounting binding differs')
    for field in ('model_config','verifier_model_config'):
        if hashlib.sha256(Path(ticket[field+'_path']).read_bytes()).hexdigest()!=ticket[field+'_fingerprint']:
            raise ValueError('memo pending configuration changed')
    with _authority_connection(scheduler_db) as connection:
        connection.execute('BEGIN')
        scope=connection.execute(f'SELECT * FROM {SCOPES} WHERE macro_ref=?',(body['macro_ref'],)).fetchone()
        if scope is None or content_hash(_body(scope))!=body['scope_hash'] or _body(scope)['company_ref']!=body['company_ref']:
            raise ValueError('memo pending frozen scope differs')
        checkpoint=connection.execute(f"SELECT * FROM {body['table']} WHERE checkpoint_id=? AND macro_binding=?",
                                      (body['checkpoint_ref'],body['macro_ref'])).fetchone()
        if checkpoint is None or checkpoint['content_hash']!=body['checkpoint_hash']:
            raise ValueError('memo pending checkpoint is missing or changed')
        record=_body(checkpoint)
        if body['table']==PAGES and record['scope_hash']!=body['scope_hash']:
            raise ValueError('memo pending page belongs to another frozen scope')
        latest=connection.execute(f"SELECT * FROM {body['table']} WHERE macro_binding=? ORDER BY checkpoint_id DESC LIMIT 1",
                                  (body['macro_ref'],)).fetchone()
        _body(latest)
        return latest['content_hash']
