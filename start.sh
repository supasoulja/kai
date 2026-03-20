#!/usr/bin/env bash
# Kai — Local AI Agent startup script
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║       Kai — Local AI Agent           ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── Step 1: Check Python ─────────────────────────────────────────────────────
echo "[1/5] Checking Python..."
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "  [!] Python is not installed."
    echo "      Install it with your package manager:"
    echo "        Ubuntu/Debian: sudo apt install python3 python3-pip"
    echo "        Fedora:        sudo dnf install python3 python3-pip"
    echo "        macOS:         brew install python3"
    exit 1
fi
PY=$(command -v python3 || command -v python)
echo "      $($PY --version) found."

# ── Step 2: Install Python dependencies ──────────────────────────────────────
echo ""
echo "[2/5] Checking Python packages..."
if ! $PY -c "import pydantic, fastapi, uvicorn, psutil, sqlite_vec" &>/dev/null; then
    echo "      Installing dependencies..."
    $PY -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
    echo "      Packages installed."
else
    echo "      All packages present."
fi

# ── Step 3: Check Ollama ─────────────────────────────────────────────────────
echo ""
echo "[3/5] Checking Ollama..."
if ! command -v ollama &>/dev/null; then
    echo "  [!] Ollama is not installed."
    echo "      Install it from: https://ollama.com/download"
    echo "      Or run: curl -fsSL https://ollama.com/install.sh | sh"
    exit 1
fi
echo "      $(ollama --version) found."

# Check if Ollama is running
if ! curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    echo "      Ollama is not running — starting it..."
    ollama serve &>/dev/null &
    sleep 3
    if ! curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
        echo "      Waiting for Ollama to start..."
        sleep 5
    fi
fi
echo "      Ollama is running."

# ── Step 4: Pull models ──────────────────────────────────────────────────────
echo ""
echo "[4/5] Checking AI models..."

check_model() {
    local model="$1"
    local label="$2"
    if ! ollama show "$model" &>/dev/null; then
        echo "      Pulling $label model: $model (this may take a few minutes)..."
        ollama pull "$model"
        echo "      $model ready."
    else
        echo "      $model ready."
    fi
}

check_model "qwen3.5:9b"            "Chat"
check_model "qwen3:8b"              "Reasoning"
check_model "qwen3-embedding:4b"    "Embedding"

# ── Step 5: Set KV cache quantization for 8GB cards ─────────────────────────
echo ""
echo "[5/5] Configuring for 8 GB VRAM..."
export OLLAMA_KV_CACHE_TYPE=q8_0
echo "      KV cache quantization: q8_0"

# ── Launch ───────────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════"
echo "   Starting Kai..."
echo "  ════════════════════════════════════════"
echo ""
$PY web.py "$@"
