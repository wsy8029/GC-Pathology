#!/usr/bin/env bash
set -euo pipefail

# Create and populate a virtual environment for running virchow-embedding.py.
# Usage: bash setup_venv.sh [VENV_DIR]

VENV_DIR="${1:-.venv}"
PYTHON_BIN="${PYTHON:-python3}"

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

echo "Virtualenv ready at $VENV_DIR. Activate with: source $VENV_DIR/bin/activate"
