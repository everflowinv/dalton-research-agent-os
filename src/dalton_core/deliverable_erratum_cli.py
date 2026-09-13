"""Submit an exact owner-directed factual erratum as a gate-reopen proposal."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from .coverage_mission import CoverageMissionAuthority
from .deliverable_reopen import GateReopenAuthority
from .store import DaltonStore, content_hash

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--core-db',type=Path,required=True)
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--expected-candidate-sha256',required=True)
    p.add_argument('--actor-ref',required=True)
    a=p.parse_args(argv); wire=a.candidate.read_bytes()
    if hashlib.sha256(wire).hexdigest()!=a.expected_candidate_sha256:
        p.error('candidate file sha256 binding failed')
    candidate=json.loads(wire)
    with DaltonStore(str(a.core_db)) as store:
        source=(candidate.get('source_version') or {}).get('version_ref')
        row=store.connection.execute('SELECT mission_version_ref FROM mission_deliverable_versions WHERE version_id=?',(source,)).fetchone()
        if row is None:p.error('candidate source version was not found')
        mission=CoverageMissionAuthority(store).mission(row['mission_version_ref'])
        result=GateReopenAuthority(store).propose_human_revision(candidate=candidate,
            candidate_hash=content_hash(candidate),candidate_file_sha256=a.expected_candidate_sha256,
            mission=mission,actor_ref=a.actor_ref)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    return 0

if __name__=='__main__':raise SystemExit(main())
