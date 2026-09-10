from __future__ import annotations
import shutil, tempfile, unittest
from pathlib import Path
from dalton_core.install_disk_preflight import BUILD_COPIES, METADATA_FLOOR_BYTES, check_space, required_bytes

class DiskPreflightTests(unittest.TestCase):
    def test_installer_checks_space_before_its_first_mutation(self):
        install = (Path(__file__).parents[1] / "deploy/macos/install.sh").read_text()
        check = install.index('install_disk_preflight.py')
        self.assertLess(check, install.index('mkdir -p "$config_dir"'))
        self.assertLess(check, install.index('stop_job "$label"'))

    def test_requirement_is_derived_from_preserved_and_transient_bytes(self):
        self.assertEqual(required_bytes(source=10, runtime=20, databases=30, reserve=7),
                         10 * BUILD_COPIES + 20 + 30 + 7)
        self.assertEqual(required_bytes(source=1, runtime=2, databases=3),
                         BUILD_COPIES + 2 + 3 + METADATA_FLOOR_BYTES)

    def test_insufficient_space_fails_without_creating_the_target(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); repo = root / "repo"; repo.mkdir()
            (repo / "source").write_bytes(b"source"); target = root / "absent" / "Dalton"
            with self.assertRaisesRegex(RuntimeError, "need .* have 1"):
                check_space(repo_root=repo, dalton_root=target, reserve=0,
                            usage=lambda _: shutil._ntuple_diskusage(100, 99, 1))
            self.assertFalse(target.exists())

    def test_existing_runtime_and_database_sizes_are_included(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); repo = root / "repo"
            runtime = root / "Dalton" / "runtime"
            state = root / "Dalton" / "state" / "dalton-core"
            repo.mkdir(); runtime.mkdir(parents=True); state.mkdir(parents=True)
            (repo / "source").write_bytes(b"a" * 11)
            (runtime / "installed").write_bytes(b"b" * 13)
            (state / "core.sqlite").write_bytes(b"c" * 17)
            result = check_space(repo_root=repo, dalton_root=root / "Dalton", reserve=19,
                                 usage=lambda _: shutil._ntuple_diskusage(1000, 0, 1000))
            self.assertEqual(result["required_bytes"], 11 * 3 + 13 + 17 + 19)

if __name__ == "__main__": unittest.main()
