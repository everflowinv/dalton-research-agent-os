"""Closed propose -> owner decision -> N+1 -> fresh exit-gate flow."""
from __future__ import annotations
import argparse, hashlib, json, os, sqlite3
from pathlib import Path
from .coverage_mission import CoverageMissionAuthority
from .deliverable_reopen import GateReopenAuthority, approved_reopen
from .governance_cli import ephemeral_call
from .initial_screen import assess_exit_gate
from .initial_screen_cli import _playbook
from .mission_deliverable import MissionDeliverableAuthority
from .mission_stage import evaluate_mission, planned_spec_refs_from_directory
from .store import DaltonStore, content_hash

def run(args):
    wire=args.candidate.read_bytes(); file_hash=hashlib.sha256(wire).hexdigest()
    if file_hash!=args.expected_candidate_sha256: raise ValueError('candidate file sha256 binding failed')
    candidate=json.loads(wire); state=args.state_dir.resolve(); source=candidate['source_version']; subject=candidate['subject_ref']
    if not args.execute:
        connection=sqlite3.connect((state/'core.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        connection.row_factory=sqlite3.Row
        try:
            connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
            row=connection.execute('SELECT mission_version_ref,content_hash,version_number FROM mission_deliverable_versions WHERE version_id=?',(source['version_ref'],)).fetchone()
            if row is None or row['content_hash']!=source['content_hash'] or row['version_number']!=source['version_number']:
                raise ValueError('source version binding failed')
            source_mission=connection.execute('SELECT mission_ref FROM coverage_mission_versions WHERE mission_version_id=?',(row['mission_version_ref'],)).fetchone()
            if source_mission is None:raise ValueError('source mission is absent')
            mission_row=connection.execute('SELECT v.record_json,v.content_hash,v.actor_ref FROM coverage_mission_pointer p JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id WHERE p.mission_ref=?',(source_mission['mission_ref'],)).fetchone()
            if mission_row is None:raise ValueError('source mission has no active version')
            mission=json.loads(mission_row['record_json']);automation=mission['autonomy']['automation_principal']
            if mission_row['content_hash']!=mission['content_hash']:raise ValueError('mission authority hash changed')
            if args.expected_automation_actor!=automation:raise ValueError('mission automation actor binding failed')
            if args.owner_actor!=mission_row['actor_ref'] or not args.owner_actor.startswith('human:'):
                raise ValueError('owner actor is not the existing mission author')
        finally:connection.close()
        return {'schema_version':'deliverable-human-erratum-execution:0.1','status':'planned_read_only',
          'candidate_file_sha256':file_hash,'candidate_hash':content_hash(candidate),
          'source_version_ref':source['version_ref'],'source_version_hash':source['content_hash'],
          'source_mission_version_ref':row['mission_version_ref'],'mission_version_ref':mission['id'],'mission_version_hash':mission['content_hash'],
          'mission_author_actor_ref':mission_row['actor_ref'],'automation_actor_ref':automation,
          'owner_actor_ref':args.owner_actor,'steps':['propose_human_revision','decide_gate_reopen','append_exact_n_plus_1','assess_and_record_exit_gate']}
    store=DaltonStore(str(state/'core.sqlite'))
    try:
        row=store.connection.execute('SELECT mission_version_ref FROM mission_deliverable_versions WHERE version_id=?',(source['version_ref'],)).fetchone()
        if row is None: raise ValueError('source version is absent')
        missions=CoverageMissionAuthority(store);source_mission=missions.mission(row['mission_version_ref'])
        pointer=store.connection.execute('SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?',(source_mission['mission_ref'],)).fetchone()
        if pointer is None:raise ValueError('source mission has no active version')
        mission=missions.mission(pointer['mission_version_id'])
        automation=mission['autonomy']['automation_principal']
        if args.expected_automation_actor!=automation: raise ValueError('mission automation actor binding failed')
        mission_author=store.connection.execute('SELECT actor_ref FROM coverage_mission_versions WHERE mission_version_id=?',(mission['id'],)).fetchone()
        if mission_author is None or args.owner_actor!=mission_author['actor_ref'] or not args.owner_actor.startswith('human:'):
            raise ValueError('owner actor is not the existing mission author')
        plan={'schema_version':'deliverable-human-erratum-execution:0.1','status':'planned_read_only',
          'candidate_file_sha256':file_hash,'candidate_hash':content_hash(candidate),
          'source_version_ref':source['version_ref'],'source_version_hash':source['content_hash'],
          'automation_actor_ref':automation,'owner_actor_ref':args.owner_actor,
          'steps':['propose_human_revision','decide_gate_reopen','append_exact_n_plus_1','assess_and_record_exit_gate']}
        if not args.execute:return plan
        reopens=GateReopenAuthority(store)
        existing=store.connection.execute(
          "SELECT record_json FROM gate_reopen_proposals WHERE change_reason='human_revision' "
          "AND json_extract(record_json,'$.erratum.candidate_hash')=? "
          "AND json_extract(record_json,'$.erratum.candidate_file_sha256')=?",
          (plan['candidate_hash'],file_hash)).fetchall()
        if len(existing)>1:raise ValueError('candidate has multiple erratum proposals')
        proposal=(json.loads(existing[0]['record_json']) if existing else
          reopens.propose_human_revision(candidate=candidate,candidate_hash=plan['candidate_hash'],
            candidate_file_sha256=file_hash,mission=mission,actor_ref=automation))
        persisted_decision=reopens.decision_for(proposal['id'])
        if persisted_decision is None:
            decision=ephemeral_call(args.token_config,args.writer_socket,actor_ref=args.owner_actor,
              operation='decide_gate_reopen',params={'proposal_ref':proposal['id'],
                'proposal_hash':proposal['content_hash'],'verdict':'approve','reason':args.reason})
        else:
            if (persisted_decision.get('verdict')!='approve'
                    or persisted_decision.get('proposal_hash')!=proposal['content_hash']
                    or persisted_decision.get('actor_ref')!=args.owner_actor):
                raise ValueError('existing erratum decision is not the exact owner approval')
            decision=persisted_decision
        persisted_proposal=reopens.proposal(proposal['id']);persisted_decision=reopens.decision_for(proposal['id'])
        permission=approved_reopen(store.connection,subject)
        if permission is None and persisted_decision is not None and persisted_decision.get('verdict')=='approve':
            permission={**persisted_decision,'proposal':persisted_proposal}
        if permission is None or permission['proposal_ref']!=proposal['id'] or permission['id']!=decision['id']:
            raise ValueError('approved reopen did not read back exactly')
        authority=MissionDeliverableAuthority(store)
        created=authority.publish_human_erratum(permission=permission,mission=mission,
          playbook=_playbook(store,mission),actor_ref=automation)
        if created['version']!=source['version_number']+1:raise ValueError('erratum did not append exactly N+1')
        stage_rows=evaluate_mission(store.connection,mission,
          planned_specs=planned_spec_refs_from_directory(state/'discovery-plans'),
          stage_state=missions.stage_state_by_company(mission['mission_ref']))
        checklist=next(x for x in stage_rows if x['company_ref']==subject)
        gate=assess_exit_gate(playbook=_playbook(store,mission),checklist_entry=checklist,
                              sections=created['sections'])
        stage=missions.record_stage(mission_version_ref=mission['id'],mission_version_hash=mission['content_hash'],
          company_ref=subject,stage_ref='initial_screen',status='gate_passed' if gate['passed'] else 'gate_failed',
          evidence_refs=[created['id'],mission['id']],rationale=('事实纠错后重新出口门自评：'+gate['rationale'])[:2000],
          actor_ref=automation,idempotency_key=f"{mission['id']}:{subject}:initial_screen:{created['id']}")
        return {**plan,'status':'executed','proposal_ref':proposal['id'],'decision_ref':decision['id'],
          'new_version_ref':created['id'],'new_version_hash':created['content_hash'],
          'new_version_number':created['version'],'gate_passed':gate['passed'],'stage_record':stage.get('id')}
    finally:store.close()

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--state-dir',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--expected-candidate-sha256',required=True);p.add_argument('--expected-automation-actor',required=True)
    p.add_argument('--owner-actor',required=True);p.add_argument('--token-config',type=Path);p.add_argument('--writer-socket',type=Path)
    p.add_argument('--reason',default='纠正四条 Claim 已证明的报告期间标签，不改变投资判断。');p.add_argument('--execute',action='store_true');p.add_argument('--receipt',type=Path,required=True)
    a=p.parse_args(argv)
    if a.execute and (a.token_config is None or a.writer_socket is None):p.error('--execute requires writer paths')
    fd=os.open(a.receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        try:result=run(a)
        except Exception as exc:
            result={'schema_version':'deliverable-human-erratum-execution:0.1','status':'failed','mode':'execute' if a.execute else 'plan','error_type':type(exc).__name__,'error':str(exc)}
            os.write(fd,(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode());os.fsync(fd);raise
        os.write(fd,(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode());os.fsync(fd)
    finally:os.close(fd)
    parent_fd=os.open(a.receipt.parent,os.O_RDONLY)
    try:os.fsync(parent_fd)
    finally:os.close(parent_fd)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')));return 0
if __name__=='__main__':raise SystemExit(main())
