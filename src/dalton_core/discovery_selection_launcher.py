"""Owner-only asynchronous child lifecycle for discovery candidate selection."""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from .child_tickets import adopt_finished_child
from .store import canonical_json, content_hash

PREFIX="discovery-selection"; _RE=re.compile(r"discovery-selection:[0-9a-f]{24}\Z")
def _write(path:Path,value:Any):
    tmp=path.with_name('.'+path.name+'.tmp'); tmp.write_text(canonical_json(value)+'\n'); os.chmod(tmp,0o600); os.replace(tmp,path)
def _now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds")

class DiscoverySelectionLauncher:
    def __init__(self,*,state_dir:str|Path,model_config_path:str|Path,scheduler_db:str|Path,
                 python_executable:str|None=None):
        self.state=Path(state_dir).resolve(); self.config=Path(model_config_path).resolve()
        self.scheduler=Path(scheduler_db).resolve(); self.python=python_executable or sys.executable
        self.root=self.state/'discovery-selections'; self.root.mkdir(parents=True,exist_ok=True); os.chmod(self.root,0o700)
        self._lock=threading.Lock(); self._current=None
    def latest(self,discovery_ref:str):
        found=[]
        for path in self.root.glob('*/ticket.json'):
            try: row=json.loads(path.read_text())
            except (OSError,ValueError): continue
            if row.get('discovery_ref')==discovery_ref: found.append(row)
        return None if not found else self.status(sorted(found,key=lambda x:x['started_at'])[-1]['id'])
    def start(self,*,discovery_ref:str,view:Mapping[str,Any],mission_ref:str,
              company:Mapping[str,Any],missing_periods:list[str]):
        identity={"view_hash":view['content_hash'],"mission_ref":mission_ref,"company":dict(company),
                  "missing_periods":missing_periods,"config_hash":hashlib.sha256(self.config.read_bytes()).hexdigest()}
        digest=content_hash(identity)[:24]; tid=f'{PREFIX}:{digest}'; directory=self.root/digest
        directory.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(directory,0o700)
        existing=self.latest(discovery_ref)
        if existing and existing.get('identity_hash')==content_hash(identity): return existing
        with self._lock:
            if self._current and self._current[1].poll() is None: return {"status":"busy"}
            _write(directory/'input.json',{"view":dict(view),"mission_ref":mission_ref,
                "company":dict(company),"missing_periods":missing_periods,"identity_hash":content_hash(identity)})
            log=os.open(directory/'run.log',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            try: proc=subprocess.Popen([self.python,'-m','dalton_core.discovery_selection_cli',
                '--state-dir',str(self.state),'--model-config',str(self.config),'--scheduler-db',str(self.scheduler),
                '--input',str(directory/'input.json'),'--summary-dir',str(directory)],stdin=subprocess.DEVNULL,
                stdout=log,stderr=subprocess.STDOUT,cwd=self.state)
            finally: os.close(log)
            row={"schema_version":"0.1","id":tid,"discovery_ref":discovery_ref,
                 "identity_hash":content_hash(identity),"started_at":_now(),"pid":proc.pid,"status":"running",
                 "exit_code":None,"completed_at":None}
            _write(directory/'ticket.json',row); self._current=(tid,proc); return row
    def status(self,ref:str):
        if not _RE.fullmatch(ref): raise ValueError('invalid discovery selection ticket')
        path=self.root/ref.split(':',1)[1]/'ticket.json'; row=json.loads(path.read_text())
        with self._lock:
            proc=self._current[1] if self._current and self._current[0]==ref else None
            if row['status']=='running':
                code=proc.poll() if proc else None
                if proc and code is not None:
                    row.update(status='succeeded' if code==0 else 'failed',exit_code=code,completed_at=_now()); _write(path,row)
                elif not proc and not self._alive(row['pid']):
                    if not adopt_finished_child(row,path.with_name('summary.json'),now=_now()): row.update(status='orphaned',completed_at=_now())
                    _write(path,row)
        summary=path.with_name('summary.json')
        return {**row,"summary":json.loads(summary.read_text()) if row['status']!='running' and summary.exists() else None}
    @staticmethod
    def _alive(pid):
        try: os.kill(pid,0); return True
        except (ProcessLookupError,TypeError): return False
        except PermissionError: return True
