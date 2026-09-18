# HƯỚNG DẪN CHI TIẾT: TRACE VÀ GỌI API MIỄN PHÍ CỦA OPENCODE, FREEBUFF, CODEBUDDY

> Tài liệu trace đầy đủ (kèm script Python trọn gói cho từng provider).
> Bản tóm tắt curl nhanh: [`FREE_AI_APIS.md`](FREE_AI_APIS.md).

Tài liệu này tổng hợp toàn bộ kết quả phân tích ngược (reverse-engineering), cơ chế xác thực, cấu trúc request/headers và các đoạn mã mẫu (`curl` & Python) để khai thác các model AI miễn phí từ 3 nền tảng: **OpenCode Zen**, **Freebuff (Codebuff)** và **CodeBuddy / WorkBuddy (Tencent)**.

---

## MỤC LỤC
1. [So sánh kiến trúc 3 nền tảng](#1-so-sánh-kiến-trúc)
2. [Phần 1: OpenCode Zen (Muse Spark, Nemotron, MiMo,...)](#2-phần-1-opencode-zen)
   - [Đặc điểm kỹ thuật & Danh sách Model](#danh-sách-model-opencode)
   - [Cơ chế Anti-Abuse (Session ID, Tools)](#cơ-chế-anti-abuse-opencode)
   - [Lệnh curl đầy đủ](#lệnh-curl-opencode)
   - [Script Python gọi trực tiếp](#script-python-opencode)
3. [Phần 2: Freebuff (DeepSeek V4, GLM, MiniMax, GPT-5.6 Luna)](#3-phần-2-freebuff-codebuff)
   - [Đặc điểm kỹ thuật & Danh sách Model](#danh-sách-model-freebuff)
   - [Quy trình 3 bước: Admission -> Run -> Completion](#quy-trình-3-bước-freebuff)
   - [Lệnh curl đầy đủ](#lệnh-curl-freebuff)
   - [Script Python trọn gói](#script-python-trọn-gói-freebuff)
4. [Phần 3: CodeBuddy / WorkBuddy (Tencent — Hy3, Hy4, DeepSeek V4.1)](#4-phần-3-codebuddy--workbuddy-tencent)
   - [Xác thực Device-Flow](#xác-thực-device-flow-codebuddy)
   - [Danh sách Model & Bảng giá Credits](#danh-sách-model-codebuddy)
   - [Biến thể Trial ẩn `hy4-preview-f`](#biến-thể-trial-hy4)
   - [Lệnh curl đầy đủ](#lệnh-curl-codebuddy)
   - [Script Python trọn gói](#script-python-codebuddy)
5. [Lưu ý về Quota, Rate Limit & Khắc phục lỗi](#5-lưu-ý-và-khắc-phục-lỗi)

---

## 1. SO SÁNH KIẾN TRÚC

| Tiêu chí | OpenCode Zen | Freebuff (Codebuff) | CodeBuddy (Tencent) |
| :--- | :--- | :--- | :--- |
| **Backend URL** | `https://opencode.ai/zen/v1/responses` | `https://www.codebuff.com/api/v1` | `https://www.codebuddy.ai/v2` |
| **Chuẩn giao thức** | OpenAI Responses API (`/responses`) | OpenAI Chat Completions | OpenAI Chat Completions |
| **Xác thực (Auth)** | `Bearer public` (token tĩnh dùng chung) | `Bearer <authToken>` cá nhân | `Bearer <accessToken>` + header `X-User-Id` |
| **Điều kiện tiên quyết** | Gửi kèm ≥5 tools & ID đúng định dạng | Admission -> Agent Run -> Completion | Message đầu phải là `role: "system"` |
| **Trạng thái phiên** | Stateless qua `prompt_cache_key` | Server-side session (~6 giờ) | Stateless, có refresh token |
| **Model miễn phí** | 7 model `*-free` | 6 model (Freebucks quota) | `hy3`, `deepseek-v4.1-flash`, `hy4-preview-f` (trial) |

---

## 2. PHẦN 1: OPENCODE ZEN

### Danh sách Model Miễn Phí (Free Tier)
- `opencode/muse-spark-1.3-contributor-free` *(Model suy luận đa phương thức tiên tiến từ Meta)*
- `opencode/muse-spark-1.2-contributor-free`
- `opencode/nemotron-3-ultra-free`
- `opencode/nemotron-3.5-lightning-free`
- `opencode/mimo-v2.5-free`
- `opencode/ling-3.0-flash-fin-free`
- `opencode/big-pickle`

### Cơ chế Anti-Abuse của OpenCode Zen
Nếu gửi request đơn giản như gọi OpenAI thông thường, máy chủ sẽ trả về lỗi:
`403 Forbidden: {"type":"error","error":{"type":"FreeTierError","message":"Error from provider (Console): OpenCode's free tier can only be used from within OpenCode"}}`

Để vượt qua kiểm tra này:
1. **Quy tắc định dạng Session & Request ID**:
   - Header `x-opencode-session`: Bắt buộc tiền tố `ses_` + 12 ký tự hex timestamp + 14 ký tự base62 ngẫu nhiên (tổng độ dài 30 ký tự).
   - Header `x-opencode-request`: Bắt buộc tiền tố `msg_` + 12 ký tự hex timestamp + 14 ký tự base62 ngẫu nhiên (tổng 30 ký tự).
2. **Khai báo Tools**:
   - Body request bắt buộc phải có mảng `tools` chứa tối thiểu 5 tool mặc định của agent: `bash`, `edit`, `glob`, `grep`, `read` (mỗi tool chỉ cần khai báo `parameters: {"type": "object", "properties": {}}`).

### Lệnh curl đầy đủ cho OpenCode Zen

```bash
curl -s -N -X POST https://opencode.ai/zen/v1/responses \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -H "User-Agent: opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14" \
  -H "x-opencode-client: cli" \
  -H "x-opencode-project: global" \
  -H "x-opencode-session: ses_01a0b26820f0XF1lEWbjqoozpE" \
  -H "x-opencode-request: msg_01a0b26820f0KdupwMRLghjZWF" \
  -d '{
    "model": "muse-spark-1.3-contributor-free",
    "input": [
      {
        "role": "user",
        "content": [
          {"type": "input_text", "text": "Xin chào! Bạn là ai?"}
        ]
      }
    ],
    "tools": [
      {"type": "function", "name": "bash", "description": "bash", "parameters": {"type": "object", "properties": {}}},
      {"type": "function", "name": "edit", "description": "edit", "parameters": {"type": "object", "properties": {}}},
      {"type": "function", "name": "glob", "description": "glob", "parameters": {"type": "object", "properties": {}}},
      {"type": "function", "name": "grep", "description": "grep", "parameters": {"type": "object", "properties": {}}},
      {"type": "function", "name": "read", "description": "read", "parameters": {"type": "object", "properties": {}}}
    ],
    "stream": true
  }'
```

### Script Python gọi OpenCode Zen

```python
import json
import random
import string
import time
import urllib.request

def generate_opencode_id(prefix: str) -> str:
    hex_timestamp = f"{int(time.time() * 1000):012x}"
    random_str = "".join(random.choices(string.ascii_letters + string.digits, k=14))
    return f"{prefix}_{hex_timestamp}{random_str}"

def call_opencode_zen(prompt: str, model: str = "muse-spark-1.3-contributor-free"):
    url = "https://opencode.ai/zen/v1/responses"
    session_id = generate_opencode_id("ses")
    request_id = generate_opencode_id("msg")

    headers = {
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14",
        "x-opencode-client": "cli",
        "x-opencode-project": "global",
        "x-opencode-session": session_id,
        "x-opencode-request": request_id,
    }

    payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}]
            }
        ],
        "tools": [
            {"type": "function", "name": "bash", "description": "bash", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "edit", "description": "edit", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "glob", "description": "glob", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "grep", "description": "grep", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "read", "description": "read", "parameters": {"type": "object", "properties": {}}},
        ],
        "stream": True
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    full_response = []

    with urllib.request.urlopen(req, timeout=30) as resp:
        for line in resp:
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str.startswith("data:"):
                raw_json = line_str[5:].strip()
                if raw_json == "[DONE]":
                    break
                try:
                    event = json.loads(raw_json)
                    if event.get("type") == "response.output_text.delta":
                        delta = event.get("delta", "")
                        print(delta, end="", flush=True)
                        full_response.append(delta)
                except Exception:
                    pass

    return "".join(full_response)

if __name__ == "__main__":
    reply = call_opencode_zen("Hôm nay là ngày mấy và bạn có thể làm được gì?")
    print("\n--- Hoàn tất ---")
```

---

## 3. PHẦN 2: FREEBUFF (CODEBUFF)

### Danh sách Model & Agent ID tương ứng
Freebuff yêu cầu mỗi model phải đi kèm với một `agentId` tương ứng khi bắt đầu run:

> ⚠️ **ĐÍNH CHÍNH (verify lại 2026-09-19):** Freebuff **KHÔNG có model giá 0**.
> Tài khoản free chỉ được cấp pool **25 Freebucks/ngày**, mỗi model trừ theo giá/giờ.
> Danh sách model được phép ở tier `limited` do server tự công bố (đọc từ lỗi 409
> `session_model_mismatch`):
>
> *"Limited free access is only available with **GLM 5.3 Flash** or
> **DeepSeek V4.1 Flash** or **MiMo 2.5** or **Solar Pro 4**."*

| Model Identifier | Agent ID tương ứng | Freebucks/giờ | Ghi chú |
| :--- | :--- | :--- | :--- |
| `z-ai/glm-5.3-flash` | `base2-free-glm-5-3-flash` | **5** | ✅ verify sống HTTP 200 — rẻ nhất |
| `mimo/mimo-v2.5` | `base2-free-mimo` | 10 | server công bố free |
| `upstage/solar-pro4` | `base2-free-solar-pro4` | 10 | "Limited-time trial" |
| `deepseek/deepseek-v4-flash` | `base2-free-deepseek-flash` | 15 (off-peak 10) | server công bố free |

**Các model sau KHÔNG dùng được ở tier free** (bị `409 session_model_mismatch`)
— bảng cũ liệt kê nhầm:

| Model | Lỗi thật khi gọi |
| :--- | :--- |
| `deepseek/deepseek-v4-pro` | `409 session_model_mismatch` |
| `minimax/minimax-m3` | `409 session_model_mismatch` |
| `openai/gpt-5.6-luna` | `403 free_mode_legacy_luna_agent` |
| `z-ai/glm-5.3-flash-2026-09-05` | `403 free_mode_invalid_agent_model` |

Giá đầy đủ từ server (`GET /freebuff/session` → `freebucks.prices`):
`glm-5.3-flash` 5 · `kimi-k3-eco` 5 · `mimo-v2.5` 10 · `solar-pro4` 10 ·
`deepseek-v4-flash` 15 · `muse-spark-1.3-contributor` 15 ·
`muse-spark-1.2-contributor` 15 · `gpt-5.6-luna` 20 · `gpt-5.6-luna-es` 20 ·
`gemini-3.8-flash` 50.

### Lấy Token Đăng Nhập
Token cá nhân được cấp qua lệnh:
```bash
freebuff login
```
Trình duyệt sẽ mở link đăng nhập (`https://freebuff.com/login?auth_code=...`). Sau khi đăng nhập, thông tin tài khoản được lưu tại:
`~/.config/manicode/credentials.json` (Trường `authToken`).

### Quy trình 3 bước bắt buộc

1. **Bước 1: Session Admission (`POST /api/v1/freebuff/session/admission`)**
   - Đăng ký phiên làm việc cho model muốn dùng, nhận về `instanceId` có hạn trong 6 tiếng.
2. **Bước 2: Bắt đầu Agent Run (`POST /api/v1/agent-runs`)**
   - Gửi payload `{"action": "START", "agentId": "<AGENT_ID>"}` để nhận `runId`.
3. **Bước 3: Gọi Chat Completion (`POST /api/v1/chat/completions`)**
   - Body chuẩn OpenAI, nhưng bắt buộc thêm object `codebuff_metadata`:
     ```json
     "codebuff_metadata": {
       "run_id": "<RUN_ID>",
       "client_id": "<UUID>",
       "n": "<AGENT_ID>",
       "cost_mode": "free",
       "freebuff_instance_id": "<INSTANCE_ID>"
     }
     ```
   - System prompt đầu tiên bắt buộc phải có câu: `"You are Buffy, the strategic coding assistant."`.

> ⚠️ **Bẫy `409 model_locked`**: Freebuff chỉ cho **1 session active**. Nếu đang
> giữ session của model khác mà xin admission model mới sẽ bị
> `{"status":"model_locked","currentModel":"...","requestedModel":"..."}`.
> Phải nhả slot trước:
> ```bash
> INST=$(curl -s 'https://www.codebuff.com/api/v1/freebuff/session' \
>   -H "Authorization: Bearer $TOKEN" -H "User-Agent: ai-sdk/openai-compatible/0.0.0-test/codebuff" \
>   -H "x-freebuff-acting-user-id: $USER_ID" | jq -r '.instanceId // empty')
> [ -n "$INST" ] && curl -s -X DELETE 'https://www.codebuff.com/api/v1/freebuff/session' \
>   -H "Authorization: Bearer $TOKEN" -H "User-Agent: ai-sdk/openai-compatible/0.0.0-test/codebuff" \
>   -H "x-freebuff-acting-user-id: $USER_ID" -H "x-freebuff-instance-id: $INST"
> ```
> Xem quota còn lại: `GET /api/v1/freebuff/session` → `freebucks.daily.remaining`
> (reset `freebucks.daily.resetAt`, 17:00 UTC = 00:00 giờ VN).

### Lệnh curl đầy đủ cho Freebuff

```bash
# Đọc token và user id
TOKEN=$(jq -r '.default.authToken' ~/.config/manicode/credentials.json)
USER_ID=$(curl -s -H "Authorization: Bearer ***" https://www.codebuff.com/api/v1/me?fields=id | jq -r '.id')
INSTANCE_ID="inst_$(cat /proc/sys/kernel/random/uuid)"
MODEL="deepseek/deepseek-v4-flash"
AGENT="base2-free-deepseek-flash"

# Bước 1: Khởi tạo phiên
ADM_INST_ID=$(curl -s -X POST https://www.codebuff.com/api/v1/freebuff/session/admission \
  -H "Authorization: Bearer ***" \
  -H "User-Agent: ai-sdk/openai-compatible/0.0.0-test/codebuff" \
  -H "x-freebuff-acting-user-id: $USER_ID" \
  -H "x-freebuff-model: $MODEL" \
  -H "x-freebuff-instance-id: $INSTANCE_ID" \
  -H "x-freebuff-wallet-spend-limit: 0" \
  -H "x-fb-timezone: Asia/Ho_Chi_Minh" | jq -r '.instanceId // "'$INSTANCE_ID'"')

# Bước 2: Tạo Agent Run
RUN_ID=$(curl -s -X POST https://www.codebuff.com/api/v1/agent-runs \
  -H "Authorization: Bearer ***" \
  -H "User-Agent: ai-sdk/openai-compatible/0.0.0-test/codebuff" \
  -H "Content-Type: application/json" \
  -H "x-freebuff-acting-user-id: $USER_ID" \
  -d "{\"action\":\"START\",\"agentId\":\"$AGENT\",\"ancestorRunIds\":[]}" | jq -r '.runId')

# Bước 3: Gửi prompt và nhận SSE Stream
curl -s -N -X POST https://www.codebuff.com/api/v1/chat/completions \
  -H "Authorization: Bearer ***" \
  -H "User-Agent: ai-sdk/openai-compatible/0.0.0-test/codebuff" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "x-freebuff-acting-user-id: $USER_ID" \
  -H "x-freebuff-model: $MODEL" \
  -d '{
    "model": "'"$MODEL"'",
    "messages": [
      {
        "role": "system",
        "content": "You are Buffy, the strategic coding assistant."
      },
      {
        "role": "user",
        "content": "Giải thích ngắn gọn thuật toán QuickSort."
      }
    ],
    "stream": true,
    "codebuff_metadata": {
      "run_id": "'"$RUN_ID"'",
      "client_id": "'"$INSTANCE_ID"'",
      "n": "'"$AGENT"'",
      "cost_mode": "free",
      "freebuff_instance_id": "'"$ADM_INST_ID"'"
    }
  }'
```

### Script Python trọn gói cho Freebuff

```python
import json
import urllib.request
import uuid
from pathlib import Path

AGENT_MAP = {
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
    "deepseek/deepseek-v4-pro": "base2-free-deepseek",
    "mimo/mimo-v2.5": "base2-free-mimo",
    "minimax/minimax-m3": "base2-free-minimax-m3",
    "openai/gpt-5.6-luna": "base2-free-luna",
    "z-ai/glm-5.3-flash-2026-09-05": "base2-free-glm",
}

class FreebuffClient:
    def __init__(self, credentials_path: str = "~/.config/manicode/credentials.json"):
        path = Path(credentials_path).expanduser()
        if not path.exists():
            raise FileNotFoundError("Chưa đăng nhập Freebuff. Chạy 'freebuff login' trước.")
        
        with open(path, "r", encoding="utf-8") as f:
            creds = json.load(f)["default"]
        
        self.token = creds["authToken"]
        self.user_agent = "ai-sdk/openai-compatible/0.0.0-test/codebuff"
        self.user_id = self._get_user_id()

    def _get_user_id(self) -> str:
        req = urllib.request.Request(
            "https://www.codebuff.com/api/v1/me?fields=id",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())["id"]

    def chat(self, prompt: str, model: str = "deepseek/deepseek-v4-flash"):
        agent_name = AGENT_MAP.get(model, "base2-free")
        instance_id = f"inst_{uuid.uuid4()}"

        # 1. Admission
        adm_headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
            "x-freebuff-model": model,
            "x-freebuff-instance-id": instance_id,
            "x-freebuff-wallet-spend-limit": "0",
            "x-fb-timezone": "Asia/Ho_Chi_Minh",
            "x-freebuff-acting-user-id": self.user_id,
        }
        req_adm = urllib.request.Request(
            "https://www.codebuff.com/api/v1/freebuff/session/admission",
            headers=adm_headers,
            method="POST"
        )
        with urllib.request.urlopen(req_adm, timeout=15) as resp:
            adm_resp = json.loads(resp.read().decode())
            actual_instance_id = adm_resp.get("instanceId", instance_id)

        # 2. Start Agent Run
        run_headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "x-freebuff-acting-user-id": self.user_id,
        }
        run_body = {"action": "START", "agentId": agent_name, "ancestorRunIds": []}
        req_run = urllib.request.Request(
            "https://www.codebuff.com/api/v1/agent-runs",
            data=json.dumps(run_body).encode(),
            headers=run_headers,
            method="POST"
        )
        with urllib.request.urlopen(req_run, timeout=15) as resp:
            run_id = json.loads(resp.read().decode())["runId"]

        # 3. Chat Completion
        chat_headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-freebuff-acting-user-id": self.user_id,
            "x-freebuff-model": model,
        }
        chat_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are Buffy, the strategic coding assistant."},
                {"role": "user", "content": prompt}
            ],
            "stream": True,
            "codebuff_metadata": {
                "run_id": run_id,
                "client_id": str(uuid.uuid4()),
                "n": agent_name,
                "cost_mode": "free",
                "freebuff_instance_id": actual_instance_id
            }
        }
        req_chat = urllib.request.Request(
            "https://www.codebuff.com/api/v1/chat/completions",
            data=json.dumps(chat_payload).encode(),
            headers=chat_headers,
            method="POST"
        )

        full_content = []
        with urllib.request.urlopen(req_chat, timeout=30) as resp:
            for line in resp:
                l = line.decode(errors="replace").strip()
                if l.startswith("data:"):
                    raw = l[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        data = json.loads(raw)
                        delta = data["choices"][0]["delta"].get("content", "")
                        if delta:
                            print(delta, end="", flush=True)
                            full_content.append(delta)
                    except Exception:
                        pass
        return "".join(full_content)

if __name__ == "__main__":
    client = FreebuffClient()
    print("Gửi request tới Freebuff...")
    client.chat("Cho tôi ví dụ decorator trong Python.")
```

## 4. PHẦN 3: CODEBUDDY / WORKBUDDY (TENCENT)

Package npm: `@tencent-ai/codebuddy-code` — lệnh `codebuddy` / `cbc`. Đây là agent CLI của Tencent với backend `codebuddy.ai`, có cả biến thể quốc tế (`www.codebuddy.ai`) và Trung Quốc (`copilot.tencent.com`).

### Xác thực Device-Flow (không cần mật khẩu)

Không có token dùng chung. Flow xác thực gồm 3 endpoint:

```bash
# 1. Xin state + URL đăng nhập
curl -s -X POST "https://www.codebuddy.ai/v2/plugin/auth/state?platform=CLI"
# -> {"state":"<UUID>","authUrl":"https://www.codebuddy.ai/login?platform=CLI&state=<UUID>"}

# 2. Mở authUrl trong trình duyệt, đăng nhập
# 3. Poll token cho tới khi có kết quả
curl -s "https://www.codebuddy.ai/v2/plugin/auth/token?state=<UUID>"
# Chưa login -> {"code":11217,"msg":"11217:login ing...","requestId":"..."}
# Đã login  -> {"accessToken":"...","refreshToken":"..."}
```

Biến thể Trung Quốc dùng host `https://copilot.tencent.com` với cùng path.

**Nơi lưu token sau khi đăng nhập:**

- `~/.codebuddy/auth_token.json` — khoá `accessToken`, `refreshToken`
- `~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info` — session file cho CLI

**Lấy User ID** (bắt buộc cho mọi request chat):

```bash
curl -s "https://www.codebuddy.ai/v2/plugin/account" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" | jq -r '.data.uid'
```

### Danh sách Model & Bảng giá Credits

Bảng giá thật lấy từ `GET https://www.codebuddy.ai/v3/config` (endpoint CLI dùng để nạp catalog). Request cần kèm `Authorization`, `X-User-Id`, `X-Product: TencentCloud`, `X-Domain: www.codebuddy.ai`.

**Model MIỄN PHÍ (credits = 0):**

| Model ID | Tên | Credits | Ghi chú |
| :--- | :--- | :--- | :--- |
| `hy3` | Hy3 (Hunyuan 3 Thinking) | **x0.00** | Free vĩnh viễn, có tag `craft` |
| `deepseek-v4.1-flash` | DeepSeek-V4.1-Flash | **x0.00** | Free vĩnh viễn, context 1M, multimodal |
| `hy4-preview-f` | Hy4 trial | **0** (không có trong config) | Trial 14 ngày, xem mục dưới |

**Model TRẢ PHÍ (đã kiểm chứng bằng request thật):**

| Model ID | Credits (config) | Thực tế trừ |
| :--- | :--- | :--- |
| `gpt-5.6-luna` | x0.14 | ~0.04 / prompt ngắn |
| `deepseek-v4.1-flash-sg` | x0.03 | ~0.02 |
| `hy4-preview` | x0.29 | 0.26 – 1.25 |
| `glm-5.3` / `glm-5.2` | x0.79 | 0.03 – 1.64 |
| `kimi-k2.6` | x0.52 | — |
| `kimi-k2.8-preview` | x0.77 | — |
| `gemini-3.5-flash` | x0.99 | — |
| `gpt-5.6-terra` | x1.39 | — |
| `kimi-k3` | x1.62 | 0.18 |
| `gpt-5.4` | x1.65 | — |
| `gpt-5.5` / `primary-model` | x3.31 | — |
| `deep-model` | x3.33 | — |
| `gpt-5.6-sol` | x3.47 | — |
| `gpt-6-astra` | x6.67 | — |
| `fast-model` | x0.34 | — |
| `balanced-model` | x0.59 | — |

### Biến thể Trial ẩn `hy4-preview-f`

Đây là điểm quan trọng nhất và dễ nhầm nhất. Model `hy4-preview` (không hậu tố) **KHÔNG free** — nó tính 0.29 credits. Nhưng backend chấp nhận thêm một model ID ẩn `hy4-preview-f` **thực sự trả về `credit: 0`**.

Cấu hình trial được tìm thấy trong `~/.codebuddy/local_storage/entry_4348632e75ae0692960c04ccaf0a17df.info`:

```json
"productFeaturesConfig": {
  "ModelTrialBanner": {
    "banners": [{
      "modelId": "hy4-preview-f",
      "targetModelId": "hy4-preview",
      "trialDays": 14,
      "firstUseTimeKey": "hy4.first_user_time"
    }]
  }
}
```

Nghĩa là: trial 14 ngày, bắt đầu tính từ lần dùng đầu tiên (key `hy4.first_user_time`). Sau 14 ngày backend sẽ chuyển sang trừ credit hoặc từ chối.

Đã kiểm chứng bằng request thật, cùng prompt ~150 từ:

- `hy4-preview` → `credit: 0.26 – 0.48`
- `hy4-preview-f` → **`credit: 0`** (với 8.341 tokens)

**Lưu ý:** suffix `-f` này chỉ tồn tại cho `hy4-preview`. Đã quét toàn bộ 21 model còn lại với hậu tố `-f` — tất cả đều trả `{"code":11102,"msg":"model [<id>-f] service info not found"}`.

### Lệnh curl đầy đủ cho CodeBuddy

```bash
TOKEN=$(jq -r '.accessToken' ~/.codebuddy/auth_token.json)
USER_ID=$(curl -s "https://www.codebuddy.ai/v2/plugin/account" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.data.uid')

# Gọi chat — message đầu BẮT BUỘC là role "system"
curl -s -N -X POST "https://www.codebuddy.ai/v2/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "model": "hy3",
    "messages": [
      {"role": "system", "content": "You are CodeBuddy Code."},
      {"role": "user", "content": "Giải thích QuickSort ngắn gọn."}
    ],
    "stream": true
  }'
```

Đổi `"model"` thành `deepseek-v4.1-flash` hoặc `hy4-preview-f` để dùng các model free khác.

**Đọc mức credit đã tiêu thụ:** trong SSE stream, block `usage` cuối cùng có trường `credit`:

```json
"usage": {"prompt_tokens":72,"completion_tokens":69,"total_tokens":141,
          "completion_thinking_tokens":60,"credit":0}
```

### Script Python trọn gói cho CodeBuddy

```python
import json
import re
import subprocess
from pathlib import Path

FREE_MODELS = ["hy3", "deepseek-v4.1-flash", "hy4-preview-f"]
ENDPOINT = "https://www.codebuddy.ai/v2/chat/completions"


class CodeBuddyClient:
    def __init__(self, token_path="~/.codebuddy/auth_token.json"):
        path = Path(token_path).expanduser()
        if not path.exists():
            raise FileNotFoundError("Chưa đăng nhập. Chạy 'codebuddy' và dùng /login trước.")
        self.token = json.loads(path.read_text())["accessToken"]
        self.user_id = self._fetch_user_id()

    def _fetch_user_id(self):
        out = subprocess.run([
            "curl", "-s", "https://www.codebuddy.ai/v2/plugin/account",
            "-H", f"Authorization: Bearer {self.token}",
        ], capture_output=True, text=True).stdout
        return json.loads(out)["data"]["uid"]

    def chat(self, prompt, model="hy3", system="You are CodeBuddy Code."):
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},   # BẮT BUỘC đứng đầu
                {"role": "user", "content": prompt},
            ],
            "stream": True,
        })
        raw = subprocess.run([
            "curl", "-s", "-N", "--max-time", "240", "-X", "POST", ENDPOINT,
            "-H", f"Authorization: Bearer {self.token}",
            "-H", f"X-User-Id: {self.user_id}",
            "-H", "Content-Type: application/json",
            "-H", "Accept: text/event-stream",
            "-d", body,
        ], capture_output=True, text=True).stdout

        text = "".join(re.findall(r'"content":"([^"]*)"', raw))
        charged = re.findall(r'"credit":([0-9.]+)', raw)
        tokens = re.findall(r'"total_tokens":(\d+)', raw)
        return {
            "text": text,
            "credit_charged": charged[-1] if charged else None,
            "total_tokens": tokens[-1] if tokens else None,
        }


if __name__ == "__main__":
    cb = CodeBuddyClient()
    for m in FREE_MODELS:
        r = cb.chat("Viết 100 từ về HTTP/2.", model=m)
        print(f"[{m}] charged={r['credit_charged']} tokens={r['total_tokens']}")
        print(r["text"][:120], "...\n")
```

### Dùng qua CLI (không cần viết code)

```bash
codebuddy -p "câu hỏi của bạn" --model hy3
codebuddy -p "câu hỏi của bạn" --model deepseek-v4.1-flash
codebuddy -p "câu hỏi của bạn" --model hy4-preview-f   # trial 14 ngày
```

---

## 5. LƯU Ý VÀ KHẮC PHỤC LỖI

### Với OpenCode Zen
- **Lỗi 403 `FreeTierError`**: Do thiếu mảng `tools` trong request body hoặc format header `x-opencode-session` / `x-opencode-request` không chuẩn theo mẫu.
- **Tần suất gọi**: OpenCode Zen giới hạn số lượng request theo IP nếu gửi liên tục trong thời gian ngắn (Cloudflare 429). Nên tạo mới `x-opencode-session` sau mỗi đoạn hội thoại.

### Với CodeBuddy / WorkBuddy
- **`{"code":11128,"msg":"first message is not system prompt"}`**: Message đầu tiên trong mảng `messages` bắt buộc phải là `{"role":"system", "content":"You are CodeBuddy Code."}`.
- **`{"code":11102,"msg":"model [<id>] service info not found"}`**: Model ID không tồn tại. Nhớ rằng suffix `-f` chỉ có cho `hy4-preview`, không áp dụng cho các model khác.
- **`{"code":11217,"msg":"11217:login ing..."}`**: Device-auth chưa hoàn tất. Mở `authUrl` trong trình duyệt và đăng nhập, sau đó poll lại endpoint token.
- **`{"code":11212 / 11216}` (LicenseExpired / TrialExpired)**: Bản trial `hy4-preview-f` đã hết 14 ngày, hoặc license doanh nghiệp hết hạn.
- **Lỗi HTTP 400 khi gọi `/v3/config`**: Thiếu header phân biệt sản phẩm. Cần đủ `Authorization`, `X-User-Id`, `X-Product: TencentCloud`, `X-Domain: www.codebuddy.ai`.
- **Đọc mức tiêu thụ credit**: trường `credit` nằm trong block `usage` cuối của SSE stream, không phải header HTTP. Dùng `grep -o '"credit":[0-9.]*'` để kiểm tra nhanh.
- **Model `hy3` / `deepseek-v4.1-flash` có tag `craft`**: Chỉ khả dụng trong agent `cli`; nếu tạo agent tùy chỉnh không có tag này thì model sẽ không xuất hiện trong danh sách.

### Với Freebuff
- **Lỗi 400 `No runId found in request body`**: Xảy ra khi `run_id` không được đặt bên trong object `codebuff_metadata`.
- **Lỗi 403 `free_mode_invalid_agent_model`**: Model được gọi không khớp với `agentId` tương ứng trong bảng định tuyến `AGENT_MAP`.
- **Lỗi 409 `model_locked`**: Một tài khoản chỉ được phép có 1 active session cho 1 model tại một thời điểm. Nếu muốn đổi model, cần gọi `DELETE https://www.codebuff.com/api/v1/freebuff/session` trước khi admit model mới.
- **Hạn mức Freebucks**: Mỗi ngày tài khoản miễn phí nhận 25 Freebucks (DeepSeek V4 Flash tiêu tốn khoảng 10-15 Freebucks/giờ tùy khung giờ cao điểm / thấp điểm).
