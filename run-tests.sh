#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="book-stitch"
COMPOSE_TEST_FILE="docker-compose.test.yml"

# Check if the book-stitch container is running
if docker container inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
    echo "==> Running tests in existing '$CONTAINER_NAME' container..."

    # Install pytest if not already present
    if ! docker exec "$CONTAINER_NAME" python -c "import pytest" 2>/dev/null; then
        echo "    Installing pytest..."
        docker exec "$CONTAINER_NAME" pip install -q pytest
    fi

    docker exec -w /app "$CONTAINER_NAME" python -m pytest tests/ "$@"
else
    echo "==> Container '$CONTAINER_NAME' is not running. Using docker compose..."
    docker compose -f "$COMPOSE_TEST_FILE" run --rm test "$@"
fi
