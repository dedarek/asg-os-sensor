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

echo "[1/2] 检查核心依赖..."
$PY_BIN -c "import psutil, yaml" 2>/dev/null || {
    echo "[提示] 安装依赖包..."
    $PY_BIN -m pip install -r requirements.txt
}

echo "[2/2] 启动服务: http://127.0.0.1:8080"
echo "按 Ctrl+C 停止服务。"
echo ""

$PY_BIN monitor_dashboard.py
