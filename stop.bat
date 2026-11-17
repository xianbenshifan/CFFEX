@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

title CFFEX Stop Service

echo ============================================================
echo   CFFEX Quiz Service - Stop
echo ============================================================
echo.

set FOUND=0
for %%P in (8889 8931 8080) do (
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
        echo   Stopping PID %%A on port %%P ...
        taskkill /F /PID %%A >nul 2>nul
        set FOUND=1
    )
)

if !FOUND!==0 (
    echo   No running service found.
) else (
    timeout /t 2 /nobreak >nul
    echo.
    echo   Stopped.
)

echo.
echo Press any key to close ...
pause >nul
exit /b 0
