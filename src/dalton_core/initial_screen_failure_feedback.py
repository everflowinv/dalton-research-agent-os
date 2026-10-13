"""Checked, bounded initial-screen diagnostics; never evidence or retry authority."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone
from .store import canonical_json, content_hash, DaltonStore
from .initial_screen import (STRL_CONTRACT_VERSION, InitialScreenContractError,
    build_worksheet_prompt, build_document_prompt, validate_worksheet, parse_document_output,
    verify_document)
from .contracts import ResultEnvelope, WorkOrder
from .model_router import ModelRouterError
from .coverage_mission import CoverageMissionError
from .mission_deliverable import MissionDeliverableError
from .thesis_impact_budget import ThesisImpactBudgetError

PROOF_ERRORS=(OSError,ValueError,KeyError,TypeError,RuntimeError,sqlite3.Error,
              ModelRouterError,CoverageMissionError,MissionDeliverableError,ThesisImpactBudgetError)

CONTRACT = 'initial-screen-validation-feedback:0.1'
STRL_PURPOSE = 'initial_screen'
MAX_BYTES = 4 * 1024 * 1024
MAX_FINDINGS = 8
_REF = re.compile(r'^initial-screen:([0-9a-f]{24})$')

def _private_directory(path):
    info=Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('initial-screen diagnostic directory is not private')

def _read(path):
    _private_directory(Path(path).parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077 or before.st_size > MAX_BYTES:
            raise ValueError('initial-screen diagnostic file is not private/bounded')
        data = b''
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, min(1024*1024, MAX_BYTES+1-len(data)))
            if not chunk: break
            data += chunk
        after = os.fstat(fd);held = path.lstat()
        sig = lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(data) != before.st_size or sig(before)!=sig(after) or sig(after)!=sig(held):
            raise ValueError('initial-screen diagnostic file changed')
        return json.loads(data)
    finally: os.close(fd)

def _body(record):
    body = dict(record);digest = body.pop('content_hash',None)
    if digest != content_hash(body):raise ValueError('initial-screen feedback hash differs')
    return body

def _settled_call(model, call, mission, *, purpose=STRL_PURPOSE):
    """Read exact formal/route and every admission for this Work, without DDL."""
    if purpose not in {STRL_PURPOSE, 'initial_screen_verifier', 'draft'}:
        raise ValueError('initial-screen checkpoint purpose is outside its closed scope')
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    with _authority_connection(model.scheduler_db) as scheduler, recovery_readers(
            model.config["model_router_db"], model.config["budget_db"],
            now=model.clock()) as (router, budget):
        wr = scheduler.execute("SELECT * FROM scheduler_work_orders WHERE work_order_id=?",
                               (call["work_order_ref"],)).fetchone()
        fr = scheduler.execute("SELECT * FROM scheduler_formal_results WHERE work_order_id=? "
                               "ORDER BY attempt_number DESC,created_at DESC,result_record_id DESC LIMIT 1",
                               (call["work_order_ref"],)).fetchone()
        if wr is None or fr is None or budget is None:
            raise ValueError("initial-screen call has no complete formal and budget authority")
        work = WorkOrder.from_dict(json.loads(wr["work_order_json"])).to_dict()
        result = ResultEnvelope.from_dict(json.loads(fr["result_envelope_json"])).to_dict()
        formal = {k: fr[col] for k, col in (
            ("id", "result_record_id"), ("work_order_id", "work_order_id"),
            ("attempt_number", "attempt_number"), ("result_envelope_id", "result_envelope_id"),
            ("result_envelope_hash", "result_envelope_hash"), ("terminal_state", "terminal_state"),
            ("created_at", "created_at"))}
        metadata = work["metadata"]
        if (canonical_json(work) != wr["work_order_json"]
                or content_hash(work) != wr["work_order_hash"]
                or work["id"] != call["work_order_ref"]
                or metadata.get("purpose") != purpose
                or metadata.get("mission_version_ref") != mission["id"]
                or metadata.get("mission_version_hash", mission["content_hash"]) != mission["content_hash"]
                or canonical_json(result) != fr["result_envelope_json"]
                or content_hash(result) != fr["result_envelope_hash"]
                or content_hash(formal) != fr["content_hash"]
                or fr["terminal_state"] != "succeeded" or result["status"] != "succeeded"
                or result["work_order_ref"] != work["id"]
                or result["id"] != call["result_envelope_ref"]
                or result.get("invocation_ref") != call["invocation_ref"]
                or result["metadata"].get("route_decision_ref") != call["route_decision_ref"]):
            raise ValueError("initial-screen formal call binding drifted")
        route = router.get_decision(call["route_decision_ref"])
        if (route.get("outcome") != "selected" or route.get("work_order_ref") != work["id"]
                or route.get("work_order_hash") != wr["work_order_hash"]
                or route.get("attempt_number") != fr["attempt_number"]):
            raise ValueError("initial-screen route binding drifted")
        rows = budget.connection.execute(
            "SELECT attempt_number,phase FROM thesis_impact_day_admissions WHERE work_order_ref=? "
            "ORDER BY attempt_number,phase",
            (work["id"],)).fetchall()
        if not rows:
            raise ValueError("initial-screen paid call has no admission")
        settled = []
        accounted_micros = 0
        selected = False
        for row in rows:
            proof = budget.admission(work_order_ref=work["id"],
                                     attempt_number=row["attempt_number"], phase=row["phase"])
            binding = proof["mission_binding"] or {}
            if (proof["settlement"] is None
                    or binding.get("mission_version_ref") != mission["id"]
                    or binding.get("mission_version_hash") != mission["content_hash"]):
                raise ValueError("initial-screen admission is unsettled or belongs to another mission")
            admission = proof["admission"]
            # A fallback chain shares its first route's reservation. Require
            # the admission's own exact route and current attempt, not that
            # the eventual serving route was necessarily the first link.
            admitted_route = router.get_decision(admission["route_decision_ref"])
            if (admitted_route.get("work_order_ref") != work["id"]
                    or admitted_route.get("work_order_hash") != wr["work_order_hash"]
                    or admitted_route.get("attempt_number") != row["attempt_number"]):
                raise ValueError("initial-screen admission route binding drifted")
            charge = proof["settlement"]["actual_micros"]
            if not isinstance(charge, int) or isinstance(charge, bool) or charge < 0:
                raise ValueError("initial-screen settlement amount is invalid")
            accounted_micros += charge
            selected |= row["attempt_number"] == fr["attempt_number"]
            settled.append({"admission_ref": admission["admission_id"],
                            "admission_hash": admission["content_hash"],
                            "settlement_ref": proof["settlement"]["settlement_id"],
                            "settlement_hash": proof["settlement"]["content_hash"]})
        if not selected:
            raise ValueError("initial-screen selected route has no settled admission")
        return {"work_order_ref": work["id"], "work_order_hash": wr["work_order_hash"],
                "result_envelope_ref": result["id"], "formal_hash": fr["content_hash"],
                "settlements": settled, "cost_status": call.get("cost_status", "unproved"),
                "accounted_micros": accounted_micros, "mission_ref": mission["id"],
                "mission_hash": mission["content_hash"]}, work, result


def _call(model, call, mission, prompt):
    proof,work,result = _settled_call(model, call, mission)
    if work['question'] != prompt:raise ValueError('initial-screen diagnostic frozen prompt differs')
    from .cockpit_model import unwrap_json_object
    return unwrap_json_object(result['outputs']['text']),proof


def validation_prompt(prompt, context):
    return prompt+'\nFrozen validation context '+CONTRACT+' SHA256: '+validation_context_hash(context)


def validation_context_hash(context):
    return content_hash({k:context[k] for k in ('company','pack','prior_reference','source_bindings')})

def snapshot_passage_bindings(authority, pack):
    """Freeze index versions before calling; later originals must prove each used binding."""
    from .product_contract import tag_entry
    from .evidence_index import _resolve,EvidenceIndexError
    from .planner_paid_output_recovery import _authority_connection
    refs=set()
    for tag in pack.get('tags',{}):
        entry=tag_entry(pack,tag)
        if entry and entry['kind']=='passage' and entry['ref'].startswith('doc-passage:'):refs.add(entry['ref'])
    if len(refs)>128:return {}
    path=Path(authority.store.path).parent/'document-index.sqlite'
    if not refs or not path.exists():return {}
    try:
        with _authority_connection(path) as index:
            found={ref:_resolve(index,ref) for ref in sorted(refs)}
        return {ref:{k:value[k] for k in ('document_ref','document_sha256','start','end')}
                for ref,value in found.items() if value}
    except (OSError,ValueError,sqlite3.Error,EvidenceIndexError):return {}

def prepare_prompt(model, *, prompt, context, request_id, mission):
    """Keep an existing old Work byte-identical; bind only naturally new calls."""
    if not all(hasattr(model,key) for key in ('budget_for','config','scheduler_db')):
        return prompt,False
    from .cockpit_model import build_work, broker_frame_execution_binding
    from .call_budget import budget_fingerprint
    from .planner_paid_output_recovery import _authority_connection
    effective=model.budget_for(STRL_PURPOSE)
    legacy={k:getattr(model,k) for k in ('max_input_tokens','max_output_tokens','max_cost_usd')}
    legacy['timeout_seconds']=model.timeout_seconds
    explicit=(effective!=legacy or 'call_budget' in model.config or STRL_PURPOSE in model.config.get('purpose_call_budgets',{}))
    work=build_work(purpose=STRL_PURPOSE,request_id=request_id,prompt=prompt,
        mission_version_ref=mission['id'],created_at=mission.get('created_at'),
        max_input_tokens=effective['max_input_tokens'],max_output_tokens=effective['max_output_tokens'],
        max_cost_usd=effective['max_cost_usd'],max_seconds=effective['timeout_seconds'],
        budget_identity=budget_fingerprint(effective) if explicit else None,
        provider_retry=model.config.get('provider_retry'),broker_frame_policy=broker_frame_execution_binding(model.config))
    path=Path(model.scheduler_db)
    if path.exists():
        with _authority_connection(path) as connection:
            row=connection.execute('SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',(work.id,)).fetchone()
            if row:
                held=json.loads(row['work_order_json'])
                if row['work_order_json']!=canonical_json(held) or row['work_order_hash']!=content_hash(held) or held['question']!=prompt:
                    raise ValueError('initial-screen legacy Work identity differs')
                return prompt,False
    return validation_prompt(prompt,context),True

def _replay(model, mission, context, calls, authority):
    from .material_pack import _sha
    from .coverage_mission import CoverageMissionAuthority
    missions=object.__new__(CoverageMissionAuthority);missions.connection=authority.connection;missions.store=authority.store
    held=missions._mission_record(mission['id'])
    if held['content_hash'] != mission['content_hash'] or held['mission_ref'] != mission['mission_ref']:
        raise ValueError('initial-screen Core mission binding differs')
    company,pack=context['company'],context['pack']
    if company['company_ref'] not in {row['company_ref'] for row in held['universe']}:
        raise ValueError('initial-screen company is outside its frozen mission')
    if (pack.get('company_ref') != company['company_ref'] or pack.get('product')!='initial_screen'
        or pack.get('content_hash') != _sha({'recipe':pack['recipe_version'],'text':pack['text'],'tags':pack['tags']})):
        raise ValueError('initial-screen feedback frozen material identity differs')
    prior=context.get('prior_reference')
    worksheet_prompt=build_worksheet_prompt(company=company,pack=pack,prior_reference=prior)
    repairs=context.get('worksheet_repair',[])
    if context.get('worksheet_bound'):
        worksheet_prompt=validation_prompt(worksheet_prompt,context)
    if repairs:
        worksheet_prompt += '\n\nYour previous worksheet was rejected: ' + '；'.join(f"{item['code']} {item.get('detail') or ''}" for item in repairs[:20]) + '\nReturn the complete worksheet again with every finding fixed.'
    raw,worksheet_proof=_call(model,calls['worksheet'],mission,worksheet_prompt)
    try:worksheet,findings=validate_worksheet(raw,pack)
    except InitialScreenContractError as exc:worksheet,findings=None,[{'code':'worksheet_shape','detail':str(exc)}]
    proofs={'worksheet':worksheet_proof}
    if context['stage']=='worksheet':
        if not context.get('worksheet_bound'):raise ValueError('legacy worksheet lacks frozen validation binding')
        return findings,proofs
    if context['stage']!='document' or findings or worksheet is None:
        raise ValueError('document feedback lacks its accepted formal worksheet')
    prompt=build_document_prompt(company=company,pack=pack,worksheet=worksheet,prior_reference=prior,repair=context.get('document_repair',[]))
    if not context.get('document_bound'):raise ValueError('legacy document lacks frozen validation binding')
    prompt=validation_prompt(prompt,context)
    raw,proofs['document']=_call(model,calls['document'],mission,prompt)
    try:sections,findings=parse_document_output(raw,pack=pack)
    except InitialScreenContractError as exc:sections,findings=[],[{'code':'document_shape','detail':str(exc)}]
    if sections:
        live,cell,passage,source_proofs=_source_resolvers(authority,sections,pack,context['source_bindings'])
        request_reader=authority.request_resolver(company['company_ref'])
        def request(ref):
            record=request_reader(ref) if request_reader else None
            if record:source_proofs[ref]={'record_hash':record['content_hash']}
            return record
        findings += verify_document(sections,worksheet=worksheet,pack=pack,
            live_claim_refs=live,resolve_cell=cell,resolve_passage=passage,
            resolve_request=request)['findings']
        proofs['sources']=source_proofs
    return findings,proofs

def _retired_refs(connection, refs):
    # Same retirement-minus-nonwithdrawn-reinstatement rule, restricted to the
    # actual cited versions; never enumerate unrelated retirement history.
    from .claim_retirement import _table_exists
    if not refs or not _table_exists(connection,'claim_retirement_decisions'):return set()
    args=sorted(refs);marks=','.join('?' for _ in args)
    retired={row[0] for row in connection.execute("SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired' AND claim_version_ref IN ("+marks+')',args)}
    if not _table_exists(connection,'claim_retirement_reinstatements'):return retired
    clause=' AND NOT EXISTS (SELECT 1 FROM claim_retirement_reinstatement_withdrawals w WHERE w.reinstatement_ref=r.reinstatement_id)' if _table_exists(connection,'claim_retirement_reinstatement_withdrawals') else ''
    reinstated={row[0] for row in connection.execute('SELECT r.claim_version_ref FROM claim_retirement_reinstatements r WHERE r.claim_version_ref IN ('+marks+')'+clause,args)}
    return retired-reinstated

def _source_resolvers(authority, sections, pack, frozen_bindings, *, original_proof_scope='diagnostic'):
    """Resolve immutable cited versions, never current replacement pointers."""
    from .product_contract import tag_entry
    from .contracts import ClaimVersion
    proof={}
    live=set()
    refs={ref for section in sections for ref in section.get('claim_refs',[])}
    if len(refs)>64:raise ValueError('initial-screen claim references exceed diagnostic bound')
    retired=_retired_refs(authority.connection,refs)
    for ref in sorted(refs):
        row=authority.connection.execute('SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?',(ref,)).fetchone()
        if row is None:continue
        raw=json.loads(row['claim_json'])
        if raw.get('schema_version')=='0.2':
            from .research_review import validate_claim_version_v0_2
            claim=validate_claim_version_v0_2(raw)
        else:claim=ClaimVersion.from_dict(raw).to_dict()
        _body(claim)
        if claim['id']!=ref or claim['content_hash']!=row['content_hash'] or canonical_json(claim)!=row['claim_json']:
            raise ValueError('initial-screen cited claim version differs')
        proof[ref]={'record_hash':claim['content_hash'],'retired':ref in retired}
        if ref not in retired:live.add(ref)
    base_cell=authority.cell_resolver()
    def cell(value):
        ref=value.get('version_ref')
        if ref:
            table='valuation_snapshot_versions' if value.get('kind')=='valuation_metric' else 'forecast_model_versions'
            row=authority.connection.execute(f'SELECT record_json,content_hash FROM {table} WHERE version_id=?',(ref,)).fetchone()
            if row is None:return False
            record=json.loads(row['record_json']);_body(record)
            if record['content_hash']!=row['content_hash'] or record.get('id')!=ref:
                raise ValueError('initial-screen cited computed version differs')
            proof[ref]={'record_hash':row['content_hash']}
        return base_cell(value)
    used=set()
    def visit(value):
        if isinstance(value,dict):
            for item in value.values():visit(item)
        elif isinstance(value,list):
            for item in value:visit(item)
        elif isinstance(value,str) and value.startswith('doc-passage:'):used.add(value)
    visit(sections)
    if any(ref not in frozen_bindings for ref in used):raise ValueError('initial-screen source version was not frozen before the call')
    originals=_checked_originals(Path(authority.store.path).parent,used,include_render_metadata=True,
        **({} if original_proof_scope=='diagnostic' else {'proof_scope':original_proof_scope})) if used else {}
    for ref,found in originals.items():
        if {k:found[k] for k in ('document_ref','document_sha256','start','end')}!=frozen_bindings[ref]:
            raise ValueError('initial-screen original version differs from the frozen call')
    def passage(ref):
        found=originals.get(ref)
        if not found:return None
        proof[ref]={k:v for k,v in found.items() if k not in {'text','render_header'}}
        return found['text']
    # Pack text cannot bypass original-span verification by resolving first.
    for tag in pack.get('tags',{}):
        entry=tag_entry(pack,tag)
        if entry and entry['kind']=='passage' and entry['ref'] in used:
            from .product_passage_validation import rendered_passage_matches
            if not rendered_passage_matches(passage(entry['ref']),entry.get('text'),
                                            originals[entry['ref']].get('render_header')):
                raise ValueError('initial-screen frozen passage differs from its original')
    return live,cell,passage,proof

MAX_PASSAGES = 64
MAX_ORIGINALS = 16
MAX_SOURCE_BYTES = 64_000_000
SOURCE_TIMEOUT_SECONDS = 4
CHECKPOINT_MAX_PASSAGES = 128
CHECKPOINT_MAX_ORIGINALS = 128
CHECKPOINT_SOURCE_TIMEOUT_SECONDS = 12

def _original_limits(proof_scope):
    # Pause authority proves the union of several paid steps, unlike one small
    # diagnostic. Keep a fixed ceiling equal to the frozen binding contract;
    # the raw-byte budget and exact source-version checks remain unchanged.
    if proof_scope == 'diagnostic':
        return MAX_PASSAGES, MAX_ORIGINALS, SOURCE_TIMEOUT_SECONDS
    if proof_scope == 'initial_screen_checkpoint':
        return CHECKPOINT_MAX_PASSAGES, CHECKPOINT_MAX_ORIGINALS, CHECKPOINT_SOURCE_TIMEOUT_SECONDS
    raise ValueError('unknown original proof scope')

def _checked_originals(state, refs, *, include_render_metadata=False, proof_scope='diagnostic', include_source_metadata=False):
    """One bounded pure-reader process; it has no model or writable authority."""
    import subprocess
    import sys
    max_passages, _, timeout = _original_limits(proof_scope)
    if len(refs)>max_passages:raise DiagnosticUnavailable('original_passage_limit')
    code='from dalton_core.initial_screen_failure_feedback import _original_check_main;_original_check_main()'
    try:
        result=subprocess.run([sys.executable,'-c',code],input=canonical_json({'state_dir':str(state),'refs':sorted(refs),'include_render_metadata':include_render_metadata,'proof_scope':proof_scope,**({'include_source_metadata':True} if include_source_metadata else {})}),
            text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout,check=False)
    except subprocess.TimeoutExpired as exc:
        raise DiagnosticUnavailable('original_verification_timeout') from exc
    if result.returncode!=0 or len(result.stdout.encode())>MAX_BYTES:
        raise ValueError('initial-screen cited original passage is unavailable')
    found=json.loads(result.stdout)
    if isinstance(found,dict) and found.get('failure_code') in ORIGINAL_FAILURE_CODES:
        raise DiagnosticUnavailable(found['failure_code'])
    if set(found)!=set(refs):raise ValueError('initial-screen original proof coverage differs')
    return found

ORIGINAL_FAILURE_CODES=frozenset({'original_passage_limit','original_document_limit',
    'original_passage_byte_limit','original_raw_byte_limit','original_verification_timeout'})

class DiagnosticUnavailable(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)

class _BoundedOriginalSpool:
    """Read-only plain/gzip objects with a shared decompressed-byte ceiling."""
    def __init__(self,spool):
        self.roots=list(getattr(spool,'_roots',()))
        self.read_bytes=0
        self.limit_failure=None
    def _limited(self):
        self.limit_failure='original_raw_byte_limit'
        raise DiagnosticUnavailable(self.limit_failure)
    def read_object(self,digest):
        import gzip
        import hashlib
        if not re.fullmatch(r'[0-9a-f]{64}',str(digest)):raise ValueError('invalid original object hash')
        for root in self.roots:
            shard=root._objects/digest[:2]
            if shard.is_symlink():continue
            for compressed in (False,True):
                path=shard/(digest+'.gz' if compressed else digest)
                if not path.is_file() or path.is_symlink():continue
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as stream:
                    before=os.fstat(stream.fileno())
                    ceiling=min(24_000_000,MAX_SOURCE_BYTES-self.read_bytes)
                    if before.st_size>ceiling:self._limited()
                    if compressed:
                        with gzip.GzipFile(fileobj=stream) as decoded:raw=decoded.read(ceiling+1)
                    else:raw=stream.read(ceiling+1)
                    after=os.fstat(stream.fileno());held=path.stat()
                if len(raw)>ceiling:self._limited()
                signature=lambda st:(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
                if signature(before)!=signature(after) or signature(after)!=signature(held):
                    raise ValueError('original object changed during verification')
                if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('original object hash differs')
                self.read_bytes+=len(raw)
                return raw
        raise ValueError('original object is unavailable')

def _original_check_main():
    import sys
    request=json.loads(sys.stdin.read(MAX_BYTES+1))
    try:result=_original_passages(request['state_dir'],request['refs'],include_render_metadata=request.get('include_render_metadata',False),proof_scope=request.get('proof_scope','diagnostic'),include_source_metadata=request.get('include_source_metadata',False))
    except DiagnosticUnavailable as exc:result={'failure_code':exc.code}
    print(canonical_json(result))

def _original_passages(state_dir, refs, *, include_render_metadata=False, proof_scope='diagnostic', include_source_metadata=False):
    """Strong local originals, once per document, and exact frozen spans."""
    from .planner_paid_output_recovery import _authority_connection
    from .evidence_index import _resolve
    from .evidence_corpus import CorpusContext,Candidate,load_document
    state=Path(state_dir)
    max_passages, max_originals, _ = _original_limits(proof_scope)
    if len(refs)>max_passages:raise DiagnosticUnavailable('original_passage_limit')
    with _authority_connection(state/'document-index.sqlite') as index:
        spans={ref:_resolve(index,ref) for ref in refs}
        if any(not found for found in spans.values()):raise ValueError('missing initial-screen original span')
        identities={(found['document_ref'],found['document_sha256']) for found in spans.values()}
        if len(identities)>max_originals:raise DiagnosticUnavailable('original_document_limit')
        if sum(len(found['text'].encode()) for found in spans.values())>1_000_000:
            raise DiagnosticUnavailable('original_passage_byte_limit')
        candidates={}
        for identity in sorted(identities):
            row=index.execute('SELECT source_key FROM evidence_documents WHERE document_ref=? AND text_sha256=? ORDER BY doc_id LIMIT 1',identity).fetchone()
            if row is None:raise ValueError('missing initial-screen original manifest')
            key=row['source_key'];family,_,ticket=key.partition(':')
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',ticket):raise ValueError('invalid original source key')
            if family=='alphaengine':candidate=Candidate(key,'alphaengine',state/'acquisitions'/ticket/'manifest.json')
            elif re.fullmatch(r'feed-acquisitions-[a-z0-9-]+',family):candidate=Candidate(key,'feed',state/family/ticket/'manifest.json')
            elif family=='guidepoint':candidate=Candidate(key,'guidepoint',state/'guidepoint-acquire-runs'/ticket/'manifest.json')
            elif family in ('fetch','fetch-exhibit'):candidate=Candidate(key,'fetch_exhibit' if family=='fetch-exhibit' else 'fetch',state/'fetches'/ticket/('exhibit-manifest.json' if family=='fetch-exhibit' else 'manifest.json'))
            else:raise ValueError('unsupported original source family')
            if candidate.path.stat().st_size>256_000:raise ValueError('original manifest exceeds bound')
            candidates[identity]=candidate
    with _authority_connection(state/'core.sqlite') as core,CorpusContext(state,core=core) as ctx:
        ctx.spool=_BoundedOriginalSpool(ctx.spool)
        # Avoid CorpusContext's default full-history discovery enumeration.
        ctx._discovered={};ctx._discovered_by_ticket={}
        documents=sorted({ref for ref,_ in identities})
        rows=core.execute('SELECT company_ref,source_ref,document_ref,ticket_ref,discovery_ref,status,created_at FROM coverage_mission_discovered_documents WHERE document_ref IN ('+','.join('?' for _ in documents)+') LIMIT 257',documents).fetchall()
        if len(rows)>256:raise ValueError('original discovery metadata exceeds bound')
        for row in rows:
            item=dict(row);ctx._discovered.setdefault(item['document_ref'],[]).append(item)
            if item.get('ticket_ref'):ctx._discovered_by_ticket.setdefault(item['ticket_ref'],[]).append(item)
        bodies={}
        for identity,candidate in candidates.items():
            try:body=load_document(ctx,candidate)
            except Exception:
                if ctx.spool.limit_failure:raise DiagnosticUnavailable(ctx.spool.limit_failure)
                raise
            if (body.document_ref,body.text_sha256)!=identity:raise ValueError('original document version differs')
            bodies[identity]=body
        out={}
        for ref,found in spans.items():
            body=bodies[(found['document_ref'],found['document_sha256'])]
            if body.text[found['start']:found['end']]!=found['text']:raise ValueError('original passage span differs')
            out[ref]={'document_ref':found['document_ref'],'document_sha256':found['document_sha256'],
                      'passage_hash':content_hash(found['text']),'start':found['start'],'end':found['end'],'text':found['text']}
            if include_source_metadata:
                # Derived by the strong acquired-original loader, never a
                # document prefix guess or an optional mission review.
                out[ref]['source_ref']=body.source_ref
            # Current provenance title/date are not frozen acquisition proof.
            # Never certify a renderer header from those mutable metadata rows.
        return out

def _semantic(mission, context, findings):
    normalized={content_hash(row):row for row in findings}
    ordered=[normalized[k] for k in sorted(normalized)]
    body={'contract_ref':CONTRACT,'company_ref':context['company']['company_ref'],
        'mission_version_ref':mission['id'],'mission_version_hash':mission['content_hash'],
        'stage':context['stage'],'pack_hash':context['pack']['content_hash'],
        'validation_context_hash':validation_context_hash(context),
        'prior_reference_hash':content_hash(context.get('prior_reference')),'findings':ordered}
    return content_hash(body),ordered

def save_failure(*,state_dir,summary_dir,model,mission,context,calls,authority):
    """Record only an independently recomputed deterministic rejection."""
    state=Path(state_dir).resolve();directory=Path(summary_dir).absolute()
    _private_directory(directory)
    if (state/'initial-screens').is_symlink():raise ValueError('initial-screen child root is a symlink')
    directory=directory.resolve()
    if directory.parent != state/'initial-screens' or not _REF.fullmatch('initial-screen:'+directory.name):
        raise ValueError('initial-screen feedback needs its official child directory')
    findings,proofs=_replay(model,mission,context,calls,authority)
    if not findings:raise ValueError('initial-screen feedback has no deterministic rejection')
    digest,ordered=_semantic(mission,context,findings)
    body={'contract_ref':CONTRACT,'producer_contract_ref':STRL_CONTRACT_VERSION,
        'source_ticket_ref':'initial-screen:'+directory.name,'company_ref':context['company']['company_ref'],
        'mission_ref':mission['id'],'mission_hash':mission['content_hash'],'context':context,
        'calls':{stage:{k:call.get(k) for k in ('work_order_ref','route_decision_ref','result_envelope_ref','invocation_ref','cost_status')} for stage,call in calls.items()},
        'proofs':proofs,'findings':ordered,'semantic_hash':digest}
    record={**body,'content_hash':content_hash(body)}
    path=directory/'validation-feedback.json';_write(path,record,immutable=True)
    index=state/'initial-screen-validation-feedback';index.mkdir(mode=0o700,exist_ok=True)
    _private_directory(index)
    path=index/(content_hash(body['company_ref'])+'.json')
    previous=_read(path).get('tickets',[]) if path.exists() else []
    tickets=[body['source_ticket_ref'],*[x for x in previous if x!=body['source_ticket_ref']]][:2]
    _write(path,{'tickets':tickets})
    return {'status':'recorded','contract_ref':CONTRACT,'semantic_hash':digest,'receipt_hash':record['content_hash']}

def _write(path, value, *, immutable=False):
    import tempfile
    payload=(canonical_json(value)+'\n').encode()
    if len(payload)>MAX_BYTES:raise ValueError('initial-screen diagnostic receipt exceeds bound')
    if path.is_symlink():raise ValueError('initial-screen diagnostic path is a symlink')
    if immutable and path.exists():
        if _read(path)!=value:raise ValueError('initial-screen diagnostic receipt already differs')
        return
    fd,name=tempfile.mkstemp(prefix='.validation-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)

def _compact_findings(ordered):
    selected=[];codes=set()
    for row in ordered:
        if row['code'] not in codes and len(selected)<MAX_FINDINGS:
            selected.append(row);codes.add(row['code'])
    selected_hashes={content_hash(row) for row in selected}
    for row in ordered:
        if len(selected)>=MAX_FINDINGS:break
        if content_hash(row) not in selected_hashes:
            selected.append(row);selected_hashes.add(content_hash(row))
    omitted=[row for row in ordered if content_hash(row) not in selected_hashes]
    compact=[{k:str(row[k])[:180] for k in ('code','section','figure','detail') if k in row} for row in selected]
    return compact,omitted

def read_feedback(core, *,state_dir,mission,company_ref,config):
    """Exact local receipts only. Old weak summaries are explicitly unverified."""
    state=Path(state_dir).resolve();path=state/'initial-screen-validation-feedback'/(content_hash(company_ref)+'.json')
    if not path.exists():
        from .initial_screen_validator_recovery import read_feedback as current_feedback
        from .mission_deliverable import MissionDeliverableAuthority
        store=object.__new__(DaltonStore);store.connection=core;store.path=str(state/'core.sqlite')
        authority=object.__new__(MissionDeliverableAuthority);authority.connection=core;authority.store=store
        model=SimpleNamespace(config=config,scheduler_db=str(state/'scheduler.sqlite'),clock=lambda:datetime.now(timezone.utc))
        rechecked=current_feedback(authority,state=state,mission=mission,model=model,company_ref=company_ref)
        return rechecked if rechecked is not None else {'status':'not_recorded','items':[],'legacy_summary_authority':'unverified'}
    try:
        index=_read(path);tickets=index['tickets']
        if not isinstance(tickets,list) or not 1<=len(tickets)<=2:raise ValueError('initial-screen feedback index is not bounded')
        from .mission_deliverable import MissionDeliverableAuthority
        store=object.__new__(DaltonStore);store.connection=core;store.path=str(state/'core.sqlite')
        authority=object.__new__(MissionDeliverableAuthority);authority.connection=core;authority.store=store
        model=SimpleNamespace(config=config,scheduler_db=str(state/'scheduler.sqlite'),clock=lambda:datetime.now(timezone.utc))
        from .initial_screen_validator_recovery import read_feedback as current_feedback
        rechecked=current_feedback(authority,state=state,mission=mission,model=model,company_ref=company_ref)
        if rechecked is not None:return rechecked
        for ref in tickets:
            match=_REF.fullmatch(ref)
            if not match:raise ValueError('initial-screen feedback ticket identity differs')
            if (state/'initial-screens').is_symlink():raise ValueError('initial-screen child root is a symlink')
            directory=state/'initial-screens'/match[1];ticket=_read(directory/'ticket.json')
            if ticket.get('status')=='running':continue
            summary=_read(directory/'summary.json');record=_read(directory/'validation-feedback.json');_body(record)
            if (ticket.get('id')!=ref or ticket.get('status')!='failed' or ticket.get('exit_code')!=1
                or summary.get('status')!='failed' or summary.get('drafted',{}).get('company_ref')!=company_ref
                or record['contract_ref']!=CONTRACT or record['producer_contract_ref']!=STRL_CONTRACT_VERSION
                or record['source_ticket_ref']!=ref or record['company_ref']!=company_ref
                or record['context']['company']['company_ref']!=company_ref
                or record['mission_ref']!=mission['id'] or record['mission_hash']!=mission['content_hash']):
                raise ValueError('initial-screen feedback terminal authority differs')
            findings,proofs=_replay(model,mission,record['context'],record['calls'],authority)
            digest,ordered=_semantic(mission,record['context'],findings)
            if not findings or record['proofs']!=proofs or record['findings']!=ordered or record['semantic_hash']!=digest:
                raise ValueError('initial-screen feedback recomputation differs')
            latest=authority.latest('mission-deliverable:initial_screen:'+company_ref.rsplit(':',1)[-1])
            if latest:
                _body(latest)
                if latest.get('subject_ref')!=company_ref or latest.get('kind')!='initial_screen':
                    raise ValueError('initial-screen completion company differs')
            if latest and latest.get('mission_version_ref')==mission['id'] and datetime.fromisoformat(latest['created_at'])>=datetime.fromisoformat(ticket['completed_at']):
                return {'status':'cleared_by_published_product','items':[]}
            compact,omitted=_compact_findings(ordered)
            return {'status':'verified','items':[{'contract_ref':CONTRACT,'company_ref':company_ref,
                'source_ticket_ref':ref,'receipt_hash':record['content_hash'],'semantic_hash':digest,
                'stage':record['context']['stage'],'pack_hash':record['context']['pack']['content_hash'],
                'findings':compact,'full_findings_hash':content_hash(ordered),'omitted':len(omitted),
                'omitted_hash':content_hash(omitted),'financial_evidence':False,'retry_authority':False}]}
        return {'status':'pending_terminal','items':[]}
    except PROOF_ERRORS as exc:
        return {'status':'unverified','items':[],'reason':'initial_screen_failure_proof_unavailable','error_type':type(exc).__name__,
                **({'verification_limit':exc.code} if isinstance(exc,DiagnosticUnavailable) else {})}
