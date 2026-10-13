"""Stop an existing extraction run at a durable boundary, without signals.

Requests live only in the exact launch directory. They do not change budget,
Work, fee, or research authority, and never authorize another model attempt.
"""
from __future__ import annotations
import argparse
import json
import os
import tempfile
from pathlib import Path
from .store import content_hash

SCHEMA = 'document-extraction-quiesce:0.1'
BINDING_FIELDS = ('id', 'pid', 'started_at', 'model_config_path', 'model_config_fingerprint')


class ExtractionQuiesced(BaseException):
    """Cooperative control flow; never classify cancellation as a source error."""
    def __init__(self, proof):
        self.proof = proof
        super().__init__('document extraction quiesced at a durable boundary')


def _binding(ticket):
    return {key: ticket[key] for key in BINDING_FIELDS}


def request_quiesce(run_dir: Path, *, expected_ticket_hash: str):
    directory = Path(run_dir)
    if directory.is_symlink() or directory.joinpath('ticket.json').is_symlink():
        raise ValueError('extraction quiesce requires an exact regular ticket')
    ticket = json.loads(directory.joinpath('ticket.json').read_text())
    if ticket.get('status') != 'running' or content_hash(ticket) != expected_ticket_hash:
        raise ValueError('extraction quiesce ticket changed or is not running')
    body = {'schema_version': SCHEMA, 'ticket_binding': _binding(ticket),
            'ticket_hash_at_request': expected_ticket_hash}
    request = {**body, 'content_hash': content_hash(body)}
    path = directory / 'quiesce-request.json'
    # Publish complete bytes atomically. The child must never see a partly
    # written stop request as a malformed source or governance failure.
    fd, temporary = tempfile.mkstemp(prefix='.quiesce-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(request, stream, sort_keys=True);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or json.loads(path.read_text()) != request:
                raise ValueError('extraction quiesce request conflicts')
    finally:
        Path(temporary).unlink(missing_ok=True)
    fd = os.open(directory, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return request


class RunQuiesce:
    def __init__(self, directory: Path, model_config_path: Path):
        self.directory = Path(directory)
        self.model_config_path = str(model_config_path)

    def checkpoint(self):
        path = self.directory / 'quiesce-request.json'
        if not path.exists():
            return
        if path.is_symlink():
            raise ValueError('extraction quiesce request must be regular')
        request = json.loads(path.read_text())
        body = {k:v for k,v in request.items() if k != 'content_hash'}
        ticket = json.loads(self.directory.joinpath('ticket.json').read_text())
        if (set(request) != {'schema_version','ticket_binding','ticket_hash_at_request','content_hash'}
                or request['schema_version'] != SCHEMA
                or request['content_hash'] != content_hash(body)
                or request['ticket_binding'] != _binding(ticket)
                or ticket['pid'] != os.getpid()
                or ticket['model_config_path'] != self.model_config_path
                or ticket['status'] != 'running'
                or request['ticket_hash_at_request'] != content_hash(ticket)):
            raise ValueError('extraction quiesce request does not bind this live run')
        raise ExtractionQuiesced(request)


def quiesced_summary_matches(summary, ticket):
    if not isinstance(summary, dict) or summary.get('status') != 'quiesced':
        return False
    proof = summary.get('quiesce')
    if not isinstance(proof, dict): return False
    body = {k:v for k,v in proof.items() if k != 'content_hash'}
    original_ticket = {**ticket, 'status':'running', 'exit_code':None, 'completed_at':None}
    original_ticket.pop('adopted_from_summary', None)
    return (set(proof) == {'schema_version','ticket_binding','ticket_hash_at_request','content_hash'}
            and proof['schema_version'] == SCHEMA
            and proof['content_hash'] == content_hash(body)
            and proof['ticket_binding'] == _binding(ticket)
            and proof['ticket_hash_at_request'] == content_hash(original_ticket)
            and summary.get('resumable') is True
            and summary.get('stop_reason') == 'cooperative_quiesce')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Request safe boundary stop of one existing extraction run')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--expected-ticket-hash', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(request_quiesce(args.run_dir,expected_ticket_hash=args.expected_ticket_hash),sort_keys=True))
    return 0

if __name__ == '__main__': raise SystemExit(main())
