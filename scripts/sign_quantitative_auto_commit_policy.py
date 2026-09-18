#!/usr/bin/env python3
"""WP-F: sign the two deterministic filed-number auto-commit rules, once.

This install holds ~5,000 numbers that are already verified against the exact
SEC rows they came from -- filed XBRL statement lines, the margins derived from
two lines of one filing, and the company-filed document figures ADR-0007
admitted -- and not one of them can enter ``claim_versions``, because the
Ledger's rule is that an automated commit needs a *named* rule in a policy the
owner has signed.  That is the correct rule and this script does not weaken it.
It does exactly one thing: publish the next governance policy version with

    policy.research_candidate_auto_commit.rules += [
        "research-auto-commit:mission-verified-figure:v1",
        "research-auto-commit:sec-statement-line:v1",
    ]

and rebind the constitution and mission to it, which is the same four-step
cascade ``publish_extraction_authority_chain.py`` already uses (a mission whose
constitution binds a stale policy cannot spend, and would be broken by a policy
publish that did not cascade).  Everything else in every record is byte
identical to its prior version.  No INSERT is hand-written anywhere: the policy
goes through ``DaltonStore.create_policy`` and, with ``--apply``, through the
live writer's ephemeral human principal exactly as every other governance
change does.

What the owner is signing, in one paragraph each:

``research-auto-commit:mission-verified-figure:v1`` -- a number published by
the company in its own document, whose digits and as-reported label were found
in the exact quoted span before the figure row was written and are re-checked
against that stored quote at admission.  Numbers spoken on an earnings call are
refused by grade and stay qualitative.

``research-auto-commit:sec-statement-line:v1`` -- a row of the filer's own XBRL
exhibit, identified by accession, statement, ordinal and concept, re-read out of
Core at admission; or a gross/operating margin computed as the ratio of two such
rows of one filing and one period, recomputed by the Ledger's numeric verifier
from the two filed values.  No model is involved at any point in either rule.

This is now a two-rule preset over ``scripts/sign_auto_commit_rules.py``,
which does the same publish for any environment and any rule: the planning,
the cascade and the writer call all live there, so a fix to one is a fix to
both.  The only thing this file still decides is *which* rules, and that this
install's state directory is the default.

Usage
-----

    # read-only: what would change, and nothing else
    .venv/bin/python scripts/sign_quantitative_auto_commit_policy.py \
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core"

    # rehearse the publish on a copy of the live Core
    ... --rehearse /tmp/wpf-policy-rehearsal --actor human:lumos

    # publish for real, through the live writer
    ... --apply --actor human:lumos

    # any other environment, or any other rule
    .venv/bin/python scripts/sign_auto_commit_rules.py --state-dir <state> \
        --rule research-auto-commit:mission-document-qualitative:v1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dalton_core.research_auto_commit import (  # noqa: E402
    MISSION_VERIFIED_FIGURE_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
)
from scripts.sign_auto_commit_rules import live, plan, rehearse  # noqa: E402

RULES = [MISSION_VERIFIED_FIGURE_RULE_REF, SEC_STATEMENT_LINE_RULE_REF]


def _live_state() -> Path:
    return Path(os.path.expanduser(
        "~/Library/Application Support/Dalton/state/dalton-core"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=_live_state())
    parser.add_argument("--mission-ref", default=None,
                        help="which active mission to rebind; only needed when "
                             "the Core has more than one")
    parser.add_argument("--apply", action="store_true",
                        help="publish through the live writer as the given human actor")
    parser.add_argument("--rehearse", type=Path,
                        help="copy the Core here and publish the cascade on the copy")
    parser.add_argument("--actor", default=None,
                        help="human:<name> principal that signs this policy version")
    args = parser.parse_args(argv)
    state_dir = Path(os.path.expanduser(str(args.state_dir))).resolve()
    if args.apply and args.rehearse:
        raise SystemExit("--apply and --rehearse are different runs; pick one")
    if (args.apply or args.rehearse) and not (args.actor or "").startswith("human:"):
        raise SystemExit("--actor human:<name> is required to sign a policy version")
    common = {"rules": list(RULES), "mission_ref": args.mission_ref}
    if args.apply:
        result = live(state_dir, actor=args.actor, **common)
    elif args.rehearse:
        result = rehearse(state_dir, Path(args.rehearse).expanduser().resolve(),
                          actor=args.actor, **common)
    else:
        result = plan(state_dir, **common)
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - an owner-run script
    sys.exit(main())
