"""Read-only AT census of persisted question/staging authority."""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from pathlib import Path
from .readonly_sqlite import connect_read_only
from .research_question_identity import canonical_question_key
from .research_question_maintenance import OPERATIONS_PATTERN
from .store import content_hash

def replay_questions(core_path, *, staging_path=None):
    db=connect_read_only(core_path);db.row_factory=__import__('sqlite3').Row
    try:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result={'core':str(core_path),'read_only':True,'question_authority':'research_question_backlog','model_calls':0}
        shadows=db.execute('SELECT count(*) FROM research_question_versions').fetchone()[0] if 'research_question_versions' in names else 0
        rows=db.execute("SELECT v.*, (SELECT state FROM backlog_question_events e WHERE e.question_ref=v.question_ref ORDER BY event_seq DESC LIMIT 1) AS state FROM backlog_question_pointer p JOIN backlog_question_versions v ON v.version_id=p.version_id ORDER BY v.question_ref").fetchall() if 'backlog_question_pointer' in names else []
        active=[]
        if 'coverage_mission_pointer' in names:
            for row in db.execute('SELECT v.record_json FROM coverage_mission_pointer p JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id'):
                active.append(json.loads(row[0]))
        permitted={item['company_ref'] for m in active for item in m['universe']} | {m['industry_ref'] for m in active}
        groups=defaultdict(list);states=Counter();operations=[];out_of_scope=[]
        for row in rows:
            wire=json.loads(row['record_json'])
            if (wire.get('content_hash')!=row['content_hash'] or content_hash({k:v for k,v in wire.items() if k!='content_hash'})!=row['content_hash']):
                raise ValueError('persisted question hash drifted')
            key=canonical_question_key(row['company_ref'],row['question'],row['answer_criteria'],json.loads(row['source_refs_json']))
            groups[content_hash({'mandate_ref':row['mandate_ref'],**key})].append(row['question_ref'])
            states[row['state']]+=1
            if active and row['state'] not in {'answered','blocked','retired'} and row['company_ref'] not in permitted: out_of_scope.append(row['question_ref'])
            if row['state'] not in {'answered','blocked','retired'} and OPERATIONS_PATTERN.search(row['question']): operations.append(row['question_ref'])
        shadow_outside=0
        if 'research_question_versions' in names:
            shadow_outside=sum(r[0] not in permitted for r in db.execute('SELECT company_ref FROM research_question_versions')) if active else 0
        result['legacy_shadow_outside_scope_terminal_candidates']=shadow_outside
        result.update({'head_count':len(rows),'states':dict(states),'legacy_shadow_versions_retained':shadows,
            'canonical_key_count':len(groups),'duplicate_canonical_groups':[refs for refs in groups.values() if len(refs)>1],
            'operations_terminal_candidates':operations,'outside_scope_terminal_candidates':out_of_scope,'new_shadow_writes_expected':0})
        if 'backlog_answer_bindings' in names:
            result['formal_answer_bindings']=db.execute('SELECT count(*) FROM backlog_answer_bindings').fetchone()[0]
        return result
    finally: db.close()
