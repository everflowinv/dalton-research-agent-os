"""Owner-authored prospective outcome targets; default validation is read-only."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from .outcome_registration import OutcomeRegistrationAuthority
from .store import DaltonStore

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--draft',type=Path,required=True)
    parser.add_argument('--actor',required=True)
    parser.add_argument('--apply',action='store_true',help='append this explicit owner target; no retrospective registration')
    args=parser.parse_args(argv);draft=json.loads(args.draft.read_text())
    if args.apply:
        store=DaltonStore(str(args.state_dir/'core.sqlite'));authority=OutcomeRegistrationAuthority(store)
    else:
        connection=sqlite3.connect((args.state_dir/'core.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        connection.row_factory=sqlite3.Row
        store=SimpleNamespace(connection=connection,path=str(args.state_dir/'core.sqlite'),close=connection.close)
        authority=object.__new__(OutcomeRegistrationAuthority);authority.store=store;authority.connection=connection
        authority.clock=lambda:datetime.now(timezone.utc).isoformat(timespec='microseconds')
    try:
        result=authority.record(draft,actor_ref=args.actor,apply=args.apply)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    finally:store.close()
    return 0

if __name__=='__main__':raise SystemExit(main())
