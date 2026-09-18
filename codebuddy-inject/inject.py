#!/usr/bin/env python3
"""Internal OpenAI-compatible adapter cho CodeBuddy / WorkBuddy (Tencent).

Khác biệt so với upstream nằm ở 3 điểm, phát hiện qua trace:

1. Message đầu tiên trong `messages` PHẢI là role "system".
   Thiếu → `{"code":11128,"msg":"first message is not system prompt"}`.
2. Header `X-User-Id` KHÔNG bắt buộc cho /v2/chat/completions.
   Đã test: thiếu / rỗng / sai UUID đều trả HTTP 200. Chỉ Authorization mới cần.
   (Adapter vẫn gửi nếu có cấu hình, để khớp hành vi CLI gốc.)
3. SSE stream trả về field `credit` trong block `usage` cuối
   (= số credit đã tiêu thụ). Model free trả `credit: 0`.

Model free đã verify trên tài khoản hiện tại (credit = 0):
  - hy3                  : Hunyuan 3 Thinking, có tag `craft`
  - deepseek-v4.1-flash  : context 1M, native multimodal
  - hy4-preview-f        : bản trial 14 ngày của hy4-preview

CẢNH BÁO về hy4-preview-f: backend chỉ chấp nhận ID này trong 14 ngày kể từ
lần dùng đầu tiên (key `hy4.first_user_time` trong local_storage của CLI).
Sau đó sẽ bị trừ credit như `hy4-preview` thường (0.29). KHÔNG đưa model này
vào /v1/models công bố cho 9Router — tránh hiện model ảo rồi 403.

Suffix `-f` KHÔNG áp dụng cho 20 model còn lại: đã quét toàn bộ, tất cả trả
`{"code":11102,"msg":"model [<id>-f] service info not found"}`.

Token: đọc từ env CODEBUDDY_TOKEN (đặt trong .env của host, không commit).
Cách lấy token: chạy CLI `codebuddy` → `/login` (device flow), token lưu tại
`~/.codebuddy/auth_token.json` với expiresIn ~365 ngày nên không cần refresh.
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPSConnection
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8094"))
UPSTREAM = os.environ.get("UPSTREAM", "https://www.codebuddy.ai")
TOKEN = os.environ.get("CODEBUDDY_TOKEN", "")
USER_ID = os.environ.get("CODEBUDDY_USER_ID", "")
SYSTEM_PROMPT = os.environ.get("CODEBUDDY_SYSTEM", "You are CodeBuddy Code.")

# Model công bố ra /v1/models cho 9Router. Chỉ những model đã verify sống.
# hy4-preview-f để NGOÀI mặc định: có hạn trial 14 ngày, thêm vào sẽ khiến
# client thấy model ảo sau khi hết hạn. Bật bằng CODEBUDDY_FREE_ONLY=0 nếu cần.
MODELS = [m.strip() for m in os.environ.get(
    "CODEBUDDY_MODELS", "hy3,deepseek-v4.1-flash").split(",") if m.strip()]

# Model chỉ dùng để test nội bộ, không công bố.
TRIAL_MODELS = {"hy4-preview-f"}

_UP = urlparse(UPSTREAM)
_UP_HOST = _UP.hostname or "www.codebuddy.ai"
_UP_TLS = _UP.scheme != "http"
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}


def upstream_post(path, payload, extra_headers=None):
    """Gọi upstream, trả (status, headers, body bytes)."""
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "X-User-Id": USER_ID,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    headers.update(extra_headers or {})
    conn = HTTPSConnection(_UP_HOST, timeout=300) if _UP_TLS else None
    if conn is None:
        raise RuntimeError("UPSTREAM phải là https")
    try:
        conn.request("POST", path, body=json.dumps(payload).encode(), headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        print(f"{self.command} {self.path} -> {format % args}", flush=True)

    def do_GET(self):
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404)
            return
        data = [{"id": m, "object": "model", "created": 0, "owned_by": "codebuddy"}
                for m in MODELS]
        self._send(200, "application/json", json.dumps({"object": "list", "data": data}).encode())

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        if not TOKEN:
            self._send(500, "application/json",
                       json.dumps({"error": {"message": "CODEBUDDY_TOKEN chưa cấu hình"}}).encode())
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            model = data.get("model", "")

            if model not in MODELS and model not in TRIAL_MODELS:
                self._send(400, "application/json", json.dumps({
                    "error": {"message": f"model '{model}' không được hỗ trợ",
                              "supported": MODELS}}).encode())
                return

            messages = data.get("messages") or []
            # Bắt buộc: message đầu là system. Client OpenAI thường không gửi
            # system đầu tiên → tự chèn để tránh 11128.
            if not (messages and isinstance(messages[0], dict)
                    and messages[0].get("role") == "system"):
                messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

            payload = {"model": model, "messages": messages, "stream": True}
            # Giữ các tham số sampling nếu client có gửi
            for key in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
                if key in data:
                    payload[key] = data[key]

            status, headers, body = upstream_post("/v2/chat/completions", payload)

            # Log mức credit tiêu thụ (model free → 0)
            try:
                import re
                credits = re.findall(rb'"credit":([0-9.]+)', body)
                if credits:
                    print(f"CODEBUDDY model={model} status={status} "
                          f"credit={credits[-1].decode()} bytes={len(body)}", flush=True)
                else:
                    print(f"CODEBUDDY model={model} status={status} bytes={len(body)}", flush=True)
            except Exception:
                pass

            self._send(status, headers.get("Content-Type", "text/event-stream"), body)

        except Exception as e:
            msg = str(e)
            hint = ""
            if "11128" in msg or "first message is not system" in msg:
                hint = " — message đầu phải là role system"
            print(f"CODEBUDDY error: {msg}{hint}", flush=True)
            self._send(502, "application/json",
                       json.dumps({"error": {"message": msg}}).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if not TOKEN:
        print("CẢNH BÁO: CODEBUDDY_TOKEN trống — mọi request sẽ trả 500.", file=sys.stderr)
    print(f"codebuddy-inject on :{PORT} upstream={UPSTREAM} "
          f"token_configured={bool(TOKEN)} user_id_configured={bool(USER_ID)} "
          f"models={MODELS}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
