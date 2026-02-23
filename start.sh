#!/bin/bash
# Pulse API startup script
# Persistent data stored at ~/.pulse/companions (survives restarts)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Persistent data directory — NOT /tmp
export PULSE_DATA_DIR="${PULSE_DATA_DIR:-/Users/iris/.pulse/companions}"

# Load environment variables if .env exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Ensure data dir exists
mkdir -p "$PULSE_DATA_DIR"

echo "Starting Pulse API..."
echo "  Data dir: $PULSE_DATA_DIR"
echo "  Port: 8000"

exec ./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 "$@"
