"""Source-authority capabilities; management statements are never filed facts."""
from __future__ import annotations
from typing import Any, Mapping
from .document_figure_grade import FILED, SPOKEN, grade_for
from datetime import date, datetime
import sqlite3

SPOKEN_CONTRACT_REF = 'directed-document-spoken-numeric:0.1'
FILED_CONTRACT_REF = 'directed-document-numeric:0.1'
NUMERIC_STRATEGIES = frozenset({FILED_CONTRACT_REF, SPOKEN_CONTRACT_REF})


def source_capability(connection: Any, registration: Mapping[str, Any], company_ref: str) -> dict[str, Any]:
    """Replay the exact source/review chain of an already validated registration."""
    from .coverage_mission import validate_mission_source_discovery
    import json
    authority = registration['source_authority']
    rows = connection.execute(
        'SELECT r.review_id,r.company_ref,r.document_ref,r.discovered_document_ref,d.source_ref,'
        's.spec_ref,s.record_id AS discovery_ref,s.record_json AS discovery_json,s.content_hash AS discovery_hash '
        'FROM coverage_mission_document_reviews r JOIN coverage_mission_discovered_documents d '
        'ON d.record_id=r.discovered_document_ref JOIN coverage_mission_source_discoveries s '
        'ON s.record_id=d.discovery_ref WHERE d.record_id=? AND r.document_ref=? AND r.company_ref=?',
        (authority['ref'], registration['document_ref'], company_ref)).fetchall()
    if len(rows) != 1 or rows[0]['source_ref'] != registration['source_ref']:
        raise ValueError('numeric source has no exact mission review chain')
    row = dict(rows[0])
    from .store import canonical_json
    raw = row.pop('discovery_json')
    discovery = validate_mission_source_discovery(json.loads(raw))
    if canonical_json(discovery)!=raw:raise ValueError('numeric source discovery bytes differ')
    if (discovery['content_hash'] != row.pop('discovery_hash') or discovery['id'] != row.pop('discovery_ref')
            or discovery['spec_ref'] != row['spec_ref'] or discovery['company_ref'] != company_ref
            or discovery['source_ref'] != row['source_ref']):
        raise ValueError('numeric source discovery identity differs')
    row['source_grade'] = grade_for(row['spec_ref'])
    row['source_manifest_hash'] = registration['manifest_hash']
    return row


def planning_capability(connection, registration, company_ref):
    """One authority-derived grade; the published contract maps it to strategy.

    Repeating the long strategy name on every original wastes bounded planner
    input. Unknown grade preserves ordinary reading, with no numeric capability.
    """
    try:
        binding = source_capability(connection, registration, company_ref)
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc): raise
        return {'source_grade': None}
    except (ValueError, KeyError, TypeError):
        return {'source_grade': None}
    return {'source_grade': binding['source_grade']}


def spoken_answer(admission):
    strategy = admission['planner_inquiry']['directed_document']['strategy_version']
    marker = admission.get('numeric_authority', {}).get('answer_contract_ref')
    if strategy == SPOKEN_CONTRACT_REF:
        if marker != SPOKEN_CONTRACT_REF:
            raise ValueError('spoken numeric answer lacks its versioned authority')
        return True
    if marker is not None:
        raise ValueError('filed numeric authority carries an unsupported answer contract')
    return False


def document_date(registration):
    """Only a source-provided ISO date; an absent date remains explicitly unknown."""
    value = registration.get('doc_date')
    if not isinstance(value, str):return None
    try:
        if len(value)==10:date.fromisoformat(value)
        elif 'T' in value:datetime.fromisoformat(value)
        else:return None
    except ValueError:return None
    return value
