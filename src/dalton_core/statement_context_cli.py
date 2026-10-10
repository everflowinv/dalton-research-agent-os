"""Offline complete-context backfill; dry-run by default, held local bytes only."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from .statement_context import prepare_context, StatementContextAuthority


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--company', action='append')
    parser.add_argument('--ingest-id', action='append')
    parser.add_argument('--apply', action='store_true', help='Append verified context versions and explicit refusal records')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    db = args.db.expanduser().resolve()
    state = (args.state_dir or db.parent).expanduser().resolve()
    store = None
    if args.apply:
        from .store import DaltonStore
        store = DaltonStore(str(db))
        connection = store.connection
        authority = StatementContextAuthority(store)
    else:
        connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
    try:
        filings = connection.execute('SELECT ingest_id,company_ref FROM coverage_mission_statement_filings ORDER BY company_ref,report_date,accession').fetchall()
        results = []
        sources = {}
        from .valuation_history import company_facts
        for row in filings:
            if args.company and row['company_ref'] not in args.company:
                continue
            if args.ingest_id and row['ingest_id'] not in args.ingest_id:
                continue
            if row['company_ref'] not in sources:
                sources[row['company_ref']] = company_facts(connection, state, row['company_ref'])
            source = sources[row['company_ref']]
            record = (authority.backfill(state, row['ingest_id'], companyfacts_source=source, discover_companyfacts=False)
                      if args.apply else prepare_context(connection, state, row['ingest_id'], companyfacts_source=source, discover_companyfacts=False))
            results.append({key: record.get(key) for key in ('version_id','ingest_id','company_ref','status','kind','reason','mapping_hash')})
        summary = {'dry_run': not args.apply, 'filings': len(results), 'available': sum(r['status']=='available' for r in results),
                   'refused': sum(r['status']=='refused' for r in results), 'results': results}
        encoded = json.dumps(summary, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            args.output.write_text(encoded, encoding='utf-8')
        else:
            print(encoded, end='')
    finally:
        store.close() if store is not None else connection.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
