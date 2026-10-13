"""One local-only v5 reparse of an exact cached form-contract failure.

No old dispatch, ticket, parse or fee is changed. A current authorized dispatch
gets a fixed maintenance ticket and rereads the original content-addressed
artifact. Failed/unknown/live requests and missing proof never fetch again.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .store import canonical_json, content_hash

CONTRACT = "sec-statement-cached-form-reparse:0.1"
PREFIX = "sec-form-reparse:"
MAX_RAW_BYTES = 32_000_000
ERROR = re.compile(r"AuthorityResolutionConflict: output\.filings\[[0-7]\]\.form is outside the frozen enum\Z")


def _json(path, limit=1_000_000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("cached SEC reparse requires bounded regular original files")
    data = path.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def original(state, ticket_ref, *, expected_ticket_hash=None, expected_summary_hash=None):
    from .lane_child_launcher import ticket_identity
    from .raw_spool import RawSpoolReader
    from .sec_financials_cli import _load_governance
    if not re.fullmatch(r"sec-financials-run:[0-9a-f]{24}", ticket_ref or ""):
        raise ValueError("cached SEC reparse original ticket reference is invalid")
    directory = Path(state) / "sec-financials-runs" / ticket_ref.split(":")[-1]
    ticket, th = _json(directory / "ticket.json")
    summary, sh = _json(directory / "summary.json")
    if (ticket.get("id") != ticket_ref or ticket.get("status") != "failed"
            or summary.get("status") != "failed" or summary.get("observation") is not None
            or not ERROR.fullmatch(str(summary.get("failure_reason")))
            or ticket_identity(ticket, directory / "ticket.json") is not False
            or (expected_ticket_hash is not None and th != expected_ticket_hash)
            or (expected_summary_hash is not None and sh != expected_summary_hash)):
        raise ValueError("cached SEC reparse failure or exited caller is not proved")
    governance_path = Path(state) / "connector-governance" / "sec-financial-statements-v4.json"
    try:
        governance, version = _load_governance(governance_path)
    except RuntimeError as exc:
        raise ValueError("cached SEC reparse original contract is unavailable") from exc
    ticker, form, limit = ticket.get("ticker"), ticket.get("form"), ticket.get("limit")
    digest = hashlib.sha256(f"sec-financials-run|{ticker}|{form}|{limit}|{governance.content_hash}".encode()).hexdigest()[:24]
    command = ticket.get("command", [])
    if not isinstance(command, list) or any(not isinstance(v, str) for v in command):
        raise ValueError("cached SEC reparse original argv is invalid")
    def argument(flag):
        if command.count(flag) != 1 or command.index(flag) + 1 >= len(command):
            raise ValueError("cached SEC reparse original argv binding is missing")
        return command[command.index(flag) + 1]
    if (version != 4 or ticket_ref != "sec-financials-run:" + digest
            or form not in {"10-Q", "10-K"} or type(limit) is not int or not 1 <= limit <= 8
            or not isinstance(ticker, str) or ticker != ticker.upper()
            or any(ticket.get(k) != summary.get(k) for k in ("ticker", "form", "governance_ref", "governance_hash"))
            or ticket.get("governance_ref") != governance.id
            or ticket.get("governance_hash") != governance.content_hash
            or "--governance" not in command
            or Path(argument("--governance")).resolve() != governance_path.resolve()
            or "--allow-network" not in command or "--fixture-file" in command
            or any(argument(flag) != str(value)
                   for flag, value in (("--ticker", ticker), ("--form", form), ("--limit", limit)))
            or summary.get("transport") != "public-https"):
        raise ValueError("cached SEC reparse original request/governance binding differs")
    artifact = summary.get("artifact") or {}
    digest = artifact.get("content_hash")
    if (not re.fullmatch(r"[0-9a-f]{64}", digest or "")
            or artifact.get("storage_locator") != f"spool:objects/{digest[:2]}/{digest}"
            or type(artifact.get("size_bytes")) is not int or not 0 < artifact["size_bytes"] <= MAX_RAW_BYTES):
        raise ValueError("cached SEC reparse artifact identity differs")
    obj = Path(state) / "connector-spool" / "objects" / digest[:2] / digest
    if obj.is_symlink() or not obj.is_file() or obj.stat().st_size != artifact["size_bytes"]:
        raise ValueError("cached SEC reparse original object bounds differ")
    try:
        raw = RawSpoolReader(Path(state) / "connector-spool").read_object(digest)
    except Exception as exc:
        from .raw_spool import RawSpoolError
        if not isinstance(exc, (RawSpoolError, OSError)):
            raise
        raise ValueError("cached SEC reparse original object is unavailable") from exc
    if len(raw) != artifact["size_bytes"] or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("cached SEC reparse original bytes differ")
    parsed = json.loads(raw)
    filings = parsed.get("filings")
    if (not isinstance(filings, list) or not 1 <= len(filings) <= limit
            or not any(f.get("form") == form + "/A" for f in filings)
            or any(f.get("form") not in {form, form + "/A"} for f in filings)
            or not re.fullmatch(r"[0-9]{1,10}", str(parsed.get("cik")))
            or not isinstance(parsed.get("entity_name"), str) or not parsed["entity_name"].strip()
            or any(not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", str(f.get("accession")))
                   for f in filings)):
        raise ValueError("cached SEC reparse contains an unrelated issuer or unsupported filing form")
    # SEC accession prefixes identify the submitter, possibly a filing agent,
    # not necessarily the issuer (SEC Webmaster FAQ). The issuer comes from
    # the frozen company request and its captured parser response instead.
    return {"issuer_cik": str(parsed["cik"]).zfill(10), "entity_name": parsed["entity_name"],
            "ticket_ref": ticket_ref, "ticket_sha256": th, "summary_sha256": sh,
            "artifact_hash": digest, "ticker": ticker, "form": form, "limit": limit}, parsed


def request_path(state, dispatch_ref):
    if not re.fullmatch(r"mission-statement-dispatch:[0-9a-f]{32}", dispatch_ref):
        raise ValueError("cached SEC reparse dispatch reference is invalid")
    return Path(state) / "sec-statement-reparse-requests" / (dispatch_ref.split(":")[-1] + ".json")


def is_reparse_dispatch(missions, dispatch):
    """Missing a companion must refuse, never silently switch to HTTP."""
    row = missions.connection.execute(
        "SELECT 1 FROM sec_statement_cached_reparse_dispatches WHERE dispatch_ref=?",
        (dispatch["dispatch_id"],)).fetchone()
    # Even a malformed marker routes to the local path, which verifies it and
    # refuses. Absence of a companion can never downgrade a known local kind.
    return row is not None


def prepare(missions, launcher, *, company_ref, form):
    from .sec_financials_cli import _load_governance
    from .workspace_runtime_setup import _atomic_seed
    try:
        governance, version = _load_governance(launcher.reparse_governance_path() if hasattr(launcher, "reparse_governance_path") else launcher.governance_path)
    except RuntimeError as exc:
        raise ValueError("cached SEC reparse current contract is unavailable") from exc
    if version != 5:
        return None
    prior = missions.connection.execute(
        "SELECT * FROM coverage_mission_statement_dispatches WHERE company_ref=? AND form=? "
        "AND status='failed' ORDER BY updated_at DESC,dispatch_id DESC LIMIT 1", (company_ref, form)).fetchone()
    if prior is None or not ERROR.fullmatch(str(prior["failure_reason"])):
        return None
    authorization = missions.sec_lane_authorization_for_company(company_ref)
    proof, _ = original(launcher.state_dir, prior["ticket_ref"])
    issuer_matches = (company_ref == "company:sec-cik:" + proof["issuer_cik"]
                      or company_ref == "company:ticker:" + proof["ticker"].lower())
    if (authorization["ticker"] != proof["ticker"] or prior["ticker"] != proof["ticker"]
            or not issuer_matches):
        raise ValueError("cached SEC reparse company authorization differs")
    salt = PREFIX + content_hash({"contract": CONTRACT, "ticket": proof["ticket_ref"]})[:32]
    request = {"mission_version_ref": authorization["mission_version_ref"],
        "mission_version_hash": authorization["mission_version_hash"], "company_ref": company_ref,
        "ticker": proof["ticker"], "actor_ref": authorization["actor_ref"], "form": form,
        "filing_limit": proof["limit"], "attempt": 0, "retry_salt": salt}
    dispatch_ref = "mission-statement-dispatch:" + content_hash(request)[:32]
    body = {"schema_version": CONTRACT, "dispatch_ref": dispatch_ref, "request": request,
        "authorization": authorization, "original": proof,
        "governance_ref": governance.id, "governance_hash": governance.content_hash}
    replay = {**body, "content_hash": content_hash(body)}
    path = request_path(launcher.state_dir, dispatch_ref)
    _atomic_seed(path, replay)  # Before queue: a crash cannot turn this into a network dispatch.
    if json.loads(path.read_bytes()) != replay:
        raise ValueError("cached SEC reparse request conflicts")
    with missions._transaction() as cursor:
        held = cursor.execute("SELECT record_json,content_hash FROM sec_statement_cached_reparse_dispatches WHERE dispatch_ref=?", (dispatch_ref,)).fetchone()
        if held is not None:
            if held["record_json"] != canonical_json(replay) or held["content_hash"] != replay["content_hash"]:
                raise ValueError("cached SEC reparse kind marker conflicts")
        else:
            cursor.execute("INSERT INTO sec_statement_cached_reparse_dispatches VALUES(?,?,?)",
                           (dispatch_ref, canonical_json(replay), replay["content_hash"]))
    return missions.queue_statement_dispatch(authorization=authorization, form=form,
        filing_limit=proof["limit"], attempt=0, retry_salt=salt)


def validate_request(path, state, missions, governance):
    replay, _ = _json(Path(path))
    body = {k: v for k, v in replay.items() if k != "content_hash"}
    if (set(replay) != {"schema_version", "dispatch_ref", "request", "authorization", "original",
                       "governance_ref", "governance_hash", "content_hash"}
            or replay["schema_version"] != CONTRACT or content_hash(body) != replay["content_hash"]
            or Path(path).resolve() != request_path(state, replay["dispatch_ref"]).resolve()
            or governance.id != "connector-governance:sec-financial-statements:v5"
            or not governance.approved or governance.content_hash != replay["governance_hash"]
            or governance.id != replay["governance_ref"]):
        raise ValueError("cached SEC reparse request or v5 approval differs")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if (now < governance._parse_time(governance.effective_from, "effective_from")
            or (governance.wire["effective_until"] is not None and
                now >= governance._parse_time(governance.wire["effective_until"], "effective_until"))):
        raise ValueError("cached SEC reparse current approval is outside its effective window")
    marker = missions.connection.execute("SELECT record_json,content_hash FROM sec_statement_cached_reparse_dispatches WHERE dispatch_ref=?", (replay["dispatch_ref"],)).fetchone()
    if marker is None or marker["record_json"] != canonical_json(replay) or marker["content_hash"] != replay["content_hash"]:
        raise ValueError("cached SEC reparse kind marker differs")
    authorization = replay["authorization"]
    current = missions.authorize_sec_lane(company_ref=authorization["company_ref"], ticker=authorization["ticker"],
        actor_ref=authorization["actor_ref"], mission_version_ref=authorization["mission_version_ref"],
        mission_version_hash=authorization["mission_version_hash"])
    dispatch = missions._statement_dispatch(replay["dispatch_ref"])
    if (current != authorization or dispatch["status"] not in {"pending", "launched"}
            or dispatch["request_hash"] != content_hash(replay["request"])
            or json.loads(dispatch["authorization_json"]) != current):
        raise ValueError("cached SEC reparse current mission/dispatch permission differs")
    proof, raw = original(state, replay["original"]["ticket_ref"],
        expected_ticket_hash=replay["original"]["ticket_sha256"],
        expected_summary_hash=replay["original"]["summary_sha256"])
    company = current["company_ref"]
    issuer_matches = (company == "company:sec-cik:" + proof["issuer_cik"]
                      or company == "company:ticker:" + proof["ticker"].lower())
    if (proof != replay["original"] or proof["ticker"] != current["ticker"]
            or not issuer_matches or replay["request"]["form"] != proof["form"]
            or replay["request"]["filing_limit"] != proof["limit"]
            or replay["dispatch_ref"] != "mission-statement-dispatch:" + content_hash(replay["request"])[:32]):
        raise ValueError("cached SEC reparse original input differs")
    return replay, raw
