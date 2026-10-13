"""Closed goal identity compatibility through official release-only transitions."""
from __future__ import annotations
import hashlib,json,os,re,tempfile
from pathlib import Path
from typing import Any,Mapping

SCHEMA='workspace-release-transition:0.1'
BINDING_SCHEMA='workspace-first-goal-stable-binding:0.1'
HISTORY='workspace-release-history'
MAX_HOPS=32
SHA=re.compile(r'^[0-9a-f]{64}$')

def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _identity(manifest):
    return {k:v for k,v in manifest.items() if k not in {'content_hash','release_path','release_ref','shared_readonly_paths'}}

def _checked(manifest):
    if not isinstance(manifest,dict) or not SHA.fullmatch(str(manifest.get('content_hash',''))) or _hash({k:v for k,v in manifest.items()if k!='content_hash'})!=manifest['content_hash']:
        raise ValueError('workspace manifest hash differs')
    return manifest

def _release_only(old,new):
    _checked(old);_checked(new)
    if _identity(old)!=_identity(new):
        raise ValueError('workspace stable identity or permissions changed')
    for m in (old,new):
        path=Path(m['release_path'])
        if not path.is_absolute() or str(path.resolve())!=str(path) or not re.fullmatch(r'release:sha256:[0-9a-f]{64}',m['release_ref']):
            raise ValueError('workspace release identity is invalid')
        shared=m['shared_readonly_paths']
        if not isinstance(shared,list)or len(set(shared))!=len(shared)or m['release_path']not in shared:
            raise ValueError('workspace shared read-only permissions are invalid')
    if set(new['shared_readonly_paths'])!=set(old['shared_readonly_paths'])|{new['release_path']}:
        raise ValueError('workspace non-release read-only permissions changed')

def _fsync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def record_transition(old_bytes:bytes,new:dict[str,Any])->str|None:
    """Official switch records exact old bytes before changing workspace.json."""
    old=_checked(json.loads(old_bytes));_release_only(old,new)
    root=Path(old['workspace_root'])
    if not root.is_absolute() or Path(old['state_dir']).resolve()!=root.resolve()/'state/dalton-core':
        raise ValueError('workspace history must be inside canonical workspace state')
    if old['content_hash']==new['content_hash']:return None
    directory=Path(old['state_dir'])/HISTORY
    if directory.is_symlink():raise ValueError('workspace release history directory is a symlink')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(directory,0o700);_fsync_dir(directory.parent)
    body={'schema_version':SCHEMA,'producer':'scripts/release_switch.py','old_manifest_json':old_bytes.decode('utf-8'),'old_manifest_file_sha256':hashlib.sha256(old_bytes).hexdigest(),'old_manifest_hash':old['content_hash'],'new_manifest':new,'new_manifest_hash':new['content_hash']}
    wire={**body,'content_hash':_hash(body)};data=(json.dumps(wire,ensure_ascii=False,indent=2)+'\n').encode();target=directory/(new['content_hash']+'.json')
    fd,name=tempfile.mkstemp(prefix='.transition-',dir=directory)
    try:
        with os.fdopen(fd,'wb')as f:f.write(data);f.flush();os.fsync(f.fileno())
        try:os.link(name,target)
        except FileExistsError:
            if target.is_symlink()or target.read_bytes()!=data:raise ValueError('workspace release transition proof collision')
        _fsync_dir(directory)
    finally:Path(name).unlink(missing_ok=True)
    return str(target)

def current_manifest(workspace):
    from .workspace import WorkspacePaths
    path=workspace.manifest_path or workspace.workspace_root/'workspace.json'
    manifest=_checked(json.loads(Path(path).read_bytes()));parsed=WorkspacePaths.from_manifest(manifest)
    if parsed.content_hash!=workspace.content_hash or parsed.workspace_id!=workspace.workspace_id:
        raise ValueError('workspace manifest differs from current authority')
    return manifest

def stable_binding(workspace):
    m=current_manifest(workspace)
    return {'schema_version':BINDING_SCHEMA,'manifest_hash_at_draft':m['content_hash'],'stable_identity_hash':_hash(_identity(m)),'shared_readonly_paths':list(m['shared_readonly_paths'])}

def verify_binding(workspace,proposal:Mapping[str,Any])->None:
    from .workspace import WorkspacePaths
    current=current_manifest(workspace);expected=proposal.get('workspace_manifest_hash')
    if not isinstance(expected,str)or not SHA.fullmatch(expected):raise ValueError('proposal workspace manifest hash is invalid')
    seen=set();original=current
    for _ in range(MAX_HOPS+1):
        key=original['content_hash']
        if key==expected:break
        if key in seen:raise ValueError('workspace release history has a cycle')
        if len(seen)>=MAX_HOPS:raise ValueError('workspace release history exceeds bounded hops')
        seen.add(key);directory=workspace.state_dir/HISTORY;path=directory/(key+'.json')
        if directory.is_symlink()or path.is_symlink():raise ValueError('workspace release transition proof is a symlink')
        if not path.is_file()or path.stat().st_size>128000:raise ValueError('workspace release transition proof is unavailable')
        if directory.stat().st_uid!=os.getuid() or directory.stat().st_mode&0o077 or path.stat().st_uid!=os.getuid() or path.stat().st_mode&0o077:
            raise ValueError('workspace release transition proof permissions are not private')
        wire=json.loads(path.read_bytes());fields={'schema_version','producer','old_manifest_json','old_manifest_file_sha256','old_manifest_hash','new_manifest','new_manifest_hash','content_hash'}
        if not isinstance(wire,dict)or set(wire)!=fields or wire['schema_version']!=SCHEMA or wire['producer']!='scripts/release_switch.py' or _hash({k:v for k,v in wire.items()if k!='content_hash'})!=wire['content_hash']:
            raise ValueError('workspace release transition proof hash or schema differs')
        raw=wire['old_manifest_json'].encode('utf-8')
        if hashlib.sha256(raw).hexdigest()!=wire['old_manifest_file_sha256']:raise ValueError('workspace prior manifest bytes differ')
        old=_checked(json.loads(raw));new=_checked(wire['new_manifest'])
        if new!=original or wire['new_manifest_hash']!=key or wire['old_manifest_hash']!=old['content_hash']:
            raise ValueError('workspace release transition is not bound to current manifest')
        WorkspacePaths.from_manifest(old);WorkspacePaths.from_manifest(new);_release_only(old,new);original=old
    if original['content_hash']!=expected:raise ValueError('workspace release history does not reach draft')
    binding=proposal.get('workspace_identity_binding')
    if binding is not None:
        wanted={'schema_version':BINDING_SCHEMA,'manifest_hash_at_draft':expected,'stable_identity_hash':_hash(_identity(original)),'shared_readonly_paths':list(original['shared_readonly_paths'])}
        if binding!=wanted:raise ValueError('proposal stable workspace binding differs')
    if original['workspace_id']!=proposal.get('workspace_id')or _identity(original)!=_identity(current):
        raise ValueError('proposal belongs to another workspace')
