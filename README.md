# THU CyberCraze Agent

Interactive terminal coding agent powered by the THU lab proxy (OpenAI-compatible API). It runs in your current terminal, works in your current directory, can inspect files, propose shell commands, and wait for your approval before running them.

## 1. Installation

### 1.1 Get an API key

Create a key here:

```text
https://lab.cs.tsinghua.edu.cn/ai-platform/c/new
```

Base URL:

```text
https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1
```

Set environment variables:

```bash
export THU_LAB_PROXY_API_KEY='your_proxy_key_here'
export THU_LAB_PROXY_BASE_URL='https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1'
```

Windows PowerShell:

```powershell
$env:THU_LAB_PROXY_API_KEY='your_proxy_key_here'
$env:THU_LAB_PROXY_BASE_URL='https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1'
```

You can also launch the agent and paste the key when prompted. The agent saves it into a per-user global config file:

- Linux and macOS: `~/.thu-cybercraze-agent/.env`
- Windows: `%USERPROFILE%\.thu-cybercraze-agent\.env`

### 1.2 Run the agent

Linux (built binary):

```bash
./dist/thu-agent
```

Windows (built on Windows):

```powershell
.\dist\thu-agent.exe
```

macOS (run Python directly):

```bash
python3 agent.py
```

### 1.3 Build the binaries (if needed)

Linux build:

```bash
bash build_agent.sh
```

Result:

```text
dist/thu-agent
```

Windows build (run on Windows, not inside WSL):

```powershell
py -3 -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\build_agent_windows.ps1
```

Result:

```text
dist\thu-agent.exe
```

### 1.4 Optional: Run globally

Linux:

```bash
sudo install -m 755 dist/thu-agent /usr/local/bin/thu-agent
```

Windows: add the repo `dist` directory to `PATH`, or copy the `.exe` into a directory already on `PATH`.

Example (PowerShell):

```powershell
[Environment]::SetEnvironmentVariable(
  "Path",
  $env:Path + ";C:\Users\USER\Downloads\THU-deepseek-glm-api-mcp-server\dist",
  "User"
)
```

Open a new terminal and run:

```powershell
thu-agent.exe
```

## 2. Usage

Start the agent:

```bash
./dist/thu-agent
```

Or run with Python:

```bash
python3 agent.py
```

Pass model and key directly if you want:

```bash
python3 agent.py --model deepseek-v3.2 --api-key "$THU_LAB_PROXY_API_KEY"
```

Default model:

```text
deepseek-v3.2
```

Current models:

- `qwen3-max-thinking`
- `qwen3-max`
- `glm-5`
- `glm-5-thinking`
- `glm-4.7-thinking`
- `kimi-k2.5`
- `kimi-k2.5-thinking`
- `minimax-m2.5`
- `minimax-m2.5-thinking`
- `qwen3.5-plus`
- `qwen3.5-plus-thinking`
- `qwen3.5-mini`
- `deepseek-v3.2-thinking`
- `deepseek-v3.2`

While the agent is thinking or running a command, press `Ctrl+C` to interrupt. It will ask for a follow-up instruction. Type `/stop` there to discard the interrupted turn, or type a new instruction to continue.

## 3. Function List

Slash commands available:

- `/help`
- `/save [name]`
- `/autosave`
- `/context`
- `/compact [keep]`
- `/clear`
- `/status`
- `/attach <path> [instruction]`
- `/stop`
- `/sessions`
- `/load <id|name>`
- `/fork <id|name> [new-name]`
- `/new [name]`
- `/delete <id|name>`
- `/update`
- `/model`
- `/key`
- `/pwd`
- `/alwaysRun`
- `/exit`

## 4. Function Explanation

Session and memory:

- `/save [name]` saves the current session to disk. Sessions are manual-save by default.
- `/autosave` toggles automatic saving for this session.
- `/sessions` lists saved sessions with an ID, summary, and last-used time.
- `/load <id|name>` loads a saved session.
- `/fork <id|name> [new-name]` creates a new session from a saved one.
- `/new [name]` starts a new session with a fresh context.
- `/delete <id|name>` deletes a saved session.

Context management:

- `/context` refreshes and displays the startup context snapshot (date, git status, nearby memory files like `AGENTS.md` or `CLAUDE.md`).
- `/compact [keep]` summarizes older messages and keeps recent turns to reduce context size.
- `/clear` clears in-memory conversation while preserving current project context.
- `/status` shows version, model, session name, autosave state, message count, and context size.

Commands and execution:

- `/alwaysRun` toggles auto-approval for shell commands.
- `/stop` is used only after an interrupt prompt to discard the interrupted turn.

Attachments:

- `/attach path/to/file.txt explain this file` inlines small text/code files into the next model turn.
- Non-text files are passed as file references for the agent to inspect with commands.
- Image files can be sent as multimodal content only when the selected model/proxy supports it and `THU_AGENT_MULTIMODAL=1` is set. Otherwise they are treated as file references.

Models and keys:

- `/model` reselects the model (this resets the conversation context).
- `/key` updates the API key and saves it to the global `.env`.

Updates:

- At startup, the agent compares its embedded version with the GitHub `VERSION` file and reminds you if it is behind.
- `/update` clones the GitHub repo to a temp directory, rebuilds the binary, installs it to the current executable path (or `/usr/local/bin/thu-agent` on Linux), then removes the temp clone. On Windows it stages a post-exit replacement of the running `.exe`.

Other:

- `/pwd` prints the current working directory.
- `/help` shows the command list.
- `/exit` quits the agent.

Notes:

- The MCP server in `server.py` is separate from the interactive agent in `agent.py`.
