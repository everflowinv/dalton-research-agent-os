#!/usr/bin/env python3
"""从当前 Git HEAD 构建一个发布：wheel → release venv → release manifest。

这一步原来是手工做的，于是每次做出来的东西都只在收据里留下痕迹，而计算发布
哈希的那段代码从来没有进过仓库。反推现有收据之后规则是确定的：

    release_hash = sha256(canonical_json({
        "dependency_lock_hash": <锁定的依赖集哈希>,
        "wheel_sha256": <wheel 的 sha256>,
    }))

（在 workspace-runtime-release-20260914/builds/ 下 8 个收据与
legacy-upgrade-20260915l 的收据上逐一验证过。）

依赖锁哈希 D 是一个历史常量，原始算法已不可考，因此按值钉在
deploy/release/dependency-lock.json 里，并附上它所代表的 62 个 wheel 的精确
清单；本脚本会核对 venv 里真正装了什么与清单是否一致，不一致就拒绝，而不是
悄悄换一个哈希。

默认 dry-run：只打印计划与将要执行的命令，不写任何东西。--apply 才落地。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

SCHEMA = "dalton-release-build:0.3"
DEFAULT_LOCK = Path(__file__).resolve().parent.parent / "deploy" / "release" / "dependency-lock.json"
DEFAULT_RELEASES = Path.home() / ".dalton" / "runtime" / "releases"
DEFAULT_PYTHON = "/opt/homebrew/opt/python@3.14/bin/python3.14"


class BuildError(RuntimeError):
    """构建被拒绝。"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_hash(*, wheel_sha256: str, dependency_lock_hash: str) -> str:
    """发布哈希，与历史收据完全一致的算法。"""

    for name, value in (("wheel_sha256", wheel_sha256),
                        ("dependency_lock_hash", dependency_lock_hash)):
        if re.fullmatch(r"[0-9a-f]{64}", str(value)) is None:
            raise BuildError(f"{name} 必须是 64 位小写十六进制")
    return hashlib.sha256(canonical_json({
        "dependency_lock_hash": dependency_lock_hash,
        "wheel_sha256": wheel_sha256,
    }).encode("utf-8")).hexdigest()


def run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(command, cwd=None if cwd is None else str(cwd),
                               capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise BuildError(f"命令失败：{' '.join(command)}\n{completed.stderr.strip()[:2000]}")
    return completed.stdout.strip()


def git_state(repo: Path) -> dict[str, Any]:
    commit = run(["git", "rev-parse", "HEAD"], cwd=repo)
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BuildError("HEAD 不是一个完整的提交哈希")
    dirty = run(["git", "status", "--porcelain"], cwd=repo)
    src_tree = run(["git", "rev-parse", f"{commit}:src"], cwd=repo)
    return {"source_commit": commit, "git_src_tree": src_tree,
            "clean_tree": dirty == "",
            "dirty_paths": [line[3:] for line in dirty.splitlines()][:50]}


def read_lock(path: Path) -> dict[str, Any]:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(lock, dict)
            or lock.get("schema_version") != "dalton-release-dependency-lock:0.1"
            or re.fullmatch(r"[0-9a-f]{64}", str(lock.get("dependency_lock_hash"))) is None
            or not isinstance(lock.get("dependency_wheels"), list)
            or not lock["dependency_wheels"]):
        raise BuildError("依赖锁文件格式无效")
    return lock


def installed_distributions(venv: Path) -> dict[str, str]:
    """venv 里已安装的发行包名 → 版本。"""

    packages = sorted(venv.glob("lib/python*/site-packages/*.dist-info"))
    found: dict[str, str] = {}
    for item in packages:
        name, _, version = item.name[: -len(".dist-info")].rpartition("-")
        if name:
            found[name.replace("_", "-").lower()] = version
    return found


def locked_distributions(lock: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in lock["dependency_wheels"]:
        filename = item["filename"]
        name, _, rest = filename.partition("-")
        version = rest.split("-", 1)[0]
        found[name.replace("_", "-").lower()] = version
    return found


def verify_dependencies(venv: Path, lock: dict[str, Any]) -> dict[str, Any]:
    installed = installed_distributions(venv)
    expected = locked_distributions(lock)
    installed.pop("dalton-core", None)
    # venv 自带的引导包不属于依赖集（历史发布的 venv 里同样有 pip）。
    for bootstrap in ("pip", "setuptools", "wheel"):
        if bootstrap not in expected:
            installed.pop(bootstrap, None)
    missing = sorted(set(expected) - set(installed))
    extra = sorted(set(installed) - set(expected))
    changed = sorted(name for name in set(expected) & set(installed)
                     if expected[name] != installed[name])
    return {"missing": missing, "extra": extra, "changed_versions": changed,
            "matches": not (missing or extra or changed)}


def plan(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.expanduser().resolve()
    lock = read_lock(args.dependency_lock.expanduser().resolve())
    state = git_state(repo)
    if not state["clean_tree"] and not args.allow_dirty:
        raise BuildError("工作树不干净；发布必须从干净的 HEAD 构建（或显式 --allow-dirty）")
    return {
        "schema_version": SCHEMA,
        "writes_performed": False,
        "repo": str(repo),
        "python": args.python,
        "releases_root": str(args.releases_root.expanduser().resolve()),
        "dependency_lock_hash": lock["dependency_lock_hash"],
        "dependency_wheel_count": len(lock["dependency_wheels"]),
        **state,
        "steps": [
            f"git archive {state['source_commit']} → 临时源码树（只用提交里的字节）",
            f"{wheel_python(args)} -m build --wheel（在临时源码树里）",
            "release_hash = sha256(canonical_json({dependency_lock_hash, wheel_sha256}))",
            f"{args.python} -m venv {args.releases_root}/<release_hash>/venv"
            "（bin/python* 是指向该解释器的符号链接，不用 --copies：TCC 按真实路径记授权）",
            "<venv>/bin/python -m pip install <wheel>",
            "核对 venv 里的依赖集与 deploy/release/dependency-lock.json 一致",
            "写 <release>/release-manifest.json（0600）",
        ],
    }


def wheel_python(args: argparse.Namespace) -> str:
    """The interpreter that builds the wheel: the one running this script.

    The wheel is pure Python (``py3-none-any``), so it does not have to be
    built by the interpreter that will run it.  ``--python`` names the
    interpreter for the release venv -- live, the Homebrew python@3.14 that
    holds the Full Disk Access grant -- and that interpreter has no ``build``
    module and should not need one.  The dev venv this script runs from does.
    ``--wheel-python`` overrides for a caller that wants something else.
    """
    return args.wheel_python or sys.executable


def build(args: argparse.Namespace) -> dict[str, Any]:
    result = plan(args)
    repo = Path(result["repo"])
    commit = result["source_commit"]
    lock = read_lock(args.dependency_lock.expanduser().resolve())
    releases_root = Path(result["releases_root"])
    work = Path(tempfile.mkdtemp(prefix="dalton-release-"))
    try:
        source = work / "src"
        source.mkdir()
        archive = work / "source.tar"
        with archive.open("wb") as stream:
            completed = subprocess.run(
                ["git", "archive", "--format=tar", commit], cwd=str(repo),
                stdout=stream, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise BuildError("git archive 失败：" + completed.stderr.decode()[:500])
        run(["tar", "-xf", str(archive), "-C", str(source)])
        run([wheel_python(args), "-m", "build", "--wheel", "--outdir", str(work / "dist")],
            cwd=source)
        wheels = sorted((work / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise BuildError("构建应当恰好产出一个 wheel")
        wheel = wheels[0]
        wheel_sha = sha256_file(wheel)
        digest = release_hash(wheel_sha256=wheel_sha,
                              dependency_lock_hash=lock["dependency_lock_hash"])
        release = releases_root / digest
        venv = release / "venv"
        if venv.exists():
            raise BuildError(f"该发布已经存在：{venv}")
        release.mkdir(parents=True)
        os.chmod(release, 0o755)
        # 不用 --copies：venv/bin/python* 是指向 args.python 的符号链接。launchd
        # 执行 venv/bin/python 时内核解析到 Homebrew 的真实文件，macOS TCC 按
        # 这个真实路径记"可移除宗卷"授权，于是每个新发布不再各弹一次授权窗。
        # 复制出来的 python 本来也链接着 Homebrew 的 Python.framework，并不独立。
        from dalton_core.workspace_release import drop_venv_novelty_aliases
        run([args.python, "-m", "venv", str(venv)])
        drop_venv_novelty_aliases(venv)
        # 核心包本身不带依赖（全部是 optional extras）；依赖集按锁定清单精确安装，
        # 否则 venv 里只有 dalton_core 一个包，与历史发布不一致。
        pinned = work / "lock-requirements.txt"
        pinned.write_text("\n".join(
            f"{name}=={version}" for name, version in sorted(locked_distributions(lock).items()))
            + "\n", encoding="utf-8")
        run([str(venv / "bin" / "python"), "-m", "pip", "install", "--no-input",
             str(wheel), "-r", str(pinned)])
        dependencies = verify_dependencies(venv, lock)
        if not dependencies["matches"] and not args.allow_dependency_drift:
            shutil.rmtree(release, ignore_errors=True)
            raise BuildError(
                "venv 里的依赖集与锁定清单不一致，发布哈希会失去含义："
                + canonical_json(dependencies))
        wheel_keep = release / wheel.name
        shutil.copyfile(wheel, wheel_keep)
        os.chmod(wheel_keep, 0o600)
        # venv 内的完整性清单（dalton-release.json）：workspace 的安装/修复在
        # 指向一个发布之前会核对它；2026-09-17 之前本脚本没有写，于是第一次
        # 对这类发布做 workspace 修复就报 "release is incomplete"。
        from dalton_core.workspace_release import write_release_marker
        write_release_marker(
            venv, release_hash=digest, wheel_sha256=wheel_sha,
            dependency_lock_hash=lock["dependency_lock_hash"],
            dependency_wheels=lock["dependency_wheels"])
        manifest = {
            "schema_version": SCHEMA,
            "release_hash": digest,
            "release_ref": f"release:sha256:{digest}",
            "release_path": str(venv),
            "source_commit": commit,
            "git_src_tree": result["git_src_tree"],
            "clean_tree": result["clean_tree"],
            "wheel_sha256": wheel_sha,
            "wheel_file": wheel.name,
            "dependency_lock_hash": lock["dependency_lock_hash"],
            "dependency_verification": dependencies,
            "python": args.python,
            "built_from": "git archive of source commit",
            "status": "installed",
        }
        manifest_path = release / "release-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        manifest["candidate_manifest_sha256"] = sha256_file(manifest_path)
        manifest["release_manifest_path"] = str(manifest_path)
        manifest["writes_performed"] = True
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--releases-root", type=Path, default=DEFAULT_RELEASES)
    parser.add_argument("--dependency-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--python", default=DEFAULT_PYTHON,
                        help="用来建 venv 的解释器（线上是 Homebrew python@3.14）")
    parser.add_argument("--wheel-python", default=None,
                        help="构建 wheel 用的解释器（缺省用运行本脚本的解释器，它装有 build）")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-dependency-drift", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="真正构建；缺省只打印计划")
    args = parser.parse_args(argv)
    try:
        result = build(args) if args.apply else plan(args)
    except BuildError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
