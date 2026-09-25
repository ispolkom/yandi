#!/usr/bin/env python3
"""Подделка llama-server для тестов родного движка: те же аргументы командной строки и те же пути (/health, /v1/chat/completions, /v1/embeddings)."""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default

model = opt("-m")
port = int(opt("--port"))
embedding = "--embedding" in args
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"pid": os.getpid(), "args": args}, ensure_ascii=False) + "\n")
if "crash" in model:
    sys.exit(3)
start = time.time()
delay = 0.4 if "loading" in model else 0.0
never_ready = "never" in model

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health":
            ok = (time.time() - start) >= delay and not never_ready
            self._send(200 if ok else 503, {"status": "ok" if ok else "loading model"})
        else:
            self._send(404, {})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/v1/chat/completions" and not embedding:
            if "http500" in model:
                return self._send(500, {"error": "boom"})
            self._send(200, {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}, "finish_reason": "length" if body.get("max_tokens") == 1 else "stop"}], "usage": {"completion_tokens": 7}})
        elif self.path == "/v1/embeddings" and embedding:
            inp = body["input"]
            inp = inp if isinstance(inp, list) else [inp]
            data = [{"index": i, "embedding": [float(len(t)), 0.5, float(i)]} for i, t in enumerate(inp)]
            self._send(200, {"data": list(reversed(data))})
        else:
            self._send(404, {"error": "no such endpoint in this mode"})

HTTPServer(("127.0.0.1", port), H).serve_forever()
