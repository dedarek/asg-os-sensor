#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ASG_PYTHON="${ASG_SETUP_PYTHON:-python3}"
if ! "$ASG_PYTHON" -c 'import sys;sys.exit(sys.version_info < (3,10))' >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.12
    ASG_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
  elif command -v apt-get >/dev/null 2>&1; then
    ASG_SUDO=()
    if [ "$(id -u)" != 0 ]; then ASG_SUDO=(sudo); fi
    "${ASG_SUDO[@]}" apt-get update
    "${ASG_SUDO[@]}" apt-get install -y python3 python3-venv python3-pip
    ASG_PYTHON=python3
  else
    echo 'Python 3.10+ is required. Install Python (with venv/pip) and rerun setup.sh.' >&2
    exit 1
  fi
fi
exec "$ASG_PYTHON" deploy.py setup "$@"
