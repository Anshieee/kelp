# Source this before running anything in the spike. Keeps every cache inside this folder.
SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SPIKE_DIR
export HF_HOME="$SPIKE_DIR/cache/hf"
export XDG_CACHE_HOME="$SPIKE_DIR/cache/xdg"
export UV_CACHE_DIR="$SPIKE_DIR/cache/uv"
export PIP_CACHE_DIR="$SPIKE_DIR/cache/pip"
export UV_PYTHON_INSTALL_DIR="$SPIKE_DIR/cache/uv-python"
export UV_PROJECT_ENVIRONMENT="$SPIKE_DIR/.venv"
export UV_LINK_MODE=copy
export OLLAMA_HOST="127.0.0.1:11555"
export TOKENIZERS_PARALLELISM=false
