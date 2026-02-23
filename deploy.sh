#!/bin/bash
# Deploy Pulse API to Fly.io
# Run from the pulse-api/ directory
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PULSE_SRC="${SCRIPT_DIR}/../../../pulse/src"

echo "=== Copying Pulse source modules ==="
rm -rf pulse/
mkdir -p pulse/src
cp -r "$PULSE_SRC"/*.py pulse/src/
touch pulse/__init__.py
touch pulse/src/__init__.py
echo "✅ Copied $(ls pulse/src/*.py | wc -l | tr -d ' ') modules"

echo ""
echo "=== Deploying to Fly.io ==="
fly deploy

echo ""
echo "✅ Done! API at: https://pulse-api.fly.dev"
