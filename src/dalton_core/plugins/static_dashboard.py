"""Render and publish a self-contained Dalton supervision dashboard.

The plugin reads only the disposable dashboard projection.  The Tencent COS
adapter intentionally uploads one scoped object and never changes bucket
website configuration, so the existing site root remains untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..dashboard import DashboardQueryService, _HTML_PATH


class StaticDashboardError(RuntimeError):
    pass


_EMBED_MARKER = "const EMBEDDED_DATA = null;"


def _safe_script_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def dashboard_snapshot(projection_db: str | Path) -> dict[str, Any]:
    """Materialize the fixed dashboard API surface into one JSON object."""
    service = DashboardQueryService(projection_db)
    try:
        workflows = service.workflows(limit=200)
        data: dict[str, Any] = {
            "/v1/dashboard/summary": service.summary(),
            "/v1/workflows": workflows,
            "/v1/models": service.models(limit=200),
            "/v1/agenda": service.agenda(limit=30),
            "/v1/metadata-sources": service.metadata_sources(limit=100),
            "/v1/connectors": service.connectors(limit=100),
            "/v1/capabilities": service.capabilities(limit=200),
            "/v1/artifacts": service.artifacts(limit=200),
            "/v1/projection/status": service.status(),
        }
        for row in workflows["data"][:8]:
            workflow_ref = row["workflow_ref"]
            encoded = urllib.parse.quote(workflow_ref, safe="")
            data[f"/v1/workflows/{encoded}/tree"] = service.workflow_tree(workflow_ref)
        return data
    finally:
        service.close()


def render_static_dashboard(
    projection_db: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Atomically render a self-contained HTML dashboard from a projection."""
    snapshot = dashboard_snapshot(projection_db)
    template = _HTML_PATH.read_text(encoding="utf-8")
    if template.count(_EMBED_MARKER) != 1:
        raise StaticDashboardError("dashboard template embed marker is missing or ambiguous")
    html = template.replace(
        _EMBED_MARKER,
        f"const EMBEDDED_DATA = {_safe_script_json(snapshot)};",
        1,
    )
    body = html.encode("utf-8")
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    status = snapshot["/v1/projection/status"]
    return {
        "path": str(destination),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "as_of": status["as_of"],
        "projection_watermark": status["projection_watermark"],
    }


def _fetch(url: str, *, timeout: float = 30.0) -> tuple[bytes, Mapping[str, str], int]:
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}_dalton_verify={time.time_ns()}",
        headers={"User-Agent": "Dalton static-dashboard verifier", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.read(), headers, response.status


@dataclass(frozen=True, slots=True)
class TencentCosConfig:
    bucket: str
    region: str
    key: str
    public_url: str
    keychain_account: str
    secret_id_service: str
    secret_key_service: str
    protected_urls: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TencentCosConfig":
        expected = {
            "bucket", "region", "key", "public_url", "keychain_account",
            "secret_id_service", "secret_key_service", "protected_urls",
        }
        if set(raw) != expected:
            raise StaticDashboardError("Tencent COS config has an invalid shape")
        values = [raw[name] for name in expected - {"protected_urls"}]
        if any(not isinstance(value, str) or not value for value in values):
            raise StaticDashboardError("Tencent COS config contains an empty string")
        protected = raw["protected_urls"]
        if not isinstance(protected, list) or any(
            not isinstance(value, str) or not value.startswith("https://") for value in protected
        ):
            raise StaticDashboardError("protected_urls must contain HTTPS URLs")
        if raw["key"] != "dalton/index.html":
            raise StaticDashboardError("the Dalton publisher is restricted to dalton/index.html")
        if not raw["public_url"].startswith("https://"):
            raise StaticDashboardError("public_url must use HTTPS")
        return cls(
            bucket=raw["bucket"],
            region=raw["region"],
            key=raw["key"],
            public_url=raw["public_url"],
            keychain_account=raw["keychain_account"],
            secret_id_service=raw["secret_id_service"],
            secret_key_service=raw["secret_key_service"],
            protected_urls=tuple(protected),
        )


class TencentCosPublisher:
    def __init__(self, config: TencentCosConfig) -> None:
        self.config = config

    def _credential(self, service: str) -> str:
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-a", self.config.keychain_account,
                    "-s", service,
                    "-w",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StaticDashboardError(f"Keychain credential is unavailable: {service}") from exc
        value = result.stdout.strip()
        if not value:
            raise StaticDashboardError(f"Keychain credential is empty: {service}")
        return value

    def _client(self) -> Any:
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise StaticDashboardError(
                "cos-python-sdk-v5 is missing; install dalton-core[deploy]"
            ) from exc
        config = CosConfig(
            Region=self.config.region,
            SecretId=self._credential(self.config.secret_id_service),
            SecretKey=self._credential(self.config.secret_key_service),
            Scheme="https",
        )
        return CosS3Client(config)

    def publish(self, path: str | Path) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise StaticDashboardError(f"dashboard HTML does not exist: {source}")
        body = source.read_bytes()
        if not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise StaticDashboardError("dashboard output is not a complete HTML document")
        local_sha = hashlib.sha256(body).hexdigest()
        protected_before = {
            url: hashlib.sha256(_fetch(url)[0]).hexdigest()
            for url in self.config.protected_urls
        }
        response = self._client().put_object(
            Bucket=self.config.bucket,
            Key=self.config.key,
            Body=body,
            ContentType="text/html; charset=utf-8",
            ContentDisposition="inline",
            CacheControl="no-cache, no-store, must-revalidate",
            ACL="public-read",
        )
        remote_body, headers, status = _fetch(self.config.public_url)
        remote_sha = hashlib.sha256(remote_body).hexdigest()
        if remote_sha != local_sha:
            raise StaticDashboardError(
                f"COS readback mismatch: local={local_sha} remote={remote_sha}"
            )
        for url, before in protected_before.items():
            after = hashlib.sha256(_fetch(url)[0]).hexdigest()
            if before != after:
                raise StaticDashboardError(f"protected dashboard changed during publish: {url}")
        content_type = headers.get("content-type", "")
        disposition = headers.get("content-disposition", "")
        force_download = headers.get("x-cos-force-download", "").lower() == "true"
        if force_download or "attachment" in disposition.lower():
            raise StaticDashboardError("published HTML is configured as a download")
        return {
            "ok": True,
            "bucket": self.config.bucket,
            "region": self.config.region,
            "key": self.config.key,
            "public_url": self.config.public_url,
            "status": status,
            "content_type": content_type,
            "etag": str(response.get("ETag", "")).strip('"'),
            "bytes": len(body),
            "sha256": local_sha,
        }


@dataclass(slots=True)
class StaticDashboardPlugin:
    output_path: Path
    publisher: TencentCosPublisher | None = None
    name: str = "static_dashboard"
    last_render: dict[str, Any] | None = field(default=None, init=False)
    last_publish: dict[str, Any] | None = field(default=None, init=False)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StaticDashboardPlugin":
        expected = {"type", "enabled", "output_path", "publisher"}
        if set(raw) != expected or raw.get("type") != "static_dashboard":
            raise StaticDashboardError("static dashboard plugin config has an invalid shape")
        if raw.get("enabled") is not True:
            raise StaticDashboardError("disabled plugins must be omitted from service config")
        output_path = raw.get("output_path")
        if not isinstance(output_path, str) or not Path(output_path).is_absolute():
            raise StaticDashboardError("static dashboard output_path must be absolute")
        publisher_raw = raw.get("publisher")
        publisher = None
        if publisher_raw is not None:
            if not isinstance(publisher_raw, Mapping) or publisher_raw.get("type") != "tencent_cos":
                raise StaticDashboardError("unsupported static dashboard publisher")
            publisher_fields = dict(publisher_raw)
            publisher_fields.pop("type")
            publisher = TencentCosPublisher(TencentCosConfig.from_mapping(publisher_fields))
        return cls(Path(output_path), publisher)

    def on_projection(self, projection_db: str | Path) -> dict[str, Any]:
        rendered = render_static_dashboard(projection_db, self.output_path)
        self.last_render = rendered
        published = None if self.publisher is None else self.publisher.publish(self.output_path)
        self.last_publish = published
        return {"render": rendered, "publish": published}
