@echo off
chcp 65001 >nul
echo ========================================
echo 启动Charles Mirror文件监听服务
echo ========================================
echo.
python watch_charles_mirror.py
pause

