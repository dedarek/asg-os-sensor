#!/usr/bin/env bash
# Compatibility entry point: use the same managed deployment as setup.sh.
set -euo pipefail
cd "$(dirname "$0")"
ASG_PYTHON="${ASG_SETUP_PYTHON:-python3}"
if [ -x .venv/bin/python ]; then ASG_PYTHON=.venv/bin/python; fi
exec "$ASG_PYTHON" deploy.py start "$@"
