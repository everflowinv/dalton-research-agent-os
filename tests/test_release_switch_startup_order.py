"""All writer sockets precede dependent bootstraps; no external services used."""
import json
import plistlib
import socket
import unittest
from unittest.mock import patch
from tests import test_release_tooling as tooling

switch = tooling.release_switch


class StartupOrderTests(unittest.TestCase):
    def setUp(self):
        f = tooling.SwitchPlanTests('test_both_launch_agent_naming_families_are_discovered')
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.args = f.args(tooling.RELEASE_B)
        self.args.backup_dir = f.root / 'backups'
        self.args.settle_seconds = 0
        self.args.writer_ready_seconds = 0
        self.events = []
        self.listeners = []
        self.writer_labels = ['space.lumos.dalton.writer', 'space.lumos.dalton.workspace.synthetic.writer']
        self.sockets = {}
        for i, label in enumerate(self.writer_labels):
            path = f.agents / (label + '.plist')
            endpoint = f.root / ('writer' + str(i) + '.sock')
            self.sockets[label] = endpoint
            path.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': [f.old_venv + '/bin/python', '-m', 'dalton_core.writer_server', '--socket', str(endpoint)]}))
        control = 'space.lumos.dalton.control'
        (f.agents / (control + '.plist')).write_bytes(plistlib.dumps({'Label': control, 'ProgramArguments': [f.old_venv + '/bin/python', '-m', 'dalton_core.control_server']}))
        self.plan = switch.build_plan(self.args)
        self.addCleanup(self.close_sockets)

    def close_sockets(self):
        for listener in self.listeners:
            listener.close()

    def start_socket(self, label):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.sockets[label]))
        listener.listen(16)
        self.listeners.append(listener)

    def launch(self, action, label, path):
        self.events.append((action, label))
        if action == 'start' and label in self.sockets:
            self.start_socket(label)
        if action == 'start' and label not in self.sockets:
            self.assertTrue(all(switch.writer_socket_readable(p) for p in self.sockets.values()))
        return 'launchctl ' + action + ' -> 0'

    def apply(self, launch=None):
        with patch.object(switch, 'launchctl', side_effect=launch or self.launch), patch.object(switch.time, 'sleep'):
            return switch.apply_switch(self.args, self.plan)

    def test_both_actual_writer_sockets_accept_before_control_controller_and_publication(self):
        result = self.apply()
        starts = [label for action, label in self.events if action == 'start']
        self.assertEqual(set(starts[:2]), set(self.writer_labels))
        self.assertEqual(starts[2:], ['space.lumos.dalton.control', 'space.lumos.dalton.controller', 'com.dalton.research-publication-worker'])
        self.assertEqual({r['socket'] for r in result['writer_readiness']}, {str(p) for p in self.sockets.values()})
        self.assertTrue(all(r['readable'] for r in result['writer_readiness']))

    def test_one_unready_writer_prevents_every_dependent_even_if_global_socket_ready(self):
        def launch(action, label, path):
            self.events.append((action, label))
            if action == 'start' and label == self.writer_labels[0]:
                self.start_socket(label)
            return 'launchctl -> 0'
        with self.assertRaisesRegex(switch.SwitchError, 'writer sockets did not become ready'):
            self.apply(launch)
        self.assertEqual({label for action, label in self.events if action == 'start'}, set(self.writer_labels))

    def test_plain_existing_file_is_not_socket_readiness(self):
        path = self.f.root / 'plain-file.sock'
        path.touch()
        self.assertFalse(switch.writer_socket_readable(path))

    def test_writer_bootstrap_retry_still_precedes_dependents(self):
        attempts = {}
        def launch(action, label, path):
            self.events.append((action, label))
            if action == 'start':
                attempts[label] = attempts.get(label, 0) + 1
                if label == self.writer_labels[0] and attempts[label] == 1:
                    return 'launchctl -> 5 bootstrap failed'
                if label in self.sockets:
                    self.start_socket(label)
                else:
                    self.assertTrue(all(switch.writer_socket_readable(p) for p in self.sockets.values()))
            return 'launchctl -> 0'
        result = self.apply(launch)
        self.assertEqual(attempts[self.writer_labels[0]], 2)
        self.assertTrue(any(line.startswith('retry ') for line in result['actions']))
        first_control = self.events.index(('start', 'space.lumos.dalton.control'))
        self.assertLess(max(i for i, e in enumerate(self.events) if e == ('start', self.writer_labels[0])), first_control)

    def test_failed_writer_retry_never_bootstraps_dependents(self):
        def launch(action, label, path):
            self.events.append((action, label))
            return 'launchctl -> 5 unavailable' if action == 'start' and label in self.writer_labels else 'launchctl -> 0'
        with self.assertRaisesRegex(switch.SwitchError, 'bootstrap retry failed'):
            self.apply(launch)
        self.assertTrue(all(label in self.writer_labels for action, label in self.events if action == 'start'))

    def test_socket_validation_happens_before_repoint_or_pointer_writes(self):
        path = self.f.agents / (self.writer_labels[0] + '.plist')
        wire = plistlib.loads(path.read_bytes())
        wire['ProgramArguments'] = wire['ProgramArguments'][:-2]
        path.write_bytes(plistlib.dumps(wire))
        manager = self.f.manager.read_bytes()
        with self.assertRaisesRegex(switch.SwitchError, 'absolute --socket'):
            self.apply()
        self.assertEqual(self.f.manager.read_bytes(), manager)
        self.assertFalse(self.args.backup_dir.exists())
        self.assertEqual(self.events, [])

    def test_unbounded_or_nan_ready_timeout_refuses_before_writes(self):
        for value in [float('nan'), float('inf'), 601, -1]:
            self.args.writer_ready_seconds = value
            with self.assertRaisesRegex(switch.SwitchError, 'readiness timeout'):
                self.apply()
        self.assertFalse(self.args.backup_dir.exists())
        self.assertEqual(self.events, [])

    def test_null_heartbeat_is_not_success_even_when_all_config_pointers_match(self):
        self.apply()
        self.args.state_dir = self.f.root / 'state'
        self.args.writer_socket = self.sockets[self.writer_labels[0]]
        self.args.heartbeat_wait_seconds = 0
        with patch.object(switch, 'controller_heartbeat', return_value=None), patch.object(switch.time, 'sleep'):
            result = switch.health(self.args, self.plan)
        self.assertIsNone(result['controller_heartbeat_advanced'])
        self.assertFalse(result['ok'])

    def test_actual_heartbeat_shape_last_tick_advances_while_projection_stays_old(self):
        self.apply()
        self.args.state_dir = self.f.root / 'state'
        self.args.writer_socket = self.sockets[self.writer_labels[0]]
        self.args.heartbeat_wait_seconds = 0
        path = self.args.state_dir / 'run' / 'heartbeat.json'
        path.parent.mkdir(parents=True)
        heartbeat = {'last_tick_at': '2026-10-10T16:00:00+00:00',
                     'last_projection_at': '2026-10-10T15:59:00+00:00', 'pid': 1}
        path.write_text(json.dumps(heartbeat))
        self.assertEqual(switch.controller_heartbeat(self.args.state_dir), heartbeat['last_tick_at'])
        def complete_tick(seconds):
            heartbeat['last_tick_at'] = '2026-10-10T16:00:01+00:00'
            path.write_text(json.dumps(heartbeat))
        with patch.object(switch.time, 'sleep', side_effect=complete_tick):
            result = switch.health(self.args, self.plan)
        self.assertTrue(result['controller_heartbeat_advanced'])
        self.assertTrue(result['ok'])

    def test_writer_wait_rechecks_both_and_does_not_accept_only_one_earlier_connection(self):
        rows = [(label, self.f.agents / (label + '.plist'), p) for label, p in self.sockets.items()]
        probes = iter([True, False, False, True, True, True])
        self.args.writer_ready_seconds = 1
        with patch.object(switch, 'writer_socket_readable', side_effect=lambda p: next(probes)), patch.object(switch.time, 'sleep'):
            result = switch.wait_writer_sockets(rows, 1)
        self.assertTrue(all(row['readable'] for row in result))


if __name__ == '__main__':
    unittest.main()
