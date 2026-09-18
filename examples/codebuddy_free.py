#!/usr/bin/env python3
"""
CodeBuddy / WorkBuddy (Tencent) — gọi model miễn phí không cần dùng CLI.

Model free đã kiểm chứng:
  - hy3                   : x0.00 credits, free vĩnh viễn
  - deepseek-v4.1-flash   : x0.00 credits, free vĩnh viễn, context 1M, multimodal
  - hy4-preview-f         : credit = 0, trial 14 ngày (kể từ lần dùng đầu)

Điều kiện bắt buộc:
  1. Message đầu tiên trong `messages` PHẢI là role "system"
  2. Header `X-User-Id` phải khớp với token
  3. Cần đăng nhập trước bằng CLI `codebuddy` -> /login (device flow)

Cách dùng:
    python3 codebuddy_free.py                    # test cả 3 model free
    python3 codebuddy_free.py "câu hỏi" hy3      # hỏi 1 model cụ thể
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ENDPOINT = "https://www.codebuddy.ai/v2/chat/completions"
ACCOUNT_ENDPOINT = "https://www.codebuddy.ai/v2/plugin/account"
CONFIG_ENDPOINT = "https://www.codebuddy.ai/v3/config"

TOKEN_PATH = Path("~/.codebuddy/auth_token.json").expanduser()

FREE_MODELS = ["hy3", "deepseek-v4.1-flash", "hy4-preview-f"]
SYSTEM_PROMPT = "You are CodeBuddy Code."

# Header bắt buộc khi gọi /v3/config (endpoint catalog model)
CONFIG_HEADERS_EXTRA = {
    "X-Product": "TencentCloud",
    "X-Domain": "www.codebuddy.ai",
    "User-Agent": "CLI/2.154.0 CodeBuddy/2.154.0",
}


class CodeBuddyClient:
    def __init__(self, token_path=TOKEN_PATH):
        path = Path(token_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"Không tìm thấy {path}. Chạy 'codebuddy' và dùng /login trước."
            )
        self.token = json.loads(path.read_text())["accessToken"]
        self.user_id = self._fetch_user_id()

    def _curl(self, url, method="GET", body=None, stream=False, extra_headers=None):
        cmd = ["curl", "-s"]
        if stream:
            cmd += ["-N", "--max-time", "240"]
        cmd += ["-X", method, url, "-H", f"Authorization: Bearer {self.token}"]
        if extra_headers:
            for k, v in extra_headers.items():
                cmd += ["-H", f"{k}: {v}"]
        if body is not None:
            cmd += ["-H", "Content-Type: application/json"]
        if stream:
            cmd += ["-H", "Accept: text/event-stream"]
        if body is not None:
            cmd += ["-d", json.dumps(body)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.stdout

    def _fetch_user_id(self):
        raw = self._curl(ACCOUNT_ENDPOINT)
        return json.loads(raw)["data"]["uid"]

    def list_models(self):
        """Lấy catalog model + giá credits từ server (không phải hardcode)."""
        raw = self._curl(CONFIG_ENDPOINT, extra_headers=CONFIG_HEADERS_EXTRA)
        models = json.loads(raw)["data"]["models"]
        return {m["id"]: m.get("credits", "?") for m in models}

    def chat(self, prompt, model="hy3", system=SYSTEM_PROMPT):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},  # BẮT BUỘC đứng đầu
                {"role": "user", "content": prompt},
            ],
            "stream": True,
        }
        raw = self._curl(ENDPOINT, method="POST", body=body, stream=True)

        # Lỗi trả về dạng JSON thay vì SSE
        if '"code":' in raw and "data:" not in raw:
            try:
                return {"error": json.loads(raw)}
            except json.JSONDecodeError:
                return {"error": raw[:300]}

        text = "".join(re.findall(r'"content":"([^"]*)"', raw))
        charged = re.findall(r'"credit":([0-9.]+)', raw)
        tokens = re.findall(r'"total_tokens":(\d+)', raw)
        return {
            "model": model,
            "text": text,
            "credit_charged": charged[-1] if charged else None,
            "total_tokens": tokens[-1] if tokens else None,
        }


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Viết 150 từ giải thích HTTP/2 khác HTTP/1.1 thế nào."
    models = [sys.argv[2]] if len(sys.argv) > 2 else FREE_MODELS

    cb = CodeBuddyClient()
    print(f"User ID: {cb.user_id}\n")

    print("=== Catalog model (từ /v3/config) ===")
    for mid, credits in cb.list_models().items():
        flag = "  <-- FREE" if credits == "x0.00" else ""
        print(f"  {mid:32} {credits}{flag}")
    print()

    print(f"=== Gọi chat: {prompt[:60]}... ===")
    for m in models:
        r = cb.chat(prompt, model=m)
        if "error" in r:
            print(f"[{m}] LỖI: {r['error']}")
            continue
        print(f"[{m}] credit_charged={r['credit_charged']}  tokens={r['total_tokens']}")
        print(f"  {r['text'][:200]}...\n")


if __name__ == "__main__":
    main()
