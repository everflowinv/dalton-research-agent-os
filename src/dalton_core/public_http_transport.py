"""SSRF-safe, credential-free HTTPS transport for public connectors.

The transport validates every URL and every redirect hop, resolves DNS once
per hop, rejects the entire answer set if any address is non-public, and pins
the socket to one validated address while retaining the original hostname for
TLS SNI and certificate checks.  It does not accept credential grants,
Authorization/Cookie headers, URL userinfo, or credential-shaped query args.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, urljoin, urlsplit


class PublicTransportError(Exception):
    pass


class PublicTransportPolicyError(PublicTransportError, ValueError):
    pass


class PublicTransportNetworkError(PublicTransportError):
    pass


class PublicTransportResponseTooLarge(PublicTransportError):
    pass


_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "authorization", "cookie", "host", "proxy-authorization", "proxy-connection",
        "te", "trailer", "transfer-encoding", "upgrade", "x-api-key",
    }
)
_ALLOWED_REQUEST_HEADERS = frozenset(
    {"accept", "accept-language", "content-type", "user-agent"}
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token", "api_key", "apikey", "authorization", "client_secret",
        "cookie", "credential", "key", "password", "refresh_token", "secret",
        "sig", "signature", "token",
    }
)
_SENSITIVE_RESPONSE_HEADERS = frozenset(
    {
        "authentication-info", "proxy-authenticate", "proxy-authentication-info",
        "set-cookie", "set-cookie2", "www-authenticate",
    }
)


def _credential_shaped_name(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if normalized in _SENSITIVE_QUERY_KEYS:
        return True
    return any(part in _SENSITIVE_QUERY_KEYS for part in normalized.split("_") if part)


@dataclass(frozen=True, slots=True)
class PublicHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class ResolvedPublicTarget:
    url: str
    hostname: str
    port: int
    connect_ip: str
    request_target: str


@dataclass(frozen=True, slots=True)
class PublicHttpResponse:
    status: int
    reason: str
    headers: Mapping[str, str]
    final_url: str
    redirect_chain: tuple[str, ...]
    bytes_written: int
    resolved_ips: tuple[str, ...]
    body: bytes


class ResponseLike(Protocol):
    status: int
    reason: str

    def getheaders(self) -> Sequence[tuple[str, str]]: ...

    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...


class _ConnectionBoundResponse:
    def __init__(
        self, response: http.client.HTTPResponse, connection: http.client.HTTPSConnection
    ) -> None:
        self._response = response
        self._connection = connection
        self.status = response.status
        self.reason = response.reason

    def getheaders(self) -> list[tuple[str, str]]:
        return self._response.getheaders()

    def read(self, amt: int | None = None) -> bytes:
        return self._response.read(amt)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


Exchange = Callable[
    [ResolvedPublicTarget, str, Mapping[str, str], bytes | None, float],
    ResponseLike,
]
Resolver = Callable[[str, int], Sequence[str]]


def _canonical_hostname(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise PublicTransportPolicyError("URL hostname is required")
    if "%" in value:
        raise PublicTransportPolicyError("IPv6 zone identifiers are forbidden")
    try:
        ascii_host = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise PublicTransportPolicyError("URL hostname is not valid IDNA") from exc
    if not ascii_host or any(char.isspace() for char in ascii_host):
        raise PublicTransportPolicyError("URL hostname is invalid")
    return ascii_host


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise PublicTransportPolicyError("DNS resolver returned a non-IP value") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global:
        raise PublicTransportPolicyError(f"DNS resolved to non-public address {address}")
    return address.compressed


def system_public_resolver(hostname: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError as exc:
        raise PublicTransportNetworkError("public DNS resolution failed") from exc
    values: list[str] = []
    for answer in answers:
        value = answer[4][0]
        if value not in values:
            values.append(value)
    return tuple(values)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        connect_ip: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._connect_ip, self.port), self.timeout, self.source_address
        )
        try:
            peer = _public_address(str(raw.getpeername()[0]))
            if peer != _public_address(self._connect_ip):
                raise PublicTransportPolicyError("connected peer does not match pinned DNS address")
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def pinned_https_exchange(
    target: ResolvedPublicTarget,
    method: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> ResponseLike:
    context = ssl.create_default_context()
    connection = _PinnedHTTPSConnection(
        target.hostname, target.connect_ip, target.port,
        timeout=timeout, context=context,
    )
    try:
        connection.request(method, target.request_target, body=body, headers=dict(headers))
        response = connection.getresponse()
    except BaseException:
        connection.close()
        raise
    return _ConnectionBoundResponse(response, connection)


class PublicHttpTransport:
    """Perform one bounded public HTTPS request without credential authority."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        exchange: Exchange | None = None,
        chunk_size: int = 64 * 1024,
    ) -> None:
        if type(chunk_size) is not int or chunk_size < 1:
            raise PublicTransportPolicyError("chunk_size must be positive")
        self._resolver = resolver or system_public_resolver
        self._exchange = exchange or pinned_https_exchange
        self._chunk_size = chunk_size

    @staticmethod
    def _headers(headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping):
            raise PublicTransportPolicyError("request headers must be an object")
        result: dict[str, str] = {}
        for raw_name, raw_value in headers.items():
            if not isinstance(raw_name, str) or not raw_name:
                raise PublicTransportPolicyError("request header name is invalid")
            name = raw_name.strip().lower()
            if name in _FORBIDDEN_REQUEST_HEADERS or name not in _ALLOWED_REQUEST_HEADERS:
                raise PublicTransportPolicyError(f"request header {name!r} is not allowed")
            if (
                not isinstance(raw_value, str)
                or any(ord(char) < 32 and char != "\t" for char in raw_value)
                or "\x7f" in raw_value
            ):
                raise PublicTransportPolicyError("request header value is invalid")
            if name in result:
                raise PublicTransportPolicyError("duplicate request headers are forbidden")
            result[name] = raw_value
        return result

    @staticmethod
    def _credential_shaped(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if _credential_shaped_name(key):
                    return True
                if PublicHttpTransport._credential_shaped(child):
                    return True
        elif isinstance(value, list):
            return any(PublicHttpTransport._credential_shaped(item) for item in value)
        return False

    @staticmethod
    def _validate_body(body: bytes | None, headers: Mapping[str, str]) -> None:
        if body in (None, b""):
            return
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PublicTransportPolicyError("JSON request body is invalid") from exc
            if PublicHttpTransport._credential_shaped(parsed):
                raise PublicTransportPolicyError(
                    "credential-shaped JSON request fields are forbidden"
                )
            return
        if content_type == "application/x-www-form-urlencoded":
            try:
                encoded = body.decode("ascii")
            except UnicodeDecodeError as exc:
                raise PublicTransportPolicyError("form request body must be ASCII") from exc
            for key, _ in parse_qsl(encoded, keep_blank_values=True):
                if _credential_shaped_name(key):
                    raise PublicTransportPolicyError(
                        "credential-shaped form fields are forbidden"
                    )
            return
        raise PublicTransportPolicyError(
            "public request bodies require JSON or form content type"
        )

    def _target(self, url: str, allowed_hosts: frozenset[str]) -> tuple[ResolvedPublicTarget, tuple[str, ...]]:
        if not isinstance(url, str) or not url:
            raise PublicTransportPolicyError("request URL must be non-empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise PublicTransportPolicyError("request URL contains control characters")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise PublicTransportPolicyError("request URL is malformed") from exc
        if parsed.scheme.lower() != "https":
            raise PublicTransportPolicyError("public connector transport requires https")
        if parsed.username is not None or parsed.password is not None:
            raise PublicTransportPolicyError("URL userinfo is forbidden")
        if parsed.fragment:
            raise PublicTransportPolicyError("URL fragments are forbidden")
        hostname = _canonical_hostname(parsed.hostname or "")
        if hostname not in allowed_hosts:
            raise PublicTransportPolicyError("URL hostname is not exactly allowlisted")
        if port not in (None, 443):
            raise PublicTransportPolicyError("public connector transport only permits port 443")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False):
            if _credential_shaped_name(key):
                raise PublicTransportPolicyError(
                    "credential-shaped query parameters are forbidden"
                )
        answers = tuple(self._resolver(hostname, 443))
        if not answers:
            raise PublicTransportNetworkError("public DNS resolution returned no addresses")
        public_answers = tuple(_public_address(answer) for answer in answers)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += "?" + parsed.query
        return (
            ResolvedPublicTarget(
                url=url,
                hostname=hostname,
                port=443,
                connect_ip=public_answers[0],
                request_target=request_target,
            ),
            public_answers,
        )

    @staticmethod
    def _response_headers(response: ResponseLike) -> tuple[dict[str, str], str | None]:
        result: dict[str, str] = {}
        locations: list[str] = []
        for raw_name, raw_value in response.getheaders():
            name = raw_name.strip().lower()
            if name == "location":
                locations.append(raw_value)
                continue
            if name in _SENSITIVE_RESPONSE_HEADERS:
                continue
            if name in result:
                if name == "content-length" and result[name] != raw_value:
                    raise PublicTransportPolicyError(
                        "conflicting Content-Length headers are forbidden"
                    )
                continue
            result[name] = raw_value
        if len(locations) > 1:
            raise PublicTransportPolicyError("multiple redirect Location headers are forbidden")
        return result, locations[0] if locations else None

    def request(
        self,
        request: PublicHttpRequest,
        raw_sink: Any,
        *,
        allowed_hosts: Sequence[str],
        allow_redirects: bool,
        max_redirects: int,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> PublicHttpResponse:
        method = request.method.upper() if isinstance(request.method, str) else ""
        if method not in _ALLOWED_METHODS:
            raise PublicTransportPolicyError("public request method is not allowed")
        headers = self._headers(request.headers)
        if request.body is not None and not isinstance(request.body, bytes):
            raise PublicTransportPolicyError("request body must be bytes or null")
        if method in {"GET", "HEAD"} and request.body not in (None, b""):
            raise PublicTransportPolicyError("GET/HEAD requests cannot carry a body")
        self._validate_body(request.body, headers)
        if type(allow_redirects) is not bool:
            raise PublicTransportPolicyError("allow_redirects must be boolean")
        if type(max_redirects) is not int or max_redirects < 0:
            raise PublicTransportPolicyError("max_redirects must be non-negative")
        if not allow_redirects and max_redirects != 0:
            raise PublicTransportPolicyError("disabled redirects require max_redirects=0")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise PublicTransportPolicyError("max_response_bytes must be positive")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise PublicTransportPolicyError("timeout_seconds must be positive")
        if isinstance(allowed_hosts, (str, bytes)) or not isinstance(allowed_hosts, Sequence):
            raise PublicTransportPolicyError("allowed_hosts must be an array")
        supplied_hosts = list(allowed_hosts)
        canonical_hosts = frozenset(_canonical_hostname(host) for host in supplied_hosts)
        if not canonical_hosts:
            raise PublicTransportPolicyError("allowed_hosts must not be empty")
        if len(canonical_hosts) != len(supplied_hosts):
            raise PublicTransportPolicyError("allowed_hosts must be unique")

        current_url = request.url
        current_method = method
        current_body = request.body
        redirect_chain: list[str] = []
        resolved_history: list[str] = []
        while True:
            target, resolved = self._target(current_url, canonical_hosts)
            resolved_history.extend(resolved)
            try:
                response = self._exchange(
                    target, current_method, headers, current_body,
                    float(timeout_seconds),
                )
            except PublicTransportError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise PublicTransportNetworkError("public HTTPS request failed") from exc
            try:
                response_headers, location = self._response_headers(response)
                status = int(response.status)
                if status in _REDIRECT_STATUSES:
                    if not allow_redirects:
                        raise PublicTransportPolicyError("redirects are disabled")
                    if location is None:
                        raise PublicTransportPolicyError("redirect response lacks Location")
                    if len(redirect_chain) >= max_redirects:
                        raise PublicTransportPolicyError("redirect limit exceeded")
                    if current_method not in {"GET", "HEAD"}:
                        raise PublicTransportPolicyError(
                            "redirects for non-idempotent public requests are forbidden"
                        )
                    next_url = urljoin(current_url, location)
                    self._target(next_url, canonical_hosts)
                    redirect_chain.append(next_url)
                    current_url = next_url
                    continue
                content_length = response_headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise PublicTransportPolicyError("invalid Content-Length") from exc
                    if declared_length < 0 or declared_length > max_response_bytes:
                        raise PublicTransportResponseTooLarge(
                            "response Content-Length exceeds the configured limit"
                        )
                written = 0
                body_chunks: list[bytes] = []
                if current_method != "HEAD":
                    while True:
                        chunk = response.read(self._chunk_size)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise PublicTransportNetworkError("response reader returned non-bytes")
                        written += len(chunk)
                        if written > max_response_bytes:
                            raise PublicTransportResponseTooLarge(
                                "response body exceeds the configured limit"
                            )
                        raw_sink.write(chunk)
                        body_chunks.append(chunk)
                return PublicHttpResponse(
                    status=status,
                    reason=str(response.reason or ""),
                    headers=response_headers,
                    final_url=current_url,
                    redirect_chain=tuple(redirect_chain),
                    bytes_written=written,
                    resolved_ips=tuple(resolved_history),
                    body=b"".join(body_chunks),
                )
            finally:
                response.close()


__all__ = [
    "PublicHttpRequest", "PublicHttpResponse", "PublicHttpTransport",
    "PublicTransportError", "PublicTransportNetworkError",
    "PublicTransportPolicyError", "PublicTransportResponseTooLarge",
    "ResolvedPublicTarget", "pinned_https_exchange", "system_public_resolver",
]
