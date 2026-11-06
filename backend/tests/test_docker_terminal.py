import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.sandbox.bound_adapter import BoundSandboxAdapter
from app.sandbox.terminal import DockerTerminal, DockerTerminalFactory


class DockerTerminalTests(unittest.TestCase):
    def test_socket_timeout_input_resize_and_close(self):
        client, stream = Mock(), Mock()
        terminal = DockerTerminal(client, stream, "exec-id")
        stream._sock.recv.side_effect = [socket.timeout(), b"x", b""]
        self.assertIsNone(terminal.read())
        self.assertEqual(terminal.read(), b"x")
        self.assertEqual(terminal.read(), b"")
        terminal.write(b"\x03")
        stream._sock.sendall.assert_called_once_with(b"\x03")
        terminal.resize(100, 30)
        client.api.exec_resize.assert_called_once_with("exec-id", height=30, width=100)
        terminal.close()
        stream._response.close.assert_called_once()
        stream.close.assert_called_once()
        client.close.assert_called_once()

    def test_open_uses_bound_container_nonroot_and_no_shell_interpolation(self):
        client = Mock()
        client.api.exec_create.return_value = {"Id": "exec-id"}
        obj = SimpleNamespace(id="original", status="running")
        with (
            patch("docker.DockerClient", return_value=client),
            patch(
                "app.sandbox.terminal.DockerSubmissionBridge._container",
                return_value=obj,
            ),
        ):
            terminal = DockerTerminalFactory(
                ("unix:///var/run/docker.sock", "node", 20)
            ).open(SimpleNamespace(status="in_progress"))
        args, kwargs = client.api.exec_create.call_args
        self.assertEqual(args[0], "original")
        self.assertEqual(kwargs["user"], "1000:1000")
        self.assertEqual(kwargs["workdir"], "/workspace")
        self.assertTrue(kwargs["tty"])
        terminal.close()

    def test_paused_container_cannot_open_and_client_is_closed(self):
        client = Mock()
        with (
            patch("docker.DockerClient", return_value=client),
            patch(
                "app.sandbox.terminal.DockerSubmissionBridge._container",
                return_value=SimpleNamespace(status="paused"),
            ),
            self.assertRaises(ValueError),
        ):
            DockerTerminalFactory(("unix:///var/run/docker.sock", "node", 20)).open(
                SimpleNamespace(status="in_progress")
            )
        client.api.exec_create.assert_not_called()
        client.close.assert_called_once()

    def test_failed_start_closes_client(self):
        client = Mock()
        client.api.exec_create.return_value = {"Id": "id"}
        client.api.exec_start.side_effect = RuntimeError("failed")
        with (
            patch("docker.DockerClient", return_value=client),
            patch(
                "app.sandbox.terminal.DockerSubmissionBridge._container",
                return_value=SimpleNamespace(id="original", status="running"),
            ),
            self.assertRaises(RuntimeError),
        ):
            DockerTerminalFactory(("unix:///var/run/docker.sock", "node", 20)).open(
                SimpleNamespace(status="in_progress")
            )
        client.close.assert_called_once()

    def test_read_rejects_path_before_docker(self):
        client = Mock()
        with self.assertRaises(ValueError):
            BoundSandboxAdapter(client, "node").read_file(
                SimpleNamespace(status="in_progress"), "../secret"
            )
        client.containers.get.assert_not_called()
