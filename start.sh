#!/bin/bash

# Graceful shutdown handler
cleanup() {
    echo "Shutting down services..."
    if [ -n "$WEB_PID" ]; then
        kill $WEB_PID 2>/dev/null
    fi
    exit 0
}

# Set up signal handlers for graceful shutdown
trap cleanup SIGTERM SIGINT

PAGEKEEPER_ENV_LABEL="${PAGEKEEPER_ENV:-production}"
HOST_WEB_URL="${HOST_WEB_URL:-http://localhost:4477}"
HOST_KOSYNC_PORT="${HOST_KOSYNC_PORT:-${KOSYNC_PORT:-4477}}"

echo "Starting PageKeeper (${PAGEKEEPER_ENV_LABEL^^} Integrated Mode)..."
echo ""

# Database migrations run inside the app (DatabaseService) — the single
# migration authority that safely handles pre-Alembic/legacy databases.
# Do not run `alembic upgrade` here: on a pre-Alembic DB it fails with
# "table books already exists" and leaves an empty alembic_version table
# that poisons the app's legacy recovery path.

# Main Supervisor Loop
while true; do
    echo "  Starting unified service (web + sync daemon)..."
    # Start in background so we can trap signals
    python /app/src/web_server.py &
    WEB_PID=$!

    echo ""
    echo "Service started successfully!"
    echo "   • Environment: ${PAGEKEEPER_ENV_LABEL^^}"
    echo "   • Unified Service PID: $WEB_PID"
    echo "   • Container Web UI: http://localhost:4477"
    echo "   • Host Web UI: $HOST_WEB_URL"
    echo "   • KoSync port: $HOST_KOSYNC_PORT"
    echo "   • Sync daemon running in background thread"
    echo ""

    # Wait for the process to exit
    # This will block until the python process ends (crashes or is killed)
    # If os.execv() is used, the PID stays the same and wait continues working.
    wait $WEB_PID
    EXIT_CODE=$?

    # If we get here, the app exited/crashed
    echo "Running cleanup..."
    
    # If exit code is 0 (clean exit), maybe we should still restart? 
    # Usually servers don't exit with 0 unless stopped. 
    # But if we were killed by signal trapped above, the script exits in 'cleanup'.
    
    echo "Application exited with code $EXIT_CODE. Restarting in 3 seconds..."
    sleep 3
done
