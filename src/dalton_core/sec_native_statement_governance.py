"""Explicit owner approval for native annual v6; preview is the default.

This grants the new output scope. It is not a compatibility inference that an
old v5 approval already permitted foreign filings. Retain the old approval for
its exact local-only cached replay contract. Run apply only during the normal
managed deployment; this module never changes services, selectors or plists.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
from .store import content_hash


def preview(workspace, *, approved_by, effective_from=None):
    from .workspace_runtime_setup import _catalog
    from .sec_financials_cli import _load_governance
    from .connector_governance import build_governance_record, ConnectorGovernance
    from .sec_financials_core import KIND
    if not isinstance(approved_by, str) or not approved_by.startswith('human:') or len(approved_by) <= 6:
        raise ValueError('native annual approval requires the explicit owner principal')
    if workspace.lifecycle != 'active':
        raise ValueError('native annual approval requires an active managed environment')
    catalog = getattr(workspace, 'migration_catalog', None) or _catalog(workspace)
    if not isinstance(catalog, dict) or catalog.get('content_hash') != content_hash({k:v for k,v in catalog.items() if k != 'content_hash'}):
        raise ValueError('native annual source catalog hash differs')
    sources = [s for s in catalog['sources'] if s.get('connector_ref') == 'connector:sec-edgar']
    if len(sources) != 1:
        raise ValueError('native annual approval requires one selected SEC source')
    source = sources[0]
    if (source.get('enabled', True) is not True or source.get('auth_mode') != 'none'
            or source.get('allowed_hosts') != ['data.sec.gov','www.sec.gov']
            or set(source.get('allowed_operations', [])) != {'list_filings','get_company_facts'}
            or source.get('credential_slot_refs') != []
            or source.get('capability_id') != 'capability:dalton:connector:sec-edgar'
            or source.get('transport') != {'config_path':None,'endpoint_ref':'transport:public-http:0.1',
                                           'kind':'connector','socket_path':None}):
        raise ValueError('native annual source permissions or public endpoint differ')
    directory = Path(workspace.state_dir) / 'connector-governance'
    prior_path = directory / 'sec-financial-statements-v5.json'
    if prior_path.is_symlink() or not prior_path.is_file():
        raise ValueError('native annual approval requires the retained approved v5 predecessor')
    prior, version = _load_governance(prior_path)
    if version != 5:
        raise ValueError('native annual predecessor contract differs')
    now = datetime.now(timezone.utc)
    if (now < prior._parse_time(prior.effective_from, 'effective_from')
            or (prior.wire['effective_until'] is not None and now >= prior._parse_time(prior.wire['effective_until'], 'effective_until'))):
        raise ValueError('native annual predecessor is outside its approval window')
    start = effective_from or now.isoformat()
    if prior._parse_time(start, 'effective_from') < prior._parse_time(prior.effective_from, 'prior_effective_from'):
        raise ValueError('native annual approval predates the original source approval')
    record = build_governance_record(KIND, approved_by=approved_by, status='approved', version=6,
        effective_from=start, max_lease_seconds=prior.wire['max_lease_seconds'])
    record['effective_until'] = prior.wire['effective_until']
    record['content_hash'] = content_hash({k:v for k,v in record.items() if k != 'content_hash'})
    # Explicit new approval does not increase public transport or lease rights.
    if (record['allowed_permissions'] != prior.wire['allowed_permissions']
            or record['expected_source_hash'] != prior.wire['expected_source_hash']):
        raise ValueError('native annual approval changes the source transport scope')
    target = directory / 'sec-financial-statements-v6.json'
    status = 'eligible'
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise ValueError('native annual approval target must be regular')
        existing, version = _load_governance(target)
        if version != 6 or existing.wire['approved_by'] != approved_by:
            raise ValueError('native annual existing owner approval differs')
        record = existing.wire
        status = 'existing'
    from .sec_native_annual import verify_native_approval
    verify_native_approval(ConnectorGovernance(record))
    binding = {'workspace_ref': workspace.workspace_id, 'workspace_hash': workspace.content_hash,
               'catalog_hash': catalog['content_hash'], 'source_hash': content_hash(source),
               'prior_governance_hash': prior.content_hash}
    return {'status': status, 'contract_ref': 'sec-native-annual-owner-approval:0.1', 'binding': binding,
            'path': str(target), 'retained_prior_path': str(prior_path), 'record': record,
            'selector_change_required': True, 'service_actions': 0}


def apply(workspace, *, approved_by, expected_binding):
    from .workspace_runtime_setup import _atomic_seed
    from .sec_financials_cli import _load_governance
    result = preview(workspace, approved_by=approved_by)
    if result['binding'] != expected_binding:
        raise ValueError('native annual approval managed source binding changed')
    prior_bytes = Path(result['retained_prior_path']).read_bytes()
    if result['status'] == 'eligible':
        _atomic_seed(Path(result['path']), result['record'])
    saved, version = _load_governance(Path(result['path']))
    if version != 6 or saved.wire != result['record'] or Path(result['retained_prior_path']).read_bytes() != prior_bytes:
        raise ValueError('native annual approval raced a different record')
    return {**result, 'status':'applied' if result['status']=='eligible' else 'existing'}


def main(argv=None):
    import json
    from .sec_statement_form_migration import managed_workspace
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manager',required=True);parser.add_argument('--environment',required=True)
    parser.add_argument('--approved-by',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--sync-writer',action='store_true')
    parser.add_argument('--launch-agents-dir',default=str(Path.home()/'Library'/'LaunchAgents'))
    parser.add_argument('--expected-plist-sha256')
    parser.add_argument('--expected-binding-json')
    args=parser.parse_args(argv)
    workspace=managed_workspace(args.manager,args.environment)
    if args.sync_writer:
        from .sec_native_statement_writer_sync import preview as sync_preview, apply as sync_apply
        if args.apply and not args.expected_plist_sha256:
            parser.error('--sync-writer --apply requires --expected-plist-sha256')
        result=(sync_apply(workspace,args.launch_agents_dir,approved_by=args.approved_by,
                    expected_plist_sha256=args.expected_plist_sha256) if args.apply
                else sync_preview(workspace,args.launch_agents_dir,approved_by=args.approved_by))
        print(json.dumps({k:v for k,v in result.items() if k!='plist'},ensure_ascii=False))
        return 0
    if args.apply and not args.expected_binding_json:
        parser.error('--apply requires the exact preview binding JSON')
    result=(apply(workspace,approved_by=args.approved_by,expected_binding=json.loads(args.expected_binding_json))
            if args.apply else preview(workspace,approved_by=args.approved_by))
    # Binding hashes and public approval only; no connection credentials.
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
