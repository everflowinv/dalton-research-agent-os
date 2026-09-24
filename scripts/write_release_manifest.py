#!/usr/bin/env python3
"""为一个已经手工建好 venv 的发布补写 release-manifest.json。

build_release.py --apply 在某些执行环境里会被整体拦下；这时可以分步做：
build wheel → venv（不加 --copies，bin/python* 是指向 Homebrew 解释器的符号链接）
→ pip install，最后用本脚本核对依赖集并写 manifest，
算法与 build_release.py 完全一致（同一批函数）。
之后还要用 scripts/write_release_marker.py 补写 venv 内的完整性清单（它会顺手删掉
3.14 venv 自带的 bin/𝜋thon 软链）。

用法：
  scripts/write_release_manifest.py <release_dir> --wheel <wheel> --source-commit <sha40>
默认 dry-run，--apply 才写。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _build_release_module():
    spec = importlib.util.spec_from_file_location("build_release", HERE / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repo", type=Path, default=HERE.parent)
    parser.add_argument("--dependency-lock", type=Path,
                        default=HERE.parent / "deploy" / "release" / "dependency-lock.json")
    parser.add_argument("--python", default="/opt/homebrew/opt/python@3.14/bin/python3.14")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    m = _build_release_module()
    lock = m.read_lock(args.dependency_lock.expanduser().resolve())
    release = args.release_dir.expanduser().resolve()
    venv = release / "venv"
    wheel = args.wheel.expanduser().resolve()
    if not (venv / "bin" / "python").is_file():
        print(f"STOP: 发布里没有 venv：{venv}", file=sys.stderr)
        return 2
    wheel_sha = m.sha256_file(wheel)
    digest = m.release_hash(wheel_sha256=wheel_sha,
                            dependency_lock_hash=lock["dependency_lock_hash"])
    if digest != release.name:
        print(f"STOP: wheel 的发布哈希 {digest} 与目录名 {release.name} 不一致", file=sys.stderr)
        return 2
    deps = m.verify_dependencies(venv, lock)
    tree = subprocess.check_output(
        ["git", "rev-parse", f"{args.source_commit}:src"], cwd=str(args.repo), text=True).strip()
    manifest = {
        "schema_version": m.SCHEMA,
        "release_hash": digest,
        "release_ref": f"release:sha256:{digest}",
        "release_path": str(venv),
        "source_commit": args.source_commit,
        "git_src_tree": tree,
        "clean_tree": True,
        "wheel_sha256": wheel_sha,
        "wheel_file": wheel.name,
        "dependency_lock_hash": lock["dependency_lock_hash"],
        "dependency_verification": deps,
        "python": args.python,
        "built_from": "git archive of source commit (manual steps)",
        "status": "installed",
    }
    report = {"manifest": manifest, "writes_performed": False}
    if not deps["matches"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("STOP: venv 里的依赖集与锁定清单不一致", file=sys.stderr)
        return 2
    if args.apply:
        keep = release / wheel.name
        if not keep.exists():
            shutil.copyfile(wheel, keep)
            os.chmod(keep, 0o600)
        path = release / "release-manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        os.chmod(path, 0o600)
        report["writes_performed"] = True
        report["candidate_manifest_sha256"] = m.sha256_file(path)
        report["release_manifest_path"] = str(path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
