# CLAUDE.md

## Commit Rules

Do not include `Co-Authored-By` lines or any other Claude/AI attribution in commit messages.

## Testing

Always run tests via `./run-tests.sh` — never bare `pytest`. The test suite requires Docker for `epubcfi` and `ffmpeg` dependencies that aren't available locally.

```bash
./run-tests.sh                              # full suite
./run-tests.sh tests/test_sync_manager.py   # single file
./run-tests.sh -k "test_name" -v            # filtered + verbose
```

## Security Notes

`KOSYNC_KEY` is intentionally revealed to the user via the SHOW button on the settings page (fetch-on-demand, never embedded in HTML). However, it must never appear in log output — always sanitize before logging.
