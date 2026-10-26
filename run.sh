#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ -f "$DIR/venv/bin/python" ]; then
    PYTHON_BIN="$DIR/venv/bin/python"
elif [ -f "$DIR/../Portfolio-Management/venv/bin/python" ]; then
    PYTHON_BIN="$DIR/../Portfolio-Management/venv/bin/python"
elif [ -f "$DIR/../.venv/bin/python" ]; then
    PYTHON_BIN="$DIR/../.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

export PORT="${PORT:-5152}"
echo "Starting Portfolio Dashboard server using: $PYTHON_BIN"
echo "Open dashboard at: http://127.0.0.1:$PORT"
"$PYTHON_BIN" app.py
