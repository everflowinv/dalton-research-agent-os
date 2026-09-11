from __future__ import annotations
import argparse,json,os
from pathlib import Path
from .cockpit_model import CockpitModel
from .coverage_mission import CoverageMissionAuthority
from .discovery_candidate_selection import CockpitDiscoveryCandidateSelector
from .store import DaltonStore,canonical_json
def run(args):
    data=json.loads(args.input.read_text()); store=DaltonStore(str(args.state_dir/'core.sqlite'))
    summary={"schema_version":"0.1","status":"failed","identity_hash":data['identity_hash']}
    try:
        mission=CoverageMissionAuthority(store).mission(data['mission_ref'])
        model=CockpitModel(json.loads(args.model_config.read_text()),scheduler_db=str(args.scheduler_db))
        result=CockpitDiscoveryCandidateSelector(model).select(data['view'],mission=mission,
            company=data['company'],missing_periods=data['missing_periods'])
        summary.update(status='succeeded',selection=result)
    except Exception as exc: summary['failure_reason']=f'{type(exc).__name__}: {exc}'[:500]
    finally: store.close()
    out=args.summary_dir/'summary.json'; tmp=out.with_name('.summary.tmp'); tmp.write_text(canonical_json(summary)+'\n'); os.chmod(tmp,0o600); os.replace(tmp,out)
    return 0 if summary['status']=='succeeded' else 1
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--state-dir',type=Path,required=True); p.add_argument('--model-config',type=Path,required=True); p.add_argument('--scheduler-db',type=Path,required=True); p.add_argument('--input',type=Path,required=True); p.add_argument('--summary-dir',type=Path,required=True); return run(p.parse_args(argv))
if __name__=='__main__': raise SystemExit(main())
