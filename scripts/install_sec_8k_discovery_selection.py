#!/usr/bin/env python3
"""Prepare or explicitly install the reviewed SEC 8-K discovery selection."""
from __future__ import annotations
import argparse, hashlib, json, os, sys, tempfile
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))
from build_sec_8k_discovery_proposal import read_active_mission
from dalton_core.macos_launchagent import SEC_PLAN_SELECTOR
from dalton_core.mission_source_discovery import load_discovery_plan, validate_discovery_plan
from dalton_core.store import content_hash


def sha(path: Path)->str: return hashlib.sha256(path.read_bytes()).hexdigest()
def checked(path:Path, expected:str)->dict[str,Any]:
    if sha(path)!=expected: raise ValueError(f"sha256 mismatch: {path}")
    value=json.loads(path.read_text());
    if not isinstance(value,dict): raise ValueError(f"{path} must contain an object")
    return value

def validate_packet(*,active_path:Path,active_hash:str,candidate_path:Path,candidate_sha:str,
                    selector_path:Path,selector_sha:str,source_core:Path)->dict[str,Any]:
    active=load_discovery_plan(active_path)
    if active['content_hash']!=active_hash: raise ValueError('active plan original hash changed')
    candidate=validate_discovery_plan(checked(candidate_path,candidate_sha))
    selector=checked(selector_path,selector_sha)
    expected={'schema_version','id','status','source_ref','plan_ref','plan_hash','plan_path','content_hash'}
    if set(selector)!=expected or selector['status']!='proposed': raise ValueError('selector must be the closed proposed record')
    if selector['content_hash']!=content_hash({k:v for k,v in selector.items() if k!='content_hash'}): raise ValueError('selector content hash drifted')
    if selector['plan_ref']!=candidate['id'] or selector['plan_hash']!=candidate['content_hash']: raise ValueError('selector does not bind candidate ref/hash')
    preserved=set(active)-{'id','created_at','content_hash','specs'}
    if any(candidate.get(k)!=active.get(k) for k in preserved): raise ValueError('candidate changes preserved plan scope')
    if candidate['specs'][:-1]!=active['specs'] or len(candidate['specs'])!=len(active['specs'])+1 or candidate['specs'][-1].get('form')!='8-K': raise ValueError('candidate is not exactly one appended 8-K spec')
    mission=read_active_mission(source_core,active['mission_ref'])
    if candidate['mission_ref']!=mission['mission_ref'] or not set(candidate['companies']).issubset({x['company_ref'] for x in mission['universe']}): raise ValueError('candidate is outside current live mission scope')
    approved={**selector,'status':'approved'}; approved['content_hash']=content_hash({k:v for k,v in approved.items() if k!='content_hash'})
    return {'active':active,'candidate':candidate,'approved_selector':approved,'mission':mission}

def state(path:Path,data:bytes)->str:
    if not path.exists() and not path.is_symlink(): return 'absent'
    if path.is_file() and path.read_bytes()==data: return 'identical'
    return 'conflict'
def atomic_create(path:Path,data:bytes)->str:
    current=state(path,data)
    if current=='identical': return 'identical'
    if current=='conflict': raise FileExistsError(f'refusing to overwrite different target: {path}')
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(name,0o600)
        os.link(name,path)  # atomic publication, and never overwrites a racer
    finally:
        Path(name).unlink(missing_ok=True)
    return 'created'
def run(args)->dict[str,Any]:
    packet=validate_packet(active_path=args.active_plan,active_hash=args.active_plan_hash,
      candidate_path=args.candidate,candidate_sha=args.candidate_sha256,selector_path=args.selector,
      selector_sha=args.selector_sha256,source_core=args.source_core)
    plan_name=packet['approved_selector']['plan_path']; target_plan=args.target_dir/plan_name
    target_selector=args.target_dir/SEC_PLAN_SELECTOR
    plan_bytes=(json.dumps(packet['candidate'],sort_keys=True,separators=(',',':'))+'\n').encode()
    selector_bytes=(json.dumps(packet['approved_selector'],sort_keys=True,separators=(',',':'))+'\n').encode()
    report={'mode':args.command,'actor_ref':args.actor,'mission_binding':{'ref':packet['mission']['id'],'hash':packet['mission']['content_hash']},
      'prior_plan':{'ref':packet['active']['id'],'hash':packet['active']['content_hash']},
      'candidate':{'ref':packet['candidate']['id'],'hash':packet['candidate']['content_hash'],'target':str(target_plan),'target_state':state(target_plan,plan_bytes)},
      'selector':{'ref':packet['approved_selector']['id'],'hash':packet['approved_selector']['content_hash'],'target':str(target_selector),'target_state':state(target_selector,selector_bytes)},
      'delta':{'added_specs':[packet['candidate']['specs'][-1]],'preserved_companies':sorted(packet['candidate']['companies']),'budget':packet['candidate']['budget']},
      'requires_reinstall':True,'artifact_acceptance':False}
    conflicts=[x for x in ('candidate','selector') if report[x]['target_state']=='conflict']
    if conflicts: raise FileExistsError('different target already exists: '+','.join(conflicts))
    if args.command=='apply':
      if not args.execute or not args.service_stopped_ack: raise ValueError('apply requires --execute and --service-stopped-ack')
      if not isinstance(args.actor,str) or not args.actor.startswith('human:'): raise ValueError('apply requires a human: approval actor')
      report['candidate']['write']=atomic_create(target_plan,plan_bytes)
      report['selector']['write']=atomic_create(target_selector,selector_bytes)
      report['result']='installed_for_next_render; run install/re-render before activation'
    else: report['result']='prepared_only; no files written'
    return report

def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument('command',choices=('prepare','apply')); p.add_argument('--active-plan',type=Path,required=True); p.add_argument('--active-plan-hash',required=True); p.add_argument('--candidate',type=Path,required=True); p.add_argument('--candidate-sha256',required=True); p.add_argument('--selector',type=Path,required=True); p.add_argument('--selector-sha256',required=True); p.add_argument('--source-core',type=Path,required=True); p.add_argument('--target-dir',type=Path,required=True); p.add_argument('--actor'); p.add_argument('--execute',action='store_true'); p.add_argument('--service-stopped-ack',action='store_true')
 args=p.parse_args(argv); print(json.dumps(run(args),ensure_ascii=False,indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
