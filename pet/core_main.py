"""Start the Python Core in *core mode*: a separate process, listening on 127.0.0.1 only, locked until the Node unlocks it.

    python -m pet.core_main provision          # once, out of band: reads the base64 derived key on stdin, writes the check value
    python -m pet.core_main                    # the real application (PET) behind the lifecycle boundary
    python -m pet.core_main --shell            # the lifecycle boundary with a tiny stand-in application (fast, for the conformance runs)

Environment (all set by whoever launches the core; the Node, or a developer by hand):
    YANDI_CORE_SECRET_FILE   path of a 0600 file holding this launch's secret (32 random bytes, hex). Required. It is read once
                             and the file is removed; the secret is never taken from a command line or from the environment.
    YANDI_CORE_STATE_DIR     where the check value lives (default ~/.local/share/yandi/core)
    YANDI_CORE_PORT_FILE     optional: the chosen port is written here (0600). `--port 0` (the default) lets the system choose.

A core started by hand behaves exactly like one started by the Node: it waits, locked, for a valid `POST /v1/unlock`.
The ordinary `start.sh` / `python -m pet.council_chat_server` start is untouched: it does not use any of this.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import math
import os
import socket
import sys
import tempfile
from pathlib import Path

from pet.core_lifecycle import (ENV_PORT_FILE, ENV_SECRET_FILE, ENV_STATE_DIR, CoreBoundary, Lifecycle, LaunchSecretError,
                                default_state_dir, in_test_mode, load_launch_secret, provision_state)

HOST = "127.0.0.1"                         # not configurable on purpose: the core has exactly one caller, on this machine


def _fail(message: str, code: int = 2) -> "None":
    print(f"[core] cannot start: {message}", file=sys.stderr)
    raise SystemExit(code)


def _state_dir() -> Path:
    return Path(os.environ[ENV_STATE_DIR]) if os.environ.get(ENV_STATE_DIR) else default_state_dir()


def cmd_provision() -> int:
    """The Node's first-run step: the key comes on stdin, never on a command line."""
    data = sys.stdin.readline().strip()
    try:
        key = base64.b64decode(data, validate=True)
    except ValueError:
        _fail("the key on stdin is not base64")
    if len(key) != 32:
        _fail("the key on stdin is not 32 bytes")
    provision_state(_state_dir(), key)
    print("[core] check value written")
    return 0


def _stand_in_app():
    """What stands behind the boundary in --shell mode: nothing but a proof that the gate opens and closes."""
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "http":
            body = b'{"legacy":"alive"}' if scope["path"] == "/legacy/ping" else b'{"detail":"Not Found"}'
            await send({"type": "http.response.start", "status": 200 if scope["path"] == "/legacy/ping" else 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
    return app


def _pet_app():
    """The real application. Its start-up steps (they write to the database and to the system-state store) must not run while
    the core is locked, so they are taken off the application and handed to the lifecycle, which runs them once after the first
    successful unlock. Nothing in them is changed."""
    from pet.council_chat_server import app
    hooks = list(app.router.on_startup)
    app.router.on_startup.clear()
    return app, hooks


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.set_inheritable(False)
    return sock


def _write_private(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".port-")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_serve(shell: bool, port: int) -> int:
    import uvicorn

    secret_path = os.environ.get(ENV_SECRET_FILE)
    if not secret_path:
        _fail(f"{ENV_SECRET_FILE} is not set")
    try:
        secret = load_launch_secret(secret_path)
    except LaunchSecretError as exc:
        _fail(str(exc))
    try:
        os.unlink(secret_path)                 # one launch, one secret: nothing the application starts later can read it from disk
    except OSError:
        pass
    os.environ.pop(ENV_SECRET_FILE, None)

    if shell:
        inner, hooks = _stand_in_app(), []
    else:
        inner, hooks = _pet_app()
    sock = _bind(port)
    chosen = sock.getsockname()[1]
    if os.environ.get(ENV_PORT_FILE):
        _write_private(os.environ[ENV_PORT_FILE], str(chosen))

    holder: dict = {}
    lifecycle = Lifecycle(secret, _state_dir(), on_ready=hooks, test_mode=in_test_mode(),
                          request_stop=lambda remaining: _stop(holder["server"], remaining))
    config = uvicorn.Config(CoreBoundary(inner, lifecycle), log_level="warning", access_log=False, server_header=False,
                            date_header=False, lifespan="on")
    server = uvicorn.Server(config)
    holder["server"] = server
    print(f"[core] listening on {HOST}:{chosen}, locked until unlocked")
    asyncio.run(server.serve(sockets=[sock]))
    return 0


def _stop(server, remaining: float) -> None:
    server.config.timeout_graceful_shutdown = max(1, math.ceil(remaining))
    server.should_exit = True


def build_parser() -> argparse.ArgumentParser:
    """No option carries a secret, a key or a bind address: those come from a 0600 file, stdin, and the code itself."""
    parser = argparse.ArgumentParser(prog="python -m pet.core_main", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["serve", "provision"], default="serve")
    parser.add_argument("--shell", action="store_true", help="a tiny stand-in application instead of PET (fast conformance runs)")
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 (default) lets the system choose")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "provision":
        return cmd_provision()
    return cmd_serve(args.shell, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
