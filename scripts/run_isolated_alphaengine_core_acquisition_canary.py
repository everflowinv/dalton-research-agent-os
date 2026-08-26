#!/usr/bin/env python3
"""Isolated AlphaEngine Core-hosted acquisition canary (thin wrapper).

The implementation moved to ``dalton_core.alphaengine_acquisition_cli`` so the
writer service can launch the same program out of process.  This wrapper keeps
the original entry point and defaults:

* ``--fake-document-file PATH`` (+ ``--governance-approved-by human:<who>``):
  no network, in-memory approved governance, rehearsal only.
* ``--allow-network``: real loopback MCP call; requires the committed
  ``approved`` governance record (defaults to the repository proposal path).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.alphaengine_acquisition_cli import main as _main  # noqa: E402

DEFAULT_GOVERNANCE = ROOT / "deploy" / "connector-governance" / "alphaengine-get-document-v1.json"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--governance" not in args and "--governance-approved-by" not in args:
        args += ["--governance", str(DEFAULT_GOVERNANCE)]
    return _main(args)


if __name__ == "__main__":
    sys.exit(main())
