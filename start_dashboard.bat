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

echo [2/3] 检查依赖包...
python -c "import psutil, yaml" >nul 2>nul
if %errorlevel% neq 0 (
    echo [提示] 正在安装所需依赖 (requirements.txt)...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [错误] 依赖安装失败，请手动运行 pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo [3/3] 启动实时监控与治理看板...
echo 访问地址: http://127.0.0.1:8080
echo 正在启动后台扫描线程 (30s 周期) 与 Web 交互控制台...
echo 按 Ctrl+C 退出服务。
echo.

python monitor_dashboard.py

pause
