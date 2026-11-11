"""Allow-list for metered-session calls (review finding C-02).

A session call is an OpenAI-compatible request made with the session's
limited token. Iteration 01 forwarded the client's body to the upstream
almost verbatim, so fields such as ``n`` or ``max_completion_tokens`` let a
small session buy an arbitrary amount of upstream work. Now:

* Only the parameters listed below are accepted; anything else is refused
  with 400 ``parameter_not_allowed`` (never silently dropped, so a client is
  not surprised by different semantics).
* ``n`` must be absent or 1 and is forwarded as 1 (chat).
* The output limit (``max_tokens`` / ``max_completion_tokens`` for chat,
  ``max_output_tokens`` for responses) is always written by the gateway from
  the remaining budget; a client value can only lower it.
* Message/input content is text only. Images, audio, files and references to
  upstream-stored state (``previous_response_id``, ``conversation``,
  ``prompt``, ``file_id``) are refused because their token cost is not bounded
  by the request bytes, which is what the reservation is computed from.
* Tools are function definitions only; built-in tools (web search, file
  search, code interpreter, ...) are billed separately by providers. A
  ``namespace`` tool (a named group of function tools) is accepted: it is only
  a grouping of function definitions.
* Round 03 (contract §5.3, the built-in engineering runtime — Codex 0.146.1
  app-server — through a session): ``store`` (only ``false``), ``include``
  (only ``reasoning.encrypted_content``), ``prompt_cache_key`` (re-keyed per
  organisation by the gateway), ``client_metadata`` (validated, **never
  forwarded**), ``reasoning`` input items and list-shaped
  ``function_call_output.output`` (text parts only) are accepted.

Evaluation of every parameter (kept / refused and why) is in cloud/README.md.
"""
from __future__ import annotations

import hashlib
import json
import math

from .catalog import INPUT_TOKEN_OVERHEAD
from .errors import ApiError, bad_request

MAX_SESSION_TOKENS = 5_000_000
MAX_RESPONSES_INPUT_ITEMS = 4096

# name -> validator. Output-limit, n and stream are handled separately.
_STR = (str,)


def _num(low: float, high: float):
    def check(name, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not low <= value <= high:
            raise bad_request(f"{name} 须为 {low}–{high} 的数值", code="invalid_parameter")
    return check


def _int(low: int, high: int):
    def check(name, value):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise bad_request(f"{name} 须为 {low}–{high} 的整数", code="invalid_parameter")
    return check


def _bool(name, value):
    if not isinstance(value, bool):
        raise bad_request(f"{name} 须为布尔值", code="invalid_parameter")


def _short_str(limit: int):
    def check(name, value):
        if not isinstance(value, str) or len(value) > limit:
            raise bad_request(f"{name} 须为 ≤{limit} 字符的字符串", code="invalid_parameter")
    return check


def _stop(name, value):
    if isinstance(value, str) and len(value) <= 200:
        return
    if isinstance(value, list) and len(value) <= 4 and all(isinstance(v, str) and len(v) <= 200 for v in value):
        return
    raise bad_request("stop 须为字符串或 ≤4 个字符串的列表", code="invalid_parameter")


def _dict(name, value):
    if not isinstance(value, dict):
        raise bad_request(f"{name} 须为对象", code="invalid_parameter")


_FUNCTION_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")


def _keys(where: str, value, allowed: set[str], required: set[str] = frozenset(), code: str = "invalid_parameter"):
    """Strict nested allow-list (rereview N-09): a dict whose keys are a subset of ``allowed``."""
    if not isinstance(value, dict):
        raise bad_request(f"{where} 须为对象", code=code)
    extra = sorted(set(value) - allowed)
    if extra:
        raise ApiError(400, "parameter_not_allowed", f"{where} 不接受这些字段：{extra}（允许：{sorted(allowed)}）",
                       parameters=[f"{where}.{k}" for k in extra])
    missing = sorted(required - set(value))
    if missing:
        raise bad_request(f"{where} 缺少字段：{missing}", code=code)


def _function_name(where: str, value) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not set(value) <= _FUNCTION_NAME_CHARS:
        raise bad_request(f"{where} 须为 1–64 个字母、数字或 _-.: 组成的函数名", code="invalid_parameter")


def _text(where: str, value, limit: int) -> None:
    if not isinstance(value, str) or len(value) > limit:
        raise bad_request(f"{where} 须为 ≤{limit} 字符的字符串", code="invalid_parameter")


def _json_schema_obj(where: str, value) -> None:
    # A JSON Schema is free-form by nature; it must be an object and its bytes
    # count towards the input bound like everything else that is forwarded.
    if not isinstance(value, dict):
        raise bad_request(f"{where} 须为 JSON Schema 对象", code="invalid_parameter")


def _strict_flag(where: str, value) -> None:
    if value is not None and not isinstance(value, bool):
        raise bad_request(f"{where} 须为布尔值", code="invalid_parameter")


def _chat_tool_choice(name, value):
    if isinstance(value, str) and value in ("none", "auto", "required"):
        return
    if isinstance(value, dict) and value.get("type") == "function":
        _keys(name, value, {"type", "function"}, {"type", "function"})
        _keys(f"{name}.function", value["function"], {"name"}, {"name"})
        _function_name(f"{name}.function.name", value["function"]["name"])
        return
    raise bad_request("tool_choice 须为 none / auto / required 或 {type: function, function: {name}}",
                      code="invalid_parameter")


def _responses_tool_choice(name, value):
    if isinstance(value, str) and value in ("none", "auto", "required"):
        return
    if isinstance(value, dict) and value.get("type") == "function":
        _keys(name, value, {"type", "name"}, {"type", "name"})
        _function_name(f"{name}.name", value["name"])
        return
    raise bad_request("tool_choice 须为 none / auto / required 或 {type: function, name}", code="invalid_parameter")


def _chat_tools(name, value):
    if not isinstance(value, list) or len(value) > 128:
        raise bad_request("tools 须为 ≤128 项的列表", code="invalid_parameter")
    for i, tool in enumerate(value):
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise bad_request("tools 只支持 {type: function, function: {...}}（不支持内置工具）", code="tool_not_allowed")
        where = f"tools[{i}]"
        _keys(where, tool, {"type", "function"}, {"type", "function"})
        fn = tool["function"]
        _keys(f"{where}.function", fn, {"name", "description", "parameters", "strict"}, {"name"})
        _function_name(f"{where}.function.name", fn["name"])
        if "description" in fn:
            _text(f"{where}.function.description", fn["description"], 100_000)
        if "parameters" in fn:
            _json_schema_obj(f"{where}.function.parameters", fn["parameters"])
        _strict_flag(f"{where}.function.strict", fn.get("strict"))


def _responses_function(where: str, tool) -> None:
    if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("name"), str):
        raise bad_request("tools 只支持函数工具 {type: function, name, parameters}（不支持 web_search 等内置工具）",
                          code="tool_not_allowed")
    _keys(where, tool, {"type", "name", "description", "parameters", "strict"}, {"type", "name"})
    _function_name(f"{where}.name", tool["name"])
    if "description" in tool:
        _text(f"{where}.description", tool["description"], 100_000)
    if "parameters" in tool:
        _json_schema_obj(f"{where}.parameters", tool["parameters"])
    _strict_flag(f"{where}.strict", tool.get("strict"))


def _responses_tools(name, value):
    if not isinstance(value, list) or len(value) > 128:
        raise bad_request("tools 须为 ≤128 项的列表", code="invalid_parameter")
    for i, tool in enumerate(value):
        where = f"tools[{i}]"
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            # Round 03: a named group of function tools (Codex 0.146.1 sends its
            # sub-agent tools this way). Only function tools inside, no nesting.
            _keys(where, tool, {"type", "name", "description", "tools"}, {"type", "name", "tools"})
            _function_name(f"{where}.name", tool["name"])
            if "description" in tool:
                _text(f"{where}.description", tool["description"], 100_000)
            inner = tool["tools"]
            if not isinstance(inner, list) or not 1 <= len(inner) <= 128:
                raise bad_request(f"{where}.tools 须为 1–128 项的列表", code="invalid_parameter")
            for j, fn in enumerate(inner):
                _responses_function(f"{where}.tools[{j}]", fn)
            continue
        _responses_function(where, tool)
    if sum(len(t["tools"]) if t.get("type") == "namespace" else 1 for t in value) > 128:
        raise bad_request("tools（含命名空间内的函数）合计不能超过 128 个", code="invalid_parameter")


def tool_count(tools) -> int:
    """Function definitions in ``tools``, namespaces flattened (each gets the template overhead)."""
    return sum(len(t.get("tools") or []) if isinstance(t, dict) and t.get("type") == "namespace" else 1
               for t in tools or [])


def _schema_format(where: str, value, nested: bool) -> None:
    """``response_format`` (chat, schema nested under ``json_schema``) / ``text.format`` (responses, flat)."""
    if not isinstance(value, dict) or value.get("type") not in ("text", "json_object", "json_schema"):
        raise bad_request(f"{where} 须为 {{type: text | json_object | json_schema}}", code="invalid_parameter")
    if value["type"] != "json_schema":
        _keys(where, value, {"type"}, {"type"})
        return
    if nested:
        _keys(where, value, {"type", "json_schema"}, {"type", "json_schema"})
        where, value = f"{where}.json_schema", value["json_schema"]
        _keys(where, value, {"name", "description", "schema", "strict"}, {"name"})
    else:
        _keys(where, value, {"type", "name", "description", "schema", "strict"}, {"type", "name"})
    _function_name(f"{where}.name", value["name"])
    if "description" in value:
        _text(f"{where}.description", value["description"], 100_000)
    if "schema" in value:
        _json_schema_obj(f"{where}.schema", value["schema"])
    _strict_flag(f"{where}.strict", value.get("strict"))


def _response_format(name, value):
    _schema_format(name, value, nested=True)


def _responses_text(name, value):
    _keys(name, value, {"format", "verbosity"})
    if "format" in value:
        _schema_format(f"{name}.format", value["format"], nested=False)
    if "verbosity" in value and value["verbosity"] not in ("low", "medium", "high"):
        raise bad_request(f"{name}.verbosity 须为 low / medium / high", code="invalid_parameter")


_INCLUDE_ALLOWED = ("reasoning.encrypted_content",)


def _store(name, value):
    # store=true keeps the request/response on the operator's provider account
    # (and enables previous_response_id state, which is refused anyway).
    if value is not False:
        raise ApiError(400, "parameter_not_allowed", "store 只接受 false（会话调用不在平台的上游账户下保存数据）",
                       parameters=["store"])


def _include(name, value):
    # Only the encrypted reasoning of *this* response (its tokens are already
    # output tokens). Other include values return stored files, search
    # results, image URLs or logprobs of built-in tools.
    if not isinstance(value, list) or len(value) > len(_INCLUDE_ALLOWED) \
            or not all(isinstance(v, str) and v in _INCLUDE_ALLOWED for v in value):
        raise ApiError(400, "parameter_not_allowed", f"include 只接受 {list(_INCLUDE_ALLOWED)}",
                       parameters=["include"])


def _cache_key(name, value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise bad_request(f"{name} 须为 1–256 字符的字符串", code="invalid_parameter")


def _client_metadata(name, value):
    # Client-side telemetry (installation / thread ids). Validated so it cannot
    # carry large payloads, then dropped: it does not change the generation,
    # and device identifiers are not passed on to the provider.
    if not isinstance(value, dict) or len(value) > 32:
        raise bad_request(f"{name} 须为 ≤32 项的对象", code="invalid_parameter")
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64 or not isinstance(item, str) or len(item) > 4096:
            raise bad_request(f"{name} 的键须为 ≤64 字符、值须为 ≤4096 字符的字符串", code="invalid_parameter")


def scoped_cache_key(org_id: str, key: str) -> str:
    """The prompt cache key actually sent upstream: one provider account serves
    every tenant, so a client-chosen key is namespaced by organisation (a
    tenant cannot steer its requests onto another tenant's cache routing)."""
    return "nx-" + hashlib.sha256(f"{org_id}\0{key}".encode()).hexdigest()[:40]


def _reasoning(name, value):
    if not isinstance(value, dict) or not set(value) <= {"effort", "summary"} \
            or not all(isinstance(v, str) and len(v) <= 20 for v in value.values()):
        raise bad_request("reasoning 只接受 {effort, summary}", code="invalid_parameter")


COMMON = {
    "temperature": _num(0, 2),
    "top_p": _num(0, 1),
    "parallel_tool_calls": _bool,
    "user": _short_str(256),
}
CHAT_PARAMS = {
    **COMMON,
    "stop": _stop,
    "tools": _chat_tools,
    "tool_choice": _chat_tool_choice,
    "response_format": _response_format,
    "seed": _int(-(2 ** 63), 2 ** 63 - 1),
    "presence_penalty": _num(-2, 2),
    "frequency_penalty": _num(-2, 2),
    "logprobs": _bool,
    "top_logprobs": _int(0, 20),
    "reasoning_effort": _short_str(20),
}
RESPONSES_PARAMS = {
    **COMMON,
    "instructions": _short_str(400_000),
    "tools": _responses_tools,
    "tool_choice": _responses_tool_choice,
    "text": _responses_text,
    "reasoning": _reasoning,
    # Round 03 (§5.3): what Codex 0.146.1 sends on every request.
    "store": _store,
    "include": _include,
    "prompt_cache_key": _cache_key,
    "client_metadata": _client_metadata,
}
# Validated, then removed from what is forwarded (and from the input bound).
RESPONSES_STRIPPED = ("client_metadata",)
# Accepted and then overwritten / fixed by the gateway.
CHAT_CONTROL = ("model", "stream", "n", "max_tokens", "max_completion_tokens")
RESPONSES_CONTROL = ("model", "stream", "max_output_tokens")

_CHAT_ROLES = ("system", "developer", "user", "assistant", "tool")
_CHAT_MESSAGE_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id", "refusal"}
_CHAT_PART_TYPES = ("text", "refusal")
_RESP_PART_TYPES = ("input_text", "output_text", "refusal", "text")


def _refuse_content(where: str) -> ApiError:
    return bad_request(f"{where}：会话 v1 只接受文本内容（图片/音频/文件等的 token 成本无法从请求字节数界定）",
                       code="content_not_allowed")


# Per content-part type: the exact keys accepted (rereview N-09).
_PART_KEYS = {
    "text": {"type", "text"},
    "input_text": {"type", "text"},
    "output_text": {"type", "text", "annotations"},
    "refusal": {"type", "refusal"},
}


def _text_parts(parts, allowed: tuple[str, ...], where: str) -> None:
    if not isinstance(parts, list):
        raise _refuse_content(where)
    for j, part in enumerate(parts):
        if not isinstance(part, dict) or part.get("type") not in allowed:
            raise _refuse_content(where)
        kind = part["type"]
        _keys(f"{where}.content[{j}]", part, _PART_KEYS[kind], {"type"})
        field_name = "refusal" if kind == "refusal" else "text"
        _text(f"{where}.content[{j}].{field_name}", part.get(field_name), 400_000)
        if "annotations" in part and part["annotations"] != []:
            # Citations only come from built-in tools, which sessions do not allow.
            raise bad_request(f"{where}.content[{j}].annotations 只接受空列表", code="invalid_parameter")


def _check_tool_calls(where: str, calls) -> None:
    if not isinstance(calls, list) or len(calls) > 128:
        raise bad_request(f"{where}.tool_calls 只支持函数调用", code="invalid_parameter")
    for k, call in enumerate(calls):
        here = f"{where}.tool_calls[{k}]"
        if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
            raise bad_request(f"{where}.tool_calls 只支持函数调用", code="invalid_parameter")
        _keys(here, call, {"id", "type", "function"}, {"id", "type", "function"})
        _text(f"{here}.id", call["id"], 200)
        _keys(f"{here}.function", call["function"], {"name", "arguments"}, {"name", "arguments"})
        _function_name(f"{here}.function.name", call["function"]["name"])
        _text(f"{here}.function.arguments", call["function"]["arguments"], 400_000)


def _check_chat_messages(messages) -> int:
    if not isinstance(messages, list) or not messages or len(messages) > 1000:
        raise bad_request("messages 须为 1–1000 项的列表")
    for i, message in enumerate(messages):
        where = f"messages[{i}]"
        if not isinstance(message, dict) or message.get("role") not in _CHAT_ROLES or not set(message) <= _CHAT_MESSAGE_KEYS:
            raise bad_request(f"{where} 须为 {{role: {'|'.join(_CHAT_ROLES)}, content}}，且只含 {sorted(_CHAT_MESSAGE_KEYS)}",
                              code="invalid_parameter")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            _text_parts(content, _CHAT_PART_TYPES, where)
        if message.get("tool_calls") is not None:
            _check_tool_calls(where, message["tool_calls"])
        if message.get("name") is not None:
            _function_name(f"{where}.name", message["name"])
        if message.get("tool_call_id") is not None:
            _text(f"{where}.tool_call_id", message["tool_call_id"], 200)
        if message.get("refusal") is not None:
            _text(f"{where}.refusal", message["refusal"], 400_000)
    return len(messages)


def _check_responses_input(value) -> int:
    if isinstance(value, str):
        if not value:
            raise bad_request("input 不能为空")
        return 1
    # Review Q3-07: a tool-loop runtime resends every item each turn (two per
    # tool call), so 1000 items ended long sessions early. Each item still adds
    # its bytes plus the template overhead to the input bound.
    if not isinstance(value, list) or not value or len(value) > MAX_RESPONSES_INPUT_ITEMS:
        raise bad_request(f"input 须为字符串或 1–{MAX_RESPONSES_INPUT_ITEMS} 项的列表")
    for i, item in enumerate(value):
        where = f"input[{i}]"
        if not isinstance(item, dict):
            raise bad_request(f"{where} 须为对象", code="invalid_parameter")
        kind = item.get("type", "message")
        if kind == "message":
            if item.get("role") not in ("system", "developer", "user", "assistant") \
                    or not set(item) <= {"type", "role", "content", "id", "status"}:
                raise bad_request(f"{where} 消息须为 {{role, content}}", code="invalid_parameter")
            if not isinstance(item.get("content"), str):
                _text_parts(item.get("content"), _RESP_PART_TYPES, where)
        elif kind == "function_call":
            if not set(item) <= {"type", "id", "call_id", "name", "arguments", "status", "namespace"}:
                raise bad_request(f"{where} function_call 字段无效", code="invalid_parameter")
            _text(f"{where}.call_id", item.get("call_id"), 200)
            _function_name(f"{where}.name", item.get("name"))
            _text(f"{where}.arguments", item.get("arguments"), 400_000)
            if item.get("namespace") is not None:
                _function_name(f"{where}.namespace", item["namespace"])
        elif kind == "function_call_output":
            output = item.get("output")
            if not set(item) <= {"type", "id", "call_id", "output", "status"} or not isinstance(output, (str, list)):
                raise bad_request(f"{where} function_call_output 须带字符串或文本片段列表 output", code="invalid_parameter")
            _text(f"{where}.call_id", item.get("call_id"), 200)
            if isinstance(output, list):
                # Tool results as content parts: text only (an image result's
                # token cost is not bounded by its bytes).
                _text_parts(output, ("input_text",), f"{where}.output")
        elif kind == "reasoning":
            # A reasoning item returned by the previous response, sent back in
            # a tool loop (store=false). Its summary/text bytes and the opaque
            # encrypted blob count towards the input bound like everything else;
            # if a provider bills it above that bound, ADR-012 applies.
            _keys(where, item, {"type", "id", "summary", "content", "encrypted_content", "status"}, {"type"})
            summary = item.get("summary", [])
            if not isinstance(summary, list) or len(summary) > 100:
                raise bad_request(f"{where}.summary 须为列表", code="invalid_parameter")
            for j, part in enumerate(summary):
                _keys(f"{where}.summary[{j}]", part, {"type", "text"}, {"type", "text"})
                if part["type"] != "summary_text":
                    raise bad_request(f"{where}.summary[{j}].type 须为 summary_text", code="invalid_parameter")
                _text(f"{where}.summary[{j}].text", part["text"], 400_000)
            content = item.get("content")
            if content is not None:
                if not isinstance(content, list) or len(content) > 100:
                    raise bad_request(f"{where}.content 须为列表或 null", code="invalid_parameter")
                for j, part in enumerate(content):
                    _keys(f"{where}.content[{j}]", part, {"type", "text"}, {"type", "text"})
                    if part["type"] != "reasoning_text":
                        raise bad_request(f"{where}.content[{j}].type 须为 reasoning_text", code="invalid_parameter")
                    _text(f"{where}.content[{j}].text", part["text"], 400_000)
            if item.get("encrypted_content") is not None:
                _text(f"{where}.encrypted_content", item["encrypted_content"], 400_000)
        else:
            raise _refuse_content(f"{where}（type={kind}）")
        # Shared by the three accepted item types.
        if item.get("id") is not None:
            _text(f"{where}.id", item["id"], 200)
        if item.get("status") is not None and item["status"] not in ("in_progress", "completed", "incomplete"):
            raise bad_request(f"{where}.status 须为 in_progress / completed / incomplete", code="invalid_parameter")
    return len(value)


def sanitize(path: str, body: dict) -> tuple[dict, int, int | None, bool]:
    """Validate a session call body.

    Returns ``(forward, input_bound, requested_out, stream)``: the allow-listed
    body without model/stream/limits (the gateway adds those), an upper bound
    on prompt tokens, the smallest output limit the client asked for (or
    ``None``), and whether the client asked for SSE streaming (§4.3).
    """
    if not isinstance(body, dict):
        raise bad_request("请求体须为对象")
    chat = path != "/responses"
    allowed, control = (CHAT_PARAMS, CHAT_CONTROL) if chat else (RESPONSES_PARAMS, RESPONSES_CONTROL)
    content_key = "messages" if chat else "input"
    unknown = sorted(k for k in body if k not in allowed and k not in control and k != content_key)
    if unknown:
        raise ApiError(400, "parameter_not_allowed",
                       f"会话调用不接受这些参数：{unknown}（允许：{sorted([content_key, *allowed, *control])}）",
                       parameters=unknown)
    if body.get("stream") is not None and not isinstance(body.get("stream"), bool):
        raise bad_request("stream 须为布尔值", code="invalid_parameter")
    if chat and body.get("n") not in (None, 1):
        raise bad_request("会话调用只支持 n=1", code="n_unsupported")
    requested = []
    for key in ("max_tokens", "max_completion_tokens") if chat else ("max_output_tokens",):
        if body.get(key) is not None:
            _int(1, MAX_SESSION_TOKENS)(key, body[key])
            requested.append(body[key])
    for key, check in allowed.items():
        if key in body and body[key] is not None:
            check(key, body[key])
    items = _check_chat_messages(body.get("messages")) if chat else _check_responses_input(body.get("input"))
    stripped = () if chat else RESPONSES_STRIPPED
    forward = {k: v for k, v in body.items() if k not in control and k not in stripped and v is not None}
    tools = tool_count(forward.get("tools"))
    # Same rule as /infer: bytes are an upper bound on byte-level-BPE tokens;
    # every message and every tool definition also gets the template overhead.
    input_bytes = len(json.dumps(forward, ensure_ascii=False, separators=(",", ":")).encode())
    input_bound = input_bytes + INPUT_TOKEN_OVERHEAD * (max(1, items) + tools)
    if not chat:
        input_bound += INPUT_TOKEN_OVERHEAD   # the gateway always writes a fixed-length scoped prompt_cache_key
    return forward, input_bound, (min(requested) if requested else None), body.get("stream") is True


def with_output_limit(path: str, forward: dict, out: int, output_param: str, stream: bool = False) -> dict:
    """The body actually sent upstream: allow-listed fields + server-set limit (+ n=1 for chat).

    Streaming (§4.3): ``stream: true``; chat also gets
    ``stream_options: {include_usage: true}`` written by the server (clients
    cannot send ``stream_options``) so the provider's usage arrives in the stream."""
    if path == "/responses":
        return {**forward, "max_output_tokens": out, **({"stream": True} if stream else {})}
    limits = {"max_tokens": out, "max_completion_tokens": out} if output_param == "both" \
        else {("max_completion_tokens" if output_param == "max_completion_tokens" else "max_tokens"): out}
    extra = {"stream": True, "stream_options": {"include_usage": True}} if stream else {}
    return {**forward, "n": 1, **limits, **extra}
