#!/usr/bin/env python3
"""Rehearse a Dalton deploy against a copy of the live state, touching nothing.

``deploy/macos/install.sh`` hard-codes ``$HOME/Library/Application
Support/Dalton`` in its first four lines and offers no environment override, so
there is no way to point it at a scratch root.  Running it to find out whether
a deploy works is therefore the deploy.  This script performs the same ordered
steps against a temporary root instead:

    copy live state (read-only, via SQLite backup)
      -> bootstrap / open every authority, which is where migrations run
      -> seed the governance records and discovery plans install.sh seeds
      -> sync the model catalog against a *copy* of model-router.sqlite
      -> render the LaunchAgent plists into the temp dir and diff them
      -> start the writer against the temp Core with the broker stubbed
      -> drive exactly one BoundedPlannerDriver.run_once tick
      -> print lane -> status -> reason

What it deliberately does *not* do: launchctl, the live broker sockets, any
network call, any paid model call.  The only thing it opens under the live root
is a ``mode=ro`` SQLite connection and a handful of ``read_bytes()``.

The step functions below are impure by nature (they copy hundreds of megabytes
and fork a server).  Everything that decides *what* to do -- the copy plan, the
path rewriting, the seed list, the plist normalisation, the lane
classification -- is a pure function above them, and those are what the tests
cover.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_SUBDIR = Path("state") / "dalton-core"
CONFIG_SUBDIR = Path("config")

# ---------------------------------------------------------------------------
# pure: what gets copied
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyItem:
    """One thing to bring across from the live root.

    ``kind`` is ``sqlite`` (copied through a read-only backup, so a live writer
    mid-transaction cannot hand us a torn page), ``tree`` (a directory of small
    JSON records) or ``file``.
    """

    source: Path
    destination: Path
    kind: str
    required: bool = True


#: Directories under the state dir whose SQLite files are part of the Core's
#: working set.  ``acquisitions``/``fetches``/``extractions`` are deliberately
#: absent: 23 GiB of fetched documents that no migration and no tick reads.
SQLITE_SUBDIRS: tuple[str, ...] = ("", "cockpit", "research-review")

#: Small JSON trees the writer and the lanes load at start.
STATE_TREES: tuple[str, ...] = ("connector-governance", "discovery-plans", "run")

#: Loose files beside the databases.
STATE_GLOBS: tuple[str, ...] = ("*-model-config.json",)

#: Sockets and lock files never travel: a copied ``writer.sock`` is a dead
#: inode that would make the temp writer refuse to bind.
COPY_EXCLUDE_SUFFIXES: tuple[str, ...] = (".sock", ".lock")


def copy_plan(live_root: Path, temp_root: Path) -> list[CopyItem]:
    """Every file the rehearsal needs, as ``(source, destination, kind)``.

    Pure: it stats the live tree to expand the globs but decides nothing from
    the result beyond which paths exist, and it writes nothing.
    """

    live_state = live_root / STATE_SUBDIR
    temp_state = temp_root / STATE_SUBDIR
    items: list[CopyItem] = [
        CopyItem(
            live_root / CONFIG_SUBDIR / "service.json",
            temp_root / CONFIG_SUBDIR / "service.json",
            "file",
        ),
        CopyItem(
            live_state / "writer-tokens.json",
            temp_state / "writer-tokens.json",
            "file",
            required=False,
        ),
    ]
    for subdir in SQLITE_SUBDIRS:
        directory = live_state / subdir if subdir else live_state
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.sqlite")):
            relative = path.relative_to(live_state)
            items.append(CopyItem(path, temp_state / relative, "sqlite", required=False))
    for pattern in STATE_GLOBS:
        for path in sorted(live_state.glob(pattern)):
            items.append(
                CopyItem(path, temp_state / path.name, "file", required=False)
            )
    for tree in STATE_TREES:
        source = live_state / tree
        items.append(CopyItem(source, temp_state / tree, "tree", required=False))
    return items


def copy_excluded(path: Path) -> bool:
    """Whether this file must not be copied into the rehearsal root."""

    return path.suffix in COPY_EXCLUDE_SUFFIXES or path.name.startswith(".")


# ---------------------------------------------------------------------------
# pure: rewriting the live paths in service.json
# ---------------------------------------------------------------------------


def path_replacements(
    live_root: Path, temp_root: Path, *, broker_dir: Path, stub_broker_dir: Path
) -> dict[str, str]:
    """The literal string swaps that make a live service.json point at the copy.

    Order matters and is why this returns a dict the caller applies longest-key
    first: the broker directory is not under the Dalton root, so it needs its
    own rule, and rewriting ``/Users/x/.openclaw`` after the Dalton root would
    be harmless but rewriting a prefix of another key would not be.
    """

    return {
        str(broker_dir): str(stub_broker_dir),
        str(live_root): str(temp_root),
    }


def rewrite_paths(value: Any, replacements: Mapping[str, str]) -> Any:
    """Recursively swap path prefixes inside a JSON-shaped value.

    Longest key first, so ``/a/b`` never shadows ``/a/b/c``.
    """

    ordered = sorted(replacements.items(), key=lambda item: -len(item[0]))
    if isinstance(value, Mapping):
        return {key: rewrite_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rewrite_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in ordered:
            if value.startswith(old):
                return new + value[len(old):]
        return value
    return value


def invert(replacements: Mapping[str, str]) -> dict[str, str]:
    """The reverse swap, for reading a temp-root artefact as if it were live."""

    return {new: old for old, new in replacements.items()}


# ---------------------------------------------------------------------------
# pure: what install.sh seeds, and from where
# ---------------------------------------------------------------------------


#: The conditions install.sh checks before it reaches a gated seed block, as
#: predicates over the environment rather than as prose.  Each returns
#: ``(open, why)``; ``why`` is what the rehearsal prints when the gate is shut,
#: and it names the variable or path the owner would have to provide.
#:
#: These read the environment the rehearsal was started with, which is the
#: point: the question is not "could this lane exist" but "would install.sh
#: install it on this machine, today".


def _openclaw_workspace(env: Mapping[str, str]) -> Path:
    named = env.get("DALTON_OPENCLAW_WORKSPACE")
    return Path(named) if named else Path.home() / ".openclaw" / "workspace"


def _gate_market_digest(env: Mapping[str, str]) -> tuple[bool, str]:
    source = _openclaw_workspace(env) / "skills" / "market-digest" / "output"
    return source.is_dir(), f"no market-digest output at {source}"


def _gate_company_wiki(env: Mapping[str, str]) -> tuple[bool, str]:
    workspace = _openclaw_workspace(env)
    index = workspace / "wiki-index.sqlite"
    return (workspace.is_dir() and index.exists()), f"no wiki index at {index}"


def _gate_any_feed(env: Mapping[str, str]) -> tuple[bool, str]:
    """The feed plan is seeded by *either* feed lane, so its gate is the union."""

    digest, _ = _gate_market_digest(env)
    wiki, _ = _gate_company_wiki(env)
    return (digest or wiki), "neither feed lane is installed, so no feed plan is seeded"


CROWD_TOOL_VARS: tuple[str, ...] = (
    "DALTON_AGENT_REACH_TOOL", "DALTON_XUEQIU_HOT_RANK_TOOL", "DALTON_XREACH_TOOL",
)


def _gate_crowd_tools(env: Mapping[str, str]) -> tuple[bool, str]:
    absent = [
        name for name in CROWD_TOOL_VARS
        if not (env.get(name) and os.access(env[name], os.X_OK))
    ]
    return not absent, "not set to an executable: " + ", ".join(absent)


GATES: dict[str, Callable[[Mapping[str, str]], tuple[bool, str]]] = {
    "market-digest": _gate_market_digest,
    "company-wiki": _gate_company_wiki,
    "any-feed": _gate_any_feed,
    "crowd-tools": _gate_crowd_tools,
}


def gate_open(gate: str, env: Mapping[str, str]) -> tuple[bool, str]:
    """Whether install.sh would reach a seed behind ``gate`` in this environment."""

    if not gate:
        return True, ""
    return GATES[gate](env)


@dataclass(frozen=True)
class SeedSpec:
    """One seed-once copy install.sh performs.

    ``repo`` is relative to the repo root, ``state`` to the state directory.

    ``optional`` means *gated*: install.sh only reaches this copy when some
    condition outside the repository holds -- an environment variable naming a
    host tool, a directory the OpenClaw workspace is supposed to contain.  A
    gated seed that does not land is the script working as designed, and the
    rehearsal reports it as a note rather than a fault.  ``gate`` names which
    condition, as a key into :data:`GATES`, because "this lane is not
    installed" is only useful to an owner who is also told what would install
    it -- and because the rehearsal has to *evaluate* the gate rather than
    seed through it.  Copying a gated record anyway would leave the lane half
    installed: the crowd-source records with none of the three host tools they
    need, which is precisely the state install.sh's all-or-nothing rule
    exists to prevent.

    Every block in install.sh is additionally guarded by
    ``[[ -f "$repo_record" ]]``, so a seed whose source is missing from the
    repository is skipped rather than fatal.  That guard is not what this flag
    records: ``SeedTests`` asserts that *every* seed source exists, gated or
    not, because a seed pointing at a file nobody committed is a lane that can
    never be installed no matter what the owner sets.
    """

    repo: str
    state: str
    optional: bool = False
    gate: str = ""

    def __post_init__(self) -> None:
        if bool(self.optional) != bool(self.gate):
            raise ValueError(
                f"{self.repo}: a gated seed needs a gate and an ungated one "
                "must not have a gate; the two say the same thing and may not "
                "disagree"
            )
        if self.gate and self.gate not in GATES:
            raise ValueError(f"{self.repo}: {self.gate!r} is not a known gate")


#: Exactly what ``deploy/macos/install.sh`` copies, in its order.  Kept as data
#: so the test can assert it against the shell script and catch a seed added to
#: one and not the other -- which is how ``sec-filings-index-v1.json`` came to
#: exist on the live Core and nowhere in the repo.
INSTALL_SEEDS: tuple[SeedSpec, ...] = (
    SeedSpec(
        "deploy/connector-governance/alphaengine-get-document-v1.json",
        "connector-governance/alphaengine-get-document-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-company-facts-v1.json",
        "connector-governance/sec-company-facts-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-company-facts-v2.json",
        "connector-governance/sec-company-facts-v2.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-company-facts-v3.json",
        "connector-governance/sec-company-facts-v3.json",
    ),
    # INT3: the two roic.ai records are committed and install.sh deliberately
    # does not seed them -- see DELIBERATELY_UNSEEDED in the script. roic
    # answers 403 site-wide and nothing in the writer loads either record, so
    # a seed here would be two approvals to make about a source that answers
    # nothing.
    SeedSpec(
        "deploy/connector-governance/guidepoint-search-library-v1.json",
        "connector-governance/guidepoint-search-library-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/guidepoint-get-transcript-v1.json",
        "connector-governance/guidepoint-get-transcript-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-financial-statements-v1.json",
        "connector-governance/sec-financial-statements-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-financial-statements-v2.json",
        "connector-governance/sec-financial-statements-v2.json",
    ),
    SeedSpec(
        "deploy/connector-governance/yfinance-daily-prices-v1.json",
        "connector-governance/yfinance-daily-prices-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/yfinance-analyst-estimates-v1.json",
        "connector-governance/yfinance-analyst-estimates-v1.json",
    ),
    # S5: the four ownership records go down together -- the lane's argument is
    # the governance directory and the launcher asks it which operations it may
    # run, so three of four is a lane that reports one operation unapproved for
    # ever.  The two watcher records turn nothing on by themselves: the
    # watcher's switch is the declared-pages file, which install.sh does not
    # write because the ten URLs need a human to confirm them first.
    SeedSpec(
        "deploy/connector-governance/sec-form4-transactions-v1.json",
        "connector-governance/sec-form4-transactions-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-beneficial-ownership-v1.json",
        "connector-governance/sec-beneficial-ownership-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-form144-notices-v1.json",
        "connector-governance/sec-form144-notices-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/sec-form13f-holdings-v1.json",
        "connector-governance/sec-form13f-holdings-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/ir-page-watch-list-watches-v1.json",
        "connector-governance/ir-page-watch-list-watches-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/ir-page-watch-get-diff-v1.json",
        "connector-governance/ir-page-watch-get-diff-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/alphaengine-search-library-v1.json",
        "connector-governance/alphaengine-search-library-v1.json",
    ),
    SeedSpec(
        "deploy/phase9/p9d-us-it-services-discovery-plan-v1.json",
        "discovery-plans/us-it-services-alphaengine-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/gemini-web-search-v1.json",
        "connector-governance/gemini-web-search-v1.json",
    ),
    SeedSpec(
        "deploy/connector-governance/web-fetch-v1.json",
        "connector-governance/web-fetch-v1.json",
    ),
    SeedSpec(
        "deploy/phase9/p9d4-us-it-services-web-search-plan-v3.json",
        "discovery-plans/us-it-services-web-search-v3.json",
    ),
    # P10u / INT3: the filings lane is a record *and* a plan, seeded as one
    # block. The writer's plist names the record unconditionally, so a Core
    # without it gets ``--sec-filings-governance`` pointing at nothing -- which
    # is what it did until the record was recovered into the repository.
    SeedSpec(
        "deploy/connector-governance/sec-filings-index-v1.json",
        "connector-governance/sec-filings-index-v1.json",
    ),
    SeedSpec(
        "deploy/phase10/p10-us-it-services-sec-filings-plan-v1.json",
        "discovery-plans/us-it-services-sec-filings-v1.json",
    ),
    # -- INT2's second batch -------------------------------------------------
    # C1: the catalyst calendar's one record is the whole switch for that lane.
    # Ungated: install.sh seeds it on every machine.
    SeedSpec(
        "deploy/connector-governance/yfinance-calendar-v1.json",
        "connector-governance/yfinance-calendar-v1.json",
    ),
    # S4: six China / Hong Kong fundamentals records with no lane in this wave.
    # Ungated and harmless -- seeding them turns nothing on, and cannot
    # half-turn-on anything, because there is nothing to turn on.  They are on
    # disk so the owner can read and approve six schema hashes separately.
    *(
        SeedSpec(
            f"deploy/connector-governance/cn-hk-findata-{kind}-v1.json",
            f"connector-governance/cn-hk-findata-{kind}-v1.json",
        )
        for kind in (
            "financial-statements", "shareholders", "buybacks",
            "margin-balance", "northbound-flow", "ah-premium",
        )
    ),
    # S2 / INT2: the Guidepoint lane's discovery plan.  The two records were
    # already seeded further up; the plan is the other half of the switch and
    # its argv fragment requires both, so they are seeded together.
    SeedSpec(
        "deploy/phase9/p9-us-it-services-guidepoint-v1.json",
        "discovery-plans/us-it-services-guidepoint-v1.json",
    ),
    # The narrowing record goes to ``governance-decisions/``, NOT to
    # ``connector-governance/``.  install.sh is explicit about this and the
    # distinction matters: nothing loads this record, it is the note the owner
    # reads before deciding what to do about an operation the upstream does not
    # have.  Seeding it into the runtime governance directory would put a
    # permanently-``proposed`` record where the cockpit looks, and the owner
    # would be shown a lane waiting for an approval about nothing.
    SeedSpec(
        "deploy/connector-governance/guidepoint-get-transcript-narrowing-v1.json",
        "governance-decisions/guidepoint-get-transcript-narrowing-v1.json",
    ),
    # P14a / INT2: the tracking policy is the daily-tracking lane's whole
    # switch. One file, so all-or-nothing is automatic.
    SeedSpec(
        "deploy/phase9/p14a-tracking-policy-v1.json",
        "tracking-policy.json",
    ),
    # P14e / INT2: publication material, put where the owner reads it and no
    # lane looks. It is not the ad-hoc lane's switch and does not turn it on.
    SeedSpec(
        "deploy/phase8/p14e-adhoc-probe-templates-v1.json",
        "phase8/p14e-adhoc-probe-templates-v1.json",
    ),
    # S1: the two human-feed lanes.  Both gated on the OpenClaw workspace
    # actually containing what they read, and seeded independently -- sales
    # notes and the company wiki are separate approvals and separate
    # directories, so one being absent must not take the other with it.
    SeedSpec(
        "deploy/phase9/p9-us-it-services-feeds-v1.json",
        "feed-plans/p9-us-it-services-feeds-v1.json", optional=True,
        gate="any-feed",
    ),
    *(
        SeedSpec(
            f"deploy/connector-governance/{kind}-v1.json",
            f"connector-governance/{kind}-v1.json", optional=True,
            gate="market-digest",
        )
        for kind in ("sales-notes-list-notes", "sales-notes-get-note")
    ),
    *(
        SeedSpec(
            f"deploy/connector-governance/{kind}-v1.json",
            f"connector-governance/{kind}-v1.json", optional=True,
            gate="company-wiki",
        )
        for kind in ("company-wiki-list-documents", "company-wiki-get-document")
    ),
    # S3: seven crowd-source records and the per-company map, all or nothing.
    # The map is the lane's switch, and a lane switched on with no host tool
    # refuses every networked run with "no tool configured" -- which is the
    # failure the all-or-nothing rule exists to prevent.
    *(
        SeedSpec(
            f"deploy/connector-governance/{kind}-v1.json",
            f"connector-governance/{kind}-v1.json", optional=True,
            gate="crowd-tools",
        )
        for kind in (
            "xueqiu-search-posts", "xueqiu-get-post", "xueqiu-hot-rank",
            "x-xreach-user-timeline", "x-xreach-search", "x-xreach-thread",
            "employee-reviews-blind",
        )
    ),
    SeedSpec(
        "deploy/phase9/p9-us-it-services-crowd-sources-v1.json",
        "phase9/p9-us-it-services-crowd-sources-v1.json", optional=True,
        gate="crowd-tools",
    ),
)


def seeded_repo_records(seeds: Sequence[SeedSpec] = INSTALL_SEEDS) -> frozenset[str]:
    """The ``deploy/connector-governance`` file names install.sh seeds."""

    prefix = "deploy/connector-governance/"
    return frozenset(
        spec.repo[len(prefix):] for spec in seeds if spec.repo.startswith(prefix)
    )


# ---------------------------------------------------------------------------
# pure: reading the seeds back out of install.sh itself
# ---------------------------------------------------------------------------

_GOVERNANCE_SOURCE = "$repo_root/deploy/connector-governance/"


def strip_shell_comments(text: str) -> str:
    """``install.sh`` with its comment lines removed.

    Half the connector names in that file appear only in a comment explaining
    why they are *not* seeded, so reading the raw text would report every
    deliberately-absent lane as installed.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def expand_for_loops(code: str) -> str:
    """Unroll ``for VAR in a b c; do ... done`` so the file names are literal.

    Several seed blocks are written as ``${roic_kind}-v1.json`` inside a loop,
    so the name of the record being copied appears nowhere in the script's
    text.  Reading the text without this reported six ``cn-hk-findata-*``
    records as unseeded while install.sh was seeding them -- a gap that was
    invisible because it *skipped* rather than failed.  These loops are never
    nested, so a non-greedy match to the first ``done`` is exact.
    """

    joined = re.sub(r"\\\n\s*", " ", code)
    pattern = re.compile(
        r"^ *for +(\w+) +in +(.+?); *do\n(.*?)^ *done *$",
        re.DOTALL | re.MULTILINE,
    )
    while True:
        match = pattern.search(joined)
        if match is None:
            return joined
        variable, words, body = match.group(1), match.group(2).split(), match.group(3)
        unrolled = "".join(
            body.replace("${" + variable + "}", word).replace("$" + variable, word)
            for word in words
        )
        joined = joined[: match.start()] + unrolled + joined[match.end():]


def install_script_code(install_sh: Path) -> str:
    """``install.sh`` with its comments gone and its seed loops unrolled."""

    return expand_for_loops(strip_shell_comments(
        install_sh.read_text(encoding="utf-8")))


def install_seeded_records(install_sh: Path) -> frozenset[str]:
    """Every committed record install.sh copies *into the runtime governance
    directory*.

    Read out of the script rather than transcribed, because a transcription is
    what drifts.  A record copied somewhere else -- the Guidepoint narrowing
    note goes to ``governance-decisions/`` -- is not seeded for this purpose:
    the question this answers is "which approvals will the owner be shown",
    and a file no lane and no cockpit loads is not one of them.
    """

    code = install_script_code(install_sh)
    assignments: dict[str, str] = {}
    seeded: set[str] = set()
    assign = re.compile(r'^\s*(\w+)="([^"]*)"\s*$')
    copy = re.compile(r'^\s*cp +"([^"]+)" +"\$(\w+)"\s*$')
    for line in code.splitlines():
        found = assign.match(line)
        if found is not None:
            assignments[found.group(1)] = found.group(2)
            continue
        found = copy.match(line)
        if found is None:
            continue
        source, destination = found.group(1), assignments.get(found.group(2), "")
        if source.startswith("$") and not source.startswith("$repo_root"):
            # ``cp "$repo_record" "$sec_financials_file"``: the source is a
            # variable too, so resolve it the same way as the destination.
            source = assignments.get(source.lstrip("$"), source)
        if not source.startswith(_GOVERNANCE_SOURCE):
            continue
        if not destination.startswith("$governance_dir/"):
            continue
        seeded.add(source[len(_GOVERNANCE_SOURCE):])
    return frozenset(seeded)


def deliberately_unseeded_records(install_sh: Path) -> frozenset[str]:
    """The ``DELIBERATELY_UNSEEDED`` array install.sh declares, with reasons.

    The array is read by nothing at runtime.  It is there so that "every
    committed record is either seeded or deliberately not" can be *checked*
    rather than asserted in a comment, which is what stops the next record
    from being committed and forgotten.
    """

    text = install_sh.read_text(encoding="utf-8")
    match = re.search(r"^DELIBERATELY_UNSEEDED=\(\n(.*?)^\)\s*$",
                      text, re.DOTALL | re.MULTILINE)
    if match is None:
        return frozenset()
    return frozenset(
        word
        for line in match.group(1).splitlines()
        if not line.lstrip().startswith("#")
        for word in line.split()
    )


def committed_governance_records(repo_root: Path) -> frozenset[str]:
    """Every record file ``deploy/connector-governance`` ships."""

    directory = repo_root / "deploy" / "connector-governance"
    if not directory.is_dir():
        return frozenset()
    return frozenset(path.name for path in directory.glob("*.json"))


def unseeded_governance_records(
    repo_root: Path, install_sh: Path | None = None
) -> tuple[str, ...]:
    """Committed governance records install.sh seeds nowhere *and* has not
    named as a deliberate absence.

    This should be empty.  A record here is one whose lane will report
    ``unconfigured`` for ever with nobody having decided that -- which is how
    fifteen records, ``yfinance-calendar-v1.json`` among them, sat in the repo
    switching nothing on.  Fix it by seeding it in its lane's block, or by
    naming it in ``DELIBERATELY_UNSEEDED`` with the reason.
    """

    script = install_sh or (repo_root / "deploy" / "macos" / "install.sh")
    if not script.is_file():
        return ()
    remaining = (committed_governance_records(repo_root)
                 - install_seeded_records(script)
                 - deliberately_unseeded_records(script))
    return tuple(sorted(remaining))


def orphan_live_records(live_governance_dir: Path, repo_root: Path) -> tuple[str, ...]:
    """Records the live Core holds that the repo cannot re-seed.

    These are the ones that would silently vanish from a Core rebuilt from
    scratch, so the runbook has to say "back this directory up" rather than
    "install.sh will put it back".
    """

    if not live_governance_dir.is_dir():
        return ()
    repo_dir = repo_root / "deploy" / "connector-governance"
    committed = {path.name for path in repo_dir.glob("*.json")} if repo_dir.is_dir() else set()
    return tuple(
        sorted(
            path.name
            for path in live_governance_dir.glob("*.json")
            if path.name not in committed
        )
    )


#: Lanes whose switch is a file the deploy has to put on disk, and whether
#: ``install.sh`` puts it there.  A lane whose switch install.sh does not write
#: is not broken -- it is simply never installed, and it will read as
#: ``unconfigured`` for ever without anyone having decided that.
@dataclass(frozen=True)
class LaneSwitch:
    lane: str
    state_file: str
    repo_source: str | None
    seeded_by_install: bool
    note: str = ""


LANE_SWITCHES: tuple[LaneSwitch, ...] = (
    LaneSwitch(
        "mission_tracking (P14a)", "tracking-policy.json",
        "deploy/phase9/p14a-tracking-policy-v1.json", True,
        "INT2 seed block; one file, so all-or-nothing is automatic",
    ),
    LaneSwitch(
        "catalog_sync (P14-M2)", "model-catalog-sync.json", None, True,
        "P14-M2 seed block, beside the catalog sync itself; one file naming the "
        "gateway config to follow and the router to follow it into, written "
        "only when ~/.openclaw/openclaw.json is there",
    ),
    LaneSwitch(
        "event_judgement (P14a)", "event-judgement-model-config.json", None, False,
        "INT3: written when DALTON_EVENT_JUDGEMENT_MODEL_PROFILE/TIER is set, "
        "and only together with the verifier",
    ),
    LaneSwitch(
        "event_judgement verifier (P14a)", "event-verifier-model-config.json", None, False,
        "INT3: written when DALTON_EVENT_VERIFIER_MODEL_PROFILE/TIER is set; "
        "must be a different family, or every judgement comes back "
        "gated:same_family",
    ),
    LaneSwitch(
        "claim_index (P12b)", "claim-index-model-config.json", None, False,
        "INT3: written when DALTON_CLAIM_INDEX_MODEL_PROFILE/TIER is set",
    ),
    LaneSwitch(
        "document_extraction", "document-extraction-model-config.json", None, True,
        "dalton_core.document_extraction_setup --tier cheap",
    ),
    LaneSwitch(
        "initial_screen", "initial-screen-model-config.json", None, False,
        "written only when DALTON_DELIVERABLE_MODEL_PROFILE/TIER is set",
    ),
    LaneSwitch(
        "research_plan", "research-planner-model-config.json", None, False,
        "written only when DALTON_PLANNER_MODEL_PROFILE/TIER is set",
    ),
)


def missing_lane_switches(state_dir: Path) -> tuple[LaneSwitch, ...]:
    """Lane switches that are not on disk after the install steps have run."""

    return tuple(
        switch for switch in LANE_SWITCHES
        if not (state_dir / switch.state_file).exists()
    )


#: The ``autonomy.may_write`` words the merged lanes need, and who says so.
#: This is the list the runbook's "mission version publish" step has to carry,
#: so it lives here next to the check that reads the live mission rather than
#: only in prose.
REQUIRED_WRITE_SCOPES: tuple[tuple[str, str], ...] = (
    ("market_price", "P11a / INT1: the price lane; without it every tick is ungranted"),
    ("market_event", "P14a: the tracking lane writes nothing without it"),
    ("claim_index", "P12b / INT1: the index lane holds not_authorized and spends nothing"),
    ("research_task", "P14e: ad-hoc research tasks"),
    ("dossier", "P12a: the company dossier lane"),
    ("debate_map", "P12c: the debate map lane"),
    ("valuation", "P11a: valuation snapshots"),
    ("consensus_estimate", "P11a: analyst estimates"),
    ("forecast_revision_proposal", "P14a: forecast revisions proposed by an event"),
    ("thesis_revision_candidate", "P14a / ADR-0007: also needs a human_checkpoint"),
    ("conviction_call", "W3: conviction calls"),
)


def missing_write_scopes(
    granted: Iterable[str], required: Sequence[tuple[str, str]] = REQUIRED_WRITE_SCOPES
) -> list[tuple[str, str]]:
    """The ``may_write`` words a merged main needs and the live mission lacks."""

    have = set(granted)
    return [(word, why) for word, why in required if word not in have]


# ---------------------------------------------------------------------------
# pure: which authority owns which schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationSpec:
    """One schema application, named the way a deploy failure would name it.

    ``kind`` is ``core`` for an authority constructed on the shared
    ``DaltonStore`` (its ``__init__`` runs the ``executescript`` and any
    ``ALTER``/rebuild migration), or ``sidecar`` for a store that owns its own
    database file.
    """

    schema: str
    module: str
    symbol: str
    kind: str
    database: str = "core.sqlite"


#: Every ``*_schema.sql`` in ``dalton_core`` and the class whose constructor
#: applies it.  This is the deploy's real migration list: ``dalton-bootstrap``
#: opens only five of these, and the other forty-seven run the first time the
#: writer or a lane constructs their authority -- which on a live Core is
#: several minutes after the install script has already exited zero.  The
#: rehearsal runs all of them up front so a broken one is found here.
CORE_MIGRATIONS: tuple[MigrationSpec, ...] = (
    MigrationSpec("schema.sql", "dalton_core.store", "DaltonStore", "root"),
    MigrationSpec("observability_schema.sql", "dalton_core.observability", "ObservabilityStore", "core"),
    MigrationSpec("agenda_schema.sql", "dalton_core.agenda", "AgendaStore", "core"),
    MigrationSpec("model_input_schema.sql", "dalton_core.model_input", "ModelInputLedger", "core"),
    MigrationSpec("industry_research_schema.sql", "dalton_core.industry_research", "IndustryResearchAuthority", "core"),
    MigrationSpec("analyst_journal_schema.sql", "dalton_core.analyst_journal", "AnalystJournalAuthority", "core"),
    MigrationSpec("bounded_planner_loop_schema.sql", "dalton_core.bounded_planner_loop", "BoundedPlannerAuthority", "core"),
    MigrationSpec("capability_schema.sql", "dalton_core.capability_registry", "CapabilityRegistry", "core"),
    MigrationSpec("catalyst_calendar_schema.sql", "dalton_core.catalyst_calendar", "CatalystCalendarAuthority", "core"),
    MigrationSpec("claim_index_schema.sql", "dalton_core.claim_index_authority", "ClaimIndexAuthority", "core"),
    MigrationSpec("claim_retirement_schema.sql", "dalton_core.claim_retirement", "ClaimRetirementAuthority", "core"),
    MigrationSpec("company_dossier_schema.sql", "dalton_core.company_dossier", "CompanyDossierAuthority", "core"),
    MigrationSpec("connector_schema.sql", "dalton_core.connector", "ConnectorStore", "core"),
    # P11b: what the street expects, from the vendor daily.
    MigrationSpec("consensus_estimate_schema.sql", "dalton_core.consensus_estimate", "ConsensusEstimateAuthority", "core"),
    MigrationSpec("conviction_call_schema.sql", "dalton_core.conviction_call", "ConvictionCallAuthority", "core"),
    MigrationSpec("coverage_mission_schema.sql", "dalton_core.coverage_mission", "CoverageMissionAuthority", "core"),
    MigrationSpec("credential_authority_schema.sql", "dalton_core.credential_authority", "CredentialAuthorityStore", "core"),
    MigrationSpec("debate_map_schema.sql", "dalton_core.debate_map", "DebateMapAuthority", "core"),
    MigrationSpec("deep_insight_gate_schema.sql", "dalton_core.deep_insight_gate", "DeepInsightGateAuthority", "core"),
    MigrationSpec("deliverable_reopen_schema.sql", "dalton_core.deliverable_reopen", "GateReopenAuthority", "core"),
    MigrationSpec("event_judgement_schema.sql", "dalton_core.event_judgement", "EventJudgementAuthority", "core"),
    MigrationSpec("forecast_driver_schema.sql", "dalton_core.model_forecast_driver", "ForecastModelAuthority", "core"),
    MigrationSpec("forecast_reconciliation_schema.sql", "dalton_core.forecast_reconciliation", "ForecastReconciliationAuthority", "core"),
    MigrationSpec("market_price_schema.sql", "dalton_core.market_price", "MarketPriceSeriesAuthority", "core"),
    MigrationSpec("mission_deliverable_schema.sql", "dalton_core.mission_deliverable", "MissionDeliverableAuthority", "core"),
    MigrationSpec("model_forecast_schema.sql", "dalton_core.model_forecast", "ModelForecastAuthority", "core"),
    MigrationSpec("research_constitution_schema.sql", "dalton_core.research_constitution", "ResearchConstitutionAuthority", "core"),
    MigrationSpec("research_cycle_reflection_schema.sql", "dalton_core.research_cycle_reflection", "ResearchCycleReflectionAuthority", "core"),
    MigrationSpec("research_doctrine_schema.sql", "dalton_core.research_doctrine", "ResearchDoctrineAuthority", "core"),
    MigrationSpec("research_event_schema.sql", "dalton_core.research_event", "ResearchEventAuthority", "core"),
    MigrationSpec("research_plan_schema.sql", "dalton_core.research_plan", "ResearchPlanAuthority", "core"),
    MigrationSpec("research_playbook_schema.sql", "dalton_core.research_playbook", "ResearchPlaybookAuthority", "core"),
    MigrationSpec("research_quality_schema.sql", "dalton_core.research_quality_score", "QualityScoreAuthority", "core"),
    MigrationSpec("research_question_backlog_schema.sql", "dalton_core.research_question_backlog", "ResearchQuestionBacklog", "core"),
    MigrationSpec("runner_journal_schema.sql", "dalton_core.runner_journal", "RunnerJournal", "core"),
    MigrationSpec("statement_snapshot_schema.sql", "dalton_core.statement_snapshot", "StatementSnapshotAuthority", "core"),
    # P11b: the broker notes those expectations were read out of.
    MigrationSpec("street_estimate_schema.sql", "dalton_core.street_estimate", "StreetEstimateStore", "core"),
    MigrationSpec("tracking_cadence_schema.sql", "dalton_core.tracking_cadence", "TrackingCadenceAuthority", "core"),
    MigrationSpec("transcript_correction_schema.sql", "dalton_core.transcript_correction", "TranscriptCorrectionAuthority", "core"),
    MigrationSpec("transcript_polish_schema.sql", "dalton_core.transcript_polish", "TranscriptPolishAuthority", "core"),
    MigrationSpec("valuation_snapshot_schema.sql", "dalton_core.valuation_snapshot", "ValuationSnapshotAuthority", "core"),
    MigrationSpec("weekly_brief_schema.sql", "dalton_core.weekly_brief", "WeeklyBriefAuthority", "core"),
    MigrationSpec("answer_routing_schema.sql", "dalton_core.answer_routing", "AnswerRoutingAuthority", "core"),
    MigrationSpec("thesis_revision_schema.sql", "dalton_core.thesis_revision", "ThesisRevisionAuthority", "core"),
    MigrationSpec("thesis_impact_schema.sql", "dalton_core.thesis_impact", "ThesisImpactAuthority", "core"),
    # P10x: the only Core authority that takes the store's *connection* rather
    # than the store.  It lives in core.sqlite all the same -- it reads the
    # discovered-document rows the debate map's independence ladder reads --
    # so it belongs here and not among the sidecars.
    MigrationSpec("extraction_backlog_schema.sql", "dalton_core.extraction_backlog", "DocumentProvenanceStore", "core"),
)

SIDECAR_MIGRATIONS: tuple[MigrationSpec, ...] = (
    MigrationSpec("scheduler_schema.sql", "dalton_core.scheduler", "Scheduler", "sidecar", "scheduler.sqlite"),
    MigrationSpec("model_router_schema.sql", "dalton_core.model_router", "ModelRouter", "sidecar", "model-router.sqlite"),
    MigrationSpec("dashboard_schema.sql", "dalton_core.dashboard", "ProjectionWriter", "sidecar", "dashboard-projection.sqlite"),
    MigrationSpec("capability_catalog_schema.sql", "dalton_core.capability_catalog", "CapabilityCatalog", "sidecar", "catalog.sqlite"),
    MigrationSpec("document_index_schema.sql", "dalton_core.document_index", "DocumentIndex", "sidecar", "document-index.sqlite"),
    MigrationSpec("human_intent_schema.sql", "dalton_core.human_intent", "HumanIntentAuthority", "sidecar", "human-intent.sqlite"),
    MigrationSpec("openclaw_exporter_schema.sql", "dalton_core.openclaw_metadata_exporter", "OpenClawMetadataExporter", "sidecar", "openclaw-exporter.sqlite"),
    MigrationSpec("research_coordinator_schema.sql", "dalton_core.research_coordinator", "ResearchCoordinatorStore", "sidecar", "research-coordinator.sqlite"),
    MigrationSpec("candidate_staging_schema.sql", "dalton_core.research_verification", "CandidateStagingStore", "sidecar", "research-review/candidate-staging.sqlite"),
    # The review authority opens the *candidate staging* database -- it has no
    # file of its own -- so its schema lands beside the staging tables and must
    # be applied after them.
    MigrationSpec("research_review_schema.sql", "dalton_core.research_review", "HumanReviewAuthority", "sidecar", "research-review/candidate-staging.sqlite"),
    MigrationSpec("tick_ledger_schema.sql", "dalton_core.tick_ledger", "TickLedger", "sidecar", "tick-ledger.sqlite"),
    # C2: budget_pools_schema.sql is applied by ``apply_pool_migration``, which
    # ThesisImpactBudgetStore calls -- it is not a constructor of its own.
    MigrationSpec("thesis_impact_budget_schema.sql", "dalton_core.thesis_impact_budget", "ThesisImpactBudgetStore", "sidecar", "thesis-impact-budget.sqlite"),
    MigrationSpec("budget_pools_schema.sql", "dalton_core.thesis_impact_budget", "ThesisImpactBudgetStore", "sidecar", "thesis-impact-budget.sqlite"),
)


def known_schema_files() -> frozenset[str]:
    """Every schema this script claims an owner for."""

    return frozenset(
        spec.schema for spec in CORE_MIGRATIONS + SIDECAR_MIGRATIONS
    )


def package_schema_files(package_dir: Path) -> frozenset[str]:
    """Every ``*.sql`` shipped in ``dalton_core``."""

    return frozenset(path.name for path in package_dir.glob("*.sql"))


# ---------------------------------------------------------------------------
# pure: plist comparison
# ---------------------------------------------------------------------------


def normalise_plist(value: Any, replacements: Mapping[str, str]) -> Any:
    """A rendered plist with the temp paths mapped back to their live form.

    Without this every single argument differs and the diff says nothing.  With
    it, the only lines left are the ones a deploy would actually change.
    """

    return rewrite_paths(value, replacements)


def plist_diff(live: Mapping[str, Any], rendered: Mapping[str, Any]) -> list[str]:
    """Human-readable differences between the installed and rendered plists."""

    differences: list[str] = []
    for key in sorted(set(live) | set(rendered)):
        before, after = live.get(key, _MISSING), rendered.get(key, _MISSING)
        if before == after:
            continue
        if key == "ProgramArguments" and isinstance(before, list) and isinstance(after, list):
            differences.extend(_argv_diff(before, after))
            continue
        differences.append(f"{key}: {_show(before)} -> {_show(after)}")
    return differences


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<absent>"


_MISSING = _Missing()


def _show(value: Any) -> str:
    if value is _MISSING:
        return "<absent>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _argv_diff(before: Sequence[Any], after: Sequence[Any]) -> list[str]:
    """Argument-level diff: added and removed flags, plus reordering.

    A plist's ProgramArguments is a flag list, so reporting it as one changed
    blob hides the single new ``--market-price-governance`` that is the whole
    point of the comparison.
    """

    added = [item for item in after if item not in before]
    removed = [item for item in before if item not in after]
    lines: list[str] = []
    for item in removed:
        lines.append(f"ProgramArguments: -{item}")
    for item in added:
        lines.append(f"ProgramArguments: +{item}")
    if not lines and list(before) != list(after):
        lines.append("ProgramArguments: same arguments, different order")
    return lines


# ---------------------------------------------------------------------------
# pure: reading one tick
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneRow:
    lane: str
    operation: str
    status: str
    reason: str
    held: bool


#: A tick key that is not a lane.  ``lane_rows`` reports them too -- a tick
#: whose own ledger write failed matters as much as a lane that raised -- but
#: it labels them so the table does not claim the registry has 20 lanes.
NON_LANE_KEYS: tuple[str, ...] = ("mission_sec_dispatch", "forecast_reconciliation", "tick_ledger")

#: The prefixes a driver puts on a status when something *escaped* rather than
#: being held.  ``run_once`` catches every lane exception and turns it into
#: ``unavailable:<ExceptionName>``; ``_record_tick`` does the same with
#: ``unrecorded:<ExceptionName>``.  Everything else -- ``ungranted``,
#: ``unconfigured``, ``unapproved``, ``idle``, ``deferred`` -- is a lane that
#: looked at its own preconditions and declined, which is the healthy answer
#: for a Core whose approvals are not yet in place.
ESCAPED_STATUS_PREFIXES: tuple[str, ...] = ("unavailable:", "unrecorded:")


def status_of(result: Any) -> str:
    if isinstance(result, Mapping):
        value = result.get("status")
        if isinstance(value, str):
            return value
        return "<no status>"
    return f"<{type(result).__name__}>"


def reason_of(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    for key in ("reason", "detail", "message", "lane_status", "note"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    counts = {
        key: value for key, value in result.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if counts:
        return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    return ""


def escaped(status: str) -> bool:
    """Whether this status is an exception that got out of its lane."""

    return status.startswith(ESCAPED_STATUS_PREFIXES)


def lane_rows(summary: Mapping[str, Any], lane_operations: Mapping[str, str]) -> list[LaneRow]:
    """The lane table, in controller-tick order, with the non-lanes last."""

    rows: list[LaneRow] = []
    for driver_key, operation in lane_operations.items():
        if driver_key not in summary:
            rows.append(LaneRow(driver_key, operation, "<not in tick>", "", False))
            continue
        result = summary[driver_key]
        status = status_of(result)
        rows.append(
            LaneRow(driver_key, operation, status, reason_of(result), not escaped(status))
        )
    for key in NON_LANE_KEYS:
        if key not in summary:
            continue
        result = summary[key]
        status = status_of(result)
        rows.append(LaneRow(key, "(tick)", status, reason_of(result), not escaped(status)))
    return rows


def render_table(rows: Sequence[LaneRow]) -> str:
    """``lane -> status -> reason``, aligned, no colour, safe in a report."""

    headers = ("lane", "status", "reason")
    body = [(row.lane, row.status, row.reason[:78]) for row in rows]
    widths = [
        max(len(headers[index]), *(len(item[index]) for item in body)) if body
        else len(headers[index])
        for index in range(3)
    ]
    line = "  ".join("-" * width for width in widths)
    out = ["  ".join(headers[i].ljust(widths[i]) for i in range(3)), line]
    out.extend("  ".join(item[i].ljust(widths[i]) for i in range(3)).rstrip() for item in body)
    return "\n".join(out)


def escaped_rows(rows: Sequence[LaneRow]) -> list[LaneRow]:
    return [row for row in rows if not row.held]


# ---------------------------------------------------------------------------
# impure: the steps
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    name: str
    ok: bool
    seconds: float
    detail: str = ""
    findings: list[str] = field(default_factory=list)


class Rehearsal:
    def __init__(
        self,
        live_root: Path,
        temp_root: Path,
        *,
        openclaw_config: Path,
        log: Callable[[str], None] = print,
    ) -> None:
        self.live_root = live_root.expanduser().resolve()
        self.temp_root = temp_root.expanduser().resolve()
        self.openclaw_config = openclaw_config.expanduser()
        self.log = log
        self.temp_state = self.temp_root / STATE_SUBDIR
        self.temp_config = self.temp_root / CONFIG_SUBDIR / "service.json"
        self.stub_broker_dir = self.temp_root / "broker"
        self.launch_agents_dir = self.temp_root / "LaunchAgents"
        self.log_dir = self.temp_root / "logs"
        self.steps: list[StepResult] = []
        self.replacements = path_replacements(
            self.live_root,
            self.temp_root,
            broker_dir=Path.home() / ".openclaw",
            stub_broker_dir=self.stub_broker_dir,
        )
        self.tick_summary: dict[str, Any] = {}
        self.rows: list[LaneRow] = []
        self._writer: subprocess.Popen[bytes] | None = None
        self._broker: "StubBroker | None" = None

    # -- step plumbing ------------------------------------------------------

    def step(self, name: str, fn: Callable[[], tuple[str, list[str]]]) -> StepResult:
        self.log(f"\n== {name}")
        started = time.monotonic()
        try:
            detail, findings = fn()
            result = StepResult(name, True, time.monotonic() - started, detail, findings)
        except Exception as exc:  # noqa: BLE001 - a failed step is the output
            result = StepResult(
                name, False, time.monotonic() - started,
                f"{type(exc).__name__}: {exc}",
            )
        self.steps.append(result)
        self.log(f"   [{'ok' if result.ok else 'FAILED'}] {result.seconds:.1f}s {result.detail}")
        for finding in result.findings:
            self.log(f"   ! {finding}")
        return result

    # -- 1. copy ------------------------------------------------------------

    def copy_state(self) -> tuple[str, list[str]]:
        items = copy_plan(self.live_root, self.temp_root)
        copied = skipped = 0
        total_bytes = 0
        findings: list[str] = []
        for item in items:
            if not item.source.exists():
                if item.required:
                    raise FileNotFoundError(f"live root is missing {item.source}")
                skipped += 1
                continue
            item.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if item.kind == "sqlite":
                total_bytes += backup_sqlite(item.source, item.destination)
            elif item.kind == "tree":
                total_bytes += copy_tree(item.source, item.destination)
            else:
                shutil.copy2(item.source, item.destination)
                total_bytes += item.source.stat().st_size
            copied += 1
        # The writer must bind its own socket; a copied one is a dead inode.
        for stale in (self.temp_state / "run").glob("*.sock"):
            stale.unlink()
        orphans = orphan_live_records(
            self.live_root / STATE_SUBDIR / "connector-governance", REPO_ROOT
        )
        if orphans:
            findings.append(
                "live governance records with no committed source (install.sh "
                "cannot re-create these; back them up): " + ", ".join(orphans)
            )
        return (
            f"{copied} copied, {skipped} absent, {total_bytes / 1e6:.0f} MB",
            findings,
        )

    # -- 2. config ----------------------------------------------------------

    def rewrite_config(self) -> tuple[str, list[str]]:
        raw = json.loads(self.temp_config.read_text(encoding="utf-8"))
        rewritten = rewrite_paths(raw, self.replacements)
        # Nothing else is edited.  Disabling the outbox looked prudent and was
        # wrong twice over: ``ServiceConfig`` refuses an enabled weekly-brief
        # coordinator with no outbox bridge, so the rehearsal failed to parse
        # its own config -- and more importantly, a rehearsal that trims the
        # config is no longer rehearsing the config the deploy will run.  The
        # blocks that reach the network (outbox, agenda, the static-dashboard
        # publisher) belong to ``daltond``, and the rehearsal never starts it:
        # it starts the writer and calls ``run_once`` directly.
        self.temp_config.write_text(
            json.dumps(rewritten, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.temp_config, 0o600)
        remaining = [
            line for line in json.dumps(rewritten, sort_keys=True).split('"')
            if line.startswith(str(self.live_root)) or line.startswith(str(Path.home() / ".openclaw"))
        ]
        findings = (
            [f"service.json still names {len(remaining)} live paths after rewrite: "
             + ", ".join(sorted(set(remaining))[:4])]
            if remaining else []
        )
        return f"{self.temp_config} rewritten onto the temp root", findings

    # -- 3. bootstrap -------------------------------------------------------

    def run_bootstrap(self) -> tuple[str, list[str]]:
        from dalton_core.bootstrap import bootstrap

        result = bootstrap(self.temp_state, self.temp_config)
        return f"core={Path(result['core_db']).name} tokens={Path(result['token_config']).name}", []

    # -- 4. migrations ------------------------------------------------------

    def run_migrations(self) -> tuple[str, list[str]]:
        import importlib

        from dalton_core.store import DaltonStore

        findings: list[str] = []
        applied = 0
        package_dir = Path(importlib.import_module("dalton_core").__file__).parent
        unowned = package_schema_files(package_dir) - known_schema_files()
        if unowned:
            findings.append(
                "schema files this rehearsal does not know an owner for: "
                + ", ".join(sorted(unowned))
            )
        core_path = self.temp_state / "core.sqlite"
        scheduler_path = self.temp_state / "scheduler.sqlite"
        with DaltonStore(core_path) as store:
            from dalton_core.scheduler import Scheduler

            with Scheduler(scheduler_path) as scheduler:
                for spec in CORE_MIGRATIONS:
                    if spec.kind == "root":
                        applied += 1
                        continue
                    symbol = getattr(importlib.import_module(spec.module), spec.symbol)
                    try:
                        _construct_core_authority(symbol, store, scheduler)
                    except Exception as exc:  # noqa: BLE001
                        findings.append(
                            f"{spec.schema} ({spec.symbol}) failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    applied += 1
        # A sidecar the live Core does not have is applied into a scratch
        # database instead of the state directory: the SQL still runs and a
        # broken schema still fails here, but the rehearsal does not leave
        # behind an empty ``document-index.sqlite`` that the real deploy would
        # never create and an operator would later have to explain.
        probe_dir = self.temp_state / "migration-probe"
        probe_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for spec in SIDECAR_MIGRATIONS:
            symbol = getattr(importlib.import_module(spec.module), spec.symbol)
            path = self.temp_state / spec.database
            if not path.exists():
                path = probe_dir / Path(spec.database).name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                closer = _construct_sidecar(symbol, path)
                close = getattr(closer, "close", None)
                if callable(close):
                    close()
            except Exception as exc:  # noqa: BLE001
                findings.append(
                    f"{spec.schema} ({spec.symbol}) failed: {type(exc).__name__}: {exc}"
                )
                continue
            applied += 1
        findings.extend(self._check_pool_migration())
        findings.extend(self._check_deliverable_check())
        return f"{applied}/{len(CORE_MIGRATIONS) + len(SIDECAR_MIGRATIONS)} schemas applied", findings

    def _check_pool_migration(self) -> list[str]:
        """C2: the nullable ``pool`` columns, and what a pre-migration admission
        replays as.

        The migration is additive, so an admission written before it has
        ``pool IS NULL``.  ``day_pool_spend`` reports those under ``unpooled``
        rather than guessing a pool for them, which is the behaviour the
        runbook has to warn about: on the day of the deploy the pool figures
        are split between the real pools and ``unpooled`` until every
        pre-migration admission has settled.
        """

        from dalton_core.budget_pools import has_pool_columns

        path = self.temp_state / "thesis-impact-budget.sqlite"
        if not path.exists():
            return ["no thesis-impact-budget.sqlite: the pool migration had nothing to migrate"]
        findings: list[str] = []
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            if not has_pool_columns(connection):
                return ["thesis-impact-budget.sqlite has no pool column after the migration"]
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "model_budget_pool_rejections" not in tables:
                findings.append("model_budget_pool_rejections is missing after the migration")
            if "thesis_impact_day_admissions" in tables:
                unpooled = connection.execute(
                    "SELECT COUNT(*) FROM thesis_impact_day_admissions WHERE pool IS NULL"
                ).fetchone()[0]
                total = connection.execute(
                    "SELECT COUNT(*) FROM thesis_impact_day_admissions"
                ).fetchone()[0]
                if unpooled:
                    findings.append(
                        f"pre-migration admissions: {unpooled}/{total} rows have "
                        "pool IS NULL and replay as 'unpooled' in pool_status "
                        "until they settle -- expected, and the reason the "
                        "first day's pool figures do not add up to the day cap"
                    )
        return findings

    def _check_deliverable_check(self) -> list[str]:
        """The ``mission_deliverable`` CHECK has to admit *every* declared kind.

        This checked one literal, ``event_note``, because that was the only
        kind P14a added.  Three slices have added kinds since, and a rehearsal
        that only ever looks for the first one would pass a Core migrated as
        far as P14a and no further -- which is exactly the failure the
        migration exists to prevent, arriving as an ``IntegrityError`` from a
        constraint the first time somebody publishes the newest kind.  The
        vocabulary is the list, so the vocabulary is what is checked.
        """

        from dalton_core.mission_deliverable import DELIVERABLE_KINDS

        path = self.temp_state / "core.sqlite"
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='mission_deliverable_versions'"
            ).fetchone()
        if row is None:
            return ["mission_deliverable_versions does not exist after the migration"]
        sql = row[0] or ""
        missing = [kind for kind in DELIVERABLE_KINDS if f"'{kind}'" not in sql]
        if missing:
            return [
                "mission_deliverable_versions CHECK still refuses "
                + ", ".join(repr(kind) for kind in missing)
            ]
        return []

    # -- 5. seeds -----------------------------------------------------------

    def run_seeds(self) -> tuple[str, list[str]]:
        seeded = present = missing = 0
        findings: list[str] = []
        shut: dict[str, list[str]] = {}
        for spec in INSTALL_SEEDS:
            source = REPO_ROOT / spec.repo
            destination = self.temp_state / spec.state
            if destination.exists():
                present += 1
                continue
            if not source.exists():
                findings.append(f"install.sh would fail: {spec.repo} is not in the repo")
                missing += 1
                continue
            # A gated seed is skipped exactly as install.sh skips it.  Copying
            # it anyway would install half a lane and give the rehearsal a tick
            # table this machine will never produce.
            is_open, why = gate_open(spec.gate, os.environ)
            if not is_open:
                shut.setdefault(f"{spec.gate}: {why}", []).append(Path(spec.state).name)
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
            seeded += 1
        for why, names in sorted(shut.items()):
            findings.append(
                f"gate shut, {len(names)} seed(s) not installed -- {why} "
                f"({', '.join(sorted(names))})"
            )
        unseeded = unseeded_governance_records(REPO_ROOT)
        if unseeded:
            findings.append(
                "committed governance records install.sh neither seeds nor "
                "names in DELIBERATELY_UNSEEDED (their lanes stay "
                "unconfigured with nobody having decided that): "
                + ", ".join(unseeded)
            )
        named = deliberately_unseeded_records(
            REPO_ROOT / "deploy" / "macos" / "install.sh")
        if named:
            findings.append(
                "deliberately not seeded, with a reason in install.sh: "
                + ", ".join(sorted(named))
            )
        return (
            f"{seeded} seeded, {present} already present, "
            f"{sum(len(n) for n in shut.values())} gated out, {missing} absent from repo",
            findings,
        )

    def _check_plist_referenced_seeds(self) -> list[str]:
        """Governance records the writer's plist names but nothing seeds.

        A missing record here is not a lane that stays quiet: it is a writer
        argument pointing at a path that does not exist.
        """

        plist_path = self.launch_agents_dir / "space.lumos.dalton.writer.plist"
        if not plist_path.exists():
            return []
        argv = plistlib.loads(plist_path.read_bytes())["ProgramArguments"]
        findings: list[str] = []
        for value in argv:
            if not isinstance(value, str) or "/connector-governance/" not in value:
                continue
            if Path(value).exists():
                continue
            name = Path(value).name
            committed = (REPO_ROOT / "deploy" / "connector-governance" / name).exists()
            findings.append(
                f"writer plist names {name}, which is absent"
                + ("; the repo has it but install.sh does not seed it"
                   if committed else " and the repo does not carry it either")
            )
        return findings

    # -- 6. catalog sync ----------------------------------------------------

    def run_catalog_sync(self) -> tuple[str, list[str]]:
        router_db = self.temp_state / "model-router.sqlite"
        if not router_db.exists():
            return ("no model-router.sqlite in the copy; sync skipped", [
                "install.sh would skip the catalog sync too, but a Core with a "
                "gateway and no router is already broken"
            ])
        if not self.openclaw_config.is_file():
            return ("no ~/.openclaw/openclaw.json; install.sh skips the sync", [])
        command = [
            sys.executable, str(REPO_ROOT / "scripts" / "sync_openclaw_model_catalog.py"),
            "--openclaw-config", str(self.openclaw_config),
            "--model-router-db", str(router_db),
        ]
        environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
        completed = subprocess.run(
            command, capture_output=True, text=True, env=environment, timeout=600,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"catalog sync exited {completed.returncode}: "
                f"{completed.stderr.strip()[:400]}"
            )
        report = json.loads(completed.stdout)
        self.catalog_report = report
        findings: list[str] = []
        for key in ("registered", "retired", "revived"):
            values = report.get(key) or []
            if values:
                findings.append(f"catalog {key}: " + ", ".join(str(v) for v in values))
        # Idempotence: install.sh runs this on every deploy, so a second run
        # that changes something would mean the deploy never converges.
        again = subprocess.run(
            command, capture_output=True, text=True, env=environment, timeout=600,
        )
        if again.returncode == 0:
            second = json.loads(again.stdout)
            for key in ("registered", "retired", "revived"):
                if second.get(key):
                    findings.append(
                        f"catalog sync is not idempotent: a second run still {key} "
                        + ", ".join(str(v) for v in second[key])
                    )
        findings.extend(self._check_verifier_pin(router_db))
        return json.dumps(
            {k: v for k, v in report.items() if k != "profiles"}, sort_keys=True
        )[:200], findings

    def _check_verifier_pin(self, router_db: Path) -> list[str]:
        """P14-M: the sync retires the profile the verifier phase pin names.

        ``VERIFIER_POLICY_REF`` pins ``profile:gemini-3-7-flash``, which the
        broker no longer offers.  After the sync that profile carries a
        ``retired`` version, and verifier routing is *refused* with
        ``profile_retired`` rather than failing at the broker.  That is a
        better failure, but it is still a failure, and repointing an immutable
        phase pin is the owner's decision -- so the rehearsal has to say so out
        loud rather than let the deploy discover it.
        """

        from dalton_core.model_deployment import VERIFIER_POLICY_REF, VERIFIER_PROFILE_ID
        from dalton_core.model_router import ModelRouter

        with ModelRouter(router_db, read_only=True) as router:
            latest = {
                profile.get("id"): profile for profile in router.latest_profiles()
            }
        profile = latest.get(VERIFIER_PROFILE_ID)
        if profile is None:
            return [
                f"verifier phase pin {VERIFIER_POLICY_REF} names "
                f"{VERIFIER_PROFILE_ID}, which this router does not carry at all"
            ]
        status = profile.get("status")
        if status == "retired":
            return [
                f"verifier phase pin {VERIFIER_POLICY_REF} names {VERIFIER_PROFILE_ID}, "
                "which this sync retired; verifier routing will be refused with "
                "profile_retired until the owner repoints the pin at the "
                "verifier tier chain"
            ]
        return []

    # -- mission grants -----------------------------------------------------

    def check_mission(self) -> tuple[str, list[str]]:
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.store import DaltonStore

        with DaltonStore(self.temp_state / "core.sqlite") as store:
            missions = CoverageMissionAuthority(store)
            row = store.connection.execute(
                "SELECT mission_ref FROM coverage_mission_versions "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return ("no mission version on this Core", [
                    "no coverage mission: every lane will report ungranted"
                ])
            mission = missions.active_mission(row["mission_ref"])
        record = mission.get("record") or mission
        autonomy = record.get("autonomy") or {}
        granted = list(autonomy.get("may_write") or [])
        checkpoints = list(autonomy.get("human_checkpoints") or [])
        self.mission_granted = granted
        missing = missing_write_scopes(granted)
        findings = [
            f"mission {mission.get('mission_version_id') or mission.get('id')} "
            f"does not grant may_write:{word} -- {why}"
            for word, why in missing
        ]
        if "thesis_revision_candidate" not in checkpoints:
            findings.append(
                "mission human_checkpoints lacks thesis_revision_candidate "
                "(ADR-0007); revise_thesis records queued instead of proposing"
            )
        return (
            f"may_write grants {len(granted)}, missing {len(missing)}",
            findings,
        )

    def check_lane_switches(self) -> tuple[str, list[str]]:
        missing = missing_lane_switches(self.temp_state)
        findings = [
            f"{switch.lane}: {switch.state_file} absent"
            + (f", repo has {switch.repo_source}" if switch.repo_source else "")
            + (f" ({switch.note})" if switch.note else "")
            for switch in missing
            if not switch.seeded_by_install
        ]
        broken = [
            f"{switch.lane}: install.sh claims to write {switch.state_file} and did not"
            for switch in missing if switch.seeded_by_install
        ]
        return (
            f"{len(LANE_SWITCHES) - len(missing)}/{len(LANE_SWITCHES)} lane switches on disk",
            broken + findings,
        )

    # -- 7. plists ----------------------------------------------------------

    def render_plists(self) -> tuple[str, list[str]]:
        from dalton_core.macos_launchagent import render

        # The broker sockets have to exist *before* the render, not before the
        # writer: ``macos_launchagent.render`` passes
        # ``--web-search-broker-socket`` only when the file is on disk, so
        # rendering first made the rehearsal's own plist differ from the live
        # one in five arguments that the deploy does not actually change.
        if self._broker is None:
            self._broker = StubBroker(self.stub_broker_dir)
        self.launch_agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        rendered = render(
            self.launch_agents_dir,
            Path(sys.executable).parent,
            self.temp_state,
            self.temp_config,
            self.log_dir,
        )
        findings: list[str] = []
        # Three things differ between a rehearsal and a real install for
        # reasons that are not the deploy's: the venv, the log directory
        # (install.sh uses ~/Library/Logs/Dalton, which is outside the root
        # this script is allowed to write to) and the root itself.  All three
        # are mapped back, so what is left in the diff is only what the deploy
        # would really change.
        back = invert(self.replacements) | {
            str(Path(sys.executable).parent): "<venv>/bin",
            str(self.log_dir): str(Path.home() / "Library" / "Logs" / "Dalton"),
        }
        live_agents = Path.home() / "Library" / "LaunchAgents"
        for label in (
            "space.lumos.dalton.writer", "space.lumos.dalton.controller",
            "space.lumos.dalton.control", "space.lumos.dalton.thesis-impact",
        ):
            new_path = self.launch_agents_dir / f"{label}.plist"
            old_path = live_agents / f"{label}.plist"
            if not new_path.exists() and not old_path.exists():
                continue
            if not old_path.exists():
                findings.append(f"{label}.plist is new: this deploy installs it")
                continue
            if not new_path.exists():
                findings.append(f"{label}.plist would be removed by this deploy")
                continue
            old = rewrite_paths(
                plistlib.loads(old_path.read_bytes()),
                {str(Path.home() / "Library" / "Application Support" / "Dalton"
                     / "runtime" / "venv" / "bin"): "<venv>/bin"},
            )
            new = normalise_plist(plistlib.loads(new_path.read_bytes()), back)
            for line in plist_diff(old, new):
                findings.append(f"{label}: {line}")
        findings.extend(self._check_plist_referenced_seeds())
        return f"{len(rendered)} plists rendered into {self.launch_agents_dir}", findings

    # -- 8. writer ----------------------------------------------------------

    def start_writer(self) -> tuple[str, list[str]]:
        plist = plistlib.loads(
            (self.launch_agents_dir / "space.lumos.dalton.writer.plist").read_bytes()
        )
        argv = list(plist["ProgramArguments"])
        # The plist names the installed console script, which on a developer
        # machine resolves dalton_core out of whatever the venv was installed
        # from -- not this worktree.  Same module, same arguments, explicit
        # PYTHONPATH.
        argv[0:1] = [sys.executable, "-m", "dalton_core.writer_server"]
        self._broker = StubBroker(self.stub_broker_dir)
        (self.temp_state / "run").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._writer_log = open(self.log_dir / "writer.log", "wb")
        self._writer = subprocess.Popen(
            argv,
            stdout=self._writer_log,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"), PYTHONUNBUFFERED="1"),
            cwd=str(self.temp_state),
        )
        socket_path = self.temp_state / "run" / "writer.sock"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if socket_path.exists():
                break
            if self._writer.poll() is not None:
                tail = (self.log_dir / "writer.log").read_text(errors="replace")[-1500:]
                raise RuntimeError(
                    f"writer exited {self._writer.returncode} before binding:\n{tail}"
                )
            time.sleep(0.25)
        else:
            raise TimeoutError("writer did not bind its socket within 120s")
        return f"writer pid {self._writer.pid}, socket bound", []

    def stop_writer(self) -> None:
        if self._writer is not None and self._writer.poll() is None:
            self._writer.terminate()
            try:
                self._writer.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                self._writer.kill()
        writer_log = getattr(self, "_writer_log", None)
        if writer_log is not None:
            writer_log.close()
        if self._broker is not None:
            self._broker.close()

    # -- 9. one tick --------------------------------------------------------

    def run_tick(self) -> tuple[str, list[str]]:
        from dalton_core.bounded_planner_driver import (
            BoundedPlannerDriver,
            BoundedPlannerDriverConfig,
        )
        from dalton_core.lane_registry import tick_lanes
        from dalton_core.service import ServiceConfig

        service = ServiceConfig.from_file(self.temp_config)
        raw = json.loads(self.temp_config.read_text(encoding="utf-8"))
        block = dict(raw["bounded_planner"]["config"])
        config = BoundedPlannerDriverConfig.from_mapping(block)
        driver = BoundedPlannerDriver(config, transport=RefusingTransport())
        started = time.monotonic()
        self.tick_summary = driver.run_once()
        elapsed = time.monotonic() - started
        operations = {
            spec.driver_key: spec.operation for spec in tick_lanes()
        }
        self.rows = lane_rows(self.tick_summary, operations)
        bad = escaped_rows(self.rows)
        findings = [
            f"{row.lane} escaped its lane: {row.status} {row.reason}".strip()
            for row in bad
        ]
        self.log("\n" + render_table(self.rows))
        return (
            f"{len(self.rows)} entries, {len(bad)} escaped, {elapsed:.1f}s "
            f"(service tick_seconds={service.tick_seconds})",
            findings,
        )

    # -- run ----------------------------------------------------------------

    def run(self) -> int:
        self.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.step("copy live state (read-only)", self.copy_state)
            self.step("rewrite service.json onto the temp root", self.rewrite_config)
            self.step("dalton-bootstrap", self.run_bootstrap)
            self.step("migrations (every *_schema.sql)", self.run_migrations)
            # install.sh's order, and it matters: several lanes' ``argv_fragment``
            # turns the lane on only when its governance record is already on
            # disk, so rendering the plists before seeding silently produces a
            # writer with the market-price, catalyst and SEC-filings lanes
            # missing.  The first run of this script did exactly that and
            # reported ``mission_market_prices: unconfigured`` as though the
            # deploy were at fault.
            self.step("governance seeds", self.run_seeds)
            self.step("model catalog sync (copy of model-router.sqlite)", self.run_catalog_sync)
            self.step("render LaunchAgent plists and diff", self.render_plists)
            self.step("mission grants (autonomy.may_write)", self.check_mission)
            self.step("lane switches on disk", self.check_lane_switches)
            self.step("start writer against the temp Core", self.start_writer)
            self.step("one controller tick", self.run_tick)
        finally:
            self.stop_writer()
        return 0 if all(step.ok for step in self.steps) else 1

    def report(self) -> str:
        lines = ["", "=" * 72, "rehearsal summary", "=" * 72]
        for step in self.steps:
            lines.append(
                f"{'ok ' if step.ok else 'FAIL'} {step.seconds:6.1f}s  {step.name}"
                + (f" -- {step.detail}" if step.detail else "")
            )
        findings = [
            f"{step.name}: {finding}" for step in self.steps for finding in step.findings
        ]
        lines.append("")
        lines.append(f"findings ({len(findings)}):")
        lines.extend(f"  - {finding}" for finding in findings) or lines.append("  none")
        if self.rows:
            lines.extend(["", "lane table:", render_table(self.rows)])
        return "\n".join(lines)


def _construct_core_authority(symbol: Any, store: Any, scheduler: Any) -> Any:
    """Build one core authority with whatever collaborators it declares.

    Three of the forty take more than a store, and all three take things this
    rehearsal already has open.  Anything else is a new shape and should fail
    loudly rather than be guessed at.
    """

    from dalton_core.agenda import AgendaStore
    from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
    from dalton_core.industry_research import IndustryResearchAuthority
    from dalton_core.research_question_backlog import ResearchQuestionBacklog

    name = symbol.__name__
    if name == "WeeklyBriefAuthority":
        return symbol(store, IndustryResearchAuthority(store))
    if name == "ThesisImpactAuthority":
        return symbol(store, scheduler)
    if name == "AnswerRoutingAuthority":
        return symbol(
            store,
            AgendaStore(store),
            ResearchQuestionBacklog(store),
            BoundedPlannerAuthority(store),
            IndustryResearchAuthority(store),
        )
    # Four authorities take a resolver they only call at *use* time.  A
    # rehearsal applies the schema and never calls them, so a resolver that
    # raises is both honest and safe: if one of them ever started reading
    # during construction, this would say so rather than silently sample live
    # artefacts.
    if name in _CONNECTION_AUTHORITIES:
        return symbol(store.connection)
    if name in _RESOLVER_KWARGS:
        kwargs: dict[str, Any] = {}
        for keyword in _RESOLVER_KWARGS[name]:
            kwargs[keyword] = (
                _rehearsal_raw_spool(Path(store.connection.execute(
                    "PRAGMA database_list").fetchone()["file"]).parent)
                if keyword == "spool" else _refusing_resolver(f"{name}.{keyword}")
            )
        return symbol(store, **kwargs)
    return symbol(store)


def _rehearsal_raw_spool(directory: Path) -> Any:
    """A real spool rooted in the rehearsal directory.

    ``RawSpool`` is type-checked by its callers rather than duck-typed, so a
    stub will not do; it is also purely a directory, so pointing one at the
    temp root costs nothing and touches nothing live.
    """

    from dalton_core.raw_spool import RawSpool

    root = directory / "migration-probe" / "raw-spool"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return RawSpool(root, max_total_bytes=1 << 20)


#: Keyword-only collaborators that are pure call-time resolvers.
#: Core authorities constructed on ``store.connection`` rather than ``store``.
#: Same database, same migration, different constructor -- worth naming rather
#: than sniffing, so that a class that grows a store argument later fails here
#: instead of quietly being handed the wrong object.
_CONNECTION_AUTHORITIES: frozenset[str] = frozenset({"DocumentProvenanceStore"})

_RESOLVER_KWARGS: dict[str, tuple[str, ...]] = {
    "CredentialAuthorityStore": ("handle_resolver",),
    "StatementSnapshotAuthority": ("artifact_resolver",),
    "TranscriptCorrectionAuthority": ("spool", "manifest_resolver", "evidence_resolver"),
    "TranscriptPolishAuthority": ("spool", "manifest_resolver"),
}


def _refusing_resolver(name: str) -> Callable[..., Any]:
    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"rehearsal: {name} must not be called during a migration")

    return _refuse


#: Sidecar stores whose constructors take more than a path.  Same rule: the
#: extra arguments are call-time collaborators or descriptive strings, never
#: something the schema application reads.
_SIDECAR_KWARGS: dict[str, dict[str, Any]] = {
    "OpenClawMetadataExporter": {
        "source_instance_ref": "rehearsal:dalton-core",
        "openclaw_version": "rehearsal:0",
        "exporter_version": "rehearsal:0",
    },
}


def _construct_sidecar(symbol: Any, path: Path) -> Any:
    if symbol.__name__ == "DocumentIndex":
        from dalton_core.observability import ObservabilityStore
        from dalton_core.store import DaltonStore

        store = DaltonStore(path.with_name(path.stem + "-observability.sqlite"))
        return symbol(
            path,
            observability=ObservabilityStore(store),
            raw_spool=_rehearsal_raw_spool(path.parent.parent),
        )
    extra = _SIDECAR_KWARGS.get(symbol.__name__)
    return symbol(path, **extra) if extra else symbol(path)


# ---------------------------------------------------------------------------
# impure helpers
# ---------------------------------------------------------------------------


def backup_sqlite(source: Path, destination: Path) -> int:
    """Copy a possibly-live SQLite database without writing to it.

    ``mode=ro`` plus the backup API is the pattern the repo's other live
    canaries use: it takes a read lock, follows the WAL, and produces a
    consistent snapshot rather than the three-file ``cp`` that can hand you a
    header from before a checkpoint and pages from after it.
    """

    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=60)
    try:
        writer = sqlite3.connect(destination)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()
    return destination.stat().st_size


def copy_tree(source: Path, destination: Path) -> int:
    total = 0
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        if copy_excluded(path):
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(path, target)
        total += path.stat().st_size
    return total


class RefusingTransport:
    """A probe transport that refuses instead of reaching the network.

    ``BoundedPlannerDriver`` defaults to ``PublicHttpTransport``.  A rehearsal
    that fetched anything from SEC would not be a rehearsal.  The driver wraps
    probe execution in its own try/except and turns the refusal into a probe
    envelope, so this exercises the refusal path rather than short-circuiting
    the tick.
    """

    def fetch(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("rehearsal: no network")

    def __getattr__(self, name: str) -> Any:
        def _refuse(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"rehearsal: no network ({name})")

        return _refuse


class StubBroker:
    """A local stand-in for the OpenClaw model broker socket.

    Line-delimited JSON in, one refusal out.  It exists so the writer's model
    path is *wired* during the rehearsal -- the socket is there, the auth key
    is there, the client connects and gets an answer -- while no model call
    leaves the machine and the live ``~/.openclaw/dalton-*.sock`` is never
    opened.  A lane whose only failure mode is "the broker said no" is exactly
    what a Core with no approvals should look like.
    """

    REFUSAL = {
        "status": "error",
        "error": {"code": "rehearsal_stub", "message": "rehearsal broker: no model calls"},
    }

    def __init__(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.paths: list[Path] = []
        self._servers: list[socket.socket] = []
        self._closed = threading.Event()
        for name in ("dalton-model-broker.sock", "dalton-web-search-broker.sock"):
            path = directory / name
            if path.exists():
                path.unlink()
            key = directory / f"{name}.key"
            if not key.exists():
                key.write_text("0" * 64 + "\n", encoding="utf-8")
                os.chmod(key, 0o600)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(os.fspath(path))
            os.chmod(path, 0o600)
            server.listen(8)
            self._servers.append(server)
            self.paths.append(path)
            threading.Thread(target=self._serve, args=(server,), daemon=True).start()

    def _serve(self, server: socket.socket) -> None:
        while not self._closed.is_set():
            try:
                client, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        with client:
            buffer = bytearray()
            client.settimeout(10)
            try:
                while b"\n" not in buffer:
                    chunk = client.recv(16_384)
                    if not chunk:
                        return
                    buffer.extend(chunk)
                client.sendall(
                    json.dumps(self.REFUSAL, sort_keys=True).encode("utf-8") + b"\n"
                )
            except OSError:
                return

    def close(self) -> None:
        self._closed.set()
        for server in self._servers:
            try:
                server.close()
            except OSError:  # pragma: no cover - defensive
                pass
        for path in self.paths:
            try:
                path.unlink()
            except FileNotFoundError:  # pragma: no cover - defensive
                pass


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live-root", type=Path,
        default=Path.home() / "Library" / "Application Support" / "Dalton",
        help="the live Dalton root; opened read-only and never written to",
    )
    parser.add_argument(
        "--temp-root", type=Path, default=None,
        help="where to rehearse; defaults to /tmp/dalton-rehearsal-<utc timestamp>",
    )
    parser.add_argument(
        "--openclaw-config", type=Path, default=Path.home() / ".openclaw" / "openclaw.json",
        help="read-only source of the broker's model catalog",
    )
    parser.add_argument("--report", type=Path, default=None, help="also write the summary here")
    args = parser.parse_args(argv)
    temp_root = args.temp_root or Path("/tmp") / (
        "dalton-rehearsal-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    if not str(temp_root).startswith(("/tmp/", "/private/tmp/", "/var/folders/")):
        raise SystemExit("--temp-root must live under /tmp; this script writes there only")
    if temp_root.resolve() == args.live_root.expanduser().resolve():
        raise SystemExit("--temp-root must not be the live root")
    rehearsal = Rehearsal(args.live_root, temp_root, openclaw_config=args.openclaw_config)
    code = rehearsal.run()
    summary = rehearsal.report()
    print(summary)
    if args.report is not None:
        args.report.write_text(summary + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
