"""Loopback gateway authentication; disabled unless explicitly configured."""
from __future__ import annotations
import os,re,secrets
from pathlib import Path
from urllib.parse import urlsplit
from .shared_access_identity import SharedLogin

def secret(path: str | Path) -> str:
    p=Path(path)
    if not p.is_absolute() or p.is_symlink(): raise ValueError('shared secret path invalid')
    st=p.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or not p.is_file():
        raise ValueError('shared secret permissions invalid')
    value=p.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,256}',value): raise ValueError('shared secret shape invalid')
    return value

def validate_config(raw):
    if raw is None:return None
    if not isinstance(raw,dict) or set(raw)!={'secret_file','public_origin','principal'}:raise ValueError('shared gateway config invalid')
    origin=urlsplit(raw['public_origin']);p=Path(raw['secret_file'])
    if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment or not p.is_absolute():raise ValueError('shared gateway origin/path invalid')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}',raw['principal']):raise ValueError('shared gateway principal invalid')
    return dict(raw)

def authenticate(config,headers,peer):
    if config is None or peer not in {'127.0.0.1','::1'}:return None
    if len(headers.get_all('X-Dalton-Shared-Gateway',[]))!=1:return None
    try:
        expected=secret(config['secret_file'])
    except (OSError,ValueError):
        return None
    if not secrets.compare_digest(headers.get('X-Dalton-Shared-Gateway',''),expected):return None
    prefix=headers.get('X-Dalton-Public-Prefix','')
    if not re.fullmatch(r'/dalton/w/[a-z0-9][a-z0-9-]{0,62}/',prefix):return None
    return SharedLogin(config['principal'])
