# API miễn phí của 4 CLI coding agent — curl đầy đủ

Ghi chép reverse-engineer 4 CLI coding agent đang cho dùng model miễn phí, và
cách gọi thẳng API riêng của chúng bằng `curl` / Python (không cần CLI).

| # | Provider | CLI | Model free | Endpoint gốc |
|---|---|---|---|---|
| 1 | OpenCode Zen | `opencode` | `muse-spark-1.3-contributor-free` | `https://opencode.ai/zen/v1` |
| 2 | Freebuff / Codebuff | `freebuff` | pool **25 Freebucks/ngày** | `https://www.codebuff.com/api/v1` |
| 3 | CodeBuddy / WorkBuddy (Tencent) | `codebuddy` | `hy3`, `deepseek-v4.1-flash`, `hy4-preview-f` | `https://www.codebuddy.ai` |
| 4 | XQAPI | – | – | `https://xqapi.com` |

> Tất cả đều là API **không chính thức**, lấy được bằng trace traffic của CLI.
> Điều khoản sử dụng của các dịch vụ này có thể thay đổi bất cứ lúc nào.

---

## 0. Bảng tổng hợp nhanh

### CodeBuddy — model free (verify bằng trường `credit` trong usage)

| Model | Credit/req | Ghi chú |
|---|---|---|
| `hy3` | **0** | Hunyuan 3 Thinking, reasoning model |
| `deepseek-v4.1-flash` | **0** | context 1M, native multimodal |
| `hy4-preview-f` | **0** | trial 14 ngày của `hy4-preview` |
| `hy4-preview` | 0.29 | KHÔNG free |
| `deepseek-v4.1-flash-sg` | 0.03 | KHÔNG free |
| `glm-5.3` | 0.79 | KHÔNG free |

### Freebuff — không có model giá 0

Freebuff cấp **25 Freebucks/ngày** (reset 00:00 giờ Saigon = 17:00 UTC), mỗi
model trừ theo giá/giờ. Danh sách model được phép ở tier `limited` (server tự
công bố trong lỗi 409):

> "Limited free access is only available with **GLM 5.3 Flash** or
> **DeepSeek V4.1 Flash** or **MiMo 2.5** or **Solar Pro 4**."

| Model | Freebucks/giờ | Verify |
|---|---|---|
| `z-ai/glm-5.3-flash` | 5 | ✅ HTTP 200, text thật |
| `mimo/mimo-v2.5` | 10 | server công bố free |
| `upstage/solar-pro4` | 10 | "Limited-time trial" |
| `deepseek/deepseek-v4-flash` | 15 (off-peak 10) | server công bố free |

Các model khác trong bảng giá (`openai/gpt-5.6-luna` 20, `google/gemini-3.8-flash`
50, `crof/kimi-k3-eco` 5, `meta/muse-spark-*` 15) chỉ mở cho tier trả phí —
gọi vào bị `409 session_model_mismatch`.

---

## 1. CodeBuddy / WorkBuddy (Tencent)

### 1.1 Lấy token (device flow)

```bash
# Bước 1: xin state + link đăng nhập
curl -s -X POST 'https://www.codebuddy.ai/v2/plugin/auth/state?platform=CLI' \
  -H 'Content-Type: application/json' -d '{}'
# -> {"state":"...","authUrl":"https://www.codebuddy.ai/..."}

# Mở authUrl trên browser, đăng nhập, xong quay lại:

# Bước 2: đổi state lấy token
curl -s 'https://www.codebuddy.ai/v2/plugin/auth/token?state=<STATE>'
# -> {"accessToken":"...","refreshToken":"...","expiresIn":31535991}
```

`expiresIn` = 31.535.991 giây ≈ **365 ngày** → set một lần dùng cả năm, không
cần refresh.

Hoặc đơn giản hơn: cài CLI rồi `/login`, token nằm ở
`~/.codebuddy/auth_token.json`.

```bash
export CB_TOKEN=$(python3 -c "import json;print(json.load(open('$HOME/.codebuddy/auth_token.json'))['accessToken'])")
# X-User-Id: KHÔNG bắt buộc cho /v2/chat/completions (đã test thiếu/rỗng/sai UUID
# đều HTTP 200). Chỉ cần nếu bạn muốn khớp hành vi CLI gốc.
export CB_UID=$(curl -s 'https://www.codebuddy.ai/v2/plugin/account' \
  -H "Authorization: Bearer $CB_TOKEN" | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['uid'])")
```

### 1.2 Liệt kê model + giá credit

```bash
curl -s 'https://www.codebuddy.ai/v3/config' \
  -H "Authorization: Bearer $CB_TOKEN" \
  -H "X-User-Id: $CB_UID" \
  -H 'User-Agent: CLI/2.154.0 CodeBuddy/2.154.0' \
  -H 'X-Product: TencentCloud' \
  -H 'X-Domain: www.codebuddy.ai' \
  -H 'Accept: application/json' \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
for m in d['data']['models']:
    print(f\"{m.get('id'):34} credits={m.get('credits')}\")
"
```

### 1.3 ⭐ Chat với model free — curl đầy đủ

```bash
# hy3 (Hunyuan 3 Thinking) — 0 credit
curl -N -X POST 'https://www.codebuddy.ai/v2/chat/completions' \
  -H "Authorization: Bearer $CB_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{
    "model": "hy3",
    "messages": [
      {"role": "system", "content": "You are CodeBuddy Code."},
      {"role": "user", "content": "5*8=? tra loi ngan"}
    ],
    "stream": true
  }'
```

Đổi `"model"` thành `deepseek-v4.1-flash` hoặc `hy4-preview-f` là xong — cùng
một endpoint, cùng header.

**Bắt buộc**: message đầu tiên trong `messages` phải là `role: "system"`.
Thiếu → `{"code":11128,"msg":"first message is not system prompt"}`.

**KHÔNG bắt buộc**: header `X-User-Id`. Đã test 4 trường hợp (thiếu / rỗng / sai
UUID / đúng UUID) — tất cả đều HTTP 200 và `credit=0`. Chỉ `Authorization` mới
cần. (`GET /v3/config` thì ngược lại: cần `User-Agent: CLI/<ver> CodeBuddy/<ver>`
+ `X-Product` + `X-Domain`, thiếu → `{"code":12403,"msg":"check ua..."}`.)

**Đọc kết quả**: `hy3` là reasoning model nên có 2 field:
- `reasoning_content` = phần suy luận (dài)
- `content` = câu trả lời cuối (`"40"`)

Credit tiêu thụ nằm ở event cuối: `usage.credit` — model free trả `0`.

### 1.4 Vì sao `hy4-preview` cũng "bằng 0" mà lại không free

Trong danh sách model của CLI bạn có thể thấy `hy4-preview-f` và tưởng đó là
`hy4-preview`. Thực tế:

- `hy4-preview` = **0.29 credit/req** (đo thật 0.26–1.25).
- `hy4-preview-f` = **0 credit**, nhưng là **bản trial 14 ngày** của
  `hy4-preview`, định nghĩa trong local storage của CLI:

```json
{"modelId": "hy4-preview-f", "targetModelId": "hy4-preview",
 "trialDays": 14, "firstUseTimeKey": "hy4.first_user_time"}
```

Hết 14 ngày kể từ lần dùng đầu (`hy4.first_user_time`) thì bị tính như
`hy4-preview` thường. Model này **không** xuất hiện trong `/v3/config` lẫn
`/models` — chỉ gọi thẳng API mới thấy.

Suffix `-f` **không** phải cơ chế chung: đã quét toàn bộ 21 model với suffix
`-f`, tất cả đều trả `{"code":11102,"msg":"model [<id>-f] service info not found"}`.
Chỉ duy nhất `hy4-preview-f` tồn tại.

---

## 2. Freebuff / Codebuff

### 2.1 Lấy token

```bash
export FB_TOKEN=$(python3 -c "import json;from pathlib import Path;print(json.loads(Path('~/.config/manicode/credentials.json').expanduser().read_text())['default']['authToken'])")
export FB_UA='ai-sdk/openai-compatible/0.0.0-test/codebuff'

export FB_UID=$(curl -s 'https://www.codebuff.com/api/v1/me?fields=id' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
```

### 2.2 Xem quota + bảng giá thật

```bash
curl -s 'https://www.codebuff.com/api/v1/freebuff/session' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  -H "x-freebuff-acting-user-id: $FB_UID" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
fb=d['freebucks']
print('quota ngày :', fb['daily'])
print('giá/giờ    :')
for m,p in sorted(fb['prices'].items(), key=lambda x:x[1]): print(f'   {p:>3}  {m}')
"
```

Trả về `freebucks.daily` = `{limit:25, spent, remaining, resetAt:...T17:00:00Z}`.

### 2.3 Giải phóng session cũ (bẫy `409 model_locked`)

Freebuff chỉ cho **1 session active** cho 1 model. Đang giữ session của model
khác rồi xin model mới → `409 {"status":"model_locked"}`. Phải nhả trước:

```bash
# lấy instanceId đang giữ
INST=$(curl -s 'https://www.codebuff.com/api/v1/freebuff/session' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  -H "x-freebuff-acting-user-id: $FB_UID" \
  | python3 -c "import json,sys;print(json.load(sys.stdin).get('instanceId') or '')")

# nhả slot
[ -n "$INST" ] && curl -s -X DELETE 'https://www.codebuff.com/api/v1/freebuff/session' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  -H "x-freebuff-acting-user-id: $FB_UID" \
  -H "x-freebuff-instance-id: $INST"
```

### 2.4 ⭐ Chat — 3 bước bắt buộc

```bash
MODEL='z-ai/glm-5.3-flash'          # hoặc deepseek/deepseek-v4-flash,
AGENT='base2-free-glm-5-3-flash'    # mimo/mimo-v2.5, upstage/solar-pro4
INST="inst_$(cat /proc/sys/kernel/random/uuid)"

# --- Bước 1: admission (mở session, trừ Freebucks ở đây) ---
curl -s -X POST 'https://www.codebuff.com/api/v1/freebuff/session/admission' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  -H "x-freebuff-acting-user-id: $FB_UID" \
  -H "x-freebuff-model: $MODEL" \
  -H "x-freebuff-instance-id: $INST" \
  -H 'x-freebuff-wallet-spend-limit: 0' \
  -H 'x-fb-timezone: Asia/Ho_Chi_Minh' \
  -H 'Content-Type: application/json' -d '{}'
# -> {"status":"active","accessTier":"limited","instanceId":"inst_..."}

# --- Bước 2: tạo run ---
RUN=$(curl -s -X POST 'https://www.codebuff.com/api/v1/agent-runs' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  -H "x-freebuff-acting-user-id: $FB_UID" \
  -H 'Content-Type: application/json' \
  -d "{\"action\":\"START\",\"agentId\":\"$AGENT\",\"ancestorRunIds\":[]}" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['runId'])")

# --- Bước 3: chat ---
curl -N -X POST 'https://www.codebuff.com/api/v1/chat/completions' \
  -H "Authorization: Bearer $FB_TOKEN" -H "User-Agent: $FB_UA" \
  -H "x-freebuff-acting-user-id: $FB_UID" \
  -H "x-freebuff-model: $MODEL" \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [
      {\"role\":\"system\",\"content\":\"You are Buffy, the strategic coding assistant.\"},
      {\"role\":\"user\",\"content\":\"1+1=? tra loi ngan\"}
    ],
    \"stream\": true,
    \"codebuff_metadata\": {
      \"run_id\": \"$RUN\",
      \"client_id\": \"$(cat /proc/sys/kernel/random/uuid)\",
      \"n\": \"$AGENT\",
      \"cost_mode\": \"free\",
      \"freebuff_instance_id\": \"$INST\"
    }
  }"
```

Kết quả verify sống: `z-ai/glm-5.3-flash` → `"**1 + 1 = 2**"`.

### 2.5 Map model → agent

```
z-ai/glm-5.3-flash          → base2-free-glm-5-3-flash
deepseek/deepseek-v4-flash  → base2-free-deepseek-flash
mimo/mimo-v2.5              → base2-free-mimo          (hoặc base3-free-mimo)
upstage/solar-pro4          → base2-free-solar-pro4    (hoặc base3-free-solar-pro4)
```

### 2.6 Các lỗi đã gặp

| Lỗi | Nghĩa |
|---|---|
| `409 model_locked` | Đang giữ session model khác → DELETE trước (2.3) |
| `409 session_model_mismatch` | Model không thuộc 4 model limited → chọn model khác |
| `403 free_mode_invalid_agent_model` | Sai cặp model↔agent |
| `403 free_mode_legacy_luna_agent` | Agent Luna đã bị khai tử |
| `429 rate_limited` | Hết 25 Freebucks/ngày → chờ `daily.resetAt` |
| `400 No runId found in request body` | Thiếu `codebuff_metadata.run_id` |

---

## 3. Chạy qua adapter nội bộ (nếu dùng stack `ai-proxy-caddy`)

Cả 3 provider đã được đóng gói thành container OpenAI-compatible trong repo
`ai-proxy-caddy`, tự xử lý token/header/flow:

| Base URL | Provider | Ghi chú |
|---|---|---|
| `http://opencode-proxy:8089/v1` | OpenCode Zen | prefix 9Router: `oczen` |
| `http://opencode-proxy:8086/v1` | Freebuff | `GET /v1/freebucks` xem quota |
| `http://opencode-proxy:8085/v1` | CodeBuddy | `GET /v1/models` |
| `http://opencode-proxy:8087` | XQAPI | giữ nguyên path upstream |

```bash
curl -X POST http://127.0.0.1:8085/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hy3","messages":[{"role":"user","content":"5*8=?"}]}'
```

Token đặt trong `.env` của stack (`CODEBUDDY_TOKEN`, `CODEBUDDY_USER_ID`,
`FREEBUFF_TOKEN`, `FREEBUFF_USER_ID`) — không commit vào repo.

---

## 4. Anti-abuse từng provider

| Provider | Cơ chế | Cách lách |
|---|---|---|
| OpenCode Zen | Free tier chỉ nhận request có ≥5 tool (`bash`,`edit`,`glob`,`grep`,`read`); 1–4 tool → 403 `FreeTierError` | Gửi kèm đủ 5 tool trong body |
| CodeBuddy | Message đầu phải là `system`; `hy4-preview-f` giới hạn 14 ngày | Chèn system message; dùng `hy3`/`deepseek-v4.1-flash` cho lâu dài |
| Freebuff | 25 Freebucks/ngày; 1 session active; chỉ 4 model cho tier limited | Chọn GLM 5.3 Flash (5/h) cho tiết kiệm nhất; nhả session khi đổi model |

---

## 5. File trong repo

- `codebuddy-inject/inject.py` — adapter CodeBuddy (chèn system message, log credit)
- `freebuff-inject/inject.py` — adapter Freebuff (tự nhả session, `GET /v1/freebucks`)
- `oc-inject/inject.py` — adapter OpenCode Zen (shim chat → Responses + 5 tool)
- `xq-inject/inject.py` — chèn `routing` theo model cho XQAPI
- `docs/FREE_AI_APIS.md` — tài liệu này
