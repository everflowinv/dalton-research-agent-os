"""Review or explicitly apply a bounded existing-industry specialization."""
import argparse
import json
from pathlib import Path
from .industry_archetype_repair import plan_repair, apply_repair, ArchetypeRepairError
from .readonly_sqlite import connect_read_only, connect_cold_wal_snapshot
from .store import DaltonStore

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--mission',required=True)
    p.add_argument('--output',type=Path)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--plan',type=Path)
    p.add_argument('--plan-hash')
    p.add_argument('--actor')
    p.add_argument('--cold-snapshot',action='store_true',help='Read a checkpointed, sidecar-free isolated WAL copy; never a live database.')
    args=p.parse_args(argv)
    db=args.state_dir/'core.sqlite'
    if args.output and args.output.resolve() == db.resolve():
        p.error('the review output cannot replace the Core authority database')
    if args.apply:
        if args.cold_snapshot or not args.plan or not args.plan_hash or not args.actor:
            p.error('--apply requires --plan, --plan-hash and --actor, without --cold-snapshot')
        plan=json.loads(args.plan.read_text())
        if plan.get('content_hash') != args.plan_hash or plan.get('prior_mission',{}).get('mission_ref') != args.mission:
            raise ArchetypeRepairError('confirmed plan hash or mission differs')
        store=DaltonStore(db)
        try: result=apply_repair(store,args.state_dir,plan,actor_ref=args.actor)
        finally: store.close()
    elif args.cold_snapshot:
        with connect_cold_wal_snapshot(db) as connection:
            connection.row_factory=__import__('sqlite3').Row
            result=plan_repair(connection,args.mission)
    else:
        connection=connect_read_only(db);connection.row_factory=__import__('sqlite3').Row
        try: result=plan_repair(connection,args.mission)
        finally: connection.close()
    rendered=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(rendered)
    else: print(rendered,end='')
    return 0

if __name__=='__main__':raise SystemExit(main())
