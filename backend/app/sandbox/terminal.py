"""Docker exec PTY; never execute candidate commands on the API host."""

import socket

from .submission_bridge import DockerSubmissionBridge


class DockerTerminal:
    def __init__(self, client, stream, exec_id):
        self.client = client
        self.stream = stream
        self.socket = getattr(stream, "_sock", stream)
        self.socket.settimeout(0.5)
        self.exec_id = exec_id

    def read(self):
        try:
            return self.socket.recv(8192)
        except (socket.timeout, BlockingIOError):
            return None

    def write(self, data):
        self.socket.sendall(data)

    def resize(self, cols, rows):
        self.client.api.exec_resize(self.exec_id, height=rows, width=cols)

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            try:
                response = getattr(self.stream, "_response", None)
                if response is not None:
                    response.close()
            finally:
                self.stream.close()
                self.client.close()


class DockerTerminalFactory:
    def __init__(self, settings):
        self.settings = settings

    def open(self, view):
        import docker

        client = docker.DockerClient(
            base_url=self.settings[0], timeout=self.settings[2]
        )
        stream = None
        try:
            obj = DockerSubmissionBridge(client, self.settings[1])._container(view)
            if obj.status != "running" or view.status != "in_progress":
                raise ValueError("active sandbox required")
            created = client.api.exec_create(
                obj.id,
                ["/bin/bash", "--noprofile", "--norc", "-i"],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                user="1000:1000",
                workdir="/workspace",
                environment={"TERM": "xterm-256color", "HISTFILE": "/dev/null"},
            )
            stream = client.api.exec_start(created["Id"], tty=True, socket=True)
            terminal = DockerTerminal(client, stream, created["Id"])
            terminal.resize(120, 32)
            return terminal
        except BaseException:
            if stream is not None:
                response = getattr(stream, "_response", None)
                if response is not None:
                    response.close()
                stream.close()
            client.close()
            raise
