#!/usr/bin/env bash
# Claude Code PreToolUse hook: blocks bare pytest commands and redirects to ./run-tests.sh

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# Allow if empty or not a pytest command
if [ -z "$COMMAND" ]; then
    exit 0
fi

# Check if this is a pytest invocation without docker/run-tests
if echo "$COMMAND" | grep -qE '(^|\s)(pytest|python\s+-m\s+pytest)(\s|$)'; then
    if ! echo "$COMMAND" | grep -qE '(docker|run-tests)'; then
        echo '{"decision":"deny","reason":"Use ./run-tests.sh instead of bare pytest. The test suite requires Docker for epubcfi and ffmpeg dependencies. Example: ./run-tests.sh tests/test_foo.py -v"}'
        exit 0
    fi
fi

exit 0
