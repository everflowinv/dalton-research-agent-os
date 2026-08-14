"""Source-specific, offline-only CNINFO and SEC recorded adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .connector_runner import validate_adapter_transport_observation
from .store import canonical_json, content_hash


REFERENCE_FIXTURE_DIR = Path(__file__).with_name("reference_shadow_fixtures")
RECORDED_FIXTURE_CREATED_AT = "2026-08-14T21:00:00.000000+00:00"
_SCENARIOS = frozenset(
    {"success", "empty", "pagination", "partial", "schema_drift", "rate_limited", "timeout", "malformed"}
)
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


class RecordedSourceError(ValueError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecordedSourceError(f"{name} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise RecordedSourceError(
            f"{name} has invalid closed shape; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RecordedSourceError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecordedSourceError(f"{name} must be a non-empty string")
    return value.strip()


def _page(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecordedSourceError(f"{name} must be a positive integer")
    return value


def _nullable_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def validate_recorded_source_fixture(spec: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(
        spec,
        {
            "schema_version", "id", "created_at", "source_ref", "operation",
            "parent_parameters", "parent_query_hash", "scenarios", "content_hash",
        },
        "RecordedSourceFixture",
    )
    if wire["schema_version"] != "0.1":
        raise RecordedSourceError("unsupported RecordedSourceFixture schema_version")
    for name in ("id", "created_at", "source_ref", "operation"):
        wire[name] = _text(wire[name], name)
    expected_operation = {
        "source:cninfo": "list_announcements",
        "source:sec-edgar": "list_filings",
    }.get(wire["source_ref"])
    if expected_operation != wire["operation"]:
        raise RecordedSourceError("fixture source/operation pair is not a reference source")
    slug = "cninfo" if wire["source_ref"] == "source:cninfo" else "sec"
    if (
        wire["id"] != f"recorded-source-fixture:{slug}:0.1"
        or wire["created_at"] != RECORDED_FIXTURE_CREATED_AT
    ):
        raise RecordedSourceError("recorded fixture identity is not frozen")
    expected_parameters = (
        {
            "stock_code": "600309", "date_from": "2026-01-01",
            "date_to": "2026-06-30", "page": 1, "page_size": 50,
        }
        if slug == "cninfo"
        else {
            "issuer": "0000000001", "form": "10-Q",
            "date_from": "2026-01-01", "date_to": "2026-06-30", "limit": 50,
        }
    )
    if wire["parent_parameters"] != expected_parameters:
        raise RecordedSourceError("fixture parent parameters are not frozen")
    expected_query_hash = content_hash(
        {"operation": wire["operation"], "parameters": expected_parameters}
    )
    if wire["parent_query_hash"] != expected_query_hash:
        raise RecordedSourceError("fixture parent_query_hash is not exact")
    if not isinstance(wire["scenarios"], list):
        raise RecordedSourceError("scenarios must be an array")
    scenario_fields = {
        "scenario", "behavior", "advance_seconds", "retry_after_ms", "error_code", "pages"
    }
    page_fields = {
        "ordinal", "provider_request_id", "provider_status", "request_cursor",
        "next_cursor", "raw_payload",
    }
    names: set[str] = set()
    scenarios: list[dict[str, Any]] = []
    for scenario_index, item in enumerate(wire["scenarios"]):
        scenario = _closed(item, scenario_fields, f"scenarios[{scenario_index}]")
        scenario["scenario"] = _text(scenario["scenario"], "scenario")
        if scenario["scenario"] in names or scenario["scenario"] not in _SCENARIOS:
            raise RecordedSourceError("scenario names must be the unique frozen matrix")
        names.add(scenario["scenario"])
        if scenario["behavior"] not in {
            "return", "rate_limited", "timeout", "normalize_error"
        }:
            raise RecordedSourceError("fixture behavior is invalid")
        expected_behavior = {
            "success": "return", "empty": "return", "pagination": "return",
            "partial": "return", "schema_drift": "normalize_error",
            "rate_limited": "rate_limited", "timeout": "timeout",
            "malformed": "normalize_error",
        }[scenario["scenario"]]
        if scenario["behavior"] != expected_behavior:
            raise RecordedSourceError("scenario does not bind its recorded behavior")
        advance = scenario["advance_seconds"]
        if isinstance(advance, bool) or not isinstance(advance, int) or advance < 0:
            raise RecordedSourceError("advance_seconds must be non-negative")
        retry = scenario["retry_after_ms"]
        if retry is not None and (
            isinstance(retry, bool) or not isinstance(retry, int) or retry < 1
        ):
            raise RecordedSourceError("retry_after_ms must be positive or null")
        scenario["error_code"] = _nullable_text(scenario["error_code"], "error_code")
        expected_error = (
            scenario["scenario"]
            if scenario["scenario"] in {
                "schema_drift", "rate_limited", "timeout", "malformed"
            }
            else None
        )
        if scenario["error_code"] != expected_error:
            raise RecordedSourceError("scenario error_code is not exact")
        if (scenario["scenario"] == "rate_limited") != (retry is not None):
            raise RecordedSourceError("retry_after_ms is reserved for rate limiting")
        if scenario["scenario"] == "timeout":
            if advance <= 0:
                raise RecordedSourceError("timeout fixture must advance the runner clock")
        elif advance != 0:
            raise RecordedSourceError("only timeout fixtures may advance the clock")
        if not isinstance(scenario["pages"], list):
            raise RecordedSourceError("scenario pages must be an array")
        pages: list[dict[str, Any]] = []
        scenario_label = scenario["scenario"].replace("_", "-")
        for page_index, raw_page in enumerate(scenario["pages"], start=1):
            page = _closed(raw_page, page_fields, f"pages[{page_index}]")
            if _page(page["ordinal"], "ordinal") != page_index:
                raise RecordedSourceError("fixture pages must be contiguous")
            page["provider_request_id"] = _text(
                page["provider_request_id"], "provider_request_id"
            )
            if page["provider_request_id"] != (
                f"fixture:{slug}:{scenario_label}:request:{page_index}"
            ):
                raise RecordedSourceError("provider_request_id is not authority-derived")
            status = page["provider_status"]
            if status != 200:
                raise RecordedSourceError("recorded pages require provider_status 200")
            page["request_cursor"] = _nullable_text(page["request_cursor"], "request_cursor")
            page["next_cursor"] = _nullable_text(page["next_cursor"], "next_cursor")
            if page_index == 1 and page["request_cursor"] is not None:
                raise RecordedSourceError("first recorded page must start without a cursor")
            if page_index > 1 and page["request_cursor"] != pages[-1]["next_cursor"]:
                raise RecordedSourceError("fixture cursor chain is not contiguous")
            if not isinstance(page["raw_payload"], (Mapping, str)):
                raise RecordedSourceError("raw_payload must be an object or malformed string")
            pages.append(page)
        scenario["pages"] = pages
        if scenario["behavior"] == "rate_limited":
            if pages or retry is None or scenario["scenario"] != "rate_limited":
                raise RecordedSourceError("rate-limit fixture shape is inconsistent")
        elif not pages:
            raise RecordedSourceError("non-rate-limit fixture requires recorded pages")
        scenario_name = scenario["scenario"]
        if scenario_name == "pagination":
            if len(pages) < 2 or pages[-1]["next_cursor"] is not None:
                raise RecordedSourceError("pagination fixture must terminate after multiple pages")
            if any(page["next_cursor"] is None for page in pages[:-1]):
                raise RecordedSourceError("pagination fixture cannot terminate early")
        elif scenario_name == "partial":
            if pages[-1]["next_cursor"] is None:
                raise RecordedSourceError("partial fixture must preserve a continuation cursor")
        elif pages and pages[-1]["next_cursor"] is not None:
            raise RecordedSourceError("terminal fixture cannot claim a continuation cursor")
        normalizer = _normalize_cninfo if slug == "cninfo" else _normalize_sec
        if scenario_name in {"success", "empty", "pagination", "partial", "timeout"}:
            normalized_counts = [len(normalizer(page["raw_payload"])[1]) for page in pages]
            if scenario_name == "empty" and any(normalized_counts):
                raise RecordedSourceError("empty fixture contains source records")
            if scenario_name in {"success", "pagination", "partial"} and not all(
                count > 0 for count in normalized_counts
            ):
                raise RecordedSourceError("non-empty fixture page lacks source records")
            for page in pages:
                payload = page["raw_payload"]
                assert isinstance(payload, Mapping)
                raw_records = (
                    payload["announcements"] if slug == "cninfo" else payload["filings"]
                )
                for record in raw_records:
                    if slug == "cninfo":
                        published = _text(record["published_at"], "published_at")[:10]
                        if (
                            record["stock_code"] != expected_parameters["stock_code"]
                            or not expected_parameters["date_from"]
                            <= published
                            <= expected_parameters["date_to"]
                        ):
                            raise RecordedSourceError(
                                "CNINFO fixture record differs from its parent query"
                            )
                    else:
                        accession = _text(record["accession"], "accession")
                        filing_date = _text(record["filing_date"], "filing_date")
                        if (
                            accession[:10] != expected_parameters["issuer"]
                            or record["form"]
                            not in {
                                expected_parameters["form"],
                                expected_parameters["form"] + "/A",
                            }
                            or not expected_parameters["date_from"]
                            <= filing_date
                            <= expected_parameters["date_to"]
                        ):
                            raise RecordedSourceError(
                                "SEC fixture record differs from its parent query"
                            )
        elif scenario_name in {"schema_drift", "malformed"}:
            try:
                normalizer(pages[0]["raw_payload"])
            except RecordedSourceError:
                pass
            else:
                raise RecordedSourceError("normalization-error fixture is not malformed")
        scenarios.append(scenario)
    if names != _SCENARIOS:
        raise RecordedSourceError("recorded source fixture matrix is incomplete")
    wire["scenarios"] = scenarios
    if wire["content_hash"] != content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    ):
        raise RecordedSourceError("recorded fixture content_hash mismatch")
    return wire


def _normalize_cninfo(payload: Any) -> tuple[list[dict[str, Any]], list[str]]:
    page = _closed(payload, {"page", "announcements"}, "CNINFO page")
    _page(page["page"], "CNINFO page.page")
    if not isinstance(page["announcements"], list):
        raise RecordedSourceError("CNINFO announcements must be an array")
    records: list[dict[str, Any]] = []
    refs: list[str] = []
    for index, item in enumerate(page["announcements"]):
        record = _closed(
            item,
            {"announcement_id", "stock_code", "title", "published_at", "revision_of"},
            f"CNINFO announcements[{index}]",
        )
        announcement_id = _text(record["announcement_id"], "announcement_id")
        _text(record["stock_code"], "stock_code")
        _text(record["title"], "title")
        _text(record["published_at"], "published_at")
        revision = _nullable_text(record["revision_of"], "revision_of")
        record_ref = f"cninfo:announcement:{announcement_id}"
        normalized = {
            "record_ref": record_ref,
            "revision_of_ref": None if revision is None else f"cninfo:announcement:{revision}",
            "record_hash": content_hash(record),
        }
        records.append(normalized)
        refs.append(record_ref)
    if len(refs) != len(set(refs)):
        raise RecordedSourceError("CNINFO page contains duplicate announcement ids")
    return records, refs


def _normalize_sec(payload: Any) -> tuple[list[dict[str, Any]], list[str]]:
    page = _closed(payload, {"ordinal", "filings"}, "SEC page")
    _page(page["ordinal"], "SEC page.ordinal")
    if not isinstance(page["filings"], list):
        raise RecordedSourceError("SEC filings must be an array")
    records: list[dict[str, Any]] = []
    refs: list[str] = []
    for index, item in enumerate(page["filings"]):
        record = _closed(
            item,
            {"accession", "form", "filing_date", "primary_document", "revision_of"},
            f"SEC filings[{index}]",
        )
        accession = _text(record["accession"], "accession")
        if not _ACCESSION_RE.fullmatch(accession):
            raise RecordedSourceError("SEC accession format is invalid")
        _text(record["form"], "form")
        _text(record["filing_date"], "filing_date")
        primary = _text(record["primary_document"], "primary_document")
        if "/" in primary or "\\" in primary or primary.startswith("."):
            raise RecordedSourceError("SEC primary document must come from accession manifest")
        revision = _nullable_text(record["revision_of"], "revision_of")
        if revision is not None and not _ACCESSION_RE.fullmatch(revision):
            raise RecordedSourceError("SEC revision accession is invalid")
        record_ref = f"sec:filing:{accession}"
        normalized = {
            "record_ref": record_ref,
            "revision_of_ref": None if revision is None else f"sec:filing:{revision}",
            "record_hash": content_hash(record),
        }
        records.append(normalized)
        refs.append(record_ref)
    if len(refs) != len(set(refs)):
        raise RecordedSourceError("SEC page contains duplicate accessions")
    return records, refs


class RecordedSourceFixtureAdapter:
    """Replay one source/scenario; each page is a separate adapter call."""

    def __init__(
        self,
        fixture: Mapping[str, Any] | str | Path,
        *,
        scenario: str,
        advance_clock: Callable[[float], None] | None = None,
    ) -> None:
        if isinstance(fixture, (str, Path)):
            try:
                fixture = json.loads(Path(fixture).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RecordedSourceError("recorded source fixture is unreadable") from exc
        self.fixture = validate_recorded_source_fixture(fixture)
        self.scenario = _text(scenario, "scenario")
        matches = [
            item for item in self.fixture["scenarios"]
            if item["scenario"] == self.scenario
        ]
        if len(matches) != 1:
            raise RecordedSourceError("recorded scenario is not present")
        self._case = matches[0]
        slug = "cninfo" if self.fixture["source_ref"] == "source:cninfo" else "sec"
        self.scenario_ref = f"recorded-source-scenario:{slug}:{self.scenario}:0.1"
        self.scenario_hash = content_hash(self._case)
        self.behavior = self._case["behavior"]
        self._advance_clock = advance_clock

    @property
    def selected_case(self) -> dict[str, Any]:
        return json.loads(canonical_json(self._case))

    def __call__(self, request: Mapping[str, Any], raw_sink: Any) -> dict[str, Any]:
        pagination = _closed(
            request.get("pagination_authority"),
            {
                "mode", "cursor_field", "page_ordinal", "request_cursor",
                "parent_query_hash", "prior_adapter_request_hash",
                "prior_observation_hash", "prior_physical_attempt_ref",
                "prior_physical_attempt_hash", "recorded_scenario_ref",
                "recorded_scenario_hash", "recorded_behavior",
            },
            "pagination_authority",
        )
        ordinal = _page(request.get("physical_attempt_number"), "physical_attempt_number")
        if pagination["page_ordinal"] != ordinal:
            raise RecordedSourceError("pagination authority does not bind attempt ordinal")
        if pagination["parent_query_hash"] != self.fixture["parent_query_hash"]:
            raise RecordedSourceError("fixture does not bind the parent logical query")
        if (
            pagination["recorded_scenario_ref"] != self.scenario_ref
            or pagination["recorded_scenario_hash"] != self.scenario_hash
            or pagination["recorded_behavior"] != self.behavior
        ):
            raise RecordedSourceError("adapter request does not bind the selected scenario")
        expected_parameters = dict(self.fixture["parent_parameters"])
        if pagination["page_ordinal"] > 1:
            expected_parameters[pagination["cursor_field"]] = pagination["request_cursor"]
            if pagination["cursor_field"] == "page":
                expected_parameters["page"] = pagination["page_ordinal"]
        if request.get("parameters") != expected_parameters:
            raise RecordedSourceError("adapter parameters differ from the frozen fixture query")
        if self._case["behavior"] == "rate_limited":
            return self._observation(
                request,
                outcome="rate_limited",
                provider_request_id=f"fixture:{self.fixture['source_ref']}:rate-limited",
                provider_status=429,
                retry_after_ms=self._case["retry_after_ms"],
                records=[],
                refs=[],
                cursor=None,
                request_cursor=None,
                page_ordinal=ordinal,
                error=None,
            )
        pages = self._case["pages"]
        if ordinal > len(pages):
            raise RecordedSourceError("fixture has no page for this physical attempt")
        page = pages[ordinal - 1]
        if page["request_cursor"] != pagination["request_cursor"]:
            raise RecordedSourceError(
                "recorded page cursor does not match request pagination authority"
            )
        cursor_field = pagination["cursor_field"]
        if ordinal == 1:
            if cursor_field == "page" and request["parameters"].get(cursor_field) != 1:
                raise RecordedSourceError("first page request is not page one")
            if cursor_field == "cursor" and request["parameters"].get(cursor_field) not in {None, ""}:
                raise RecordedSourceError("first cursor request is not empty")
        elif cursor_field == "page":
            if request["parameters"].get(cursor_field) != int(page["request_cursor"]):
                raise RecordedSourceError("page request parameter is not authority-derived")
        elif request["parameters"].get(cursor_field) != page["request_cursor"]:
            raise RecordedSourceError("cursor request parameter is not authority-derived")
        raw = page["raw_payload"]
        raw_bytes = (
            canonical_json(raw).encode("utf-8") if isinstance(raw, Mapping)
            else raw.encode("utf-8")
        )
        raw_sink.write(raw_bytes)
        advance = float(self._case["advance_seconds"])
        if advance and self._advance_clock is not None:
            self._advance_clock(advance)
        normalizer = (
            _normalize_cninfo if self.fixture["source_ref"] == "source:cninfo"
            else _normalize_sec
        )
        records, refs = normalizer(raw)
        if self._case["behavior"] == "normalize_error":
            raise RecordedSourceError(self._case["error_code"] or "normalization failed")
        return self._observation(
            request,
            outcome="succeeded",
            provider_request_id=page["provider_request_id"],
            provider_status=page["provider_status"],
            retry_after_ms=None,
            records=records,
            refs=refs,
            cursor=page["next_cursor"],
            request_cursor=page["request_cursor"],
            page_ordinal=page["ordinal"],
            error=None,
        )

    @staticmethod
    def _observation(
        request: Mapping[str, Any],
        *,
        outcome: str,
        provider_request_id: str,
        provider_status: int | None,
        retry_after_ms: int | None,
        records: list[dict[str, Any]],
        refs: list[str],
        cursor: str | None,
        request_cursor: str | None,
        page_ordinal: int,
        error: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        base = {
            "protocol_version": "0.1",
            "request_hash": request["content_hash"],
            "outcome": outcome,
            "provider_request_id": provider_request_id,
            "provider_status_code": provider_status,
            "retry_after_ms": retry_after_ms,
            "structured_output": {
                "records": records,
                "source_record_refs": refs,
                "request_cursor": request_cursor,
                "next_cursor": cursor,
                "page_ordinal": page_ordinal,
                "provider_status": provider_status,
            },
            "source_record_refs": refs,
            "cursor": cursor,
            "provider_usage": {"calls": 1},
            "error": None if error is None else dict(error),
        }
        return validate_adapter_transport_observation(_with_hash(base))


def _scenario(
    name: str,
    behavior: str,
    pages: list[dict[str, Any]],
    *,
    advance_seconds: float = 0,
    retry_after_ms: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "scenario": name, "behavior": behavior,
        "advance_seconds": advance_seconds, "retry_after_ms": retry_after_ms,
        "error_code": error_code, "pages": pages,
    }


def _recorded_page(
    ordinal: int,
    source: str,
    payload: Mapping[str, Any] | str,
    *,
    request_cursor: str | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "provider_request_id": f"fixture:{source}:request:{ordinal}",
        "provider_status": 200,
        "request_cursor": request_cursor,
        "next_cursor": next_cursor,
        "raw_payload": payload,
    }


def build_recorded_source_fixtures() -> dict[str, dict[str, Any]]:
    created_at = RECORDED_FIXTURE_CREATED_AT
    cninfo_record_1 = {
        "announcement_id": "cn-0001", "stock_code": "600309",
        "title": "Synthetic annual report", "published_at": "2026-04-01T00:00:00+00:00",
        "revision_of": None,
    }
    cninfo_record_2 = {
        "announcement_id": "cn-0002", "stock_code": "600309",
        "title": "Synthetic corrected annual report", "published_at": "2026-04-02T00:00:00+00:00",
        "revision_of": "cn-0001",
    }
    cninfo_page_1 = {"page": 1, "announcements": [cninfo_record_1]}
    cninfo_page_2 = {"page": 2, "announcements": [cninfo_record_2]}
    cninfo = _with_hash(
        {
            "schema_version": "0.1", "id": "recorded-source-fixture:cninfo:0.1",
            "created_at": created_at, "source_ref": "source:cninfo",
            "operation": "list_announcements",
            "parent_parameters": {
                "stock_code": "600309", "date_from": "2026-01-01",
                "date_to": "2026-06-30", "page": 1, "page_size": 50,
            },
            "parent_query_hash": content_hash(
                {
                    "operation": "list_announcements",
                    "parameters": {
                        "stock_code": "600309", "date_from": "2026-01-01",
                        "date_to": "2026-06-30", "page": 1, "page_size": 50,
                    },
                }
            ),
            "scenarios": [
                _scenario("success", "return", [_recorded_page(1, "cninfo:success", cninfo_page_1)]),
                _scenario("empty", "return", [_recorded_page(1, "cninfo:empty", {"page": 1, "announcements": []})]),
                _scenario("pagination", "return", [
                    _recorded_page(1, "cninfo:pagination", cninfo_page_1, next_cursor="2"),
                    _recorded_page(2, "cninfo:pagination", cninfo_page_2, request_cursor="2"),
                ]),
                _scenario("partial", "return", [
                    _recorded_page(1, "cninfo:partial", cninfo_page_1, next_cursor="2")
                ]),
                _scenario("schema_drift", "normalize_error", [
                    _recorded_page(1, "cninfo:schema-drift", {**cninfo_page_1, "unexpected": True})
                ], error_code="schema_drift"),
                _scenario("rate_limited", "rate_limited", [], retry_after_ms=1000, error_code="rate_limited"),
                _scenario("timeout", "timeout", [
                    _recorded_page(1, "cninfo:timeout", cninfo_page_1)
                ], advance_seconds=10, error_code="timeout"),
                _scenario("malformed", "normalize_error", [
                    _recorded_page(1, "cninfo:malformed", "{not-json")
                ], error_code="malformed"),
            ],
        }
    )
    sec_record_1 = {
        "accession": "0000000001-26-000001", "form": "10-Q",
        "filing_date": "2026-05-01", "primary_document": "q1.htm",
        "revision_of": None,
    }
    sec_record_2 = {
        "accession": "0000000001-26-000002", "form": "10-Q/A",
        "filing_date": "2026-05-02", "primary_document": "q1a.htm",
        "revision_of": "0000000001-26-000001",
    }
    sec_page_1 = {"ordinal": 1, "filings": [sec_record_1]}
    sec_page_2 = {"ordinal": 2, "filings": [sec_record_2]}
    sec = _with_hash(
        {
            "schema_version": "0.1", "id": "recorded-source-fixture:sec:0.1",
            "created_at": created_at, "source_ref": "source:sec-edgar",
            "operation": "list_filings",
            "parent_parameters": {
                "issuer": "0000000001", "form": "10-Q",
                "date_from": "2026-01-01", "date_to": "2026-06-30", "limit": 50,
            },
            "parent_query_hash": content_hash(
                {
                    "operation": "list_filings",
                    "parameters": {
                        "issuer": "0000000001", "form": "10-Q",
                        "date_from": "2026-01-01", "date_to": "2026-06-30", "limit": 50,
                    },
                }
            ),
            "scenarios": [
                _scenario("success", "return", [_recorded_page(1, "sec:success", sec_page_1)]),
                _scenario("empty", "return", [_recorded_page(1, "sec:empty", {"ordinal": 1, "filings": []})]),
                _scenario("pagination", "return", [
                    _recorded_page(1, "sec:pagination", sec_page_1, next_cursor="cursor-2"),
                    _recorded_page(2, "sec:pagination", sec_page_2, request_cursor="cursor-2"),
                ]),
                _scenario("partial", "return", [
                    _recorded_page(1, "sec:partial", sec_page_1, next_cursor="cursor-2")
                ]),
                _scenario("schema_drift", "normalize_error", [
                    _recorded_page(1, "sec:schema-drift", {**sec_page_1, "unexpected": True})
                ], error_code="schema_drift"),
                _scenario("rate_limited", "rate_limited", [], retry_after_ms=1000, error_code="rate_limited"),
                _scenario("timeout", "timeout", [
                    _recorded_page(1, "sec:timeout", sec_page_1)
                ], advance_seconds=10, error_code="timeout"),
                _scenario("malformed", "normalize_error", [
                    _recorded_page(1, "sec:malformed", "{not-json")
                ], error_code="malformed"),
            ],
        }
    )
    return {
        "cninfo": validate_recorded_source_fixture(cninfo),
        "sec": validate_recorded_source_fixture(sec),
    }


def load_recorded_source_fixture(source: str) -> dict[str, Any]:
    if source not in {"cninfo", "sec"}:
        raise RecordedSourceError("unknown recorded reference source")
    fixture = validate_recorded_source_fixture(
        json.loads((REFERENCE_FIXTURE_DIR / f"{source}.json").read_text(encoding="utf-8"))
    )
    if fixture != build_recorded_source_fixtures()[source]:
        raise RecordedSourceError(
            "packaged recorded fixture differs from the deterministic frozen build"
        )
    return fixture


__all__ = [
    "RecordedSourceError", "RecordedSourceFixtureAdapter",
    "build_recorded_source_fixtures", "load_recorded_source_fixture",
    "validate_recorded_source_fixture",
]
