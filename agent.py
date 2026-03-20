#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
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
from rich.text import Text

DEFAULT_BASE_URL = "https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1"
DEFAULT_MODEL = "deepseek-v3.2"
APP_VERSION = "0.5.1"
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


def _slash_commands() -> list[str]:
    return [
        "/help",
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
            text = str(message.get("content", "")).strip().replace("\n", " ")
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
        install_result = _run_update_command(["install", "-m", "755", str(source_bin), str(target_bin)])
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


def _should_retry(status_code: int | None, message: str | None) -> bool:
    if status_code in {400, 408, 409, 425, 429, 500, 502, 503, 504}:
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
                response = client.post(url, headers=_headers(api_key), json=body)
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
                if attempt < max_retries and status_code in {400, 408, 409, 425, 429, 500, 502, 503, 504}:
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
            "When you need multiple commands in one tool round use:\n",
            '{"type":"run_many","reasoning":["short step","short step"],"parallel":false,"commands":[{"command":"pwd","reason":"confirm current directory"},{"command":"rg --files","reason":"list files"}],"reason":"gather context in one batch"}\n',
            "Set parallel=true only when the commands are independent and safe to run concurrently.\n",
            "If a later command depends on an earlier command, use run_many with parallel=false.\n",
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
        def _decode_output(data: bytes | None) -> str:
            if not data:
                return ""
            return data.decode("utf-8", errors="replace")
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(timeout=120)
        except KeyboardInterrupt:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            output = (_decode_output(stdout) + _decode_output(stderr)).strip()
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]..."
            return {"exit_code": 130, "output": (output + "\nInterrupted by user.").strip(), "interrupted": True}
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
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 124,
            "output": "Command timed out after 120 seconds.",
            "interrupted": False,
            "terminated": False,
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
    _touch_render_budget(len(title) + len(subtitle) + 40)
    text = Text(title, style=f"bold {ACCENT}")
    if subtitle:
        text.append("  ", style=DIM)
        text.append(subtitle, style=f"italic {DIM}")
    console.print(Padding(text, (0, 0, 0, RESPONSE_INDENT)))


def _render_markdown(markdown_text: str) -> None:
    content = markdown_text.strip() or "_No response._"
    _touch_render_budget(len(content) + 200)
    console.print(Padding(Markdown(content), (0, 1, 0, RESPONSE_INDENT)))


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
        group_items.append(Text(reason, style=f"italic dim {MUTED}"))
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
    console.print(Padding(Text(text, style=f"dim {MUTED}"), (0, 0, 0, RESPONSE_INDENT)))


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
                    "terminated": result.get("terminated", False),
                }
            )
    return sorted(results, key=lambda item: item["index"])


def _print_banner(model: str, cwd: str, runtime: dict[str, str]) -> None:
    header = Group(
        Text("THU CyberCraze Agent", style=f"bold {ACCENT}"),
        Text("interactive coding session", style=f"italic {DIM}"),
        Text(f"version {APP_VERSION}", style=MUTED),
        Text(f"model  {model}", style=MUTED),
        Text(f"cwd    {cwd}", style=MUTED),
        Text(f"os     {runtime['system']} {runtime['release']}  via {runtime['shell_label']}", style=MUTED),
        Text("commands  /help  /sessions  /load  /fork  /new  /delete  /update  /model  /key  /pwd  /alwaysRun  /exit", style=DIM),
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

    session_name = _default_session_name()
    messages: list[dict[str, str]] = [{"role": "system", "content": _agent_system_prompt(cwd, runtime)}]
    _save_session(session_name, model=model, cwd=cwd, messages=messages)
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
                lines = ["| ID | Session | Last Used | Model | Summary |", "| --- | --- | --- | --- | --- |"]
                for idx, session in enumerate(sessions, start=1):
                    lines.append(
                        f"| {idx} | `{session['name']}` | {session['last_used_at'] or '-'} | "
                        f"{session['model'] or '-'} | {session['summary']} |"
                    )
                _render_markdown("\n".join(lines))
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
            _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"forked session into {session_name}")
            continue
        if user_input.startswith("/new"):
            _, _, raw_name = user_input.partition(" ")
            session_name = _slugify_session_name(raw_name) if raw_name.strip() else _default_session_name()
            messages = [{"role": "system", "content": _agent_system_prompt(cwd, runtime)}]
            _save_session(session_name, model=model, cwd=cwd, messages=messages)
            _render_info(f"started new session {session_name}")
            continue
        if user_input.startswith("/delete"):
            _, _, raw_name = user_input.partition(" ")
            session_query = raw_name.strip() or _prompt("Session name: ").strip()
            if not session_query:
                _render_info("session name is required")
                continue
            resolved_name = _resolve_session_reference(session_query)
            deleted = _delete_session(resolved_name)
            if deleted:
                _render_info(f"deleted session {resolved_name}")
                if resolved_name == session_name:
                    session_name = _default_session_name()
                    messages = [{"role": "system", "content": _agent_system_prompt(cwd, runtime)}]
                    _save_session(session_name, model=model, cwd=cwd, messages=messages)
                    _render_info(f"started new session {session_name}")
            else:
                _render_info(f"session not found: {resolved_name}")
            continue
        if user_input == "/model":
            model = _prompt_model(model)
            messages = [{"role": "system", "content": _agent_system_prompt(cwd, runtime)}]
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
        _save_session(session_name, model=model, cwd=cwd, messages=messages)

        while True:
            try:
                while True:
                    _render_step("Thinking")
                    try:
                        with console.status("[dim]thinking…[/dim]", spinner="dots"):
                            response = _chat_completion(
                                api_key=api_key,
                                model=model,
                                messages=messages,
                                base_url=base_url,
                            )
                    except KeyboardInterrupt:
                        _render_step("Cancelled")
                        _render_info("interrupted current model request")
                        if messages and messages[-1].get("role") == "user":
                            messages.pop()
                        break
                    if not response["ok"]:
                        _render_step("Upstream Error")
                        console.print(Padding(f"upstream error: {response['error']}", (0, 0, 0, RESPONSE_INDENT)), style=ERROR)
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
                        if response.get("status") in {None, 400, 408, 409, 425, 429, 500, 502, 503, 504}:
                            _render_info("attempting to continue after upstream error")
                            messages.append({"role": "user", "content": _runtime_error_message(str(response["error"]))})
                            messages = _trim_history(messages)
                            _save_session(session_name, model=model, cwd=cwd, messages=messages)
                            continue
                        break

                    assistant_text = response["text"].strip()
                    messages.append({"role": "assistant", "content": assistant_text})
                    _save_session(session_name, model=model, cwd=cwd, messages=messages)
                    action = _extract_json_object(assistant_text)
                    reasoning_text = _extract_reasoning_for_display(response, assistant_text, action)
                    _render_reasoning(reasoning_text)

                    if not action:
                        messages.append({"role": "user", "content": _repair_instruction(assistant_text)})
                        messages = _trim_history(messages)
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
                        break

                    if action_type == "run_many":
                        command_items = _normalize_command_batch(action)
                        run_parallel = _command_batch_parallel(action)
                        if not command_items:
                            console.print("empty command batch request", style=ERROR)
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
                                _render_step("Cancelled")
                                _render_info("interrupted command batch")
                                break
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
                                    break
                            tool_result = "\n\n".join(rendered_chunks)
                        messages.append({"role": "user", "content": _tool_result_message(tool_result)})
                        messages = _trim_history(messages)
                        _save_session(session_name, model=model, cwd=cwd, messages=messages)
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
                            break

                    messages.append({"role": "user", "content": _tool_result_message(tool_result)})
                    messages = _trim_history(messages)
                    _save_session(session_name, model=model, cwd=cwd, messages=messages)
                break
            except Exception as exc:
                _render_step("Runtime Error")
                _render_error_snippet("runtime error", str(exc))
                messages.append({"role": "user", "content": _runtime_error_message(str(exc))})
                messages = _trim_history(messages)
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
