@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

title CFFEX mitmproxy

echo ============================================================
echo   CFFEX Quiz Service - mitmproxy
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

python -c "import mitmproxy" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] mitmproxy is not installed.
    echo         Run: python -m pip install mitmproxy
    pause
    exit /b 1
)

if not exist "log" mkdir "log"

for /f "usebackq delims=" %%M in (`python -c "import os,shutil,sys; p=shutil.which('mitmdump'); exe=os.path.join(os.path.dirname(sys.executable),'Scripts','mitmdump.exe'); print(p or (exe if os.path.exists(exe) else ''))"`) do set MITMDUMP=%%M
if "%MITMDUMP%"=="" (
    echo [ERROR] mitmdump.exe not found.
    pause
    exit /b 1
)

for /f "usebackq delims=" %%P in (`python -c "import config; print(config.get('MITM_LISTEN_PORT','8080'))"`) do set MITM_PORT=%%P
for /f "usebackq delims=" %%H in (`python -c "import config; print(config.get('MITM_ALLOW_HOSTS','quiz-activity\\.eastmoney\\.com'))"`) do set MITM_HOSTS=%%H
if "%MITM_PORT%"=="" set MITM_PORT=8080

echo [1/3] Freeing mitmproxy port %MITM_PORT% ...
for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%MITM_PORT%" ^| findstr "LISTENING"') do (
    echo       Port %MITM_PORT% held by PID %%A, killing ...
    taskkill /F /PID %%A >nul 2>nul
)

echo [2/3] Quiz server on 8889 ...
set SERVER_UP=0
netstat -ano | findstr ":8889" | findstr "LISTENING" >nul
if not errorlevel 1 set SERVER_UP=1
if "!SERVER_UP!"=="0" (
    echo       Starting proxy_server.py ...
    start "CFFEX-Server" /min cmd /c "python proxy_server.py"
) else (
    echo       Already listening, leave it running.
)

echo [3/3] Waiting for the quiz server, then starting mitmdump ...
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
    echo [FAILED] Quiz server not ready in 60s. See log\server.log
    pause
    exit /b 1
)

start "CFFEX-MITM" /min cmd /c ""%MITMDUMP%" --listen-host 0.0.0.0 --listen-port %MITM_PORT% --allow-hosts "%MITM_HOSTS%" -s "%CD%\mitm_capture.py""

set MITM_READY=0
for /l %%i in (1,1,20) do (
    if !MITM_READY!==0 (
        timeout /t 1 /nobreak >nul
        netstat -ano | findstr ":%MITM_PORT%" | findstr "LISTENING" >nul
        if not errorlevel 1 set MITM_READY=1
    )
)
if !MITM_READY!==0 (
    echo.
    echo [FAILED] mitmdump is not listening on %MITM_PORT%.
    pause
    exit /b 1
)

for /f "usebackq tokens=*" %%A in (`python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()"`) do set LOCAL_IP=%%A
if "%LOCAL_IP%"=="" set LOCAL_IP=127.0.0.1

echo.
echo ============================================================
echo   READY
echo ============================================================
echo   Phone proxy : %LOCAL_IP%:%MITM_PORT%
echo   Certificate : %USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer
echo   Dashboard   : http://%LOCAL_IP%:8889/dashboard
echo   Only host   : %MITM_HOSTS%
echo ============================================================
echo   Install that certificate on the phone and trust it.
echo   Point the phone Wi-Fi proxy at the address above.
echo   Charles is not started by this script.
echo ============================================================
echo.

start "" "http://%LOCAL_IP%:8889/dashboard"

echo Press any key to close this window (services keep running) ...
pause >nul
exit /b 0
