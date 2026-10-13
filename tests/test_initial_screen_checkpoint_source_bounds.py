"""Full paid-step citation unions retain bounded, exact original proof."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from dalton_core import initial_screen_failure_feedback as feedback
from dalton_core import initial_screen_quiesce as quiesce
from dalton_core import evidence_index
from dalton_core.material_pack import _sha
from tests import test_evidence_index as corpus_fixture
from tests import test_initial_screen_quiesce as pause_fixture


class CheckpointSourceBoundsTests(unittest.TestCase):
    setUp = pause_fixture.InitialScreenQuiesceTests.setUp
    new_ticket = pause_fixture.InitialScreenQuiesceTests.new_ticket
    proxy = pause_fixture.InitialScreenQuiesceTests.proxy
    request = pause_fixture.InitialScreenQuiesceTests.request
    call = pause_fixture.InitialScreenQuiesceTests.call

    def originals(self):
        state = self.root / 'originals'; state.mkdir()
        builder = corpus_fixture.CorpusBuilder(state)
        self.addCleanup(builder.close)
        entries, bindings = {}, {}
        for number in range(1, 18):
            ticket = f'proof{number}'
            document = builder.alphaengine(ticket, f'{100000 + number}',
                corpus_fixture.TRANSCRIPT + f'\nOriginal document number {number}.',
                spec='earnings-call-transcripts', title=f'Synthetic source {number}',
                broker=None, published='2026-10-01', named=['Synthetic Widgets'])
            builder.db.commit()
            evidence_index.index_acquired_manifest(state, state/'acquisitions'/ticket/'manifest.json')
            span = evidence_index.passages_for_document(state, document)[0]
            ref = span['passage_ref']
            entries[f'P{number}'] = {'ref': ref, 'text': span['text']}
            bindings[ref] = {key: span[key] for key in ('document_ref', 'document_sha256', 'start', 'end')}
        return state, entries, bindings

    def context_for(self, entries, bindings):
        context = copy.deepcopy(self.context)
        context['pack']['tags'] = entries
        context['pack']['text'] = '\n'.join(f'[{tag}]\n{row["text"]}' for tag,row in entries.items())
        context['pack']['content_hash'] = _sha({'recipe':context['pack']['recipe_version'],
            'text':context['pack']['text'], 'tags':context['pack']['tags']})
        context['source_bindings'] = bindings
        return context

    def test_seventeen_documents_strongly_pause_and_replay_both_paid_steps_without_new_call(self):
        state, entries, bindings = self.originals()
        with self.assertRaisesRegex(ValueError, 'original_document_limit'):
            feedback._checked_originals(state, set(bindings))
        originals = feedback._checked_originals(state, set(bindings),
            proof_scope='initial_screen_checkpoint')
        self.assertEqual(set(originals), set(bindings))
        self.assertEqual(len({v['document_ref'] for v in originals.values()}), 17)
        context = self.context_for(entries, bindings)
        self.adapter.value = {'key_passages': list(entries)}
        store = SimpleNamespace(path=state/'core.sqlite', connection=self.fixture.store.connection)
        with mock.patch.object(self.fixture.authority, 'store', store):
            proxy = self.proxy(context=context)
            first = self.call(proxy)
            second = self.call(proxy, name='document')
            self.request()
            count = len(self.adapter.served)
            with self.assertRaises(quiesce.ScreenQuiesced) as caught:
                self.call(proxy, name='verify', purpose='initial_screen_verifier')
            checkpoint = caught.exception.checkpoint
            self.assertEqual(len(checkpoint['completed']), 2)
            self.assertEqual(len(checkpoint['source_proofs']), 17)
            self.assertEqual(len(self.adapter.served), count)
            summary = quiesce.paused_summary({}, caught.exception)
            feedback._write(self.path/'summary.json', summary)
            feedback._write(self.path/'ticket.json', {**self.ticket, 'status':'quiesced',
                'exit_code':0, 'completed_at':'2026-10-11T17:01:00+00:00'})
            successor = self.path.with_name('b'*24)
            self.new_ticket(successor, resumed_from=self.ticket['id'])
            resumed = self.proxy(directory=successor, context=context, resume=self.path)
            for name, expected in (('worksheet', first), ('document', second)):
                answer = self.call(resumed, name=name)
                self.assertTrue(answer['replayed'])
                self.assertEqual(answer['cost_micros'], 0)
                self.assertEqual(answer['work_order_ref'], expected['work_order_ref'])
            self.assertEqual(len(self.adapter.served), count)
            # A changed original manifest never becomes a free resume proof.
            manifest = state/'acquisitions/proof17/manifest.json'
            wire = json.loads(manifest.read_text()); wire['document_ref'] = 'alphaengine-doc:foreign'
            manifest.write_text(json.dumps(wire))
            with self.assertRaises((*feedback.PROOF_ERRORS, quiesce.ScreenQuiesceUnproved)):
                self.proxy(directory=successor, context=context, resume=self.path)
            self.assertEqual(len(self.adapter.served), count)

    def test_checkpoint_bounds_and_unknown_scope_refuse_before_original_process(self):
        with mock.patch('subprocess.run') as worker:
            with self.assertRaisesRegex(ValueError, 'original_passage_limit'):
                feedback._checked_originals(self.root, {f'doc-passage:{n}' for n in range(129)},
                    proof_scope='initial_screen_checkpoint')
            with self.assertRaisesRegex(ValueError, 'unknown original proof scope'):
                feedback._checked_originals(self.root, set(), proof_scope='unbounded')
        worker.assert_not_called()

    def test_complete_context_exists_before_any_paid_step_and_tampering_cannot_send(self):
        proxy = self.proxy()
        path = self.path/'frozen-context.json'
        original = self.adapter.execute
        def execute(*args):
            value = feedback._read(path); feedback._body(value)
            self.assertEqual(value['context'], self.context)
            self.assertEqual(value['scope'], proxy.scope)
            self.assertEqual(value['ticket_binding']['id'], self.ticket['id'])
            return original(*args)
        with mock.patch.object(self.adapter, 'execute', side_effect=execute):
            self.call(proxy)
            self.call(proxy, name='document')
        before = len(self.adapter.served)
        held = feedback._read(path); held['context']['pack']['text'] += 'foreign'
        held['content_hash'] = feedback.content_hash({k:v for k,v in held.items() if k!='content_hash'})
        feedback._write(path, held)
        with self.assertRaises(quiesce.ScreenQuiesceUnproved): self.call(proxy, name='verify')
        self.assertEqual(len(self.adapter.served), before)

    def test_frozen_context_is_only_a_hint_and_does_not_authorize_resume(self):
        self.proxy()
        self.assertTrue((self.path/'frozen-context.json').exists())
        successor = self.path.with_name('b'*24)
        self.new_ticket(successor, resumed_from=self.ticket['id'])
        before = len(self.adapter.served)
        # No quiesced summary / strong step-checkpoint exists.
        with self.assertRaises((*feedback.PROOF_ERRORS, quiesce.ScreenQuiesceUnproved)):
            self.proxy(directory=successor, resume=self.path)
        self.assertEqual(len(self.adapter.served), before)

    def test_old_strong_checkpoint_without_context_hint_still_replays_for_free(self):
        # Existing checkpoint schema and exact paid proof do not change.
        pause_fixture.InitialScreenQuiesceTests.pause(self)
        (self.path/'frozen-context.json').unlink()
        successor = self.path.with_name('b'*24)
        self.new_ticket(successor, resumed_from=self.ticket['id'])
        count = len(self.adapter.served)
        resumed = self.proxy(directory=successor, resume=self.path)
        answer = self.call(resumed)
        self.assertTrue(answer['replayed'])
        self.assertEqual(answer['cost_micros'], 0)
        self.assertEqual(len(self.adapter.served), count)

    def test_oversized_complete_context_fails_before_saving_or_paying(self):
        context = copy.deepcopy(self.context)
        context['pack']['text'] = 'x' * (feedback.MAX_BYTES + 1)
        context['pack']['content_hash'] = _sha({'recipe':context['pack']['recipe_version'],
            'text':context['pack']['text'], 'tags':context['pack']['tags']})
        count = len(self.adapter.served)
        with self.assertRaisesRegex(quiesce.ScreenQuiesceUnproved, 'byte bound'):
            self.proxy(context=context)
        self.assertFalse((self.path/'frozen-context.json').exists())
        self.assertEqual(len(self.adapter.served), count)

    def test_checkpoint_timeout_child_is_reaped_and_limits_do_not_change_diagnostics(self):
        import subprocess, sys
        real_run = subprocess.run; pids = []
        def sleeping(*a, **kw):
            try:
                return real_run([sys.executable,'-c',
                    'import os,time;print(os.getpid(),flush=True);time.sleep(5)'], **kw)
            except subprocess.TimeoutExpired as exc:
                pids.append(int(exc.stdout)); raise
        with mock.patch.object(feedback, 'CHECKPOINT_SOURCE_TIMEOUT_SECONDS', 0.2), \
                mock.patch('subprocess.run', side_effect=sleeping):
            with self.assertRaisesRegex(ValueError, 'original_verification_timeout'):
                feedback._checked_originals(self.root, {'doc-passage:one'},
                    proof_scope='initial_screen_checkpoint')
        with self.assertRaises(ProcessLookupError): os.kill(pids[0], 0)
        self.assertEqual(feedback._original_limits('diagnostic'), (64,16,4))
        self.assertEqual(feedback._original_limits('initial_screen_checkpoint'), (128,128,12))

    def test_raw_budget_and_tampered_pack_or_frozen_version_still_refuse(self):
        state, entries, bindings = self.originals()
        with mock.patch.object(feedback, 'MAX_SOURCE_BYTES', 32):
            with self.assertRaisesRegex(ValueError, 'original_raw_byte_limit'):
                feedback._original_passages(state, list(bindings), proof_scope='initial_screen_checkpoint')
        context = self.context_for(entries, bindings)
        self.adapter.value = {'key_passages': list(entries)}
        store = SimpleNamespace(path=state/'core.sqlite', connection=self.fixture.store.connection)
        with mock.patch.object(self.fixture.authority, 'store', store):
            proxy = self.proxy(context=context); self.call(proxy)
            self.request(); count = len(self.adapter.served)
            for wrong in ('frozen_version', 'pack'):
                damaged = copy.deepcopy(context)
                if wrong == 'frozen_version': damaged['source_bindings'][next(iter(bindings))]['document_sha256'] = 'a'*64
                else: damaged['pack']['text'] += 'changed'
                proxy.context = damaged
                with self.assertRaises(quiesce.ScreenQuiesceUnproved): self.call(proxy, name='next')
                self.assertEqual(len(self.adapter.served), count)
