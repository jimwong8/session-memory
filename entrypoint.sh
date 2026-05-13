#!/bin/bash
set -e

# Pre-flight dependency check for monitoring probes
missing=""
for cmd in sshpass ssh curl python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        missing="$missing $cmd"
    fi
done

if [ -n "$missing" ]; then
    echo "FATAL: Missing required commands:$missing" >&2
    exit 1
fi

echo "All required dependencies present."

# Execute the actual application
exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 4
