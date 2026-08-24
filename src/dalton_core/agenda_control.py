"""Owner-authenticated Dalton Cockpit behind Tailscale Serve.

The service binds loopback only.  Tailscale Serve terminates HTTPS, strips
spoofed identity headers, and injects ``Tailscale-User-Login``.  The backend
adds one in-memory SameSite CSRF session before accepting any write.  Agenda,
research review, and transcript correction share this shell but retain their
separate writer principals and fail-closed authority gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from .research_review import ResearchReviewError
from .research_review_control import (
    ResearchReviewControlConfig,
    ResearchReviewControlError,
    ResearchReviewControlPlane,
)
from .store import content_hash
from .writer_client import WriterClient
from .writer_server import load_principals


_HTML_PATH = Path(__file__).with_name("cockpit_control.html")
MAX_BODY_BYTES = 16384
SESSION_TTL_SECONDS = 3600
AUTOMATION_SUBJECT = "automation:timeout"


class AgendaControlError(RuntimeError):
    pass


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgendaControlError(f"{name} must be a non-empty string")
    return value.strip()


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AgendaControlError(f"{name} must be an absolute path")
    return Path(value)


def _positive_int(value: Any, name: str, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise AgendaControlError(f"{name} must be 1..{upper}")
    return value


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise AgendaControlError(f"{name} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgendaControlError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AgendaControlError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AgendaControlConfig:
    host: str
    port: int
    tailscale_host: str
    tailscale_executable: Path
    allowed_tailscale_logins: tuple[str, ...]
    writer_socket: Path
    token_config: Path
    endpoint_ref: str
    feedback_timeout_seconds: int
    sweep_interval_seconds: int
    research_review: ResearchReviewControlConfig | None = None

    @property
    def public_url(self) -> str:
        return f"https://{self.tailscale_host}:{self.port}/"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AgendaControlConfig":
        expected = {
            "host", "port", "tailscale_host", "tailscale_executable", "allowed_tailscale_logins",
            "writer_socket", "token_config", "endpoint_ref",
            "feedback_timeout_seconds", "sweep_interval_seconds",
        }
        if set(raw) - expected - {"research_review"} or not expected.issubset(raw):
            raise AgendaControlError("Agenda control config has an invalid closed shape")
        host = _string(raw["host"], "host")
        if host not in {"127.0.0.1", "::1"}:
            raise AgendaControlError("Agenda control must bind loopback")
        tailscale_host = _string(raw["tailscale_host"], "tailscale_host").rstrip(".")
        if any(char in tailscale_host for char in "/:@") or not tailscale_host.endswith(".ts.net"):
            raise AgendaControlError("tailscale_host must be a ts.net hostname")
        logins = raw["allowed_tailscale_logins"]
        if (
            not isinstance(logins, list) or not logins
            or any(not isinstance(item, str) or not item.strip() for item in logins)
            or len(set(logins)) != len(logins)
        ):
            raise AgendaControlError("allowed_tailscale_logins must be unique strings")
        review_config = None
        review_raw = raw.get("research_review")
        if review_raw is not None:
            if not isinstance(review_raw, Mapping):
                raise AgendaControlError("research_review must be an object or null")
            try:
                review_config = ResearchReviewControlConfig.from_mapping(review_raw)
            except ResearchReviewControlError as exc:
                raise AgendaControlError("embedded research review config is invalid") from exc
        return cls(
            host=host,
            port=_positive_int(raw["port"], "port", 65535),
            tailscale_host=tailscale_host,
            tailscale_executable=_path(raw["tailscale_executable"], "tailscale_executable"),
            allowed_tailscale_logins=tuple(item.strip() for item in logins),
            writer_socket=_path(raw["writer_socket"], "writer_socket"),
            token_config=_path(raw["token_config"], "token_config"),
            endpoint_ref=_string(raw["endpoint_ref"], "endpoint_ref"),
            feedback_timeout_seconds=_positive_int(
                raw["feedback_timeout_seconds"], "feedback_timeout_seconds", 31 * 86400
            ),
            sweep_interval_seconds=_positive_int(
                raw["sweep_interval_seconds"], "sweep_interval_seconds", 86400
            ),
            research_review=review_config,
        )

    @classmethod
    def from_service_file(cls, path: str | Path) -> "AgendaControlConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            mode = config_path.stat().st_mode & 0o777
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("service config is unavailable") from exc
        if mode & 0o022 or not isinstance(raw, Mapping):
            raise AgendaControlError("service config permissions or shape are invalid")
        control = raw.get("control")
        if (
            not isinstance(control, Mapping)
            or set(control) != {"enabled", "config"}
            or control.get("enabled") is not True
            or not isinstance(control.get("config"), Mapping)
        ):
            raise AgendaControlError("Agenda control is not enabled")
        return cls.from_mapping(control["config"])


def _subject_for_login(login: str) -> str:
    digest = hashlib.sha256(login.encode("utf-8")).hexdigest()[:32]
    return f"human:tailscale-{digest}"


class AgendaControlPlane:
    def __init__(
        self,
        config: AgendaControlConfig,
        *,
        dashboard_client: WriterClient | None = None,
        timeout_client: WriterClient | None = None,
    ) -> None:
        self.config = config
        principals = None
        if dashboard_client is None or timeout_client is None:
            principals = load_principals(config.token_config)
        if dashboard_client is None:
            principal = (principals or {}).get("dashboard-control")
            if principal is None:
                raise AgendaControlError("dashboard-control principal is unavailable")
            dashboard_client = WriterClient(str(config.writer_socket), principal.token, timeout=30)
        if timeout_client is None:
            principal = (principals or {}).get("agenda-timeout")
            if principal is None:
                raise AgendaControlError("agenda-timeout principal is unavailable")
            timeout_client = WriterClient(str(config.writer_socket), principal.token, timeout=30)
        self.dashboard = dashboard_client
        self.timeout = timeout_client

    def _targets(self, client: WriterClient) -> list[dict[str, Any]]:
        value = client.list_agenda_feedback_targets(
            endpoint_ref=self.config.endpoint_ref, limit=200
        )
        if not isinstance(value, list):
            raise AgendaControlError("writer returned invalid Agenda targets")
        return value

    @staticmethod
    def _decision_ref(target: Mapping[str, Any]) -> str:
        payload = target.get("payload")
        if not isinstance(payload, Mapping):
            raise AgendaControlError("Agenda target payload is invalid")
        return _string(payload.get("decision_ref"), "decision_ref")

    @staticmethod
    def _human_feedback(latest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [
            value for subject, value in latest.items()
            if isinstance(subject, str) and subject.startswith("human:")
            and isinstance(value, Mapping)
        ]

    def view(self, login: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        subject = _subject_for_login(login)
        rows = []
        for target in self._targets(self.dashboard):
            payload = target.get("payload")
            latest = target.get("latest_feedback")
            if not isinstance(payload, Mapping) or not isinstance(latest, Mapping):
                raise AgendaControlError("Agenda target is invalid")
            delivered = _parse_time(target.get("delivered_at"), "delivered_at")
            deadline = delivered + timedelta(seconds=self.config.feedback_timeout_seconds)
            own = latest.get(subject)
            human = self._human_feedback(latest)
            automatic = latest.get(AUTOMATION_SUBJECT)
            if human:
                verdicts = {str(item.get("verdict")) for item in human}
                effective = next(iter(verdicts)) if len(verdicts) == 1 else "mixed"
                resolution = "explicit_human"
            elif isinstance(automatic, Mapping):
                effective = "agree"
                resolution = "auto_accept_timeout"
            else:
                effective = "pending"
                resolution = "pending"
            rows.append({
                "decision_ref": self._decision_ref(target),
                "company_ref": payload.get("company_ref"),
                "selected": payload.get("selected", []),
                "deferred_count": int(payload.get("deferred_count", 0)),
                "rejected_count": int(payload.get("rejected_count", 0)),
                "delivered_at": delivered.isoformat(timespec="seconds"),
                "deadline_at": deadline.isoformat(timespec="seconds"),
                "seconds_remaining": max(0, math.ceil((deadline - now).total_seconds())),
                "own_verdict": own.get("verdict") if isinstance(own, Mapping) else None,
                "effective_verdict": effective,
                "resolution": resolution,
            })
        return {
            "as_of": now.isoformat(timespec="seconds"),
            "timeout_seconds": self.config.feedback_timeout_seconds,
            "items": rows,
        }

    def record(self, login: str, decision_ref: str, verdict: str) -> dict[str, Any]:
        decision_ref = _string(decision_ref, "decision_ref")
        if verdict not in {"agree", "disagree"}:
            raise AgendaControlError("dashboard verdict must be agree or disagree")
        subject = _subject_for_login(login)
        target = next(
            (item for item in self._targets(self.dashboard) if self._decision_ref(item) == decision_ref),
            None,
        )
        if target is None:
            raise AgendaControlError("Agenda decision is not available for feedback")
        latest = target.get("latest_feedback")
        if not isinstance(latest, Mapping):
            raise AgendaControlError("Agenda feedback state is invalid")
        previous = latest.get(subject)
        if isinstance(previous, Mapping) and previous.get("verdict") == verdict:
            return {"status": "duplicate", "decision_ref": decision_ref, "verdict": verdict}
        prior_ref = previous.get("feedback_id") if isinstance(previous, Mapping) else None
        nonce = secrets.token_hex(16)
        identity = {
            "decision_ref": decision_ref, "subject_ref": subject,
            "prior_feedback_ref": prior_ref, "verdict": verdict, "nonce": nonce,
        }
        digest = content_hash(identity)[:32]
        result = self.dashboard.record_agenda_feedback(
            decision_id=decision_ref,
            verdict=verdict,
            notes="Tailscale dashboard action",
            feedback_id=f"agenda-feedback:{digest}",
            idempotency_key=f"agenda-dashboard-feedback:{digest}",
            subject_ref=subject,
            prior_feedback_ref=prior_ref,
            source="tailscale_dashboard",
            source_event_ref=f"dashboard-feedback:{digest}",
        )
        return {"status": result.get("status"), "decision_ref": decision_ref, "verdict": verdict}

    def sweep(self, *, now: datetime | None = None) -> dict[str, int]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        checked = recorded = pending = explicit = existing = 0
        for target in self._targets(self.timeout):
            checked += 1
            latest = target.get("latest_feedback")
            if not isinstance(latest, Mapping):
                raise AgendaControlError("Agenda feedback state is invalid")
            if self._human_feedback(latest):
                explicit += 1
                continue
            if AUTOMATION_SUBJECT in latest:
                existing += 1
                continue
            delivered = _parse_time(target.get("delivered_at"), "delivered_at")
            deadline = delivered + timedelta(seconds=self.config.feedback_timeout_seconds)
            if now < deadline:
                pending += 1
                continue
            decision_ref = self._decision_ref(target)
            digest = content_hash({
                "decision_ref": decision_ref,
                "delivered_at": delivered.isoformat(timespec="microseconds"),
                "timeout_seconds": self.config.feedback_timeout_seconds,
            })[:32]
            result = self.timeout.record_agenda_feedback(
                decision_id=decision_ref,
                verdict="agree",
                notes=f"No explicit human response within {self.config.feedback_timeout_seconds} seconds",
                feedback_id=f"agenda-feedback:{digest}",
                idempotency_key=f"agenda-auto-accept:{digest}",
                subject_ref=AUTOMATION_SUBJECT,
                prior_feedback_ref=None,
                source="auto_accept_timeout",
                source_event_ref=f"agenda-timeout:{digest}",
            )
            if result.get("status") == "fresh":
                recorded += 1
        return {
            "checked": checked, "recorded": recorded, "pending": pending,
            "explicit": explicit, "existing": existing,
        }


@dataclass(slots=True)
class _Session:
    login: str
    csrf: str
    expires_at: float


class AgendaControlApplication:
    def __init__(
        self,
        config: AgendaControlConfig,
        plane: AgendaControlPlane,
        review_plane: ResearchReviewControlPlane | None = None,
    ) -> None:
        self.config = config
        self.plane = plane
        self.review_plane = review_plane
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
                session_id = cookie.get("dalton_session").value if cookie.get("dalton_session") else None
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
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != {"decision_ref", "verdict"}:
            raise AgendaControlError("request body has an invalid closed shape")
        return self.plane.record(login, value["decision_ref"], value["verdict"])

    def _review_body(self, session: _Session, csrf: str | None, body: bytes) -> Mapping[str, Any]:
        if self.review_plane is None:
            raise AgendaControlError("research review is not enabled")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping):
            raise AgendaControlError("request body must be an object")
        return value

    def post_review(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        value = self._review_body(session, csrf, body)
        assert self.review_plane is not None
        return self.review_plane.record(login, value)

    def post_transcript_review(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        value = self._review_body(session, csrf, body)
        assert self.review_plane is not None
        return self.review_plane.record_transcript(login, value)


def _handler(application: AgendaControlApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DaltonCockpit/0.2"

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

        def _send(
            self, status: int, content_type: str, body: bytes, *, session_cookie: str | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
            )
            if session_cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"dalton_session={session_cookie}; Path=/; Secure; HttpOnly; SameSite=Strict",
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
                elif path == "/v1/agenda":
                    value = application.plane.view(login)
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/research-review":
                    value = (
                        {"as_of": datetime.now(timezone.utc).isoformat(), "items": [], "enabled": False}
                        if application.review_plane is None
                        else {**application.review_plane.view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/transcript-review":
                    value = (
                        {"as_of": datetime.now(timezone.utc).isoformat(), "items": [], "enabled": False}
                        if application.review_plane is None
                        else {**application.review_plane.transcript_view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/research-trajectory":
                    value = (
                        {
                            "as_of": datetime.now(timezone.utc).isoformat(),
                            "items": [],
                            "projection_only": True,
                            "enabled": False,
                        }
                        if application.review_plane is None
                        else {**application.review_plane.trajectory_view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                else:
                    self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}')
                    return
            except Exception:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json", b'{"error":"unavailable"}')
                return
            self._send(
                HTTPStatus.OK, content_type, body,
                session_cookie=session_id if created else None,
            )

        def do_POST(self) -> None:  # noqa: N802
            context = self._context()
            if context is None:
                return
            login, session_id, session, created = context
            path = urlparse(self.path).path
            actions = {
                "/v1/agenda/feedback": application.post,
                "/v1/research-review/decision": application.post_review,
                "/v1/transcript-review/decision": application.post_transcript_review,
            }
            action = actions.get(path)
            if action is None:
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
                result = action(
                    login, session, self.headers.get("X-Dalton-CSRF"), self.rfile.read(int(length))
                )
            except PermissionError:
                self._send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"csrf"}')
                return
            except (AgendaControlError, ResearchReviewControlError, ResearchReviewError):
                self._send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"invalid_request"}')
                return
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            self._send(
                HTTPStatus.OK, "application/json; charset=utf-8", body,
                session_cookie=session_id if created else None,
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def serve(config: AgendaControlConfig) -> None:
    if config.host not in {"127.0.0.1", "::1"}:
        raise AgendaControlError("Agenda control must bind loopback")
    plane = AgendaControlPlane(config)
    review_plane = (
        None
        if config.research_review is None
        else ResearchReviewControlPlane(
            config.research_review,
            writer_socket=config.writer_socket,
            token_config=config.token_config,
        )
    )
    application = AgendaControlApplication(config, plane, review_plane)
    stop = threading.Event()

    def sweep_loop() -> None:
        while not stop.is_set():
            try:
                plane.sweep()
            except Exception:
                pass
            stop.wait(config.sweep_interval_seconds)

    sweeper = threading.Thread(target=sweep_loop, name="dalton-agenda-timeout", daemon=True)
    sweeper.start()
    reconciler = None
    if review_plane is not None:
        def reconcile_loop() -> None:
            while not stop.is_set():
                try:
                    review_plane.reconcile()
                except Exception:
                    pass
                stop.wait(config.research_review.reconcile_interval_seconds)

        reconciler = threading.Thread(
            target=reconcile_loop,
            name="dalton-research-review-reconcile",
            daemon=True,
        )
        reconciler.start()
    server = ThreadingHTTPServer((config.host, config.port), _handler(application))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        sweeper.join(timeout=5)
        if reconciler is not None:
            reconciler.join(timeout=5)
        if review_plane is not None:
            review_plane.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the owner-only Dalton Cockpit")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(AgendaControlConfig.from_service_file(args.config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AgendaControlApplication", "AgendaControlConfig", "AgendaControlError",
    "AgendaControlPlane", "serve",
]
