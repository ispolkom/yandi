"""One HTTP exchange, with nothing hidden: raw request bytes out, raw status/headers/body in.

Uses only the standard library and a fresh connection per request (Connection: close), so a fixture sees exactly what a
different language's client would see. A refused or reset connection is a result, not an exception.
"""
from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# The ports of the LIVE system. The runner refuses to talk to any of them, whatever the fixture or the operator says.
LIVE_PORTS = {
    9010: "PET web (live YANDI)",
    9999: "node web",
    10000: "node data",
    9000: "node discovery",
    8080: "node http proxy",
    9111: "node mobile",
    9112: "node mobile",
    18082: "node AI-RPC",
    18083: "python intelligence bridge",
    6379: "Redis",
    3306: "MySQL",
    11434: "Ollama",
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class RefusedTarget(Exception):
    """The runner will not talk to this address."""


def check_target_url(base_url: str) -> tuple[str, int]:
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme != "http" or host not in LOOPBACK or parts.port is None:
        raise RefusedTarget(f"{base_url}: only http://127.0.0.1:<port> style loopback targets are allowed")
    if parts.port in LIVE_PORTS:
        raise RefusedTarget(f"{base_url}: port {parts.port} is the live system ({LIVE_PORTS[parts.port]}); the contract suite never touches it")
    return host, parts.port


@dataclass
class Response:
    status: int | None = None
    headers: dict = field(default_factory=dict)      # lower-cased name -> value (last wins)
    body: bytes = b""
    error: str | None = None                          # 'connection_refused' | 'connection_reset' | 'timeout' | 'protocol'

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        """Parse the body as JSON; duplicate keys, NaN and invalid UTF-8 make it unparseable (I10 applies to answers too)."""
        def no_dupes(pairs):
            keys = [k for k, _ in pairs]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate key in response")
            return dict(pairs)

        def no_const(name):
            raise ValueError(f"non-JSON constant {name}")
        return json.loads(self.body.decode("utf-8"), object_pairs_hook=no_dupes, parse_constant=no_const)


def send(base_url: str, method: str, path: str, headers: dict, body: bytes | None, timeout: float = 15.0) -> Response:
    host, port = check_target_url(base_url)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    resp = Response()
    try:
        try:
            conn.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.putheader("Connection", "close")
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
        except (BrokenPipeError, ConnectionResetError):
            # The server may answer and close before it has read a large body; the answer is still there to read.
            pass
        r = conn.getresponse()
        resp.status = r.status
        resp.headers = {k.lower(): v for k, v in r.getheaders()}
        resp.body = r.read()
    except ConnectionRefusedError:
        resp.error = "connection_refused"
    except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected):
        resp.error = "connection_reset"
    except (socket.timeout, TimeoutError):
        resp.error = "timeout"
    except (http.client.HTTPException, OSError):
        resp.error = "protocol"
    finally:
        conn.close()
    return resp
