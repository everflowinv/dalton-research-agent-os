# DocumentResearch source-authority audit

Date: 2026-09-11  
Audited source commit: `073879a60d40f7e5e6557b72b56327cb0fcb5d11`  
Scope: source and schema review plus an inert, read-only contract and a later
read-only authority audit of acquired rows; no live writes, model calls, Claim
writes, promotion, planner execution, or deployment.

## Finding

Dalton retains more source material than its product reader exposes, but it
does not yet have one authority that can answer a new question by reopening
every acquired document. Full text is split among source-specific acquisition
manifests and content-addressed spool objects. The generic `DocumentIndex` is
a disposable recall projection over `ArtifactVersion`; a connector's artifact
is often its JSON wire response rather than the separately assembled document.
Its search result returns immutable metadata, not source text or excerpts.

The Cockpit Research Library and `CompanyResearchView` expose deliverables,
Claim versions, and evidence relationships. They do not enumerate acquisition
manifests or rematerialize source documents. A Claim can therefore help find a
topic, but it cannot recover omitted wording from the source. The missing link
is an exact document registration and read authority between acquisition and
question-specific context assembly.

## Coverage matrix

“Original” means bytes obtained from the source transport or local source
container. “Readable text” means the text Dalton can currently re-verify and
quote. These are separate columns because a rendered PDF is not its original
PDF and a connector response is not automatically the document it describes.

| Source / document family | Original bytes retained | Normalized readable text | Exact authority and location | Current retrieval surface | Boundary / gap |
| --- | --- | --- | --- | --- | --- |
| AlphaEngine sell-side reports, call transcripts and other `get_document` records | Yes: every raw page response is an `ArtifactVersion` in `RawSpool` | Yes: complete contiguous page text plus an assembled UTF-8 object | `alphaengine-document-acquisition` manifest binds document ref, declared full-content hash/characters, page offsets, nine Core receipt refs/hashes, and assembled object | Fixed-window extraction and the adapter in this branch; generic `DocumentIndex` may index raw connector JSON if pointed at the response artifact | Only manifests with `status=complete`, terminal pagination, and a full assembled hash are text authority. Search-library rows that were not acquired remain metadata. |
| SEC filings fetched through public web, including annual reports and other filing forms | Yes: raw HTTP response body is a content-addressed `ArtifactVersion` | Yes when the versioned renderer accepts the media type and does not truncate | `public-web-fetch-manifest` → invocation/profile/call/attempt/usage/cost/settlement/SourceEnvelope/ArtifactVersion → raw body; rendering carries renderer, media type, text hash, offsets and truncation | Annual reports have `RegisteredAnnualReportRegistry.search`; this branch adds source-neutral search/read for any complete acquired filing | `list_filings` rows alone are filing metadata. A filing is readable only after `fetch_get`. The annual registry retains its additional form/issuer rules; the generic adapter derives SEC identity only from the acquired Core row. |
| Ordinary fetched web pages and fetched earnings-call transcript pages | Yes: raw HTTP body | Yes when deterministic HTML/text/PDF/gzip rendering succeeds without truncation | Same public-web manifest and Core chain; earnings-call projection additionally proves issuer, fiscal period, document markers and raw-body hash | Fixed-window extraction plus the fetched-document search/read adapter in this branch | Search result URLs/snippets are not page bodies. Unsupported charset/media, malformed PDF, or truncated rendering remains explicitly unavailable. |
| Sales notes | The normalized note body is retained; a separate original digest/mail-container object is not bound by the feed manifest | Yes: complete assembled UTF-8 note body | Completed owner-only ticket + summary + `feed-document-acquisition` manifest + connector invocation/profile + spool object hash/size/text hash | Source-specific extraction and, in this slice, `DocumentResearch` search/read | Connector raw artifact is the child JSON response, not the original digest. Registration therefore reports `raw_source.status=not_bound`. |
| Company wiki | The normalized whole file text is retained; no separate original-file artifact is bound | Yes: assembled UTF-8 text including frontmatter | Same completed feed ticket/manifest/invocation/profile/spool chain | Source-specific extraction and, in this slice, `DocumentResearch` search/read | The current reader uses UTF-8 replacement on invalid bytes. The normalized text is authoritative for reading; it is not proof that every original byte is preserved. |
| Prior research (`md`, `txt`, `pdf`, `docx`, `xlsx`) | Yes in the GET child: `source_artifact` and `artifact-manifest.json` archive the source container | A rendered assembled UTF-8 projection is retained | Feed manifest binds normalized text. Summary separately names source artifact and artifact-manifest hash | Fixed-window extraction | **Authority gap:** the feed acquisition manifest does not bind the raw source artifact/bundle. Text may also be truncated at 600k characters and workbook sheets have bounds. Do not register as complete until the raw bundle is joined into the acquisition authority and loss/truncation is explicit. |
| Guidepoint | Yes: raw `search_library` response | Yes, but the authorized document is one complete Q&A excerpt | Guidepoint excerpt manifest replays all Core receipts, re-derives the excerpt ordinal/text from the raw response, verifies the excerpt object, and binds quote policy | Source-specific extraction | Upstream has no `get_transcript`. It is false to describe an excerpt as a full interview. Verbatim reproduction remains limited to the bound policy (currently at most 20 words). |
| HKEX next-day disclosure / interests operations | Operation capture is spooled; daily buyback cache also retains the source workbook | Parsed, typed rows are retained; source-document URL/hash/size metadata is recorded | HKEX summary/capture/invocation identities and operation-specific parser hash | Event generation and index output | Not a generic full-document acquisition manifest. Monthly returns and announcements are intentionally indexes; their PDFs/announcements are not read. A later adapter must distinguish the workbook/capture from linked filing documents. |
| SEC/HKEX/web/AlphaEngine discovery and index rows without a successful GET | No document body | No | Discovery/SourceEnvelope proves the index response and listed document refs | Discovery and selection only | Metadata-only. Must be rejected by a document reader. |
| ROIC transcript | No connected working acquisition lane | No | Disabled/proposed connector records only | None | Not connected; no readable document may be claimed. |
| Claim, Evidence, dossier, debate map, memo, thesis views | Derived records are retained | Derived statements only | Claim/Evidence/deliverable hashes and relations | Cockpit Research Library and `CompanyResearchView` | These are consumers and recall hints, never substitutes for source text. |

## Where the bytes and identities live

The durable raw object store is `RawSpool`, under the configured
`connector-spool/objects/<sha-prefix>/<sha>`. Core stores immutable connector
and artifact authority in `connector_source_envelopes`, connector invocation /
attempt / usage / cost / settlement tables, and
`observability_artifact_versions_v2`. Coverage mission tables record discovery
and review workflow (`coverage_mission_discovered_documents` and
`coverage_mission_document_reviews`); those rows point to acquisition tickets
but do not themselves contain source text.

Source children put `ticket.json`, `summary.json`, and `manifest.json` in their
bounded owner-only ticket directory. AlphaEngine, public web, Guidepoint, and
feed launchers all expose `read_completed_manifest` and
`locate_completed_manifest`. The verified readers then reopen Core receipts
and content-addressed objects. `document_read_completion_proofs` record that a
whole document-reading workflow completed, but targeted retrieval does not
need prior whole-document review; acquisition completeness is the relevant
precondition.

`document_index_documents.extracted_text` and the FTS5 table are disposable.
The snapshot contract intentionally omits the text body, and `search()`
returns record metadata. It is suitable as a recall accelerator only after an
exact registration exists; every hit must be rematerialized through its source
adapter before text is returned.

## DocumentResearch 0.2 registration contract

The new `document_research.py` slice establishes the shared shape without
changing a planner or execution graph:

1. A source adapter locates a completed acquisition through a trusted launcher;
   callers cannot register a filesystem path, arbitrary bytes, a metadata row,
   or a Claim.
2. The adapter runs the existing source verifier and rereads the connector
   invocation and profile. Source identity/version plus access, retention, and
   terms policy come from that profile rather than caller metadata.
3. A deterministic registration separately describes `raw_source` and
   `normalized_text`. For sales notes and wiki, the former explicitly says
   `not_bound`; the latter binds object hash, byte count, text hash, character
   count, completeness, and truncation. The registration also separates the
   logical `document_ref` selected by research from `content_document_ref`,
   the exact body-version ref stored in the acquisition manifest.
4. Search binds the full research question, exact query terms, registration,
   configured policy hash, and per-request bounds. Version 0.1 uses literal,
   case-insensitive lexical matching (spaces may match source whitespace).
   It does not perform semantic or cross-language retrieval. A planner may
   later generate multilingual/synonym terms, but that strategy must be part
   of a new request identity rather than hidden in the reader.
5. Every match carries exact match and context character offsets, excerpt
   SHA-256, registration identity, and a stable source location. `read` returns
   an exact bounded character range; setting the range to the registered text
   length reads the full normalized text when policy permits.
6. Search/read replay re-runs the source verifier and rejects source, profile,
   spool, policy, registration, or proof drift. No result becomes Evidence or
   a Claim in this module.

The research policy is configured once with allowed purposes, allowed source
access-policy refs, and positive resource bounds. Ordinary authorized research
does not require a new human signature for each question. A purpose or source
policy outside that configured grant is refused.

## Parallel implementation slices

1. **Landed in this branch:** sales-note, company-wiki, complete AlphaEngine,
   and non-truncated fetched-document adapters plus generic versioned
   registration/search/read/replay/availability contracts. The
   `register_acquired_document(record_id=...)` path first reads the exact
   `coverage_mission_discovered_documents` row and pins its mission, company,
   source, logical document ref, and acquisition ticket before dispatching to
   the matching sales-note, wiki, AlphaEngine, or fetched-document adapter.
   The adapter must independently return that same source, document, and
   ticket. Thus an acquired SEC 8-K stays `source:sec-edgar` because Core says
   so; the public-web profile proves only how the body was fetched and cannot
   relabel an arbitrary URL as SEC. Ordinary web registrations retain the
   source from their actual discovery envelope. The planned web-search path is
   an explicit exception already declared by `SOURCE_PLAN_ALIASES`: its Core
   discovery/acquired rows retain logical `source:web-search`, while the actual
   connector SourceEnvelope is `source:public-web`. DocumentResearch accepts
   that translation only through the Core-acquired path, when the exact source
   discovery row has the same company and logical source, belongs to the exact
   current mission or one of its immutable ancestors, and binds the exact
   SourceEnvelope ref/hash returned by the physical adapter. This ancestry
   check matters because `carry_forward_superseded_documents` deliberately
   keeps the original discovery ref while re-registering a document under the
   new mission version. The alias
   binding has its own 0.2 authority identity including the discovery ref,
   resolved source and envelope ref/hash; changing any one makes replay fail.
   Direct caller-selected source aliases remain insufficient. A read-only
   registry audit found six readable active `source:web-search` documents with
   this exact relationship. Two additional bodies remain unavailable because
   their deterministic reader rejects invalid UTF-8, one completed-ticket
   directory is incomplete, and one row has no completed acquisition ticket.

   The production factory consumes injected, already-open
   Core/spool/receipt/launcher authorities and verifies every launcher belongs
   to one state directory; it does not open or migrate authority on a read
   path. Dedicated AlphaEngine, public-web, and feed manifest readers reopen
   existing owner-only ticket files without exposing start/status operations,
   creating directories, or changing permissions. This lets a planner compose
   the registry entirely from read-only spool, receipt, Core, and manifest
   ports.
2. **Prior research authority closure:** version the feed manifest so it binds
   `source_artifact` and `artifact-manifest.json`, records renderer/loss and
   truncation explicitly, and proves the normalized projection came from that
   original object. Only then add the adapter.
3. **Guidepoint adapter:** preserve excerpt document identity and quote policy;
   expose no transcript-level completeness claim.
4. **Recall and context:** project registrations into `DocumentIndex`, then
   build question-specific ContextPack materialization from replayed search/read
   proofs. Claims may nominate documents/terms, but source text supplies the
   context.
5. **Planning:** add a general qualitative DocumentResearch operation with
   query-strategy identity and source selection after the read authority is
   stable. Keep staging/promotion separate and require downstream independent
   verification before any candidate Claim is admitted.

## Source evidence reviewed

- `src/dalton_core/raw_spool.py`
- `src/dalton_core/document_index.py` and `document_index_schema.sql`
- `src/dalton_core/alphaengine_document_acquisition.py`
- `src/dalton_core/document_extraction.py`
- `src/dalton_core/public_web_core_fetch.py` and
  `public_web_extraction_source.py`
- `src/dalton_core/feed_acquisition.py`, `feed_launcher.py`,
  `sales_notes_core.py`, `company_wiki_core.py`, `prior_research_core.py`, and
  `prior_research_cli.py`
- `src/dalton_core/guidepoint_acquisition.py`
- `src/dalton_core/hkex_filings_core.py`, `hkex_filings_adapter.py`, and
  `hkex_filings_cli.py`
- `src/dalton_core/earnings_call_transcript_connector.py`
- `src/dalton_core/cockpit_research_library.py` and
  `company_research_view.py`
- Core schemas for connectors, observability, coverage missions, document read
  completion, Claims, Evidence, and candidate staging.
