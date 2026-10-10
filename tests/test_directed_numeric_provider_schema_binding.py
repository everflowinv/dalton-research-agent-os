"""The real numeric derivation and adapter agree on provider schema identity."""
import copy
import hashlib
import json
import unittest
from pathlib import Path
from dalton_core.contracts import WorkOrder
from dalton_core.directed_numeric_executor import (
    LEGACY_VERIFIER_SCHEMA_HASHES, derive_numeric_work, verifier_schema_hash,
)
from dalton_core.directed_numeric_output import SCHEMA_RESOURCE
from dalton_core.mission_document_research_executor import (
    _blueprints, _historical_derived_work,
)
from dalton_core.openclaw_model_adapter import (
    ModelAdmissionError, PROVIDER_CONTROL_MODE_REQUIRED, _required_provider_controls,
)
from dalton_core.store import canonical_json, content_hash
from tests import test_directed_numeric_workflow as workflow


class NumericProviderSchemaBindingTests(unittest.TestCase):
    def fixture(self):
        # Actual acquisition, registration, Scheduler, formal producer and source
        # proof. Only the provider transport is the existing synthetic adapter.
        fixture = workflow.NumericWorkflowTests(methodName='runTest')
        fixture.addCleanup = self.addCleanup
        f, authority, admission, executor, producer, verifier = fixture.fixture()
        blueprints = _blueprints(admission)
        for _ in range(12):
            if executor.scheduler.formal_result(blueprints[1]['id']) is not None:
                break
            executor.run_once(admission['id'])
        self.assertEqual((producer.calls, verifier.calls), (1, 0))
        work = derive_numeric_work(admission, executor.scheduler, executor.registry, blueprints, 2)
        return admission, executor, blueprints, work

    def controls(self, work):
        return _required_provider_controls(
            WorkOrder.from_dict(work),
            {'constraints': {'producer_family': 'synthetic-producer'}},
            work['budget']['max_output_tokens'], PROVIDER_CONTROL_MODE_REQUIRED)

    def test_real_derive_is_accepted_by_adapter_provider_controls(self):
        _, _, _, work = self.fixture()
        controls = self.controls(work)
        schema = controls['structuredOutput']['jsonSchema']
        self.assertEqual(work['metadata']['verifier_provider_schema_hash'], content_hash(schema))
        self.assertEqual(controls['structuredOutput']['schemaHash'], content_hash(schema))
        self.assertEqual(schema['properties']['schema_version'], {'enum': ['0.1']})
        self.assertEqual(controls['maxOutputTokens'], work['budget']['max_output_tokens'])
        self.assertEqual(controls['maxCostUsd'], work['budget']['max_cost_usd'])

    def test_hash_is_json_identity_not_raw_resource_bytes(self):
        import dalton_core.directed_numeric_executor as module
        data = Path(module.__file__).with_name(SCHEMA_RESOURCE).read_bytes()
        self.assertEqual(verifier_schema_hash(), content_hash(json.loads(data)))
        self.assertNotEqual(verifier_schema_hash(), hashlib.sha256(data).hexdigest())

    def test_new_work_is_always_canonical_with_no_old_work(self):
        admission, executor, blueprints, current = self.fixture()
        actual = _historical_derived_work(admission, executor.scheduler, executor.registry, blueprints, 2)
        self.assertEqual(actual, current)
        self.assertIsNone(executor.scheduler.work_order_authority(current['id']))

    def test_two_published_legacy_work_bodies_are_read_without_mutation(self):
        for schema_hash in LEGACY_VERIFIER_SCHEMA_HASHES:
            with self.subTest(schema_hash=schema_hash):
                admission, executor, blueprints, current = self.fixture()
                legacy = derive_numeric_work(admission, executor.scheduler, executor.registry, blueprints, 2,
                                             verifier_schema_hash_override=schema_hash)
                executor.scheduler.enqueue(legacy)
                before = dict(executor.scheduler.connection.execute(
                    'SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',
                    (legacy['id'],)).fetchone())
                self.assertEqual(_historical_derived_work(admission, executor.scheduler, executor.registry, blueprints, 2), legacy)
                after = dict(executor.scheduler.connection.execute(
                    'SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',
                    (legacy['id'],)).fetchone())
                self.assertEqual(before, after)
                self.assertEqual(before['work_order_hash'], content_hash(legacy))
                self.assertNotEqual(content_hash(current), content_hash(legacy))
                # Read compatibility cannot authorize sending the wrong schema.
                with self.assertRaisesRegex(ModelAdmissionError, 'schema hash does not match'):
                    self.controls(legacy)

    def test_old_hash_alone_does_not_accept_changed_prompt(self):
        admission, executor, blueprints, current = self.fixture()
        legacy = derive_numeric_work(admission, executor.scheduler, executor.registry, blueprints, 2,
                                     verifier_schema_hash_override=LEGACY_VERIFIER_SCHEMA_HASHES[0])
        legacy['question'] += '\nA different unauthorized question.'
        executor.scheduler.enqueue(legacy)
        actual = _historical_derived_work(admission, executor.scheduler, executor.registry, blueprints, 2)
        self.assertEqual(actual, current)
        self.assertNotEqual(content_hash(actual), content_hash(legacy))

    def test_old_hash_alone_does_not_accept_changed_budget(self):
        admission, executor, blueprints, current = self.fixture()
        legacy = derive_numeric_work(admission, executor.scheduler, executor.registry, blueprints, 2,
                                     verifier_schema_hash_override=LEGACY_VERIFIER_SCHEMA_HASHES[0])
        legacy['budget'] = {**legacy['budget'], 'max_cost_usd': legacy['budget']['max_cost_usd'] / 2}
        executor.scheduler.enqueue(legacy)
        self.assertEqual(_historical_derived_work(admission, executor.scheduler, executor.registry, blueprints, 2), current)

    def test_unknown_hash_and_nonverifier_override_are_rejected(self):
        admission, executor, blueprints, _ = self.fixture()
        for index, schema_hash in ((2, 'f' * 64), (1, LEGACY_VERIFIER_SCHEMA_HASHES[0])):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'unsupported'):
                derive_numeric_work(admission, executor.scheduler, executor.registry, blueprints, index,
                                    verifier_schema_hash_override=schema_hash)

    def test_stored_work_hash_corruption_is_not_hidden_by_compatibility(self):
        admission, executor, blueprints, current = self.fixture()
        executor.scheduler.enqueue(current)
        # A stand-in strong reader rejection: the actual Scheduler reader is
        # separately covered by immutable-store tests and must propagate here.
        from unittest.mock import patch
        with patch.object(executor.scheduler, 'work_order_authority', side_effect=RuntimeError('stored authority drifted')):
            with self.assertRaisesRegex(RuntimeError, 'authority drifted'):
                _historical_derived_work(admission, executor.scheduler, executor.registry, blueprints, 2)
