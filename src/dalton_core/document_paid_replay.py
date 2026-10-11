"""A paid route may be replayed under a later Scheduler lease, never resent."""
from .store import content_hash


def paid_replay_attempt(connection, router, work, formal, route, worker_ref):
    """Require the exact expired original lease and current terminal completion.

    This proves only which attempt served the saved reply. Invocation, route,
    Usage, actual Cost and settlement must additionally be verified by caller.
    It is not a no-send proof and grants no new retry capacity.
    """
    from .mission_document_research_executor import _formal_hash
    if route['attempt_number'] == formal['attempt_number']:
        return route['attempt_number']
    if (not isinstance(route['attempt_number'], int)
            or not 0 < route['attempt_number'] < formal['attempt_number']
            or formal.get('content_hash', _formal_hash(formal)) != _formal_hash(formal)
            or formal['terminal_state'] != 'failed'
            or (formal['result_envelope'].get('error') or {}).get('code') != 'MODEL_OUTPUT_CONTRACT_REJECTED'):
        return None
    events = connection.execute(
        'SELECT e.*,l.owner_ref FROM scheduler_attempt_events e JOIN scheduler_leases l '
        'ON l.lease_revision_id=e.lease_revision_id WHERE e.work_order_id=? '
        'AND ((e.attempt_number=? AND e.state=?) OR (e.attempt_number=? AND e.result_envelope_id=?)) '
        'ORDER BY e.event_seq', (work['id'], route['attempt_number'], 'expired',
        formal['attempt_number'], formal['result_envelope_id'])).fetchall()
    if len(events) != 2:
        return None
    expired, completed = events
    for row in events:
        body = {'schema_version': '0.1', 'wire_version': row['wire_version'],
                'id': row['event_id'], 'created_at': row['created_at'],
                'work_order_ref': row['work_order_id'], 'attempt_number': row['attempt_number'],
                'state': row['state'], 'lease_ref': row['lease_revision_id'],
                'result_envelope_ref': row['result_envelope_id'],
                'result_envelope_hash': row['result_envelope_hash'], 'reason': row['reason'],
                'not_before': row['not_before'], 'prior_event_ref': row['prior_event_id']}
        if row['owner_ref'] != worker_ref or content_hash(body) != row['content_hash']:
            return None
    if (completed['state'] != 'failed' or completed['reason'] != 'worker_completion'
            or completed['result_envelope_hash'] != formal['result_envelope_hash']
            or expired['reason'] != 'lease_expired'):
        return None
    # The worker replays its last unused route; a newer route is a different
    # execution, so an older reply cannot be attributed to its completion.
    decisions = router.list_decisions(work_order_id=work['id'])
    if not decisions or decisions[-1]['id'] != route['id']:
        return None
    return route['attempt_number']
