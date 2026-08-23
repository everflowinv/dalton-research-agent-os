#!/usr/bin/env python3
"""Write the deterministic proposal-only earnings-call connector package."""

from __future__ import annotations

import json
from pathlib import Path

from dalton_core.earnings_call_transcript_proposal import (
    build_earnings_call_transcript_proposal_package,
)


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deploy" / "connector-proposals" / "earnings-call-transcript"


def main() -> int:
    package = build_earnings_call_transcript_proposal_package()
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, value in package.items():
        (TARGET / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
