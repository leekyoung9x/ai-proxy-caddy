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
| `Caddyfile` | Proxy localhost: `:8089` (opencode), `:8088` (openrouter), `:8087` (xqapi raw) |
| `xq-inject/inject.py` | Chèn `routing` (route lock) vào JSON body cho XQAPI theo MODEL, stream SSE passthrough |
| `docker-compose.yml` | Chạy 2 container (`opencode-proxy` caddy + `xq-inject`), join network `root_poki-net` (external) |
| `.gitignore` | Bỏ qua data/volumes local |

## Cấu hình chi tiết

### :8089 → OpenCode Zen (`https://opencode.ai`)

- `handle_path /v1/*` + `rewrite * /zen/v1{path}`: client gọi `/v1/chat/completions`,
  upstream nhận `/zen/v1/chat/completions`.
- `header_up` gắn cứng:
  - `Host: opencode.ai`
  - `x-opencode-session: hehe` (free tier bắt buộc session id; 9Router không
    forward custom header nên gắn tại proxy)
  - `x-opencode-client: desktop`
- `Authorization` **pass-through**: client tự gửi `Bearer <key>` của mình,
  proxy không đè.
- Base URL cho client: `http://opencode-proxy:8089/v1` (khi chạy cùng Docker
  network) hoặc `http://127.0.0.1:8089/v1` (khi gọi từ host).
  ⚠️ Không thêm `/zen` vào base URL — proxy đã tự thêm, thừa sẽ thành
  `/zen/zen/...` → upstream trả lỗi.

### :8088 → OpenRouter (`https://openrouter.ai`)

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

Luật chèn (fail closed):
- Model có trong map → proxy **luôn overwrite** `routing` của client
  (client gửi gì cũng thua).
- Model không có trong map → trả `400`, **không forward** lên XQAPI.
- Body không phải JSON chat (GET, không có field `model`) → pass-through nguyên.

Map hiện tại (`MODEL_ROUTES_JSON` trong `docker-compose.yml`):

| Model | Failover | Route hiện tại* |
|---|---|---|
| `deepseek-v4-flash` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (temp 1%) |
| `deepseek-flash` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | `pin:false` tạm mở auto (temp 680 chết) |
| `glm-5.3-flash` | `false` = khóa cứng mọi khung giờ | động (temp 1%) |
| `kimi-k3` | `auto` = chỉ fallback trong khung giảm giá, còn lại khóa cứng | động (temp 1%, duy nhất 1 route) |

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
4. **`FreeUsageLimitError / Rate limit exceeded`** → không phải lỗi proxy
   (direct gọi thẳng upstream cũng vậy). Free tier hết quota / bị giới hạn theo
   IP — đổi session id hoặc chờ reset.
5. **Khác Docker network** → container không resolve được tên service. Kiểm tra
   bằng `docker network inspect <net>` và cho container cần gọi join cùng net.

## License

MIT
