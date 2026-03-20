@echo off
setlocal enabledelayedexpansion
title Kai — Local AI Agent
color 0F

echo.
echo  ╔══════════════════════════════════════╗
echo  ║       Kai — Local AI Agent           ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── Step 1: Check Python ─────────────────────────────────────────────────────
echo [1/5] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [!] Python is not installed or not in PATH.
    echo      Download it from: https://www.python.org/downloads/
    echo      IMPORTANT: Check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo      Python %PYVER% found.

:: ── Step 2: Install Python dependencies ──────────────────────────────────────
echo.
echo [2/5] Checking Python packages...
python -c "import pydantic, fastapi, uvicorn, psutil, sqlite_vec" >nul 2>&1
if %errorlevel% neq 0 (
    echo      Installing dependencies...
    python -m pip install -r "%~dp0requirements.txt" --quiet
    if %errorlevel% neq 0 (
        echo.
        echo  [!] Failed to install packages. Try running manually:
        echo      python -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo      Packages installed.
) else (
    echo      All packages present.
)

:: ── Step 3: Check Ollama ─────────────────────────────────────────────────────
echo.
echo [3/5] Checking Ollama...
ollama --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [!] Ollama is not installed or not in PATH.
    echo      Download it from: https://ollama.com/download
    echo      After installing, restart this script.
    echo.
    pause
    exit /b 1
)
for /f "tokens=4" %%v in ('ollama --version 2^>^&1') do set OLLVER=%%v
echo      Ollama %OLLVER% found.

:: Check if Ollama is running (try to reach the API)
curl -s -o nul -w "" http://127.0.0.1:11434/api/tags >nul 2>&1
if %errorlevel% neq 0 (
    echo      Ollama is not running — starting it...
    start /min "" ollama serve
    :: Wait for it to come up
    timeout /t 3 /nobreak >nul
    curl -s -o nul http://127.0.0.1:11434/api/tags >nul 2>&1
    if %errorlevel% neq 0 (
        echo      Waiting for Ollama to start...
        timeout /t 5 /nobreak >nul
    )
)
echo      Ollama is running.

:: ── Step 4: Pull models ──────────────────────────────────────────────────────
echo.
echo [4/5] Checking AI models...

:: Chat model
call :check_model "qwen3.5:9b" "Chat"

:: Reasoning model
call :check_model "qwen3:8b" "Reasoning"

:: Embedding model
call :check_model "qwen3-embedding:4b" "Embedding"

:: ── Step 5: Set KV cache quantization for 8GB cards ─────────────────────────
echo.
echo [5/5] Configuring for 8 GB VRAM...
set OLLAMA_KV_CACHE_TYPE=q8_0
echo      KV cache quantization: q8_0

:: ── Launch ───────────────────────────────────────────────────────────────────
echo.
echo  ════════════════════════════════════════
echo   Starting Kai...
echo  ════════════════════════════════════════
echo.
cd /d "%~dp0"
python web.py
pause
exit /b 0

:: ── Helper: check and pull a model if missing ───────────────────────────────
:check_model
set MODEL=%~1
set LABEL=%~2
ollama show %MODEL% >nul 2>&1
if %errorlevel% neq 0 (
    echo      Pulling %LABEL% model: %MODEL% (this may take a few minutes^)...
    ollama pull %MODEL%
    if %errorlevel% neq 0 (
        echo  [!] Failed to pull %MODEL%.
        echo      Check your internet connection and try: ollama pull %MODEL%
        pause
        exit /b 1
    )
    echo      %MODEL% ready.
) else (
    echo      %MODEL% ready.
)
exit /b 0
