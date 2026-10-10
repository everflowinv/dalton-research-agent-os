"""Offline held-data model-cell replay; default never opens a writer."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from collections import Counter
from .model_cell_reconciliation import ModelCellReconciliationAuthority, collect_actuals, compare_cells, _verified
from .store import DaltonStore

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--company',required=True)
    parser.add_argument('--apply',action='store_true',help='append comparisons only; never modifies forecast/actual source rows')
    args=parser.parse_args(argv)
    if args.apply:
        store=DaltonStore(str(args.state_dir/'core.sqlite'))
        try:records=ModelCellReconciliationAuthority(store).refresh(args.company)
        finally:store.close()
    else:
        connection=sqlite3.connect((args.state_dir/'core.sqlite').resolve().as_uri()+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row
        try:
            actuals,events=collect_actuals(connection,args.company)
            records=[record for row in connection.execute('SELECT * FROM forecast_model_versions WHERE company_ref=? ORDER BY created_at',(args.company,))
                     for record in compare_cells(_verified(row),actuals,events)]
        finally:connection.close()
    print(json.dumps({'company_ref':args.company,'mode':'apply' if args.apply else 'dry_run',
                      'cells':len(records),'compared':sum(row['status']=='compared' for row in records),
                      'unavailable_reasons':dict(Counter(row['reason'] for row in records if row['status']=='unavailable')),
                      'redraft_required':sum(row['redraft_required'] for row in records),
                      'records':records},ensure_ascii=False,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
