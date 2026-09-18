#!/usr/bin/env python3
"""
Freebuff (Codebuff) — gọi model miễn phí không cần chạy CLI freebuff.

Khác với OpenCode, Freebuff KHÔNG có token dùng chung: cần `authToken` cá nhân
lấy qua `freebuff login`, lưu tại ~/.config/manicode/credentials.json
(khoá `default.authToken`).

Flow 3 bước BẮT BUỘC (thiếu bước nào cũng lỗi):
  1. POST /api/v1/freebuff/session/admission  -> instanceId (hạn ~6 giờ)
     Một tài khoản chỉ được 1 active session / model. Đổi model phải
     DELETE /api/v1/freebuff/session trước, nếu không sẽ gặp 409 model_locked.
  2. POST /api/v1/agent-runs  {"action":"START","agentId":<agent>}  -> runId
  3. POST /api/v1/chat/completions  -> SSE stream
     Body cần object `codebuff_metadata` (run_id, client_id, n, cost_mode:"free",
     freebuff_instance_id). System prompt PHẢI chứa câu
     "You are Buffy, the strategic coding assistant."

Cách dùng:
    python3 freebuff_free.py                       # test model mặc định
    python3 freebuff_free.py "câu hỏi"
    python3 freebuff_free.py "câu hỏi" openai/gpt-5.6-luna
"""
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "https://www.codebuff.com/api/v1"
CREDENTIALS = Path("~/.config/manicode/credentials.json").expanduser()
USER_AGENT = "ai-sdk/openai-compatible/0.0.0-test/codebuff"
SYSTEM_PROMPT = "You are Buffy, the strategic coding assistant."

# model -> agentId tương ứng (sai mapping -> 403 free_mode_invalid_agent_model)
AGENTS = {
    # 4 model duy nhất Freebuff cho dùng ở tier "limited".
    # Server tự công bố trong lỗi 409 session_model_mismatch:
    #   "Limited free access is only available with GLM 5.3 Flash or
    #    DeepSeek V4.1 Flash or MiMo 2.5 or Solar Pro 4."
    # Freebuff KHONG co model gia 0: tai khoan free co 25 Freebucks/ngay.
    "z-ai/glm-5.3-flash": "base2-free-glm-5-3-flash",      # 5 Freebucks/h
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",  # 15/h (off-peak 10)
    "mimo/mimo-v2.5": "base2-free-mimo",                    # 10/h
    "upstage/solar-pro4": "base2-free-solar-pro4",           # 10/h
}
DEFAULT_MODEL = "z-ai/glm-5.3-flash"   # re nhat (5 Freebucks/h) -> test duoc nhieu nhat


class FreebuffClient:
    def __init__(self, credentials_path=CREDENTIALS):
        path = Path(credentials_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"Không tìm thấy {path}. Chạy 'freebuff login' trước.")
        self.token = json.loads(path.read_text())["default"]["authToken"]
        self.user_id = self._get_user_id()

    def _post(self, url, body=None, extra_headers=None, timeout=60):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
            "x-freebuff-acting-user-id": self.user_id,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method="POST")
        return urllib.request.urlopen(req, timeout=timeout)

    def _get_user_id(self):
        req = urllib.request.Request(
            f"{BASE}/me?fields=id",
            headers={"Authorization": f"Bearer {self.token}",
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())["id"]

    def release_slot(self):
        """Nhả session đang giữ.

        Freebuff chỉ cho 1 session active. Đang giữ session model khác rồi xin
        admission model mới -> 409 {"status":"model_locked"}. Phải GET lấy
        instanceId rồi DELETE kèm header x-freebuff-instance-id trước.
        """
        req = urllib.request.Request(
            f"{BASE}/freebuff/session",
            headers={"Authorization": f"Bearer {self.token}",
                     "User-Agent": USER_AGENT,
                     "x-freebuff-acting-user-id": self.user_id})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                inst = json.loads(r.read().decode()).get("instanceId")
        except Exception:
            return None
        if not inst:
            return None
        dreq = urllib.request.Request(
            f"{BASE}/freebuff/session",
            headers={"Authorization": f"Bearer {self.token}",
                     "User-Agent": USER_AGENT,
                     "x-freebuff-acting-user-id": self.user_id,
                     "x-freebuff-instance-id": inst},
            method="DELETE")
        try:
            with urllib.request.urlopen(dreq, timeout=20) as r:
                r.read()
            print(f"  (đã nhả session cũ {inst})")
        except Exception:
            pass
        return inst

    def quota(self):
        """Xem Freebucks còn lại trong ngày."""
        req = urllib.request.Request(
            f"{BASE}/freebuff/session",
            headers={"Authorization": f"Bearer {self.token}",
                     "User-Agent": USER_AGENT,
                     "x-freebuff-acting-user-id": self.user_id})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode()).get("freebucks", {}).get("daily")
        except Exception:
            return None

    def admit(self, model):
        """Bước 1: đăng ký phiên, trả instanceId."""
        instance_id = f"inst_{uuid.uuid4()}"
        headers = {
            "x-freebuff-model": model,
            "x-freebuff-instance-id": instance_id,
            "x-freebuff-wallet-spend-limit": "0",
            "x-fb-timezone": "Asia/Ho_Chi_Minh",
        }
        with self._post(f"{BASE}/freebuff/session/admission",
                        body={}, extra_headers=headers, timeout=30) as r:
            data = json.loads(r.read().decode())
        return data.get("instanceId", instance_id), data.get("status")

    def start_run(self, agent_id):
        """Bước 2: tạo agent run, trả runId."""
        body = {"action": "START", "agentId": agent_id, "ancestorRunIds": []}
        with self._post(f"{BASE}/agent-runs", body=body, timeout=30) as r:
            return json.loads(r.read().decode())["runId"]

    def chat(self, prompt, model=DEFAULT_MODEL, system=SYSTEM_PROMPT):
        """Bước 3: gọi chat completion, stream SSE."""
        agent_id = AGENTS.get(model)
        if not agent_id:
            return {"error": f"Model '{model}' chưa có trong AGENTS. "
                             f"Chọn một trong: {list(AGENTS)}"}

        try:
            # Nhả session cũ trước, tránh 409 model_locked khi đổi model.
            self.release_slot()
            instance_id, status = self.admit(model)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429 or "rate_limited" in body:
                q = self.quota() or {}
                return {"error": f"429 rate_limited — hết Freebucks ngày. "
                                 f"reset lúc {q.get('resetAt')} ({q.get('resetTimeZone')})",
                        "daily": q, "raw": body[:300]}
            if e.code == 409 or "model_locked" in body:
                return {"error": "409 model_locked — tài khoản đang có session active "
                                 "cho model khác. Đã thử nhả slot nhưng chưa được.",
                        "raw": body[:300]}
            return {"error": f"HTTP {e.code} khi admission", "raw": body[:300]}

        try:
            run_id = self.start_run(agent_id)
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code} khi agent-runs",
                    "raw": e.read().decode(errors="replace")[:300]}

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "codebuff_metadata": {
                "run_id": run_id,
                "client_id": str(uuid.uuid4()),
                "n": agent_id,
                "cost_mode": "free",
                "freebuff_instance_id": instance_id,
            },
        }
        headers = {
            "x-freebuff-model": model,
            "Accept": "text/event-stream",
        }

        parts = []
        try:
            with self._post(f"{BASE}/chat/completions", body=payload,
                            extra_headers=headers, timeout=240) as r:
                for raw_line in r:
                    line = raw_line.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                        delta = ev["choices"][0]["delta"].get("content", "")
                        if delta:
                            parts.append(delta)
                            print(delta, end="", flush=True)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
            print()
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code} khi chat/completions",
                    "raw": e.read().decode(errors="replace")[:300]}

        return {"model": model, "agent": agent_id, "run_id": run_id,
                "instance_id": instance_id, "admission_status": status,
                "text": "".join(parts)}


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Giải thích ngắn gọn QuickSort."
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    print(f"[{model}] {prompt}\n")
    result = FreebuffClient().chat(prompt, model=model)
    if "error" in result:
        print("LỖI:", result["error"])
        if result.get("raw"):
            print("RAW:", result["raw"])
    print("\n--- hoàn tất ---")
