"""Raise the fixed SQLite inspection ceiling without skipping integrity checks."""
from __future__ import annotations
import argparse, json, os, subprocess, tempfile
from pathlib import Path
SUPPORTED_VERSION = "2026.9.3"
ORIGINAL = '''const SQLITE_INSPECTION_TIMEOUT_MS = 3e4;\nfunction sqliteInspectionTimeoutError(operation, pathname) {\n\treturn /* @__PURE__ */ new Error(`SQLite ${operation} timed out after 30 seconds for ${pathname}. Stop the Gateway service and other OpenClaw processes using this database, then retry; if already stopped, check storage performance.`);\n}'''
PATCHED = '''const SQLITE_INSPECTION_TIMEOUT_MS = 12e4;\nfunction sqliteInspectionTimeoutError(operation, pathname) {\n\treturn /* @__PURE__ */ new Error(`SQLite ${operation} timed out after 120 seconds for ${pathname}. Stop the Gateway service and other OpenClaw processes using this database, then retry; if already stopped, check storage performance.`);\n}'''
def target(root: Path) -> Path:
    if json.loads((root/'package.json').read_text())['version'] != SUPPORTED_VERSION: raise ValueError('unsupported OpenClaw version')
    found=list((root/'dist').glob('sqlite-readonly-worker-*.mjs'))
    if len(found)!=1: raise ValueError('expected one SQLite readonly worker bundle')
    return found[0]
def apply(root: Path, *, check: bool=False) -> bool:
    p=target(root); before=p.read_bytes(); text=before.decode(); oc=text.count(ORIGINAL); pc=text.count(PATCHED)
    if pc==1 and oc==0:return False
    if pc or oc!=1: raise ValueError('SQLite inspection timeout anchor changed')
    if check: raise ValueError('SQLite inspection timeout patch is missing')
    candidate=None
    try:
      with tempfile.NamedTemporaryFile('w',encoding='utf-8',suffix='.mjs',dir=p.parent,prefix='.'+p.name+'.',delete=False) as h:
       candidate=Path(h.name);h.write(text.replace(ORIGINAL,PATCHED,1))
      run=subprocess.run(['node','--check',str(candidate)],capture_output=True,text=True)
      if run.returncode: raise ValueError('patched bundle failed syntax validation')
      if p.read_bytes()!=before: raise ValueError('bundle changed during validation')
      os.chmod(candidate,p.stat().st_mode);os.replace(candidate,p)
    finally:
      if candidate: candidate.unlink(missing_ok=True)
    return True
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--openclaw-root',type=Path,required=True);parser.add_argument('--check',action='store_true');a=parser.parse_args();print('PATCH_CHANGED' if apply(a.openclaw_root,check=a.check) else 'OK')
