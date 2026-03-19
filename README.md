# Tsinghua DeepSeek MCP Server (Linux)

A small MCP server for Linux that wraps the Tsinghua DeepSeek-compatible chat API.

This repo also includes a standalone interactive terminal agent executable.
The interactive agent can also use OpenRouter for the `qwen/qwen3-coder:free` model.

## Features

- MCP tools:
  - `list_models`
  - `health_check`
  - `simple_chat`
  - `chat_completion`
- MCP resource:
  - `config://deepseek`
- Supports both:
  - `stdio` transport for local IDE/agent clients
  - `streamable-http` transport for HTTP MCP clients
- HTTP mode is served through the `mcp` package's built-in uvicorn integration.
- Standalone terminal agent:
  - prompts for model and API key
  - runs in the current directory
  - can ask to execute shell commands with your approval

## 1) Install

### Option A: `uv` (recommended)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /mnt/c/Users/USER/Downloads/THU-deepseek-glm-api-mcp-server
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### Option B: `pip`

```bash
cd /mnt/c/Users/USER/Downloads/THU-deepseek-glm-api-mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Set your API key

The token from the Tsinghua site reportedly expires about 5 hours after login.

```bash
export TSINGHUA_DEEPSEEK_API_KEY='your_token_here'
export TSINGHUA_DEEPSEEK_BASE_URL='https://madmodel.cs.tsinghua.edu.cn/v1'
export OPENROUTER_API_KEY='your_openrouter_key_here'
```

Or copy `.env.example` into your own shell loader.

## 3) Run the server

### STDIO transport

Use this for MCP hosts that launch the server as a subprocess.

```bash
python3 server.py
```

### Streamable HTTP transport

```bash
python3 server.py --transport http --host 127.0.0.1 --port 8000
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## 3b) Run the interactive terminal agent

```bash
python3 agent.py
```

You can also skip the prompts:

```bash
python3 agent.py --model DeepSeek-R1-Distill-32B --api-key "$TSINGHUA_DEEPSEEK_API_KEY"
python3 agent.py --model 'qwen/qwen3-coder:free' --api-key "$OPENROUTER_API_KEY"
```

Agent commands:

- `/help`
- `/model`
- `/key`
- `/pwd`
- `/exit`

## 4) Quick test

### HTTP test with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
```

Then connect to:

```text
http://127.0.0.1:8000/mcp
```

### Direct API sanity check (outside MCP)

```bash
curl --location --request POST \
  'https://madmodel.cs.tsinghua.edu.cn/v1/chat/completions' \
  --header 'Content-Type: application/json' \
  --header "authorization: Bearer $TSINGHUA_DEEPSEEK_API_KEY" \
  --data-raw '{
    "model": "DeepSeek-R1-Distill-32B",
    "messages": [{"role": "user", "content": "你好"}],
    "temperature": 0.6,
    "repetition_penalty": 1.2,
    "stream": false
  }'
```

### Interactive agent smoke test

```bash
python3 agent.py --model DeepSeek-R1-Distill-32B --api-key "$TSINGHUA_DEEPSEEK_API_KEY"
```

Try:

```text
list the files in this directory
```

## 5) Example MCP client configs

### Claude Code (HTTP)

```bash
claude mcp add --transport http tsinghua-deepseek http://127.0.0.1:8000/mcp
```

### Generic stdio-style config JSON

```json
{
  "mcpServers": {
    "tsinghua-deepseek": {
      "command": "python3",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "TSINGHUA_DEEPSEEK_API_KEY": "your_token_here",
        "TSINGHUA_DEEPSEEK_BASE_URL": "https://madmodel.cs.tsinghua.edu.cn/v1"
      }
    }
  }
}
```

## 5b) Build a Linux executable

Install PyInstaller and build:

```bash
python3 -m pip install pyinstaller
./build_agent.sh
```

Binary output:

```text
dist/thu-agent
```

## 6) Tool behavior

### `simple_chat`

Inputs:
- `prompt`: user message
- `system`: optional system prompt
- `model`: `DeepSeek-R1-Distill-32B` or `DeepSeek-R1-671B`
- `temperature`
- `repetition_penalty`
- `stream`: if true, server aggregates the streamed chunks and returns final text
- `timeout`
- `max_tokens`

### `chat_completion`

Inputs:
- `messages`: full OpenAI-style message list
- the same generation params as above
- `extra_body`: optional extra JSON fields to pass through

## 7) Notes

- Do **not** print to stdout in stdio mode. MCP JSON-RPC uses stdout.
- Logs are written to stderr.
- If requests suddenly fail after a while, refresh your Tsinghua token and restart the client/server session.
