#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="pagekeeper"
COMPOSE_TEST_FILE="docker-compose.test.yml"

# Default: run tests in the isolated test container (no /data volume mounted).
# Opt in to running inside an existing container with PAGEKEEPER_TEST_IN_CONTAINER=1.
if [[ "${PAGEKEEPER_TEST_IN_CONTAINER:-0}" == "1" ]]; then
    if ! docker container inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
        echo "ERROR: PAGEKEEPER_TEST_IN_CONTAINER=1 but container '$CONTAINER_NAME' is not running." >&2
        exit 1
    fi

    echo "!!! WARNING: running tests inside the existing '$CONTAINER_NAME' container." >&2
    echo "!!! Tests will use that container's /data volume, which may hold a REAL database." >&2
    echo "!!! Unset PAGEKEEPER_TEST_IN_CONTAINER to use the isolated test container instead." >&2

    # Install pytest if not already present
    if ! docker exec "$CONTAINER_NAME" python -c "import pytest" 2>/dev/null; then
        echo "    Installing pytest..."
        docker exec "$CONTAINER_NAME" pip install -q pytest
    fi

    docker exec -w /app "$CONTAINER_NAME" python -m pytest "${@:-tests/}"
else
    echo "==> Running tests in isolated test container..."
    docker compose -f "$COMPOSE_TEST_FILE" run --rm test "${@:-tests/}"
fi
