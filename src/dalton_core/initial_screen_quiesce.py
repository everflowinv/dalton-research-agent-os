"""Deployment pause at settled screen steps; never retry or publish authority.

Parsed prose is not a checkpoint authority. Resume revalidates exact paid
Work/formal/route/all-settlement records and regenerates deterministic checks.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from .store import canonical_json, content_hash
from .initial_screen_failure_feedback import (
    _read, _write, _body, _settled_call, _source_resolvers, PROOF_ERRORS,
)

SCHEMA = 'initial-screen-quiesce:0.1'
MAX_CALLS = 32
MAX_RESUMES = 64
FIELDS = ('id', 'pid', 'started_at', 'command', 'model_config_path', 'model_config_fingerprint')
IDENTITY = ('work_order_ref', 'result_envelope_ref', 'route_decision_ref', 'invocation_ref',
            'cost_status', 'cost_micros')


class ScreenQuiesced(BaseException):
    def __init__(self, request, checkpoint):
        self.request, self.checkpoint = request, checkpoint
        super().__init__('initial screen paused at a settled step boundary')


class ScreenQuiesceUnproved(BaseException):
    """Never turn unknown accounting or mismatched input into resume authority."""


def _binding(ticket):
    if not re.fullmatch(r'initial-screen:[0-9a-f]{24}', str(ticket.get('id'))):
        raise ValueError('initial-screen ticket identity is invalid')
    return {key: ticket[key] for key in FIELDS}


def request_quiesce(run_dir: Path, *, expected_ticket_hash: str):
    from .lane_child_launcher import ticket_identity
    directory = Path(run_dir)
    ticket = _read(directory / 'ticket.json')
    if (ticket.get('status') != 'running' or content_hash(ticket) != expected_ticket_hash
            or ticket_identity(ticket, directory / 'ticket.json') is not True):
        raise ValueError('initial-screen ticket changed or live process identity is unproved')
    body = {'schema_version': SCHEMA, 'ticket_binding': _binding(ticket),
            'ticket_hash_at_request': expected_ticket_hash}
    record = {**body, 'content_hash': content_hash(body)}
    _write(directory / 'quiesce-request.json', record, immutable=True)
    return record


def _config_hash(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError('initial-screen model configuration is not a regular file')
    return hashlib.sha256(target.read_bytes()).hexdigest()


def quiesced_summary_matches(summary, ticket):
    if not isinstance(summary, dict) or summary.get('status') != 'quiesced':
        return False
    try:
        proof = summary['quiesce']; _body(proof)
        original = {**ticket, 'status': 'running', 'completed_at': None, 'exit_code': None}
        for key in ('summary', 'adopted_from_summary', 'orphaned_reason'):
            original.pop(key, None)
        return (set(proof) == {'schema_version', 'ticket_binding', 'ticket_hash_at_request', 'content_hash'}
                and proof['schema_version'] == SCHEMA
                and proof['ticket_binding'] == _binding(original)
                and proof['ticket_hash_at_request'] == content_hash(original)
                and summary['resumable'] is True and summary['stop_reason'] == 'cooperative_quiesce'
                and isinstance(summary['checkpoint_hash'], str))
    except (KeyError, ValueError, TypeError):
        return False


def _scope(authority, mission, context):
    from .coverage_mission import CoverageMissionAuthority
    from .mission_deliverable import WRITE_SCOPE
    missions = object.__new__(CoverageMissionAuthority)
    missions.store, missions.connection = authority.store, authority.connection
    held = missions._mission_record(mission['id'])
    if context['mode'] == 'strl':
        from .material_pack import _sha
        pack = context['pack']
        if (pack.get('company_ref') != context['company']['company_ref']
                or pack.get('product') != 'initial_screen'
                or pack.get('content_hash') != _sha({'recipe':pack['recipe_version'],
                    'text':pack['text'],'tags':pack['tags']})):
            raise ValueError('initial-screen frozen material identity differs')
    if (held['content_hash'] != mission['content_hash']
            or context['company']['company_ref'] not in {v['company_ref'] for v in held['universe']}
            or WRITE_SCOPE not in held['autonomy']['may_write']):
        raise ValueError('initial-screen checkpoint mission, company or write scope differs')
    prior = authority.latest('mission-deliverable:initial_screen:'+context['company']['company_ref'].rsplit(':',1)[-1])
    if prior is not None: _body(prior)
    expected_prior = (context.get('prior_reference') or {}).get('version_ref')
    if expected_prior != (prior or {}).get('id'):
        raise ValueError('initial-screen prior product changed')
    return {'mission_ref': mission['id'], 'mission_hash': mission['content_hash'],
            'prior_product_hash': (prior or {}).get('content_hash'),
            'context_hash': content_hash(context)}


def _sources(authority, context, results):
    """Prove only actually cited originals, using the bounded pure reader."""
    from .product_contract import tag_entry
    pack = context.get('pack')
    if not pack:  # Legacy context has exact Claim versions; no material-pack passages.
        refs = context.get('claim_refs', [])
        _, cell, _, proofs = _source_resolvers(authority,
            [{'claim_refs': refs, 'source_refs': []}], {}, {})
        for number in context.get('claim_context', {}).get('numbers', []):
            if number.get('cell') and not cell(number['cell']):
                raise ValueError('legacy initial-screen computed citation is unavailable')
        if any(ref not in proofs for ref in refs):
            raise ValueError('legacy initial-screen claim is unavailable')
        return proofs
    # Before the first paid call there is no formal prompt binding. Prove the
    # whole frozen source set rather than authenticate input from a hash alone.
    tags = set(pack.get('tags', {})) if not results else set()
    direct_refs = set()
    def visit(value):
        if isinstance(value, dict):
            for item in value.values(): visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, str):
            tags.update(tag for tag in re.findall(r'\b[PNCO][0-9]+\b', value) if tag in pack.get('tags', {}))
            if value.startswith(('doc-passage:', 'claim-version:')): direct_refs.add(value)
    for result in results:
        text = result.get('outputs', {}).get('text', '')
        try:
            from .cockpit_model import unwrap_json_object
            visit(unwrap_json_object(text))
        except (ValueError, RuntimeError, TypeError):
            # A malformed paid answer still has durable accounting; it must be
            # deterministically rejected on resume, not purchased again.
            visit(text)
    cells = []
    for tag in sorted(tags):
        entry = tag_entry(pack, tag)
        if entry:
            direct_refs.add(entry['ref'])
            if entry.get('cell'): cells.append(entry['cell'])
    section = {'claim_refs': sorted(ref for ref in direct_refs if ref.startswith('claim-version:')),
               'source_refs': sorted(ref for ref in direct_refs if ref.startswith('doc-passage:'))}
    _, cell, _, proofs = _source_resolvers(authority, [section], pack, context['source_bindings'])
    for value in cells:
        if not cell(value): raise ValueError('initial-screen computed citation is unavailable')
    return proofs


class RunQuiesce:
    """Transparent call proxy; no new Work identity and no change to call limits."""
    def __init__(self, model, directory, *, context, mission, authority, resume_directory=None):
        self.model, self.directory = model, Path(directory)
        self.context, self.mission, self.authority = context, mission, authority
        self.ticket = _read(self.directory / 'ticket.json')
        self.binding = _binding(self.ticket)
        if self.ticket['pid'] != os.getpid() or self.ticket['status'] != 'running':
            raise ScreenQuiesceUnproved('initial-screen checkpoint does not bind this child')
        if _config_hash(self.binding['model_config_path']) != self.binding['model_config_fingerprint']:
            raise ScreenQuiesceUnproved('initial-screen loaded configuration changed')
        purposes = ('draft',) if context['mode'] == 'legacy' else ('initial_screen', 'initial_screen_verifier')
        self.budgets = {purpose: model.budget_for(purpose) for purpose in purposes}
        self.configurations = self._configuration()
        self.scope = _scope(authority, mission, context)
        self.records, self.saved = [], []
        if resume_directory is not None:
            parent = Path(resume_directory)
            parent_ticket = _read(parent / 'ticket.json'); summary = _read(parent / 'summary.json')
            if (not quiesced_summary_matches(summary, parent_ticket)
                    or self.ticket.get('resumed_from') != parent_ticket['id']):
                raise ScreenQuiesceUnproved('initial-screen parent pause is not proved')
            checkpoint = _read(parent / 'step-checkpoint.json'); _body(checkpoint)
            if (checkpoint['content_hash'] != summary['checkpoint_hash']
                    or checkpoint['scope'] != self.scope or checkpoint['budgets'] != self.budgets
                    or checkpoint['model_config_fingerprint'] != self.binding['model_config_fingerprint']
                    or checkpoint['configurations'] != self.configurations
                    or checkpoint['schema_version'] != SCHEMA):
                raise ScreenQuiesceUnproved('initial-screen paused input or configuration differs')
            self.saved = checkpoint['calls']
            if len(self.saved) > MAX_CALLS: raise ScreenQuiesceUnproved('initial-screen checkpoint exceeds call bound')
            self._prove(self.saved, checkpoint)

    def _configuration(self):
        from .host_research_reconcile import resolved_model_config
        return {purpose: content_hash(resolved_model_config(
            Path(self.model.scheduler_db).parent, self.model.config, purpose))
            for purpose in self.budgets}

    def __getattr__(self, name):
        return getattr(self.model, name)

    def _prove(self, records, expected=None):
        results, completed = [], []
        if _scope(self.authority, self.mission, self.context) != self.scope:
            raise ValueError('initial-screen mission changed')
        if _config_hash(self.binding['model_config_path']) != self.binding['model_config_fingerprint']:
            raise ValueError('initial-screen loaded configuration changed')
        if self._configuration() != self.configurations:
            raise ValueError('initial-screen resolved routing or budget authority changed')
        if any(self.model.budget_for(p) != b for p, b in self.budgets.items()):
            raise ValueError('initial-screen purpose budget changed')
        for record in records:
            key, call = record['key'], record['call']
            if call.get('cost_status') not in {'actual','estimated','replayed'}:
                raise ValueError('initial-screen call accounting is unknown')
            proof, work, result = _settled_call(self.model, call, self.mission, purpose=key['purpose'])
            request = work['metadata']['request_id']
            if (hashlib.sha256(work['question'].encode()).hexdigest() != key['prompt_sha256']
                    or not (request == key['request_id'] or request.startswith(key['request_id'] + ':'))
                    or tuple(work['metadata'].get('producer_route_decision_refs') or ()) != tuple(key['producer_routes'])):
                raise ValueError('initial-screen checkpoint differs from frozen call input')
            completed.append(proof); results.append(result)
        sources = _sources(self.authority, self.context, results)
        checkpoint = {'schema_version': SCHEMA, 'scope': self.scope, 'budgets': self.budgets,
                      'model_config_fingerprint': self.binding['model_config_fingerprint'],
                      'context': self.context, 'configurations': self.configurations, 'calls': records, 'completed': completed, 'source_proofs': sources}
        checkpoint['content_hash'] = content_hash(checkpoint)
        if expected is not None and checkpoint != expected:
            raise ValueError('initial-screen checkpoint strong proof differs')
        return checkpoint

    def checkpoint(self):
        path = self.directory / 'quiesce-request.json'
        if not path.exists(): return
        try:
            request = _read(path); _body(request)
            ticket = _read(self.directory / 'ticket.json')
            if (request['schema_version'] != SCHEMA or request['ticket_binding'] != self.binding
                    or request['ticket_hash_at_request'] != content_hash(ticket)
                    or ticket != self.ticket):
                raise ValueError('initial-screen pause request differs from this exact child')
            proof = self._prove(self.records)
            _write(self.directory / 'step-checkpoint.json', proof)
        except PROOF_ERRORS as exc:
            raise ScreenQuiesceUnproved(str(exc)) from exc
        raise ScreenQuiesced(request, proof)

    def call(self, *, producer_route_decision_refs=(), **kwargs):
        self.checkpoint()  # Previous response's deterministic validation has now completed.
        if kwargs['mission']['id'] != self.mission['id'] or kwargs['mission']['content_hash'] != self.mission['content_hash']:
            raise ScreenQuiesceUnproved('initial-screen call belongs to another mission')
        if kwargs['purpose'] not in self.budgets:
            raise ScreenQuiesceUnproved('initial-screen purpose is outside this contract')
        key = {'purpose': kwargs['purpose'], 'request_id': kwargs['request_id'],
               'prompt_sha256': hashlib.sha256(kwargs['prompt'].encode()).hexdigest(),
               'producer_routes': sorted(set(producer_route_decision_refs))}
        if self.saved:
            expected = self.saved[len(self.records)] if len(self.records) < len(self.saved) else None
            if expected is not None:
                if expected['key'] != key: raise ScreenQuiesceUnproved('initial-screen replay step or prompt differs')
                _, _, result = _settled_call(self.model, expected['call'], self.mission, purpose=key['purpose'])
                self.records.append(expected)
                return {**expected['call'], 'text': result['outputs']['text'], 'cost_micros': 0,
                        'replayed': True}
        if len(self.records) >= MAX_CALLS: raise ScreenQuiesceUnproved('initial-screen step bound exceeded')
        supplied = dict(kwargs)
        if producer_route_decision_refs:
            from .cockpit_model import independent_model_call
            answer = independent_model_call(self.model, producer_route_decision_refs=producer_route_decision_refs, **supplied)
        else: answer = self.model.call(**supplied)
        self.records.append({'key': key, 'call': {k: answer.get(k) for k in IDENTITY}})
        return answer


def wrap(model, args, *, context, mission, authority):
    """Old standalone calls/tickets keep their exact behavior without new metadata."""
    directory = getattr(args, 'summary_dir', None)
    if directory is None or not (Path(directory) / 'ticket.json').exists(): return model
    ticket = _read(Path(directory) / 'ticket.json')
    if 'model_config_fingerprint' not in ticket: return model
    resume = None
    if ticket.get('resumed_from'):
        ref = ticket['resumed_from']
        if not re.fullmatch(r'initial-screen:[0-9a-f]{24}', str(ref)):
            raise ScreenQuiesceUnproved('invalid initial-screen predecessor')
        resume = Path(directory).parent / ref.split(':', 1)[1]
    try:
        return RunQuiesce(model, directory, context=context, mission=mission,
                          authority=authority, resume_directory=resume)
    except PROOF_ERRORS as exc:
        raise ScreenQuiesceUnproved(str(exc)) from exc


def resume_context(args):
    """Untrusted frozen input hint. RunQuiesce proves it before any call."""
    directory = getattr(args, 'summary_dir', None)
    if directory is None: return None
    path = Path(directory) / 'ticket.json'
    if os.environ.get('DALTON_INITIAL_SCREEN_TICKET'):
        import time
        for _ in range(100):
            if path.exists() and _read(path).get('pid') == os.getpid(): break
            time.sleep(0.05)
        else: raise ScreenQuiesceUnproved('launcher did not publish this child ticket')
    if not path.exists(): return None
    ticket = _read(path)
    if not ticket.get('resumed_from'): return None
    ref = ticket['resumed_from']
    if not re.fullmatch(r'initial-screen:[0-9a-f]{24}', str(ref)):
        raise ScreenQuiesceUnproved('invalid initial-screen predecessor')
    parent = path.parent.parent / ref.split(':', 1)[1]
    try:
        parent_ticket = _read(parent / 'ticket.json'); summary = _read(parent / 'summary.json')
        checkpoint = _read(parent / 'step-checkpoint.json'); _body(checkpoint)
        if (not quiesced_summary_matches(summary, parent_ticket)
                or summary['checkpoint_hash'] != checkpoint['content_hash']):
            raise ValueError('initial-screen predecessor pause changed')
        return checkpoint['context']
    except PROOF_ERRORS as exc:
        raise ScreenQuiesceUnproved(str(exc)) from exc


def paused_summary(summary, exc):
    """Control boundary only: no deliverable, gate, or failure feedback published."""
    if isinstance(exc, ScreenQuiesced):
        return {**summary, 'status': 'quiesced', 'resumable': True,
                'stop_reason': 'cooperative_quiesce', 'quiesce': exc.request,
                'checkpoint_hash': exc.checkpoint['content_hash'],
                'completed_steps': exc.checkpoint['completed'],
                'formal_authority_writes': 0, 'gate': None}
    return {**summary, 'status': 'held', 'resumable': False,
            'reason_code': 'initial_screen_quiesce_unproved',
            'failure_reason': str(exc), 'formal_authority_writes': 0, 'gate': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--expected-ticket-hash', required=True)
    args = parser.parse_args(argv)
    print(canonical_json(request_quiesce(args.run_dir, expected_ticket_hash=args.expected_ticket_hash)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
