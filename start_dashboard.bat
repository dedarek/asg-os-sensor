@echo off
chcp 65001 >nul
echo ========================================================
echo  ASG OS Sensor ^& Autonomous Agent Governance Console
echo ========================================================
echo.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo [1/3] 检查 Python 环境...
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未在系统 PATH 中检测到 Python，请先安装 Python 3.10+ 并配置环境变量。
    pause
    exit /b 1
)

echo [2/4] 检查依赖包...
python -c "import psutil, yaml, requests, dotenv" >nul 2>nul
if %errorlevel% neq 0 (
    echo [提示] 正在安装所需依赖 (requirements.txt)...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [错误] 依赖安装失败，请手动运行 pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo [3/4] LLM 自检 (缺 key 只告警不退出)...
python verify_llm.py
if %errorlevel% neq 0 (
    echo [提示] LLM 未就绪: 看板仍可启动, 自动逆向会被跳过。按 .env.example 配好 key 后重跑 verify_llm.py。
)
echo [4/4] 启动实时监控与治理看板...
echo 访问地址: http://127.0.0.1:8080 ^(ASG_HOST/ASG_PORT 可改, ASG_SCAN_INTERVAL 可调扫描秒数^)
echo 按 Ctrl+C 退出服务。
echo.
python monitor_dashboard.py

pause
