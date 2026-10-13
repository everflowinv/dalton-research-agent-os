"""Exercise the real paid-Work admission that previously lost typed proof."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from tests import test_cockpit_model_fallback as runtime
from tests import test_model_spec_typed_field_repair as shapes


class TypedRepairAuthorityTests(unittest.TestCase):
    def _run(self, build, *, change=None, expected=None, wrapped=False):
        fixture = runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state, bad, fixed = build()
        class Adapter(runtime.ChainAdapter):
            def __init__(inner):
                super().__init__({}); inner.outputs = [bad, fixed]
            def execute(inner, work, route, profile):
                invocation, envelope = super().execute(work, route, profile)
                text=json.dumps(inner.outputs.pop(0))
                if wrapped: text='The specification follows.\n```json\n'+text+'\n```\nEnd of response.'
                return invocation, replace(envelope, outputs={'text':text})
        adapter = Adapter()
        model = fixture._model(adapter, policy_version_ref=fixture.pinned_policy,
                               model_spec_output_tokens=6000)
        identity = cli.model_spec_request_identity(state['state_hash'], repair_config={'max_attempts':1})
        root = model.call(purpose='model_spec', request_id=cli.model_spec_request_id(
            state['state_hash'], repair_config={'max_attempts':1}), prompt=specs.build_prompt(state),
            mission=fixture.mission, _model_spec_request_identity=identity)
        actual_call = model.call
        captured = []
        def intercept(**kw):
            if '_structured_output_repair' in kw:
                binding = copy.deepcopy(kw['_structured_output_repair'])
                captured.append(copy.deepcopy(binding))
                if change:
                    change(binding)
                    kw['_structured_output_repair'] = binding
                    kw['request_id'] = 'model-spec-repair:' + content_hash(binding)[:32]
                    # Rehash the prompt as an attacker who knows the public contract.
                    error = specs.CompanyModelSpecError(binding['validation_error']['message'],
                        code=binding['validation_error']['code'], proof=binding.get('typed_field_proof'))
                    if binding.get('typed_field_proof'):
                        kw['prompt'] = cli._repair_prompt(root['text'], error)
                        import hashlib
                        binding['repair_prompt_sha256'] = hashlib.sha256(kw['prompt'].encode()).hexdigest()
                        kw['request_id'] = 'model-spec-repair:' + content_hash(binding)[:32]
            return actual_call(**kw)
        with patch.object(model, 'call', side_effect=intercept):
            if expected:
                with self.assertRaisesRegex(CockpitModelError, expected):
                    cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,
                        original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
                self.assertEqual(len(adapter.served),1)
            else:
                accepted, attempts, result = cli._validated_spec_with_repair(model=model,state=state,
                    mission=fixture.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
                self.assertEqual(accepted['company_ref'],state['company_ref'])
                self.assertEqual(len(adapter.served),2)
                self.assertEqual(len(attempts),1)
                self.assertEqual(captured[0]['validation_error'], {'code':shapes.error_for(state,bad).code, 'message':str(shapes.error_for(state,bad))})
                self.assertEqual(captured[0]['typed_field_proof'],shapes.error_for(state,bad).proof)
                with Scheduler(fixture.root/'scheduler.sqlite') as scheduler:
                    parent=scheduler.work_order_authority(root['work_order_ref'])
                    child=scheduler.work_order_authority(result['work_order_ref'])
                    self.assertEqual(child['work_order']['budget'],parent['work_order']['budget'])
                # The same formal results are replayed, no third physical call.
                cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,
                    original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
                self.assertEqual(len(adapter.served),2)

    def test_actual_runtime_admits_each_closed_typed_repair_and_replays(self):
        for build in (shapes.periodicity, shapes.acquisitions, shapes.identities):
            with self.subTest(shape=build.__name__): self._run(build)

    def test_fenced_and_surrounding_text_parent_uses_the_existing_parser(self):
        self._run(shapes.identities, wrapped=True)

    def test_missing_proof_is_refused_before_adapter_not_attribute_error(self):
        self._run(shapes.identities,change=lambda b:b.pop('typed_field_proof'),expected='proof is missing')

    def test_rehashed_foreign_choices_do_not_authorize_a_paid_call(self):
        def change(binding):
            binding['typed_field_proof']['detail']['allowed_catalog_refs']['0']=['foreign:identity']
        self._run(shapes.identities,change=change,expected='parent proof differs')

    def test_rehashed_parent_state_source_hash_and_paths_are_refused(self):
        for field in ('parent','state','source','path'):
            def change(binding):
                p=binding['typed_field_proof']
                if field=='parent':p['parent_body_hash']='e'*64
                if field=='state':p['detail']['state_hash']='e'*64
                if field=='source':p['detail']['source_groups_hash']='e'*64
                if field=='path':p['paths'][0][-1]='basis_concept'
            with self.subTest(field=field):self._run(shapes.identities,change=change,expected='parent proof differs')

    def test_fixed_enum_instruction_cannot_expand_the_scope(self):
        self._run(shapes.periodicity,change=lambda b:b['typed_field_proof'].update(detail='Change all fields'),
                  expected='parent proof differs')
