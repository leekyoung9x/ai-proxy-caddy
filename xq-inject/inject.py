"""xq-inject: body injector for XQAPI (https://xqapi.com).

Sits between internal clients (9Router, curl) and XQAPI. For POSTs with a
JSON body, looks at the body's `model` field and injects a default `routing`
object (route lock) from the model -> routing map, unless the client already
sent its own `routing` (client wins). Models not in the map pass through
untouched (like the old /generic path).

Map comes from MODEL_ROUTES_JSON env:
  {"deepseek-v4-flash": {"route": "route-405", "strategy": "auto", "failover": true}, ...}

Run:  python3 inject.py  (env PORT, UPSTREAM, MODEL_ROUTES_JSON)
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPSConnection
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8091"))
UPSTREAM = os.environ.get("UPSTREAM", "https://xqapi.com")
try:
    MODEL_ROUTES = json.loads(os.environ.get("MODEL_ROUTES_JSON", "{}"))
except json.JSONDecodeError as e:
    print(f"BAD MODEL_ROUTES_JSON: {e}", flush=True)
    MODEL_ROUTES = {}

_UP = urlparse(UPSTREAM)
_UP_HOST = _UP.hostname or "xqapi.com"
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}


def read_body(handler):
    """Read full request body, de-chunking if needed. Returns bytes."""
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks = []
        rf = handler.rfile
        while True:
            line = rf.readline().strip()
            if not line:
                break
            size = int(line.split(b";")[0], 16)
            if size == 0:
                rf.readline()
                break
            chunks.append(rf.read(size))
            rf.readline()
        return b"".join(chunks)
    length = handler.headers.get("Content-Length")
    if length:
        return handler.rfile.read(int(length))
    return b""


def maybe_inject(body):
    """Inject default routing based on body's model field.
    Returns (new_body, injected_route_or_None)."""
    if not body:
        return body, None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, None
    if not isinstance(data, dict):
        return body, None
    if "routing" in data and isinstance(data["routing"], dict):
        return body, None  # client already pins routing; respect it
    route = MODEL_ROUTES.get(data.get("model", ""))
    if not route:
        return body, None  # unknown model: pass through untouched
    data["routing"] = {"failover": route.get("failover", True),
                       "route": route["route"],
                       "strategy": route.get("strategy", "auto")}
    return json.dumps(data, separators=(",", ":")).encode(), route["route"]


class Handler(BaseHTTPRequestHandler):
    server_version = "xq-inject/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        sys.stdout.write(f"{self.address_string()[0]} {self.command} {self.path} "
                         f"-> {format % args}\n")
        sys.stdout.flush()

    def _proxy(self):
        body = read_body(self) if self.command in ("POST", "PUT", "PATCH") else b""
        injected = None
        ctype = self.headers.get("Content-Type", "")
        if body and "application/json" in ctype:
            body, injected = maybe_inject(body)

        conn = HTTPSConnection(_UP_HOST, _UP.port or 443, timeout=120)
        fwd = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP_HEADERS or kl == "x-xqapi-prefix":
                continue
            fwd[k] = v
        fwd["Host"] = _UP_HOST
        if body:
            fwd["Content-Length"] = str(len(body))
        else:
            fwd.pop("Content-Length", None)

        try:
            conn.request(self.command, self.path, body=body or None, headers=fwd)
            resp = conn.getresponse()
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "26")
            self.end_headers()
            self.wfile.write(b'{"error":"upstream unreachable"}')
            print(f"UPSTREAM ERR {self.path}: {e}", flush=True)
            return

        self.send_response(resp.status)
        rlen = None
        for k, v in resp.getheaders():
            if k.lower() in HOP_HEADERS:
                continue
            self.send_header(k, v)
            if k.lower() == "content-length":
                rlen = v
        # Force close downstream so no hanging on keep-alive mismatch.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
        conn.close()
        if injected:
            print(f"INJECTED routing={injected} {self.path}", flush=True)

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_OPTIONS = _proxy


if __name__ == "__main__":
    print(f"xq-inject on :{PORT} -> {UPSTREAM} models={list(MODEL_ROUTES)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
