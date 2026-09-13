"""Read-only display attachments, published explicitly outside research authorities.

An attachment is selected only for the exact source projection. Reading the
Cockpit never creates a directory, calls a model, or changes a research record.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import re
import sqlite3
import tempfile
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


def publish_ui_texts(directory: str | Path, batches: list[dict[str, Any]]) -> Path:
    """Merge exact-string mappings so one new product cannot erase other pages."""
    import fcntl
    for batch in batches:
        validate_localization(batch['source'],batch['localization'])
    root=Path(directory)
    if root.is_symlink():raise ValueError('unsafe display directory')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=root/'ui-texts.json'
    fd=os.open(root/'.index.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        existing=_read_json(target) if target.exists() else {'schema_version':'cockpit-ui-texts:0.1','batches':[]}
        if existing.get('schema_version')!='cockpit-ui-texts:0.1':raise ValueError('invalid UI mapping schema')
        merged={source_content_hash(batch['source']):batch for batch in existing['batches']}
        for batch in batches:merged[source_content_hash(batch['source'])]=batch
        # A repeated exact string is intentionally one display entry; preserve
        # the first approved translation instead of making page context change it.
        _atomic_json(target,{'schema_version':'cockpit-ui-texts:0.1','batches':list(merged.values())})
    return target


@functools.lru_cache(maxsize=8)
def _load_ui_cached(path_string: str, inode: int, modified_ns: int, size: int) -> dict[str, str]:
    payload = _read_json(Path(path_string))
    if payload.get("schema_version") != "cockpit-ui-texts:0.1":
        return {}
    entries = {}
    for batch in payload.get("batches", []):
        source = batch["source"]
        valid = validate_localization(source, batch["localization"])
        for original, localized in zip(source["sections"], valid["sections"]):
            old, new = original["body"], localized["body"]
            if old not in entries:
                entries[old] = new
    return entries


def load_ui_texts(database: str | Path) -> dict[str, str]:
    path = directory_for_database(database) / "ui-texts.json"
    try:
        if path.parent.is_symlink() or path.is_symlink():
            return {}
        stat = path.stat()
        return dict(_load_ui_cached(str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError, KeyError, TypeError, ResearchLocalizationError):
        return {}


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
