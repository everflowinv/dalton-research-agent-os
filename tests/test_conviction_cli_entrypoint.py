"""Execute the actual module entrypoint against an isolated governed Core."""
import json
import runpy
import sys
import unittest
from unittest.mock import patch

class ConvictionModuleEntrypointTests(unittest.TestCase):
    def test_current_consensus_reaches_comparison_helper_before_any_model_call(self):
        from tests.test_conviction_call_lane import ConvictionHarness, ACN
        from dalton_core.conviction_call import WRITE_SCOPE, CHECKPOINT_KIND
        fx = ConvictionHarness()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.grant(WRITE_SCOPE, checkpoints=(CHECKPOINT_KIND,))
        output = fx.state_dir / 'module-entrypoint'
        # Exercise the live branch that formerly looked up the helper while
        # __main__ had not defined it. The helper itself is not patched.
        held = {'metrics': [{'metric': 'revenue', 'unit': 'usd', 'refs': []}]}
        argv = ['conviction_call_cli', '--state-dir', str(fx.state_dir), '--company-ref', ACN,
                '--summary-dir', str(output), '--dry-run', '--quiet']
        with patch.object(sys, 'argv', argv), \
             patch('dalton_core.consensus_estimate.latest_consensus', return_value=held) as reader, \
             patch('dalton_core.cockpit_model.CockpitModel', side_effect=AssertionError('dry run must not call models')):
            with self.assertRaises(SystemExit) as exit:
                runpy.run_module('dalton_core.conviction_call_cli', run_name='__main__')
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(reader.call_count, 1)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertIn(summary['status'], {'succeeded', 'held', 'idle'})
        self.assertFalse(summary['model_called'])
        self.assertNotIn('NameError', str(summary.get('failure_reason')))
