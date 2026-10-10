"""Offline held-input scenario valuation; read-only by default, append on explicit flag."""
import argparse,json,sqlite3
from pathlib import Path
from .scenario_valuation import build_valuation,held_inputs,refresh_company
from .store import DaltonStore


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--db",required=True);p.add_argument("--company",required=True)
    p.add_argument("--state-dir");p.add_argument("--append",action="store_true");p.add_argument("--output")
    args=p.parse_args(argv)
    if args.append:
        with DaltonStore(args.db) as store: value=refresh_company(store,args.company,state_dir=args.state_dir)
    else:
        connection=sqlite3.connect("file:"+str(Path(args.db).resolve())+"?mode=ro",uri=True);connection.row_factory=sqlite3.Row
        try:value=build_valuation(**held_inputs(connection,args.company,state_dir=args.state_dir))
        finally:connection.close()
    text=json.dumps(value,ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(text+"\n")
    else:print(text)
    return 0

if __name__=="__main__":raise SystemExit(main())
