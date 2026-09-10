"""W3: the tick that reads the fund's own earlier work on a company.

Order 32, between source discovery (30) and everything that finds things
outside the building. That placement is the design in one number: on a company
we already have a file on, the first thing to read is the file, and paying a
vendor to tell us what we already wrote down is the waste this lane exists to
avoid.

It is S1's feed lane in every mechanical respect and reuses it outright --
enumerate a bounded window, record one discovery per *document* because for a
local feed the acquisition is the discovery, acquire, review. Three things are
its own:

**One shot per document, keyed by what the document says.** The child hashes
the rendered text and the ticket digest is content-addressed, so a document
re-read after the owner touched nothing lands on the same ticket rather than
on a second child. A document whose body changed is a different body and is
read again -- which is right: somebody edited the memo.

**Two grants, and they are not the same one.** ``source_discovery`` and
``observation`` are what reading the feed needs, and they are checked by the
mission authority the same way every discovery is. Importing a prior Initial
Screen as v0 needs ``deliverable`` on top of those, and that is checked where
the import happens rather than here, so a mission that grants the first two
still gets its notes and memos read while the screen import waits for a
mission version that grants the third.

**Absent rather than broken.** No declared directory, no approved records, no
lane -- the argv fragment is empty and the writer never learns the flag. A
Core whose owner has no earlier files on these companies is not a Core with a
lane that refuses every tick.
"""

from __future__ import annotations

from typing import Any, Mapping

from .lane_registry import LaneSpec, register_lane

LAUNCHER_KWARG = "prior_research_feed_launcher"
GOVERNANCE = (
    "prior-research-list-documents-v1.json",
    "prior-research-get-document-v1.json",
)
FEED_PLAN_NAME = "p9-us-it-services-feeds-v2.json"
CORPUS_DIRNAME = "prior-research"

#: The two words reading this feed needs. The same pair every discovery source
#: needs, and named here so the lane's test can assert they are in the
#: mission vocabulary rather than assuming it.
WRITE_SCOPES: tuple[str, ...] = ("source_discovery", "observation")
#: What importing a prior Initial Screen as version 0 needs on top. Checked at
#: the import, not at the tick: reading the feed and writing a deliverable are
#: different permissions and a mission may reasonably grant one without the
#: other.
IMPORT_WRITE_SCOPE = "deliverable"


def may_read_prior_research(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission grants what reading the prior corpus needs."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, (list, tuple)):
        return False
    return set(WRITE_SCOPES) <= set(scopes)


def may_import_prior_screen(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission also grants the deliverable write the import needs."""

    if not may_read_prior_research(mission):
        return False
    return IMPORT_WRITE_SCOPE in set(mission["autonomy"]["may_write"])


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick: read what this fund already wrote, and queue it."""

    from .mission_feed_lane import PRIOR_RESEARCH_SOURCE_REF, _dispatch

    return _dispatch(server, PRIOR_RESEARCH_SOURCE_REF, LAUNCHER_KWARG)


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--prior-research-corpus-root",
        help="the declared directory of prior work (DALTON_PRIOR_RESEARCH_DIR), "
             "one folder per company, each with a manifest.json",
    )
    parser.add_argument("--prior-research-governance-list")
    parser.add_argument("--prior-research-governance-get")


def build_launcher(args: Any) -> Any | None:
    from pathlib import Path as _Path

    from .mission_feed_lane import _build
    from .prior_research_launcher import PriorResearchFeedLauncher

    state = _Path(args.db).expanduser().resolve().parent
    return _build(
        PriorResearchFeedLauncher,
        plan=getattr(args, "feed_discovery_plan", None),
        spool_dir=getattr(args, "transcript_spool", None),
        state_dir=state,
        governance_paths={
            "list_documents": getattr(args, "prior_research_governance_list", None),
            "get_document": getattr(args, "prior_research_governance_get", None),
        },
        corpus_root=getattr(args, "prior_research_corpus_root", None),
    )


def argv_fragment(context: Any) -> list[str]:
    """Every file this lane needs, or none of them.

    All-or-nothing on the code side of INT2's rule, and it keeps
    ``--feed-discovery-plan``. The company-wiki fragment drops that flag
    because the sales-note lane at order 120 always renders before it; this
    lane is at 32 and renders *first*, and a Core whose owner set only
    ``DALTON_PRIOR_RESEARCH_DIR`` -- no OpenClaw workspace at all -- has no
    other lane to supply it. Dropping it there would mean the one lane that is
    installed comes up with no plan and refuses every tick.

    The duplicate the wiki fragment was avoiding is handled where duplicates
    belong: ``lane_argv`` de-duplicates the flag across fragments.
    """

    from .mission_feed_lane import _feed_argv

    corpus = context.state / "feeds" / CORPUS_DIRNAME
    return _feed_argv(
        context, plan_name=FEED_PLAN_NAME, governance=GOVERNANCE,
        flags=[
            ("--prior-research-corpus-root", corpus),
            ("--prior-research-governance-list",
             context.state / "connector-governance" / GOVERNANCE[0]),
            ("--prior-research-governance-get",
             context.state / "connector-governance" / GOVERNANCE[1]),
        ],
    )


LANE = register_lane(LaneSpec(
    operation="dispatch_prior_research",
    # 32: after source discovery at 30 and before every source outside this
    # building. On a company we already have a file on, the file is the first
    # thing to read.
    order=32,
    driver_key="prior_research",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="W3: the fund's own earlier screens, memos, notes and models.",
))


__all__ = [
    "CORPUS_DIRNAME",
    "FEED_PLAN_NAME",
    "GOVERNANCE",
    "IMPORT_WRITE_SCOPE",
    "LANE",
    "LAUNCHER_KWARG",
    "WRITE_SCOPES",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "may_import_prior_screen",
    "may_read_prior_research",
]
