"""Stage one immutable acquisition bundle for Cockpit transcript review."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .raw_spool import RawSpool
from .research_review_control import (
    ResearchReviewControlError,
    ResearchReviewControlPlane,
)
from .store import canonical_json


class TranscriptReviewInboxError(RuntimeError):
    pass


def _directory(value: str | Path, name: str, *, must_exist: bool) -> Path:
    path = Path(value).expanduser().resolve()
    if must_exist and not path.is_dir():
        raise TranscriptReviewInboxError(f"{name} is unavailable")
    if path == Path(path.anchor):
        raise TranscriptReviewInboxError(f"{name} cannot be a filesystem root")
    return path


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage_transcript_review_bundle(
    acquisition_directory: str | Path,
    review_directory: str | Path,
    transcript_spool_directory: str | Path,
) -> dict[str, Any]:
    """Copy one verified raw object and packet into owner-only runtime state.

    This operation creates no Evidence, Claim, Thesis, correction set, or
    citation.  The later Cockpit action remains the only human admission.
    """

    acquisition = _directory(
        acquisition_directory, "acquisition_directory", must_exist=True
    )
    review_root = _directory(
        review_directory, "review_directory", must_exist=False
    )
    spool_root = _directory(
        transcript_spool_directory,
        "transcript_spool_directory",
        must_exist=False,
    )
    packet_path = acquisition / "review-packet.json"
    manifest_path = acquisition / "source-manifest.json"
    try:
        packet, manifest = ResearchReviewControlPlane._validate_packet(
            ResearchReviewControlPlane._secure_json(
                packet_path, "transcript review packet"
            ),
            ResearchReviewControlPlane._secure_json(
                manifest_path, "source manifest"
            ),
        )
        source_hash = manifest["assembled_object"]["content_hash"]
        source_spool = RawSpool(acquisition, max_total_bytes=1_000_000_000)
        source_bytes = source_spool.read_object(source_hash)
        target_spool = RawSpool(spool_root, max_total_bytes=1_000_000_000)
        if target_spool.object_exists(source_hash):
            if target_spool.read_object(source_hash) != source_bytes:
                raise TranscriptReviewInboxError(
                    "existing transcript spool object drifted"
                )
            target_hash = source_hash
        else:
            sink = target_spool.open_sink(
                f"raw-sink:{source_hash}",
                max_response_bytes=len(source_bytes),
            )
            try:
                sink.write(source_bytes)
                target_hash = sink.finalize().content_hash
            except BaseException:
                sink.abort()
                raise
    except Exception as exc:
        if isinstance(exc, TranscriptReviewInboxError):
            raise
        raise TranscriptReviewInboxError(
            "transcript review bundle failed exact validation"
        ) from exc
    if target_hash != source_hash:
        raise TranscriptReviewInboxError("staged transcript digest drifted")
    case = review_root / packet["content_hash"][:24]
    if case.exists():
        try:
            existing_packet, existing_manifest = (
                ResearchReviewControlPlane._validate_packet(
                    ResearchReviewControlPlane._secure_json(
                        case / "review-packet.json", "existing review packet"
                    ),
                    ResearchReviewControlPlane._secure_json(
                        case / "source-manifest.json", "existing source manifest"
                    ),
                )
            )
        except ResearchReviewControlError as exc:
            raise TranscriptReviewInboxError(
                "existing review bundle is invalid"
            ) from exc
        if existing_packet != packet or existing_manifest != manifest:
            raise TranscriptReviewInboxError(
                "review inbox identity conflicts with existing content"
            )
        status = "duplicate"
    else:
        case.mkdir(mode=0o700, parents=True)
        os.chmod(case, 0o700)
        _atomic_json(case / "source-manifest.json", manifest)
        _atomic_json(case / "review-packet.json", packet)
        status = "fresh"
    return {
        "status": status,
        "packet_ref": packet["id"],
        "packet_hash": packet["content_hash"],
        "source_manifest_ref": manifest["id"],
        "source_manifest_hash": manifest["content_hash"],
        "source_content_hash": source_hash,
        "review_case_path": str(case),
        "production_activated": False,
        "formal_authority_writes": 0,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage one acquisition bundle for Dalton Cockpit review"
    )
    parser.add_argument("--acquisition-dir", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--transcript-spool-dir", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = stage_transcript_review_bundle(
        args.acquisition_dir, args.review_dir, args.transcript_spool_dir
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "TranscriptReviewInboxError", "stage_transcript_review_bundle",
]
