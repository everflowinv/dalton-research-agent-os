"""Cooperative publication stop at a persisted product/batch boundary.

Deployment automation requests quiescence, waits for the existing worker lock,
then stops the service. The acknowledgement is not a fee settlement proof.
A request survives scheduled runs and config replacement until explicit CAS
resume; no model/cache/attempt/fee record is modified by this protocol.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

_SCHEMA = 'research-publication-quiesce:0.1'
_FIELDS = {'schema_version', 'request_id', 'config_path', 'work_dir',
           'config_sha256', 'created_at', 'content_hash'}


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _safe_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
        raise ValueError('publication quiesce file must be private, owned and regular')


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


class QuiesceControl:
    def __init__(self, config_path, work_dir):
        self.config_path = Path(config_path).resolve()
        self.root = Path(work_dir).resolve()
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('publication work directory must be owned')
        self.request_path = self.root / 'quiesce-request.json'
        self.ack_path = self.root / 'quiesce-ack.json'

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def control_lock(self):
        path = self.root / '.quiesce-control.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            _safe_file(path)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally: os.close(fd)

    def read_request(self):
        try: _safe_file(self.request_path)
        except FileNotFoundError: return None
        value = json.loads(self.request_path.read_text())
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError('publication quiesce request has invalid fields')
        body = {k: v for k, v in value.items() if k != 'content_hash'}
        if (value['schema_version'] != _SCHEMA or value['content_hash'] != _digest(body)
                or value['config_path'] != str(self.config_path)
                or value['work_dir'] != str(self.root)
                or not isinstance(value['request_id'], str)
                or len(value['request_id']) != 32
                or any(c not in '0123456789abcdef' for c in value['request_id'])
                or not isinstance(value['config_sha256'], str)
                or len(value['config_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in value['config_sha256'])):
            raise ValueError('publication quiesce scope/hash is invalid')
        moment = datetime.fromisoformat(value['created_at'])
        if moment.tzinfo is None: raise ValueError('publication quiesce time must be aware')
        return value

    def read_ack(self):
        try: _safe_file(self.ack_path)
        except FileNotFoundError: return None
        value = json.loads(self.ack_path.read_text())
        fields = {'schema_version', 'request_hash', 'work_dir', 'worker_pid',
                  'created_at', 'status', 'checkpoint_sha256', 'fee_settlement_proof',
                  'content_hash'}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError('publication quiesce acknowledgement has invalid fields')
        body = {k: v for k, v in value.items() if k != 'content_hash'}
        if (value['schema_version'] != 'research-publication-quiesce-ack:0.1'
                or value['content_hash'] != _digest(body)
                or value['work_dir'] != str(self.root)
                or value['status'] != 'worker_at_persisted_boundary'
                or value['fee_settlement_proof'] is not False):
            raise ValueError('publication quiesce acknowledgement drifted')
        return value

    def requested(self):
        return self.read_request() is not None

    def request(self):
        with self.control_lock():
            current = self.read_request()
            if current is not None: return current
            body = {'schema_version': _SCHEMA, 'request_id': uuid.uuid4().hex,
                    'config_path': str(self.config_path), 'work_dir': str(self.root),
                    'config_sha256': sha256(self.config_path.read_bytes()).hexdigest(),
                    'created_at': self.now()}
            value = {**body, 'content_hash': _digest(body)}
            _write(self.request_path, value)
            return value

    def acknowledge(self, checkpoint_path):
        # Caller owns .worker.lock and has no outstanding prepare callback.
        with self.control_lock():
            request = self.read_request()
            if request is None: return None
            body = {'schema_version': 'research-publication-quiesce-ack:0.1',
                    'request_hash': request['content_hash'], 'work_dir': str(self.root),
                    'worker_pid': os.getpid(), 'created_at': self.now(),
                    'status': 'worker_at_persisted_boundary',
                    'checkpoint_sha256': sha256(Path(checkpoint_path).read_bytes()).hexdigest(),
                    'fee_settlement_proof': False}
            value = {**body, 'content_hash': _digest(body)}
            _write(self.ack_path, value)
            return value

    def resume(self, expected_request_hash):
        # Never let automation clear the request under a still-running pass.
        path = self.root / '.worker.lock'
        _safe_file(path)
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('publication worker is still active') from None
            with self.control_lock():
                request = self.read_request()
                if request is None or request['content_hash'] != expected_request_hash:
                    raise ValueError('publication quiesce resume CAS failed')
                # Retain each exact request/ack as deployment diagnostics.
                archive = self.root / ('quiesce-retired-' + request['request_id'] + '.json')
                if archive.exists(): raise ValueError('publication quiesce retirement already exists')
                _write(archive, request)
                ack = self.read_ack()
                if ack is not None and ack['request_hash'] == expected_request_hash:
                    _write(self.root / ('quiesce-retired-' + request['request_id'] + '-ack.json'), ack)
                self.request_path.unlink()
                return {'status': 'resumed', 'request_hash': expected_request_hash}
        finally: os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('request', 'inspect', 'resume'))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--request-hash')
    args = parser.parse_args(argv)
    from .research_output_preparation import validate_worker_config
    cfg = json.loads(args.config.read_text()); validate_worker_config(cfg)
    control = QuiesceControl(args.config, cfg['work_dir'])
    if args.command == 'request': result = control.request()
    elif args.command == 'resume': result = control.resume(args.request_hash)
    else:
        request = control.read_request()
        result = {'requested': request is not None,
                  'request_hash': request['content_hash'] if request else None}
        if request:
            ack = control.read_ack()
            result['acknowledged'] = bool(ack and ack['request_hash'] == request['content_hash'])
            if result['acknowledged']: result['acknowledgement'] = ack
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
