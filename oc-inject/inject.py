"""oc-inject: anticheat wrapper cho OpenCode Zen free tier (https://opencode.ai).

Điều kiện free tier (phát hiện qua trace):
- Authorization: Bearer public (token tĩnh) — luôn ghi đè.
- x-opencode-session: ses_ + 12 hex (timestamp ms) + 14 ngẫu nhiên = 30 ký tự.
- x-opencode-request: msg_ + 12 hex (timestamp ms) + 14 ngẫu nhiên.
- Body chat/completions PHẢI có tools của OpenCode (bash, edit, glob, grep, read)
  nếu thiếu → 403 FreeTierError.

Caddy (opencode-proxy :8089) rewrite /v1/* → /zen/v1/* rồi forward vào đây;
service này forward tiếp lên https://opencode.ai giữ nguyên path.
"""
import json
import os
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPSConnection
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8092"))
UPSTREAM = os.environ.get("UPSTREAM", "https://opencode.ai")
OC_VERSION = os.environ.get("OC_VERSION", "1.18.0")

_UP = urlparse(UPSTREAM)
_UP_HOST = _UP.hostname or "opencode.ai"
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

REQUIRED_TOOLS = ("bash", "edit", "glob", "grep", "read")

_TOOL_DEFS = {
    "bash": ("Run a shell command in the workspace",
             {"type": "object", "properties": {
                 "command": {"type": "string", "description": "The command to run"}},
              "required": ["command"]}),
    "edit": ("Edit a file in the workspace",
             {"type": "object", "properties": {
                 "path": {"type": "string", "description": "File path"},
                 "content": {"type": "string", "description": "New file content"}},
              "required": ["path", "content"]}),
    "glob": ("Find files matching a glob pattern",
             {"type": "object", "properties": {
                 "pattern": {"type": "string", "description": "Glob pattern"}},
              "required": ["pattern"]}),
    "grep": ("Search file contents with a regex",
             {"type": "object", "properties": {
                 "pattern": {"type": "string", "description": "Regex pattern"},
                 "path": {"type": "string", "description": "Directory or file"}},
              "required": ["pattern"]}),
    "read": ("Read a file from the workspace",
             {"type": "object", "properties": {
                 "path": {"type": "string", "description": "File path"}},
              "required": ["path"]}),
}


def gen_id(prefix):
    """ses_/msg_ + 12 hex (timestamp ms, pad trái) + 14 hex ngẫu nhiên."""
    ts = format(int(time.time() * 1000), "x").rjust(12, "0")[-12:]
    rnd = "".join(random.choice("0123456789abcdef") for _ in range(14))
    return f"{prefix}_{ts}{rnd}"


def fix_tools(data, flat=False):
    """Đảm bảo mảng tools có đủ function OpenCode. Trả True nếu đã sửa.

    flat=True: format Responses API ({"type":"function","name":...})
    flat=False: format Chat Completions (name trong wrapper "function").
    """
    specs = [
        {"type": "function", "name": n, "description": d, "parameters": p}
        if flat else
        {"type": "function", "function": {"name": n, "description": d,
                                          "parameters": p}}
        for n, (d, p) in _TOOL_DEFS.items()
    ]

    def tool_name(t):
        if not isinstance(t, dict):
            return None
        if isinstance(t.get("function"), dict) and \
                isinstance(t["function"].get("name"), str):
            return t["function"]["name"]
        return t.get("name") if isinstance(t.get("name"), str) else None

    if not isinstance(data.get("tools"), list):
        data["tools"] = specs
        return True
    tools = data["tools"]
    have = {tool_name(t) for t in tools}
    missing = [n for n in REQUIRED_TOOLS if n not in have]
    if not missing:
        return False
    for s in specs:
        name = (s.get("function") or s).get("name")
        if name in missing:
            tools.append(s)
    return True


def sse_to_completion(raw, model):
    """Gom SSE stream thành 1 JSON chat.completion chuẩn cho client non-stream."""
    content, reasoning = [], []
    tool_calls = {}
    finish, usage, cid = None, None, None
    created = int(time.time())
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        if chunk.get("id"):
            cid = chunk["id"]
        if chunk.get("created"):
            created = chunk["created"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        c0 = choices[0]
        if c0.get("finish_reason"):
            finish = c0["finish_reason"]
        d = c0.get("delta") or {}
        if isinstance(d.get("content"), str):
            content.append(d["content"])
        if isinstance(d.get("reasoning"), str):
            reasoning.append(d["reasoning"])
        for tc in d.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            agg = tool_calls.setdefault(
                idx, {"id": None, "name": None, "arguments": []})
            if tc.get("id"):
                agg["id"] = tc["id"]
            fn = tc.get("function") or {}
            if isinstance(fn, dict):
                if fn.get("name"):
                    agg["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    agg["arguments"].append(fn["arguments"])
    msg = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = [
            {"id": a["id"] or f"call_{i}", "type": "function",
             "function": {"name": a["name"], "arguments": "".join(a["arguments"])}}
            for i, a in sorted(tool_calls.items())
        ]
        msg["content"] = None
    obj = {"id": cid or gen_id("msg"), "object": "chat.completion",
           "created": created, "model": model,
           "choices": [{"index": 0, "message": msg,
                        "finish_reason": finish or "stop"}]}
    if usage:
        obj["usage"] = usage
    return obj


def read_body(handler):
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks = []
        rf = handler.rfile
        while True:
            line = rf.readline().strip()
            if not line:
                break
            size = int(line.split(b";")[0], 16)
            if size == 0:
                rf.readline()
                break
            chunks.append(rf.read(size))
            rf.readline()
        return b"".join(chunks)
    length = handler.headers.get("Content-Length")
    if length:
        return handler.rfile.read(int(length))
    return b""


class Handler(BaseHTTPRequestHandler):
    server_version = "oc-inject/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        sys.stdout.write(f"{self.address_string()[0]} {self.command} {self.path} "
                         f"-> {format % args}\n")
        sys.stdout.flush()

    def _proxy(self):
        body = read_body(self) if self.command in ("POST", "PUT", "PATCH") else b""
        fixed = False
        want_json = True  # OpenAI mặc định non-stream
        ctype = self.headers.get("Content-Type", "")
        is_responses = "/responses" in self.path
        if body and "application/json" in ctype:
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    # Ghi nhớ ý client TRƯỚC khi ép stream cho upstream.
                    want_json = data.get("stream") is not True
                    fixed = fix_tools(data, flat=is_responses)
                    # Zen free tier requires the OpenCode streaming/tool handshake.
                    data["stream"] = True
                    data["tool_choice"] = "auto"
                    fixed = True
                    body = json.dumps(data, separators=(",", ":")).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        conn = HTTPSConnection(_UP_HOST, _UP.port or 443, timeout=120)
        fwd = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP_HEADERS or kl.startswith("x-opencode-") \
                    or kl == "authorization":
                continue
            fwd[k] = v
        fwd["Host"] = _UP_HOST
        fwd["Authorization"] = "Bearer public"
        fwd["User-Agent"] = (f"opencode/{OC_VERSION} "
                             "ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.13")
        fwd["x-opencode-client"] = "cli"
        fwd["x-opencode-project"] = "global"
        fwd["x-opencode-session"] = gen_id("ses")
        fwd["x-opencode-request"] = gen_id("msg")
        if body:
            fwd["Content-Type"] = ctype or "application/json"
            fwd["Content-Length"] = str(len(body))
        else:
            fwd.pop("Content-Length", None)

        try:
            conn.request(self.command, self.path, body=body or None, headers=fwd)
            resp = conn.getresponse()
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "26")
            self.end_headers()
            self.wfile.write(b'{"error":"upstream unreachable"}')
            print(f"UPSTREAM ERR {self.path}: {e}", flush=True)
            return

        model = None
        try:
            model = json.loads(body).get("model")
        except Exception:
            pass
        dbg = os.environ.get("DEBUG_DUMP") == "1"
        print(f"REQ {model or '-'} session={fwd['x-opencode-session']} "
              f"request={fwd['x-opencode-request']} tools_fixed={fixed} "
              f"collect={want_json}", flush=True)
        if dbg:
            print(f"BODY {self.path} :: {body[:900].decode('utf-8','replace')}",
                  flush=True)

        if not want_json or resp.status != 200:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_HEADERS:
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
            conn.close()
            return

        # Client đã xin non-stream → gom SSE upstream thành 1 chat.completion.
        raw = b""
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            raw += chunk
        conn.close()
        ctype2 = resp.getheaders()
        is_sse = any(k.lower() == "content-type" and "event-stream" in v
                     for k, v in ctype2)
        if not is_sse:
            out = raw
            code = resp.status
        elif is_responses:
            from aggregate_responses import sse_to_response
            obj = sse_to_response(raw.decode("utf-8", "replace"))
            obj["model"] = model or "unknown"
            out = json.dumps(obj).encode()
            code = 200
        else:
            comp = sse_to_completion(raw.decode("utf-8", "replace"),
                                     model or "unknown")
            out = json.dumps(comp).encode()
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_OPTIONS = _proxy


if __name__ == "__main__":
    print(f"oc-inject on :{PORT} -> {UPSTREAM} tools={list(REQUIRED_TOOLS)}",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
