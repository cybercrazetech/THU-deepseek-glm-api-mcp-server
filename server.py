#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

APP_NAME = "Tsinghua DeepSeek MCP"
DEFAULT_BASE_URL = "https://madmodel.cs.tsinghua.edu.cn/v1"
DEFAULT_MODEL = "DeepSeek-R1-Distill-32B"
SUPPORTED_MODELS = ["DeepSeek-R1-671B", "DeepSeek-R1-Distill-32B"]

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tsinghua-deepseek-mcp")

mcp = FastMCP(APP_NAME, json_response=True, streamable_http_path="/mcp")


def _get_api_key() -> str:
    api_key = os.environ.get("TSINGHUA_DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError(
            "Missing API key. Set TSINGHUA_DEEPSEEK_API_KEY (preferred) or DEEPSEEK_API_KEY."
        )
    return api_key


def _get_base_url() -> str:
    return os.environ.get("TSINGHUA_DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _get_default_model() -> str:
    return os.environ.get("TSINGHUA_DEEPSEEK_MODEL", DEFAULT_MODEL)


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {_get_api_key()}",
    }


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    allowed_roles = {"system", "user", "assistant"}
    for idx, msg in enumerate(messages):
        role = str(msg.get("role", "")).strip()
        content = msg.get("content", "")
        if role not in allowed_roles:
            raise ValueError(f"messages[{idx}].role must be one of {sorted(allowed_roles)}")
        if not isinstance(content, str):
            raise ValueError(f"messages[{idx}].content must be a string")
        normalized.append({"role": role, "content": content})
    if not normalized:
        raise ValueError("messages must not be empty")
    return normalized


def _extract_text_from_nonstream_response(payload: dict[str, Any]) -> str:
    try:
        choices = payload.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content")
            if isinstance(content, str):
                return content
    except Exception:
        pass
    return ""


def _extract_api_error(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    status = payload.get("status")
    message = payload.get("message")
    if isinstance(status, int) and isinstance(message, str):
        return status, message
    if payload.get("success") is False and isinstance(message, str):
        return status if isinstance(status, int) else None, message
    return None, None


def _should_retry(status_code: int | None, message: str | None) -> bool:
    if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    if not message:
        return False
    lowered = message.lower()
    return "busy" in lowered or "timeout" in lowered or "繁忙" in message


def _collect_streaming_text(response: httpx.Response) -> str:
    parts: list[str] = []
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping non-JSON stream chunk: %s", data[:200])
            continue
        for choice in obj.get("choices", []):
            delta = choice.get("delta", {})
            content = delta.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(choice.get("message", {}).get("content"), str):
                parts.append(choice["message"]["content"])
    return "".join(parts)


def _post_chat_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.6,
    repetition_penalty: float = 1.2,
    stream: bool = False,
    timeout: float = 120.0,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
    max_retries: int = 2,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    if model not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported model: {model}. Choose one of {SUPPORTED_MODELS}")

    body: dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages),
        "temperature": temperature,
        "repetition_penalty": repetition_penalty,
        "stream": stream,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if extra_body:
        body.update(extra_body)

    url = f"{_get_base_url()}/chat/completions"
    logger.info("POST %s model=%s stream=%s", url, model, stream)

    with httpx.Client(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            if stream:
                with client.stream("POST", url, headers=_headers(), json=body) as response:
                    response.raise_for_status()
                    text = _collect_streaming_text(response)
                    return {
                        "ok": True,
                        "model": model,
                        "text": text,
                        "raw": None,
                    }

            response = client.post(url, headers=_headers(), json=body)
            response.raise_for_status()
            payload = response.json()
            status_code, error_message = _extract_api_error(payload)
            if error_message:
                if attempt < max_retries and _should_retry(status_code, error_message):
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                return {
                    "ok": False,
                    "model": model,
                    "text": "",
                    "error": error_message,
                    "status": status_code,
                    "raw": payload,
                }
            return {
                "ok": True,
                "model": model,
                "text": _extract_text_from_nonstream_response(payload),
                "raw": payload,
            }

    raise RuntimeError("Request loop exited unexpectedly")


@mcp.tool()
def list_models() -> dict[str, Any]:
    """List the models currently documented by the Tsinghua DeepSeek endpoint."""
    return {
        "base_url": _get_base_url(),
        "models": SUPPORTED_MODELS,
        "default_model": _get_default_model(),
        "notes": [
            "API key is expected in TSINGHUA_DEEPSEEK_API_KEY.",
            "The copied token reportedly expires about 5 hours after login, so refresh it when requests start failing.",
        ],
    }


@mcp.tool()
def health_check(model: str = _get_default_model(), timeout: float = 30.0) -> dict[str, Any]:
    """Send a tiny request to verify that the API key and endpoint are working."""
    return _post_chat_completion(
        model=model,
        messages=[{"role": "user", "content": "Reply with only: OK"}],
        temperature=0.0,
        repetition_penalty=1.0,
        stream=False,
        timeout=timeout,
        max_tokens=16,
    )


@mcp.tool()
def simple_chat(
    prompt: str,
    system: str = "",
    model: str = _get_default_model(),
    temperature: float = 0.6,
    repetition_penalty: float = 1.2,
    stream: bool = False,
    timeout: float = 120.0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Send a single-turn chat request to the Tsinghua DeepSeek endpoint."""
    messages: list[dict[str, str]] = []
    if system.strip():
        messages.append({"role": "system", "content": system.strip()})
    messages.append({"role": "user", "content": prompt})
    return _post_chat_completion(
        model=model,
        messages=messages,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        stream=stream,
        timeout=timeout,
        max_tokens=max_tokens,
    )


@mcp.tool()
def chat_completion(
    messages: list[dict[str, Any]],
    model: str = _get_default_model(),
    temperature: float = 0.6,
    repetition_penalty: float = 1.2,
    stream: bool = False,
    timeout: float = 120.0,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OpenAI-compatible chat completion tool with full message history support."""
    return _post_chat_completion(
        model=model,
        messages=messages,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        stream=stream,
        timeout=timeout,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )


@mcp.resource("config://deepseek")
def config_resource() -> str:
    """Expose current server configuration without revealing the API key."""
    has_key = bool(os.environ.get("TSINGHUA_DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
    return json.dumps(
        {
            "app_name": APP_NAME,
            "base_url": _get_base_url(),
            "default_model": _get_default_model(),
            "supported_models": SUPPORTED_MODELS,
            "api_key_present": has_key,
            "transport_help": {
                "stdio": "python3 server.py",
                "http": "python3 server.py --transport http --host 127.0.0.1 --port 8000",
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for Tsinghua DeepSeek API")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Run over stdio for local MCP clients, or streamable HTTP for remote clients.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    args = parser.parse_args()

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = "/mcp"
        logger.info("Starting HTTP MCP server on %s:%s", args.host, args.port)
        mcp.run(transport="streamable-http")
    else:
        logger.info("Starting STDIO MCP server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
