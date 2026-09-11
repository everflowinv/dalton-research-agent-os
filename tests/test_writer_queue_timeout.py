import concurrent.futures
import socket
import threading
import unittest
from unittest import mock

from dalton_core.writer_protocol import decode_frame, parse_response, request_frame
from dalton_core.writer_server import WriterServer


class WriterQueueTimeoutTests(unittest.TestCase):
    def _server(self, executor, handler):
        server = object.__new__(WriterServer)
        server._store_executor = executor
        server._stop = threading.Event()
        server._handle = handler
        server._log_unhandled = mock.Mock()
        return server

    def _call(self, server, *, request_id="request-1"):
        client, peer = socket.socketpair()
        thread = threading.Thread(target=server._serve_connection, args=(peer,))
        thread.start()
        client.sendall(request_frame(
            request_id, "register_invocation", {}, "test-token",
        ))
        line = client.makefile("rb").readline()
        client.close()
        thread.join(1)
        peer.close()
        self.assertFalse(thread.is_alive())
        return parse_response(decode_frame(line))

    @mock.patch("dalton_core.writer_server.STORE_REQUEST_TIMEOUT", 0.05)
    def test_queued_request_is_cancelled_and_executor_remains_healthy(self):
        blocker = threading.Event()
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            occupied = executor.submit(blocker.wait)
            server = self._server(executor, lambda request: calls.append(request))
            response = self._call(server)
            self.assertFalse(response.ok)
            server._log_unhandled.assert_called_once()
            self.assertEqual(
                server._log_unhandled.call_args.kwargs,
                {"operation": "register_invocation", "queued_cancelled": True},
            )
            blocker.set()
            occupied.result(1)
            self.assertEqual(calls, [])
            self.assertEqual(executor.submit(lambda: "healthy").result(1), "healthy")

    @mock.patch("dalton_core.writer_server.STORE_REQUEST_TIMEOUT", 0.05)
    def test_running_request_is_not_cancelled_after_reported_timeout(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def handler(request):
            calls.append(request.request_id)
            started.set()
            release.wait()
            return {"status": "completed"}

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            server = self._server(executor, handler)
            response = self._call(server, request_id="running-request")
            self.assertTrue(started.is_set())
            self.assertFalse(response.ok)
            self.assertEqual(
                server._log_unhandled.call_args.kwargs,
                {"operation": "register_invocation", "queued_cancelled": False},
            )
            release.set()
            executor.shutdown(wait=True)
            self.assertEqual(calls, ["running-request"])


if __name__ == "__main__":
    unittest.main()
