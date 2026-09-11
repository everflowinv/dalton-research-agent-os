"""P9d-4c: a fetched public-web page as a verified, read-only extraction source."""

from __future__ import annotations

import hashlib
import gzip
import io
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from dalton_core.public_web_extraction_source import (
    MAX_SOURCE_CHARS,
    PublicWebSourceConflict,
    PublicWebSourceError,
    normalized_media_type,
    render_public_web_text,
    verified_public_web_source,
)
from dalton_core.store import content_hash
from tests.test_public_web_fetch_lane import BODY, BODY_HASH, FetchHarness, URL_A, URL_B, transport_for


PAGE = (
    b"<!doctype html><html><head><title>Leadership update</title>"
    b"<style>.a{color:red}</style><script>steal()</script></head>"
    b"<body><h1>Accenture names a new CFO</h1>"
    b"<p>The company said the change is effective&nbsp;immediately.</p>"
    b"<ul><li>First point</li><li>Second point</li></ul>"
    b"<div>Line one<br>Line two</div>"
    b"<noscript>hidden fallback</noscript></body></html>"
)


def remanifest(manifest: dict, **overrides) -> dict:
    """Rebuild a manifest with overrides, keeping its derived id/hash valid."""

    base = {key: value for key, value in manifest.items() if key != "content_hash"}
    base.update(overrides)
    base["id"] = "public-web-fetch-manifest:" + content_hash(
        {"document_ref": base["document_ref"], "source_envelope_hash": base["source_envelope_hash"]}
    )
    return {**base, "content_hash": content_hash(base)}


class RenderTests(unittest.TestCase):
    def test_gzip_text_is_decoded_with_integrity_and_configured_size_bounds(self):
        raw = gzip.compress(PAGE, mtime=0)
        rendered = render_public_web_text(raw, raw_media_type="text/html")
        plain = render_public_web_text(PAGE, raw_media_type="text/html")
        self.assertEqual(rendered["text"], plain["text"])
        self.assertEqual(rendered["renderer"], "gzip:0.1|" + plain["renderer"])
        with self.assertRaisesRegex(PublicWebSourceError, "decompressed byte limit"):
            render_public_web_text(raw, raw_media_type="text/html", max_decompressed_bytes=len(PAGE)-1)
        self.assertEqual(render_public_web_text(
            raw, raw_media_type="text/html", max_decompressed_bytes=len(PAGE)), rendered)
        corrupt = bytearray(raw)
        corrupt[-8] ^= 1  # Fail CRC even though every text byte is available.
        for broken in (bytes(corrupt), raw[:-4], b"\x1f\x8bgarbage"):
            with self.subTest(body=broken[:8]), self.assertRaisesRegex(PublicWebSourceError, "gzip content"):
                render_public_web_text(broken, raw_media_type="text/html")

    def test_visible_blocks_only_and_deterministic(self) -> None:
        first = render_public_web_text(PAGE, raw_media_type="text/html; charset=utf-8")
        second = render_public_web_text(PAGE, raw_media_type="text/html")
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(
            first["text"],
            "Leadership update\n\nAccenture names a new CFO\n\n"
            "The company said the change is effective immediately.\n\n"
            "First point\n\nSecond point\n\nLine one\n\nLine two",
        )
        self.assertEqual((first["renderer"], first["media_type"], first["truncated"]),
                         ("html-visible-blocks:0.1", "text/html", False))
        self.assertEqual(first["rendered_chars"], len(first["text"]))
        # Script, style and noscript payloads never reach the human.
        for banned in ("steal()", "color:red", "hidden fallback"):
            self.assertNotIn(banned, first["text"])

    def test_zero_width_and_unicode_whitespace_cannot_hide_text(self) -> None:
        raw = "<p>net\u200bincome rose\u00a0by 4%</p>".encode("utf-8")
        rendered = render_public_web_text(raw, raw_media_type="text/html")
        self.assertEqual(rendered["text"], "netincome rose by 4%")

    def test_plain_text_and_media_type_parsing(self) -> None:
        rendered = render_public_web_text(b"para one\n\n  para   two\n", raw_media_type="text/plain")
        self.assertEqual((rendered["text"], rendered["renderer"]), ("para one\n\npara two", "text-plain:0.1"))
        self.assertEqual(normalized_media_type("Text/HTML; Charset=UTF-8"), ("text/html", "utf-8"))
        with self.assertRaises(PublicWebSourceError):
            normalized_media_type("")

    def test_unrenderable_bytes_are_refused_not_guessed(self) -> None:
        for raw, media_type, reason in (
            (b"\x89PNG\r\n", "image/png", "media type"),
            (b"\xff\xfe\x00", "text/html", "UTF-8"),
            (b"caf\xe9", "text/html; charset=iso-8859-1", "charset"),
        ):
            with self.subTest(media_type=media_type), self.assertRaises(PublicWebSourceError) as ctx:
                render_public_web_text(raw, raw_media_type=media_type)
            self.assertIn(reason, str(ctx.exception))

    def test_oversized_page_is_truncated_and_declared(self) -> None:
        body = ("<p>" + ("word " * 200) + "</p>") * 700
        rendered = render_public_web_text(body.encode("utf-8"), raw_media_type="text/html")
        self.assertTrue(rendered["truncated"])
        self.assertEqual(rendered["rendered_chars"], MAX_SOURCE_CHARS)
        self.assertEqual(len(rendered["text"]), MAX_SOURCE_CHARS)


class VerifiedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = FetchHarness(Path(self.temp.name), transport=transport_for(PAGE))
        self.addCleanup(self.h.close)
        self.receipt = self.h.discover()
        fetched = self.h.fetch.fetch(self.h.fetch.build_request(self.h.authority(self.receipt, URL_A)))
        self.manifest = self.h.fetch.manifest(fetched)

    def verify(self, manifest=None, spool=None):
        return verified_public_web_source(
            self.h.core, spool or self.h.spool, manifest or self.manifest, self.h.fetch.receipts,
        )

    def test_verified_page_renders_its_exact_bytes(self) -> None:
        manifest, rendering = self.verify()
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(rendering["text"], render_public_web_text(PAGE, raw_media_type="text/html")["text"])
        self.assertEqual(manifest["body_sha256"], hashlib.sha256(PAGE).hexdigest())
        self.assertIn("Accenture names a new CFO", rendering["text"])

    def test_gzip_source_retains_compressed_receipt_and_spool_authority(self):
        raw = gzip.compress(PAGE, mtime=0)
        root = Path(self.temp.name) / "gzip"
        root.mkdir()
        harness = FetchHarness(root, transport=transport_for(raw))
        self.addCleanup(harness.close)
        receipt = harness.discover()
        fetched = harness.fetch.fetch(harness.fetch.build_request(harness.authority(receipt, URL_A)))
        manifest = harness.fetch.manifest(fetched)
        verified, rendered = verified_public_web_source(
            harness.core, harness.spool, manifest, harness.fetch.receipts)
        self.assertEqual(verified, manifest)
        self.assertEqual(verified["body_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(harness.spool.read_object(verified["raw_response_hash"]), raw)
        self.assertIn("Accenture names a new CFO", rendered["text"])
        self.assertEqual(rendered["renderer"], "gzip:0.1|html-visible-blocks:0.1")

    def test_receipt_and_lineage_drift_fail_closed(self) -> None:
        cases = {
            "invocation hash": {"connector_invocation_hash": "0" * 64},
            "profile hash": {"connector_profile_hash": "0" * 64},
            "host": {"host": "attacker.example"},
            "profile ref": {"connector_profile_ref": "connector-profile:web-fetch:other:v1"},
            "artifact ref": {"raw_artifact_version_ref": "artifact-version:other"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label), self.assertRaises((PublicWebSourceConflict, Exception)) as ctx:
                self.verify(remanifest(self.manifest, **overrides))
            self.assertNotIsInstance(ctx.exception, AssertionError)

    def test_manifest_cannot_borrow_another_pages_envelope(self) -> None:
        other = self.h.fetch.fetch(self.h.fetch.build_request(self.h.authority(self.receipt, URL_B)))
        other_manifest = self.h.fetch.manifest(other)
        self.assertNotEqual(other_manifest["source_envelope_ref"], self.manifest["source_envelope_ref"])
        # Page A's envelope with page B's fetched record: the envelope names
        # exactly one record ref, so the swap is refused.
        forged = remanifest(
            self.manifest,
            document_ref=other_manifest["document_ref"],
            source_record_ref=other_manifest["source_record_ref"],
            body_sha256=other_manifest["body_sha256"],
            raw_response_hash=other_manifest["raw_response_hash"],
        )
        with self.assertRaises(PublicWebSourceConflict):
            self.verify(forged)

    def test_spool_bytes_that_do_not_hash_to_the_record_are_refused(self) -> None:
        class LyingSpool:
            def read_object(self, _content_hash):
                return b"<html><body><p>substituted</p></body></html>"

        with self.assertRaises(PublicWebSourceConflict) as ctx:
            self.verify(spool=LyingSpool())
        self.assertIn("differ from the recorded body hash", str(ctx.exception))

    def test_unfetched_and_failed_pages_have_no_verified_source(self) -> None:
        root = Path(self.temp.name) / "failed"
        root.mkdir()
        failed_harness = FetchHarness(root, transport=transport_for(b"err", status=500))
        self.addCleanup(failed_harness.close)
        receipt = failed_harness.discover()
        failed = failed_harness.fetch.fetch(failed_harness.fetch.build_request(failed_harness.authority(receipt, URL_A)))
        self.assertNotEqual(failed["outcome"], "succeeded")
        from dalton_core.public_web_core_fetch import PublicWebCoreFetchError

        with self.assertRaises(PublicWebCoreFetchError):
            failed_harness.fetch.manifest(failed)


if __name__ == "__main__":
    unittest.main()


def minimal_pdf(lines: list[str], *, encrypted_marker: bool = False) -> bytes:
    """Build a tiny valid PDF with extractable text, without a PDF library."""

    body = " ".join(f"BT /F1 12 Tf 72 {720 - index * 20} Td ({line}) Tj ET" for index, line in enumerate(lines))
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n%s\nendstream" % (len(body), body.encode("ascii")),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % index + obj + b"endobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    trailer = b"<</Size %d/Root 1 0 R" % (len(objects) + 1)
    if encrypted_marker:
        trailer += b"/Encrypt 9 0 R"
    trailer += b">>"
    out += b"trailer" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


class PdfRenderTests(unittest.TestCase):
    def test_empty_password_pdf_is_readable_but_real_password_stays_required(self):
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(minimal_pdf(["Annual revenue increased by 12 percent."])))
        for password, algorithm in (("", "RC4-128"), ("required-secret", "RC4-128"),
                                    ("", "AES-256"), ("required-secret", "AES-256")):
            writer = pypdf.PdfWriter()
            writer.add_page(reader.pages[0])
            writer.encrypt(password, owner_password="owner-edit-password", algorithm=algorithm)
            out = io.BytesIO()
            writer.write(out)
            raw = out.getvalue()
            if password:
                with self.assertRaisesRegex(PublicWebSourceError, "requires a password"):
                    render_public_web_text(raw, raw_media_type="application/pdf")
            else:
                result = render_public_web_text(raw, raw_media_type="application/pdf")
                self.assertIn("Annual revenue increased by 12 percent.", result["text"])
                self.assertEqual(result["renderer"], f"pdf-pypdf-{pypdf.__version__}:0.1:empty-password:0.1")
                self.assertEqual(result, render_public_web_text(raw, raw_media_type="application/pdf"))

    def test_a_pdf_renders_deterministically_and_names_its_extractor(self) -> None:
        raw = minimal_pdf(["New bookings of $21.1 billion", "Revenues of $16.5 billion"])
        first = render_public_web_text(raw, raw_media_type="application/pdf")
        second = render_public_web_text(raw, raw_media_type=" Application/PDF ")
        self.assertEqual(first["text"], second["text"])
        self.assertIn("New bookings of $21.1 billion", first["text"])
        self.assertIn("Revenues of $16.5 billion", first["text"])
        self.assertEqual(first["media_type"], "application/pdf")
        self.assertFalse(first["truncated"])
        self.assertEqual(first["rendered_chars"], len(first["text"]))
        # The extractor version is part of the renderer identity, so an
        # upgrade invalidates a context instead of moving a citation.
        import pypdf

        self.assertEqual(first["renderer"], f"pdf-pypdf-{pypdf.__version__}:0.1")

    def test_malformed_encrypted_and_empty_pdfs_are_refused(self) -> None:
        for raw, reason in (
            (b"%PDF-1.7 not really a pdf", "could not be read"),
            (minimal_pdf([], ), "no extractable text"),
        ):
            with self.subTest(reason=reason), self.assertRaises(PublicWebSourceError) as ctx:
                render_public_web_text(raw, raw_media_type="application/pdf")
            self.assertIn(reason, str(ctx.exception))

    def test_a_pdf_is_refused_when_the_extractor_is_not_installed(self) -> None:
        raw = minimal_pdf(["Revenues of $16.5 billion"])
        import builtins

        real_import = builtins.__import__

        def missing(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "__import__", missing):
            with self.assertRaises(PublicWebSourceError) as ctx:
                render_public_web_text(raw, raw_media_type="application/pdf")
        self.assertIn("PDF extractor is not installed", str(ctx.exception))
