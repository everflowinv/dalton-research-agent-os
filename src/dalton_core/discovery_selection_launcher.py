"""Owner-only asynchronous child lifecycle for discovery candidate selection."""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys, threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from .child_tickets import adopt_finished_child
from .lane_child_launcher import process_matches
from .store import canonical_json, content_hash

def _formal_selection_valid(scheduler_db: Path, selection: Mapping[str, Any],
                            source: Mapping[str, Any]) -> bool:
    """Verify the selection came from one exact successful Scheduler result."""
    from .contracts import ResultEnvelope, WorkOrder
    from .readonly_sqlite import connect_read_only
    try:
        with closing(connect_read_only(scheduler_db)) as connection:
            import sqlite3
            connection.row_factory = sqlite3.Row
            work_row = connection.execute(
                "SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?",
                (selection.get("work_order_ref"),)).fetchone()
            formal = connection.execute(
                "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
                (selection.get("work_order_ref"),)).fetchone()
        if work_row is None or formal is None:
            return False
        work = WorkOrder.from_dict(json.loads(work_row["work_order_json"])).to_dict()
        envelope = ResultEnvelope.from_dict(json.loads(formal["result_envelope_json"])).to_dict()
        from .discovery_candidate_selection import selection_prompt, validate_selection
        validated = validate_selection(envelope.get("outputs", {}).get("text", ""), source["view"])
        record = {"id": formal["result_record_id"], "work_order_id": formal["work_order_id"],
                  "attempt_number": formal["attempt_number"],
                  "result_envelope_id": formal["result_envelope_id"],
                  "result_envelope_hash": formal["result_envelope_hash"],
                  "terminal_state": formal["terminal_state"], "created_at": formal["created_at"]}
        return bool(
            canonical_json(work) == work_row["work_order_json"]
            and content_hash(work) == work_row["work_order_hash"]
            and work.get("metadata", {}).get("purpose") == "discovery_selection"
            and work.get("metadata", {}).get("mission_version_ref") == source["mission_ref"]
            and work["question"] == selection_prompt(source["view"], company=source["company"],
                                                       missing_periods=source["missing_periods"])
            and canonical_json(envelope) == formal["result_envelope_json"]
            and content_hash(envelope) == formal["result_envelope_hash"]
            and content_hash(record) == formal["content_hash"]
            and formal["terminal_state"] == "succeeded"
            and selection.get("result_envelope_ref") == envelope["id"]
            and selection.get("invocation_ref") == envelope.get("invocation_ref")
            and selection.get("route_decision_ref") == envelope.get("metadata", {}).get("route_decision_ref")
            and all(selection.get(key) == value for key, value in validated.items())
        )
    except (Exception,):
        return False

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
        # Reuse the installed, closed capacity recovery policy.  It already
        # versions the model WorkOrder and is preserved by tier setup.
        retry = json.loads(self.config.read_text()).get("capacity_retry", {})
        self.max_recovery_epochs = retry.get("max_recovery_epochs", 0)
        self.cooldown_seconds = retry.get("cooldown_seconds", 300)
        if (not isinstance(self.max_recovery_epochs, int) or isinstance(self.max_recovery_epochs, bool)
                or not 0 <= self.max_recovery_epochs <= 10):
            raise ValueError("invalid discovery selection recovery limit")
        if (not isinstance(self.cooldown_seconds, int) or isinstance(self.cooldown_seconds, bool)
                or not 0 <= self.cooldown_seconds <= 86_400):
            raise ValueError("invalid discovery selection retry cooldown")
    def latest(self,discovery_ref:str):
        found=[]
        for path in self.root.glob('*/ticket.json'):
            try: row=json.loads(path.read_text())
            except (OSError,ValueError): continue
            if row.get('discovery_ref')==discovery_ref: found.append(row)
        return None if not found else self.status(sorted(found,key=lambda x:x['started_at'])[-1]['id'])
    def completed_empty_discoveries(self)->list[str]:
        result=[]
        for path in self.root.glob('*/ticket.json'):
            try:
                ticket=self.status(json.loads(path.read_text())['id']); summary=ticket.get('summary') or {}
                if ticket['status']=='succeeded' and summary.get('status')=='succeeded' and not summary.get('selection',{}).get('selected'):
                    result.append(ticket['discovery_ref'])
            except (OSError,ValueError,KeyError): continue
        return sorted(set(result))
    def successful_selections(self)->dict[str,tuple[str,...]]:
        result={}
        for path in self.root.glob('*/ticket.json'):
            try:
                ticket=self.status(json.loads(path.read_text())['id']); summary=ticket.get('summary') or {}
                if ticket['status']=='succeeded' and summary.get('status')=='succeeded':
                    result[ticket['discovery_ref']]=tuple(
                        row['document_ref'] for row in summary['selection']['selected'])
            except (OSError,ValueError,KeyError,TypeError):
                continue
        return result
    def start(self,*,discovery_ref:str,view:Mapping[str,Any],mission_ref:str,
              company:Mapping[str,Any],missing_periods:list[str]):
        identity={"view_hash":view['content_hash'],"mission_ref":mission_ref,"company":dict(company),
                  "missing_periods":missing_periods,"config_hash":hashlib.sha256(self.config.read_bytes()).hexdigest()}
        existing=self.latest(discovery_ref)
        epoch = 0
        if existing and existing.get('base_identity_hash', existing.get('identity_hash')) == content_hash(identity):
            if existing.get('status') in {'running', 'succeeded'}:
                return existing
            epoch = int(existing.get('recovery_epoch', 0)) + 1
            if epoch > self.max_recovery_epochs:
                return {**existing, "status": "exhausted"}
            completed = datetime.fromisoformat(existing['completed_at'])
            if self._now_datetime() < completed + timedelta(seconds=self.cooldown_seconds):
                return {**existing, "status": "cooldown"}
        attempt_identity = {**identity, "recovery_epoch": epoch}
        digest=content_hash(attempt_identity)[:24]; tid=f'{PREFIX}:{digest}'; directory=self.root/digest
        directory.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(directory,0o700)
        with self._lock:
            if self._current and self._current[1].poll() is None: return {"status":"busy"}
            for ticket_path in self.root.glob('*/ticket.json'):
                try:
                    persisted = json.loads(ticket_path.read_text())
                except (OSError, ValueError):
                    continue
                if (persisted.get('status') == 'running'
                        and process_matches(persisted.get('pid'), persisted.get('command'))):
                    return {"status":"busy"}
            _write(directory/'input.json',{"view":dict(view),"mission_ref":mission_ref,
                "company":dict(company),"missing_periods":missing_periods,
                "identity_hash":content_hash(attempt_identity), "recovery_epoch":epoch})
            log=os.open(directory/'run.log',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            command=[self.python,'-m','dalton_core.discovery_selection_cli',
                '--state-dir',str(self.state),'--model-config',str(self.config),'--scheduler-db',str(self.scheduler),
                '--input',str(directory/'input.json'),'--summary-dir',str(directory)]
            try: proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,
                stdout=log,stderr=subprocess.STDOUT,cwd=self.state)
            finally: os.close(log)
            row={"schema_version":"0.1","id":tid,"discovery_ref":discovery_ref,
                 "identity_hash":content_hash(attempt_identity), "base_identity_hash":content_hash(identity),
                 "recovery_epoch":epoch,"started_at":_now(),"pid":proc.pid,"status":"running",
                 "command":command,"exit_code":None,"completed_at":None}
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
                elif not proc and not process_matches(row.get('pid'), row.get('command')):
                    if not adopt_finished_child(row,path.with_name('summary.json'),now=_now()): row.update(status='orphaned',completed_at=_now())
                    _write(path,row)
        summary=path.with_name('summary.json')
        parsed = json.loads(summary.read_text()) if row['status']!='running' and summary.exists() else None
        if parsed is not None:
            source = json.loads(path.with_name('input.json').read_text())
            selection = parsed.get('selection') if isinstance(parsed, Mapping) else None
            valid = (parsed.get('identity_hash') == row['identity_hash']
                     and isinstance(selection, Mapping)
                     and selection.get('candidate_view_hash') == source['view']['content_hash']
                     and selection.get('recovery_epoch') == row.get('recovery_epoch', 0)
                     and _formal_selection_valid(self.scheduler, selection, source)
                     and selection.get('content_hash') == content_hash({
                         key: value for key, value in selection.items()
                         if key not in {'content_hash', 'work_order_ref', 'result_envelope_ref',
                                        'invocation_ref', 'route_decision_ref', 'replayed',
                                        'config_hash', 'recovery_epoch'}
                     }))
            if parsed.get('status') == 'succeeded' and not valid:
                row = {**row, 'status': 'failed', 'exit_code': 1,
                       'completed_at': row.get('completed_at') or _now(),
                       'failure_reason': 'selection summary authority drifted'}
                _write(path, row)
                parsed = None
        return {**row,"summary":parsed}
    def _now_datetime(self):
        return datetime.now(timezone.utc)
