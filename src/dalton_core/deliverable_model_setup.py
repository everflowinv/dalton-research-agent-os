"""P13ad: let the deliverable be written by a model chosen for writing it.

The Initial Screen was drafted by whatever model the *extraction* lane used,
because the launcher was handed the extraction config and nobody ever chose
otherwise.  That model is picked for reading one window of a filing and
pulling a figure out of it, cheaply, thousands of times.  The deliverable is a
different job: the sections that matter -- the core thesis, the risks and
anti-thesis, the read-across to the rest of the universe -- are the ones where
a weak model writes fluent prose that is not attached to the evidence.  Live,
the anti-thesis section came back ``dropped_unsourced`` and the relevance
section published with no figures at all.

So the deliverable gets its own routing policy, exactly as the planner did, and
for the same reason: two jobs sharing one pinned policy can never differ.

**Nothing routes here unless the owner asks.**  A strong model is fifty times
the unit cost, which is nothing against a handful of documents a day and is
still not a default anyone should acquire by upgrading.  The installer only
calls this when ``DALTON_DELIVERABLE_MODEL_PROFILE`` is set; without it the
screen keeps being written by the extraction model, exactly as before.

Never touches a credential: the broker key path is referenced, not read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .model_configurations import register_model_config_name
from .research_planner_setup import PlannerSetupError, install as install_role

POLICY_ID = "model-routing-policy:dalton-openclaw-deliverable-drafting"
CONFIG_FILE_NAME = "initial-screen-model-config.json"
# Registered from the lane rather than listed in the cap-raise script: this is
# the configuration that was left out of that script's tuple once already.
register_model_config_name(CONFIG_FILE_NAME)
# P14-M: drafting a deliverable is brain-tier work -- the sections that carry
# the argument are exactly the ones a weak model writes fluently and wrongly.
TIER = "brain"


def install(config_path: str | Path, *, profile_ids: list[str] | None = None,
            tier: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Pin the deliverable's model and write its own closed config.

    Named neither a profile nor a tier, it takes the brain chain: that is what
    the owner chose for this job, and defaulting to it is what makes a plain
    re-install keep the fallback rather than lose it.
    """

    if profile_ids is None and tier is None:
        tier = TIER
    return install_role(config_path, profile_ids=profile_ids, tier=tier,
                        policy_id=POLICY_ID, config_file_name=CONFIG_FILE_NAME, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="service.json")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--profile-ids",
        help="comma-separated profile ids the deliverable may route to, "
             "e.g. profile:gpt-6-astra",
    )
    group.add_argument(
        "--tier", help=f"pin a purpose tier's whole chain instead (default {TIER})"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    profile_ids = (
        [item.strip() for item in args.profile_ids.split(",") if item.strip()]
        if args.profile_ids else None
    )
    try:
        result = install(args.config, profile_ids=profile_ids, tier=args.tier)
    except PlannerSetupError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["CONFIG_FILE_NAME", "POLICY_ID", "TIER", "install"]
