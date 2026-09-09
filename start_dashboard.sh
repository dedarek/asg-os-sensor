#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "========================================================"
echo " ASG OS Sensor & Autonomous Agent Governance Console"
echo "========================================================"
echo ""

if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo "[错误] 未检测到 Python，请先安装 Python 3.10+。"
    exit 1
fi

PY_BIN="python3"
if ! command -v python3 &> /dev/null; then
    PY_BIN="python"
fi

echo "[1/3] 检查核心依赖..."
$PY_BIN -c "import psutil, yaml, requests, dotenv" 2>/dev/null || {
    echo "[提示] 安装依赖包..."
    $PY_BIN -m pip install -r requirements.txt
}
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
echo "[2/3] LLM 自检 (缺 key 只告警不退出)..."
$PY_BIN verify_llm.py || echo "[提示] LLM 未就绪: 看板仍可启动, 自动逆向会被跳过。按 .env.example 配好 key 后重跑 verify_llm.py。"
HOST_SHOW=${ASG_HOST:-127.0.0.1}
PORT_SHOW=${ASG_PORT:-8080}
echo "[3/3] 启动服务: http://${HOST_SHOW}:${PORT_SHOW} (ASG_HOST/ASG_PORT 可改, ASG_SCAN_INTERVAL 可调扫描秒数)"
echo "按 Ctrl+C 停止服务。"
echo ""
$PY_BIN monitor_dashboard.py
