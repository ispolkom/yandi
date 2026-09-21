#!/usr/bin/env python3
"""A stand-in Core for the supervisor's tests (standard library only).

It follows the real core's launch protocol (a 0600 secret file that is read once and removed, a port file) and the lifecycle answers of
contract 1.0-rc1, and it misbehaves on cue. Behaviour of launch N comes from the JSON file named by FAKE_CORE_SCRIPT:
    {"work_dir": "...", "launches": ["normal", "crash_after_ready", ...], "default": "normal", "rogue_port": 12345}
Everything it observes goes to <work_dir>/records.jsonl, which only the tests read.
"""
import base64, hashlib, http.server, json, os, signal, stat, sys, threading, time

script = json.load(open(os.environ["FAKE_CORE_SCRIPT"]))
work = script["work_dir"]


def record(**kw):
    with open(os.path.join(work, "records.jsonl"), "a") as fh:
        fh.write(json.dumps(kw) + "\n")


if len(sys.argv) > 1 and sys.argv[1] == "provision":
    key = sys.stdin.readline().strip()
    state = os.environ["YANDI_CORE_STATE_DIR"]
    os.makedirs(state, exist_ok=True)
    open(os.path.join(state, "check-value.json"), "w").write(json.dumps({"key_sha256": hashlib.sha256(key.encode()).hexdigest()}))
    record(event="provision", key_sha256=hashlib.sha256(key.encode()).hexdigest(), argv=sys.argv)
    sys.exit(0)

counter = os.path.join(work, "launches")
n = (int(open(counter).read()) if os.path.exists(counter) else 0) + 1
open(counter, "w").write(str(n))
launches = script.get("launches", [])
behavior = launches[n - 1] if n - 1 < len(launches) else script.get("default", "normal")

secret_path = os.environ["YANDI_CORE_SECRET_FILE"]
st = os.stat(secret_path, follow_symlinks=False)
dir_mode = stat.S_IMODE(os.stat(os.path.dirname(secret_path)).st_mode)
secret = open(secret_path).read().strip()
os.unlink(secret_path)
record(event="start", launch=n, behavior=behavior, pid=os.getpid(), argv=sys.argv, env=dict(os.environ), secret=secret,
       secret_file_mode=oct(stat.S_IMODE(st.st_mode)), dir_mode=oct(dir_mode), secret_file_removed=not os.path.exists(secret_path),
       cmdline=open("/proc/self/cmdline").read().split("\0"))

if behavior == "crash_at_start":
    sys.exit(3)
if behavior == "never_answers":
    while True:
        time.sleep(60)
if behavior in ("ignore_term", "ignore_shutdown_and_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

state = {"v": "ready" if behavior == "starts_ready" else "locked"}


def reply(handler, status, body):
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _err(self, status, code):
        reply(self, status, {"error": {"code": code, "message": "no", "retryable": False, "request_id": "req_fake"}})

    def do_GET(self):
        if self.path == "/v1/health":
            return reply(self, 200, {"state": state["v"]})
        if self.headers.get("Authorization") != "Bearer " + secret:
            return self._err(401, "unauthorized")
        if self.path == "/v1/capabilities":
            return reply(self, 200, {"contract": "1.0-rc1", "implementation": {"name": "fake", "version": "0"}, "features": [],
                                     "limits": {}, "storage_schema": 0, "egress_confinement": "none"})
        self._err(404, "not_found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != "Bearer " + secret:
            return self._err(401, "unauthorized")
        if not self.headers.get("Idempotency-Key"):
            return self._err(400, "invalid_payload")
        if self.path == "/v1/unlock":
            key = body.get("key", "")
            record(event="unlock", launch=n, key_sha256=hashlib.sha256(key.encode()).hexdigest(), key_b64=key, context=body.get("context"),
                   authorised=True)
            if behavior == "refuse_key":
                return self._err(403, "forbidden")
            state["v"] = "ready"
            reply(self, 200, {"state": "ready"})
            if behavior == "crash_after_ready":
                threading.Timer(0.3, lambda: os._exit(3)).start()
            return
        if self.path == "/v1/lock":
            state["v"] = "locked"
            return reply(self, 200, {"state": "locked"})
        if self.path == "/v1/shutdown":
            record(event="shutdown_request", launch=n, deadline_ms=body.get("deadline_ms"))
            if behavior in ("hang_on_shutdown", "ignore_shutdown_and_term"):
                return reply(self, 202, {"state": "draining"})       # says yes, does nothing
            state["v"] = "draining"
            reply(self, 202, {"state": "draining"})
            threading.Timer(0.1, lambda: os._exit(0)).start()
            return
        self._err(404, "not_found")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


host = "0.0.0.0" if behavior == "bind_all" else "127.0.0.1"
server = Server((host, 0), Handler)
port = server.server_address[1]
advertised = script["rogue_port"] if behavior == "rogue_port" else port
tmp = os.environ["YANDI_CORE_PORT_FILE"] + ".tmp"
open(tmp, "w").write(str(advertised))
os.replace(tmp, os.environ["YANDI_CORE_PORT_FILE"])
record(event="listening", launch=n, port=port, advertised=advertised)
signal.signal(signal.SIGTERM, signal.getsignal(signal.SIGTERM) if behavior in ("ignore_term", "ignore_shutdown_and_term") else (lambda *a: os._exit(0)))
server.serve_forever()
