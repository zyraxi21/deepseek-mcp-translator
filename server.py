#!/usr/bin/env python3
"""
DeepSeek Role Translator
=========================
HTTP 代理 + MCP 服务器。Android Studio 发来的 developer 角色消息会被自动
翻译成 DeepSeek 认识的 system 角色。

用法：
    python server.py              # stdio 模式（MCP）
    python server.py --http       # HTTP 模式（代理 + MCP）
"""

import base64
import json
import os
import traceback
from datetime import datetime

import httpx
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

# ── 加载配置 ──────────────────────────────────────────────────────────────────
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


# ── 角色翻译 ──────────────────────────────────────────────────────────────────
def translate_messages(messages: list[dict]) -> list[dict]:
    for msg in messages:
        if msg.get("role") == "developer":
            msg["role"] = "system"
    return messages


# ── 诊断日志（默认关闭，设环境变量 PROXY_DEBUG=1 打开）───────────────────────
DEBUG = os.getenv("PROXY_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
DEBUG_LOG = os.getenv("PROXY_DEBUG_LOG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "proxy-debug.log"
)


def _image_brief(url) -> str:
    """描述一个图片字段：data URL 给出 mime、长度和文件头，其他给出前缀。"""
    if not isinstance(url, str):
        return f"非字符串（{type(url).__name__}）"
    if not url.startswith("data:"):
        return f"非 data URL：{url[:80]!r}"
    head, _, payload = url.partition(",")
    mime = head[5:].split(";")[0]
    try:
        raw = base64.b64decode(payload[:96], validate=False)[:12]
        magic = " ".join(f"{b:02x}" for b in raw)
    except Exception:
        magic = "<无法解码>"
    return f"mime={mime} base64 长度={len(payload)} 前 12 字节={magic}"


def _scan_images(node, path="", out=None):
    """递归找出请求体里的图片块，返回 (JSON 路径, 图片地址) 列表。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        if node.get("type") in ("input_image", "image_url", "image"):
            url = node.get("image_url") or node.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            out.append((path or "<根>", url))
        for key, value in node.items():
            _scan_images(value, f"{path}.{key}", out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_images(value, f"{path}[{index}]", out)
    return out


def _log_request(path: str, body: bytes, status=None, response_text: str = "") -> None:
    if not DEBUG:
        return
    try:
        data = json.loads(body or b"{}")
    except Exception:
        data = None

    lines = [f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {path} -> {status if status is not None else '未回包'}"]
    if isinstance(data, dict):
        reasoning = data.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        lines.append(f"  model={data.get('model')} stream={data.get('stream')} effort={effort}")
        override = _current_effort()
        if override:
            lines.append(f"  思考强度被代理覆盖为 {override}")
        items = data.get("input")
        if isinstance(items, list):
            kinds = {}
            for item in items:
                if isinstance(item, dict):
                    kind = item.get("type", "message")
                else:
                    kind = type(item).__name__
                kinds[kind] = kinds.get(kind, 0) + 1
            lines.append(f"  input 项数={len(items)} 类型分布={kinds}")
        messages = data.get("messages")
        if isinstance(messages, list):
            roles = {}
            for message in messages:
                role = message.get("role")
                roles[role] = roles.get(role, 0) + 1
            lines.append(f"  messages 条数={len(messages)} 角色分布={roles}")
        images = _scan_images(data)
        lines.append(f"  图片块={len(images)}")
        for image_path, url in images:
            lines.append(f"    {image_path} -> {_image_brief(url)}")
    else:
        lines.append(f"  请求体无法解析为 JSON，长度={len(body)}")
    if response_text:
        lines.append(f"  上游返回：{response_text[:400]}")
    lines.append("")
    with open(DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── 思考强度接管 ──────────────────────────────────────────────────────────────
# Android Studio 的内置 Agent 不提供思考强度选项，这里由代理统一覆盖。
# 取值：none / low / high / max（也接受 minimal、medium、xhigh 等别名）。
# 优先级：thinking-effort.txt 第一行 > 环境变量 THINKING_EFFORT > 不干预。
THINKING_EFFORT = (os.getenv("THINKING_EFFORT") or os.getenv("RESPONSES_EFFORT", "")).strip().lower()
EFFORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thinking-effort.txt")
EFFORT_OFF = {"none", "off", "disabled", "0", "false"}


def _current_effort() -> str:
    """每次请求都读一次，改文件即可即时生效，不用重启代理。"""
    try:
        if os.path.exists(EFFORT_FILE):
            with open(EFFORT_FILE, encoding="utf-8") as f:
                for line in f:
                    value = line.strip().lower()
                    if value:
                        return value
    except Exception:
        pass
    return THINKING_EFFORT


def _apply_thinking_chat(data: dict, effort: str) -> None:
    """Chat Completions：thinking 开关 + reasoning_effort 强度。"""
    if not effort:
        return
    if effort in EFFORT_OFF:
        data["thinking"] = {"type": "disabled"}
        data.pop("reasoning_effort", None)
    else:
        data["thinking"] = {"type": "enabled"}
        data["reasoning_effort"] = effort


# ── 代理请求 ──────────────────────────────────────────────────────────────────
def _proxy_headers():
    return {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


async def _proxy_chat(request: Request) -> Response:
    body = await request.body()
    data = json.loads(body)

    if "messages" in data:
        data["messages"] = translate_messages(data["messages"])
    if not data.get("model"):
        data["model"] = DEEPSEEK_MODEL
    _apply_thinking_chat(data, _current_effort())

    is_stream = data.get("stream", False)

    if is_stream:
        async def streamer():
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", f"{DEEPSEEK_BASE_URL}/chat/completions",
                    headers=_proxy_headers(), json=data,
                ) as resp:
                    _log_request("/v1/chat/completions", body, resp.status_code)
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(streamer(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=_proxy_headers(), json=data,
        )
    _log_request("/v1/chat/completions", body, resp.status_code,
                 resp.text if resp.status_code >= 400 else "")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type="application/json")


async def _proxy_other(request: Request) -> Response:
    body = await request.body()
    path = request.url.path
    query = str(request.url.query)
    url = f"{path}?{query}" if query else path

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(
            method=request.method, url=f"{DEEPSEEK_BASE_URL}{url}",
            headers=_proxy_headers(), content=body or None,
        )
    _log_request(path, body, resp.status_code,
                 resp.text if resp.status_code >= 400 else "")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("Content-Type", "application/json"))


# ── Responses 专用转发 ────────────────────────────────────────────────────────
# 客户端走 /v1/responses 时，思考模式要求每个工具调用轮都带 reasoning 文本，
# 客户端常常漏传，上游就返回 400。代理这里做两件事：
#   1. 记住上游返回的真实 reasoning，下次请求时按 call_id 回填；
#   2. 实在补不上，就写一段占位文本，保证请求能过。
# 最后仍有 400 reasoning_text 时，才退回到"关掉思考重试一次"。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REASONING_CACHE: dict[str, str] = {}
REASONING_CACHE_MAX = 800
REASONING_PLACEHOLDER = "（该轮思考内容未由客户端回传，按上一步结果继续。）"


def _dump_request(body: bytes, reason: str) -> None:
    """把触发失败的请求体存到项目目录，文件名带时间戳，已在 .gitignore 里。"""
    path = os.path.join(BASE_DIR, f"proxy-request-{datetime.now():%Y%m%d-%H%M%S}.json")
    try:
        with open(path, "wb") as f:
            f.write(body)
        print(f"[responses] 已保存触发「{reason}」的请求体：{path}")
    except Exception as exc:
        print(f"[responses] 保存请求体失败：{exc}")


def _set_effort(data: dict, effort: str) -> dict:
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
    reasoning["effort"] = effort
    data["reasoning"] = reasoning
    return data


def _reasoning_text_of(item) -> str:
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return ""
    return "".join(
        part.get("text") or ""
        for part in (item.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "reasoning_text"
    )


def _remember_reasoning(response_obj) -> None:
    """从上游返回里记住每轮思考，供下一次请求按 call_id 回填。"""
    if not isinstance(response_obj, dict):
        return
    last = ""
    for item in response_obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "reasoning":
            text = _reasoning_text_of(item)
            if text:
                last = text
        elif kind == "function_call" and last:
            call_id = item.get("call_id")
            if call_id:
                REASONING_CACHE[call_id] = last
    if len(REASONING_CACHE) > REASONING_CACHE_MAX:
        for stale in list(REASONING_CACHE)[: len(REASONING_CACHE) - REASONING_CACHE_MAX]:
            REASONING_CACHE.pop(stale, None)


def _inject_reasoning(data: dict) -> int:
    """给缺少 reasoning 的工具调用轮补一段，返回补了几条。"""
    items = data.get("input")
    if not isinstance(items, list):
        return 0

    rebuilt = []
    last_text = ""
    added = 0
    for item in items:
        if not isinstance(item, dict):
            rebuilt.append(item)
            continue
        kind = item.get("type", "message")
        if kind == "reasoning":
            text = _reasoning_text_of(item)
            if text:
                last_text = text
        elif kind == "function_call":
            previous = rebuilt[-1] if rebuilt else None
            previous_kind = previous.get("type") if isinstance(previous, dict) else None
            # 连续并行的工具调用共用一条 reasoning，插在中间会打乱 call 与 output 的配对
            if previous_kind not in ("reasoning", "function_call"):
                call_id = item.get("call_id") or ""
                text = REASONING_CACHE.get(call_id) or last_text or REASONING_PLACEHOLDER
                rebuilt.append({
                    "type": "reasoning",
                    "id": f"proxy-r-{call_id or added}",
                    "content": [{"type": "reasoning_text", "text": text}],
                })
                last_text = text
                added += 1
        rebuilt.append(item)

    if added:
        data["input"] = rebuilt
    return added


def _thinking_is_off(data) -> bool:
    if not isinstance(data, dict):
        return False
    reasoning = data.get("reasoning")
    return isinstance(reasoning, dict) and reasoning.get("effort") == "none"


async def _open_responses(body: bytes):
    """发起请求但不读响应体，返回 (client, response)，两者都需要关闭。"""
    client = httpx.AsyncClient(timeout=120.0)
    request = client.build_request(
        "POST", f"{DEEPSEEK_BASE_URL}/responses",
        headers=_proxy_headers(), content=body,
    )
    response = await client.send(request, stream=True)
    return client, response


def _remember_from_sse(raw: bytes) -> None:
    """从流式 SSE 的收尾事件里取完整 response，记住这一轮的 reasoning。"""
    if not raw:
        return
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if b"response.completed" not in payload and b"response.incomplete" not in payload:
            continue
        try:
            event = json.loads(payload)
        except Exception:
            continue
        response_obj = event.get("response")
        if isinstance(response_obj, dict):
            _remember_reasoning(response_obj)


async def _proxy_responses(request: Request) -> Response:
    original = await request.body()
    try:
        data = json.loads(original)
    except Exception:
        data = None

    effort = _current_effort()
    body = original
    if isinstance(data, dict):
        if effort:
            _set_effort(data, "none" if effort in EFFORT_OFF else effort)
        injected = 0 if effort in EFFORT_OFF else _inject_reasoning(data)
        if injected:
            print(f"[responses] 回填了 {injected} 条 reasoning")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    is_stream = isinstance(data, dict) and bool(data.get("stream"))

    client, upstream = await _open_responses(body)

    # 思考模式缺 reasoning 文本时，降级为不思考再试一次
    if upstream.status_code == 400 and not _thinking_is_off(data) and not effort:
        error_body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        if b"reasoning_text" in error_body:
            _dump_request(original, "reasoning_text")
            if not isinstance(data, dict):
                data = {}
            print("[responses] 上游要求回传 reasoning 文本，关闭思考后重试")
            data = _set_effort(data, "none")
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            client, upstream = await _open_responses(body)
        else:
            _log_request("/v1/responses", original, 400,
                         error_body.decode("utf-8", "replace"))
            return Response(content=error_body, status_code=400,
                            media_type="application/json")

    if upstream.status_code != 200:
        error_body = await upstream.aread()
        content_type = upstream.headers.get("content-type", "application/json")
        await upstream.aclose()
        await client.aclose()
        _log_request("/v1/responses", original, upstream.status_code,
                     error_body.decode("utf-8", "replace"))
        return Response(content=error_body, status_code=upstream.status_code,
                        media_type=content_type)

    _log_request("/v1/responses", original, 200)

    if not is_stream:
        content = await upstream.aread()
        content_type = upstream.headers.get("content-type", "application/json")
        await upstream.aclose()
        await client.aclose()
        try:
            _remember_reasoning(json.loads(content))
        except Exception:
            pass
        return Response(content=content, status_code=200, media_type=content_type)

    content_type = upstream.headers.get("content-type", "text/event-stream")

    async def relay():
        collected = bytearray()
        try:
            async for chunk in upstream.aiter_bytes():
                collected.extend(chunk)
                yield chunk
        finally:
            try:
                _remember_from_sse(bytes(collected))
            except Exception:
                pass
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), status_code=200, media_type=content_type)


# ── MCP 服务器 ────────────────────────────────────────────────────────────────
from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

mcp = MCPServer(
    name="deepseek_mcp",
    title="DeepSeek Role Translator",
    description="把 developer 角色翻译成 system，透明代理 DeepSeek API",
)


class Message(BaseModel):
    role: str = Field(..., description="system / user / assistant / developer")
    content: str = Field(...)


@mcp.tool(
    name="deepseek_chat",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=False, open_world_hint=True,
    ),
)
async def deepseek_chat(
    messages: list[Message],
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str:
    translated = translate_messages(
        [{"role": m.role, "content": m.content} for m in messages]
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=_proxy_headers(),
            json={
                "model": model or DEEPSEEK_MODEL,
                "messages": translated,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
    data = resp.json()
    return data["choices"][0]["message"]["content"] or ""


# ── ASGI 调度 ─────────────────────────────────────────────────────────────────
_mcp_app = None


def _get_mcp_app():
    """返回全局唯一的 MCP Starlette 应用。

    必须只创建一次：streamable_http_app() 每次调用都会新建一个
    StreamableHTTPSessionManager，而 manager 内部的 task group 依赖 Starlette
    的 lifespan 启动，且 run() 生命周期内只能调用一次。
    """
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = mcp.streamable_http_app()
    return _mcp_app


async def dispatch(scope, receive, send):
    # lifespan 必须转发给 Starlette 应用，由它执行 session_manager.run()，
    # 否则处理请求时会抛 "Task group is not initialized. Make sure to use run()."
    if scope["type"] == "lifespan":
        await _get_mcp_app()(scope, receive, send)
        return

    if scope["type"] == "http":
        path = scope["path"]
        method = scope["method"]
        request = Request(scope, receive)

        if path == "/v1/chat/completions" and method == "POST":
            try:
                response = await _proxy_chat(request)
            except Exception:
                traceback.print_exc()
                response = Response(
                    content=json.dumps({"error": traceback.format_exc()}),
                    status_code=500, media_type="application/json",
                )
        elif path == "/v1/responses" and method == "POST":
            try:
                response = await _proxy_responses(request)
            except Exception:
                traceback.print_exc()
                response = Response(
                    content=json.dumps({"error": traceback.format_exc()}),
                    status_code=500, media_type="application/json",
                )
        elif path.startswith("/v1/"):
            response = await _proxy_other(request)
        else:
            # 其他请求交给 MCP 处理
            await _get_mcp_app()(scope, receive, send)
            return

        await response(scope, receive, send)


# ── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="DeepSeek Role Translator")
    parser.add_argument("--http", action="store_true", help="HTTP 模式")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    args = parser.parse_args()

    if args.http:
        print(f"代理 + MCP 已启动: http://localhost:{args.port}")
        print(f"  代理端点:     http://localhost:{args.port}/v1")
        print(f"  MCP 端点:     http://localhost:{args.port}/mcp")
        uvicorn.run(dispatch, host="127.0.0.1", port=args.port, log_level="info")
    else:
        mcp.run()
