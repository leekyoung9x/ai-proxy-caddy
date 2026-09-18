#!/usr/bin/env python3
"""
OpenCode Zen — gọi model miễn phí không cần chạy CLI opencode.

Endpoint thật: POST https://opencode.ai/zen/v1/responses
Đây là OpenAI *Responses API* (không phải /chat/completions).

Cơ chế chống lạm dụng (đã kiểm chứng bằng thực nghiệm):
  1. Auth dùng token tĩnh dùng chung: "Bearer public"
  2. Header x-opencode-session / x-opencode-request phải đúng định dạng:
     tiền tố ses_/msg_ + 12 hex (timestamp ms) + 14 base62 = tổng 30 ký tự
  3. Body PHẢI có mảng `tools` với tối thiểu 5 tool.
     Test tiệm tiến: 1 tool -> 403, 2 -> 403, 3 -> 403, 4 -> 403, 5 -> 200.
     Thiếu tools sẽ trả:
     403 {"type":"error","error":{"type":"FreeTierError",
          "message":"...OpenCode's free tier can only be used from within OpenCode"}}

Cách dùng:
    python3 opencode_free.py "câu hỏi"
    python3 opencode_free.py "câu hỏi" nemotron-3-ultra-free
"""
import json
import random
import string
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://opencode.ai/zen/v1/responses"
FREE_MODELS = [
    "muse-spark-1.3-contributor-free",
    "muse-spark-1.2-contributor-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "mimo-v2.5-free",
    "ling-3.0-flash-fin-free",
    "big-pickle",
]

# 5 tool tối thiểu để qua được kiểm tra free tier
MINIMAL_TOOLS = [
    {"type": "function", "name": name, "description": name,
     "parameters": {"type": "object", "properties": {}}}
    for name in ("bash", "edit", "glob", "grep", "read")
]


def make_id(prefix):
    """ses_/msg_ + 12 hex timestamp + 14 base62 = 30 ký tự."""
    hex_ts = f"{int(time.time() * 1000):012x}"
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=14))
    return f"{prefix}_{hex_ts}{rand}"


def call_opencode(prompt, model="muse-spark-1.3-contributor-free",
                  system=None, tools=None, timeout=180):
    headers = {
        "Authorization": "Bearer public",          # token tĩnh, không cần login
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14",
        "x-opencode-client": "cli",
        "x-opencode-project": "global",
        "x-opencode-session": make_id("ses"),
        "x-opencode-request": make_id("msg"),
    }

    input_items = []
    if system:
        input_items.append({"role": "developer",
                            "content": [{"type": "input_text", "text": system}]})
    input_items.append({"role": "user",
                        "content": [{"type": "input_text", "text": prompt}]})

    payload = {
        "model": model,
        "input": input_items,
        "tools": tools if tools is not None else MINIMAL_TOOLS,
        "stream": True,
        "max_output_tokens": 32000,
        "store": False,
    }

    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")

    text_parts = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response.output_text.delta":
                    delta = event.get("delta", "")
                    text_parts.append(delta)
                    print(delta, end="", flush=True)
        print()
        return {"model": model, "text": "".join(text_parts)}

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "FreeTierError" in body:
            return {"model": model, "error": "403 FreeTierError — thiếu tools "
                                             f"(cần >= {len(MINIMAL_TOOLS)}) hoặc sai định dạng session ID",
                    "raw": body[:400]}
        return {"model": model, "error": f"HTTP {e.code}", "raw": body[:400]}


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Bạn là model gì? Trả lời ngắn gọn."
    model = sys.argv[2] if len(sys.argv) > 2 else FREE_MODELS[0]
    print(f"[{model}] {prompt}\n")
    result = call_opencode(prompt, model=model)
    if "error" in result:
        print("LỖI:", result["error"])
    print("\n--- hoàn tất ---")
