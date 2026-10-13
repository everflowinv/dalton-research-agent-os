"""Persistent controller priority changes only the exact Python module role."""
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_release_tooling import release_switch as switch


class ControllerPriorityTests(unittest.TestCase):
    def test_legacy_and_workspace_migrate_even_when_python_already_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / 'release' / 'venv'
            for label in ('space.lumos.dalton.controller',
                          'space.lumos.dalton.workspace.synthetic.controller'):
                with self.subTest(label=label):
                    path = root / (label + '.plist')
                    before = {'Label': label, 'ProcessType': 'Background',
                              'ProgramArguments': [str(venv / 'bin' / 'python'), '-m',
                                                   'dalton_core.service', '--config', '/private/config.json'],
                              'EnvironmentVariables': {'UNCHANGED': '1'}, 'KeepAlive': True,
                              'ThrottleInterval': 10, 'StandardErrorPath': '/private/controller.log'}
                    path.write_bytes(plistlib.dumps(before));path.chmod(0o640)
                    self.assertTrue(switch.repoint_agent(path, venv))
                    self.assertEqual(plistlib.loads(path.read_bytes()), before | {'ProcessType': 'Standard'})
                    self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                    landed = path.read_bytes()
                    self.assertFalse(switch.repoint_agent(path, venv))
                    self.assertEqual(path.read_bytes(), landed)

    def test_other_modules_and_service_name_in_an_argument_keep_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);venv = root / 'venv'
            for module in ('dalton_core.writer_server', 'dalton_core.control_server',
                           'dalton_core.research_publication_worker', 'dalton_core.service_extra'):
                with self.subTest(module=module):
                    path = root / 'other.plist'
                    before = {'Label': 'space.lumos.dalton.controller', 'ProcessType': 'Background',
                              'ProgramArguments': ['/old/bin/python', '-m', module,
                                                   '--config', 'dalton_core.service']}
                    path.write_bytes(plistlib.dumps(before))
                    self.assertTrue(switch.repoint_agent(path, venv))
                    expected = dict(before)
                    expected['ProgramArguments'] = [str(venv / 'bin' / 'python'), *before['ProgramArguments'][1:]]
                    self.assertEqual(plistlib.loads(path.read_bytes()), expected)
                    landed = path.read_bytes()
                    self.assertFalse(switch.repoint_agent(path, venv))
                    self.assertEqual(path.read_bytes(), landed)

    def test_controller_repoint_sets_priority_and_full_readback_refuses_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);venv = root / 'venv';path = root / 'controller.plist'
            before = {'Label': 'controller', 'ProgramArguments': ['/old/bin/python', '-m',
                       'dalton_core.service'], 'KeepAlive': True}
            path.write_bytes(plistlib.dumps(before))
            replace = os.replace
            def drift(source, target):
                replace(source, target)
                changed = plistlib.loads(Path(target).read_bytes())
                changed['ProcessType'] = 'Background'
                Path(target).write_bytes(plistlib.dumps(changed))
            with mock.patch.object(switch.os, 'replace', side_effect=drift):
                with self.assertRaisesRegex(switch.SwitchError, '回读结果与写入不一致'):
                    switch.repoint_agent(path, venv)
            path.write_bytes(plistlib.dumps(before))
            self.assertTrue(switch.repoint_agent(path, venv))
            self.assertEqual(plistlib.loads(path.read_bytes()), before | {
                'ProcessType': 'Standard', 'ProgramArguments': [str(venv / 'bin' / 'python'),
                                                             '-m', 'dalton_core.service']})


if __name__ == '__main__':
    unittest.main()
