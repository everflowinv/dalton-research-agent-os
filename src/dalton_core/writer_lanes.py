"""The lanes the writer implements itself, declared to the lane registry.

Four of the controller's lanes have no launcher of their own and no
installation switch.  Source discovery drives three coordinators built from
three plan files; document extraction runs on the coordinator the writer
builds at ``start()`` from the extraction model configuration; the stage
driver and the claim reviewer are pure reads of the writer's own store and
transcript spool.  Moving their bodies into separate modules would buy nothing
and would drag half of ``writer_server``'s private state out with them, so
they keep their ``_op_*`` methods there and declare ``handler=None``.

What they do get from the registry is what they used to repeat by hand: their
membership in ``CORE_DISCOVERY_OPERATIONS`` and ``CORE_OPERATIONS``, their
``OPERATION_FIELDS`` entry, and their position and key in the controller
tick's summary.  So the tick's lane order is readable in one place rather than
inferred from the order of eleven ``try`` blocks.
"""

from __future__ import annotations

from .lane_registry import LaneSpec, register_lane

register_lane(LaneSpec(
    operation="dispatch_mission_source_discovery",
    order=30,
    driver_key="mission_source_discovery",
    note="P9d-1: AlphaEngine, the SEC filings index and web search, one "
         "deadline shared by all three; the writer owns all three "
         "coordinators and their plan paths.",
))
register_lane(LaneSpec(
    operation="dispatch_document_extraction",
    order=40,
    driver_key="document_extraction",
    note="ADR-0005: drafts awaiting documents under the mission grant. Its "
         "coordinator is built at writer start() from the extraction model "
         "configuration and its window bounds.",
))
register_lane(LaneSpec(
    operation="dispatch_mission_stage",
    order=50,
    driver_key="mission_stage",
    note="P10a: enters the Playbook's first stage for any company that has "
         "none. No launcher; a read of the mission tables.",
))
register_lane(LaneSpec(
    operation="dispatch_claim_review",
    order=60,
    driver_key="claim_review",
    param_fields=frozenset({"max_claims"}),
    note="P10b: reads admitted Claims back against the originals they cite. "
         "Needs the transcript spool, not a launcher.",
))
