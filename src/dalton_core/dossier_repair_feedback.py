"""Read the latest completed Dossier feedback as bounded planner attention.

Dossier children leave diagnostics beside their immutable launch ticket.  The
diagnostics are not Evidence and never become Claims, but a missing-evidence
finding is still an input to deciding what research to do next.  This reader
keeps that input exact: it accepts only a successfully completed ticket and
its matching owner-only summary, selects one latest outcome per company, and
derives stable refs/hashes for the small projection the planner may see.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import content_hash


SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "company-dossier-run:"
MAX_FILE_BYTES = 512 * 1024
MAX_REPAIR_TARGETS = 12
MAX_DETAIL_CHARS = 1000
_TARGET_FIELDS = (
    "unit", "code", "slot_id", "check", "section", "figure", "detail",
)


def _read_object(path: Path) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077 != 0
            or before.st_size > MAX_FILE_BYTES
        ):
            return None
        payload = os.read(descriptor, MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        path_info = path.lstat()
        stable = lambda item: (
            item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns
        )
        if (
            len(payload) > MAX_FILE_BYTES
            or len(payload) != after.st_size
            or stable(before) != stable(after)
            or stable(after) != stable(path_info)
        ):
            return None
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)
    return dict(value) if isinstance(value, Mapping) else None


def _terminal_time(ticket: Mapping[str, Any]) -> str | None:
    value = ticket.get("completed_at")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _owner_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_mode & 0o077 == 0
    )


def _targets(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for raw in value[:MAX_REPAIR_TARGETS]:
        if not isinstance(raw, Mapping):
            continue
        target = {
            key: str(raw[key])[:MAX_DETAIL_CHARS]
            for key in _TARGET_FIELDS
            if isinstance(raw.get(key), (str, int, float, bool))
        }
        if target:
            result.append(target)
    return result


def _outcome(directory: Path) -> dict[str, Any] | None:
    from .company_dossier_launcher import run_digest

    ticket = _read_object(directory / "ticket.json")
    summary = _read_object(directory / "summary.json")
    if ticket is None or summary is None:
        return None
    ticket_ref = ticket.get("id")
    company_ref = ticket.get("company_ref")
    signature = ticket.get("signature")
    completed_at = _terminal_time(ticket)
    if (
        ticket_ref != TICKET_PREFIX + directory.name
        or ticket.get("status") != "succeeded"
        or ticket.get("exit_code") != 0
        or summary.get("status") != "succeeded"
        or not isinstance(company_ref, str)
        or not company_ref
        or summary.get("company_ref") != company_ref
        or not isinstance(signature, str)
        or not signature
        or completed_at is None
        or ticket.get("run_digest") != run_digest(company_ref, signature)
        or directory.name != ticket.get("run_digest")
    ):
        return None
    targets = []
    for ordinal, target in enumerate(_targets(summary.get("repair_targets"))):
        target_body = {
            "schema_version": SCHEMA_VERSION,
            "source_ticket_ref": ticket_ref,
            "source_ticket_signature": signature,
            "company_ref": company_ref,
            "ordinal": ordinal,
            "target": target,
        }
        target_hash = content_hash(target_body)
        targets.append({
            "id": f"dossier-repair-target:{target_hash[:32]}",
            **target,
            "content_hash": target_hash,
        })
    body = {
        "schema_version": SCHEMA_VERSION,
        "source_ticket_ref": ticket_ref,
        "source_ticket_signature": signature,
        "company_ref": company_ref,
        "dossier_status": summary.get("dossier_status"),
        "completed_at": completed_at,
        "repair_targets": targets,
    }
    digest = content_hash(body)
    version_ref = summary.get("version_ref")
    return {
        "id": f"dossier-repair-feedback:{digest[:32]}",
        **body,
        "content_hash": digest,
        # Outside the hashed body on purpose: the feedback id and hash bind
        # admitted repair research and must not move with this addition.
        "published_version_ref": (
            version_ref
            if isinstance(version_ref, str) and version_ref
            and summary.get("version_status") == "fresh"
            else None
        ),
        "target_keys": sorted({_target_key(company_ref, target) for target in _targets(
            summary.get("repair_targets"))}),
    }


def _target_key(company_ref: str, target: Mapping[str, str]) -> str:
    """The same gap across runs: the target itself, not the ticket it came from.

    A target's id and hash bind the ticket that reported it, so the same
    missing figure reported by two runs has two ids.  This key does not.
    """

    return content_hash({
        "schema_version": SCHEMA_VERSION, "company_ref": company_ref,
        "target": dict(target),
    })


def _material(history: list[dict[str, Any]]) -> dict[str, Any]:
    """What of a company's Dossier history should move a plan.

    Live 2026-09-25 06:24-06:49 legacy paid the planner five times in 25
    minutes because the latest run flipped IBM, DXC and CTSH between
    ``partial_published``, ``rubric_refused`` and ``constitution_refused``,
    and each run minted new target ids for the same gaps.  A run's verdict is
    not a new fact about the company; two things are:

    * the Dossier version currently published (the latest ``fresh`` one);
    * the repair targets that are *stable*: a target joins once it has been
      reported by two consecutive successful runs, and leaves only once two
      consecutive successful runs have not reported it.  The targets of the
      run that published the current version count as stable as well.

    ``history`` is chronological, successful outcomes only.
    """

    published = None
    published_keys: set[str] = set()
    stable: set[str] = set()
    seen_streak: dict[str, int] = {}
    absent_streak: dict[str, int] = {}
    for outcome in history:
        keys = set(outcome.get("target_keys") or ())
        if outcome.get("published_version_ref"):
            published = outcome["published_version_ref"]
            published_keys = keys
        for key in keys | set(seen_streak) | stable:
            if key in keys:
                seen_streak[key] = seen_streak.get(key, 0) + 1
                absent_streak[key] = 0
                if seen_streak[key] >= 2:
                    stable.add(key)
            else:
                seen_streak[key] = 0
                absent_streak[key] = absent_streak.get(key, 0) + 1
                if absent_streak[key] >= 2:
                    stable.discard(key)
    return {
        "published_version_ref": published,
        "stable_repair_target_keys": sorted(stable | published_keys),
    }


def read_dossier_repair_feedback(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Latest verified successful Dossier outcome for each company.

    An outcome with no targets is retained because it clears a prior gap and
    must change both the planner state hash and the cheap launch signature.
    Failed, running, mismatched and unreadable tickets have no authority to
    add or clear feedback.
    """

    state = Path(state_dir).expanduser()
    if not state.is_absolute():
        state = state.absolute()
    root = state / "company-dossier-runs"
    if not _owner_directory(state) or not _owner_directory(root):
        return {}
    history: dict[str, list[dict[str, Any]]] = {}
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not _owner_directory(directory):
            continue
        outcome = _outcome(directory)
        if outcome is None:
            continue
        history.setdefault(outcome["company_ref"], []).append(outcome)
    latest: dict[str, dict[str, Any]] = {}
    for company_ref, outcomes in history.items():
        outcomes.sort(key=lambda item: (item["completed_at"], item["source_ticket_ref"]))
        latest[company_ref] = {**outcomes[-1], "material": _material(outcomes)}
    return latest


def dossier_repair_feedback_signature(state_dir: str | Path) -> str:
    """Stable digest for the planner launcher's cheap movement check."""

    feedback = read_dossier_repair_feedback(state_dir)
    return content_hash([
        {
            "company_ref": company_ref,
            "feedback_ref": item["id"],
            "feedback_hash": item["content_hash"],
        }
        for company_ref, item in sorted(feedback.items())
    ])


__all__ = [
    "MAX_REPAIR_TARGETS", "SCHEMA_VERSION",
    "dossier_repair_feedback_signature", "read_dossier_repair_feedback",
]
