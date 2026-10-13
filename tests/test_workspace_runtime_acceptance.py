import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.run_workspace_runtime_acceptance import (
    run_acceptance, _wait_initial_screen, _stop, _fixture_popen, _live_fixture_group)
import time


class WorkspaceRuntimeAcceptanceTests(unittest.TestCase):
    def test_discovery_success_still_waits_for_formal_stage_entry(self):
        mission = {"mission_ref": "mission:fixture", "id": "mission-version:fixture"}
        waiting = {"mission_version_ref": mission["id"], "companies": []}
        entered = {"mission_version_ref": mission["id"], "companies": [{
            "current_stage": "initial_screen", "current_status": "entered", "record_count": 1}]}
        authority = SimpleNamespace(mission_progress=Mock(side_effect=[waiting, entered]))
        with patch("scripts.run_workspace_runtime_acceptance.time.sleep"), patch(
            "scripts.run_workspace_runtime_acceptance.time.monotonic", return_value=0):
            self.assertEqual(_wait_initial_screen(authority, mission), entered)
        self.assertEqual(authority.mission_progress.call_args_list,
                         [unittest.mock.call(mission["mission_ref"])] * 2)

    def test_stage_wait_is_bounded_and_wrong_mission_is_not_a_success(self):
        mission = {"mission_ref": "mission:fixture", "id": "mission-version:fixture"}
        authority = SimpleNamespace(mission_progress=Mock(return_value={
            "mission_version_ref": mission["id"], "companies": []}))
        with patch("scripts.run_workspace_runtime_acceptance.time.sleep"), patch(
            "scripts.run_workspace_runtime_acceptance.time.monotonic", side_effect=[0, 0, 21]):
            with self.assertRaisesRegex(RuntimeError, "initial_screen/entered"):
                _wait_initial_screen(authority, mission)
        authority.mission_progress.return_value = {"mission_version_ref": "mission-version:foreign"}
        with self.assertRaisesRegex(RuntimeError, "workspace mission authority differs"):
            _wait_initial_screen(authority, mission)

    def test_failed_fixture_process_preserves_exact_exit_and_bounded_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            process=subprocess.Popen([sys.executable,'-c',"import sys; print('synthetic stdout'); print('synthetic stderr',file=sys.stderr); raise SystemExit(9)"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            process.wait(timeout=5)
            path=Path(directory)/'diagnostic.json'
            _stop(process,diagnostics=path)
            diagnostic=json.loads(path.read_text())
            self.assertEqual(diagnostic['pid'],process.pid)
            self.assertEqual(diagnostic['returncode'],9)
            self.assertIn('synthetic stdout',diagnostic['stdout_tail'])
            self.assertIn('synthetic stderr',diagnostic['stderr_tail'])

    @unittest.skipUnless(os.name=='posix','fixture process groups require POSIX')
    def test_stopped_parent_cannot_leave_state_writing_children_or_stop_another_fixture(self):
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);pidfile=root/'child.pid';state=root/'state';target=state/'heartbeat'
            child=("import time; from pathlib import Path; p=Path("+repr(str(target))+");\n"
                   "while True:\n p.parent.mkdir(exist_ok=True); p.write_text(str(time.monotonic())); time.sleep(.01)")
            parent=("import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',"
                    +repr(child)+"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); Path("
                    +repr(str(pidfile))+").write_text(str(p.pid)); time.sleep(30)")
            process=_fixture_popen([sys.executable,'-c',parent],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            other=_fixture_popen([sys.executable,'-c','import time;time.sleep(30)'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+5
                while not pidfile.exists()or not target.exists():
                    self.assertLess(time.monotonic(),deadline);time.sleep(.01)
                self.assertEqual(os.getpgid(int(pidfile.read_text())),process.pid)
                _stop(process)
                self.assertEqual(_live_fixture_group(process.pid),[])
                self.assertIsNone(other.poll())
                shutil.rmtree(state);time.sleep(.05)
                self.assertFalse(state.exists())
                _stop(process)  # Already-reaped parents remain safe/idempotent.
            finally:
                _stop(process);_stop(other)

    @unittest.skipUnless(os.name=='posix','fixture process groups require POSIX')
    def test_initial_controller_start_failure_still_reaps_started_writer(self):
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);state=root/'state';state.mkdir()
            writer=_fixture_popen([sys.executable,'-c','import time;time.sleep(30)'],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            workspace=SimpleNamespace(state_dir=state,slug='alpha')
            try:
                with patch('scripts.run_workspace_runtime_acceptance._create',return_value=(workspace,{}, {},{})), \
                     patch('scripts.run_workspace_runtime_acceptance._start_writer',return_value=writer), \
                     patch('scripts.run_workspace_runtime_acceptance._start_controller',side_effect=RuntimeError('controller setup failure')):
                    with self.assertRaisesRegex(RuntimeError,'controller setup failure'):
                        run_acceptance(root/'receipt')
                self.assertIsNotNone(writer.poll());self.assertEqual(_live_fixture_group(writer.pid),[])
            finally:
                _stop(writer)

    def test_two_real_engines_keep_processing_and_refuse_cross_namespace_inputs(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            output = Path(directory) / "receipt"
            try:
                result = run_acceptance(output)
            except Exception as exc:
                files={p.name:p.read_text(encoding='utf-8')[-8192:]
                       for p in sorted(output.glob('*.json')) if p.is_file()}
                destination=os.environ.get('DALTON_TEST_DIAGNOSTICS_DIR')
                if destination and output.is_dir():
                    shutil.copytree(output,Path(destination)/'workspace-runtime-acceptance',dirs_exist_ok=True)
                raise AssertionError(f'{exc}; hermetic process diagnostics={json.dumps(files)}') from exc
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["external_provider_calls"], 0)
            self.assertGreater(result["continuous_processing"]["a_ticks_after_b"],
                               result["continuous_processing"]["a_ticks_before_b"])
            self.assertTrue(result["continuous_processing"]["a_pid_unchanged"])
            self.assertEqual({item["company_ref"] for item in result["workspaces"]},
                             {"company:ticker:same"})
            self.assertGreater(result["engine"]["a_research_tick"]["lane_count"], 0)
            self.assertGreater(result["engine"]["b_research_tick"]["lane_count"], 0)
            self.assertEqual(
                result["engine"]["a_research_tick"]["mission_stage"]["status"], "entered")
            self.assertEqual(
                result["engine"]["b_research_tick"]["mission_stage"]["status"], "entered")
            self.assertEqual(
                result["engine"]["a_mission_progress"]["companies"][0]["current_stage"],
                "initial_screen")
            self.assertEqual(
                result["engine"]["b_mission_progress"]["companies"][0]["current_stage"],
                "initial_screen")
            for key in ("a_source_discovery", "b_source_discovery"):
                self.assertGreater(result["engine"][key]["source_discovery_records"], 0)
                self.assertGreater(result["engine"][key]["discovered_document_records"], 0)
                self.assertTrue(result["engine"][key]["succeeded_ticket_refs"])
                self.assertEqual(result["engine"][key]["selected_mission_ref"],
                                 result["workspaces"][0 if key.startswith("a_") else 1]["mission_ref"])
            self.assertTrue(all(item["refused"] for item in result["isolation"].values()))
            stored = json.loads((output / "receipt.json").read_text())
            asserted = stored.pop("content_hash")
            from dalton_core.store import content_hash
            self.assertEqual(asserted, content_hash(stored))


if __name__ == "__main__":
    unittest.main()
