"""Hermes Unix-socket notification client."""

from __future__ import annotations

import json
import logging
import socket
from uuid import uuid4


class HermesNotifier:
    def __init__(self, socket_path: str, targets: tuple[str, ...]) -> None:
        self.socket_path = socket_path
        self.targets = targets

    def send(self, message: str) -> None:
        for target in self.targets:
            request = {"jsonrpc": "2.0", "id": uuid4().hex, "method": "submit",
                       "params": {"target": target, "message": message, "media_path": None}}
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(self.socket_path)
                    client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
                    response = b""
                    while not response.endswith(b"\n"):
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        response += chunk
                result = json.loads(response)
                if "error" in result:
                    raise RuntimeError(str(result["error"]))
            except Exception as exc:
                logging.warning("Hermes notification failed target=%s: %s", target, exc)


class LoggingNotifier:
    def send(self, message: str) -> None:
        logging.info("notification: %s", message.replace("\n", " | "))
