"""Read-only receipt replay: quantify the convergence caps without model calls."""
from __future__ import annotations
import argparse
import json
import time
from collections import Counter
from pathlib import Path
from .research_publication_limits import MAX_CALLS,MAX_RUNS,WINDOW_SECONDS


def replay(stage_dir, *, now=None):
    now=time.time() if now is None else now
    stage_dir=Path(stage_dir)
    runs=Counter();calls={};unreadable=0;repairs=0
    paths=[*stage_dir.glob('*.json'),*(stage_dir.parent/'semantic-stages').glob('*.json')]
    for path in sorted(paths):
        at=path.stat().st_mtime
        if not now-WINDOW_SECONDS<at<=now:continue
        try:value=json.loads(path.read_text())
        except (OSError,ValueError):unreadable+=1;continue
        if not isinstance(value,dict) or not isinstance(value.get('source_hash'),str):unreadable+=1;continue
        source=value['source_hash']
        if path.parent==stage_dir:
            runs[source]+=1;repairs+=len(value.get('repair_brain_calls') or [])
        entries=[value.get(key) for key in ('draft','checker_call','brain_call','draft_repair_call','verifier_call')]
        entries+=list(value.get('repair_brain_calls') or [])
        entries+=[row.get('draft_call') for row in value.get('draft_failures') or [] if isinstance(row,dict)]
        entries+=[row.get('verifier_call') for row in value.get('review_history') or [] if isinstance(row,dict)]
        for call in entries:
            if not isinstance(call,dict):continue
            ref=call.get('result_envelope_ref') or call.get('work_order_ref') or json.dumps(call,sort_keys=True,ensure_ascii=False)
            key=(source,ref)
            prior=calls.get(key)
            if prior is None or at<prior['at']:calls[key]={'at':at,'cost_micros':call.get('cost_micros')}
    by_source={}
    for (source,_),call in calls.items():by_source.setdefault(source,[]).append(call)
    rows=[]
    for source in sorted(set(runs)|set(by_source)):
        entries=sorted(by_source.get(source,[]),key=lambda call:call['at'])
        withheld=entries[MAX_CALLS:]
        rows.append({'source_hash':source,'runs':runs[source],'unique_calls':len(entries),
                     'runs_over_limit':max(runs[source]-MAX_RUNS,0),'calls_over_limit':len(withheld),
                     'recorded_cost_over_call_limit_micros':sum(int(call['cost_micros'] or 0) for call in withheld),
                     'missing_cost_over_limit':sum(call['cost_micros'] is None for call in withheld)})
    return {'schema_version':'publication-convergence-replay:1','as_of_epoch':now,'window_seconds':WINDOW_SECONDS,
            'read_only':True,'source_artifacts':len(rows),'runs':sum(runs.values()),'unique_calls':len(calls),
            'repeated_artifacts':sum(row['runs']>1 for row in rows),'max_runs':max(runs.values(),default=0),
            'repair_calls_in_stage_files':repairs,'runs_prevented_by_cap':sum(row['runs_over_limit'] for row in rows),
            'calls_prevented_by_cap':sum(row['calls_over_limit'] for row in rows),
            'recorded_cost_prevented_micros':sum(row['recorded_cost_over_call_limit_micros'] for row in rows),
            'missing_cost_prevented':sum(row['missing_cost_over_limit'] for row in rows),'unreadable':unreadable,
            'rows':sorted(rows,key=lambda row:(-row['runs'],-row['unique_calls'],row['source_hash'])),
            'limitations':'Stage mtime is an observation time, not a scheduler dispatch timestamp. Hashes can be historical chunks. '
                'Call identities are deduplicated across copied receipts. This estimates the call-cap effect only; run caps, '
                'substance gates and delta caching overlap and are not added. Recorded receipt cost is not a new billing claim.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-dir',type=Path,required=True)
    parser.add_argument('--now',type=float)
    args=parser.parse_args(argv)
    print(json.dumps(replay(args.stage_dir,now=args.now),ensure_ascii=False,sort_keys=True,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
