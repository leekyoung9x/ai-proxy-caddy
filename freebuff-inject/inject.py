#!/usr/bin/env python3
"""Internal OpenAI-compatible adapter for Freebuff/Codebuff free agents.

Freebuff không có model giá 0. Thay vào đó tài khoản free được cấp một pool
**25 Freebucks/ngày** (reset 00:00 giờ Saigon = 17:00 UTC), mỗi model trừ theo
giá/giờ. Giá lấy động từ GET /freebuff/session → freebucks.prices.

Danh sách model được phép dùng ở tier "limited" do server công bố, đọc từ lỗi
409 session_model_mismatch:
    "Limited free access is only available with GLM 5.3 Flash or
     DeepSeek V4.1 Flash or MiMo 2.5 or Solar Pro 4."
Các model khác trong bảng giá (luna, gemini, kimi-k3-eco, muse-spark...) chỉ
mở cho tier trả phí — gọi vào sẽ bị 409 session_model_mismatch.

Đã verify sống: z-ai/glm-5.3-flash → HTTP 200, trả text thật.
3 model còn lại server tuyên bố free nhưng bị chặn bởi quota ngày lúc verify
(HTTP 429 rate_limited) — không phải model lock.

Bẫy đã gặp: session/admission trả 409 model_locked khi đang có session cũ giữ
slot model khác. Phải GET /freebuff/session để lấy instanceId rồi DELETE trước
khi xin admission cho model mới. Adapter tự làm việc này.
"""
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PORT = int(os.environ.get("PORT", "8093"))
BASE = os.environ.get("FREEBUFF_URL", "https://www.codebuff.com/api/v1").rstrip("/")
TOKEN = os.environ.get("FREEBUFF_TOKEN", "")
USER_ID = os.environ.get("FREEBUFF_USER_ID", "")
TZ = os.environ.get("FREEBUFF_TZ", "Asia/Ho_Chi_Minh")
UA = "ai-sdk/openai-compatible/0.0.0-test/codebuff"

# model id -> agent id. 4 model duy nhất tier limited được phép.
# Giá Freebucks/giờ (từ server): xem FREE_BUCKS_PER_HOUR.
AGENTS = {
    "z-ai/glm-5.3-flash": "base2-free-glm-5-3-flash",
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
    "mimo/mimo-v2.5": "base2-free-mimo",
    "upstage/solar-pro4": "base2-free-solar-pro4",
}

# Alias ngắn cho client gọi cho tiện (9Router, curl tay). Ánh xạ về model id.
ALIASES = {
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "glm-5.3": "z-ai/glm-5.3-flash",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4.1-flash": "deepseek/deepseek-v4-flash",
    "ds-v4.1-flash": "deepseek/deepseek-v4-flash",
    "mimo-v2.5": "mimo/mimo-v2.5",
    "solar-pro4": "upstage/solar-pro4",
    "solar-pro-4": "upstage/solar-pro4",
}

# Giá tham chiếu (Freebucks/giờ) — chỉ để log/ước tính, không dùng để chặn.
FREE_BUCKS_PER_HOUR = {
    "z-ai/glm-5.3-flash": 5,
    "deepseek/deepseek-v4-flash": 15,   # off-peak (22:00–06:00 UTC) = 10
    "mimo/mimo-v2.5": 10,
    "upstage/solar-pro4": 10,
}


def resolve_model(name):
    if name in AGENTS:
        return name
    return ALIASES.get(name)


def request_json(path, method="POST", body=None, extra=None, timeout=60):
    if not TOKEN:
        raise RuntimeError("FREEBUFF_TOKEN chưa cấu hình")
    headers = {"Authorization": "Bearer " + TOKEN, "User-Agent": UA}
    headers.update(extra or {})
    raw = None if body is None else json.dumps(body).encode()
    if raw is not None:
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=raw, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def release_slot():
    """Nhả slot đang giữ để admission model khác không dính 409 model_locked."""
    try:
        _, _, raw = request_json("/freebuff/session", method="GET",
                                 extra={"x-freebuff-acting-user-id": USER_ID},
                                 timeout=20)
        inst = json.loads(raw).get("instanceId")
        if inst:
            request_json("/freebuff/session", method="DELETE",
                         extra={"x-freebuff-acting-user-id": USER_ID,
                                "x-freebuff-instance-id": inst}, timeout=20)
            print(f"FREEBUFF released instance={inst}", flush=True)
    except Exception as e:
        print(f"FREEBUFF release_slot skipped: {e}", flush=True)


def daily_status():
    try:
        _, _, raw = request_json("/freebuff/session", method="GET",
                                 extra={"x-freebuff-acting-user-id": USER_ID},
                                 timeout=20)
        fb = json.loads(raw).get("freebucks") or {}
        return fb.get("daily")
    except Exception:
        return None


def admission(model, instance):
    _, _, raw = request_json("/freebuff/session/admission", extra={
        "x-freebuff-acting-user-id": USER_ID, "x-freebuff-model": model,
        "x-freebuff-instance-id": instance,
        "x-freebuff-wallet-spend-limit": "0",
        "x-fb-timezone": TZ}, timeout=45)
    return json.loads(raw).get("instanceId", instance)


def run_agent(agent):
    _, _, raw = request_json("/agent-runs",
                             body={"action": "START", "agentId": agent,
                                   "ancestorRunIds": []},
                             extra={"x-freebuff-acting-user-id": USER_ID},
                             timeout=45)
    return json.loads(raw)["runId"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        print(f"{self.command} {self.path} -> {format % args}", flush=True)

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/v1/models":
            data = [{"id": m, "object": "model", "created": 0,
                     "owned_by": "freebuff",
                     "freebucks_per_hour": FREE_BUCKS_PER_HOUR.get(m)}
                    for m in AGENTS]
            self._json(200, {"object": "list", "data": data})
            return
        if path == "/v1/freebucks":
            # Tiện cho client tự biết còn bao nhiêu quota ngày.
            self._json(200, {"daily": daily_status()})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        if not TOKEN:
            self._json(500, {"error": {"message": "FREEBUFF_TOKEN chưa cấu hình"}})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            model = resolve_model(data.get("model", ""))
            if not model:
                self._json(400, {"error": {
                    "message": f"model '{data.get('model')}' không hỗ trợ",
                    "supported": sorted(AGENTS)}})
                return

            # Session cũ giữ slot model khác → 409 model_locked. Nhả trước.
            release_slot()
            agent = AGENTS[model]
            inst = "inst_" + str(uuid.uuid4())
            inst = admission(model, inst)
            run_id = run_agent(agent)

            messages = data.get("messages") or []
            if not any(isinstance(m, dict) and m.get("role") == "system"
                       for m in messages):
                messages = [{"role": "system",
                             "content": "You are Buffy, the strategic coding assistant."}] + messages

            payload = {k: v for k, v in data.items() if k != "codebuff_metadata"}
            payload.update({
                "model": model,
                "messages": messages,
                "stream": True,
                "codebuff_metadata": {
                    "run_id": run_id, "client_id": str(uuid.uuid4()),
                    "n": agent, "cost_mode": "free",
                    "freebuff_instance_id": inst}})

            status, headers, raw = request_json(
                "/chat/completions", body=payload,
                extra={"Accept": "text/event-stream",
                       "x-freebuff-acting-user-id": USER_ID,
                       "x-freebuff-model": model}, timeout=300)

            self.send_response(status)
            self.send_header("Content-Type",
                             headers.get("Content-Type", "text/event-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            print(f"FREEBUFF model={model} agent={agent} status={status} "
                  f"bytes={len(raw)}", flush=True)

        except HTTPError as e:
            body = e.read()
            try:
                j = json.loads(body)
            except Exception:
                j = {"message": body[:200].decode(errors="replace")}
            print(f"FREEBUFF upstream={e.code} {j.get('error') or j.get('status')}",
                  flush=True)
            self._json(e.code, {"error": {
                "message": j.get("message") or j.get("error") or "Freebuff error",
                "upstream_status": e.code, "detail": j}})
        except Exception as e:
            self._json(502, {"error": {"message": str(e)}})

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    print(f"freebuff-inject on :{PORT} upstream={BASE} "
          f"token_configured={bool(TOKEN)} models={sorted(AGENTS)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
