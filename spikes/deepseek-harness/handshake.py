"""Credential-free DeepSeek Harness JSON-RPC cold-start probe."""

from __future__ import annotations

import json
import os
import sys
import time

from deepseek_harness import HarnessClient, HarnessConfig


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: handshake.py TEMP_RUNTIME_ROOT")
    root = sys.argv[1]
    # HarnessClient copies its parent environment before adding config.env.
    # This probe never calls a model, so strip authority-like variables rather
    # than letting a developer shell accidentally hand credentials to it.
    forbidden_markers = (
        "KEY",
        "TOKEN",
        "SECRET",
        "AUTH",
        "COOKIE",
        "OPENCLAW",
        "CODEX",
        "DATABASE",
        "DB_PATH",
    )
    for name in tuple(os.environ):
        if any(marker in name.upper() for marker in forbidden_markers):
            os.environ.pop(name, None)
    started = time.perf_counter()
    client = HarnessClient(
        HarnessConfig(
            request_timeout_seconds=15,
            shutdown_timeout_seconds=2,
            cwd=root,
            env={"DSH_SESSION_ROOT": root},
        )
    )
    try:
        client.start()
        launched = time.perf_counter()
        response = client.initialize(
            cwd=root,
            provider="deepseek-official",
            model="deepseek-v4-flash",
            max_tokens=32,
        )
        initialized = time.perf_counter()
        print(
            json.dumps(
                {
                    "start_seconds": launched - started,
                    "initialize_seconds": initialized - launched,
                    "server": response.model_dump(),
                },
                sort_keys=True,
            )
        )
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
