#!/usr/bin/env python3
"""Internal OpenAI-compatible adapter for Freebuff/Codebuff free agents."""
import json, os, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PORT = int(os.environ.get("PORT", "8093"))
BASE = os.environ.get("FREEBUFF_URL", "https://www.codebuff.com/api/v1").rstrip("/")
TOKEN = os.environ.get("FREEBUFF_TOKEN", "")
USER_ID = os.environ.get("FREEBUFF_USER_ID", "")
UA = "ai-sdk/openai-compatible/0.0.0-test/codebuff"
AGENTS = {
    # Verified live on the current Freebuff account/session.
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
}

def request_json(path, body=None, extra=None):
    if not TOKEN:
        raise RuntimeError("FREEBUFF_TOKEN is not configured")
    headers = {"Authorization": "Bearer " + TOKEN, "User-Agent": UA}
    headers.update(extra or {})
    raw = None if body is None else json.dumps(body).encode()
    if raw is not None:
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=raw, headers=headers, method="POST")
    with urlopen(req, timeout=45) as r:
        return r.status, dict(r.headers), r.read()

def admission(model, instance):
    _, _, raw = request_json("/freebuff/session/admission", extra={
        "x-freebuff-acting-user-id": USER_ID, "x-freebuff-model": model,
        "x-freebuff-instance-id": instance, "x-freebuff-wallet-spend-limit": "0",
        "x-fb-timezone": "Asia/Ho_Chi_Minh"})
    return json.loads(raw).get("instanceId", instance)

def run_agent(agent):
    _, _, raw = request_json("/agent-runs", {"action": "START", "agentId": agent,
        "ancestorRunIds": []}, {"x-freebuff-acting-user-id": USER_ID})
    return json.loads(raw)["runId"]

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, format, *args):  # noqa: A002
        print(f"{self.command} {self.path} -> {format % args}", flush=True)
    def do_GET(self):
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404); return
        models = [{"id": model, "object": "model", "created": 0,
                   "owned_by": "freebuff"} for model in AGENTS]
        raw = json.dumps({"object": "list", "data": models}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404); return
        try:
            n = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(n))
            model = data.get("model", ""); agent = AGENTS.get(model)
            if not agent: raise ValueError("unsupported Freebuff model")
            admitted = admission(model, "inst_" + str(uuid.uuid4())); run_id = run_agent(agent)
            messages = data.get("messages") or []
            if not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
                messages = [{"role": "system", "content": "You are Buffy, the strategic coding assistant."}] + messages
            payload = dict(data); payload.update({"messages": messages, "stream": True,
                "codebuff_metadata": {"run_id": run_id, "client_id": str(uuid.uuid4()),
                "n": agent, "cost_mode": "free", "freebuff_instance_id": admitted}})
            status, headers, raw = request_json("/chat/completions", payload, {
                "Accept": "text/event-stream", "x-freebuff-acting-user-id": USER_ID,
                "x-freebuff-model": model})
            self.send_response(status); self.send_header("Content-Type", headers.get("Content-Type", "text/event-stream"))
            self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            print(f"FREEBUFF model={model} agent={agent} status={status} bytes={len(raw)}", flush=True)
        except HTTPError as e:
            body = e.read(500); self._json(e.code, {"error": {"message": "Freebuff upstream error", "status": e.code}})
            print(f"FREEBUFF upstream_status={e.code} body_bytes={len(body)}", flush=True)
        except Exception as e:
            self._json(502, {"error": {"message": str(e)}})
    def _json(self, code, obj):
        raw = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

if __name__ == "__main__":
    print(f"freebuff-inject on :{PORT} upstream={BASE} token_configured={bool(TOKEN)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
