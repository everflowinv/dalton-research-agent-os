"""Persistent publication limits shared by discovery, chunks and live dispatch.

Reservations are conservative: an uncertain call keeps its slot. A proved
not-sent dispatch does not consume the six-call allowance. SQLite serializes
concurrent workers; immutable events explain every admission and refusal.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .final_text_contract import FINAL_TEXT_RULES_VERSION
from .research_localization import source_content_hash
from .product_substance import SUBSTANCE_VERSION
from .openclaw_model_adapter import BrokerDefinitelyNotSent

LIMITS_VERSION = "publication-convergence:1"
WINDOW_SECONDS = 86400
MAX_RUNS = 2
MAX_CALLS = 6
MAX_LANGUAGE_ROUNDS = 2
_scope = contextvars.ContextVar("publication_scope", default=None)
_ticket = contextvars.ContextVar("publication_ticket", default=None)


class PublicationParked(ValueError):
    def __init__(self, reason, retry_after=None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason + (f"; retry_after={retry_after}" if retry_after else ""))


class PublicationDispatchParked(BrokerDefinitelyNotSent):
    """A local convergence refusal, never a provider health failure."""


def cache_binding(product):
    return {"source_hash": source_content_hash(product), "rules_version": FINAL_TEXT_RULES_VERSION,
            "substance_version": SUBSTANCE_VERSION, "limits_version": LIMITS_VERSION}


class PublicationLimits:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS publication_events (id INTEGER PRIMARY KEY, "
                       "at REAL NOT NULL, source_hash TEXT NOT NULL, tick TEXT NOT NULL, "
                       "stage TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, "
                       "cost_micros INTEGER NOT NULL DEFAULT 0, prompt_bytes INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE INDEX IF NOT EXISTS publication_window ON publication_events(source_hash,at)")
            db.execute("CREATE TRIGGER IF NOT EXISTS publication_events_no_update BEFORE UPDATE ON publication_events "
                       "BEGIN SELECT RAISE(ABORT,'publication events are append-only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS publication_events_no_delete BEFORE DELETE ON publication_events "
                       "BEGIN SELECT RAISE(ABORT,'publication events are append-only'); END")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS publication_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            if db.execute("SELECT 1 FROM publication_meta WHERE key='legacy_seed'").fetchone() is None:
                self._seed_legacy(db)
                db.execute("INSERT INTO publication_meta VALUES('legacy_seed','1')")
            with db: yield db
        finally: db.close()

    def _seed_legacy(self, db):
        """Import recent receipts conservatively, once; never rewrite old evidence."""
        seen=set()
        root=self.path.parent/'stages'
        if not root.is_dir():return
        semantic_calls={}
        semantic_root=self.path.parent/'semantic-stages'
        if semantic_root.is_dir():
            for path in semantic_root.glob('*.json'):
                try:
                    if path.stat().st_mtime<=self.clock()-WINDOW_SECONDS:continue
                    receipt=json.loads(path.read_text())
                except (OSError,ValueError):continue
                if isinstance(receipt,dict):semantic_calls.setdefault(receipt.get('source_hash'),[]).append(receipt.get('verifier_call'))
        for path in sorted(root.glob('*.json')):
            try:
                at=path.stat().st_mtime
                if at<=self.clock()-WINDOW_SECONDS:continue
                value=json.loads(path.read_text())
            except (OSError,ValueError):continue
            if not isinstance(value,dict):continue
            source=value.get('source_hash')
            if not isinstance(source,str) or not source:continue
            tick='legacy:'+path.stem
            db.execute("INSERT INTO publication_events(at,source_hash,tick,stage,kind,reason) VALUES(?,?,?,?,?,?)",
                       (at,source,tick,'pipeline','run','legacy_receipt'))
            calls=[value.get(key) for key in ('draft','checker_call','brain_call','draft_repair_call')]
            calls+=list(value.get('repair_brain_calls') or [])
            calls += [row.get('draft_call') for row in value.get('draft_failures') or [] if isinstance(row,dict)]
            calls += [row.get('verifier_call') for row in value.get('review_history') or [] if isinstance(row,dict)]
            # Semantic calls are stored separately; bind them back by source hash.
            calls+=semantic_calls.get(source,[])
            for call in calls:
                if not isinstance(call,dict):continue
                ref=call.get('result_envelope_ref') or call.get('work_order_ref') or hashlib.sha256(
                    json.dumps(call,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                key=(source,ref)
                if key in seen:continue
                seen.add(key)
                db.execute("INSERT INTO publication_events(at,source_hash,tick,stage,kind,reason,cost_micros) VALUES(?,?,?,?,?,?,?)",
                           (at,source,tick,'legacy:'+str(ref),'call','legacy_receipt',int(call.get('cost_micros') or 0)))

    def admit(self, source_hash, tick, stage, *, kind, reason="admitted", prompt_bytes=0):
        now = self.clock()
        error = None
        with self._open() as db:
            rows = db.execute("SELECT e.at,e.tick,e.stage FROM publication_events e WHERE e.source_hash=? "
                              "AND e.kind=? AND e.at>? AND NOT EXISTS (SELECT 1 FROM publication_events c "
                              "WHERE c.kind='not_sent' AND c.source_hash=e.source_hash AND c.tick=e.tick "
                              "AND c.stage=e.stage AND c.id>e.id) ORDER BY e.at", (source_hash, kind, now-WINDOW_SECONDS)).fetchall()
            duplicate = any(row[1:] == (tick, stage) for row in rows)
            if duplicate and kind == "run":
                return None
            limit = MAX_RUNS if kind == "run" else MAX_CALLS
            if duplicate:
                error = PublicationParked("publication_same_tick_stage_duplicate")
            elif len(rows) >= limit:
                error = PublicationParked(f"publication_{kind}_24h_limit", rows[0][0]+WINDOW_SECONDS)
            if error:
                db.execute("INSERT INTO publication_events(at,source_hash,tick,stage,kind,reason) "
                           "VALUES(?,?,?,?,?,?)", (now,source_hash,tick,stage,"park",error.reason))
            else:
                cursor=db.execute("INSERT INTO publication_events(at,source_hash,tick,stage,kind,reason,prompt_bytes) "
                                  "VALUES(?,?,?,?,?,?,?)", (now,source_hash,tick,stage,kind,reason,prompt_bytes))
                result=cursor.lastrowid
        if error: raise error
        return result

    def record(self, source_hash, tick, stage, reason, *, cost_micros=0, kind='result'):
        with self._open() as db:
            db.execute("INSERT INTO publication_events(at,source_hash,tick,stage,kind,reason,cost_micros) "
                       "VALUES(?,?,?,?,?,?,?)", (self.clock(),source_hash,tick,stage,kind,reason,cost_micros))


@contextmanager
def publication_scope(path, source_hash, *, tick=None):
    held = _scope.get()
    if held is not None and held[1] == source_hash:
        yield held
        return
    value = (PublicationLimits(path), source_hash, tick or uuid.uuid4().hex)
    with value[0]._open(): pass
    token = _scope.set(value)
    try: yield value
    finally: _scope.reset(token)


def current_scope():
    return _scope.get()


class GovernedPublicationModel:
    """Also works with synthetic models; real fallback dispatches get slots too."""
    def __init__(self, model, *, stage_id=''): self.model = model; self.stage_id=stage_id
    def __getattr__(self, name): return getattr(self.model, name)

    def call(self, **kwargs):
        scope = _scope.get()
        if scope is None: return self.model.call(**kwargs)
        ledger, source, tick = scope
        stage = kwargs["purpose"]+":"+hashlib.sha256((self.stage_id+kwargs['prompt']).encode()).hexdigest()
        ledger.admit(source,tick,"pipeline",kind="run",reason=getattr(ledger,'run_reason','new_version'))
        ledger.admit(source,tick,stage,kind="call",prompt_bytes=len(kwargs['prompt'].encode()))
        ticket = {"stage":stage,"dispatches":0,"prompt_bytes":len(kwargs['prompt'].encode())}
        token = _ticket.set(ticket)
        try:
            result = self.model.call(**kwargs)
            if result.get('replayed') is True and not ticket['dispatches']:
                ledger.record(source,tick,stage,'scheduler_result_replayed',kind='not_sent')
            ledger.record(source,tick,stage,"replayed" if result.get("replayed") else "completed",
                          cost_micros=int(result.get("cost_micros") or 0))
            return result
        except Exception as exc:
            from .cockpit_model import CockpitModelRouteUnavailable,CockpitModel
            if isinstance(exc,CockpitModelRouteUnavailable) and not ticket['dispatches']:
                ledger.record(source,tick,stage,'route_not_sent',kind='not_sent')
            elif isinstance(self.model,CockpitModel) and not ticket['dispatches']:
                ledger.record(source,tick,stage,'no_adapter_dispatch',kind='not_sent')
            ledger.record(source,tick,stage,type(exc).__name__+":"+str(exc)[:600])
            if ticket.get('parked_exception') is not None:raise ticket['parked_exception'] from exc
            raise
        finally: _ticket.reset(token)


def reserve_dispatch():
    """Called immediately before every actual adapter execution, including fallback."""
    scope, ticket = _scope.get(), _ticket.get()
    if scope is None or ticket is None: return
    ledger, source, tick = scope
    if ticket["dispatches"]:
        try:ledger.admit(source,tick,ticket["stage"]+f":dispatch:{ticket['dispatches']+1}",kind="call")
        except PublicationParked as exc:
            ticket['parked_exception']=exc
            raise PublicationDispatchParked(str(exc)) from exc
    ticket["dispatches"] += 1


def release_dispatch():
    scope,ticket=_scope.get(),_ticket.get()
    if scope is None or ticket is None:return
    stage=ticket['stage']+(f":dispatch:{ticket['dispatches']}" if ticket['dispatches']>1 else '')
    scope[0].record(scope[1],scope[2],stage,'broker_definitely_not_sent',kind='not_sent')


def raise_if_parked(reason):
    if str(reason).startswith('publication_'):
        import re
        match=re.search(r'retry_after=([0-9.]+)',str(reason))
        raise PublicationParked(str(reason).split(';',1)[0],float(match.group(1)) if match else None)


def fee_binding():
    scope,ticket=_scope.get(),_ticket.get()
    if scope is None or ticket is None:return None
    return {'source_hash':scope[1],'rules_version':FINAL_TEXT_RULES_VERSION,
            'stage':ticket['stage'],'prompt_bytes':ticket['prompt_bytes']}


def observability(path, *, now=None):
    path=Path(path)
    if not path.exists(): return {"present":False,"limits":{"runs":MAX_RUNS,"calls":MAX_CALLS,"hours":24},"rows":[]}
    db=sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)
    try:
        rows=db.execute("SELECT source_hash,kind,reason,SUBSTR(stage,1,INSTR(stage||':',':')-1),COUNT(*),SUM(cost_micros),SUM(prompt_bytes),MAX(at) "
                        "FROM publication_events e WHERE at>? AND (kind!='call' OR NOT EXISTS "
                        "(SELECT 1 FROM publication_events c WHERE c.kind='not_sent' AND c.source_hash=e.source_hash "
                        "AND c.tick=e.tick AND c.stage=e.stage AND c.id>e.id)) GROUP BY source_hash,kind,reason,SUBSTR(stage,1,INSTR(stage||':',':')-1) "
                        "ORDER BY MAX(at) DESC", ((time.time() if now is None else now)-WINDOW_SECONDS,)).fetchall()
    finally:db.close()
    return {"present":True,"limits":{"runs":MAX_RUNS,"calls":MAX_CALLS,"hours":24},
            "rows":[dict(zip(("source_hash","kind","reason","stage","count","cost_micros","prompt_bytes","last_at"),row)) for row in rows]}


def audit_limits(path, *, now=None):
    """H's read-only lint; distinguish retained legacy overages from new dispatches."""
    from collections import defaultdict,deque
    path=Path(path)
    if not path.exists():return {'passed':True,'present':False,'violations':[],'legacy_overages':[]}
    now=time.time() if now is None else now
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        rows=db.execute("SELECT e.at,e.source_hash,e.tick,e.stage,e.kind,e.reason FROM publication_events e "
            "WHERE e.kind IN ('run','call') AND e.at>? AND NOT EXISTS (SELECT 1 FROM publication_events c "
            "WHERE c.kind='not_sent' AND c.source_hash=e.source_hash AND c.tick=e.tick AND c.stage=e.stage AND c.id>e.id) ORDER BY e.at,e.id",(now-WINDOW_SECONDS,)).fetchall()
    finally:db.close()
    windows=defaultdict(deque);seen=set();violations=[];legacy=[]
    for at,source,tick,stage,kind,reason in rows:
        queue=windows[(source,kind)]
        while queue and queue[0]<=at-WINDOW_SECONDS:queue.popleft()
        queue.append(at)
        limit=MAX_RUNS if kind=='run' else MAX_CALLS
        if len(queue)>limit:
            (legacy if reason=='legacy_receipt' else violations).append({'source_hash':source,'kind':kind,
                'code':'rolling_24h_limit','count':len(queue),'limit':limit,'at':at})
        identity=(source,tick,stage,kind)
        if identity in seen and kind=='call':violations.append({'source_hash':source,'kind':kind,'code':'same_tick_stage_duplicate','at':at})
        seen.add(identity)
    return {'passed':not violations,'present':True,'violations':violations,'legacy_overages':legacy}
