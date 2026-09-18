# ai-proxy-caddy

Reverse-proxy nhẹ bằng Caddy 2, gom nhiều AI provider (OpenAI-compatible) về
localhost để các app nội bộ (9Router, New-API, OpenCode...) gọi mà không cần
public endpoint, đồng thời tự gắn header/auth mà client không gửi được.

## Mục đích

- **Che key / header nhạy cảm**: client nội bộ chỉ gọi `http://127.0.0.1:PORT`,
  Caddy tự gắn `Authorization`, `x-opencode-session`, `HTTP-Referer`... trước
  khi forward lên upstream HTTPS.
- **Chuẩn hoá path**: ví dụ OpenCode Zen yêu cầu prefix `/zen/v1` nhưng client
  OpenAI-compatible chỉ gọi `/v1` — proxy tự rewrite.
- **Chạy nội bộ**: bind `127.0.0.1` + join Docker network chung để các container
  gọi nhau bằng tên service, không mở port ra Internet.
- **Không đụng hạ tầng chính**: compose + Caddyfile riêng, tách khỏi Caddy
  production (80/443).

## Kiến trúc

```
 ┌──────────┐   http://opencode-proxy:8089/v1/*    ┌───────────┐
 │ 9Router / │ ──► rewrite → /zen/v1/* ──►────────► │opencode.ai│
 │ New-API   │    + header_up session/client       │ /zen/v1   │
 └──────────┘                                     └───────────┘

 ┌──────────┐   http://opencode-proxy:8088/*       ┌────────────┐
 │ 9Router / │ ──────────────────────────►────────► │openrouter  │
 │ New-API   │    + header_up HTTP-Referer          │  .ai       │
 └──────────┘                                     └────────────┘

 ┌──────────┐   http://xq-inject:8091/*               ┌───────────┐
 │ 9Router   │ ──► chèn routing theo MODEL vào body ──► │xqapi.com  │
 └──────────┘     (local, không domain/TLS)            └───────────┘
```

## Files

| File | Vai trò |
|---|---|
| `Caddyfile` | Proxy localhost: `:8089` (opencode), `:8088` (openrouter), `:8087` (xqapi raw), `:8086` (freebuff), `:8085` (codebuddy) |
| `xq-inject/inject.py` | Chèn `routing` (route lock) vào JSON body cho XQAPI theo MODEL, stream SSE passthrough |
| `oc-inject/inject.py` | Shim chat → Responses cho OpenCode Zen, gắn 5 tool + header session |
| `freebuff-inject/inject.py` | Adapter Freebuff: admission → agent-run → chat, tự nhả session lock |
| `codebuddy-inject/inject.py` | Adapter CodeBuddy: chèn system message, log `usage.credit` |
| `docker-compose.yml` | 5 container (caddy + `oc-inject` + `xq-inject` + `freebuff-inject` + `codebuddy-inject`), join network `root_poki-net` (external) |
| `docs/FREE_AI_APIS.md` | Tài liệu reverse-engineer + curl đầy đủ cho cả 4 provider |
| `docs/HUONG_DAN_TRACE_CHI_TIET.md` | Bản trace chi tiết, kèm script Python trọn gói |
| `examples/*.py` | Client độc lập (không cần CLI): `opencode_free.py`, `freebuff_free.py`, `codebuddy_free.py` |
| `.gitignore` | Bỏ qua data/volumes local |

## Cấu hình chi tiết

### :8089 → OpenCode Zen (`https://opencode.ai`)

- `handle_path /v1/*` + `rewrite * /zen/v1{path}`: client gọi `/v1/chat/completions`,
  upstream nhận `/zen/v1/chat/completions`.
- `oc-inject` tạo lại header bắt buộc: `Authorization: Bearer public`,
  `x-opencode-session`/`x-opencode-request` đúng dạng `ses_`/`msg_` + ID,
  `x-opencode-client: cli`, `x-opencode-project: global`, User-Agent OpenCode 1.18.
- Body JSON được bổ sung 5 tool OpenCode (`bash`, `edit`, `glob`, `grep`, `read`),
  ép `stream:true` và `tool_choice:auto` để match handshake free tier
  (áp cho cả `/chat/completions` lẫn `/responses` — endpoint Responses dùng
  format tools khác: `{"type":"function","name":...}` không có wrapper `function`).
- Response là SSE streaming passthrough với client xin stream; client non-stream
  nhận JSON chuẩn (`chat.completion` cho /chat/completions,
  `response` object cho /responses).
- Model chỉ chạy trên `/responses` (mặc định `muse-spark-1.3-contributor-free`,
  env `REDIRECT_MODELS`) được shim convert từ chat → Responses và trả ngược
  shape `chat.completion`:
  - user message → `input_text`
  - assistant message text → `output_text`; assistant tool call → `function_call`
  - tool result → `function_call_output` (giữ `call_id`)
  - system/developer → gộp vào `instructions`
  Nếu chỉ đổi SSE mà không đổi hai message này, lượt thứ hai sẽ gọi lại tool
  hoặc dừng, dù lượt đầu đã có `finish_reason: tool_calls`.
  - text gom từ SSE delta, không nhân đôi với `response.completed`
  - `ping`/comment lines được bỏ qua, chỉ log event type (không log body)
  - client `stream:true` nhận lại đúng `chat.completion.chunk` SSE và `data: [DONE]`;
    không passthrough event Responses nguyên bản (`response.output_text.delta`),
    vì 9Router sẽ coi stream đó là rỗng
  - `max_output_tokens` được nâng sàn 16384: Responses tính cả reasoning vào
    output budget, `max_tokens` nhỏ của Chat Completions tạo
    `response.incomplete (max_output_tokens)` → stream không có text nào →
    "Provider returned an empty response stream"
  - Tool-call events của Responses (`response.output_item.added`,
    `response.function_call_arguments.delta/done`) được đổi sang OpenAI
    `tool_calls`, để 9Router thực thi tool rồi gửi lượt hội thoại tiếp theo.
    Các delta argument map cùng `item_id`/`call_id`, giữ nguyên `index` và không
    phát header tool rỗng; nếu không client báo `Model generated invalid tool call`.
    - Tool name downstream phải là tên tool client thật đăng ký. Nếu upstream trả
      `bash` nhưng request client chỉ có `terminal`, shim remap `bash → terminal`
      (tương tự `read → read_file`, `edit → edit_file`) để Hermes không báo
      `Model generated invalid tool call`.
      Lưu ý: remap chỉ áp dụng khi request thật có tool đích; nếu client không
      khai báo `terminal`, shim giữ nguyên tên upstream để không làm sai contract.
    Arguments upstream gửi lộn xộn theo 3 dạng, shim chấp nhận cả ba và
    KHÔNG nhân đôi:
    (a) delta → done (chuẩn); (b) chỉ `arguments` nằm trong event
    `function_call_arguments.done`; (c) chỉ nằm trong `output_item.done`.
    Một khi đã có delta args thật, mọi event sau bị chặn phát lại args
    (ngược lại client nhận `{"command":...}{"command":...}` → JSON extra data).
  - Lịch sử hội thoại lượt 2 cũng remap tool name: assistant `tool_calls`
    name upstream (`bash`) đổi thành tool client thật (`terminal`) trước khi
    gửi `function_call` lên Responses, để Muse không tưởng client vẫn có
    `bash` rồi gọi tiếp `bash` vô hạn (identical_call_streak_halt).
  - Khi Hermes guardrail đã chặn một tool call lặp, shim thêm một message
    user-level vào Responses input yêu cầu Muse dừng tool và giải thích blocker;
    không để Muse đọc blocker như output bình thường rồi phát lại y nguyên call.
    Đồng thời phát hiện chuỗi `(tool name, arguments, result)` lặp ≥3 lần trong
    history và chèn cảnh báo ngay cả khi marker guardrail không còn nguyên văn
    (ví dụ app chỉ lưu preview/result stub).
  - Freebuff adapter có `GET /v1/models`; model list được công bố để 9Router
    không báo lỗi `501 Error fetching models`.
- 9Router node "OpenCode Zen" prefix **`oczen`** trỏ về base URL này
  (`http://opencode-proxy:8089/v1`, key `public`). KHÔNG dùng prefix `oc`:
  9Router có builtin provider `opencode` alias `oc` (gọi thẳng
  `https://opencode.ai/zen/v1`) nên prefix `oc` bị builtin chiếm, bypass shim
  → 403 FreeTierError. Combo tương ứng: `oczen/muse-spark-1.3-contributor-free`.
- Test từ host: 9Router nằm ở `127.0.0.1:20127` (20128 là omniroute).

### :8085 → CodeBuddy / WorkBuddy (Tencent)

Adapter nội bộ tại `codebuddy-inject:8094` gọi thẳng
`POST https://www.codebuddy.ai/v2/chat/completions`. Khác biệt so với OpenAI
chuẩn (phát hiện qua trace):

1. Message đầu tiên trong `messages` **phải** là `role: "system"`. Thiếu →
   `{"code":11128,"msg":"first message is not system prompt"}`. Adapter tự chèn
   nếu client không gửi.
2. Header `X-User-Id` **không** bắt buộc cho `/v2/chat/completions` (đã test: thiếu / rỗng / sai UUID đều HTTP 200). Chỉ `Authorization` mới cần.
3. Credit tiêu thụ nằm ở `usage.credit` trong event cuối của SSE.

Model free đã verify (`usage.credit = 0`) trên tài khoản hiện tại:

| Model | Credit/req | Ghi chú |
|---|---|---|
| `hy3` | **0** | Hunyuan 3 Thinking — reasoning model |
| `deepseek-v4.1-flash` | **0** | context 1M, native multimodal |
| `hy4-preview-f` | **0** | trial 14 ngày (xem cảnh báo dưới) |

`hy3` trả 2 field: `reasoning_content` (suy luận) và `content` (đáp án cuối).

⚠️ `hy4-preview-f` **không** được đưa vào `GET /v1/models`: backend chỉ chấp
nhận ID này trong 14 ngày kể từ lần dùng đầu (`hy4.first_user_time` trong
`local_storage` của CLI). Sau đó tính như `hy4-preview` thường (0.29 credit).
Vẫn gọi được qua adapter (trong `TRIAL_MODELS`) nhưng cố ý không công bố, tránh
9Router hiện model ảo rồi 403.

Không phải model nào cũng có bản free: `hy4-preview` = 0.29,
`deepseek-v4.1-flash-sg` = 0.03, `glm-5.3` = 0.79. Suffix `-f` không phải cơ
chế chung — quét cả 21 model × `-f`, chỉ `hy4-preview-f` tồn tại.

Endpoint cho 9Router: `http://opencode-proxy:8085/v1`. Token lấy bằng device
flow (`POST /v2/plugin/auth/state` → mở `authUrl` → `GET
/v2/plugin/auth/token?state=...`), `expiresIn` ≈ 365 ngày nên không cần refresh.
Đặt `CODEBUDDY_TOKEN` + `CODEBUDDY_USER_ID` trong `.env` của stack.

### :8086 → Freebuff/Codebuff

Adapter nội bộ tại `freebuff-inject:8093` thực hiện đúng flow của Freebuff:
`session/admission` → `agent-runs` (`START`) → `/chat/completions` với
`codebuff_metadata`.

**Freebuff KHÔNG có model giá 0.** Tài khoản free được cấp pool **25
Freebucks/ngày** (reset 00:00 giờ Saigon = 17:00 UTC), mỗi model trừ theo
giá/giờ. Danh sách model được phép ở tier `limited` do server tự công bố trong
lỗi 409 `session_model_mismatch`:

> "Limited free access is only available with GLM 5.3 Flash or DeepSeek V4.1
> Flash or MiMo 2.5 or Solar Pro 4."

| Model | Agent | Freebucks/giờ |
|---|---|---|
| `z-ai/glm-5.3-flash` | `base2-free-glm-5-3-flash` | 5 |
| `mimo/mimo-v2.5` | `base2-free-mimo` | 10 |
| `upstage/solar-pro4` | `base2-free-solar-pro4` | 10 |
| `deepseek/deepseek-v4-flash` | `base2-free-deepseek-flash` | 15 (off-peak 10) |

Đã verify sống: `z-ai/glm-5.3-flash` → HTTP 200 trả text thật. 3 model còn lại
server tuyên bố free nhưng bị chặn bởi quota ngày lúc verify (429
`rate_limited`) — không phải model lock.

Các model khác trong bảng giá (`gpt-5.6-luna` 20, `gemini-3.8-flash` 50,
`kimi-k3-eco` 5, `muse-spark-*` 15) chỉ mở cho tier trả phí — gọi vào bị `409
session_model_mismatch`, nên KHÔNG đưa vào `/v1/models`.

Bẫy: session cũ giữ slot model khác → `409 model_locked`. Adapter tự gọi
`GET /freebuff/session` lấy `instanceId` rồi `DELETE` để nhả slot trước khi xin
admission model mới.

Endpoint cho 9Router: `http://opencode-proxy:8086/v1`. Adapter tự thêm system
prompt Buffy nếu request chưa có system message và ép upstream `stream:true`.
Thêm 2 endpoint tiện dụng: `GET /v1/models` (kèm `freebucks_per_hour`) và
`GET /v1/freebucks` (xem `daily.remaining`). Token đặt `FREEBUFF_TOKEN` +
`FREEBUFF_USER_ID` trong `.env` của stack — không commit.

- Forward nguyên path, chỉ gắn:
  - `Host: openrouter.ai`
  - `HTTP-Referer: https://hermes-agent.nousresearch.com` (OpenRouter dùng để
    xếp hạng app, nên có)
- Base URL cho client: `http://opencode-proxy:8088/api/v1`
  (`GET /api/v1/models` → `200`).

### :8087 → XQAPI (`https://xqapi.com`)

- Forward nguyên path, chỉ gắn:
  - `Host: xqapi.com`
  - `X-XQAPI-Route: route-405`
  - `X-XQAPI-Failover: false`
- Base URL cho client: `http://opencode-proxy:8087`
  (từ host: `http://127.0.0.1:8087`), giữ nguyên path của upstream.

## Chạy

Yêu cầu: Docker + Docker Compose, và Docker network dùng chung đã tồn tại
(mặc định `root_poki-net`; đổi tên trong `docker-compose.yml` nếu cần).

```bash
# tạo network dùng chung (nếu chưa có)
docker network create root_poki-net

# chạy
docker compose up -d

# kiểm tra
curl http://127.0.0.1:8089/v1/models
curl http://127.0.0.1:8088/api/v1/models
curl http://127.0.0.1:8087/
```

Gọi chat OpenCode Zen (key public free tier):

```bash
curl -X POST http://127.0.0.1:8089/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer public' \
  -d '{"model":"mimo-v2.5-free","messages":[{"role":"user","content":"Hello, reply exactly OK"}]}'
```

## Nối từ container khác (ví dụ 9Router)

Điều kiện: container đó join cùng network (`root_poki-net`). Khi đó gọi bằng
tên service, **không** dùng `127.0.0.1` (nó sẽ trỏ vào chính container đó →
`ECONNREFUSED`):

- OpenCode Zen channel base URL: `http://opencode-proxy:8089/v1`
- OpenRouter channel base URL: `http://opencode-proxy:8088/api/v1`
- XQAPI base URL: `http://opencode-proxy:8087` (giữ nguyên path upstream)
- Freebuff channel base URL: `http://opencode-proxy:8086/v1`
- CodeBuddy channel base URL: `http://opencode-proxy:8085/v1`

## XQAPI local: xq-inject map MODEL → route (không domain)

XQAPI khóa route bằng field `routing` trong JSON body (header `X-XQAPI-Route`
không có tác dụng). `xq-inject` đọc field `model` trong body và chèn `routing`
mặc định từ map `MODEL_ROUTES_JSON`:

```
9Router (node prefix `xq`, baseUrl http://xq-inject:8091/v1)
  → xq-inject: resolve route rẻ nhất còn sống từ public API XQAPI,
    chèn routing theo MODEL → https://xqapi.com
```

Route id xoay theo giờ nên **không hardcode** — injector GET
`/api/public/models/<model>`, lọc `available`, ưu tiên `healthy` → rẻ nhất
(`officialPriceRatio` thấp nhất) → latency thấp nhất, cache 5 phút
(env `ROUTE_TTL_S`). API chết thì xài cache cũ; chưa có cache + không có
`route` tĩnh dự phòng → `400` (fail closed).

Route chết giữa chừng (502/503/504) → xóa cache, resolve lại ngay:
khác route cũ thì retry 1 lần với route mới rồi trả tiếp; giống route cũ
→ entry `auto` **và đang trong khung giảm giá** thì rớt xuống upstream-auto
(xóa `routing`) retry 1 lần cuối (log `RETRY-AUTO`); còn lại (khóa `false`
cứng, hoặc ngoài khung) → trả chết để 9Router điều sang model khác (log `DEAD`).

`pin:false` (tạm mở auto) cũng chỉ có tác dụng **trong khung giảm giá**;
ngoài khung vẫn pin + khóa như thường.

Luật chèn:
- Model có trong map → proxy **luôn overwrite** `routing` của client.
- Model lạ (chưa map) → cho qua thẳng, không chặn không quản.
- Body không phải JSON chat (GET, không có field `model`) → pass-through nguyên.

Map hiện tại (`MODEL_ROUTES_JSON` trong `docker-compose.yml`):

| Model | Failover | Route hiện tại* |
|---|---|---|
| `deepseek-v4-flash` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (temp 1%) |
| `deepseek-flash` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | `pin:false` tạm mở auto (temp 680 chết) |
| `glm-5.3-flash` | `false` = khóa cứng mọi khung giờ | động (temp 1%) |
| `kimi-k3` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (temp 1%, duy nhất 1 route) |
| `deepseek-v4.1-flash` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (temp 1%) |
| `gpt-image-2.5-sunburst` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (duy nhất route-650 生图特惠) |
| `grok-4.6` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (rẻ nhất: 641 Gork特惠 4%) |
| `gpt-5-6-luna` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (duy nhất route-696 temp) |

\*Route resolve động theo giờ, coi log `FINAL model=... routing={...}` để biết
route đang dùng.

Khung giảm giá 50% (giờ VN): T2-T6 07:00-08:00, 11:00-13:00, 17:00-07:00; T7+CN cả ngày.

Thêm model mới (ví dụ `qwen-flash` → route-999). Checklist:

1. Thêm entry vào `MODEL_ROUTES_JSON` của service `xq-inject` trong
   `docker-compose.yml`:
   `"qwen-flash":{"route":"route-999","strategy":"auto","failover":true}`,
   rồi `docker compose up -d --force-recreate xq-inject`.
2. Gọi trong combo 9Router: `xq/qwen-flash` (không cần node/connection mới —
   1 node `xq` + 1 key XQAPI cân hết).
   ⚠️ prefix node trong 9Router **không** được trùng builtin (`ds` = DeepSeek
   official — đã dính 1 lần).
3. Test nhanh từ host:
   `curl -X POST http://127.0.0.1:8091/v1/chat/completions -H 'Authorization: Bearer <KEY>' -H 'Content-Type: application/json' -d '{"model":"qwen-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'`
   + coi log `docker logs xq-inject` có dòng `INJECTED routing=route-999`
   và `FINAL model=qwen-flash routing={...}`.

> Khóa phía key (làm bên console XQAPI, không phải proxy):
> `Binding group = [temporary] super discounted channel`,
> `Incident convert move = OFF`, `Only binding groups = ON`,
> `Allow request override = ON`.
> Proxy khóa route, key khóa group — thiếu 1 trong 2 vẫn lọt official.

## Pitfall đã gặp (đọc trước khi debug)

1. **`ECONNREFUSED 127.0.0.1:PORT` từ container khác** → sai host. Container
   gọi nhau bằng tên service trong cùng Docker network, `127.0.0.1` luôn là
   chính nó.
2. **`Invalid JSON response` từ 9Router** → sai path: thiếu `/v1` ở base URL
   (proxy không match `handle_path` → trả body rỗng), hoặc thừa `/zen`
   (thành `/zen/zen/...`).
3. **`MissingSessionID` từ opencode** → thiếu header `x-opencode-session`.
   9Router/OpenAI-compatible channel không forward custom header → gắn cứng
   tại proxy bằng `header_up`.
4. **`FreeTierError — free tier can only be used from within OpenCode` trên
   combo `oc/...`** → 9Router có builtin provider `opencode` chiếm alias `oc`
   (gọi thẳng `https://opencode.ai/zen/v1`, bypass shim). Node custom phải
   dùng prefix `oczen`. Đổi prefix phải đổi đồng bộ 3 chỗ: providerNodes,
   providerConnections (`providerSpecificData.prefix`) và combo models.
5. **`content type input_text is not valid on assistant messages`** → lỗi schema
   `/responses` từ shim khi convert chat → Responses: assistant phải dùng
   `output_text`, system/developer phải đi `instructions` (đã fix trong
   `oc-inject`, xem mục :8089).
6. **`FreeUsageLimitError / Rate limit exceeded`** → không phải lỗi proxy
   (direct gọi thẳng upstream cũng vậy). Free tier hết quota / bị giới hạn theo
   IP — đổi session id hoặc chờ reset.
7. **Port nhầm khi test từ host**: `127.0.0.1:20128` là omniroute,
   `127.0.0.1:20127` mới là 9Router. Bắn nhầm port sẽ thấy 500 "Internal server
   error" không nguồn gốc, log 9Router trống.
8. **Khác Docker network** → container không resolve được tên service. Kiểm tra
   bằng `docker network inspect <net>` và cho container cần gọi join cùng net.
9. **CodeBuddy `{"code":11128,"msg":"first message is not system prompt"}`** →
   message đầu tiên trong `messages` phải là `role: "system"`. Client
   OpenAI-compatible thường không gửi → `codebuddy-inject` chèn tự động
   (`CODEBUDDY_SYSTEM`, mặc định `"You are CodeBuddy Code."`).
10. **CodeBuddy trả `content` rỗng** → không phải lỗi. `hy3` là reasoning model,
    chữ nằm trong `reasoning_content` (SSE delta), `content` chỉ có đáp án cuối.
11. **Freebuff `409 model_locked`** → Freebuff chỉ cho 1 session active. Đang giữ
    session model khác thì phải `GET /freebuff/session` lấy `instanceId` rồi
    `DELETE /freebuff/session` kèm header `x-freebuff-instance-id` trước khi xin
    admission model mới. `freebuff-inject` tự làm.
12. **Freebuff `409 session_model_mismatch`** → model không thuộc 4 model tier
    `limited` (`glm-5.3-flash`, `deepseek-v4-flash`, `mimo-v2.5`, `solar-pro4`).
    Đừng thêm model khác vào `/v1/models` — client sẽ thấy model ảo rồi 409.
13. **Freebuff `429 rate_limited`** → hết 25 Freebucks/ngày, không phải model
    lock. Xem `GET /v1/freebucks` → `daily.resetAt` (17:00 UTC = 00:00 giờ VN).

## License

MIT
