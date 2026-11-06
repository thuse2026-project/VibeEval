import unittest
from unittest.mock import Mock, patch

from app.sandbox.runtime import SandboxOperationFailed, SandboxRuntime


class RuntimeTests(unittest.TestCase):
    def test_timeout_terminates_worker(self):
        context, reader, writer, worker = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (reader, writer)
        context.Process.return_value = worker
        reader.poll.return_value = False
        worker.is_alive.side_effect = [True, False]
        with patch(
            "app.sandbox.runtime.multiprocessing.get_context", return_value=context
        ):
            with self.assertRaises(TimeoutError):
                SandboxRuntime(backend_ref="test").delete_sandbox(Mock())
        worker.terminate.assert_called_once()
        reader.close.assert_called_once()
        writer.close.assert_called_once()

    def test_safe_error_propagates(self):
        context, reader, writer, worker = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (reader, writer)
        context.Process.return_value = worker
        reader.poll.return_value = True
        reader.recv.return_value = (False, "SANDBOX_REQUEST_TIMEOUT")
        worker.is_alive.return_value = False
        with patch(
            "app.sandbox.runtime.multiprocessing.get_context", return_value=context
        ):
            with self.assertRaisesRegex(
                SandboxOperationFailed, "SANDBOX_REQUEST_TIMEOUT"
            ):
                SandboxRuntime(backend_ref="test").freeze_and_export(Mock())

    def test_reject_remote_daemon(self):
        with self.assertRaises(ValueError):
            SandboxRuntime(backend_ref="test", docker_url="tcp://example:2375")
