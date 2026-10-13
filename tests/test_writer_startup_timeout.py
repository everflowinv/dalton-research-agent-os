import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dalton_core.writer_server import WriterServer


class WriterStartupTimeoutTests(unittest.TestCase):
    def server(self, opener):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        server = object.__new__(WriterServer)
        server.socket_path = str(Path(directory.name) / 'writer.sock')
        server._listener = None
        server._open_store = opener
        server._close_failed_open = Mock()
        def clean():
            if server._listener is not None:
                server._listener.close()
            for name in ('_store_executor', '_connection_executor', '_read_executor'):
                executor = getattr(server, name, None)
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
        self.addCleanup(clean)
        return server

    @patch('dalton_core.writer_server.STORE_REQUEST_TIMEOUT', 0.005)
    @patch('dalton_core.writer_server.STORE_STARTUP_TIMEOUT', 1.0)
    def test_cold_initialization_can_exceed_rpc_deadline_before_serving(self):
        threads = []
        def opener():
            threads.append(threading.get_ident())
            time.sleep(0.03)
        server = self.server(opener)
        server.start()
        self.assertIsNotNone(server._listener)
        self.assertNotEqual(threads[0], threading.get_ident())
        server._close_failed_open.assert_not_called()

    @patch('dalton_core.writer_server.STORE_STARTUP_TIMEOUT', 0.005)
    def test_startup_deadline_still_refuses_listener_and_cleans_open_state(self):
        server = self.server(lambda: time.sleep(0.03))
        with self.assertRaises(TimeoutError):
            server.start()
        self.assertIsNone(server._listener)
        server._close_failed_open.assert_called_once()

    def test_initialization_error_is_preserved_without_binding_listener(self):
        def opener():
            raise ValueError('schema integrity mismatch')
        server = self.server(opener)
        with self.assertRaisesRegex(ValueError, 'schema integrity mismatch'):
            server.start()
        self.assertIsNone(server._listener)
        server._close_failed_open.assert_called_once()
