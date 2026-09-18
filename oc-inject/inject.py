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
# Model chỉ chạy trên /responses của Zen (chat/completions → 500).
# Env: REDIRECT_MODELS=muse-spark-1.3-contributor-free,other-model
REDIRECT_MODELS = {
    m.strip() for m in
    os.environ.get("REDIRECT_MODELS", "muse-spark-1.3-contributor-free").split(",")
    if m.strip()
}

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
        if chunk.get("id") and not chunk["id"].startswith("msg_"):
            cid = chunk["id"]
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


def messages_to_input(data, client_tool_names=None):
    """Convert body chat.completions → body Responses API (dùng cho model
    chỉ chạy trên /responses như muse-spark).

    Lưu ý schema /responses:
    - user message  → content [{type: input_text, text}]
    - assistant msg → content [{type: output_text, text}]
    - system/developer → đặt vào instructions (KHÔNG đưa vào input).
    """
    inp = []
    instructions_parts = []
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content")
        texts = []
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    texts.append(c["text"])
        if not texts and role not in ("assistant", "tool"):
            continue
        if role in ("system", "developer"):
            instructions_parts.extend(texts)
            continue
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                inp.append({"type": "function_call",
                            "call_id": tc.get("id") or gen_id("call"),
                            "name": _client_tool_name(fn.get("name", ""), client_tool_names),
                            "arguments": fn.get("arguments", "{}")})
            if texts:
                inp.append({"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": t} for t in texts]})
            continue
        if role == "tool":
            tool_output = content if isinstance(content, str) else json.dumps(content)
            inp.append({"type": "function_call_output",
                        "call_id": m.get("tool_call_id", ""),
                        "output": tool_output})
            # Hermes guardrails inject a deterministic blocker result after
            # repeated identical calls. Muse otherwise ignores that tool
            # result and emits the same call again. Add a user-level stop
            # instruction only for this known guardrail marker.
            if any(marker in tool_output for marker in (
                    "identical_call_streak_halt",
                    "repeated_exact_failure_block",
                    "same_tool_failure_halt")):
                inp.append({"type": "message", "role": "user", "content": [{
                    "type": "input_text",
                    "text": ("Tool guardrail blocked the repeated call. "
                             "Do not call that tool or repeat its arguments. "
                             "Stop the operation and explain the blocker to the user.")
                }]})
            continue
        inp.append({"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": t} for t in texts]})
    out = {"model": data.get("model"),
           "input": inp,
           "stream": True,
           "store": False,
           "instructions": "\n".join(instructions_parts),
           "max_output_tokens": max(16384, int(data.get("max_tokens") or 0))}
    return out


def _client_tool_name(name, client_tool_names):
    """Đổi tên tool upstream về đúng tên tool mà client thật đăng ký."""
    if not name or not client_tool_names or name in client_tool_names:
        return name
    aliases = {"bash": "terminal", "read": "read_file", "edit": "edit_file"}
    target = aliases.get(name)
    return target if target in client_tool_names else name


def responses_sse_to_chat(sse: str, model: str, client_tool_names=None) -> dict:
    """Gom SSE của /responses thành 1 object chat.completion chuẩn OpenAI.

    messages_to_input() chuyển chat sang Responses; hàm này chuyển kết quả
    trở lại shape chat.completion để client (9Router) đọc đúng.
    """
    # Gom event dạng OpenAI Responses: response.output_text.delta /
    # response.completed … gộp text theo item message.
    txt_parts = []
    tool_calls = []
    tool_acc = {}
    item_to_call = {}
    item_name = {}
    usage = {}
    # Gom text: nếu có delta events thì chỉ dùng delta (tránh double khi
    # response.completed lặp lại nội dung đã stream).
    has_deltas = False
    for line in sse.splitlines():
        if '"response.output_text.delta"' in line:
            has_deltas = True
            break
    for line in sse.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        typ = ev.get("type", "")
        if typ == "response.output_text.delta":
            txt_parts.append(ev.get("delta", ""))
        elif typ in ("response.output_item.added", "response.output_item.done"):
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call" and item.get("name"):
                item_to_call[item.get("id") or ""] = (item.get("call_id")
                                                      or item.get("id") or "t0")
                item_name[item.get("id") or ""] = {
                    "call_id": item.get("call_id") or item.get("id"),
                    "name": item.get("name")}
        elif typ == "response.function_call_arguments.delta":
            item = ev.get("item", {}) or {}
            item_id = item.get("id") or ev.get("item_id") or ""
            call_key = (item.get("call_id") or item_to_call.get(item_id)
                        or next(iter(item_name), "t0"))
            meta = item_name.get(item_id) or item_name.get(call_key) or {}
            acc = tool_acc.setdefault(call_key, {
                "id": meta.get("call_id") or call_key,
                "name": meta.get("name"), "args": []})
            if isinstance(ev.get("delta"), str) and ev["delta"]:
                acc["args"].append(ev["delta"])
        elif typ in ("response.completed", "response.done"):
            resp_obj = ev.get("response", ev)
            for item in resp_obj.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            t = c.get("text", "")
                            if t and t not in txt_parts:
                                txt_parts.append(t)
                elif item.get("type") == "function_call":
                    tool_calls.append({
                        "id": item.get("call_id") or item.get("id") or gen_id("call"),
                        "type": "function",
                        "function": {"name": item.get("name"),
                                     "arguments": item.get("arguments", "{}")}})
            u = resp_obj.get("usage") or {}
            if u:
                usage = {"prompt_tokens": u.get("input_tokens", 0),
                         "completion_tokens": u.get("output_tokens", 0),
                         "total_tokens": u.get("input_tokens", 0) +
                                         u.get("output_tokens", 0)}
            for item in resp_obj.get("output", []):
                if item.get("type") != "function_call" or not item.get("name"):
                    continue
                ck = item.get("call_id") or item.get("id") or "t0"
                item_to_call[item.get("id") or ""] = ck
                item_name[item.get("id") or ""] = {
                    "call_id": ck, "name": item.get("name")}
                tool_acc.setdefault(ck, {"id": ck,
                                         "name": item.get("name"),
                                         "args": []})
    tool_calls = []
    for ck, acc in tool_acc.items():
        tool_calls.append({"id": acc["id"], "type": "function",
                           "function": {"name": _client_tool_name(acc["name"], client_tool_names),
                                        "arguments": "".join(acc["args"]) or "{}"}})
    text = "".join(txt_parts)
    msg = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    return {"id": gen_id("msg"), "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def responses_sse_to_chat_stream(sse: str, model: str, client_tool_names=None) -> bytes:
    """Translate Responses SSE into OpenAI chat.completion SSE."""
    out = []
    cid = gen_id("chatcmpl")
    created = int(time.time())
    types = []
    tool_args = []
    tool_map = {}
    item_to_call = {}
    active_tool = None
    emitted_tool_header = set()
    tool_args_done = {}
    tool_finished = False
    for line in sse.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        typ = ev.get("type", "")
        types.append(typ)
        if typ == "response.output_text.delta":
            delta = {"role": "assistant", "content": ev.get("delta", "")}
            out.append("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [{"index": 0, "delta": delta,
                "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
        elif typ == "response.function_call_arguments.delta":
            # Responses function-call delta → OpenAI tool_calls delta.
            item = ev.get("item", {}) or {}
            item_id = item.get("id") or ev.get("item_id")
            call_key = (item.get("call_id") or item_to_call.get(item_id)
                        or active_tool or (next(iter(tool_map), None)) or "t0")
            if item_id and item.get("call_id"):
                item_to_call[item_id] = call_key
            info = tool_map.setdefault(call_key, {
                "idx": len(tool_map),
                "id": item.get("call_id") or item.get("id") or gen_id("call"),
                "name": item.get("name") or ""})
            active_tool = call_key
            chunk = ev.get("delta", "")
            if chunk:
                # Có delta thật → chặn mọi phát args lặp từ done/item.done.
                tool_args_done[call_key] = True
            if info["idx"] not in emitted_tool_header and chunk:
                emitted_tool_header.add(info["idx"])
                td = {"index": info["idx"], "id": info["id"],
                      "type": "function",
                      "function": {"name": _client_tool_name(info["name"], client_tool_names), "arguments": chunk}}
            elif info["idx"] not in emitted_tool_header:
                continue
            else:
                td = {"index": info["idx"],
                      "function": {"arguments": chunk}}
            out.append("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [{"index": 0,
                "delta": {"tool_calls": [td]}, "finish_reason": None}]}) + "\n\n")
        elif typ == "response.output_item.added":
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call":
                item_id = item.get("id")
                call_key = item.get("call_id") or item_id or "t0"
                if item_id:
                    item_to_call[item_id] = call_key
                info = tool_map.setdefault(call_key, {
                    "idx": len(tool_map),
                    "id": item.get("call_id") or item.get("id") or gen_id("call"),
                    "name": item.get("name") or ""})
                active_tool = call_key
                if info["idx"] not in emitted_tool_header:
                    emitted_tool_header.add(info["idx"])
                    td = {"index": info["idx"], "id": info["id"],
                          "type": "function",
                          "function": {"name": _client_tool_name(info["name"], client_tool_names), "arguments": ""}}
                    out.append("data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk",
                        "created": created, "model": model, "choices": [{
                        "index": 0, "delta": {"tool_calls": [td]},
                        "finish_reason": None}]}) + "\n\n")
        elif typ == "response.output_item.done":
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call":
                item_id = item.get("id")
                call_key = (item.get("call_id") or item_id
                            or item_to_call.get(item_id) or active_tool
                            or (next(iter(tool_map), None)) or "t0")
                if item_id:
                    item_to_call[item_id] = call_key
                info = tool_map.setdefault(call_key, {
                    "idx": len(tool_map), "id": call_key, "name": ""})
                if item.get("name") and not info["name"]:
                    info["name"] = item.get("name")
                if info["idx"] not in emitted_tool_header and \
                        (info["name"] or item.get("arguments")):
                    emitted_tool_header.add(info["idx"])
                    td = {"index": info["idx"], "id": info["id"],
                          "type": "function",
                          "function": {"name": info["name"] or "unknown",
                                       "arguments": ""}}
                    out.append("data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0,
                        "delta": {"tool_calls": [td]}, "finish_reason": None}]}) + "\n\n")
                # Upstream có thể chỉ gửi arguments trong item.done — nếu chưa
                # có delta/done nào phát args thì dùng chính event này.
                final_args2 = item.get("arguments")
                if isinstance(final_args2, str) and final_args2 and \
                        not tool_args_done.get(call_key):
                    tool_args_done[call_key] = True
                    td = {"index": info["idx"],
                          "function": {"arguments": final_args2}}
                    out.append("data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0,
                        "delta": {"tool_calls": [td]}, "finish_reason": None}]}) + "\n\n")
                if not tool_finished and tool_map.get(call_key) is info:
                    tool_finished = True
                    out.append("data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {},
                        "finish_reason": "tool_calls"}]}) + "\n\n")
        elif typ == "response.function_call_arguments.done":
            item = ev.get("item", {}) or {}
            item_id = item.get("id") or ev.get("item_id")
            call_key = (item.get("call_id") or item_to_call.get(item_id)
                        or active_tool or (next(iter(tool_map), None)))
            if not call_key:
                continue
            info = tool_map.setdefault(call_key, {
                "idx": len(tool_map), "id": call_key, "name": ""})
            if item.get("name") and not info["name"]:
                info["name"] = item.get("name")
            # Một số upstream chỉ gửi arguments trong event done, không có
            # delta — phát luôn như một argument chunk cuối.
            final_args = ev.get("arguments")
            if isinstance(final_args, str) and final_args and \
                    call_key in tool_map and \
                    final_args and not tool_args_done.get(call_key):
                tool_args_done[call_key] = True
                td = {"index": info["idx"],
                      "function": {"arguments": final_args}}
                out.append("data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0,
                    "delta": {"tool_calls": [td]}, "finish_reason": None}]}) + "\n\n")
            if info["idx"] not in emitted_tool_header and final_args:
                emitted_tool_header.add(info["idx"])
                out.append("data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": info["idx"], "id": info["id"], "type": "function",
                     "function": {"name": _client_tool_name(info["name"], client_tool_names), "arguments": ""}}]},
                    "finish_reason": None}]}) + "\n\n")
            if not tool_finished:
                tool_finished = True
                out.append("data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {},
                    "finish_reason": "tool_calls"}]}) + "\n\n")
        elif typ in ("response.completed", "response.done"):
            if not tool_finished:
                out.append("data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {},
                    "finish_reason": "stop"}]}) + "\n\n")
        elif typ == "response.incomplete":
            info = ev.get("response", ev)
            print("SSE_INCOMPLETE status=%s incomplete=%s error=%s" % (
                info.get("status"), info.get("incomplete_details"),
                info.get("error")), flush=True)
    print("SSE_TYPES " + ",".join(types) + " raw_bytes=" +
          str(len(sse.encode())), flush=True)
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


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
        redirect_responses = False  # chat→responses cho model chỉ hỗ trợ /responses
        client_tool_names = set()
        if body and "application/json" in ctype:
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    raw_tools = data.get("tools")
                    if isinstance(raw_tools, list):
                        for t in raw_tools:
                            if isinstance(t, dict):
                                fn = t.get("function") if isinstance(t.get("function"), dict) else t
                                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                                    client_tool_names.add(fn["name"])
                    # Ghi nhớ ý client TRƯỚC khi ép stream cho upstream.
                    want_json = data.get("stream") is not True
                    if not is_responses and REDIRECT_MODELS and \
                            data.get("model") in REDIRECT_MODELS:
                        # Model chỉ chạy trên /responses → convert và redirect.
                        data = messages_to_input(data, client_tool_names)
                        redirect_responses = True
                        is_responses = True
                        fixed = fix_tools(data, flat=True)
                    else:
                        fixed = fix_tools(data, flat=is_responses)
                        # Zen free tier requires the streaming/tool handshake.
                        data["stream"] = True
                        data["tool_choice"] = "auto"
                        fixed = True
                    body = json.dumps(data, separators=(",", ":")).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        up_path = self.path
        if redirect_responses:
            up_path = self.path.replace("/chat/completions", "/responses") \
                if "/chat/completions" in self.path else \
                self.path.rsplit("/v1/", 1)[0] + "/v1/responses"

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
            conn.request(self.command, up_path, body=body or None, headers=fwd)
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
              f"collect={want_json} client_tools={','.join(sorted(client_tool_names)) or '-'} "
              f"upstream_path={up_path}", flush=True)
        if dbg:
            print(f"BODY {self.path} :: {body[:900].decode('utf-8','replace')}",
                  flush=True)

        if not want_json or resp.status != 200:
            # Với chat→Responses và client stream=true, upstream trả Responses
            # SSE. Phải đổi về chat.completion SSE; nếu passthrough nguyên event
            # `response.output_text.delta`, 9Router kết luận stream rỗng.
            if redirect_responses and resp.status == 200:
                raw = resp.read()
                conn.close()
                out = responses_sse_to_chat_stream(
                    raw.decode("utf-8", "replace"), model or "unknown",
                    client_tool_names)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
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
            if redirect_responses:
                # Client gọi /chat/completions (đã convert sang /responses) →
                # phải trả về shape chat.completion, không phải Responses object.
                comp = responses_sse_to_chat(
                    raw.decode("utf-8", "replace"), model or "unknown",
                    client_tool_names)
            else:
                from aggregate_responses import sse_to_response
                obj = sse_to_response(raw.decode("utf-8", "replace"))
                obj["model"] = model or "unknown"
                out = json.dumps(obj).encode()
                code = 200
                comp = None
            if comp is not None:
                out = json.dumps(comp).encode()
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
