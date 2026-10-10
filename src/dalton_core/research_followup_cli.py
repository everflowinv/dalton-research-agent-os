"""Offline research followup inspection and capture; defaults to read-only."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from .research_followup import ResearchFollowupAuthority, requests
from .store import DaltonStore

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--capture',action='store_true',help='register held reflection/framework/checkpoint requests')
    parser.add_argument('--apply',action='store_true',help='append captured requests; never modifies research source authorities')
    args=parser.parse_args(argv)
    if args.apply and not args.capture:parser.error('--apply requires --capture')
    db=(args.state_dir/'core.sqlite').resolve()
    if args.apply:
        store=DaltonStore(str(db))
        try:
            authority=ResearchFollowupAuthority(store)
            captured=authority.capture_existing()
            records=requests(store.connection)
        finally:store.close()
    else:
        c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        try:records=requests(c);captured=[]
        finally:c.close()
    print(json.dumps({'mode':'apply' if args.apply else 'read_only','captured':len(captured),'requests':records,
                      'capture_pending_authorization':args.capture and not args.apply},ensure_ascii=False,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
