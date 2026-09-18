#!/usr/bin/env python3
"""Gom SSE /responses thành JSON object OpenAI Responses chuẩn.

Chạy cùng luồng client non-stream trên endpoint /v1/responses: shim đọc events,
gom text + tool_calls + status thành object dạng {"id","object":"response",...}.
"""
import json, time
import sys
sys.path.insert(0, '/app')
from inject import gen_id


def sse_to_response(raw):
    rid, status = None, None
    texts, reasons = [], []
    items = []          # output item theo thứ tự
    usage = None
    cur_text_item = {}  # id/type của item text đang ghi
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type", "")
        resp = ev.get("response") or {}
        if resp.get("id"):
            rid = resp["id"]
        if resp.get("status"):
            status = resp["status"]
        if etype == "response.output_item.added":
            it = ev.get("item") or {}
            items.append({"id": it.get("id"), "type": it.get("type"),
                          "role": it.get("role"), "content": []})
        elif etype == "response.output_text.delta":
            d = ev.get("delta")
            if isinstance(d, str) and items:
                items[-1]["content"].append({"type": "output_text", "text": d})
        elif etype == "response.reasoning_summary_text.delta":
            d = ev.get("delta")
            if isinstance(d, str):
                reasons.append(d)
        elif etype == "response.function_call_arguments.delta":
            # Tool call streaming — gom riêng
            if items and items[-1].get("type") == "function_call":
                items[-1].setdefault("_args", []).append(ev.get("delta") or "")
            else:
                items.append({"id": ev.get("item_id"), "type": "function_call",
                              "_args": [ev.get("delta") or ""]})
        elif etype == "response.function_call_arguments.done":
            if items and items[-1].get("type") == "function_call":
                fc = items[-1]
                fc["arguments"] = (fc.pop("_args", None) and
                                   "".join(fc.pop("_args"))) or ""
                fc.pop("_args", None)
        elif etype == "response.completed":
            r = ev.get("response") or {}
            usage = r.get("usage") or usage
            status = r.get("status") or status
            rid = r.get("id") or rid
            # Items hoàn chỉnh từ event cuối phản ánh trạng thái chính xác
            if r.get("output"):
                items = r["output"]
        elif etype == "response.failed" or etype == "response.error":
            status = ev.get("response", {}).get("status") or "failed"
    # Dọn _args còn sót
    for it in items:
        if isinstance(it, dict) and "_args" in it:
            it["arguments"] = "".join(it.pop("_args"))
    if not items:
        items = [{"id": gen_id("msg"), "type": "message", "role": "assistant",
                  "status": "completed",
                  "content": [{"type": "output_text",
                               "text": "".join(texts)}]}]
    out = {"id": rid or gen_id("msg"), "object": "response",
           "created_at": int(time.time()), "status": status or "completed",
           "output": items, "model": None, "parallel_tool_calls": True,
           "error": None, "incomplete_details": None,
           "usage": usage}
    return out


if __name__ == "__main__":
    demo = sys.stdin.read()
    print(json.dumps(sse_to_response(demo), ensure_ascii=False))
