"""Create an owner-only Dalton runtime layout without storing credentials."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from typing import Iterable

from .model_router import ModelRouter
from .observability import ObservabilityStore
from .scheduler import Scheduler
from .service import SCHEMA_VERSION, ServiceConfig
from .store import DaltonStore
from .writer_server import CORE_OPERATIONS, Principal, write_token_config


def _write_config(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def bootstrap(state_dir: str | Path, config_path: str | Path) -> dict[str, str]:
    root = Path(state_dir).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    for directory in (root, root / "run", root / "public"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    paths = {
        "core_db": root / "core.sqlite",
        "scheduler_db": root / "scheduler.sqlite",
        "model_router_db": root / "model-router.sqlite",
        "projection_db": root / "dashboard-projection.sqlite",
        "heartbeat_path": root / "run" / "heartbeat.json",
        "writer_socket": root / "run" / "writer.sock",
        "token_config": root / "writer-tokens.json",
        "static_output": root / "public" / "index.html",
    }
    with DaltonStore(paths["core_db"]) as store:
        ObservabilityStore(store)
    with Scheduler(paths["scheduler_db"]):
        pass
    if not paths["model_router_db"].exists():
        with ModelRouter(paths["model_router_db"]):
            pass
    if not paths["token_config"].exists():
        write_token_config(
            paths["token_config"],
            [
                Principal(
                    principal_id="core",
                    token=secrets.token_urlsafe(48),
                    operations=CORE_OPERATIONS,
                    unrestricted=True,
                    actor_ref="core",
                )
            ],
        )
    raw = {
        "schema_version": SCHEMA_VERSION,
        "core_db": str(paths["core_db"]),
        "scheduler_db": str(paths["scheduler_db"]),
        "projection_db": str(paths["projection_db"]),
        "model_router_db": str(paths["model_router_db"]),
        "capability_catalog_db": None,
        "heartbeat_path": str(paths["heartbeat_path"]),
        "writer_socket": str(paths["writer_socket"]),
        "tick_seconds": 5,
        "projection_min_interval_seconds": 2,
        "plugin_retry_seconds": 60,
        "plugins": [
            {
                "type": "static_dashboard",
                "enabled": True,
                "output_path": str(paths["static_output"]),
                "publisher": {
                    "type": "tencent_cos",
                    "bucket": "everflow-1320643462",
                    "region": "ap-hongkong",
                    "key": "dalton/index.html",
                    "public_url": "https://eve.lumos.space/dalton/",
                    "keychain_account": "everflow",
                    "secret_id_service": "com.openclaw.tencent-cos.sentiment-dashboard.secret-id",
                    "secret_key_service": "com.openclaw.tencent-cos.sentiment-dashboard.secret-key",
                    "protected_urls": [
                        "https://eve.lumos.space/",
                        "https://eve.lumos.space/kweb.html",
                    ],
                },
            }
        ],
    }
    if config.exists():
        ServiceConfig.from_file(config)
    else:
        _write_config(config, raw)
    os.chmod(config, 0o600)
    return {
        "state_dir": str(root),
        "config": str(config),
        "core_db": str(paths["core_db"]),
        "scheduler_db": str(paths["scheduler_db"]),
        "model_router_db": str(paths["model_router_db"]),
        "writer_socket": str(paths["writer_socket"]),
        "token_config": str(paths["token_config"]),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap an owner-only Dalton runtime")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = bootstrap(args.state_dir, args.config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
