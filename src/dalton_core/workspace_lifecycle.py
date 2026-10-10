"""Audited workspace lifecycle; research namespaces are never merged."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .workspace import WorkspaceError, load_workspace_manifest

STATES = frozenset({'active', 'paused', 'archived', 'deleted'})


def state_for_core(state_dir: str | Path) -> str:
    manifest = Path(state_dir).resolve().parent.parent / 'workspace.json'
    if not manifest.is_file():
        return 'active'  # legacy has no manifest
    return load_workspace_manifest(manifest).lifecycle


@contextmanager
def _read_db(path: Path):
    from .readonly_sqlite import connect_read_only, connect_cold_wal_snapshot
    try:
        db = connect_read_only(path)
    except sqlite3.OperationalError:
        with connect_cold_wal_snapshot(path) as db:
            yield db
    else:
        try:
            yield db
        finally:
            db.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _database(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(root / 'workspace-lifecycle.sqlite3', timeout=30)
    try:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS workspace_lifecycle_events ('
                   'id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL, slug TEXT NOT NULL,'
                   'request_id TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,'
                   'action TEXT NOT NULL, prior_state TEXT NOT NULL, state TEXT NOT NULL,'
                   'reason TEXT NOT NULL, payload_json TEXT NOT NULL,'
                   'UNIQUE(slug, request_id))')
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_no_{operation.lower()} "
                       f"BEFORE {operation} ON workspace_lifecycle_events BEGIN "
                       "SELECT RAISE(ABORT,'workspace lifecycle events are append only'); END")
        db.commit()
        os.chmod(root / 'workspace-lifecycle.sqlite3', 0o600)
        return db
    except BaseException:
        db.close()
        raise


def _record(config: Mapping[str, Any], slug: str) -> tuple[Path, dict[str, Any]]:
    if slug == 'legacy':
        raise WorkspaceError('主环境不能暂停、归档或删除')
    if not isinstance(slug, str) or not re.fullmatch(r'ws-[a-f0-9]{24}', slug):
        raise WorkspaceError('研究环境标识无效')
    path = Path(config['host_root']) / 'creation-requests' / (slug[3:] + '.json')
    try:
        return path, json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise WorkspaceError('没有这个研究环境') from exc


def review_items(workspace_root: Path) -> list[dict[str, Any]]:
    """Read concrete pending decisions and unpublished goal drafts before deletion."""
    out: list[dict[str, Any]] = []
    for path in (workspace_root / 'state').rglob('*.sqlite*'):
        if path.suffix not in ('.sqlite', '.sqlite3') or path.is_symlink():
            continue
        try:
            with _read_db(path) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                db.row_factory = sqlite3.Row
                if path.name == 'core.sqlite':
                    from .needs_human import gate_decisions, memo_decisions
                    for item in (*gate_decisions(db, state_dir=path.parent), *memo_decisions(db)):
                        out.append({'database': str(path.relative_to(workspace_root)), 'table': item['kind'],
                                    'row': item['ref'], 'status': 'pending_human_decision', 'title': item['title']})
                for table in sorted(tables):
                    if not any(part in table for part in ('draft', 'decision', 'review')):
                        continue
                    columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                    if 'status' not in columns:
                        continue
                    for row in db.execute(f'SELECT rowid,status FROM "{table}" WHERE status IN '
                                          "('open','pending','saved','draft','awaiting_human','pending_review','held','ready_for_confirmation')"):
                        out.append({'database': str(path.relative_to(workspace_root)),
                                    'table': table, 'row': row[0], 'status': row[1]})
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise WorkspaceError('待决策与草稿清单无法核验，不能删除') from exc
    draft = workspace_root / 'state' / 'dalton-core' / 'creation-goal-draft.json'
    if draft.is_file() and not any(row['table'] == 'cockpit_drafts' for row in out):
        out.append({'database': 'creation-goal-draft.json', 'table': 'unpublished_goal', 'row': 1, 'status': 'draft'})
    return out


def _manifest_state(path: Path, state: str, *, new_port: int | None = None) -> None:
    from .store import content_hash
    from .workspace_manager import _write
    raw = json.loads(path.read_text())
    raw['lifecycle'] = state
    if new_port is not None: raw['cockpit_port'] = new_port
    body = {key: val for key, val in raw.items() if key != 'content_hash'}
    raw['content_hash'] = content_hash(body)
    _write(path, raw)
    config_path = Path(raw['config_path'])
    if config_path.is_file():
        config = json.loads(config_path.read_text())
        if new_port is not None and config.get('control'):
            config['control']['config']['port'] = new_port
        if config.get('workspace'):
            config['workspace']['manifest_hash'] = raw['content_hash']
            _write(config_path, config)


def _workspace_logs(root: Path, slug: str) -> Path:
    """Only the exact namespace is owned; never follow a shared log link."""
    parent = root / 'workspace-logs'
    path = parent / slug
    if parent.is_symlink() or path.is_symlink():
        raise WorkspaceError('项目日志路径是链接，不能归档或删除共享目录')
    if parent.exists() and (not parent.is_dir() or parent.resolve().parent != root.resolve()):
        raise WorkspaceError('项目日志目录越界')
    if path.exists() and not path.is_dir():
        raise WorkspaceError('项目日志路径不是目录')
    if path.exists() and any(item.is_symlink() for item in path.rglob('*')):
        raise WorkspaceError('项目日志包含链接，不能归档或删除共享资料')
    return path


def _archive(root: Path, slug: str, request_id: str) -> dict[str, str]:
    suffix = hashlib.sha256(request_id.encode()).hexdigest()[:12]
    existing = sorted((root / 'archive').glob(f'{slug}-*-{suffix}'))
    directory = existing[0] if existing else root / 'archive' / f'{slug}-{datetime.now(timezone.utc):%Y%m%d}-{suffix}'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / 'workspace.tar.gz'
    workspace_root = root / 'workspaces' / slug
    if not target.exists():
        fd, name = tempfile.mkstemp(prefix='.archive-', dir=directory)
        os.close(fd)
        try:
            with tarfile.open(name, 'w:gz') as archive:
                archive.add(workspace_root, arcname=slug, recursive=True)
            os.chmod(name, 0o600)
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
    proof = {'archive_path': str(target), 'archive_sha256': _sha256_file(target)}
    logs = _workspace_logs(root, slug)
    if logs.exists():
        log_target = directory / 'workspace-logs.tar.gz'
        if not log_target.exists():
            fd, name = tempfile.mkstemp(prefix='.logs-', dir=directory)
            os.close(fd)
            try:
                with tarfile.open(name, 'w:gz') as archive:
                    archive.add(logs, arcname=slug, recursive=True)
                os.chmod(name, 0o600)
                os.replace(name, log_target)
            finally:
                Path(name).unlink(missing_ok=True)
        proof.update(log_archive_path=str(log_target), log_archive_sha256=_sha256_file(log_target))
    return proof


def _unserve(config: Mapping[str, Any], port: int, run) -> None:
    executable = config['tailscale_executable']
    response = run([executable, 'serve', 'status', '--json'], capture_output=True, text=True, check=True)
    status = json.loads(response.stdout)
    endpoint = f"{config['tailscale_host']}:{port}"
    existing = status.get('Web', {}).get(endpoint)
    if not existing:
        return
    if existing.get('Handlers', {}).get('/', {}).get('Proxy') != f'http://127.0.0.1:{port}':
        raise WorkspaceError('访问端口属于其他服务，不能移除')
    result = run([executable, 'serve', f'--https={port}', 'off'], capture_output=True, text=True, check=False)
    if result.returncode:
        raise WorkspaceError('项目访问地址未能移除，原始状态已保留')


def archive_integrity(host_root: Path, slug: str) -> dict[str, Any]:
    database = host_root / 'workspace-lifecycle.sqlite3'
    if not database.is_file():
        return {'status': 'retained', 'reason': 'paused state retained in workspace'}
    db = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
    try:
        row = db.execute("SELECT payload_json FROM workspace_lifecycle_events WHERE slug=? AND action IN ('archive','archive-proof') ORDER BY id DESC LIMIT 1", (slug,)).fetchone()
    finally:
        db.close()
    if row is None:
        return {'status': 'retained', 'reason': 'paused state retained in workspace'}
    value = json.loads(row[0]); path = Path(value['archive_path'])
    valid = path.is_file() and _sha256_file(path) == value['archive_sha256']
    if 'log_archive_path' in value:
        log_path = Path(value['log_archive_path'])
        valid = valid and log_path.is_file() and _sha256_file(log_path) == value.get('log_archive_sha256')
    return {'status': 'verified' if valid else 'invalid', **value}


def retained_integrity(state_dir: Path) -> dict[str, Any]:
    try:
        with _read_db(state_dir / 'core.sqlite') as core:
            valid = core.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        return {'status': 'verified' if valid else 'invalid', 'reason': 'retained SQLite state integrity'}
    except (sqlite3.Error, OSError, ValueError):
        return {'status': 'invalid', 'reason': 'retained state cannot be read'}


def latest_operation(root: Path, slug: str) -> dict[str, Any] | None:
    path = root / 'workspace-lifecycle.sqlite3'
    if not path.is_file(): return None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute('SELECT action,at,reason,payload_json,request_id FROM workspace_lifecycle_events WHERE slug=? ORDER BY id DESC LIMIT 1', (slug,)).fetchone()
    finally: db.close()
    if row is None: return None
    action, at, reason, payload, request_id = row
    return {'action': action.split(':')[-1], 'request_id': request_id.split(':')[0], 'at': at, 'reason': reason,
            'status': 'failed' if action.startswith('failed:') else 'processing' if action.startswith('intent:') or action == 'archive-proof' else 'complete',
            'message': '操作未完成，可查看审计并重试同一次请求' if action.startswith('failed:') else None}


def transition(config_path: Path, login: str, value: Mapping[str, Any], *, run=None, plan_only: bool = False) -> dict[str, Any]:
    from .workspace_manager import _config, _write, _public, _authorize, _admins, _is_admin, _serve
    from .workspace_process import _run_launchctl, install_workspace, label_namespace
    config = _config(config_path); _authorize(config, login)
    allowed = {'slug', 'action', 'request_id', 'reason', 'confirm_name', 'irreversible', 'review_ack'}
    if not isinstance(value, Mapping) or set(value) - allowed or not {'slug','action','request_id','reason'} <= value.keys():
        raise WorkspaceError('生命周期请求格式无效')
    slug, action, request_id, reason = (value[key] for key in ('slug','action','request_id','reason'))
    if action not in {'pause','archive','restore','delete','keep'} or not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
        raise WorkspaceError('生命周期操作或请求标识无效')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise WorkspaceError('请说明操作原因')
    root = Path(config['host_root']); record_path, record = _record(config, slug)
    admin = _is_admin(config, login)
    if not admin and record.get('owner_login') != login:
        raise PermissionError('workspace creator or administrator required')
    if action == 'delete' and not admin:
        raise PermissionError('workspace deletion requires administrator')
    run = subprocess.run if run is None else run
    with (root / '.creation.lock').open('a') as lock:
        os.chmod(lock.name, 0o600); fcntl.flock(lock, fcntl.LOCK_EX)
        db = _database(root)
        try:
            prior = db.execute('SELECT * FROM workspace_lifecycle_events WHERE slug=? AND request_id=?', (slug, request_id)).fetchone()
            if prior:
                if prior['actor'] != login or prior['reason'] != reason.strip() or prior['action'] != action:
                    raise WorkspaceError('这次请求已用于另一项操作')
                return {'status': prior['state'], 'replayed': True, 'workspace': _public(json.loads(record_path.read_text()), None)}
            record = json.loads(record_path.read_text()); manifest = root / 'workspaces' / slug / 'workspace.json'
            workspace = load_workspace_manifest(manifest); current = workspace.lifecycle
            targets = {'pause': ('active', 'paused'), 'archive': ('paused', 'archived'),
                       'restore': (('paused','archived'), 'active'), 'delete': (('paused','archived'), 'deleted'), 'keep': ('active','active')}
            expected, target = targets[action]
            intent = db.execute('SELECT * FROM workspace_lifecycle_events WHERE slug=? AND request_id=?', (slug, request_id + ':intent')).fetchone()
            recovered = current == target and intent is not None and action != 'keep'
            if intent and (intent['actor'] != login or intent['reason'] != reason.strip() or intent['action'] != 'intent:' + action):
                raise WorkspaceError('这次请求已用于另一项操作')
            if not recovered and current not in (expected if isinstance(expected, tuple) else (expected,)):
                raise WorkspaceError('当前状态不能执行此操作；运行中的项目须先暂停')
            payload: dict[str, Any] = {}
            if action in {'archive', 'delete', 'restore'}:
                _workspace_logs(root, slug)  # refuse links before any destructive step
            if action == 'delete':
                if value.get('confirm_name') != record['name'] or value.get('irreversible') is not True:
                    raise WorkspaceError('删除须输入项目名称并确认不可恢复')
                if value.get('review_ack') is not True:
                    raise WorkspaceError('删除前须确认已检查待决策项与未发布草稿')
                if current == 'paused':
                    payload['reviewed_items'] = review_items(workspace.workspace_root)
                else:
                    last = db.execute("SELECT payload_json FROM workspace_lifecycle_events WHERE slug=? AND action IN ('archive','archive-proof') ORDER BY id DESC LIMIT 1", (slug,)).fetchone()
                    payload['reviewed_items'] = json.loads(last[0]).get('reviewed_items', []) if last else []
            if plan_only:
                return {'status': 'planned', 'workspace': _public(record, None),
                        'action': action, 'target': target, 'reviewed_items': payload.get('reviewed_items', [])}
            if intent is None:
                db.execute('INSERT INTO workspace_lifecycle_events(workspace_id,slug,request_id,actor,at,action,prior_state,state,reason,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (workspace.workspace_id, slug, request_id + ':intent', login, datetime.now(timezone.utc).isoformat(), 'intent:' + action, current, current, reason.strip(), json.dumps(dict(value), ensure_ascii=False, sort_keys=True)))
                db.commit()
            if recovered and action == 'restore':
                from .workspace_manager import _readiness
                try:
                    _readiness(config, workspace, timeout=2.0, require_blank=False)
                except (OSError, ValueError, WorkspaceError):
                    _run_launchctl(manifest, config['launch_agents_dir'], 'stop', uid=os.getuid(), run=run)
                    install_workspace(manifest, config['launch_agents_dir'])
                    _run_launchctl(manifest, config['launch_agents_dir'], 'start', uid=os.getuid(), run=run)
                    _readiness(config, workspace, require_blank=False)
                    _serve(config, workspace.cockpit_port)
            elif action == 'pause':
                _run_launchctl(manifest, config['launch_agents_dir'], 'stop', uid=os.getuid(), run=run)
            elif action == 'archive':
                proof = db.execute('SELECT payload_json FROM workspace_lifecycle_events WHERE slug=? AND request_id=?', (slug, request_id + ':archive-proof')).fetchone()
                payload = json.loads(proof[0]) if proof else {**_archive(root, slug, request_id), 'reviewed_items': review_items(workspace.workspace_root)}
                # Archive bytes are verified before retiring the original namespace.
                if _sha256_file(Path(payload['archive_path'])) != payload['archive_sha256']:
                    raise WorkspaceError('归档校验失败')
                if proof is None:
                    db.execute('INSERT INTO workspace_lifecycle_events(workspace_id,slug,request_id,actor,at,action,prior_state,state,reason,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (workspace.workspace_id, slug, request_id + ':archive-proof', login, datetime.now(timezone.utc).isoformat(), 'archive-proof', current, current, reason.strip(), json.dumps(payload, ensure_ascii=False, sort_keys=True)))
                    db.commit()
            elif action == 'restore' and not recovered:
                restore_port = workspace.cockpit_port
                for peer in (root / 'workspaces').glob('*/workspace.json'):
                    other = load_workspace_manifest(peer)
                    if other.slug != slug and other.lifecycle in {'active','paused'} and other.cockpit_port == workspace.cockpit_port:
                        from .workspace_manager import _port
                        restore_port = _port(config, [json.loads(path.read_text()) for path in (root / 'creation-requests').glob('*.json')])
                        payload['port_reassigned'] = {'prior': workspace.cockpit_port, 'port': restore_port}
                        break
                if current == 'archived':
                    integrity = archive_integrity(root, slug)
                    if integrity['status'] != 'verified':
                        raise WorkspaceError('归档不存在或校验失败，不能恢复')
                    with tarfile.open(integrity['archive_path']) as archive:
                        members = archive.getmembers()
                        links = {member.name.rstrip('/') for member in members if member.issym() or member.islnk()}
                        for member in members:
                            if any(member.name.startswith(link + '/') for link in links):
                                raise WorkspaceError('归档不能通过链接写入外部目录')
                            path = root / 'workspaces' / member.name
                            if not path.resolve().is_relative_to(workspace.workspace_root) or member.isdev() or member.isfifo():
                                raise WorkspaceError('归档包含越界路径')
                            if member.issym() or member.islnk():
                                # Existing workspace configs contain intentional external read-only symlinks.
                                if member.islnk() and not (root / 'workspaces' / member.linkname).resolve().is_relative_to(workspace.workspace_root):
                                    raise WorkspaceError('归档包含越界硬链接')
                        archive.extractall(root / 'workspaces', filter='fully_trusted')
                    if integrity.get('log_archive_path'):
                        log_root = _workspace_logs(root, slug)
                        log_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with tarfile.open(integrity['log_archive_path']) as logs:
                            for member in logs.getmembers():
                                path = log_root.parent / member.name
                                if (not path.resolve().is_relative_to(log_root.resolve()) or
                                        member.issym() or member.islnk() or member.isdev() or member.isfifo()):
                                    raise WorkspaceError('项目日志归档包含越界路径或链接')
                            logs.extractall(log_root.parent, filter='data')
                _manifest_state(manifest, 'active', new_port=restore_port)
                workspace = load_workspace_manifest(manifest)
                try:
                    install_workspace(manifest, config['launch_agents_dir'])
                    _run_launchctl(manifest, config['launch_agents_dir'], 'start', uid=os.getuid(), run=run)
                    from .workspace_manager import _readiness
                    _readiness(config, workspace, require_blank=False)
                    _serve(config, workspace.cockpit_port)
                except Exception:
                    _run_launchctl(manifest, config['launch_agents_dir'], 'stop', uid=os.getuid(), run=run)
                    _manifest_state(manifest, 'paused')
                    raise
            if action in {'archive','delete'}:
                logs = _workspace_logs(root, slug)
                _unserve(config, workspace.cockpit_port, run)
                for role in ('writer','controller','control','thesis-impact','publication-worker'):
                    (Path(config['launch_agents_dir']) / f'{label_namespace(slug)}.{role}.plist').unlink(missing_ok=True)
                for child in workspace.workspace_root.iterdir():
                    if child.name in {'workspace.json','display.json'}:
                        continue
                    if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
                    else: child.unlink()
                if logs.exists():
                    shutil.rmtree(logs)
                payload['logs_removed'] = str(logs)
                if action == 'delete':
                    # Remove every retained archive for this exact namespace, preserving event history.
                    for directory in (root / 'archive').glob(slug + '-*'):
                        shutil.rmtree(directory)
            _manifest_state(manifest, target)
            record.update(port=workspace.cockpit_port, lifecycle=target, status='running' if target == 'active' else target,
                          retryable=False, changed_at=datetime.now(timezone.utc).isoformat())
            record['url'] = f"https://{config['tailscale_host']}:{workspace.cockpit_port}/" if target == 'active' else None
            if action == 'keep': record['kept_at'] = record['changed_at']
            _write(record_path, record)
            db.execute('INSERT INTO workspace_lifecycle_events(workspace_id,slug,request_id,actor,at,action,prior_state,state,reason,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (workspace.workspace_id, slug, request_id, login, record['changed_at'], action, intent['prior_state'] if intent else current, target, reason.strip(), json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            db.commit()
            return {'status': target, 'workspace': _public(record, None), 'reviewed_items': payload.get('reviewed_items', []), 'replayed': False}
        except Exception as exc:
            if not plan_only:
                try:
                    db.execute('INSERT INTO workspace_lifecycle_events(workspace_id,slug,request_id,actor,at,action,prior_state,state,reason,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (record.get('workspace_id') or 'unknown', slug, request_id + ':failure:' + str(time.time_ns()), login, datetime.now(timezone.utc).isoformat(),
                                'failed:' + action, record.get('lifecycle', 'active'), record.get('lifecycle', 'active'), reason.strip(), json.dumps({'error_class': type(exc).__name__})))
                    db.commit()
                except sqlite3.Error:
                    pass
            raise
        finally:
            db.close()


def request_transition(config_path: Path, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Detached child survives stopping the requesting workspace's control role."""
    import sys
    from .workspace_manager import _config
    plan = transition(config_path, login, value, plan_only=True)
    if plan.get("replayed"):
        return plan
    config = _config(config_path)
    directory = Path(config['host_root']) / 'lifecycle-operation-logs'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256((login + '\0' + value['request_id']).encode()).hexdigest()
    path = directory / (key + '.log')
    environment = dict(os.environ); environment.pop('DALTON_WORKSPACE_MANIFEST', None)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen([sys.executable, '-m', 'dalton_core.workspace_fleet',
                          '--config', str(config_path), '--login', login,
                          '--operation-json', json.dumps(dict(value), ensure_ascii=False)],
                         env=environment, stdin=subprocess.DEVNULL, stdout=descriptor,
                         stderr=descriptor, close_fds=True, start_new_session=True)
    finally:
        os.close(descriptor)
    return {**plan, 'status': 'accepted', 'request_id': value['request_id'],
            'message': '生命周期操作已交给独立进程；项目状态与审计事件将记录结果。'}


def readonly_research(config_path: Path, login: str, slug: str) -> dict[str, Any]:
    from .workspace_manager import _config, _authorize, _admins, _is_admin
    config = _config(config_path); _authorize(config, login)
    _, record = _record(config, slug)
    if not _is_admin(config, login) and record.get('owner_login') != login:
        raise PermissionError('workspace access denied')
    workspace = load_workspace_manifest(Path(config['host_root']) / 'workspaces' / slug / 'workspace.json')
    if workspace.lifecycle != 'paused':
        raise WorkspaceError('只有暂停的研究项目提供此只读视图')
    products = []
    with _read_db(workspace.state_dir / 'core.sqlite') as core:
        try:
            row = core.execute('SELECT v.record_json FROM coverage_mission_pointer p JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id ORDER BY v.created_at DESC LIMIT 1').fetchone()
            mission = json.loads(row[0]) if row else None
        except sqlite3.Error:
            mission = None
    if mission is not None:
        from .cockpit_research_library import research_library
        database = workspace.state_dir / 'core.sqlite'
        with _read_db(database) as db:
            db.row_factory = sqlite3.Row
            seen = set()
            for company in mission.get('universe', []):
                library = research_library(db, mission, company['company_ref'])
                for product in library['products']:
                    identity = product.get('version_ref')
                    if product.get('status') == 'available' and identity not in seen:
                        products.append(product); seen.add(identity)
    for path in sorted((workspace.state_dir / 'products').rglob('*.json')):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if isinstance(raw, dict) and raw.get('status') in {'completed','published','ready'}:
            products.append(raw)
    return {'slug': slug, 'name': record['name'], 'lifecycle': 'paused', 'read_only': True,
            'products': products, 'pending_review': review_items(workspace.workspace_root)}
