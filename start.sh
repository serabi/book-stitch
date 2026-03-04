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

echo "Starting Book Sync (Integrated Mode)..."
echo ""

echo "Running Database Migrations..."
alembic upgrade head
echo "Database Migrations Completed"
echo ""

# Main Supervisor Loop
while true; do
    echo "  Starting unified service (web + sync daemon)..."
    # Start in background so we can trap signals
    python /app/src/web_server.py &
    WEB_PID=$!

    echo ""
    echo "Service started successfully!"
    echo "   • Unified Service PID: $WEB_PID"
    echo "   • Web UI available at: http://localhost:4477"
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
