"""Read-only display attachments, published explicitly outside research authorities.

An attachment is selected only for the exact source projection. Reading the
Cockpit never creates a directory, calls a model, or changes a research record.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .research_localization import (ResearchLocalizationError, select_localized,
                                   source_content_hash, validate_localization)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 16 * 1024 * 1024


def directory_for_database(database: str | Path) -> Path:
    return Path(database).expanduser().resolve().parent / "research-localization"


def directory_for_connection(connection: sqlite3.Connection) -> Path | None:
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return directory_for_database(row[2])
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError("display attachment is missing or exceeds the size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("display attachment must be an object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("display attachment path must not be a symlink")
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(data) > _MAX_BYTES:
        raise ValueError("display attachment exceeds the size limit")
    fd, tmp = tempfile.mkstemp(prefix=".localization-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def publish_attachment(directory: str | Path, product: Mapping[str, Any],
                       candidate: Mapping[str, Any]) -> Path:
    """Explicitly publish a validated display file; no SQLite write is made."""
    import fcntl

    valid = validate_localization(product, candidate)
    root = Path(directory).expanduser()
    if root.is_symlink():
        raise ValueError("display attachment directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = root / "records"
    if records.is_symlink():
        raise ValueError("display records directory must not be a symlink")
    records.mkdir(exist_ok=True, mode=0o700)
    target = records / (valid["content_hash"] + ".json")
    if target.exists():
        if _read_json(target) != valid:
            raise ValueError("immutable display attachment changed")
    else:
        _atomic_json(target, valid)
    lock = root / ".index.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        index_path = root / "index.json"
        index = _read_json(index_path) if index_path.exists() else {
            "schema_version": "research-localization-index:0.1", "entries": {}}
        if index.get("schema_version") != "research-localization-index:0.1" or not isinstance(index.get("entries"), dict):
            raise ValueError("unsupported display index")
        index["entries"][source_content_hash(product)] = valid["content_hash"]
        _atomic_json(index_path, index)
    return target


def _review_required(root: Path) -> bool:
    policy=root.parent/'research-language-policy.json'
    if not policy.exists():return False
    try:return _read_json(policy).get('required') is True
    except (OSError,ValueError):return True


def _publication_receipt_valid(root: Path, product: Mapping[str,Any], candidate: Mapping[str,Any]) -> bool:
    from .store import content_hash
    try:
        receipt=_read_json(root/'language-reviews'/(candidate['content_hash']+'.json'))
        if (receipt.get('schema_version')!='publication-language-receipt:0.1'
            or receipt.get('source_content_hash')!=source_content_hash(product)
            or receipt.get('localization_content_hash')!=candidate['content_hash']
            or receipt.get('content_hash')!=content_hash({k:v for k,v in receipt.items() if k!='content_hash'})):
            return False
        rows=[]
        for stage in receipt['reviews']:
            if (stage.get('status')!='passed'
                or (stage.get('language_review')or{}).get('status')!='ready_for_publication'
                or (stage.get('independence')or{}).get('independent') is not True):return False
            offset=len(rows)
            rows.extend({**row,'index':offset+i} for i,row in enumerate(stage['localized']['sections']))
        return rows==[{k:r[k] for k in ('index','title','body','gaps')} for r in candidate['sections']]
    except (OSError,ValueError,KeyError,TypeError):return False


def has_reviewed_attachment(directory: str | Path, product: Mapping[str,Any]) -> bool:
    root=Path(directory)
    try:
        key=_read_json(root/'index.json')['entries'][source_content_hash(product)]
        if not isinstance(key,str) or not _SHA.fullmatch(key):return False
        candidate=_read_json(root/'records'/(key+'.json'))
        validate_localization(product,candidate)
        return _publication_receipt_valid(root,product,candidate)
    except (OSError,ValueError,KeyError,TypeError):return False


def localize_library(connection: sqlite3.Connection, library: Mapping[str, Any]) -> dict[str, Any]:
    root=directory_for_connection(connection)
    if root is None:return dict(library)
    required=_review_required(root)
    if not root.exists() and not required:return dict(library)
    result=copy.deepcopy(dict(library))
    try:
        if root.is_symlink():raise ValueError('unsafe display directory')
        index=_read_json(root/'index.json')
        if index.get('schema_version')!='research-localization-index:0.1':raise ValueError('unsupported display index')
        entries=index['entries']
        if not isinstance(entries,dict):raise ValueError('invalid display index entries')
    except (OSError,ValueError,KeyError):entries={}
    for i,product in enumerate(result.get('products')or[]):
        key=entries.get(source_content_hash(product))
        ready=False
        if isinstance(key,str) and _SHA.fullmatch(key):
            try:
                if (root/'records').is_symlink():raise ValueError('unsafe display directory')
                candidate=_read_json(root/'records'/(key+'.json'))
                if candidate.get('content_hash')!=key:raise ValueError('display index binding mismatch')
                if required and not _publication_receipt_valid(root,product,candidate):
                    raise ValueError('language review receipt is missing')
                result['products'][i]=select_localized(product,candidate)
                result['products'][i]['publication_status']='ready'
                ready=True
            except (OSError,ValueError,KeyError,TypeError):
                product['localization_status']='原文已更新，中文版本待同步'
        if required and not ready and product.get('status')=='available':
            product['sections']=[]
            product['publication_status']='pending_language_review'
            product['display_reason']='正文正在进行语言检查，完成后会在这里显示。'
    return result


UI_TEXTS_LEGACY_SCHEMA = 'cockpit-ui-texts:0.1'
UI_TEXTS_SCHEMA = 'cockpit-ui-texts:0.2'
UI_RECORDS_DIRECTORY = 'ui-records'


def publish_ui_texts(directory: str | Path, batches: list[dict[str, Any]]) -> Path:
    """Merge exact-string mappings so one new product cannot erase other pages.

    0.2 stores one file per batch under ``ui-records/<source hash>.json`` and
    keeps ``ui-texts.json`` as the ordered list of batch refs.  The 0.1 layout
    held every batch in that one file; on 2026-09-22 it reached 16,775,691 of
    its 16,777,216 permitted bytes (836 batches), and from then on every
    publication that had to merge a UI batch failed with "display attachment
    exceeds the size limit" -- 28 of 31 products after the next deploy.  The
    per-file limit stays as it is: no single batch comes near it, and a limit
    that is raised instead of split is only hit again later.

    A 0.1 file is migrated on the first publish, in its own order, so the
    first-approved-translation-wins rule of ``_load_ui_cached`` is unchanged.
    """
    import fcntl
    for batch in batches:
        validate_localization(batch['source'],batch['localization'])
    root=Path(directory)
    if root.is_symlink():raise ValueError('unsafe display directory')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=root/'ui-texts.json'
    records=root/UI_RECORDS_DIRECTORY
    fd=os.open(root/'.index.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if records.is_symlink():raise ValueError('unsafe display directory')
        records.mkdir(exist_ok=True,mode=0o700)
        existing=_read_json(target) if target.exists() else {'schema_version':UI_TEXTS_SCHEMA,'batch_refs':[]}
        if existing.get('schema_version')==UI_TEXTS_LEGACY_SCHEMA:
            order=[]
            for batch in existing['batches']:
                ref=source_content_hash(batch['source'])
                _atomic_json(records/(ref+'.json'),batch)
                if ref not in order:order.append(ref)
        elif existing.get('schema_version')==UI_TEXTS_SCHEMA:
            order=list(existing.get('batch_refs') or [])
            if any(not isinstance(ref,str) or not _SHA.fullmatch(ref) for ref in order):
                raise ValueError('invalid UI mapping index')
        else:
            raise ValueError('invalid UI mapping schema')
        for batch in batches:
            ref=source_content_hash(batch['source'])
            _atomic_json(records/(ref+'.json'),batch)
            if ref not in order:order.append(ref)
        # A repeated exact string is intentionally one display entry; preserve
        # the first approved translation instead of making page context change it.
        _atomic_json(target,{'schema_version':UI_TEXTS_SCHEMA,'batch_refs':order})
    return target


def _batch_pairs(batch: Any) -> tuple[tuple[str, str], ...]:
    """The (source, reviewed translation) bodies of one validated batch."""
    source = batch["source"]
    valid = validate_localization(source, batch["localization"])
    return tuple((original["body"], localized["body"])
                 for original, localized in zip(source["sections"], valid["sections"]))


# 2026-09-25: what one 0.2 batch record contributes, validated once per file
# state.  ``ui-texts.json`` is rewritten by every publish -- live, every one to
# two minutes -- and each rewrite used to re-read and re-validate all ~860
# records (5-8 s of regular-expression work holding the GIL) before one
# ui-texts or overview request could be answered; four parallel requests from
# the page did it four times over.  A record is replaced whole (new inode) when
# its batch is published again, so (inode, mtime, size) identifies its content
# exactly as the same triple identifies ``ui-texts.json`` itself.
_UI_RECORD_PAIRS: dict[str, tuple[tuple[int, int, int], tuple[tuple[str, str], ...] | None]] = {}
_UI_RECORD_LOCK = threading.Lock()


def _ui_record_pairs(path: Path) -> tuple[tuple[str, str], ...] | None:
    """One record's pairs, or None when it is unreadable or does not validate."""
    try:
        if path.is_symlink():
            return None
        stat = path.stat()
    except OSError:
        return None
    identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    key = str(path)
    with _UI_RECORD_LOCK:
        held = _UI_RECORD_PAIRS.get(key)
    if held is not None and held[0] == identity:
        return held[1]
    try:
        pairs: tuple[tuple[str, str], ...] | None = _batch_pairs(_read_json(path))
    except (OSError, KeyError, TypeError, ValueError):
        # One unreadable record costs its own strings, not every page's.
        pairs = None
    with _UI_RECORD_LOCK:
        _UI_RECORD_PAIRS[key] = (identity, pairs)
    return pairs


def _ui_record_pair_lists(path: Path, payload: Mapping[str, Any]) -> list[tuple[tuple[str, str], ...]]:
    records = path.parent / UI_RECORDS_DIRECTORY
    if records.is_symlink():
        return []
    wanted: list[Path] = []
    for ref in payload.get("batch_refs") or []:
        if isinstance(ref, str) and _SHA.fullmatch(ref):
            wanted.append(records / (ref + ".json"))
    result = []
    for record in wanted:
        pairs = _ui_record_pairs(record)
        if pairs is not None:
            result.append(pairs)
    # Forget records this index no longer names, so the memo stays the size
    # of the mapping it serves.
    keep = {str(record) for record in wanted}
    with _UI_RECORD_LOCK:
        for key in [key for key in _UI_RECORD_PAIRS
                    if key.startswith(str(records)) and key not in keep]:
            del _UI_RECORD_PAIRS[key]
    return result


@functools.lru_cache(maxsize=8)
def _load_ui_cached(path_string: str, inode: int, modified_ns: int, size: int) -> dict[str, str]:
    path = Path(path_string)
    payload = _read_json(path)
    entries = {}
    if payload.get("schema_version") == UI_TEXTS_LEGACY_SCHEMA:
        # 0.1 behaviour, unchanged: one bad batch in the single file
        # invalidates the file.
        pair_lists = [_batch_pairs(batch) for batch in payload.get("batches", [])]
    elif payload.get("schema_version") == UI_TEXTS_SCHEMA:
        pair_lists = _ui_record_pair_lists(path, payload)
    else:
        pair_lists = []
    for pairs in pair_lists:
        for old, new in pairs:
            if old not in entries:
                entries[old] = new
    return entries


# The Cockpit asks for UI translations by key, one view at a time, instead of
# receiving the whole mapping inside the overview.  Live on 2026-09-24 the
# mapping held 8,124 approved strings, 4.46 MB of JSON; the overview dropped
# any mapping over 2 MB whole, so not one approved translation reached the
# page.  A key is the first 16 hex digits of SHA-256 over the exact UTF-8
# source string; the page computes the same digest.
UI_TEXT_KEY_LENGTH = 16
UI_TEXT_MAX_KEYS_PER_REQUEST = 256
UI_TEXT_MAX_RESPONSE_BYTES = 256 * 1024
_UI_TEXT_KEY = re.compile(r"[0-9a-f]{%d}\Z" % UI_TEXT_KEY_LENGTH)


def ui_text_key(text: str) -> str | None:
    """The lookup key of one exact source string, or None if it has none.

    A lone surrogate has no UTF-8 form; the browser would encode it as
    U+FFFD and so could never ask for it by the same key.
    """
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(data).hexdigest()[:UI_TEXT_KEY_LENGTH]


@functools.lru_cache(maxsize=4)
def _ui_index_cached(path_string: str, inode: int, modified_ns: int,
                     size: int) -> tuple[str, dict[str, str], dict[str, str | None]]:
    """Display mapping and key index, built once per ``ui-texts.json`` state.

    Every publish rewrites ``ui-texts.json`` (0.1 whole; 0.2 its batch index
    after the records), so its stat identifies the mapping.  A request then
    costs one ``stat`` and dictionary lookups, not a re-read of every batch.
    In the key index ``None`` means the reviewed translation is the source
    string itself, which the page already holds.
    """
    entries = _load_ui_cached(path_string, inode, modified_ns, size)
    display: dict[str, str] = {}
    index: dict[str, str | None] = {}
    collided: set[str] = set()
    for original, localized in entries.items():
        shown, key = _shown_and_key(original, localized)
        display[original] = shown
        if key is None:
            continue
        if key in index:
            # Two sources sharing a key: neither is served by key, so neither
            # can be shown the other's translation.
            collided.add(key)
            continue
        index[key] = None if shown == original else shown
    for key in collided:
        index.pop(key, None)
    revision = hashlib.sha256(
        f"{path_string}\0{inode}\0{modified_ns}\0{size}".encode()).hexdigest()[:16]
    return revision, display, index


@functools.lru_cache(maxsize=65536)
def _shown_and_key(original: str, localized: str) -> tuple[str, str | None]:
    """The displayed form of one translation and its source key.

    Both are pure functions of the two strings, and the same eight thousand
    pairs come back on every rebuild, so a publish that adds one batch pays
    for that batch's strings only.
    """
    from .research_gap_display import display_metadata_text
    return display_metadata_text(localized), ui_text_key(original)


# One build at a time.  ``functools.lru_cache`` does not hold concurrent
# callers of a missing entry back, so every request that arrived while the
# index was being rebuilt started its own rebuild; the page asks with four
# requests in parallel, and the overview and the log ask too.
_UI_INDEX_BUILD_LOCK = threading.Lock()


def _ui_index(database: str | Path) -> tuple[str, dict[str, str], dict[str, str | None]] | None:
    path = directory_for_database(database) / "ui-texts.json"
    try:
        if path.parent.is_symlink() or path.is_symlink():
            return None
        stat = path.stat()
        with _UI_INDEX_BUILD_LOCK:
            return _ui_index_cached(str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, KeyError, TypeError, ResearchLocalizationError):
        return None


def load_ui_texts(database: str | Path) -> dict[str, str]:
    index = _ui_index(database)
    return {} if index is None else dict(index[1])


def ui_texts_revision(database: str | Path) -> str | None:
    """Identity of the current mapping; changes whenever a batch is published."""
    index = _ui_index(database)
    return None if index is None else index[0]


def lookup_ui_texts(database: str | Path, keys: list[str], *,
                    max_bytes: int | None = None,
                    max_keys: int | None = None) -> dict[str, Any]:
    """Reviewed translations for the requested keys, in one bounded page.

    ``texts`` maps a key to its reviewed translation; ``same`` lists keys whose
    reviewed translation is the source string itself.  A requested key in
    neither list and not in ``deferred`` has no reviewed translation.  Keys
    past ``max_keys``, or whose translation would take the page past
    ``max_bytes``, are returned in ``deferred`` to be asked for again -- a
    page is cut, never dropped.  A single translation larger than
    ``max_bytes`` on its own is listed in ``withheld``: it cannot be sent
    within the bound, and the page treats it as not yet reviewed.
    """
    max_bytes = UI_TEXT_MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    max_keys = UI_TEXT_MAX_KEYS_PER_REQUEST if max_keys is None else max_keys
    wanted: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if isinstance(key, str) and _UI_TEXT_KEY.fullmatch(key) and key not in seen:
            seen.add(key)
            wanted.append(key)
    loaded = _ui_index(database)
    revision, index = (None, {}) if loaded is None else (loaded[0], loaded[2])
    texts: dict[str, str] = {}
    same: list[str] = []
    deferred: list[str] = list(wanted[max_keys:])
    withheld: list[str] = []
    used = 0
    for position, key in enumerate(wanted[:max_keys]):
        if key not in index:
            continue
        value = index[key]
        if value is None:
            cost = len(key) + 3
        else:
            cost = len(key) + 6 + len(json.dumps(value, ensure_ascii=False).encode())
        if cost > max_bytes:
            withheld.append(key)
            continue
        if used + cost > max_bytes:
            deferred = [k for k in wanted[position:max_keys] if k in index
                        and k not in withheld] + deferred
            break
        used += cost
        if value is None:
            same.append(key)
        else:
            texts[key] = value
    return {"revision": revision, "texts": texts, "same": same,
            "deferred": deferred, "withheld": withheld}


def publish_reviewed_attachment(directory: str | Path, product: Mapping[str, Any],
                                candidate: Mapping[str, Any], reviews: list[Mapping[str, Any]]) -> Path:
    """Persist language/author/verifier receipts before making prose visible."""
    from .store import content_hash
    valid = validate_localization(product,candidate)
    sections=[]
    for stage in reviews:
        review=stage.get('language_review') or {}
        if (stage.get('status') != 'passed' or review.get('status') != 'ready_for_publication'
                or not (stage.get('independence') or {}).get('independent')):
            raise ValueError('publication language and semantic review are incomplete')
        for row in stage['localized']['sections']:
            sections.append({**row,'index':len(sections)})
    expected=[{key:row[key] for key in ('index','title','body','gaps')} for row in valid['sections']]
    if sections != expected:
        raise ValueError('reviewed sections do not match the publication')
    root=Path(directory)
    records=root/'language-reviews'
    if root.is_symlink() or records.is_symlink():
        raise ValueError('unsafe review directory')
    records.mkdir(parents=True,exist_ok=True,mode=0o700)
    receipt={'schema_version':'publication-language-receipt:0.1',
             'source_content_hash':source_content_hash(product),
             'localization_content_hash':valid['content_hash'],'reviews':reviews}
    receipt['content_hash']=content_hash(receipt)
    _atomic_json(records/(valid['content_hash']+'.json'),receipt)
    # The human-readable suggestions are all retained, in document order.
    markdown='\n\n'.join(stage['language_review']['suggestions_markdown'] for stage in reviews)
    from .research_html_export import _atomic_owner_write
    _atomic_owner_write(records/(valid['content_hash']+'.md'),markdown.encode())
    return publish_attachment(root,product,valid)
