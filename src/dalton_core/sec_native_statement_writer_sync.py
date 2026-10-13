"""Install the explicitly owner-approved native v6 writer flag; never reload."""
from __future__ import annotations
import hashlib
import json
import plistlib
from pathlib import Path
from .store import content_hash
from .macos_launchagent import _atomic_plist

FLAG='--statement-lane-governance'
SCHEMA='sec-native-statement-writer-sync:0.1'


def preview(workspace, launch_agents_dir, *, approved_by):
    from .sec_native_statement_governance import preview as approval_preview
    from .workspace_process import label_namespace
    from .mission_statement_lane import argv_fragment
    from .lane_registry import LaunchAgentContext
    label=('space.lumos.dalton.writer' if workspace.workspace_id=='legacy' else label_namespace(workspace.slug)+'.writer')
    path=Path(launch_agents_dir).expanduser().resolve()/(label+'.plist')
    if path.is_symlink() or not path.is_file():
        raise ValueError('native writer selector requires a regular installed plist')
    raw=path.read_bytes();wire=plistlib.loads(raw);argv=wire.get('ProgramArguments')
    if (wire.get('Label')!=label or not isinstance(argv,list) or not all(isinstance(x,str) for x in argv)
            or argv[1:3]!=['-m','dalton_core.writer_server']
            or argv.count(FLAG)!=1 or argv.index(FLAG)+1>=len(argv)
            or argv.count('--db')!=1 or argv.index('--db')+1>=len(argv)
            or Path(argv[argv.index('--db')+1]).expanduser().resolve()!=Path(workspace.state_dir)/'core.sqlite'):
        raise ValueError('native writer selector process or Core identity differs')
    proposed=approval_preview(workspace,approved_by=approved_by)
    if proposed['status']!='existing':
        raise ValueError('native writer selector requires the already-applied explicit owner approval')
    state=Path(workspace.state_dir);target=Path(proposed['path']);prior=Path(proposed['retained_prior_path'])
    position=argv.index(FLAG)+1;installed=Path(argv[position]).expanduser().resolve()
    if installed not in {prior.resolve(),target.resolve()}:
        raise ValueError('native writer selector refuses an unrelated existing approval')
    fragment=argv_fragment(LaunchAgentContext(state=state))
    if FLAG not in fragment or Path(fragment[fragment.index(FLAG)+1]).resolve()!=target.resolve():
        raise ValueError('native writer selector formal default does not select approved v6')
    new={**wire,'ProgramArguments':list(argv)};new['ProgramArguments'][position]=str(target)
    after=raw if installed==target.resolve() else plistlib.dumps(new,sort_keys=True)
    return {'schema_version':SCHEMA,'status':'existing' if installed==target.resolve() else 'eligible',
        'path':str(path),'label':label,'workspace_ref':workspace.workspace_id,'workspace_hash':workspace.content_hash,
        'approval_binding':proposed['binding'],'governance_hash':proposed['record']['content_hash'],
        'before_sha256':hashlib.sha256(raw).hexdigest(),'after_sha256':hashlib.sha256(after).hexdigest(),
        'before_governance_path':argv[position],'after_governance_path':str(target),
        'changed_argument_index':position,'service_reload_performed':False,'plist':new}

def apply(workspace, launch_agents_dir, *, approved_by, expected_plist_sha256):
    from .workspace_runtime_setup import _atomic_seed
    result = preview(workspace, launch_agents_dir, approved_by=approved_by)
    if result['status'] not in {'eligible', 'existing'}:
        return result
    path = Path(result['path'])
    before = path.read_bytes()
    if result['before_sha256'] != expected_plist_sha256 or hashlib.sha256(before).hexdigest() != expected_plist_sha256:
        raise ValueError('SEC writer sync installed plist changed since approval')
    if result['status'] == 'existing':
        return {k:v for k,v in result.items() if k != 'plist'}
    directory = Path(workspace.state_dir) / 'sec-native-statement-writer-sync'
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
    from .sec_financials_cli import _load_governance
    target,_ = _load_governance(Path(result['after_governance_path']))
    prior,_ = _load_governance(Path(workspace.state_dir) / 'connector-governance' / 'sec-financial-statements-v5.json')
    if (target.content_hash != result['governance_hash']
            or prior.content_hash != result['approval_binding']['prior_governance_hash']):
        raise ValueError('native writer selector approval changed before write')
    _atomic_plist(path, result['plist'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != result['after_sha256']:
        raise ValueError('SEC writer sync installed result differs')
    receipt = {k:v for k,v in result.items() if k != 'plist'}
    receipt.update(status='applied', original_backup=str(backup))
    receipt['content_hash'] = content_hash(receipt)
    _atomic_seed(directory / (receipt['content_hash'] + '.json'), receipt)
    return receipt
