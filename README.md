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
```

## Files

| File | Vai trò |
|---|---|
| `Caddyfile` | Định nghĩa 2 site `:8089` (opencode) và `:8088` (openrouter) |
| `docker-compose.yml` | Chạy 1 container `caddy:2-alpine`, bind `127.0.0.1:8088-8089`, join network `root_poki-net` (external) |
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
