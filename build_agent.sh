#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYGAME_HIDE_SUPPORT_PROMPT=1

python3 -m PyInstaller \
  --clean \
  --onefile \
  --strip \
  --optimize 2 \
  --name thu-agent \
  --exclude-module IPython \
  --exclude-module PIL \
  --exclude-module PyQt5 \
  --exclude-module PyQt6 \
  --exclude-module matplotlib \
  --exclude-module numpy \
  --exclude-module pygame \
  --exclude-module pytest \
  --exclude-module tkinter \
  --exclude-module traitlets \
  --exclude-module jedi \
  --exclude-module parso \
  --exclude-module gi \
  --exclude-module cryptography \
  --exclude-module bcrypt \
  --exclude-module cffi \
  --exclude-module pycparser \
  "$ROOT_DIR/agent.py"

echo
echo "Built executable:"
echo "  $ROOT_DIR/dist/thu-agent"
