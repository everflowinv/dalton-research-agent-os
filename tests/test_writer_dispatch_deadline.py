"""Dispatch waits preserve the result of a slow, single execution."""
import concurrent.futures
import socket
import threading
import unittest
from unittest import mock

from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import (DISPATCH_CLIENT_TIMEOUT, DISPATCH_REQUEST_TIMEOUT,
    decode_frame, parse_response, request_frame, success_frame)
from dalton_core.writer_server import WriterServer


class DispatchDeadlineTests(unittest.TestCase):
    def exchange(self, handler, operation='dispatch_company_model_spec'):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            server = object.__new__(WriterServer)
            server._store_executor = executor
            server._stop = threading.Event()
            server._handle = handler
            server._log_unhandled = mock.Mock()
            client, peer = socket.socketpair()
            thread = threading.Thread(target=server._serve_connection, args=(peer,))
            thread.start()
            try:
                client.sendall(request_frame('slow-dispatch', operation, {}, 'test-token'))
                with client.makefile('rb') as reader:
                    response = parse_response(decode_frame(reader.readline()))
            finally:
                client.close()
                thread.join(1)
                peer.close()
            self.assertFalse(thread.is_alive())
            return response, server

    @mock.patch('dalton_core.writer_server.STORE_REQUEST_TIMEOUT', .01)
    @mock.patch('dalton_core.writer_server.DISPATCH_REQUEST_TIMEOUT', .5)
    def test_slow_dispatch_returns_single_success_after_ordinary_deadline(self):
        calls = []
        def handler(request):
            calls.append(request.request_id)
            threading.Event().wait(.05)
            return {'status': 'launched', 'ticket_ref': 'ticket:one'}
        result, server = self.exchange(handler)
        self.assertTrue(result.ok)
        self.assertEqual(result.result['ticket_ref'], 'ticket:one')
        self.assertEqual(calls, ['slow-dispatch'])
        server._log_unhandled.assert_not_called()

    @mock.patch('dalton_core.writer_server.DISPATCH_REQUEST_TIMEOUT', .01)
    def test_expired_dispatch_keeps_running_and_never_reexecutes(self):
        calls = []
        def handler(request):
            calls.append(request.request_id)
            threading.Event().wait(.05)
            return {'status': 'completed'}
        result, server = self.exchange(handler)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'], 'store_timeout')
        self.assertEqual(calls, ['slow-dispatch'])
        self.assertFalse(server._log_unhandled.call_args.kwargs['queued_cancelled'])

    def socket_timeout(self, operation, timeout=45):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.makefile.return_value.readline.return_value = success_frame('fixed', {})
        with mock.patch('dalton_core.writer_client.socket.socket', return_value=sock):
            WriterClient('/tmp/no-live-socket', 'test-token', timeout=timeout).call(
                operation, request_id='fixed')
        return sock.settimeout.call_args.args[0]

    def test_dispatch_client_outlives_server_and_keeps_long_explicit_bound(self):
        self.assertGreater(DISPATCH_CLIENT_TIMEOUT, DISPATCH_REQUEST_TIMEOUT)
        self.assertEqual(self.socket_timeout('dispatch_investment_memo'), DISPATCH_CLIENT_TIMEOUT)
        self.assertEqual(self.socket_timeout('dispatch_mission_document_research', 180), 180)

    def test_short_probe_and_ordinary_calls_preserve_deadlines(self):
        self.assertEqual(self.socket_timeout('dispatch_investment_memo', 2), 2)
        self.assertEqual(self.socket_timeout('register_invocation'), 45)
