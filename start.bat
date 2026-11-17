@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

title CFFEX Quiz Service

echo ============================================================
echo   CFFEX Quiz Service - One Click Start
echo ============================================================
echo   Dir: %CD%
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [INFO] .env missing, creating from .env.example ...
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [INFO] Created .env. Please fill DEEPSEEK_API_KEY then run again.
        notepad ".env"
        pause
        exit /b 1
    ) else (
        echo [ERROR] Neither .env nor .env.example exists.
        pause
        exit /b 1
    )
)

if not exist "log" mkdir "log"

echo [1/4] Checking ports ...
for %%P in (8889 8931) do (
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
        echo       Port %%P held by PID %%A, killing ...
        taskkill /F /PID %%A >nul 2>nul
    )
)
timeout /t 2 /nobreak >nul

echo [2/4] Starting Charles MCP (HTTP, port 8931) ...
start "CFFEX-MCP" /min cmd /c "python charles_mcp_http.py"

echo [3/4] Starting quiz server (port 8889) ...
start "CFFEX-Server" /min cmd /c "python proxy_server.py"

echo [4/4] Waiting for readiness ...
set READY=0
for /l %%i in (1,1,30) do (
    if !READY!==0 (
        timeout /t 2 /nobreak >nul
        curl -s -o nul http://127.0.0.1:8889/health 2>nul
        if not errorlevel 1 set READY=1
    )
)

if !READY!==0 (
    echo.
    echo [FAILED] Service not ready in 60s. See log\server.log
    pause
    exit /b 1
)

for /f "usebackq tokens=*" %%A in (`python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()"`) do set LOCAL_IP=%%A
if "%LOCAL_IP%"=="" set LOCAL_IP=127.0.0.1

echo.
echo ============================================================
echo   READY
echo ============================================================
echo   Dashboard : http://%LOCAL_IP%:8889/dashboard
echo   Answers   : http://%LOCAL_IP%:8889/answers.md
echo   Status    : http://%LOCAL_IP%:8889/system/status
echo   MCP       : http://127.0.0.1:8931/mcp
echo   Logs      : log\
echo ============================================================
echo.

start "" "http://%LOCAL_IP%:8889/dashboard"

echo Press any key to close this window (services keep running) ...
pause >nul
exit /b 0
