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

 ┌──────────┐   http://opencode-proxy:8087/*         ┌───────────┐
 │ 9Router / │ ──────────────────────────►──────────► │ xqapi.com │
 │ New-API   │    + header_up X-XQAPI-Route/Failover  │           │
 └──────────┘                                       └───────────┘
```

## Files

| File | Vai trò |
|---|---|
| `Caddyfile` | Định nghĩa 3 site `:8089` (opencode), `:8088` (openrouter), `:8087` (xqapi) |
| `docker-compose.yml` | Chạy 1 container `caddy:2-alpine`, bind `127.0.0.1:8087-8089`, join network `root_poki-net` (external) |
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

## Front bằng path prefix trên 1 domain (Caddy chính, có TLS)

> Config này nằm ở `/root/Caddyfile` trên VPS (Caddy chính, port 443),
> **không** nằm trong repo này. Ghi lại để khỏi quên.

Mỗi model XQAPI = 1 path prefix = 1 `X-XQAPI-Route` id (chỉ cần 1 DNS + 1 cert):

```caddy
xq.cutes1tg.online {
    encode gzip zstd
    handle_path /ds/* {
        reverse_proxy https://xqapi.com {
            header_up Host xqapi.com
            header_up X-XQAPI-Route "route-405"
            header_up X-XQAPI-Failover "false"
        }
    }
    handle_path /generic/* {
        reverse_proxy https://xqapi.com {
            header_up Host xqapi.com
        }
    }
    handle {
        respond "xq proxy ok" 200
    }
}
```

Thêm model mới: copy 1 block `handle_path`, đổi prefix + route id. Checklist:

1. (Một lần duy nhất) DNS record `xq` bên Cloudflare trỏ về VPS.
2. Validate: `docker exec poki-caddy caddy validate --config /tmp/Caddyfile.new --adapter caddyfile`
3. Reload (không restart Caddy chính):
   `docker cp /root/Caddyfile poki-caddy:/tmp/Caddyfile.new && docker exec poki-caddy caddy reload --config /tmp/Caddyfile.new --adapter caddyfile`
   (⚠️ sửa `/root/Caddyfile` bằng rewrite sẽ gãy bind-mount inode cũ → container
   vẫn thấy bản cũ. Nên `docker cp` + reload như trên; lần restart container sau
   mount sẽ đọc lại bản mới.)
4. Base URL cho 9router: `https://xq.cutes1tg.online/ds`,
   `https://xq.cutes1tg.online/generic` (prefix giữ nguyên, path sau đó
   pass-through lên upstream).

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
