"""Owner-authenticated research review control plane behind Tailscale Serve.

The service owns the candidate/review staging path and a single scoped writer
token.  It never receives the Core database path.  Tailscale identity is
hashed into the immutable reviewer subject; no timeout or automation path can
produce an accepted decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from .research_review import HumanReviewAuthority, ResearchReviewError
from .store import content_hash
from .writer_client import WriterClient
from .writer_server import load_principals


_HTML_PATH = Path(__file__).with_name("research_review_control.html")
MAX_BODY_BYTES = 16384
SESSION_TTL_SECONDS = 3600
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


class ResearchReviewControlError(RuntimeError):
    pass


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchReviewControlError(f"{name} must be a non-empty string")
    return value.strip()


def _absolute_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ResearchReviewControlError(f"{name} must be an absolute path")
    return Path(value)


def _positive_int(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ResearchReviewControlError(f"{name} must be 1..{maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ResearchReviewControlConfig:
    host: str
    port: int
    tailscale_host: str
    allowed_tailscale_logins: tuple[str, ...]
    candidate_staging_path: Path
    writer_socket: Path
    token_config: Path
    reconcile_interval_seconds: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResearchReviewControlConfig":
        expected = {
            "host", "port", "tailscale_host", "allowed_tailscale_logins",
            "candidate_staging_path", "writer_socket", "token_config",
            "reconcile_interval_seconds",
        }
        if set(raw) != expected:
            raise ResearchReviewControlError("research review config has an invalid closed shape")
        host = _string(raw["host"], "host")
        if host not in {"127.0.0.1", "::1"}:
            raise ResearchReviewControlError("research review control must bind loopback")
        tailscale_host = _string(raw["tailscale_host"], "tailscale_host").rstrip(".")
        if any(char in tailscale_host for char in "/:@") or not tailscale_host.endswith(".ts.net"):
            raise ResearchReviewControlError("tailscale_host must be a ts.net hostname")
        logins = raw["allowed_tailscale_logins"]
        if (
            not isinstance(logins, list) or not logins
            or any(not isinstance(item, str) or not item.strip() for item in logins)
            or len(set(logins)) != len(logins)
        ):
            raise ResearchReviewControlError("allowed_tailscale_logins must be unique strings")
        return cls(
            host=host,
            port=_positive_int(raw["port"], "port", 65535),
            tailscale_host=tailscale_host,
            allowed_tailscale_logins=tuple(item.strip() for item in logins),
            candidate_staging_path=_absolute_path(raw["candidate_staging_path"], "candidate_staging_path"),
            writer_socket=_absolute_path(raw["writer_socket"], "writer_socket"),
            token_config=_absolute_path(raw["token_config"], "token_config"),
            reconcile_interval_seconds=_positive_int(
                raw["reconcile_interval_seconds"], "reconcile_interval_seconds", 86400
            ),
        )

    @classmethod
    def from_service_file(cls, path: str | Path) -> "ResearchReviewControlConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            mode = config_path.stat().st_mode & 0o777
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchReviewControlError("service config is unavailable") from exc
        if mode & 0o022 or not isinstance(raw, Mapping):
            raise ResearchReviewControlError("service config permissions or shape are invalid")
        review = raw.get("research_review")
        if (
            not isinstance(review, Mapping) or set(review) != {"enabled", "config"}
            or review.get("enabled") is not True
            or not isinstance(review.get("config"), Mapping)
        ):
            raise ResearchReviewControlError("research review control is not enabled")
        return cls.from_mapping(review["config"])


def _subject_for_login(login: str) -> str:
    digest = hashlib.sha256(login.encode("utf-8")).hexdigest()[:32]
    return f"human:tailscale-{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ResearchReviewControlPlane:
    def __init__(
        self,
        config: ResearchReviewControlConfig,
        *,
        authority: HumanReviewAuthority | None = None,
        writer: WriterClient | None = None,
    ) -> None:
        self.config = config
        self.authority = authority or HumanReviewAuthority(config.candidate_staging_path)
        if writer is None:
            principal = load_principals(config.token_config).get("research-review-control")
            if principal is None:
                raise ResearchReviewControlError("research-review-control principal is unavailable")
            writer = WriterClient(str(config.writer_socket), principal.token, timeout=30)
        self.writer = writer

    def close(self) -> None:
        self.authority.close()

    def view(self, login: str) -> dict[str, Any]:
        reviewer = _subject_for_login(login)
        items = []
        for item in self.authority.list_candidates(limit=200):
            claim = item["claim"]
            evidence = item["evidence"]
            decision = item["decision"]
            items.append({
                "candidate_claim_ref": claim["id"],
                "candidate_claim_hash": claim["content_hash"],
                "subject_ref": claim["subject_ref"],
                "metric_or_aspect": claim["metric_or_aspect"],
                "period": claim["period"],
                "basis": claim["basis"],
                "normalized_statement": claim["normalized_statement"],
                "value": claim["value"],
                "unit": claim["unit"],
                "currency": claim["currency"],
                "scale": claim["scale"],
                "source_type": evidence["source_type"],
                "source_ref": evidence["source_ref"],
                "source_envelope_ref": evidence["source_envelope_ref"],
                "artifact_refs": evidence["artifact_refs"],
                "decision": decision,
                "commit_state": item.get("commit_state"),
            })
        return {"as_of": _now(), "reviewer_ref": reviewer, "items": items}

    def _candidate(self, candidate_claim_ref: str, candidate_claim_hash: str) -> dict[str, Any]:
        matches = [
            item for item in self.authority.list_candidates(limit=500)
            if item["claim"]["id"] == candidate_claim_ref
        ]
        if len(matches) != 1 or matches[0]["claim"]["content_hash"] != candidate_claim_hash:
            raise ResearchReviewControlError("candidate is unavailable or changed")
        return matches[0]

    def record(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        expected = {
            "request_id", "candidate_claim_ref", "candidate_claim_hash", "verdict",
            "rationale", "findings", "proposed_revisions",
        }
        if set(value) != expected:
            raise ResearchReviewControlError("request body has an invalid closed shape")
        request_id = _string(value["request_id"], "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise ResearchReviewControlError("request_id has an invalid shape")
        candidate_claim_ref = _string(value["candidate_claim_ref"], "candidate_claim_ref")
        candidate_claim_hash = _string(value["candidate_claim_hash"], "candidate_claim_hash")
        verdict = value["verdict"]
        if verdict not in {"accept", "revise", "reject"}:
            raise ResearchReviewControlError("verdict is invalid")
        rationale = _string(value["rationale"], "rationale")
        findings = value["findings"]
        if not isinstance(findings, list) or any(not isinstance(item, str) or not item for item in findings):
            raise ResearchReviewControlError("findings must be an array of strings")
        revisions = value["proposed_revisions"]
        if revisions is not None and not isinstance(revisions, Mapping):
            raise ResearchReviewControlError("proposed_revisions must be an object or null")
        candidate = self._candidate(candidate_claim_ref, candidate_claim_hash)
        claim = candidate["claim"]
        reviewer_ref = _subject_for_login(login)
        digest = content_hash({
            "request_id": request_id, "candidate_claim_ref": candidate_claim_ref,
            "candidate_claim_hash": candidate_claim_hash, "reviewer_ref": reviewer_ref,
        })[:32]
        result = self.authority.decide(
            candidate_claim_ref=candidate_claim_ref,
            candidate_claim_hash=candidate_claim_hash,
            verdict=verdict,
            reviewed_semantics={
                field: claim[field]
                for field in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement",
                )
            },
            rationale=rationale,
            findings=findings,
            reviewer_ref=reviewer_ref,
            source_event_ref=f"research-review:{digest}",
            idempotency_key=f"research-review:{digest}",
            created_at=_now(),
            proposed_revisions=revisions,
        )
        if verdict == "accept":
            reconciled = self.reconcile(decision_ref=result["decision_ref"])
            result["commit_state"] = "committed" if reconciled["committed"] else "pending"
        return result

    def reconcile(self, *, decision_ref: str | None = None, limit: int = 20) -> dict[str, int]:
        pending = self.authority.pending_commits(limit=limit)
        if decision_ref is not None:
            pending = [item for item in pending if item["decision"]["id"] == decision_ref]
        committed = failed = 0
        for bundle in pending:
            decision = bundle["decision"]
            try:
                result = self.writer.commit_reviewed_candidate(
                    **bundle, idempotency_key=f"reviewed-ledger:{decision['id']}"
                )
                self.authority.record_commit_result(
                    decision["id"], created_at=_now(), ledger_result=result
                )
                committed += 1
            except Exception as exc:
                code = getattr(exc, "code", None)
                error_code = code if isinstance(code, str) and code else "writer_rejected"
                self.authority.record_commit_result(
                    decision["id"], created_at=_now(), error_code=error_code
                )
                failed += 1
        return {"checked": len(pending), "committed": committed, "failed": failed}


@dataclass(slots=True)
class _Session:
    login: str
    csrf: str
    expires_at: float


class ResearchReviewControlApplication:
    def __init__(self, config: ResearchReviewControlConfig, plane: ResearchReviewControlPlane) -> None:
        self.config = config
        self.plane = plane
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def allowed_login(self, value: str | None) -> str | None:
        if not isinstance(value, str) or value not in self.config.allowed_tailscale_logins:
            return None
        return value

    def session(self, login: str, cookie_header: str | None) -> tuple[str, _Session, bool]:
        session_id = None
        if cookie_header:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
                session_id = cookie.get("dalton_review_session").value if cookie.get("dalton_review_session") else None
            except Exception:
                session_id = None
        now = time.monotonic()
        with self._lock:
            for key in [key for key, value in self._sessions.items() if value.expires_at <= now]:
                self._sessions.pop(key, None)
            current = self._sessions.get(session_id or "")
            if current is not None and current.login == login:
                current.expires_at = now + SESSION_TTL_SECONDS
                return session_id or "", current, False
            session_id = secrets.token_urlsafe(32)
            current = _Session(login, secrets.token_urlsafe(32), now + SESSION_TTL_SECONDS)
            self._sessions[session_id] = current
            return session_id, current, True

    def post(self, login: str, session: _Session, csrf: str | None, body: bytes) -> dict[str, Any]:
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchReviewControlError("request body is invalid") from exc
        if not isinstance(value, Mapping):
            raise ResearchReviewControlError("request body must be an object")
        return self.plane.record(login, value)


def _handler(application: ResearchReviewControlApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DaltonResearchReview/0.1"

        def _identity(self) -> str | None:
            return application.allowed_login(self.headers.get("Tailscale-User-Login"))

        def _host_ok(self) -> bool:
            value = self.headers.get("Host")
            if not isinstance(value, str) or any(char.isspace() for char in value):
                return False
            try:
                host = urlparse(f"//{value}").hostname
            except ValueError:
                return False
            return host in {application.config.tailscale_host, "127.0.0.1", "::1"}

        def _send(self, status: int, content_type: str, body: bytes, *, session_cookie: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
            )
            if session_cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"dalton_review_session={session_cookie}; Path=/; Secure; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.write(body)

        def _context(self) -> tuple[str, str, _Session, bool] | None:
            if not self._host_ok():
                self._send(HTTPStatus.MISDIRECTED_REQUEST, "application/json", b'{"error":"invalid_host"}')
                return None
            login = self._identity()
            if login is None:
                self._send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"forbidden"}')
                return None
            session_id, session, created = application.session(login, self.headers.get("Cookie"))
            return login, session_id, session, created

        def do_GET(self) -> None:  # noqa: N802
            context = self._context()
            if context is None:
                return
            login, session_id, session, created = context
            path = urlparse(self.path).path
            try:
                if path == "/":
                    body = _HTML_PATH.read_bytes()
                    content_type = "text/html; charset=utf-8"
                elif path == "/v1/research-review":
                    value = application.plane.view(login)
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                else:
                    self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}')
                    return
            except Exception:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json", b'{"error":"unavailable"}')
                return
            self._send(HTTPStatus.OK, content_type, body, session_cookie=session_id if created else None)

        def do_POST(self) -> None:  # noqa: N802
            context = self._context()
            if context is None:
                return
            login, session_id, session, created = context
            if urlparse(self.path).path != "/v1/research-review/decision":
                self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}')
                return
            if self.headers.get_content_type() != "application/json":
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "application/json", b'{"error":"content_type"}')
                return
            length = self.headers.get("Content-Length")
            if not isinstance(length, str) or not length.isdigit() or int(length) > MAX_BODY_BYTES:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "application/json", b'{"error":"body_size"}')
                return
            try:
                result = application.post(
                    login, session, self.headers.get("X-Dalton-CSRF"), self.rfile.read(int(length))
                )
            except PermissionError:
                self._send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"csrf"}')
                return
            except (ResearchReviewControlError, ResearchReviewError):
                self._send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"invalid_request"}')
                return
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body, session_cookie=session_id if created else None)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def serve(config: ResearchReviewControlConfig) -> None:
    plane = ResearchReviewControlPlane(config)
    application = ResearchReviewControlApplication(config, plane)
    stop = threading.Event()

    def reconcile_loop() -> None:
        while not stop.is_set():
            try:
                plane.reconcile()
            except Exception:
                pass
            stop.wait(config.reconcile_interval_seconds)

    reconciler = threading.Thread(target=reconcile_loop, name="dalton-research-review-reconcile", daemon=True)
    reconciler.start()
    server = ThreadingHTTPServer((config.host, config.port), _handler(application))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        reconciler.join(timeout=5)
        plane.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Dalton human research review control plane")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(ResearchReviewControlConfig.from_service_file(args.config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ResearchReviewControlApplication", "ResearchReviewControlConfig",
    "ResearchReviewControlError", "ResearchReviewControlPlane", "serve",
]
