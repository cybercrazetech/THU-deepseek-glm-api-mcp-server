#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import getpass
import json
import mimetypes
import os
import platform
import queue
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import ConditionalCompleter, WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

DEFAULT_BASE_URL = "https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1"
DEFAULT_MODEL = "deepseek-v3.2"
APP_VERSION = "0.7.0"
GITHUB_REPO_URL = "https://github.com/cybercrazetech/THU-deepseek-glm-api-mcp-server.git"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/cybercrazetech/THU-deepseek-glm-api-mcp-server/main/VERSION"
SUPPORTED_MODELS = [
    "qwen3-max-thinking",
    "qwen3-max",
    "glm-5",
    "glm-5-thinking",
    "glm-4.7-thinking",
    "kimi-k2.5",
    "kimi-k2.5-thinking",
    "minimax-m2.5",
    "minimax-m2.5-thinking",
    "qwen3.5-plus",
    "qwen3.5-plus-thinking",
    "qwen3.5-mini",
    "deepseek-v3.2-thinking",
    "deepseek-v3.2",
]
ENV_FILE = ".env"
HISTORY_FILE = ".thu-agent-history"
CONFIG_DIR_NAME = ".thu-cybercraze-agent"
MAX_HISTORY = 24
MAX_TOOL_OUTPUT_CHARS = 12000
MAX_RENDERED_CHARS = 50000
MAX_PROJECT_CONTEXT_CHARS = 9000
MAX_MEMORY_FILE_CHARS = 3000
MAX_ATTACHED_TEXT_CHARS = 20000
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_API_ERROR_RECOVERY = 2
DISPLAY_FOLD_WIDTH = 96
RESPONSE_INDENT = 2
PANEL_INDENT = 3

ACCENT = "bright_cyan"
MUTED = "grey62"
DIM = "grey50"
ERROR = "bold red"
SUCCESS = "green"

console = Console(soft_wrap=True)
prompt_session: PromptSession[str] | None = None
rendered_char_count = 0
startup_update_notice: str | None = None
active_processes: set[subprocess.Popen[bytes]] = set()
active_processes_lock = threading.Lock()


def _slash_commands() -> list[str]:
    return [
        "/help",
        "/save",
        "/autosave",
        "/context",
        "/compact",
        "/clear",
        "/status",
        "/attach",
        "/stop",
        "/sessions",
        "/load",
        "/fork",
        "/new",
        "/delete",
        "/update",
        "/model",
        "/key",
        "/pwd",
        "/alwaysRun",
        "/exit",
    ]


def _slash_command_completer() -> ConditionalCompleter:
    @Condition
    def _starts_with_slash() -> bool:
        app = prompt_session.app if prompt_session is not None else None
        if app is None:
            return False
        return app.current_buffer.document.text.lstrip().startswith("/")

    return ConditionalCompleter(
        WordCompleter(_slash_commands(), ignore_case=True, match_middle=True, sentence=True),
        _starts_with_slash,
    )


def _prompt(prompt: str, *, password: bool = False) -> str:
    if password:
        return getpass.getpass(prompt)
    if prompt_session is None:
        return input(prompt)
    return prompt_session.prompt(prompt)


def _prompt_model(default_model: str) -> str:
    lines: list[Text] = [Text("Choose a Model", style=f"bold {ACCENT}")]
    for idx, model in enumerate(SUPPORTED_MODELS, start=1):
        default = " default" if model == default_model else ""
        line = Text()
        line.append("  •  ", style=ACCENT)
        line.append(f"{idx:>2} ", style=ACCENT)
        line.append(model, style="bold white")
        if default:
            line.append(default, style=f"italic {DIM}")
        lines.append(line)
    console.print(Panel(Group(*lines), border_style=ACCENT, padding=(1, 2), title="Session"))
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


def _api_key_env_var() -> str:
    return "THU_LAB_PROXY_API_KEY"


def _global_config_dir() -> Path:
    return Path.home() / CONFIG_DIR_NAME


def _global_env_path() -> Path:
    return _global_config_dir() / ENV_FILE


def _global_history_path() -> Path:
    return _global_config_dir() / HISTORY_FILE


def _global_sessions_dir() -> Path:
    return _global_config_dir() / "sessions"


def _clear_terminal_screen() -> None:
    runtime = _detect_runtime()
    try:
        if runtime["system"] == "Windows":
            os.system("cls")
        else:
            # Clear screen and scrollback like a normal terminal clear.
            sys.stdout.write("\033[3J\033[2J\033[H")
            sys.stdout.flush()
    except Exception:
        console.clear()


def _touch_render_budget(estimated_chars: int) -> None:
    global rendered_char_count
    if rendered_char_count + estimated_chars > MAX_RENDERED_CHARS:
        _clear_terminal_screen()
        console.print(Padding(Text("terminal output cleared to keep the session readable", style=f"dim {DIM}"), (0, 0, 0, RESPONSE_INDENT)))
        rendered_char_count = 0
    rendered_char_count += estimated_chars


def _fold_long_display_text(value: str, width: int = DISPLAY_FOLD_WIDTH) -> str:
    """Display-only folding for long tokens that Rich may otherwise crop."""
    folded_lines: list[str] = []
    for line in str(value).splitlines() or [""]:
        if len(line) <= width:
            folded_lines.append(line)
            continue
        parts = re.split(r"(\s+)", line)
        rebuilt: list[str] = []
        for part in parts:
            if len(part) <= width or part.isspace():
                rebuilt.append(part)
                continue
            rebuilt.append("\n".join(part[index : index + width] for index in range(0, len(part), width)))
        folded_lines.extend("".join(rebuilt).splitlines())
    return "\n".join(folded_lines)


def _display_text(value: str, *, style: str = "") -> Text:
    return Text(_fold_long_display_text(value), style=style, overflow="fold", no_wrap=False)


def _parse_env_file(env_path: Path) -> dict[str, str]:
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


def _load_env_file(cwd: str) -> dict[str, str]:
    values = _parse_env_file(_global_env_path())
    values.update(_parse_env_file(Path(cwd) / ENV_FILE))
    return values


def _save_api_key_to_env(api_key: str) -> None:
    env_path = _global_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    values = _parse_env_file(env_path)
    values[_api_key_env_var()] = api_key
    if "THU_LAB_PROXY_BASE_URL" not in values:
        values["THU_LAB_PROXY_BASE_URL"] = DEFAULT_BASE_URL
    lines = [f"{key}='{value}'" for key, value in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _save_base_url_to_env(base_url: str) -> None:
    env_path = _global_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    values = _parse_env_file(env_path)
    values["THU_LAB_PROXY_BASE_URL"] = base_url
    lines = [f"{key}='{value}'" for key, value in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _slugify_session_name(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return slug or f"session-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _default_session_name() -> str:
    return f"session-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _session_path(name: str) -> Path:
    return _global_sessions_dir() / f"{_slugify_session_name(name)}.json"


def _session_summary(messages: list[dict[str, str]], name: str) -> str:
    for message in messages:
        if message.get("role") == "user":
            text = _message_content_text(message.get("content", "")).strip().replace("\n", " ")
            if text:
                return text[:80]
    return _slugify_session_name(name)


def _save_session(name: str, *, model: str, cwd: str, messages: list[dict[str, str]]) -> Path:
    sessions_dir = _global_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_path = _session_path(name)
    payload = {
        "name": _slugify_session_name(name),
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "last_used_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": model,
        "cwd": cwd,
        "summary": _session_summary(messages, name),
        "messages": messages,
    }
    session_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return session_path


def _load_session(name: str) -> dict[str, Any]:
    session_path = _session_path(name)
    if not session_path.exists():
        raise FileNotFoundError(f"session not found: {session_path.name}")
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("session file is invalid")
    return payload


def _list_sessions() -> list[dict[str, Any]]:
    sessions_dir = _global_sessions_dir()
    if not sessions_dir.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sessions_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append(
            {
                "name": path.stem,
                "summary": str(payload.get("summary", path.stem)).strip() or path.stem,
                "last_used_at": str(payload.get("last_used_at", payload.get("saved_at", ""))).strip(),
                "model": str(payload.get("model", "")).strip(),
            }
        )
    entries.sort(key=lambda item: item["last_used_at"], reverse=True)
    return entries


def _resolve_session_reference(reference: str) -> str:
    reference = reference.strip()
    sessions = _list_sessions()
    if reference.isdigit():
        index = int(reference)
        if 1 <= index <= len(sessions):
            return str(sessions[index - 1]["name"])
        raise FileNotFoundError(f"session id not found: {reference}")
    return _slugify_session_name(reference)


def _delete_session(name: str) -> bool:
    session_path = _session_path(name)
    if not session_path.exists():
        return False
    session_path.unlink()
    return True


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "image_url":
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    url = str(image_url.get("url", ""))
                    parts.append(f"[image: {url[:80]}...]")
                else:
                    parts.append("[image]")
            elif item.get("type") == "image":
                parts.append("[image]")
        return "\n".join(part for part in parts if part)
    return str(content)


def _run_context_command(args: list[str], cwd: str, timeout: float = 3.0) -> str:
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    return (completed.stdout or "").strip()


def _find_memory_files(cwd: str) -> list[Path]:
    names = {"AGENTS.md", "CLAUDE.md", ".thu-agent.md"}
    start = Path(cwd).resolve()
    candidates: list[Path] = []
    for directory in [start, *start.parents]:
        for name in names:
            path = directory / name
            if path.is_file() and path not in candidates:
                candidates.append(path)
        if (directory / ".git").exists():
            break
    return candidates


def _read_memory_file(path: Path, cwd: str) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    if not content:
        return ""
    if len(content) > MAX_MEMORY_FILE_CHARS:
        content = content[:MAX_MEMORY_FILE_CHARS] + "\n...[truncated]..."
    try:
        label = str(path.relative_to(cwd))
    except ValueError:
        label = str(path)
    return f"### {label}\n{content}"


def _git_context(cwd: str) -> str:
    inside = _run_context_command(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    if inside != "true":
        return ""
    branch = _run_context_command(["git", "branch", "--show-current"], cwd) or "(detached)"
    status = _run_context_command(["git", "--no-optional-locks", "status", "--short"], cwd)
    log = _run_context_command(["git", "--no-optional-locks", "log", "--oneline", "-n", "5"], cwd)
    if len(status) > 2000:
        status = status[:2000] + "\n...[truncated]..."
    return "\n".join(
        [
            "### Git Snapshot",
            f"Branch: {branch}",
            "Status:",
            status or "(clean)",
            "Recent commits:",
            log or "(none)",
        ]
    )


def _project_context(cwd: str) -> str:
    sections = [f"### Date\n{dt.date.today().isoformat()}"]
    git_context = _git_context(cwd)
    if git_context:
        sections.append(git_context)
    memory_sections = [
        text
        for text in (_read_memory_file(path, cwd) for path in _find_memory_files(cwd))
        if text
    ]
    if memory_sections:
        sections.append("## Project Memory\n" + "\n\n".join(memory_sections))
    context = "\n\n".join(sections).strip()
    if len(context) > MAX_PROJECT_CONTEXT_CHARS:
        context = context[:MAX_PROJECT_CONTEXT_CHARS] + "\n...[truncated]..."
    return context


def _system_message(cwd: str, runtime: dict[str, str], project_context: str) -> dict[str, str]:
    return {"role": "system", "content": _agent_system_prompt(cwd, runtime, project_context)}


def _estimate_context_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(_message_content_text(message.get("content", ""))) for message in messages)


def _compact_messages(messages: list[dict[str, str]], keep_recent: int = 10) -> list[dict[str, str]]:
    if len(messages) <= keep_recent + 2:
        return messages
    system = messages[:1]
    old = messages[1:-keep_recent]
    recent = messages[-keep_recent:]
    summary_lines: list[str] = []
    for message in old:
        role = message.get("role", "message")
        content = _message_content_text(message.get("content", "")).strip().replace("\n", " ")
        if not content:
            continue
        summary_lines.append(f"- {role}: {content[:220]}")
        if len(summary_lines) >= 30:
            summary_lines.append("- ...additional earlier messages omitted...")
            break
    summary = "Earlier conversation compacted locally. Preserve these facts when continuing:\n" + "\n".join(summary_lines)
    return system + [{"role": "user", "content": summary}] + recent


def _version_key(version: str) -> tuple[Any, ...]:
    parts = re.findall(r"\d+|[A-Za-z]+", version)
    key: list[Any] = []
    for part in parts:
        key.append(int(part) if part.isdigit() else part.lower())
    return tuple(key)


def _fetch_latest_version() -> str | None:
    try:
        with httpx.Client(timeout=3.0, follow_redirects=True) as client:
            response = client.get(GITHUB_VERSION_URL)
            response.raise_for_status()
    except Exception:
        return None
    remote_version = response.text.strip()
    return remote_version or None


def _check_for_update_notice() -> str | None:
    latest_version = _fetch_latest_version()
    if not latest_version:
        return None
    if _version_key(latest_version) <= _version_key(APP_VERSION):
        return None
    return f"update available: {APP_VERSION} -> {latest_version}. run /update"


def _safe_completed_output(completed: subprocess.CompletedProcess[str]) -> str:
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return output[:2000] if len(output) > 2000 else output


def _run_update_command(command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _linux_update_target() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path("/usr/local/bin/thu-agent")


def _stage_windows_replacement(source_exe: Path, target_exe: Path, temp_root: Path) -> None:
    script_path = temp_root / "apply-update.ps1"
    source_text = str(source_exe).replace("'", "''")
    target_text = str(target_exe).replace("'", "''")
    temp_text = str(temp_root).replace("'", "''")
    script_path.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$PidToWait = {os.getpid()}",
                f"$SourceExe = '{source_text}'",
                f"$TargetExe = '{target_text}'",
                f"$TempRoot = '{temp_text}'",
                "while (Get-Process -Id $PidToWait -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 500 }",
                "Copy-Item -Force $SourceExe $TargetExe",
                "Remove-Item -Recurse -Force $TempRoot",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _perform_update(runtime: dict[str, str]) -> tuple[bool, str, bool]:
    temp_root = Path(tempfile.mkdtemp(prefix="thu-agent-update-"))
    keep_temp_root = False
    try:
        clone_result = _run_update_command(["git", "clone", "--depth", "1", GITHUB_REPO_URL, str(temp_root)])
        if clone_result.returncode != 0:
            return False, f"git clone failed:\n{_safe_completed_output(clone_result)}", False

        if runtime["system"] == "Windows":
            build_result = _run_update_command(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "build_agent_windows.ps1"],
                cwd=str(temp_root),
            )
            if build_result.returncode != 0:
                return False, f"windows build failed:\n{_safe_completed_output(build_result)}", False
            source_exe = temp_root / "dist" / "thu-agent.exe"
            target_exe = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else (Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "thu-agent.exe")
            keep_temp_root = True
            _stage_windows_replacement(source_exe, target_exe, temp_root)
            return True, f"update staged for {target_exe}. the agent will exit so Windows can replace the executable.", True

        build_env = os.environ.copy()
        build_env.setdefault("XDG_CACHE_HOME", str(temp_root / ".cache"))
        build_result = _run_update_command(["bash", "build_agent.sh"], cwd=str(temp_root), env=build_env)
        if build_result.returncode != 0:
            return False, f"linux build failed:\n{_safe_completed_output(build_result)}", False
        source_bin = temp_root / "dist" / "thu-agent"
        target_bin = _linux_update_target()
        install_result = _run_update_command(["sudo", "install", "-m", "755", str(source_bin), str(target_bin)])
        if install_result.returncode != 0:
            return False, f"install failed for {target_bin}:\n{_safe_completed_output(install_result)}", False
        return True, f"updated executable at {target_bin}", False
    finally:
        if temp_root.exists() and not keep_temp_root:
            try:
                shutil.rmtree(temp_root)
            except Exception:
                pass


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
def _headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {api_key}",
    }


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


def _parse_retry_after(headers: httpx.Headers | None) -> float | None:
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            retry_at = dt.datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
        return max(0.0, (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds())
    return max(0.0, seconds)


def _retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(60.0, retry_after)
    base = min(20.0, 1.5 * (2**attempt))
    return base + random.uniform(0.0, 0.5)


def _should_retry(status_code: int | None, message: str | None) -> bool:
    if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    if not message:
        return False
    lowered = message.lower()
    transient_tokens = [
        "busy",
        "timeout",
        "temporarily",
        "temporary",
        "connection",
        "reset",
        "overloaded",
        "rate limit",
        "too many requests",
        "try again",
        "繁忙",
        "超时",
    ]
    return any(token in lowered or token in message for token in transient_tokens)


def _is_invalid_api_key(error_message: str, status_code: int | None) -> bool:
    lowered = error_message.lower()
    if status_code in {401, 403, 404}:
        return True
    return any(
        token in lowered
        for token in ["api key", "token", "unauthorized", "invalid", "expired", "鉴权", "无效", "过期", "not found"]
    )


def _is_context_overflow_error(error_message: str) -> bool:
    lowered = error_message.lower()
    return any(
        token in lowered
        for token in [
            "context length",
            "context window",
            "maximum context",
            "too many tokens",
            "token limit",
            "prompt too long",
            "context_length_exceeded",
        ]
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
    retries: list[dict[str, Any]] = []
    http_timeout = httpx.Timeout(timeout, connect=10.0, read=timeout, write=30.0, pool=10.0)
    with httpx.Client(timeout=http_timeout, follow_redirects=False) as client:
        for attempt in range(max_retries + 1):
            try:
                response = client.post(url, headers=_headers(api_key), json=body)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                error_message = f"HTTP {status_code} from upstream"
                parsed_status, parsed_error = (None, None)
                try:
                    parsed_payload = exc.response.json()
                    parsed_status, parsed_error = _extract_api_error(parsed_payload)
                except ValueError:
                    parsed_payload = None
                if parsed_error:
                    error_message = parsed_error
                    status_code = parsed_status or status_code
                location = exc.response.headers.get("location")
                if 300 <= status_code < 400:
                    if location:
                        error_message = f"HTTP {status_code} redirect from upstream to {location}"
                    else:
                        error_message = f"HTTP {status_code} redirect from upstream"
                if status_code == 404:
                    error_message = f"HTTP 404 from upstream at {url}"
                if attempt < max_retries and _should_retry(status_code, error_message):
                    delay = _retry_delay(attempt, _parse_retry_after(exc.response.headers))
                    retries.append({"attempt": attempt + 1, "delay": delay, "status": status_code, "error": error_message})
                    time.sleep(delay)
                    continue
                return {
                    "ok": False,
                    "error": error_message,
                    "status": status_code,
                    "raw": {
                        "url": str(exc.request.url),
                        "location": location,
                        "payload": parsed_payload,
                    },
                    "text": "",
                    "reasoning": "",
                    "retries": retries,
                }
            except httpx.RequestError as exc:
                error_message = f"network error: {exc}"
                if attempt < max_retries:
                    delay = _retry_delay(attempt)
                    retries.append({"attempt": attempt + 1, "delay": delay, "status": None, "error": error_message})
                    time.sleep(delay)
                    continue
                return {
                    "ok": False,
                    "error": error_message,
                    "status": None,
                    "raw": None,
                    "text": "",
                    "reasoning": "",
                    "retries": retries,
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
                    "retries": retries,
                }

            status_code, error_message = _extract_api_error(payload)
            if error_message:
                if attempt < max_retries and _should_retry(status_code, error_message):
                    delay = _retry_delay(attempt, _parse_retry_after(response.headers))
                    retries.append({"attempt": attempt + 1, "delay": delay, "status": status_code, "error": error_message})
                    time.sleep(delay)
                    continue
                return {
                    "ok": False,
                    "error": error_message,
                    "status": status_code,
                    "raw": payload,
                    "text": "",
                    "reasoning": "",
                    "retries": retries,
                }
            return {
                "ok": True,
                "text": _extract_text(payload),
                "reasoning": _extract_reasoning(payload),
                "raw": payload,
                "retries": retries,
            }
    return {"ok": False, "error": "Request loop exited unexpectedly", "text": "", "reasoning": "", "retries": retries}


def _agent_system_prompt(cwd: str, runtime: dict[str, str], project_context: str = "") -> str:
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
            "When the user attaches a file, rely on the provided inline content when present; otherwise inspect the referenced path with shell commands.\n",
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
            "When you need multiple commands in one tool round use:\n",
            '{"type":"run_many","reasoning":["short step","short step"],"parallel":false,"commands":[{"command":"pwd","reason":"confirm current directory"},{"command":"rg --files","reason":"list files"}],"reason":"gather context in one batch"}\n',
            "Set parallel=true only when the commands are independent and safe to run concurrently.\n",
            "If a later command depends on an earlier command, use run_many with parallel=false.\n",
            "Keep reasoning short. Render user-facing explanations in markdown.\n",
            (
                "\nProject context snapshot. Treat this as startup context; inspect files directly when freshness matters.\n"
                f"{project_context}\n"
                if project_context
                else ""
            ),
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


def _chat_completion_interruptible(**kwargs: Any) -> dict[str, Any]:
    results: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            results.put(_chat_completion(**kwargs))
        except Exception as exc:
            results.put({"ok": False, "error": f"runtime error during model request: {exc}", "status": None, "text": "", "reasoning": ""})

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    while True:
        try:
            return results.get(timeout=0.15)
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            return {"ok": False, "cancelled": True, "error": "model request interrupted by user", "status": None, "text": "", "reasoning": ""}


def _register_process(process: subprocess.Popen[bytes]) -> None:
    with active_processes_lock:
        active_processes.add(process)


def _unregister_process(process: subprocess.Popen[bytes]) -> None:
    with active_processes_lock:
        active_processes.discard(process)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    runtime = _detect_runtime()
    try:
        if runtime["system"] == "Windows":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=3)
                return
            except Exception:
                process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except Exception:
                process.terminate()
        process.wait(timeout=3)
    except Exception:
        try:
            if runtime["system"] != "Windows":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def _terminate_active_processes() -> None:
    with active_processes_lock:
        processes = list(active_processes)
    for process in processes:
        _terminate_process(process)


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
        popen_kwargs: dict[str, Any] = {}
        if runtime["system"] == "Windows":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        def _decode_output(data: bytes | None) -> str:
            if not data:
                return ""
            return data.decode("utf-8", errors="replace")
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        _register_process(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                _terminate_process(process)
                stdout, stderr = process.communicate()
                output = _decode_output(stdout) + _decode_output(stderr)
                if len(output) > MAX_TOOL_OUTPUT_CHARS:
                    output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]..."
                return {
                    "exit_code": 124,
                    "output": (output.strip() + "\nCommand timed out after 120 seconds.").strip(),
                    "interrupted": False,
                    "terminated": True,
                }
        except KeyboardInterrupt:
            _terminate_process(process)
            stdout, stderr = process.communicate()
            output = (_decode_output(stdout) + _decode_output(stderr)).strip()
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]..."
            return {"exit_code": 130, "output": (output + "\nInterrupted by user.").strip(), "interrupted": True}
        finally:
            _unregister_process(process)
        output = _decode_output(stdout) + _decode_output(stderr)
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]..."
        exit_code = process.returncode
        terminated = exit_code < 0
        normalized_output = output.strip()
        if terminated:
            signal_number = abs(exit_code)
            suffix = f"Process terminated by signal {signal_number}."
            normalized_output = f"{normalized_output}\n{suffix}".strip() if normalized_output else suffix
        return {
            "exit_code": exit_code,
            "output": normalized_output,
            "interrupted": False,
            "terminated": terminated,
        }
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "output": f"Shell launch failed: {exc}",
            "interrupted": False,
            "terminated": False,
        }
    except OSError as exc:
        return {
            "exit_code": 127,
            "output": f"Command runner failed: {exc}",
            "interrupted": False,
            "terminated": False,
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
    _touch_render_budget(len(normalized) + 200)
    text = _display_text(normalized, style=f"italic dim {MUTED}")
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
    _touch_render_budget(len(title) + len(subtitle) + 40)
    text = _display_text(title, style=f"bold {ACCENT}")
    if subtitle:
        text.append("  ", style=DIM)
        text.append(subtitle, style=f"italic {DIM}")
    console.print(Padding(text, (0, 0, 0, RESPONSE_INDENT)))


def _render_markdown(markdown_text: str) -> None:
    content = markdown_text.strip() or "_No response._"
    _touch_render_budget(len(content) + 200)
    console.print(Padding(Markdown(_fold_long_display_text(content)), (0, 1, 0, RESPONSE_INDENT)), overflow="fold")


def _render_snippet(title: str, code: str, language: str = "text") -> None:
    _touch_render_budget(len(title) + len(code) + 200)
    syntax = Syntax(code.rstrip() or " ", language or "text", theme="monokai", line_numbers=False, word_wrap=True)
    console.print(
        Padding(
            Panel(syntax, title=f" {title} ", border_style=DIM, padding=(0, 1), style="dim"),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_command_request(command: str, reason: str) -> None:
    _touch_render_budget(len(command) + len(reason) + 200)
    group_items: list[Any] = [Syntax(command, "bash", theme="monokai", word_wrap=True)]
    if reason:
        group_items.append(_display_text(reason, style=f"italic dim {MUTED}"))
    console.print(
        Padding(
            Panel(Group(*group_items), border_style=DIM, title=" command ", padding=(0, 1), style="dim"),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_command_batch(command_items: list[dict[str, str]], reason: str) -> None:
    _touch_render_budget(sum(len(item["command"]) + len(item["reason"]) for item in command_items) + len(reason) + 300)
    blocks: list[Any] = []
    if reason:
        blocks.append(_display_text(reason, style=f"italic dim {MUTED}"))
    for item in command_items:
        blocks.append(Syntax(item["command"], "bash", theme="monokai", word_wrap=True))
        if item["reason"]:
            blocks.append(_display_text(item["reason"], style=f"italic dim {MUTED}"))
    console.print(
        Padding(
            Panel(Group(*blocks), border_style=DIM, title=" parallel commands ", padding=(0, 1), style="dim"),
            (0, 0, 0, PANEL_INDENT),
        )
    )


def _render_command_result(command: str, exit_code: int, output: str) -> None:
    _touch_render_budget(len(command) + len(output) + 250)
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


def _run_commands_sequential(command_items: list[dict[str, str]], cwd: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, item in enumerate(command_items):
        result = _run_command(item["command"], cwd)
        results.append(
            {
                "index": index,
                "command": item["command"],
                "reason": item["reason"],
                "exit_code": result["exit_code"],
                "output": result["output"],
                "interrupted": result.get("interrupted", False),
                "terminated": result.get("terminated", False),
            }
        )
        if result.get("interrupted") or result.get("terminated"):
            break
    return results


def _render_info(text: str) -> None:
    _touch_render_budget(len(text) + 40)
    console.print(Padding(_display_text(text, style=f"dim {MUTED}"), (0, 0, 0, RESPONSE_INDENT)), overflow="fold")


def _render_error_snippet(title: str, error_text: str) -> None:
    preview = error_text.strip()[:800] or "Unknown error"
    _touch_render_budget(len(title) + len(preview) + 100)
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
        return f"Running {count or 0} commands"
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
                            "- `/save [name]` save the current in-memory session",
                            "- `/autosave` toggle automatic saving for the current session",
                            "- `/context` show and refresh project context",
                            "- `/compact [keep]` compact old conversation messages",
                            "- `/clear` clear the in-memory conversation",
                            "- `/status` show session/runtime status",
                            "- `/attach <path> [instruction]` attach a local file to the next model turn",
                            "- `/stop` no-op at the prompt; during an interrupt prompt it discards the interrupted turn",
                            "- `/sessions` list saved sessions",
                            "- `/load <id|name>` load a saved session",
                            "- `/fork <id|name> [new-name]` copy a saved session into a new current session",
                            "- `/new [name]` start a new session",
                            "- `/delete <id|name>` delete a saved session",
                            "- `/update` check GitHub and self-update the installed agent",
                            "- `/model` reselect the model for this session",
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


def _render_sessions_table(sessions: list[dict[str, Any]]) -> None:
    table = Table(
        show_header=True,
        header_style=f"bold {ACCENT}",
        expand=True,
        border_style=DIM,
        padding=(0, 1),
    )
    table.add_column("ID", justify="right", no_wrap=True, ratio=1)
    table.add_column("Session", overflow="fold", no_wrap=False, ratio=3)
    table.add_column("Last Used", overflow="fold", no_wrap=False, ratio=4)
    table.add_column("Model", overflow="fold", no_wrap=False, ratio=3)
    table.add_column("Summary", overflow="fold", no_wrap=False, ratio=7)
    for idx, session in enumerate(sessions, start=1):
        table.add_row(
            str(idx),
            _fold_long_display_text(str(session["name"])),
            _fold_long_display_text(str(session["last_used_at"] or "-")),
            _fold_long_display_text(str(session["model"] or "-")),
            _fold_long_display_text(str(session["summary"] or "-")),
        )
    estimated = sum(
        len(str(session["name"])) + len(str(session["last_used_at"])) + len(str(session["model"])) + len(str(session["summary"]))
        for session in sessions
    )
    _touch_render_budget(estimated + 500)
    console.print(Padding(table, (0, 1, 0, RESPONSE_INDENT)), overflow="fold")


def _run_commands_parallel(command_items: list[dict[str, str]], cwd: str) -> list[dict[str, Any]]:
    indexed = list(enumerate(command_items))
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(indexed) or 1) as executor:
        future_map = {
            executor.submit(_run_command, item["command"], cwd): (index, item)
            for index, item in indexed
        }
        try:
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
                        "interrupted": result.get("interrupted", False),
                        "terminated": result.get("terminated", False),
                    }
                )
        except KeyboardInterrupt:
            _terminate_active_processes()
            for future in future_map:
                future.cancel()
            for index, item in indexed:
                if not any(result["index"] == index for result in results):
                    results.append(
                        {
                            "index": index,
                            "command": item["command"],
                            "reason": item["reason"],
                            "exit_code": 130,
                            "output": "Interrupted by user.",
                            "interrupted": True,
                            "terminated": False,
                        }
                    )
            return sorted(results, key=lambda item: item["index"])
    return sorted(results, key=lambda item: item["index"])


def _print_banner(model: str, cwd: str, runtime: dict[str, str]) -> None:
    command_text = (
        "commands  /help  /save  /autosave  /context  /compact  /clear  /status  /attach  /stop  "
        "/sessions  /load  /fork  /new  /delete  /update  /model  /key  /pwd  /alwaysRun  /exit"
    )
    header = Group(
        _display_text("THU CyberCraze Agent", style=f"bold {ACCENT}"),
        _display_text("interactive coding session", style=f"italic {DIM}"),
        _display_text(f"version {APP_VERSION}", style=MUTED),
        _display_text(f"model  {model}", style=MUTED),
        _display_text(f"cwd    {cwd}", style=MUTED),
        _display_text(f"os     {runtime['system']} {runtime['release']}  via {runtime['shell_label']}", style=MUTED),
        _display_text(command_text, style=DIM),
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


def _command_batch_parallel(action: dict[str, Any]) -> bool:
    raw = action.get("parallel")
    if isinstance(raw, bool):
        return raw
    return False


def _runtime_error_message(error_text: str) -> str:
    return (
        "The last tool or runtime step failed inside the agent.\n"
        "Treat this like a normal tool result, explain the problem briefly, and continue the task.\n"
        f"Runtime error:\n{error_text}"
    )


def _is_text_attachment(path: Path, mime_type: str | None) -> bool:
    if mime_type and mime_type.startswith("text/"):
        return True
    return path.suffix.lower() in {
        ".txt",
        ".md",
        ".markdown",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".csv",
        ".log",
        ".sh",
        ".ps1",
        ".bat",
        ".html",
        ".css",
        ".xml",
        ".sql",
        ".rs",
        ".go",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
    }


def _build_attachment_user_message(path_text: str, cwd: str, prompt_text: str = "") -> dict[str, Any]:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"attachment not found: {path}")
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        return {
            "role": "user",
            "content": (
                f"Attached file reference: {path}\n"
                f"Size: {size} bytes, larger than inline limit {MAX_ATTACHMENT_BYTES} bytes.\n"
                "Inspect it with shell commands before relying on its content."
                + (f"\nUser instruction: {prompt_text}" if prompt_text else "")
            ),
        }
    mime_type, _ = mimetypes.guess_type(str(path))
    if _is_text_attachment(path, mime_type):
        content = path.read_text(encoding="utf-8", errors="replace")
        truncated = ""
        if len(content) > MAX_ATTACHED_TEXT_CHARS:
            content = content[:MAX_ATTACHED_TEXT_CHARS]
            truncated = "\n...[truncated]..."
        return {
            "role": "user",
            "content": (
                f"Attached text file: {path}\n"
                f"MIME type: {mime_type or 'text/plain'}\n"
                f"Size: {size} bytes\n"
                + (f"User instruction: {prompt_text}\n" if prompt_text else "")
                + "Content:\n"
                f"```\n{content}{truncated}\n```"
            ),
        }
    if mime_type and mime_type.startswith("image/") and os.environ.get("THU_AGENT_MULTIMODAL") == "1":
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Attached image: {path}\n"
                        f"MIME type: {mime_type}\n"
                        f"Size: {size} bytes\n"
                        + (f"User instruction: {prompt_text}" if prompt_text else "Describe or inspect this image as relevant.")
                    ),
                },
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
            ],
        }
    return {
        "role": "user",
        "content": (
            f"Attached file reference: {path}\n"
            f"MIME type: {mime_type or 'unknown'}\n"
            f"Size: {size} bytes\n"
            "The file was not inlined. Use shell commands to inspect it if needed."
            + (f"\nUser instruction: {prompt_text}" if prompt_text else "")
        ),
    }


def _interrupt_followup_prompt() -> str:
    try:
        return _prompt("Interrupted. Enter follow-up, /stop to discard, or blank to return: ").strip()
    except (EOFError, KeyboardInterrupt):
        return "/stop"


def main() -> int:
    global prompt_session, startup_update_notice
    parser = argparse.ArgumentParser(description="Interactive THU lab proxy terminal agent")
    parser.add_argument("--model", choices=SUPPORTED_MODELS, help="Model name")
    parser.add_argument("--api-key", help="API key for the current session")
    parser.add_argument("--base-url", help="API base URL")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for shell commands")
    args = parser.parse_args()

    cwd = str(Path(args.cwd).resolve())
    runtime = _detect_runtime()
    file_env = _load_env_file(cwd)
    history_path = _global_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_session = PromptSession(
        history=FileHistory(str(history_path)),
        completer=_slash_command_completer(),
        complete_while_typing=True,
    )
    default_model = (
        os.environ.get("THU_AGENT_MODEL")
        or os.environ.get("THU_LAB_PROXY_MODEL")
        or DEFAULT_MODEL
    )
    model = args.model or _prompt_model(default_model)
    configured_base_url = (
        args.base_url
        or os.environ.get("THU_LAB_PROXY_BASE_URL")
        or file_env.get("THU_LAB_PROXY_BASE_URL")
        or DEFAULT_BASE_URL
    )
    env_key = (
        args.api_key
        or os.environ.get("THU_LAB_PROXY_API_KEY")
        or file_env.get("THU_LAB_PROXY_API_KEY")
    )
    base_url = _normalize_base_url(configured_base_url)
    api_key = args.api_key or _prompt_api_key(env_key)
    _save_api_key_to_env(api_key)
    _save_base_url_to_env(base_url)
    always_run = False
    autosave = False

    session_name = _default_session_name()
    project_context = _project_context(cwd)
    messages: list[dict[str, str]] = [_system_message(cwd, runtime, project_context)]
    startup_update_notice = _check_for_update_notice()
    _print_banner(model, cwd, runtime)
    if startup_update_notice:
        _render_info(startup_update_notice)

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
        if user_input.startswith("/save"):
            _, _, raw_name = user_input.partition(" ")
            if raw_name.strip():
                session_name = _slugify_session_name(raw_name)
            _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"saved session {session_name}")
            continue
        if user_input == "/autosave":
            autosave = not autosave
            state = "enabled" if autosave else "disabled"
            _render_info(f"autosave {state} for current session")
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            continue
        if user_input == "/context":
            project_context = _project_context(cwd)
            messages[0] = _system_message(cwd, runtime, project_context)
            _render_markdown("## Project Context\n\n" + (project_context or "_No project context found._"))
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            continue
        if user_input.startswith("/compact"):
            _, _, raw_keep = user_input.partition(" ")
            keep_recent = 10
            if raw_keep.strip().isdigit():
                keep_recent = max(4, min(30, int(raw_keep.strip())))
            before_count = len(messages)
            before_chars = _estimate_context_chars(messages)
            messages = _compact_messages(messages, keep_recent=keep_recent)
            after_chars = _estimate_context_chars(messages)
            _render_info(
                f"compacted {before_count} -> {len(messages)} messages; context chars {before_chars} -> {after_chars}"
            )
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            continue
        if user_input == "/clear":
            messages = [_system_message(cwd, runtime, project_context)]
            _render_info("cleared in-memory conversation")
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            continue
        if user_input == "/status":
            status_lines = [
                "## Status",
                f"- Version: `{APP_VERSION}`",
                f"- Model: `{model}`",
                f"- Session: `{session_name}`",
                f"- Autosave: `{'on' if autosave else 'off'}`",
                f"- AlwaysRun: `{'on' if always_run else 'off'}`",
                f"- Multimodal attachments: `{'on' if os.environ.get('THU_AGENT_MULTIMODAL') == '1' else 'off'}`",
                f"- Messages: `{len(messages)}`",
                f"- Context chars: `{_estimate_context_chars(messages)}`",
                f"- CWD: `{cwd}`",
                f"- Memory files: `{len(_find_memory_files(cwd))}`",
            ]
            _render_markdown("\n".join(status_lines))
            continue
        if user_input.startswith("/attach"):
            _, _, raw_args = user_input.partition(" ")
            raw_args = raw_args.strip()
            if not raw_args:
                raw_args = _prompt("File path: ").strip()
            if not raw_args:
                _render_info("attachment path is required")
                continue
            try:
                attach_parts = shlex.split(raw_args, posix=platform.system() != "Windows")
            except ValueError as exc:
                _render_error_snippet("attachment error", str(exc))
                continue
            path_part = attach_parts[0] if attach_parts else ""
            prompt_part = " ".join(attach_parts[1:])
            try:
                attachment_message = _build_attachment_user_message(path_part, cwd, prompt_part.strip())
            except OSError as exc:
                _render_error_snippet("attachment error", str(exc))
                continue
            messages.append(attachment_message)
            messages = _trim_history(messages)
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"attached {Path(path_part).name}; send a prompt or let the model inspect it next")
            continue
        if user_input == "/stop":
            _render_info("no active task at the prompt; press Ctrl+C while the agent is thinking or running a command to interrupt it")
            continue
        if user_input == "/update":
            latest_version = _fetch_latest_version()
            if latest_version and _version_key(latest_version) <= _version_key(APP_VERSION):
                _render_info(f"already up to date at {APP_VERSION}")
                continue
            confirm = _prompt("Update from GitHub now? [Y/n] ").strip().lower()
            if confirm not in {"", "y", "yes"}:
                _render_info("update cancelled")
                continue
            _render_step("Updating")
            with console.status("[dim]updating from GitHub…[/dim]", spinner="dots"):
                ok, message, should_exit = _perform_update(runtime)
            if ok:
                _render_info(message)
                if should_exit:
                    return 0
            else:
                _render_error_snippet("update error", message)
            continue
        if user_input == "/sessions":
            sessions = _list_sessions()
            if not sessions:
                _render_info("no saved sessions")
            else:
                _render_sessions_table(sessions)
            continue
        if user_input.startswith("/load"):
            _, _, raw_name = user_input.partition(" ")
            session_query = raw_name.strip() or _prompt("Session name: ").strip()
            if not session_query:
                _render_info("session name is required")
                continue
            try:
                payload = _load_session(_resolve_session_reference(session_query))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                _render_error_snippet("session load error", str(exc))
                continue
            loaded_messages = payload.get("messages")
            loaded_model = str(payload.get("model", model)).strip() or model
            if not isinstance(loaded_messages, list) or not loaded_messages:
                _render_error_snippet("session load error", "session has no valid messages")
                continue
            session_name = str(payload.get("name", _slugify_session_name(session_query))).strip() or _slugify_session_name(session_query)
            model = loaded_model if loaded_model in SUPPORTED_MODELS else model
            messages = loaded_messages
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"loaded session {session_name}")
            continue
        if user_input.startswith("/fork"):
            _, _, raw_args = user_input.partition(" ")
            parts = raw_args.strip().split(maxsplit=1) if raw_args.strip() else []
            source_ref = parts[0] if parts else _prompt("Session id or name: ").strip()
            if not source_ref:
                _render_info("session id or name is required")
                continue
            fork_name = parts[1].strip() if len(parts) > 1 else ""
            if not fork_name:
                fork_name = _prompt("New session name (optional): ").strip()
            try:
                payload = _load_session(_resolve_session_reference(source_ref))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                _render_error_snippet("session fork error", str(exc))
                continue
            loaded_messages = payload.get("messages")
            loaded_model = str(payload.get("model", model)).strip() or model
            if not isinstance(loaded_messages, list) or not loaded_messages:
                _render_error_snippet("session fork error", "session has no valid messages")
                continue
            session_name = _slugify_session_name(fork_name) if fork_name else _default_session_name()
            model = loaded_model if loaded_model in SUPPORTED_MODELS else model
            messages = loaded_messages
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"forked session into {session_name}" + ("" if autosave else " (not saved yet)"))
            continue
        if user_input.startswith("/new"):
            _, _, raw_name = user_input.partition(" ")
            session_name = _slugify_session_name(raw_name) if raw_name.strip() else _default_session_name()
            project_context = _project_context(cwd)
            messages = [_system_message(cwd, runtime, project_context)]
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"started new session {session_name}" + ("" if autosave else " (not saved yet)"))
            continue
        if user_input.startswith("/delete"):
            _, _, raw_name = user_input.partition(" ")
            session_query = raw_name.strip() or _prompt("Session name: ").strip()
            if not session_query:
                _render_info("session name is required")
                continue
            try:
                resolved_name = _resolve_session_reference(session_query)
            except FileNotFoundError as exc:
                _render_info(str(exc))
                continue
            deleted = _delete_session(resolved_name)
            if deleted:
                _render_info(f"deleted session {resolved_name}")
                if resolved_name == session_name:
                    session_name = _default_session_name()
                    project_context = _project_context(cwd)
                    messages = [_system_message(cwd, runtime, project_context)]
                    if autosave:
                        _save_session(session_name, model=model, cwd=cwd, messages=messages)
                    _render_info(f"started new session {session_name}" + ("" if autosave else " (not saved yet)"))
            else:
                _render_info(f"session not found: {resolved_name}")
            continue
        if user_input == "/model":
            model = _prompt_model(model)
            project_context = _project_context(cwd)
            messages = [_system_message(cwd, runtime, project_context)]
            if autosave:
                _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"model switched to {model}")
            continue
        if user_input == "/key":
            api_key = _prompt_api_key(None)
            _save_api_key_to_env(api_key)
            console.print(
                Padding(
                    f"API key updated and saved to {_global_env_path()}.",
                    (0, 0, 0, RESPONSE_INDENT),
                ),
                style=SUCCESS,
            )
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
        if autosave:
            _save_session(session_name, model=model, cwd=cwd, messages=messages)

        api_error_recovery_count = 0
        while True:
            try:
                exit_to_prompt = False
                while True:
                    _render_step("Thinking")
                    with console.status("[dim]thinking…[/dim]", spinner="dots"):
                        response = _chat_completion_interruptible(
                            api_key=api_key,
                            model=model,
                            messages=messages,
                            base_url=base_url,
                        )
                    if response.get("cancelled"):
                        _render_step("Cancelled")
                        _render_info("interrupted current model request")
                        followup = _interrupt_followup_prompt()
                        if followup == "/stop" and messages and messages[-1].get("role") == "user":
                            messages.pop()
                            _render_info("discarded interrupted user turn")
                            break
                        if followup:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": "The user interrupted the previous model request and added this follow-up instruction:\n" + followup,
                                }
                            )
                            messages = _trim_history(messages)
                            if autosave:
                                _save_session(session_name, model=model, cwd=cwd, messages=messages)
                            continue
                        break
                    if not response["ok"]:
                        _render_step("Upstream Error")
                        console.print(Padding(f"upstream error: {response['error']}", (0, 0, 0, RESPONSE_INDENT)), style=ERROR)
                        retries = response.get("retries")
                        if isinstance(retries, list) and retries:
                            _render_info(f"retry attempts already made: {len(retries)}")
                        if response.get("status") == 404:
                            _render_info(f"active base URL: {base_url}")
                            _render_info("this 404 is coming from the upstream proxy, not from local command execution.")
                            _render_info("check the selected model, retry later, or rotate the proxy key if access changed.")
                        if _is_invalid_api_key(str(response["error"]), response.get("status")):
                            _render_info("stored API key appears invalid or expired. enter a new key.")
                            api_key = _prompt_api_key(None)
                            _save_api_key_to_env(api_key)
                            _render_info(f"saved updated API key to {_global_env_path()}")
                            continue
                        if _is_context_overflow_error(str(response["error"])):
                            before_chars = _estimate_context_chars(messages)
                            messages = _compact_messages(messages, keep_recent=8)
                            after_chars = _estimate_context_chars(messages)
                            _render_info(f"compacted context after upstream context error: {before_chars} -> {after_chars} chars")
                            if autosave:
                                _save_session(session_name, model=model, cwd=cwd, messages=messages)
                            continue
                        if response.get("status") in {None, 400, 408, 409, 425, 429, 500, 502, 503, 504}:
                            api_error_recovery_count += 1
                            if api_error_recovery_count > MAX_API_ERROR_RECOVERY:
                                _render_info("stopped upstream-error recovery to avoid an infinite retry loop")
                                exit_to_prompt = True
                                break
                            _render_info("attempting to continue after upstream error")
                            messages.append({"role": "user", "content": _runtime_error_message(str(response["error"]))})
                            messages = _trim_history(messages)
                            if autosave:
                                _save_session(session_name, model=model, cwd=cwd, messages=messages)
                            continue
                        break

                    api_error_recovery_count = 0
                    assistant_text = response["text"].strip()
                    messages.append({"role": "assistant", "content": assistant_text})
                    if autosave:
                        _save_session(session_name, model=model, cwd=cwd, messages=messages)
                    action = _extract_json_object(assistant_text)
                    reasoning_text = _extract_reasoning_for_display(response, assistant_text, action)
                    _render_reasoning(reasoning_text)

                    if not action:
                        messages.append({"role": "user", "content": _repair_instruction(assistant_text)})
                        messages = _trim_history(messages)
                        if autosave:
                            _save_session(session_name, model=model, cwd=cwd, messages=messages)
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
                        exit_to_prompt = True
                        break

                    if action_type == "run_many":
                        command_items = _normalize_command_batch(action)
                        run_parallel = _command_batch_parallel(action)
                        batch_results_interrupted = False
                        if not command_items:
                            console.print("empty command batch request", style=ERROR)
                            exit_to_prompt = True
                            break
                        _render_step(_action_summary("run_many", str(action.get("reason", "")).strip(), len(command_items)))
                        _render_command_batch(command_items, str(action.get("reason", "")).strip())
                        if not _prompt_run_command(always_run):
                            tool_result = "Command batch was not approved by the user."
                            _render_info(tool_result)
                        else:
                            mode_label = "in parallel" if run_parallel else "sequentially"
                            _render_step("Running Commands", f"{len(command_items)} {mode_label}")
                            try:
                                with console.status("[dim]running commands…[/dim]", spinner="dots"):
                                    if run_parallel:
                                        results = _run_commands_parallel(command_items, cwd)
                                    else:
                                        results = _run_commands_sequential(command_items, cwd)
                            except KeyboardInterrupt:
                                _terminate_active_processes()
                                _render_step("Cancelled")
                                _render_info("interrupted command batch")
                                results = [
                                    {
                                        "index": 0,
                                        "command": "(batch)",
                                        "reason": "interrupted command batch",
                                        "exit_code": 130,
                                        "output": "Interrupted by user.",
                                        "interrupted": True,
                                        "terminated": False,
                                    }
                                ]
                            _render_step("Command Results")
                            rendered_chunks: list[str] = []
                            for result in results:
                                _render_command_result(result["command"], result["exit_code"], result["output"])
                                status_line = ""
                                if result.get("terminated"):
                                    status_line = "Status: terminated unexpectedly"
                                elif result.get("interrupted"):
                                    status_line = "Status: interrupted by user"
                                rendered_chunks.append(
                                    "\n".join(
                                        [line for line in [
                                            f"Command: {result['command']}",
                                            f"Reason: {result['reason']}",
                                            f"Exit code: {result['exit_code']}",
                                            status_line,
                                            "Output:",
                                            result["output"],
                                        ] if line]
                                    )
                                )
                                if result.get("terminated"):
                                    _render_info("command batch stopped because a command terminated unexpectedly")
                                    break
                                if result.get("interrupted"):
                                    batch_results_interrupted = True
                                    break
                            tool_result = "\n\n".join(rendered_chunks)
                        messages.append({"role": "user", "content": _tool_result_message(tool_result)})
                        messages = _trim_history(messages)
                        if autosave:
                            _save_session(session_name, model=model, cwd=cwd, messages=messages)
                        if batch_results_interrupted:
                            followup = _interrupt_followup_prompt()
                            if followup == "/stop":
                                _render_info("stopped after interrupted command batch")
                                exit_to_prompt = True
                                break
                            if followup:
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": "The user interrupted the previous command batch and added this follow-up instruction:\n" + followup,
                                    }
                                )
                                messages = _trim_history(messages)
                                if autosave:
                                    _save_session(session_name, model=model, cwd=cwd, messages=messages)
                        continue

                    if action_type != "run":
                        console.print("invalid tool response from model", style=ERROR)
                        _render_snippet("raw", assistant_text, "json")
                        exit_to_prompt = True
                        break

                    command = str(action.get("command", "")).strip()
                    reason = str(action.get("reason", "")).strip()
                    if not command:
                        console.print("empty command request", style=ERROR)
                        exit_to_prompt = True
                        break

                    _render_step(_action_summary("run", reason))
                    _render_command_request(command, reason)
                    single_result_interrupted = False
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
                        if result.get("terminated"):
                            tool_result = (
                                f"Command: {command}\n"
                                f"Exit code: {result['exit_code']}\n"
                                "Status: terminated unexpectedly\n"
                                f"Output:\n{result['output']}"
                            )
                        _render_command_result(command, result["exit_code"], result["output"])
                        if result.get("terminated"):
                            _render_info("command terminated unexpectedly")
                        if result.get("interrupted"):
                            _render_step("Cancelled")
                            _render_info("interrupted current command")
                            single_result_interrupted = True

                    messages.append({"role": "user", "content": _tool_result_message(tool_result)})
                    messages = _trim_history(messages)
                    if autosave:
                        _save_session(session_name, model=model, cwd=cwd, messages=messages)
                    stop_after_single_interrupt = False
                    if single_result_interrupted:
                        followup = _interrupt_followup_prompt()
                        if followup == "/stop":
                            _render_info("stopped after interrupted command")
                            stop_after_single_interrupt = True
                        elif followup:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": "The user interrupted the previous command and added this follow-up instruction:\n" + followup,
                                }
                            )
                            messages = _trim_history(messages)
                            if autosave:
                                _save_session(session_name, model=model, cwd=cwd, messages=messages)
                    if stop_after_single_interrupt:
                        exit_to_prompt = True
                        break
                    continue
                if exit_to_prompt:
                    break
            except Exception as exc:
                _render_step("Runtime Error")
                _render_error_snippet("runtime error", str(exc))
                messages.append({"role": "user", "content": _runtime_error_message(str(exc))})
                messages = _trim_history(messages)
                if autosave:
                    _save_session(session_name, model=model, cwd=cwd, messages=messages)
                _render_info("attempting to continue after runtime error")
                continue

        console.print(Rule(style=DIM))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        console.print(Padding("fatal runtime error", (0, 0, 0, RESPONSE_INDENT)), style=ERROR)
        _render_error_snippet("fatal error", str(exc))
        raise SystemExit(1)
