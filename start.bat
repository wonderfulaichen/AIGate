@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM first run: build release binary
if not exist "target\release\AIGate.exe" (
    echo [setup] first run, building release binary...
    echo [setup] this takes a few minutes, please wait...
    cargo build --release
    if errorlevel 1 (
        echo [error] build failed
        pause
        exit /b 1
    )
    echo [setup] build done.
    echo.
)

REM interactive config if .env is missing
if not exist ".env" (
    echo.
    echo [config] .env not found.
    set /p go_key="Enter your OpenCode Go key (press Enter for free models only): "
    echo # AIGate environment variables > .env
    echo OPENCODE_ZEN_KEY=public >> .env
    echo OPENCODE_GO_KEY=!go_key! >> .env
    echo # DEEPSEEK_API_KEY= >> .env
    echo [config] saved to .env
    echo.
)

echo starting AIGate...
echo   endpoint: http://127.0.0.1:8787
echo   press Ctrl+C to stop
echo.
target\release\AIGate.exe
pause
