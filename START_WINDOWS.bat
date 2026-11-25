@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

title KINTARO FARM OS v5.0 - Windows Launcher
echo ===============================================================
echo   KINTARO FARM OS v5.0 - TikTok Farm, Campaign ^& Unban Studio
echo ===============================================================
echo.

REM 1. Find Python 3.12+ (or py launcher)
set "PY_CMD="
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
    if !errorlevel!==0 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    where python >nul 2>&1
    if %errorlevel%==0 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
        if !errorlevel!==0 set "PY_CMD=python"
    )
)
if not defined PY_CMD (
    echo [ERROR] Python 3.12+ не найден!
    echo Скачай Python 3.12+ с https://www.python.org/downloads/windows/
    echo Обязательно поставь галочку "Add python.exe to PATH" при установке!
    pause
    exit /b 1
)

REM 2. Create .venv on first launch
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Создание виртуального окружения .venv...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать .venv
        pause
        exit /b 1
    )
)

REM 3. Install / update dependencies if kintaro-auto isn't installed yet
if not exist ".venv\Scripts\kintaro-auto.exe" (
    echo [2/4] Установка зависимостей KINTARO FARM OS ^(первый запуск ~1-2 мин^)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 (
        echo [ERROR] Ошибка установки зависимостей!
        pause
        exit /b 1
    )
)

REM 4. Ensure Chromium (System Chrome / Edge or Playwright Chromium) is available for Outlook Hub & WebView
".venv\Scripts\python.exe" -c "from kintaro_auto.platform_utils import find_chromium_executable; raise SystemExit(0 if find_chromium_executable() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [3/4] Установка Playwright Chromium для Outlook Hub ^& WebView...
    ".venv\Scripts\python.exe" -m playwright install chromium
)

REM 5. Auto-create config.toml from example if missing
if not exist "config.toml" (
    if exist "config.example.toml" (
        copy /Y "config.example.toml" "config.toml" >nul
        echo [INFO] Создан config.toml из шаблона - впиши API ключ Kintaro в веб-панели ^(Настройки^)!
    )
)

echo.
echo [4/4] Запуск KINTARO FARM OS v5.0 на http://127.0.0.1:8080 ...
echo       Закрой это окно, чтобы остановить сервер.
echo.

REM Open browser automatically after 2 seconds
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8080"

".venv\Scripts\kintaro-auto.exe" web --host 127.0.0.1 --port 8080
pause
