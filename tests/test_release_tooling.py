"""WP-B2-4/B2-6/B2-7: 发布、回收、日志轮转与迁移脚本。

这些脚本存在的原因是同一件事：发布切换过去只改 plist，而且 glob 漏掉了
com.dalton.* ，于是 2026-09-16 的机器上"当前发布"同时有三个互相矛盾的答案
（服务 37b73370 / manager 与 gate 23986f7a / 发布 worker d1f2f062）。
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_release  # noqa: E402
import gc_releases  # noqa: E402
import migrate_state_dir  # noqa: E402
import release_switch  # noqa: E402
import rotate_dalton_logs  # noqa: E402

from dalton_core.research_publication_authority import published_runtime_gate  # noqa: E402

RELEASE_A = "23986f7a8978ad573d1c2cb9c1801217cd23c3becea648533f9ed267012321d3"
RELEASE_B = "37b733707dba0c3075bad3d497b3b0159a9985fe858b5cee71303fc5ef9922ef"
LOCK = "031fdbe75e53ff9782b71625f9211df883e14148a03bc164745123a101afb02e"
COMMIT = "8f8068faf1b7c46977159052745b40ed8e0e6c6d"
MANIFEST_SHA = "07dfc9477ba6de8d2b750ccbc2438300adb167650bdb3ed7fad8fb835f4da84a"


class ReleaseHashTests(unittest.TestCase):
    """算法是从现有收据反推出来的；它必须重现真实存在的发布哈希。"""

    def test_it_reproduces_the_published_release(self):
        self.assertEqual(build_release.release_hash(
            wheel_sha256="c2bf72e6e48d819b466b6b1825e6dd2dde8db59c26b8ebd84b58a9615d51b4c3",
            dependency_lock_hash=LOCK), RELEASE_A)

    def test_it_reproduces_the_release_the_services_are_actually_on(self):
        self.assertEqual(build_release.release_hash(
            wheel_sha256="b3d55726d8fd4ce54dcc01e7128d255e45b4d239cb963c68069467de0edff637",
            dependency_lock_hash=LOCK), RELEASE_B)

    def test_a_malformed_input_is_refused(self):
        with self.assertRaises(build_release.BuildError):
            build_release.release_hash(wheel_sha256="nope", dependency_lock_hash=LOCK)

    def test_the_dependency_lock_ships_with_the_repo(self):
        lock = build_release.read_lock(build_release.DEFAULT_LOCK)
        self.assertEqual(lock["dependency_lock_hash"], LOCK)
        self.assertEqual(len(lock["dependency_wheels"]), 62)


class _Fleet(unittest.TestCase):
    """一套完整的假 fleet：两种命名的 plist、manager、workspace、指针、worker 配置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.agents = self.root / "LaunchAgents"
        self.agents.mkdir()
        self.host = self.root / ".dalton"
        self.releases = self.host / "runtime" / "releases"
        for digest in (RELEASE_A, RELEASE_B):
            binary = self.releases / digest / "venv" / "bin" / "python"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
        self.old_venv = str(self.releases / RELEASE_A / "venv")
        self.new_venv = str(self.releases / RELEASE_B / "venv")
        for label in ("space.lumos.dalton.writer", "space.lumos.dalton.controller",
                      "com.dalton.research-publication-worker"):
            path = self.agents / f"{label}.plist"
            with path.open("wb") as stream:
                plistlib.dump({"Label": label, "ProgramArguments": [
                    f"{self.old_venv}/bin/python", "-m", "dalton_core.writer_server"]},
                    stream)
        self.manager = self.host / "manager.json"
        self.manager.write_text(json.dumps({
            "release_path": self.old_venv,
            "release_ref": f"release:sha256:{RELEASE_A}",
            "shared_readonly_paths": [self.old_venv],
        }), encoding="utf-8")
        self.pointers = self.root / "pointers"
        self.pointers.mkdir()
        self.release_pointer = self.pointers / "current-release.json"
        self.runtime_pointer = self.pointers / "current-runtime-config.json"
        self.worker_config = self.root / "worker-config.json"
        self.worker_config.write_text(json.dumps({
            "publication_gate": {
                "expected_release_ref": f"release:sha256:{RELEASE_A}",
                "expected_source_commit": COMMIT,
                "release_pointer": str(self.release_pointer),
                "runtime_pointer": str(self.runtime_pointer),
            },
        }), encoding="utf-8")

    def args(self, release: str):
        import argparse
        return argparse.Namespace(
            release=self.releases / release,
            launch_agents_dir=self.agents, manager=self.manager,
            host_root=self.host, worker_config=self.worker_config,
            release_pointer=None, runtime_pointer=None,
            source_commit=COMMIT, candidate_manifest_sha256=MANIFEST_SHA)


class SwitchPlanTests(_Fleet):
    def test_both_launch_agent_naming_families_are_discovered(self):
        plan = release_switch.build_plan(self.args(RELEASE_B))
        labels = {row["label"] for row in plan["launch_agents"]}
        self.assertIn("com.dalton.research-publication-worker", labels,
                      "旧脚本的 glob 只认 space.lumos.dalton*，发布 worker 从来没被切过")
        self.assertIn("space.lumos.dalton.writer", labels)
        self.assertTrue(all(row["needs_change"] for row in plan["launch_agents"]))

    def test_the_plan_names_every_pointer_that_has_to_move_together(self):
        plan = release_switch.build_plan(self.args(RELEASE_B))
        self.assertTrue(plan["manager"]["needs_change"])
        self.assertTrue(plan["worker_config"]["needs_change"])
        self.assertEqual(plan["release_pointer"], str(self.release_pointer))
        self.assertFalse(plan["writes_performed"])

    def test_a_release_without_a_python_is_refused(self):
        args = self.args(RELEASE_B)
        (self.releases / RELEASE_B / "venv" / "bin" / "python").unlink()
        with self.assertRaises(release_switch.SwitchError):
            release_switch.build_plan(args)

    def test_a_missing_source_commit_is_refused_rather_than_guessed(self):
        args = self.args(RELEASE_B)
        args.source_commit = None
        with self.assertRaises(release_switch.SwitchError):
            release_switch.build_plan(args)

    def test_repointing_a_plist_keeps_every_other_key(self):
        path = self.agents / "com.dalton.research-publication-worker.plist"
        with path.open("rb") as stream:
            data = plistlib.load(stream)
        data["StandardErrorPath"] = "/tmp/worker.stderr.log"
        data["StartInterval"] = 300
        with path.open("wb") as stream:
            plistlib.dump(data, stream)
        self.assertTrue(release_switch.repoint_agent(path, Path(self.new_venv)))
        with path.open("rb") as stream:
            after = plistlib.load(stream)
        self.assertEqual(after["ProgramArguments"][0], f"{self.new_venv}/bin/python")
        self.assertEqual(after["StandardErrorPath"], "/tmp/worker.stderr.log")
        self.assertEqual(after["StartInterval"], 300)
        self.assertFalse(release_switch.repoint_agent(path, Path(self.new_venv)),
                         "already pointed there is not a change")


class PointerContractTests(_Fleet):
    def test_the_two_pointers_satisfy_the_gate_the_worker_actually_reads(self):
        plan = release_switch.build_plan(self.args(RELEASE_B))
        release_switch.write_pointers(
            plan, release_pointer=self.release_pointer,
            runtime_pointer=self.runtime_pointer)
        gate = published_runtime_gate({
            "release_pointer": str(self.release_pointer),
            "runtime_pointer": str(self.runtime_pointer),
            "expected_release_ref": f"release:sha256:{RELEASE_B}",
            "expected_source_commit": COMMIT,
        })
        self.assertEqual(gate["status"], "active", gate)

    def test_the_gate_stays_shut_for_the_release_that_was_replaced(self):
        plan = release_switch.build_plan(self.args(RELEASE_B))
        release_switch.write_pointers(
            plan, release_pointer=self.release_pointer,
            runtime_pointer=self.runtime_pointer)
        gate = published_runtime_gate({
            "release_pointer": str(self.release_pointer),
            "runtime_pointer": str(self.runtime_pointer),
            "expected_release_ref": f"release:sha256:{RELEASE_A}",
            "expected_source_commit": COMMIT,
        })
        self.assertEqual(gate["status"], "waiting_for_release_publication")

    def test_the_pointers_are_written_atomically_and_owner_only(self):
        plan = release_switch.build_plan(self.args(RELEASE_B))
        release_switch.write_pointers(
            plan, release_pointer=self.release_pointer,
            runtime_pointer=self.runtime_pointer)
        for path in (self.release_pointer, self.runtime_pointer):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            sorted(item.name for item in self.pointers.iterdir()),
            ["current-release.json", "current-runtime-config.json"],
            "不能留下临时文件")


class GarbageCollectionTests(_Fleet):
    def args_gc(self, keep_recent=1):
        import argparse
        return argparse.Namespace(
            releases_root=self.releases, launch_agents_dir=self.agents,
            manager=self.manager, host_root=self.host,
            pointer=[str(self.release_pointer)], keep_recent=keep_recent, apply=False)

    def test_a_release_named_by_a_launch_agent_is_never_deletable(self):
        plan = gc_releases.plan(self.args_gc(keep_recent=1))
        rows = {row["release"]: row for row in plan["releases"]}
        self.assertFalse(rows[RELEASE_A]["deletable"])
        self.assertIn("launchagent:space.lumos.dalton.writer.plist",
                      rows[RELEASE_A]["kept_because"])

    def test_a_release_named_only_by_manager_json_is_never_deletable(self):
        for path in self.agents.glob("*.plist"):
            path.unlink()
        plan = gc_releases.plan(self.args_gc(keep_recent=0 + 1))
        rows = {row["release"]: row for row in plan["releases"]}
        self.assertFalse(rows[RELEASE_A]["deletable"])
        self.assertTrue(any(item.startswith("config:")
                            for item in rows[RELEASE_A]["kept_because"]))

    def test_an_unreferenced_old_release_is_deletable_and_sized(self):
        extra = "a" * 64
        (self.releases / extra / "venv" / "bin").mkdir(parents=True)
        (self.releases / extra / "venv" / "bin" / "python").write_text("x" * 1000)
        os.utime(self.releases / extra, (0, 0))
        plan = gc_releases.plan(self.args_gc(keep_recent=1))
        rows = {row["release"]: row for row in plan["releases"]}
        self.assertTrue(rows[extra]["deletable"])
        self.assertGreaterEqual(plan["reclaimable_bytes"], 1000)
        self.assertFalse(plan["writes_performed"])


class LogRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name).resolve()

    def args(self, **overrides):
        import argparse
        values = {"log_dir": self.logs, "max_bytes": 1024, "keep": 2, "apply": True}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_rotation_truncates_in_place_so_an_open_appender_keeps_working(self):
        path = self.logs / "writer.stderr.log"
        path.write_bytes(b"old\n" * 1000)
        inode = path.stat().st_ino
        handle = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            result = rotate_dalton_logs.rotate(self.args())
            self.assertEqual(result["rotated_count"], 1)
            self.assertEqual(path.stat().st_size, 0)
            self.assertEqual(path.stat().st_ino, inode,
                             "rename 会让 launchd 持有的 fd 写进孤儿 inode")
            os.write(handle, b"after\n")
        finally:
            os.close(handle)
        self.assertEqual(path.read_bytes(), b"after\n",
                         "O_APPEND 让截断之后的写从 0 开始，不会留空洞")
        archives = list(self.logs.glob("writer.stderr.log.*.gz"))
        self.assertEqual(len(archives), 1)
        import gzip
        self.assertEqual(gzip.open(archives[0]).read(), b"old\n" * 1000)

    def test_a_small_log_is_left_alone(self):
        path = self.logs / "control.stderr.log"
        path.write_text("tiny", encoding="utf-8")
        result = rotate_dalton_logs.rotate(self.args())
        self.assertEqual(result["rotated_count"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "tiny")

    def test_dry_run_writes_nothing(self):
        path = self.logs / "writer.stderr.log"
        path.write_bytes(b"x" * 4096)
        result = rotate_dalton_logs.rotate(self.args(apply=False))
        self.assertFalse(result["writes_performed"])
        self.assertEqual(path.stat().st_size, 4096)
        self.assertEqual(list(self.logs.glob("*.gz")), [])

    def test_only_the_newest_archives_are_kept(self):
        path = self.logs / "writer.stderr.log"
        for _ in range(4):
            path.write_bytes(b"y" * 4096)
            rotate_dalton_logs.rotate(self.args())
            import time
            time.sleep(1.01)
        self.assertLessEqual(len(list(self.logs.glob("writer.stderr.log.*.gz"))), 2)


    def test_workspace_subdirectory_logs_are_rotated_too(self):
        # 过去只扫顶层 *.log：~/Library/Logs/Dalton/workspaces/<ws>/ 和
        # ~/.dalton/workspace-logs/<ws>/ 下的日志从来没有被轮转过。
        nested = self.logs / "workspaces" / "ws-abc" / "writer.stderr.log"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"z" * 4096)
        top = self.logs / "writer.stderr.log"
        top.write_bytes(b"z" * 4096)
        result = rotate_dalton_logs.rotate(self.args())
        self.assertEqual(result["rotated_count"], 2)
        self.assertEqual(nested.stat().st_size, 0)
        self.assertEqual(len(list(nested.parent.glob("writer.stderr.log.*.gz"))), 1)

    def test_extra_log_dirs_are_rotated_and_missing_ones_are_reported(self):
        other = Path(self.tmp.name) / "dalton-home" / "workspace-logs"
        (other / "ws-1").mkdir(parents=True)
        (other / "ws-1" / "control.stderr.log").write_bytes(b"q" * 4096)
        missing = Path(self.tmp.name) / "absent"
        result = rotate_dalton_logs.rotate(
            self.args(log_dir=[self.logs, other, missing]))
        self.assertEqual(result["rotated_count"], 1)
        self.assertEqual(result["missing_log_dirs"], [str(missing.resolve())])
        self.assertEqual((other / "ws-1" / "control.stderr.log").stat().st_size, 0)
        with self.assertRaises(RuntimeError):
            rotate_dalton_logs.rotate(self.args(log_dir=[missing]))

    def test_symlinks_are_never_followed_out_of_the_log_tree(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "precious.log").write_bytes(b"p" * 4096)
        logs = self.logs / "logs"
        logs.mkdir()
        (logs / "linked-dir").symlink_to(outside, target_is_directory=True)
        (logs / "linked.log").symlink_to(outside / "precious.log")
        result = rotate_dalton_logs.rotate(self.args(log_dir=[logs]))
        self.assertEqual(result["rotated_count"], 0)
        self.assertEqual((outside / "precious.log").stat().st_size, 4096)


class LogRotateLaunchAgentTemplateTests(unittest.TestCase):
    """4fa8e771 把 ProgramArguments 里的 --apply 批量替换成了 apply，
    轮转从 2026-09-17 起每天都以 "unrecognized arguments: apply" 失败。
    这里按 README 的方式渲染模板，再交给脚本自己的 argparse 解析。"""

    TEMPLATE = REPO / "deploy/macos/launchagents/com.dalton.log-rotate.plist.template"
    README = REPO / "deploy/macos/launchagents/README.md"

    def render(self):
        import re
        readme = self.README.read_text(encoding="utf-8")
        documented = set(re.findall(r"s#(@@[A-Z_]+@@)#", readme))
        text = self.TEMPLATE.read_text(encoding="utf-8")
        used = set(re.findall(r"@@[A-Z_]+@@", text))
        self.assertLessEqual(used, documented,
                             "模板里有 README 安装命令不会替换的占位符")
        values = {
            "@@RELEASE_VENV@@": "/r/venv", "@@STATE_DIR@@": "/s",
            "@@LOG_DIR@@": "/l/Logs/Dalton", "@@REPO@@": "/repo",
            "@@DALTON_HOME@@": "/h/.dalton",
        }
        for key, value in values.items():
            text = text.replace(key, value)
        self.assertNotIn("@@", text)
        return plistlib.loads(text.encode("utf-8"))

    def test_rendered_arguments_parse_with_the_scripts_own_parser(self):
        arguments = self.render()["ProgramArguments"]
        self.assertEqual(arguments[0], "/r/venv/bin/python")
        self.assertEqual(arguments[1], "/repo/scripts/rotate_dalton_logs.py")
        parser = rotate_dalton_logs.build_parser()
        # argparse exits (SystemExit 2) on an unknown argument such as "apply".
        parsed = parser.parse_args(arguments[2:])
        self.assertTrue(parsed.apply, "模板必须真正轮转，而不是每天 dry-run")
        self.assertEqual(parsed.log_dir, [Path("/l/Logs/Dalton"),
                                          Path("/h/.dalton/workspace-logs")])
        self.assertEqual(parsed.keep, 5)
        self.assertEqual(parsed.max_bytes, 16 * 1024 * 1024)

    def test_the_old_bare_apply_is_rejected(self):
        parser = rotate_dalton_logs.build_parser()
        with self.assertRaises(SystemExit), \
                open(os.devnull, "w") as sink, \
                unittest.mock.patch("sys.stderr", sink):
            parser.parse_args(["--log-dir", "/l", "apply"])


class MigrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.legacy = self.root / "Dalton" / "state"
        (self.legacy / "dalton-core").mkdir(parents=True)
        (self.legacy / "dalton-core" / "core.sqlite").write_bytes(b"x" * 2048)
        self.workspaces = self.root / ".dalton" / "workspaces"
        (self.workspaces / "ws-a" / "state" / "dalton-core").mkdir(parents=True)
        self.agents = self.root / "LaunchAgents"
        self.agents.mkdir()

    def args(self, dest, **overrides):
        import argparse
        values = {
            "dest": Path(dest), "legacy_state": self.legacy,
            "workspaces": self.workspaces,
            "releases": self.root / ".dalton" / "runtime" / "releases",
            "backup_root": None, "include_releases": False,
            "launch_agents_dir": self.agents,
            "writer_socket": self.legacy / "dalton-core" / "run" / "writer.sock",
            "free_space_multiple": 2.0, "stop_settle_seconds": 0.0,
            "start_settle_seconds": 0.0, "heartbeat_wait_seconds": 0.0,
            "rollback": False, "apply": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_the_workspace_symlink_is_the_whole_directory_not_each_state(self):
        plan = migrate_state_dir.plan(self.args(self.root / "dest"))
        rows = {row["name"]: row for row in plan["units"]}
        self.assertEqual(rows["workspaces"]["source"], str(self.workspaces))
        self.assertIn("escapes workspace_root", rows["workspaces"]["symlink_reason"])
        self.assertIn("legacy", rows)

    def test_releases_are_left_behind_unless_asked_for(self):
        (self.root / ".dalton" / "runtime" / "releases").mkdir(parents=True)
        plan = migrate_state_dir.plan(self.args(self.root / "dest"))
        self.assertNotIn("releases", {row["name"] for row in plan["units"]})
        plan = migrate_state_dir.plan(
            self.args(self.root / "dest", include_releases=True))
        self.assertIn("releases", {row["name"] for row in plan["units"]})

    def test_insufficient_space_blocks_rather_than_starting(self):
        plan = migrate_state_dir.plan(
            self.args(self.root / "dest", free_space_multiple=1e12))
        self.assertTrue(plan["blocking"])
        self.assertFalse(plan["writes_performed"])

    def test_the_plan_lists_every_sqlite_that_must_be_checkpointed(self):
        plan = migrate_state_dir.plan(self.args(self.root / "dest"))
        rows = {row["name"]: row for row in plan["units"]}
        self.assertIn(str(self.legacy / "dalton-core" / "core.sqlite"),
                      rows["legacy"]["sqlite_files"])

    def test_a_missing_destination_volume_is_refused(self):
        with self.assertRaises((migrate_state_dir.MigrationError, OSError)):
            migrate_state_dir.plan(self.args("/Volumes/definitely-not-mounted/x"))

    def test_the_symlink_decision_is_written_down(self):
        plan = migrate_state_dir.plan(self.args(self.root / "dest"))
        self.assertIn("软链", plan["symlink_versus_config_rewrite"])
        self.assertTrue(plan["rollback"])


if __name__ == "__main__":
    unittest.main()
