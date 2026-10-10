"""Read-only activation gate against the two formally published release pointers."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Any,Mapping

_RELEASE_REF = re.compile(r'(?:foundation-r[0-9]+[a-z]?|release:sha256:[0-9a-f]{64})')

def _read(path: str) -> tuple[dict[str,Any],str]:
    p=Path(path)
    if not p.is_absolute() or any(x.is_symlink() for x in (p,*p.parents)):
        raise ValueError('publication pointer path is not a regular absolute path')
    if p.stat().st_size>128000:raise ValueError('publication pointer exceeds the size limit')
    data=p.read_bytes();value=json.loads(data)
    if not isinstance(value,dict):raise ValueError('publication pointer must be an object')
    return value,hashlib.sha256(data).hexdigest()


WORKSPACE_GATE_KEYS=frozenset({'workspace_manifest'})


def _workspace_runtime_gate(config: Mapping[str,Any], *, prefix: str|None=None) -> dict[str,Any]:
    """A workspace's gate: run only under the release the workspace is pinned to.

    A workspace has no published release/runtime pointer pair -- those belong
    to the legacy install's activation.  What stands in for them is the
    workspace manifest: it names the one release this workspace's processes
    run, and the publication worker is 'active' only when the interpreter
    running it is that release's venv.  A manifest that does not load, or a
    worker started from another release, waits and makes no model call.
    """
    import sys
    base={'status':'waiting_for_release_publication','model_calls':0,
          'observed_release_sha256':None,'observed_runtime_sha256':None}
    raw=config.get('workspace_manifest')
    if not isinstance(raw,str) or not Path(raw).is_absolute():
        return {**base,'status':'invalid_release_authority','reason':'发布绑定配置格式无效'}
    try:
        from .workspace import load_workspace_manifest
        workspace=load_workspace_manifest(raw)
    except Exception:  # noqa: BLE001 - an unreadable manifest is not an active release
        return {**base,'reason':'工作区清单无法读取'}
    if workspace.lifecycle != 'active':
        return {**base,'status':'workspace_intentionally_stopped','reason':'研究项目已暂停、归档或删除'}
    running=Path(prefix if prefix is not None else sys.prefix).resolve()
    if Path(workspace.release_path).resolve()!=running:
        return {**base,'reason':'运行中的 release 不是工作区钉定的 release'}
    return {**base,'status':'active','release_ref':workspace.release_ref}


def published_runtime_gate(config: Mapping[str,Any], *, prefix: str|None=None) -> dict[str,Any]:
    if isinstance(config,Mapping) and set(config)==WORKSPACE_GATE_KEYS:
        return _workspace_runtime_gate(config,prefix=prefix)
    required={'release_pointer','runtime_pointer','expected_release_ref','expected_source_commit'}
    base={'status':'waiting_for_release_publication','model_calls':0,
          'observed_release_sha256':None,'observed_runtime_sha256':None}
    if (not isinstance(config,Mapping) or set(config)!=required
        or not re.fullmatch(r'[0-9a-f]{40}',str(config.get('expected_source_commit','')))
        or _RELEASE_REF.fullmatch(str(config.get('expected_release_ref',''))) is None):
        return {**base,'status':'invalid_release_authority','reason':'发布绑定配置格式无效'}
    try:
        release,rhash=_read(config['release_pointer']);base['observed_release_sha256']=rhash
        runtime,chash=_read(config['runtime_pointer']);base['observed_runtime_sha256']=chash
    except FileNotFoundError:return base
    except (OSError,ValueError,TypeError):
        return {**base,'status':'invalid_release_authority','reason':'发布记录无法核验'}
    if (release.get('schema_version')!='dalton-current-release-0.2'
        or runtime.get('schema_version')!='dalton-runtime-config-pointer-0.2'):
        return {**base,'status':'invalid_release_authority','reason':'发布记录版本不受支持'}
    if (release.get('status')!='deployed_verified' or runtime.get('status')!='deployed_verified'
        or release.get('release_ref')!=config['expected_release_ref']
        or release.get('source_commit')!=config['expected_source_commit']
        or runtime.get('base_release_commit')!=config['expected_source_commit']):return base
    manifest=release.get('candidate_manifest_sha256')
    if (release.get('current_runtime_config_sha256')!=chash
        or not isinstance(manifest,str) or re.fullmatch(r'[0-9a-f]{64}',manifest) is None
        or runtime.get('candidate_manifest_sha256')!=manifest):
        # The two pointer writes are independent. An intermediate pair must
        # never start work; the next scheduled pass can read the finished pair.
        return base
    return {**base,'status':'active'}
