"""P12c: redraw one subject's debate map on a tick, when its evidence moved.

Queueless, like the two lanes before it, and for a sharper reason than either.
Redrawing a map is a judgement about a *state*, not a task in a backlog: what
matters is whether the evidence this subject's map was drawn from is still the
evidence we hold.  That is derived from the Ledger every tick -- the
fingerprint of the subject's canonical claim versions against the fingerprint
the current version recorded -- so there is nothing to leave stuck and no
second place where "still to do" is written down and can go stale.

The resting state is silence, and here it is the *usual* state.  A map is
redrawn when Claims arrive, which for a covered company is a few times a week;
between those the lane says "nothing moved", which is the correct answer and
costs one projection read.  A lane that redrew a map every tick would produce a
version chain in which nothing can be seen to change, which is the exact
failure ADR-0008 exists to prevent.

Subjects are taken in the mission's own universe order, industry last: the
industry map reads across companies, and drawing it before the companies whose
evidence it summarises have been redrawn puts the summary in front of the
thing summarised.  The first subject whose evidence moved wins the slot.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget
from .lane_change_key import (
    ChangeKeyMemo, claim_change_keys, claim_index_change_keys, compose,
)
from .lane_permission_control import (
    authority_connection, current_permission, record_controlled_failure,
)

MAX_FAILURE_DETAIL_CHARS = 500
DRIVER_KEY = "mission_debate_map"
#: Where the change keys of the last tick survive a restart.
CHANGE_KEY_FILE = "debate-map-lane-change-keys.json"
#: What the memo holds for a subject with no Claims: a real state, not a miss.
#: ``evidence_fingerprint`` is a hex digest, so no fingerprint can collide.
NO_EVIDENCE = "no-evidence"
# Outcomes that say something about this moment rather than about this
# evidence, so the subject is not held back for them: the scheduler had the
# request in flight, or there was no route just then. The very next tick can
# do the work.
TRANSIENT_STATUSES: frozenset[str] = frozenset({"busy", "model_unavailable"})
# The one outcome that changes what the next tick sees. After it the stored
# version's fingerprint equals the evidence's, so the selector skips the
# subject on its own and no hold is needed.
PUBLISHED_STATUS = "fresh"

#: 2026-09-25: how often one subject may be redrawn.  ws-7d's AMZN was
#: drafted eleven times between 06:13 and 07:25 (~$0.52 each), alternating
#: between a duplicate and invalid JSON: every Claim that arrived moved the
#: fingerprint, and a moved fingerprint launched at once.
MIN_REDRAW_SECONDS = 6 * 3600
#: After a draft that published nothing (a duplicate, invalid JSON, a verifier
#: rejection), the wait doubles per consecutive failure, up to this.
MAX_BACKOFF_SECONDS = 48 * 3600
#: Where the pacing survives a restart: a deploy must not reopen the tap.
PACING_FILE = "debate-map-lane-pacing.json"

LAUNCHER_KWARG = "debate_map_launcher"
DEBATE_MAP_MODEL_CONFIG = "initial-screen-model-config.json"


def _business_key(subject_ref: str, fingerprint: str,
                  mission: dict[str, Any], launcher: Any = None) -> str:
    from .debate_map_draft import DRAFT_CONTRACT_HASH
    from .cockpit_model import verifier_provider_contract_fingerprint

    verifier_contract = verifier_provider_contract_fingerprint(
        "debate_map_verifier")
    from .model_route_recovery import configured_business_key
    return configured_business_key(
        f"{subject_ref}|{fingerprint}|{mission['id']}|"
        f"{mission['content_hash']}|contract:{DRAFT_CONTRACT_HASH}|"
        f"verifier_contract:{verifier_contract}", launcher
    )


def subject_change_keys(connection: Any, subjects: Any) -> dict[str, str]:
    """A cheap key per subject that moves whenever its fingerprint could.

    Narrower than the dossier's on purpose.  This fingerprint is a hash of one
    subject's canonical, unretired claim version refs and nothing else, so
    three things can move it: the Claims themselves, the index entries that
    say which of them are canonical, and retirements / reinstatements.
    Aggregating anything else here would make
    another lane's writes re-fingerprint five subjects, and a gate that opens
    for writes its lane does not read is not a gate.
    """

    claims = claim_change_keys(connection)
    entries = claim_index_change_keys(connection)
    # The fingerprint excludes retired Claims, so a retirement or a
    # reinstatement is the third thing that can move it.  Aggregated whole,
    # as the dossier's key does: both tables are small and append-only, and a
    # key that moves for another company costs one refs read, never a draft.
    from .claim_retirement import retirement_state_probe

    retirements = retirement_state_probe(connection)
    keys: dict[str, str] = {}
    for subject_ref in subjects:
        if subject_ref is None:
            continue
        label = str(subject_ref)
        keys[label] = compose([
            "debate-map-change-key:v2", label,
            claims.get(label), entries.get(label),
            retirements,
        ])
    return keys


class RedrawPacing:
    """When each subject was last drafted, and how many drafts failed since.

    Owner-only JSON beside the change-key memo; an unreadable file is an empty
    one (the lane then paces from its next draft on).
    """

    def __init__(self, path: Any | None) -> None:
        self.path = None if path is None else Path(path)
        self.state: dict[str, dict[str, Any]] = {}
        if self.path is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.state = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self.state = {}

    def get(self, subject_ref: str) -> dict[str, Any]:
        return dict(self.state.get(subject_ref) or {})

    def put(self, subject_ref: str, value: dict[str, Any]) -> None:
        self.state[subject_ref] = dict(value)
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            pass  # pacing is a brake, not an authority: held in memory still


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


class MissionDebateMapLaneCoordinator:
    """Launch and settle the debate-map lane."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        failure_ledger_dir: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.mission = mission
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._open: str | None = None
        self._open_business_key: str | None = None
        self.pacing = RedrawPacing(
            None if failure_ledger_dir is None else Path(failure_ledger_dir) / PACING_FILE)
        # Runs that failed, keyed by (subject, fingerprint), so a doomed
        # subject does not consume the slot every five minutes.  Held for this
        # process only: a restart is nearly always a deploy, which is the most
        # likely thing to have fixed whatever it was.
        self.budget = lane_budget(DRIVER_KEY, state_dir=failure_ledger_dir)
        # B1-5: the last cheap key each subject's fingerprint was computed
        # under.  Persisted, so the first tick after a deploy does not
        # re-fingerprint six subjects at once on the single store thread.
        from pathlib import Path as _Path

        self.change_keys = ChangeKeyMemo(
            None if failure_ledger_dir is None
            else _Path(failure_ledger_dir) / CHANGE_KEY_FILE)
        # How many subjects paid for a full fingerprint on the last tick.
        self.recomputed: list[str] = []

    # -- settling ---------------------------------------------------------

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now; try again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            # From the ticket, not the summary: a child that died before
            # writing one still has to be attributable to the run it was
            # spawned for, or it can never be held back from being retried.
            "subject_ref": ticket.get("subject_ref"),
            "evidence_fingerprint": ticket.get("evidence_fingerprint"),
            "map_status": summary.get("map_status"),
            "version_ref": summary.get("version_ref"),
            "debates": summary.get("debates"),
            "rejected": summary.get("rejected"),
            "cost_micros": summary.get("cost_micros"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        business_key = self._open_business_key
        self._open_business_key = None
        map_status = settled.get("map_status")
        published = (
            settled.get("status") == "succeeded" and map_status == PUBLISHED_STATUS
        )
        # Hold on *every* other outcome, not only the ones that look like
        # failures. A run that reasoned over this exact evidence and published
        # nothing -- refused, rejected, duplicate, gated -- has answered the
        # question for this evidence, and asking it again next tick would let
        # one subject take the slot forever while subjects two through five
        # never get a turn. This was the shape of the bug: ``duplicate`` was
        # treated as "not a failure, so do not hold", the fingerprint never
        # moved because nothing was published, and the lane re-asked about the
        # same company every five minutes.
        #
        # The hold is keyed on (subject, fingerprint) and therefore releases
        # itself the moment one Claim arrives, which is exactly when the
        # question is worth asking again. It is process-local as well, so a
        # deploy -- the most likely thing to have fixed a ``gated`` or a
        # ``not_authorized`` -- clears it too.
        hold = not published
        subject_ref = settled.get("subject_ref")
        fingerprint = settled.get("evidence_fingerprint")
        if subject_ref:
            self._pace_settled(str(subject_ref), published=published,
                               transient=str(map_status or settled.get("status"))
                               in TRANSIENT_STATUSES)
        if hold and subject_ref and fingerprint:
            key = business_key or f"{subject_ref}|{fingerprint}"
            reason = settled.get("failure_reason") or f"last run: {map_status or settled.get('status')}"
            settled["failure"] = record_controlled_failure(
                self.budget, key, self.mission() or {}, self.launcher,
                reason=reason, connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)), status=str(map_status or settled.get("status")),
            ).as_wire()
        elif subject_ref and fingerprint:
            settled["resumed"] = self.budget.clear(
                business_key or f"{subject_ref}|{fingerprint}")
        return settled

    # -- pacing -----------------------------------------------------------

    def _pace_settled(self, subject_ref: str, *, published: bool, transient: bool) -> None:
        """Book a finished run: a draft that published, or one that did not."""

        entry = self.pacing.get(subject_ref)
        launched = entry.pop("open_launched_at", None)
        if transient or launched is None:
            # No route, the scheduler busy: nothing was drafted, so nothing is
            # paced -- the lane's own probe rule decides when to ask again.
            self.pacing.put(subject_ref, entry)
            return
        entry["last_draft_at"] = launched
        entry["failures"] = 0 if published else int(entry.get("failures") or 0) + 1
        self.pacing.put(subject_ref, entry)

    def _pace_launched(self, subject_ref: str, urgent_key: str | None) -> None:
        entry = self.pacing.get(subject_ref)
        entry["open_launched_at"] = self.clock().astimezone(timezone.utc).isoformat()
        if urgent_key is not None:
            entry["urgent_key"] = urgent_key
        self.pacing.put(subject_ref, entry)

    @staticmethod
    def _retired_cited(current: Any, retired: set[str]) -> str | None:
        """A key for the retired Claims the published version still cites, or None."""

        if current is None or not retired:
            return None
        from .debate_map import cited_refs

        stale = sorted(cited_refs(current) & retired)
        return None if not stale else "retired:" + ",".join(stale)

    def _paced(self, subject_ref: str, urgent_key: str | None) -> str | None:
        """Why this subject may not be drafted yet, or None.

        At most one draft per subject every ``MIN_REDRAW_SECONDS``; after a
        draft that published nothing the wait doubles per consecutive failure
        (6 h, 12 h, 24 h, capped at 48 h).  The one exception to the minimum:
        the published version still cites a Claim that has since been retired
        (or whose reinstatement was withdrawn) -- that is redrawn at once, once
        per such set, and a failed attempt at it still backs off.
        """

        entry = self.pacing.get(subject_ref)
        last = _parse(entry.get("last_draft_at"))
        if last is None:
            return None
        failures = int(entry.get("failures") or 0)
        now = self.clock().astimezone(timezone.utc)
        if failures:
            wait = min(MIN_REDRAW_SECONDS * (2 ** (failures - 1)), MAX_BACKOFF_SECONDS)
            until = last + timedelta(seconds=wait)
            if now < until:
                return (f"backing off after {failures} draft(s) that published nothing; "
                        f"next draft after {until.isoformat()}")
            return None
        if urgent_key is not None and entry.get("urgent_key") != urgent_key:
            return None
        until = last + timedelta(seconds=MIN_REDRAW_SECONDS)
        if now < until:
            return (f"drawn at {last.isoformat()}; the next redraw is due after "
                    f"{until.isoformat()}")
        return None

    # -- the tick ---------------------------------------------------------

    def _subjects(self, mission: Any) -> list[str]:
        subjects = [
            item["company_ref"] for item in (mission.get("universe") or [])
            if isinstance(item, dict) and item.get("company_ref")
        ]
        industry = mission.get("industry_ref")
        if isinstance(industry, str) and industry:
            subjects.append(industry)
        return subjects

    def _change_keys(self, subjects: Any) -> dict[str, str]:
        """This tick's cheap keys, or nothing -- which re-fingerprints all.

        A key that cannot be read is not a reason to skip a subject: an empty
        map makes every fingerprint a miss, which is exactly what this lane
        did before the gate existed.
        """

        connection = getattr(self.store, "connection", None)
        if connection is None:
            return {}
        try:
            keys = subject_change_keys(connection, subjects)
        except Exception:  # noqa: BLE001 - an unreadable key recomputes, never refuses
            return {}
        if keys:
            self.change_keys.forget(keys)
        return keys

    def _choose(self, mission: Any) -> tuple[str | None, str | None, str | None]:
        """The first subject whose evidence has moved and is not held.

        The held check belongs *inside* the loop, not after it.  A held subject
        left in front of the queue is not a subject that waits its turn -- it
        is a subject that takes the slot every tick and reports ``held``, and
        the four behind it are never looked at.  That is the same starvation
        the hold was added to cure, one step further along.

        The last held subject is returned when nothing else qualifies, so a
        tick that does nothing still says which subject it would have run and
        why it did not.
        """

        from .debate_map import DebateMapAuthority, evidence_fingerprint
        from .debate_map_draft import subject_claim_refs

        authority = DebateMapAuthority(self.store)
        blocked: tuple[str, str, str] | None = None
        retired: set[str] | None = None
        self._urgent: dict[str, str | None] = {}
        subjects = self._subjects(mission)
        # B1-5: one grouped scan says which subjects could have moved, before
        # a single fingerprint is built.  Six subjects each read and hashed
        # the whole Ledger every tick to be told, almost always, that nothing
        # had changed -- 8 s of the writer's one store thread, five times a
        # minute.
        keys = self._change_keys(subjects)
        self.recomputed = []
        # One Ledger snapshot for the subjects that do need one, taken at most
        # once and only if at least one of them does.
        snapshot: Any = None
        for subject_ref in subjects:
            label = str(subject_ref)
            key = keys.get(label)
            fingerprint = self.change_keys.cached(label, key)
            if fingerprint is None:
                if snapshot is None:
                    snapshot = self.store.claim_index_snapshot()
                # Refs only. The full drafting rows walk evidence per claim and
                # build a title map; doing that for five subjects on every tick is
                # work spent learning nothing on the overwhelmingly common tick
                # where the answer is "nothing moved".
                refs = subject_claim_refs(
                    self.store, subject_ref, snapshot=snapshot)
                # "This subject has no Claims at all" is a state with no
                # fingerprint, and it is remembered as such: a subject nobody
                # has written about yet would otherwise be the one thing that
                # forces a Ledger snapshot on every tick forever.
                fingerprint = NO_EVIDENCE if not refs else evidence_fingerprint(refs)
                self.change_keys.remember(label, key, fingerprint)
                self.recomputed.append(label)
            if fingerprint == NO_EVIDENCE:
                continue
            current = authority.current(subject_ref)
            if (current is not None
                    and current["evidence_fingerprint"] == fingerprint
                    and current.get("mission_version_ref") == mission.get("id")
                    and current.get("mission_version_hash") == mission.get("content_hash")):
                continue
            if retired is None:
                try:
                    from .claim_retirement import retired_claim_version_refs

                    retired = retired_claim_version_refs(self.store.connection)
                except Exception:  # noqa: BLE001 - no retirement read: no urgency
                    retired = set()
            urgent_key = self._retired_cited(current, retired)
            # A mission roll is a distinct authorized input even when its
            # claim set is byte-identical.  Keeping it in the persistent
            # signature also prevents an old terminal/permission outcome from
            # suppressing the rebind.
            business_key = _business_key(subject_ref, fingerprint, mission, self.launcher)

            permission = current_permission(

                self.budget, business_key, mission, self.launcher,
                connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)))

            decision = (self.budget.blocked(permission)

                        or self.budget.blocked(business_key))
            if decision is not None:
                blocked = blocked or (subject_ref, fingerprint,
                                      decision.classification.reason)
                continue
            paced = self._paced(str(subject_ref), urgent_key)
            if paced is not None:
                blocked = blocked or (subject_ref, fingerprint, paced)
                continue
            self._urgent[str(subject_ref)] = urgent_key
            return subject_ref, fingerprint, None
        if blocked is not None:
            return blocked
        return None, None, None

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            subject_ref, fingerprint, held = self._choose(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        recomputed = ({} if not self.recomputed
                      else {"recomputed": list(self.recomputed)})
        if subject_ref is None:
            return {"status": "idle", "settled": settled, **recomputed,
                    "reason": "every subject's map was drawn from the evidence "
                              "we currently hold"}
        if held is not None:
            return {"status": "held", "subject_ref": subject_ref, **recomputed,
                    "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(
                subject_ref=subject_ref, fingerprint=fingerprint
            )
        except LaneChildConflict as exc:
            return {"status": "busy", "subject_ref": subject_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "subject_ref": subject_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        self._open_business_key = _business_key(subject_ref, fingerprint, mission, self.launcher)
        self._pace_launched(str(subject_ref), getattr(self, "_urgent", {}).get(str(subject_ref)))
        return {
            "status": "launched", "subject_ref": subject_ref,
            "evidence_fingerprint": fingerprint, "ticket_ref": ticket["id"],
            "settled": settled, **recomputed,
        }


# -- the lane ---------------------------------------------------------------

def dispatch(server: Any, params: Any) -> dict[str, Any]:
    """Controller tick (P12c).  One subject's debate map at a time."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no debate map lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionDebateMapLaneCoordinator(
            store=server.store, launcher=launcher, mission=mission,
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument("--debate-map-model-config")


def build_launcher(args: Any) -> Any | None:
    if args.debate_map_model_config is None:
        return None
    from pathlib import Path as _Path

    from .debate_map_launcher import DebateMapLauncher

    return DebateMapLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.debate_map_model_config,
        scheduler_db=args.scheduler,
    )


def argv_fragment(context: Any) -> list[str]:
    config = context.state / DEBATE_MAP_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--debate-map-model-config", str(config)]


# C2's budget pools. Drawing the map is part of covering a company -- it reads
# the Claims coverage produced and says what they disagree about -- so it
# belongs in the same pool as the screen and the model specification, not in
# the event-response pool that pays for reacting to today's news. That is also
# where an unassigned lane lands by default, so the lane is correct today with
# no declaration, and ``LaneSpec.budget_pool`` is deliberately left unset:
# every registered lane currently takes its pool from ``budget_pools.LANE_POOLS``
# and C2 has a test saying so. The explicit line belongs in that table, which
# is not this slice's file; it is named in the report's integration list.
LANE = register_lane(LaneSpec(
    operation="dispatch_debate_map",
    # 135: after the reading lanes that produce the Claims it argues over and
    # before nothing in particular. Wave 2's other agents took the neighbouring
    # tens; this one is deliberately not adjacent to the Claim index, because
    # a tick that indexed and then immediately argued about the same batch
    # would be arguing about a half-tagged one.
    order=135,
    driver_key="debate_map",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12c: what is contested about a company or the industry -- bull and "
         "bear with their evidence, where the market stands, where we differ, "
         "and which way it has been moving.",
))


__all__ = [
    "CHANGE_KEY_FILE",
    "DEBATE_MAP_MODEL_CONFIG",
    "NO_EVIDENCE",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "PUBLISHED_STATUS",
    "TRANSIENT_STATUSES",
    "MissionDebateMapLaneCoordinator",
    "subject_change_keys",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
