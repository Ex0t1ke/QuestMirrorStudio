@echo off
chcp 65001 >nul
title Meta Quest 3S Mirror Studio
cd /d "%~dp0"

echo Запуск Meta Quest 3S Mirror Studio...

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    start "" py app.py
    exit /b
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    start "" python app.py
    exit /b
)

echo [ОШИБКА] Python не найден в системе!
pause
