# NO RATE LIMIT FOR THU STUDENT!! THU Agent by CyberCraze

Interactive terminal coding agent powered by the THU lab proxy OpenAI-compatible API.

The agent runs in your current terminal, works in your current directory, can inspect files, propose shell commands, and wait for your approval before running them.

## Platform Use

### Linux

Use the built executable:

```bash
./dist/thu-agent
```

Linux executable path:

```text
dist/thu-agent
```

To run it globally, copy or symlink it into a directory on your `PATH`, for example:

```bash
sudo install -m 755 dist/thu-agent /usr/local/bin/thu-agent
```

Then run:

```bash
thu-agent
```

### Windows

Use the Windows executable after building it on Windows:

```powershell
.\dist\thu-agent.exe
```

Windows executable path:

```text
dist\thu-agent.exe
```

To run it globally on Windows, add the repo `dist` directory to your `PATH`, or copy the executable into a directory already on `PATH`.

Example PowerShell command to add the current repo `dist` directory for your user:

```powershell
[Environment]::SetEnvironmentVariable(
  "Path",
  $env:Path + ";C:\Users\USER\Downloads\THU-deepseek-glm-api-mcp-server\dist",
  "User"
)
```

Then open a new terminal and run:

```powershell
thu-agent.exe
```

Build it from Windows with:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_agent_windows.ps1
```

### macOS

There is no packaged macOS binary in this repo.

Run the Python entrypoint directly:

```bash
python3 agent.py
```

If you want a global command on macOS, create a small wrapper in `/usr/local/bin` or another directory on your `PATH`:

```bash
sudo ln -sf "/absolute/path/to/agent.py" /usr/local/bin/thu-agent.py
```

or run the repo-local command directly from a shell alias.

## API Setup

The agent uses the THU lab proxy.

Create an API key first at:

```text
https://lab.cs.tsinghua.edu.cn/ai-platform/c/new
```

Base URL:

```text
https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1
```

Set your key with an environment variable:

```bash
export THU_LAB_PROXY_API_KEY='your_proxy_key_here'
export THU_LAB_PROXY_BASE_URL='https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1'
```

On Windows PowerShell:

```powershell
$env:THU_LAB_PROXY_API_KEY='your_proxy_key_here'
$env:THU_LAB_PROXY_BASE_URL='https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1'
```

You can also launch the agent and paste the key when prompted. The agent saves it into a per-user global config file for reuse.

Config location:

- Linux and macOS: `~/.thu-cybercraze-agent/.env`
- Windows: `%USERPROFILE%\.thu-cybercraze-agent\.env`

## Start the Agent

From the repo root:

```bash
./dist/thu-agent
```

Or with Python:

```bash
python3 agent.py
```

You can also pass the model and key directly:

```bash
python3 agent.py --model deepseek-v3.2 --api-key "$THU_LAB_PROXY_API_KEY"
```

## Model Selection

The startup picker shows the models currently wired into the agent.

Default model:

```text
deepseek-v3.2
```

Current supported models:

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

## In-Agent Commands

Slash commands available in the session:

- `/help`
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

At startup, the agent compares its embedded version with the GitHub `VERSION` file. If a newer release exists, it shows a short reminder to run `/update`.

`/update` behavior:

- Linux: clones the GitHub repo to a temporary directory, rebuilds the binary, installs it to the current executable path or `/usr/local/bin/thu-agent`, and removes the temporary clone. If the install target needs elevated permissions, run the agent with appropriate privileges or update manually.
- Windows: stages a post-exit replacement of the running `.exe` after rebuilding from a temporary clone, then exits so the replacement can complete.

While the agent is thinking or running a command, press `Ctrl+C` to cancel the current operation and return to the prompt without exiting the whole session.

## Typical Workflow

1. Start the agent.
2. Choose a model or press Enter for the default.
3. Reuse the saved API key or paste a new one.
4. Type requests at the `>` prompt.
5. Approve commands when the agent asks.

Example prompts:

- `list the files in this directory`
- `write a hello world script in python`
- `inspect this project and explain how to run it`
- `create a small bash script that prints the current date`

## Command Approval

By default, the agent asks before running each command.

To auto-approve commands for the current session:

```text
/alwaysRun
```

Use that carefully.

## Build

### Linux build

```bash
bash build_agent.sh
```

Result:

```text
dist/thu-agent
```

This build uses the current Python environment and PyInstaller, with extra excludes plus strip/optimize enabled to keep the binary smaller.

### Windows build

Run this on Windows, not inside WSL:

```powershell
py -3 -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\build_agent_windows.ps1
```

Result:

```text
dist\thu-agent.exe
```

### macOS run path

macOS users should run the Python entrypoint directly:

```bash
python3 agent.py
```

## Direct API Test

You can test the proxy directly:

```bash
curl --location --request POST \
  'https://lab.cs.tsinghua.edu.cn/ai-platform/api/v1/chat/completions' \
  --header 'Content-Type: application/json' \
  --header "authorization: Bearer $THU_LAB_PROXY_API_KEY" \
  --data-raw '{
    "model": "deepseek-v3.2",
    "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
    "temperature": 0.2,
    "repetition_penalty": 1.1,
    "stream": false
  }'
```

## Notes

- The Linux binary is already buildable from this repo.
- The Windows `.exe` must be built from a Windows Python environment.
- macOS users should run `agent.py` directly unless they package it themselves.
- The MCP server code in `server.py` still uses the older backend and is separate from the interactive agent in `agent.py`.
