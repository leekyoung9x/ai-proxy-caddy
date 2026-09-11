"""xq-inject: body injector for XQAPI (https://xqapi.com).

Sits between internal clients (9Router, curl) and XQAPI. For POSTs with a
JSON body, looks at the body's `model` field and injects a `routing` object
(route lock) from the model -> routing map. Proxy ALWAYS overwrites
client-sent routing; unknown models are rejected (fail closed).

Map comes from MODEL_ROUTES_JSON env (route id KHÔNG hardcode — resolve động
từ public API của XQAPI vì route xoay theo giờ):
  {"deepseek-flash": {"strategy": "auto", "failover": "auto"},
   "glm-5.3-flash": {"strategy": "auto", "failover": false}}
Mỗi entry có thể thêm "route" tĩnh làm fallback khi API chết và chưa có cache.

Run:  python3 inject.py  (env PORT, UPSTREAM, MODEL_ROUTES_JSON, ROUTE_TTL_S)
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
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
ROUTE_TTL_S = int(os.environ.get("ROUTE_TTL_S", "300"))

# Cache route động: model -> (listed_at_epoch, [(ratio, latency, routeId, name)])
_ROUTE_CACHE = {}

# Khung GIẢM GIÁ 50% (giờ VN, UTC+7): failover BẬT. Ngoài khung → khóa cứng.
# Discount = T2-T6: 07:00-08:00, 11:00-13:00, 17:00-07:00(hôm sau); T7+CN: cả ngày.
VN_TZ = timezone(timedelta(hours=7))


def is_discount(now=None):
    now = now or datetime.now(VN_TZ)
    if now.weekday() >= 5:  # Sat, Sun
        return True
    h = now.hour + now.minute / 60
    return (7 <= h < 8) or (11 <= h < 13) or (h >= 17) or (h < 7)
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


def fetch_model_routes(model):
    """GET public route list của model từ XQAPI. Trả [(ratio, lat, routeId, name)]."""
    from urllib.parse import quote
    conn = HTTPSConnection(_UP_HOST, _UP.port or 443, timeout=10)
    conn.request("GET", f"/api/public/models/{quote(model, safe='')}",
                 headers={"Host": _UP_HOST, "Accept": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    out = []
    for r in data.get("routes", []):
        if r.get("available") is not True:
            continue
        healthy = 0 if r.get("healthStatus") == "healthy" else 1
        out.append((healthy,
                    r.get("officialPriceRatio", 999),
                    r.get("averageLatencyMs", 999999),
                    r.get("routeId"), r.get("name")))
    out.sort(key=lambda t: (t[0], t[1], t[2]))
    return [(t[3], t[4]) for t in out]  # [(routeId, name)] rẻ + khỏe trước


def resolve_route(model, cfg):
    """Route rẻ nhất còn sống cho model. Trả (routeId, name) hoặc (None, None)."""
    import time
    now = time.time()
    hit = _ROUTE_CACHE.get(model)
    if hit and now - hit[0] < ROUTE_TTL_S:
        routes = hit[1]
    else:
        try:
            routes = fetch_model_routes(model)
            _ROUTE_CACHE[model] = (now, routes)
        except Exception as e:
            print(f"ROUTE FETCH ERR {model}: {e}", flush=True)
            routes = hit[1] if hit else []
    if routes:
        return routes[0]
    static = cfg.get("route")  # fallback tĩnh khi API chết + chưa có cache
    return (static, "static-fallback") if static else (None, None)


def maybe_inject(body):
    """Hard-lock mapped models to their XQAPI route. Proxy ALWAYS wins over
    client-sent routing. Returns (new_body, route_id, block_error):
      - (body, None, None): passthrough (no body / not JSON / no model field)
      - (None, None, err): FAIL CLOSED, unknown model, do not forward
      - (new_body, route, None): routing overwritten with locked values
    """
    if not body:
        return body, None, None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, None, None
    if not isinstance(data, dict) or not data.get("model"):
        return body, None, None
    route = MODEL_ROUTES.get(data["model"])
    if not route:
        return None, None, f"No locked route configured for model: {data['model']}"
    route_id, route_name = resolve_route(data["model"], route)
    if not route_id:
        return None, None, f"No live route for model: {data['model']}"
    failover = route.get("failover", True)
    if failover == "auto":
        failover = is_discount()  # chỉ fallback trong khung giảm giá
    # Proxy luôn overwrite routing của client.
    data["routing"] = {"failover": bool(failover),
                       "route": route_id,
                       "strategy": route.get("strategy", "auto")}
    return json.dumps(data, separators=(",", ":")).encode(), \
        f"{route_id} ({route_name})", None


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
            body, injected, blocked = maybe_inject(body)
            if blocked:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                payload = json.dumps({"error": blocked}).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                print(f"BLOCKED {self.path}: {blocked}", flush=True)
                return

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

        if body and injected:
            try:
                dbg = json.loads(body)
                print(f"FINAL model={dbg.get('model')} routing={dbg.get('routing')}",
                      flush=True)
            except Exception:
                pass
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
