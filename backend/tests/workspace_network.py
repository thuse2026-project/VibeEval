"""Loopback-only real Uvicorn/WebSocket client for opt-in acceptance tests."""

import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn
from starlette.websockets import WebSocketDisconnect


class NetworkClient(httpx.Client):
    @contextmanager
    def websocket_connect(self, path, headers):
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.sync.client import connect

        cookie = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        try:
            with connect(
                str(self.base_url).replace("http://", "ws://").rstrip("/") + path,
                origin=headers["origin"],
                additional_headers={"Cookie": cookie},
                proxy=None,
                open_timeout=10,
                close_timeout=3,
            ) as connection:

                class Channel:
                    def send_json(self, value):
                        connection.send(json.dumps(value))

                    def receive_json(self):
                        try:
                            return json.loads(connection.recv(timeout=15))
                        except ConnectionClosed as error:
                            raise WebSocketDisconnect(error.code) from None

                yield Channel()
        except InvalidStatus:
            raise WebSocketDisconnect(1008) from None


@contextmanager
def live_client(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            ws="websockets",
            ws_max_size=16384,
            ws_max_queue=4,
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        end = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= end:
                raise RuntimeError("local validation server failed to start")
            time.sleep(0.01)
        with NetworkClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"X-VibeEval-Request": "1"},
            timeout=60,
            trust_env=False,
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("local validation server did not stop")
