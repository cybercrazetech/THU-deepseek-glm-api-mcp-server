#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

DEFAULT_BASE_URL = "https://madmodel.cs.tsinghua.edu.cn/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "DeepSeek-R1-Distill-32B"
SUPPORTED_MODELS = [
    "DeepSeek-R1-Distill-32B",
    "DeepSeek-R1-671B",
    "qwen/qwen3-coder:free",
]
ENV_FILE = ".env"
HISTORY_FILE = ".thu-agent-history"
MAX_HISTORY = 24
MAX_TOOL_ROUNDS = 6
MAX_TOOL_OUTPUT_CHARS = 12000
RESPONSE_INDENT = 2
PANEL_INDENT = 3

ACCENT = "bright_cyan"
MUTED = "grey62"
DIM = "grey50"
ERROR = "bold red"
SUCCESS = "green"

console = Console(soft_wrap=True)
prompt_session: PromptSession[str] | None = None


def _prompt(prompt: str, *, password: bool = False) -> str:
    if password:
        return getpass.getpass(prompt)
    if prompt_session is None:
        return input(prompt)
    return prompt_session.prompt(prompt)


def _prompt_model(default_model: str) -> str:
    rows: list[str] = []
    for idx, model in enumerate(SUPPORTED_MODELS, start=1):
        default = " default" if model == default_model else ""
        rows.append(f"{idx}. `{model}`{default}")
    body = Markdown("## Choose a Model\n" + "\n".join(f"- {row}" for row in rows))
    console.print(Panel(body, border_style=ACCENT, padding=(1, 2), title="Session"))
    while True:
        raw = _prompt("> ").strip()
        if not raw:
            return default_model
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(SUPPORTED_MODELS):
                return SUPPORTED_MODELS[index]
        if raw in SUPPORTED_MODELS:
            return raw
        console.print(f"Unsupported model. Choose one of: {', '.join(SUPPORTED_MODELS)}", style=ERROR)


def _prompt_api_key(existing: str | None) -> str:
    if existing:
        use_existing = _prompt("Use environment API key? [Y/n] ").strip().lower()
        if use_existing in {"", "y", "yes"}:
            return existing
    while True:
        api_key = _prompt("API key: ", password=True).strip()
        if api_key:
            return api_key
        console.print("API key is required.", style=ERROR)


def _provider_for_model(model: str) -> str:
    if model.startswith("qwen/"):
        return "openrouter"
    return "tsinghua"


def _base_url_for_model(model: str) -> str:
    if _provider_for_model(model) == "openrouter":
        return OPENROUTER_BASE_URL
    return DEFAULT_BASE_URL


def _api_key_env_var_for_model(model: str) -> str:
    if _provider_for_model(model) == "openrouter":
        return "OPENROUTER_API_KEY"
    return "TSINGHUA_DEEPSEEK_API_KEY"


def _load_env_file(cwd: str) -> dict[str, str]:
    env_path = Path(cwd) / ENV_FILE
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def _save_api_key_to_env(cwd: str, api_key: str, model: str) -> None:
    env_path = Path(cwd) / ENV_FILE
    values = _load_env_file(cwd)
    values[_api_key_env_var_for_model(model)] = api_key
    if "TSINGHUA_DEEPSEEK_BASE_URL" not in values:
        values["TSINGHUA_DEEPSEEK_BASE_URL"] = DEFAULT_BASE_URL
    if "OPENROUTER_BASE_URL" not in values:
        values["OPENROUTER_BASE_URL"] = OPENROUTER_BASE_URL
    lines = [f"{key}='{value}'" for key, value in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _save_base_url_to_env(cwd: str, base_url: str, model: str) -> None:
    env_path = Path(cwd) / ENV_FILE
    values = _load_env_file(cwd)
    if _provider_for_model(model) == "openrouter":
        values["OPENROUTER_BASE_URL"] = base_url
    else:
        values["TSINGHUA_DEEPSEEK_BASE_URL"] = base_url
    lines = [f"{key}='{value}'" for key, value in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    if not normalized.endswith("/v1"):
        normalized = f"{normalized}/v1"
    return normalized


def _detect_runtime() -> dict[str, str]:
    system = platform.system().strip() or "Unknown"
    release = platform.release().strip() or "unknown"
    if system == "Windows":
        shell = "powershell"
        shell_label = "PowerShell"
    else:
        shell = "bash"
        shell_label = "bash"
    return {
        "system": system,
        "release": release,
        "shell": shell,
        "shell_label": shell_label,
    }
def _headers(api_key: str, model: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    if _provider_for_model(model) == "openrouter":
        headers["HTTP-Referer"] = "https://openrouter.ai/"
        headers["X-Title"] = "THU Agent"
    return headers


def _extract_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices", [])
    if not choices:
        return {}
    message = choices[0].get("message", {})
    return message if isinstance(message, dict) else {}


def _extract_text(payload: dict[str, Any]) -> str:
    content = _extract_message(payload).get("content")
    return content if isinstance(content, str) else ""


def _extract_reasoning(payload: dict[str, Any]) -> str:
    reasoning = _extract_message(payload).get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def _extract_api_error(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        metadata = error.get("metadata")
        if isinstance(metadata, dict):
            raw = metadata.get("raw")
            provider = metadata.get("provider_name")
            if isinstance(raw, str) and raw.strip():
                if isinstance(provider, str) and provider.strip():
                    return code if isinstance(code, int) else None, f"{raw} (provider: {provider})"
                return code if isinstance(code, int) else None, raw
        if isinstance(message, str):
            return code if isinstance(code, int) else None, message
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


def _is_invalid_api_key(error_message: str, status_code: int | None) -> bool:
    lowered = error_message.lower()
    if status_code in {401, 403, 404}:
        return True
    return any(
        token in lowered
        for token in ["api key", "token", "unauthorized", "invalid", "expired", "鉴权", "无效", "过期", "not found"]
    )


def _chat_completion(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.2,
    repetition_penalty: float = 1.1,
    timeout: float = 120.0,
    max_tokens: int = 1400,
    max_retries: int = 2,
) -> dict[str, Any]:
    normalized_base_url = _normalize_base_url(base_url)
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "repetition_penalty": repetition_penalty,
        "stream": False,
        "max_tokens": max_tokens,
    }
    url = f"{normalized_base_url}/chat/completions"
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for attempt in range(max_retries + 1):
            try:
                response = client.post(url, headers=_headers(api_key, model), json=body)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                error_message = f"HTTP {status_code} from upstream"
                location = exc.response.headers.get("location")
                if 300 <= status_code < 400:
                    if location:
                        error_message = f"HTTP {status_code} redirect from upstream to {location}"
                    else:
                        error_message = f"HTTP {status_code} redirect from upstream"
                if status_code == 404:
                    error_message = f"HTTP 404 from upstream at {url}"
                if attempt < max_retries and status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                    time.sleep(attempt + 1)
                    continue
                return {
                    "ok": False,
                    "error": error_message,
                    "status": status_code,
                    "raw": {
                        "url": str(exc.request.url),
                        "location": location,
                    },
                    "text": "",
                    "reasoning": "",
                }
            except httpx.RequestError as exc:
                error_message = f"network error: {exc}"
                if attempt < max_retries:
                    time.sleep(attempt + 1)
                    continue
                return {
                    "ok": False,
                    "error": error_message,
                    "status": None,
                    "raw": None,
                    "text": "",
                    "reasoning": "",
                }

            try:
                payload = response.json()
            except ValueError:
                return {
                    "ok": False,
                    "error": "upstream returned non-JSON response",
                    "status": response.status_code,
                    "raw": response.text,
                    "text": "",
                    "reasoning": "",
                }

            status_code, error_message = _extract_api_error(payload)
            if error_message:
                if attempt < max_retries and _should_retry(status_code, error_message):
                    time.sleep(attempt + 1)
                    continue
                return {
                    "ok": False,
                    "error": error_message,
                    "status": status_code,
                    "raw": payload,
                    "text": "",
                    "reasoning": "",
                }
            return {
                "ok": True,
                "text": _extract_text(payload),
                "reasoning": _extract_reasoning(payload),
                "raw": payload,
            }
    return {"ok": False, "error": "Request loop exited unexpectedly", "text": "", "reasoning": ""}


def _agent_system_prompt(cwd: str, runtime: dict[str, str]) -> str:
    if runtime["shell"] == "powershell":
        shell_guidance = (
            "Use PowerShell-native commands and syntax.\n"
            "Prefer commands like Get-ChildItem, Get-Content, Set-Content, Add-Content, New-Item, Copy-Item, Move-Item, and Remove-Item.\n"
            "For writing files, prefer Set-Content, Add-Content, here-strings, or python -c.\n"
            "Do not use bash-only syntax such as /bin/bash, cat <<'EOF', chmod, &&-chained shell assumptions, or single-quoted echo redirection patterns that rely on POSIX shells.\n"
        )
    else:
        shell_guidance = (
            "Use POSIX shell commands and syntax.\n"
            "Prefer bash-compatible commands such as rg, ls, cat, sed, awk, printf, chmod, and sh-compatible redirection.\n"
        )
    if runtime["shell"] == "powershell":
        file_write_guidance = (
            "When writing files, use non-interactive PowerShell commands such as Set-Content, Add-Content, here-strings, or python -c.\n"
        )
    else:
        file_write_guidance = (
            "When writing files, use non-interactive shell commands such as cat with redirection, printf, tee, sed, perl, or python -c.\n"
        )
    return "".join(
        [
            f"You are a terminal coding agent running on {runtime['system']} {runtime['release']}.\n",
            f"Current working directory: {cwd}\n",
            f"Primary shell for commands: {runtime['shell_label']}\n",
            "You help the user inspect files, write code, run tests, and explain results.\n",
            "You have one tool: running a shell command in the current working directory after the user approves it.\n",
            "Prefer rg for searching. Keep commands focused and non-destructive unless the user explicitly asks.\n",
            shell_guidance,
            "Do not use interactive editors or pagers such as nano, vim, vi, less, more, or man.\n",
            file_write_guidance,
            "Never use rm -rf, git reset --hard, or similar destructive commands unless the user explicitly asks.\n",
            "For every response, think step by step and include concise visible reasoning.\n",
            "Always respond as exactly one JSON object and nothing else.\n",
            "For a direct answer use:\n",
            '{"type":"reply","reasoning":["short step","short step"],"message":"markdown answer","snippet":{"language":"python","content":"print(1)","title":"optional"}}\n',
            "The snippet field is optional.\n",
            "When you need a command use:\n",
            '{"type":"run","reasoning":["short step","short step"],"command":"rg --files","reason":"list the repository files"}\n',
            "When you need multiple commands at once use:\n",
            '{"type":"run_many","reasoning":["short step","short step"],"commands":[{"command":"rg --files","reason":"list files"},{"command":"git status --short","reason":"check worktree"}],"reason":"gather context in parallel"}\n',
            "Keep reasoning short. Render user-facing explanations in markdown.",
        ]
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _trim_history(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(messages) <= MAX_HISTORY:
        return messages
    head = messages[:1]
    tail = messages[-(MAX_HISTORY - 1) :]
    return head + tail


def _run_command(command: str, cwd: str) -> dict[str, Any]:
    interactive_patterns = [
        r"(^|\s)nano(\s|$)",
        r"(^|\s)vim(\s|$)",
        r"(^|\s)vi(\s|$)",
        r"(^|\s)less(\s|$)",
        r"(^|\s)more(\s|$)",
        r"(^|\s)man(\s|$)",
        r"(^|\s)top(\s|$)",
        r"(^|\s)htop(\s|$)",
    ]
    if any(re.search(pattern, command) for pattern in interactive_patterns):
        return {
            "exit_code": 126,
            "output": (
                "Rejected interactive command. Use a non-interactive file edit or inspection command "
                "such as cat > file, printf, tee, sed, perl, or python -c."
            ),
        }
    runtime = _detect_runtime()
    try:
        if runtime["shell"] == "powershell":
            cmd = ["powershell", "-NoProfile", "-Command", command]
        else:
            cmd = ["/bin/bash", "-lc", command]
        completed = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=120)
        output = (completed.stdout or "") + (completed.stderr or "")
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]..."
        return {"exit_code": completed.returncode, "output": output.strip()}
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "output": "Command timed out after 120 seconds."}
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "output": f"Shell launch failed: {exc}",
        }
    except OSError as exc:
        return {
            "exit_code": 127,
            "output": f"Command runner failed: {exc}",
        }


def _split_reasoning(reasoning: str) -> list[str]:
    text = reasoning.strip()
    if not text:
        return []
    parts = re.split(r"\n+|(?<=[.!?])\s+", text)
    cleaned = [part.strip(" -") for part in parts if part.strip(" -")]
    return cleaned[:6]


def _normalize_reasoning_text(reasoning: str) -> str:
    return reasoning.strip()


def _reasoning_lines(action: dict[str, Any], fallback: str) -> list[str]:
    raw = action.get("reasoning")
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
        if values:
            return values[:6]
    if isinstance(raw, str) and raw.strip():
        return _split_reasoning(raw)
    return _split_reasoning(fallback)


def _render_reasoning(reasoning_text: str) -> None:
    normalized = _normalize_reasoning_text(reasoning_text)
    if not normalized:
        return
    text = Text(normalized, style=f"italic dim {MUTED}")
    console.print(
        Padding(
            Panel(
                text,
                border_style=DIM,
                title=" thought process ",
                padding=(0, 1),
                style="dim",
            ),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_step(title: str, subtitle: str = "") -> None:
    text = Text(title, style=f"bold {ACCENT}")
    if subtitle:
        text.append("  ", style=DIM)
        text.append(subtitle, style=f"italic {DIM}")
    console.print(Padding(text, (0, 0, 0, RESPONSE_INDENT)))


def _render_markdown(markdown_text: str) -> None:
    content = markdown_text.strip() or "_No response._"
    console.print(Padding(Markdown(content), (0, 1, 0, RESPONSE_INDENT)))


def _render_snippet(title: str, code: str, language: str = "text") -> None:
    syntax = Syntax(code.rstrip() or " ", language or "text", theme="monokai", line_numbers=False, word_wrap=True)
    console.print(
        Padding(
            Panel(syntax, title=f" {title} ", border_style=DIM, padding=(0, 1), style="dim"),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_command_request(command: str, reason: str) -> None:
    group_items: list[Any] = [Syntax(command, "bash", theme="monokai", word_wrap=True)]
    if reason:
        group_items.append(Text(reason, style=f"italic dim {MUTED}"))
    console.print(
        Padding(
            Panel(Group(*group_items), border_style=DIM, title=" command ", padding=(0, 1), style="dim"),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_command_batch(command_items: list[dict[str, str]], reason: str) -> None:
    blocks: list[Any] = []
    if reason:
        blocks.append(Text(reason, style=f"italic dim {MUTED}"))
    for item in command_items:
        blocks.append(Syntax(item["command"], "bash", theme="monokai", word_wrap=True))
        if item["reason"]:
            blocks.append(Text(item["reason"], style=f"italic dim {MUTED}"))
    console.print(
        Padding(
            Panel(Group(*blocks), border_style=DIM, title=" parallel commands ", padding=(0, 1), style="dim"),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_command_result(command: str, exit_code: int, output: str) -> None:
    header = Text()
    header.append("exit ", style=f"dim {MUTED}")
    header.append(str(exit_code), style=SUCCESS if exit_code == 0 else ERROR)
    console.print(
        Padding(
            Panel(
                Group(
                    Syntax(command, "bash", theme="monokai", word_wrap=True),
                    header,
                    Syntax(output or "(no output)", "text", theme="monokai", word_wrap=True),
                ),
                border_style=DIM,
                title=" result ",
                padding=(0, 1),
                style="dim",
            ),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_info(text: str) -> None:
    console.print(Padding(Text(text, style=f"dim {MUTED}"), (0, 0, 0, RESPONSE_INDENT)))


def _render_error_snippet(title: str, error_text: str) -> None:
    preview = error_text.strip()[:800] or "Unknown error"
    console.print(
        Padding(
            Panel(
                Syntax(preview, "text", theme="monokai", word_wrap=True),
                title=f" {title} ",
                border_style=ERROR,
                padding=(0, 1),
                style="dim",
            ),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _action_summary(action_type: str, reason: str, count: int | None = None) -> str:
    if reason:
        return reason[:1].upper() + reason[1:]
    if action_type == "run_many":
        return f"Running {count or 0} commands in parallel"
    if action_type == "run":
        return "Running command"
    if action_type == "reply":
        return "Preparing response"
    return "Working"


def _repair_instruction(raw_text: str) -> str:
    return (
        "Your previous response did not follow the required JSON-only protocol.\n"
        "Convert your prior answer into exactly one JSON object.\n"
        "If it was a final answer, use type=reply.\n"
        "If it required commands, use type=run or type=run_many.\n"
        f"Previous raw response:\n{raw_text}"
    )


def _extract_reasoning_for_display(response: dict[str, Any], assistant_text: str, action: dict[str, Any] | None) -> str:
    if response["reasoning"].strip():
        return response["reasoning"].strip()
    if action:
        raw = action.get("reasoning")
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
            if values:
                return "\n".join(values)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return assistant_text.strip()


def _print_help() -> None:
    console.print(
        Padding(
            Panel(
                Markdown(
                    "\n".join(
                        [
                            "## Commands",
                            "- `/help` show this help",
                            "- `/model` show current model",
                            "- `/key` replace the API key for this session",
                            "- `/pwd` show current working directory",
                            "- `/alwaysRun` toggle command approval prompts",
                            "- `/exit` quit",
                        ]
                    )
                ),
                border_style=DIM,
                padding=(1, 2),
            ),
            (0, 0, 0, RESPONSE_INDENT),
        )
    )


def _run_commands_parallel(command_items: list[dict[str, str]], cwd: str) -> list[dict[str, Any]]:
    indexed = list(enumerate(command_items))
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(indexed) or 1) as executor:
        future_map = {
            executor.submit(_run_command, item["command"], cwd): (index, item)
            for index, item in indexed
        }
        for future in concurrent.futures.as_completed(future_map):
            index, item = future_map[future]
            result = future.result()
            results.append(
                {
                    "index": index,
                    "command": item["command"],
                    "reason": item["reason"],
                    "exit_code": result["exit_code"],
                    "output": result["output"],
                }
            )
    return sorted(results, key=lambda item: item["index"])


def _print_banner(model: str, cwd: str, runtime: dict[str, str]) -> None:
    header = Group(
        Text("THU Agent", style=f"bold {ACCENT}"),
        Text("interactive coding session", style=f"italic {DIM}"),
        Text(f"model  {model}", style=MUTED),
        Text(f"cwd    {cwd}", style=MUTED),
        Text(f"os     {runtime['system']} {runtime['release']}  via {runtime['shell_label']}", style=MUTED),
        Text("commands  /help  /model  /key  /pwd  /alwaysRun  /exit", style=DIM),
    )
    console.print()
    console.print(Padding(Panel(header, border_style=ACCENT, padding=(0, 2), title=" session "), (0, 0, 1, RESPONSE_INDENT)))
    console.print()


def _tool_result_message(tool_result: str) -> str:
    return (
        "Tool result from the approved shell-command interface:\n"
        f"{tool_result}\n"
        "Continue the task. Reply as one JSON object only."
    )


def _prompt_run_command(always_run: bool) -> bool:
    if always_run:
        _render_info("alwaysRun enabled. command approved automatically.")
        return True
    answer = _prompt("Run command? [Y/n] ").strip().lower()
    return answer in {"", "y", "yes"}


def _normalize_command_batch(action: dict[str, Any]) -> list[dict[str, str]]:
    raw_commands = action.get("commands")
    if not isinstance(raw_commands, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in raw_commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if command:
            normalized.append({"command": command, "reason": reason})
    return normalized


def main() -> int:
    global prompt_session
    parser = argparse.ArgumentParser(description="Interactive THU DeepSeek terminal agent")
    parser.add_argument("--model", choices=SUPPORTED_MODELS, help="Model name")
    parser.add_argument("--api-key", help="API key for the current session")
    parser.add_argument("--base-url", help="API base URL")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for shell commands")
    args = parser.parse_args()

    cwd = str(Path(args.cwd).resolve())
    runtime = _detect_runtime()
    file_env = _load_env_file(cwd)
    history_path = Path(cwd) / HISTORY_FILE
    prompt_session = PromptSession(history=FileHistory(str(history_path)))
    default_model = (
        os.environ.get("THU_AGENT_MODEL")
        or os.environ.get("TSINGHUA_DEEPSEEK_MODEL")
        or DEFAULT_MODEL
    )
    model = args.model or _prompt_model(default_model)
    if _provider_for_model(model) == "openrouter":
        configured_base_url = (
            args.base_url
            or os.environ.get("OPENROUTER_BASE_URL")
            or file_env.get("OPENROUTER_BASE_URL")
            or _base_url_for_model(model)
        )
        env_key = (
            args.api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or file_env.get("OPENROUTER_API_KEY")
        )
    else:
        configured_base_url = (
            args.base_url
            or os.environ.get("TSINGHUA_DEEPSEEK_BASE_URL")
            or file_env.get("TSINGHUA_DEEPSEEK_BASE_URL")
            or _base_url_for_model(model)
        )
        env_key = (
            args.api_key
            or os.environ.get("TSINGHUA_DEEPSEEK_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or file_env.get("TSINGHUA_DEEPSEEK_API_KEY")
        )
    base_url = _normalize_base_url(configured_base_url)
    api_key = args.api_key or _prompt_api_key(env_key)
    _save_api_key_to_env(cwd, api_key, model)
    _save_base_url_to_env(cwd, base_url, model)
    always_run = False

    messages: list[dict[str, str]] = [{"role": "system", "content": _agent_system_prompt(cwd, runtime)}]
    _print_banner(model, cwd, runtime)

    while True:
        try:
            user_input = _prompt("> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

        if not user_input:
            continue
        if user_input == "/exit":
            return 0
        if user_input == "/help":
            _print_help()
            continue
        if user_input == "/model":
            console.print(model, style=MUTED)
            continue
        if user_input == "/key":
            api_key = _prompt_api_key(None)
            _save_api_key_to_env(cwd, api_key, model)
            console.print(Padding(f"API key updated and saved to {ENV_FILE}.", (0, 0, 0, RESPONSE_INDENT)), style=SUCCESS)
            continue
        if user_input == "/pwd":
            console.print(cwd, style=MUTED)
            continue
        if user_input == "/alwaysRun":
            always_run = not always_run
            state = "enabled" if always_run else "disabled"
            _render_info(f"alwaysRun {state}")
            continue

        messages.append({"role": "user", "content": user_input})
        messages = _trim_history(messages)

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                _render_step("Thinking")
                with console.status("[dim]thinking…[/dim]", spinner="dots"):
                    response = _chat_completion(
                        api_key=api_key,
                        model=model,
                        messages=messages,
                        base_url=base_url,
                    )
                if not response["ok"]:
                    _render_step("Upstream Error")
                    console.print(Padding(f"upstream error: {response['error']}", (0, 0, 0, RESPONSE_INDENT)), style=ERROR)
                    if response.get("status") == 404:
                        _render_info(f"active base URL: {base_url}")
                        _render_info("this endpoint worked earlier in the session, so this 404 is likely THU auth/session state changing rather than a bad URL.")
                        _render_info("refresh the token, restart the agent, and retry.")
                    if _is_invalid_api_key(str(response["error"]), response.get("status")):
                        _render_info("stored API key appears invalid or expired. enter a new key.")
                        api_key = _prompt_api_key(None)
                        _save_api_key_to_env(cwd, api_key, model)
                        _render_info(f"saved updated API key to {ENV_FILE}")
                        continue
                    break

                assistant_text = response["text"].strip()
                messages.append({"role": "assistant", "content": assistant_text})
                action = _extract_json_object(assistant_text)
                reasoning_text = _extract_reasoning_for_display(response, assistant_text, action)
                _render_reasoning(reasoning_text)

                if not action:
                    messages.append({"role": "user", "content": _repair_instruction(assistant_text)})
                    messages = _trim_history(messages)
                    continue

                action_type = action.get("type")
                if action_type == "reply":
                    _render_step(_action_summary("reply", str(action.get("reason", "")).strip()))
                    _render_markdown(str(action.get("message", "")).strip())
                    snippet = action.get("snippet")
                    if isinstance(snippet, dict):
                        code = str(snippet.get("content", "")).strip()
                        if code:
                            _render_snippet(
                                str(snippet.get("title", "snippet")).strip() or "snippet",
                                code,
                                str(snippet.get("language", "text")).strip() or "text",
                            )
                    break

                if action_type == "run_many":
                    command_items = _normalize_command_batch(action)
                    if not command_items:
                        console.print("empty parallel command request", style=ERROR)
                        break
                    _render_step(_action_summary("run_many", str(action.get("reason", "")).strip(), len(command_items)))
                    _render_command_batch(command_items, str(action.get("reason", "")).strip())
                    if not _prompt_run_command(always_run):
                        tool_result = "Parallel command batch was not approved by the user."
                        _render_info(tool_result)
                    else:
                        _render_step("Running Commands", f"{len(command_items)} in parallel")
                        with console.status("[dim]running commands…[/dim]", spinner="dots"):
                            results = _run_commands_parallel(command_items, cwd)
                        _render_step("Command Results")
                        rendered_chunks: list[str] = []
                        for result in results:
                            _render_command_result(result["command"], result["exit_code"], result["output"])
                            rendered_chunks.append(
                                "\n".join(
                                    [
                                        f"Command: {result['command']}",
                                        f"Reason: {result['reason']}",
                                        f"Exit code: {result['exit_code']}",
                                        "Output:",
                                        result["output"],
                                    ]
                                )
                            )
                        tool_result = "\n\n".join(rendered_chunks)
                    messages.append({"role": "user", "content": _tool_result_message(tool_result)})
                    messages = _trim_history(messages)
                    continue

                if action_type != "run":
                    console.print("invalid tool response from model", style=ERROR)
                    _render_snippet("raw", assistant_text, "json")
                    break

                command = str(action.get("command", "")).strip()
                reason = str(action.get("reason", "")).strip()
                if not command:
                    console.print("empty command request", style=ERROR)
                    break

                _render_step(_action_summary("run", reason))
                _render_command_request(command, reason)
                if not _prompt_run_command(always_run):
                    tool_result = "Command was not approved by the user."
                    _render_info(tool_result)
                else:
                    _render_step("Running Command")
                    with console.status("[dim]running command…[/dim]", spinner="dots"):
                        result = _run_command(command, cwd)
                    _render_step("Command Result")
                    tool_result = (
                        f"Command: {command}\n"
                        f"Exit code: {result['exit_code']}\n"
                        f"Output:\n{result['output']}"
                    )
                    _render_command_result(command, result["exit_code"], result["output"])

                messages.append({"role": "user", "content": _tool_result_message(tool_result)})
                messages = _trim_history(messages)
            else:
                console.print("stopped after too many tool rounds", style=ERROR)
        except Exception as exc:
            _render_step("Runtime Error")
            _render_error_snippet("runtime error", str(exc))

        console.print(Rule(style=DIM))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        console.print(Padding("fatal runtime error", (0, 0, 0, RESPONSE_INDENT)), style=ERROR)
        _render_error_snippet("fatal error", str(exc))
        raise SystemExit(1)
