"""Install the exact approved SEC v4→v5 writer flag; never reload a service.

Governance migration and process configuration are separate durable steps. A
release-only interpreter replacement does not regenerate explicit lane flags.
"""
from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path

from .macos_launchagent import _atomic_plist
from .sec_statement_form_migration import preview as governance_preview
from .store import content_hash

FLAG = '--statement-lane-governance'
SCHEMA = 'sec-statement-writer-contract-sync:0.1'


def preview(workspace, launch_agents_dir):
    from .workspace_process import label_namespace
    from .mission_statement_lane import argv_fragment
    from .lane_registry import LaunchAgentContext
    label = ('space.lumos.dalton.writer' if workspace.workspace_id == 'legacy'
             else label_namespace(workspace.slug) + '.writer')
    path = Path(launch_agents_dir).expanduser().resolve() / (label + '.plist')
    if not path.exists():
        return {'status': 'skipped', 'reason': 'writer is not installed', 'path': str(path)}
    if path.is_symlink() or not path.is_file():
        raise ValueError('SEC writer sync requires a regular installed plist')
    raw = path.read_bytes()
    wire = plistlib.loads(raw)
    argv = wire.get('ProgramArguments')
    if wire.get('Label') != label or not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        raise ValueError('SEC writer sync installed process identity differs')
    if FLAG not in argv:
        return {'status': 'skipped', 'reason': 'installed SEC lane is disabled', 'path': str(path)}
    if argv.count(FLAG) != 1 or argv.index(FLAG) + 1 >= len(argv):
        raise ValueError('SEC writer sync governance flag is ambiguous')
    if argv.count('--db') != 1 or argv.index('--db') + 1 >= len(argv) or Path(argv[argv.index('--db') + 1]).expanduser().resolve() != Path(workspace.state_dir) / 'core.sqlite':
        raise ValueError('SEC writer sync installed Core identity differs')
    proposed = governance_preview(workspace)
    if proposed['status'] != 'existing':
        return {'status': 'skipped', 'reason': 'exact applied compatibility migration is required', 'path': str(path)}
    record = proposed['record']
    if 'compatibility_migration' not in record:
        raise ValueError('SEC writer sync has no existing-approval migration proof')
    state = Path(workspace.state_dir)
    target = state / 'connector-governance' / 'sec-financial-statements-v5.json'
    prior = state / 'connector-governance' / 'sec-financial-statements-v4.json'
    position = argv.index(FLAG) + 1
    installed = Path(argv[position]).expanduser().resolve()
    if installed not in {prior.resolve(), target.resolve()}:
        return {'status': 'skipped', 'reason': 'independent installed SEC source is preserved', 'path': str(path)}
    fragment = argv_fragment(LaunchAgentContext(state=state))
    if FLAG not in fragment or Path(fragment[fragment.index(FLAG) + 1]).resolve() != target.resolve():
        raise ValueError('SEC writer sync formal selector does not select migrated v5')
    if json.loads(prior.read_bytes()) != record['compatibility_migration']['prior_record']:
        raise ValueError('SEC writer sync prior approval changed')
    new = {**wire, 'ProgramArguments': list(argv)}
    new['ProgramArguments'][position] = str(target)
    after = plistlib.dumps(new, sort_keys=True)
    return {'schema_version': SCHEMA, 'status': 'existing' if installed == target.resolve() else 'eligible',
            'path': str(path), 'label': label, 'workspace_ref': workspace.workspace_id,
            'workspace_hash': workspace.content_hash, 'governance_hash': record['content_hash'],
            'before_sha256': hashlib.sha256(raw).hexdigest(), 'after_sha256': hashlib.sha256(raw if installed == target.resolve() else after).hexdigest(),
            'before_governance_path': argv[position], 'after_governance_path': str(target),
            'changed_argument_index': position, 'service_reload_performed': False,
            'plist': new}


def apply(workspace, launch_agents_dir, *, expected_plist_sha256):
    from .workspace_runtime_setup import _atomic_seed
    result = preview(workspace, launch_agents_dir)
    if result['status'] not in {'eligible', 'existing'}:
        return result
    path = Path(result['path'])
    before = path.read_bytes()
    if result['before_sha256'] != expected_plist_sha256 or hashlib.sha256(before).hexdigest() != expected_plist_sha256:
        raise ValueError('SEC writer sync installed plist changed since approval')
    if result['status'] == 'existing':
        return {k:v for k,v in result.items() if k != 'plist'}
    directory = Path(workspace.state_dir) / 'sec-statement-writer-contract-sync'
    if directory.is_symlink():
        raise ValueError('SEC writer sync original directory must be regular')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = directory / (result['before_sha256'] + '.plist')
    # Content-addressed exact original, exclusive and durable; no secret values
    # are returned by the command. Existing differing backup refuses.
    if backup.is_symlink():
        raise ValueError('SEC writer sync original backup must be regular')
    if backup.exists() and backup.read_bytes() != before:
        raise ValueError('SEC writer sync exact original backup differs')
    if not backup.exists():
        import os
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(before); stream.flush(); os.fsync(stream.fileno())
    if path.read_bytes() != before:
        raise ValueError('SEC writer sync installed plist changed before write')
    _atomic_plist(path, result['plist'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != result['after_sha256']:
        raise ValueError('SEC writer sync installed result differs')
    receipt = {k:v for k,v in result.items() if k != 'plist'}
    receipt.update(status='applied', original_backup=str(backup))
    receipt['content_hash'] = content_hash(receipt)
    _atomic_seed(directory / (receipt['content_hash'] + '.json'), receipt)
    return receipt
