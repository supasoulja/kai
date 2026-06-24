#!/usr/bin/env bash
# ── Kai — Local AI Agent (Linux launcher) ─────────────────────────────
set -euo pipefail

echo ""
echo "  +--------------------------------------+"
echo "  |       Kai - Local AI Agent           |"
echo "  +--------------------------------------+"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # repo root — this script lives in scripts/
cd "$ROOT_DIR"

# ── Resolve Python from venv or PATH ─────────────────────────────────
if [ -f ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 &>/dev/null; then
    PY="python3"
elif command -v python &>/dev/null; then
    PY="python"
else
    echo ""
    echo "  [!] Python is not installed or not in PATH."
    echo "      Install it with your package manager:"
    echo "        Ubuntu/Debian:  sudo apt install python3 python3-venv python3-pip"
    echo "        Fedora:         sudo dnf install python3 python3-pip"
    echo "        Arch:           sudo pacman -S python python-pip"
    echo ""
    exit 1
fi

# ── Step 1: Check Python ─────────────────────────────────────────────
echo "[1/5] Checking Python..."
PYVER=$("$PY" --version 2>&1)
echo "      $PYVER found."

# ── Step 2: Install Python dependencies ──────────────────────────────
echo ""
echo "[2/5] Checking Python packages..."
if "$PY" -c "import pydantic, fastapi, uvicorn, psutil, sqlite_vec" 2>/dev/null; then
    echo "      All packages present."
else
    echo "      Installing dependencies..."
    "$PY" -m pip install -r "$ROOT_DIR/requirements.txt" --quiet
    echo "      Packages installed."
fi

# ── Step 3: Check Ollama ─────────────────────────────────────────────
echo ""
echo "[3/5] Checking Ollama..."
if ! command -v ollama &>/dev/null; then
    echo ""
    echo "  [!] Ollama is not installed or not in PATH."
    echo "      Install it from: https://ollama.com/download"
    echo "      Or run:  curl -fsSL https://ollama.com/install.sh | sh"
    echo ""
    exit 1
fi
OLLVER=$(ollama --version 2>&1)
echo "      $OLLVER"

# Check if Ollama is running
if ! curl -s -o /dev/null http://127.0.0.1:11434/api/tags 2>/dev/null; then
    echo "      Ollama is not running — starting it..."
    ollama serve &>/dev/null &
    sleep 4
    if ! curl -s -o /dev/null http://127.0.0.1:11434/api/tags 2>/dev/null; then
        echo "      Still waiting for Ollama..."
        sleep 6
        if ! curl -s -o /dev/null http://127.0.0.1:11434/api/tags 2>/dev/null; then
            echo "      Could not reach Ollama. Start it manually then re-run."
            exit 1
        fi
    fi
fi
echo "      Ollama is running."

# ── Step 4: Pull models ──────────────────────────────────────────────
echo ""
echo "[4/5] Checking AI models..."

check_model() {
    local MODEL="$1"
    local LABEL="$2"
    if ollama show "$MODEL" &>/dev/null; then
        echo "      $MODEL ready."
    else
        echo "      Pulling $LABEL model: $MODEL..."
        ollama pull "$MODEL"
        echo "      $MODEL ready."
    fi
}

check_model "qwen3.5:9b"          "Chat"
check_model "qwen3:8b"            "Reasoning"
check_model "qwen3-embedding:4b"  "Embedding"

# ── Step 5: Set KV cache quantization ────────────────────────────────
echo ""
echo "[5/5] Configuring for 8 GB VRAM..."
export OLLAMA_KV_CACHE_TYPE=q8_0
echo "      KV cache quantization: q8_0"

# ── Launch ────────────────────────────────────────────────────────────
echo ""
echo "  ========================================"
echo "   Starting Kai..."
echo "  ========================================"
echo ""
"$PY" web.py
