"""The owner's company alias ledger: what a covered company is called, amended.

Until 2026-09-24 there was no sanctioned way to teach an environment a new
name for a company.  The packaged ``document_subject.COMPANY_NAMES`` is code;
a workspace's feed plan is content-hashed and ``repair_workspace_lane_parity``
only ever writes one that is missing; a mission universe member is a closed
shape with no ``name``.  So "Amazon is also called AWS and 亚马逊" could only be
said by editing a file by hand, which is exactly what the owner-actions
record refused to do.

This is the missing write.  It follows the shape every other owner decision
here has:

* **append-only.**  Each change is one immutable revision file in
  ``<state>/company-alias-revisions/``, written with ``O_EXCL`` and never
  rewritten; a revision names its predecessor, so the ledger is a chain and a
  fork or an edited file is detected rather than believed.
* **a human says so.**  ``human:`` actors only, a reason is required, and the
  CLI is a dry run until ``--apply``.
* **additive by default.**  ``add`` names a company by another name;
  ``retire`` withdraws a name (packaged ones included) when it turns out to
  over-match.  Retiring is itself a new revision -- nothing is deleted.

Nothing reads the ledger from here: ``mission_feed_lane.load_feed_discovery_plan``
attaches the current overlay to the plan it loads (``plan["company_aliases"]``),
and ``mission_company_names.mission_name_table`` applies it.  Every lane that
builds its name table from its plan -- feed attribution, extraction's subject
label, the claim subject checks -- therefore sees a new alias on its next tick,
with no restart and no deploy.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .store import content_hash

SCHEMA_VERSION = "company-alias-revision:0.1"
DIRECTORY = "company-alias-revisions"
ACTIONS = ("add", "retire")
MAX_NAMES_PER_REVISION = 20
MAX_NAME_CHARS = 100
MAX_REASON_CHARS = 1000
_FIELDS = {"schema_version", "id", "sequence", "prior_revision_ref", "ticker",
           "action", "names", "reason", "actor_ref", "created_at", "content_hash"}
_HUMAN_ACTOR_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_FILE_RE = re.compile(r"(\d{6})-([0-9a-f]{16})\.json\Z")


class CompanyAliasError(ValueError):
    """A request or the ledger itself does not satisfy the contract."""


def _wire(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode()


def _ticker(value: Any) -> str:
    ticker = str(value or "").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise CompanyAliasError(f"invalid ticker: {value!r}")
    return ticker


def _names(values: Sequence[Any]) -> list[str]:
    if isinstance(values, str) or not isinstance(values, Sequence) or not values:
        raise CompanyAliasError("at least one name is required")
    out: dict[str, str] = {}
    for value in values:
        name = unicodedata.normalize("NFC", str(value)).strip()
        if (not name or len(name) > MAX_NAME_CHARS
                or any(unicodedata.category(ch).startswith("C") for ch in name)):
            raise CompanyAliasError(f"invalid name: {value!r}")
        out.setdefault(name.casefold(), name)
    if len(out) > MAX_NAMES_PER_REVISION:
        raise CompanyAliasError(f"at most {MAX_NAMES_PER_REVISION} names per revision")
    return list(out.values())


def _revision_id(record: Mapping[str, Any]) -> str:
    body = {key: item for key, item in record.items() if key not in {"id", "content_hash"}}
    return "company-alias-revision:" + content_hash(body)[:32]


def _directory(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / DIRECTORY


def load_revisions(state_dir: str | Path) -> list[dict[str, Any]]:
    """Every revision in order, after checking hashes and the chain."""

    directory = _directory(state_dir)
    if directory.is_symlink():
        raise CompanyAliasError("company alias ledger must not be a symlink")
    if not directory.is_dir():
        return []
    revisions: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        match = _FILE_RE.fullmatch(path.name)
        if match is None or path.is_symlink():
            raise CompanyAliasError(f"unexpected file in the alias ledger: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != _FIELDS \
                or value["schema_version"] != SCHEMA_VERSION:
            raise CompanyAliasError(f"alias revision has an invalid shape: {path.name}")
        unsigned = {key: item for key, item in value.items() if key != "content_hash"}
        if content_hash(unsigned) != value["content_hash"] \
                or value["id"] != _revision_id(unsigned) \
                or int(match.group(1)) != value["sequence"] \
                or match.group(2) != value["content_hash"][:16]:
            raise CompanyAliasError(f"alias revision does not hash to itself: {path.name}")
        expected_prior = revisions[-1]["id"] if revisions else None
        if value["sequence"] != len(revisions) + 1 \
                or value["prior_revision_ref"] != expected_prior:
            raise CompanyAliasError(f"alias ledger chain is broken at {path.name}")
        if value["action"] not in ACTIONS or _ticker(value["ticker"]) != value["ticker"] \
                or _names(value["names"]) != value["names"]:
            raise CompanyAliasError(f"alias revision content is invalid: {path.name}")
        revisions.append(value)
    return revisions


def fold_revisions(revisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The net effect of the ledger: ``added`` and ``retired`` names per ticker."""

    added: dict[str, dict[str, str]] = {}
    retired: dict[str, dict[str, str]] = {}
    for revision in revisions:
        ticker = revision["ticker"]
        for name in revision["names"]:
            key = name.casefold()
            if revision["action"] == "add":
                retired.get(ticker, {}).pop(key, None)
                added.setdefault(ticker, {})[key] = name
            else:
                added.get(ticker, {}).pop(key, None)
                retired.setdefault(ticker, {})[key] = name
    return {
        "revision_ref": revisions[-1]["id"] if revisions else None,
        "revision_count": len(revisions),
        "added": {ticker: list(names.values()) for ticker, names in sorted(added.items())
                  if names},
        "retired": {ticker: list(names.values()) for ticker, names in sorted(retired.items())
                    if names},
    }


def load_overlay(state_dir: str | Path) -> dict[str, Any] | None:
    """The ledger's current overlay, or ``None`` when this environment has none."""

    revisions = load_revisions(state_dir)
    return fold_revisions(revisions) if revisions else None


def effective_names(ticker: str, overlay: Mapping[str, Any] | None,
                    plan_names: Sequence[str] = ()) -> list[str]:
    """What a lane will call this ticker, for ``show`` and for the dry run."""

    from .mission_company_names import mission_name_table

    table = mission_name_table(
        [{"ticker": ticker, "company_ref": "company:alias-preview"}],
        {"companies": {"company:alias-preview": {"names": list(plan_names)}}
         if plan_names else {}, "company_aliases": overlay or {}},
    )
    return list(table.get(ticker, ()))


def record_revision(state_dir: str | Path, *, ticker: str, action: str,
                    names: Sequence[str], reason: str, actor_ref: str,
                    apply: bool = False, now: str | None = None) -> dict[str, Any]:
    """Append one revision (or, without ``apply``, say what it would append)."""

    ticker = _ticker(ticker)
    if action not in ACTIONS:
        raise CompanyAliasError(f"action must be one of {list(ACTIONS)}")
    names = _names(names)
    if not isinstance(actor_ref, str) or not _HUMAN_ACTOR_RE.fullmatch(actor_ref):
        raise CompanyAliasError("actor_ref must be a human: principal")
    reason = str(reason or "").strip()
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise CompanyAliasError(f"reason must be 1..{MAX_REASON_CHARS} characters")
    if action == "retire" and any(name.upper() == ticker for name in names):
        raise CompanyAliasError("a ticker always names itself and cannot be retired")
    directory = _directory(state_dir)
    state = Path(state_dir).expanduser()
    if not state.is_dir():
        raise CompanyAliasError(f"state directory does not exist: {state}")
    if apply:
        directory.mkdir(mode=0o700, exist_ok=True)
        lock_path = directory / ".lock"
        if lock_path.is_symlink():
            raise CompanyAliasError("company alias ledger lock must not be a symlink")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        lock = os.fdopen(descriptor, "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
    else:
        lock = None
    try:
        revisions = load_revisions(state)
        overlay = fold_revisions(revisions)
        before = effective_names(ticker, overlay)
        present = {name.casefold() for name in before}
        changing = [name for name in names
                    if (name.casefold() not in present) == (action == "add")]
        if not changing:
            return {"status": "unchanged", "ticker": ticker, "action": action,
                    "names": names, "effective_names": before,
                    "revision_ref": overlay["revision_ref"]}
        unsigned = {
            "schema_version": SCHEMA_VERSION,
            "sequence": len(revisions) + 1,
            "prior_revision_ref": overlay["revision_ref"],
            "ticker": ticker, "action": action, "names": changing,
            "reason": reason, "actor_ref": actor_ref,
            "created_at": now or datetime.now(timezone.utc).isoformat(),
        }
        # The id is the hash of the revision's content; content_hash then
        # covers the whole record, id included.
        signed = {**unsigned, "id": _revision_id(unsigned)}
        signed["content_hash"] = content_hash(signed)
        after = effective_names(ticker, fold_revisions([*revisions, signed]))
        plan = {"ticker": ticker, "action": action, "names": changing,
                "effective_names_before": before, "effective_names_after": after,
                "prior_revision_ref": overlay["revision_ref"]}
        if not apply:
            return {"status": "dry_run", **plan}
        path = directory / f"{signed['sequence']:06d}-{signed['content_hash'][:16]}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_wire(signed))
            stream.flush()
            os.fsync(stream.fileno())
        load_revisions(state)  # the chain must still read back whole
        return {"status": "recorded", **plan, "revision": signed, "path": str(path)}
    finally:
        if lock is not None:
            lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show or amend what covered companies are called (append-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="the ledger and the names each ticker resolves to")
    show.add_argument("--state-dir", required=True)
    show.add_argument("--ticker", action="append", default=None)
    for action in ACTIONS:
        command = sub.add_parser(action, help=f"{action} names (dry run unless --apply)")
        command.add_argument("--state-dir", required=True)
        command.add_argument("--ticker", required=True)
        command.add_argument("--name", action="append", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--actor", required=True, help="human:<owner>")
        command.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            from .document_subject import COMPANY_NAMES

            revisions = load_revisions(args.state_dir)
            overlay = fold_revisions(revisions)
            tickers = [_ticker(item) for item in args.ticker] if args.ticker else sorted(
                set(COMPANY_NAMES) | set(overlay["added"]) | set(overlay["retired"]))
            out: dict[str, Any] = {
                "revision_ref": overlay["revision_ref"],
                "revisions": revisions,
                "effective_names": {ticker: effective_names(ticker, overlay)
                                    for ticker in tickers},
                "note": ("a workspace feed plan's own companies[].names come first "
                         "for that workspace's lanes; these are the packaged names "
                         "plus this ledger"),
            }
        else:
            out = record_revision(args.state_dir, ticker=args.ticker, action=args.command,
                                  names=args.name, reason=args.reason,
                                  actor_ref=args.actor, apply=args.apply)
    except CompanyAliasError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


__all__ = [
    "ACTIONS",
    "CompanyAliasError",
    "DIRECTORY",
    "SCHEMA_VERSION",
    "effective_names",
    "fold_revisions",
    "load_overlay",
    "load_revisions",
    "main",
    "record_revision",
]


if __name__ == "__main__":
    sys.exit(main())
