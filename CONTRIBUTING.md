# Contributing to PageKeeper

Thanks for your interest in contributing to PageKeeper!

## Ways to Contribute

- **Report bugs**: open an issue with steps to reproduce
- **Suggest features**: open an issue so we can chat about it
- **Improve documentation**: typos, clarity, missing info
- **Submit code**: bug fixes, new features, refactors

For larger changes, please open an issue first so we can discuss the approach before you invest time in a PR.

## Development Setup

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) or [OrbStack](https://orbstack.dev/) (required for full app runs and Docker test parity)
- [uv](https://docs.astral.sh/uv/) (recommended for a local Python environment)
- [Git](https://git-scm.com/)

### Getting Started

```bash
# Fork the repo on GitHub first, then:
git clone https://github.com/<your-username>/pagekeeper.git
cd pagekeeper
```

The repo includes a `docker-compose.dev.yml` that builds from source and mounts your local code into the container, so changes take effect on restart without a full rebuild:

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

The dev dashboard is available at `http://localhost:4478` (port 4478, not 4477 — this avoids conflicts if you're also running a production instance). The dev compose file also sets `PAGEKEEPER_ENV=dev`, which turns on the in-app `DEV` badge, `[DEV]` browser-tab prefix, and dev-specific startup messaging. This flag is only for visual/runtime identification and should stay out of production compose files. All settings — integrations, API keys, sync configuration — are managed through the web dashboard.

If another PageKeeper dev worktree is already using ports 4478/5759, choose alternate host ports:

```bash
PAGEKEEPER_DEV_WEB_PORT=4480 PAGEKEEPER_DEV_KOSYNC_PORT=5761 docker compose -f docker-compose.dev.yml up --build -d
```

### Optional Local Python Environment

Docker is still the most production-like test path, but a local venv is useful for Ruff and fast targeted tests:

```bash
uv venv --python 3.13 .venv
VIRTUAL_ENV= uv pip install --python .venv/bin/python -r requirements.txt pytest ruff
```

The explicit `--python .venv/bin/python` keeps installs inside the project venv even if your shell already has another virtualenv active.

## Running Tests and Checks

### Ruff

```bash
./.venv/bin/ruff check --no-cache src tests alembic scripts
```

### Targeted Tests on macOS

Use a temp data directory so local pytest does not touch real PageKeeper data:

```bash
DATA_DIR="$PWD/.tmp/test-data" ./.venv/bin/python -m pytest tests/test_app_runtime.py
```

### Docker Tests

Tests run inside an isolated Docker test container with no `/data` volume mounted, so they can never touch a real PageKeeper database. From the project root:

```bash
# Run the full test suite
./run-tests.sh

# Run a specific test file
./run-tests.sh tests/test_sync_manager.py

# Run tests matching a keyword
./run-tests.sh -k "test_progress"
```

The test container handles all dependencies (epubcfi, ffmpeg, etc.), so there's nothing extra to install locally.

To run tests inside an already-running `pagekeeper` container instead (not recommended — that container's `/data` volume may hold a real database), set `PAGEKEEPER_TEST_IN_CONTAINER=1`:

```bash
PAGEKEEPER_TEST_IN_CONTAINER=1 ./run-tests.sh
```

For a direct Docker compose invocation:

```bash
docker compose -f docker-compose.test.yml run --rm test tests/test_app_runtime.py
```

### Continuous Integration

CI runs two separate jobs on pushes and pull requests:

- **Ruff** (`.github/workflows/lint.yml`) — `ruff check src/ tests/ alembic/ scripts/`
- **pytest** (`.github/workflows/test.yml`) — runs the full suite in the Docker test container with the same command you can run locally:

  ```bash
  docker compose -f docker-compose.test.yml run --rm test tests/
  ```

Both jobs must be green before a PR is merged.

### Dev App Smoke Test

```bash
docker compose -f docker-compose.dev.yml up --build -d
docker compose -f docker-compose.dev.yml ps
curl -fsS http://localhost:4478/healthcheck
curl -fsS -I http://localhost:4478/
```

If you used alternate ports, replace `4478` with your `PAGEKEEPER_DEV_WEB_PORT`.

The dev compose file stores app state under the ignored local `data/` directory. Do not copy production databases or secrets into a branch unless the task explicitly needs them.

## Branching & Pull Requests

1. **Fork** the repo and clone your fork
2. **Branch from `dev`** — `main` is for releases only
3. **Keep PRs focused** — one feature or fix per PR
4. **Open your PR against `dev`**, not `main`

```bash
git checkout dev
git pull origin dev
git checkout -b my-feature
# ... make changes ...
git push origin my-feature
# Open PR → base: dev
```

## Code Style

- **Python** — formatted and linted with [ruff](https://docs.astral.sh/ruff/). Run `ruff check` and `ruff format` before submitting.
- **Frontend** — vanilla JS, HTML, and CSS

## AI Usage

I use AI to build PageKeeper and I'm fine with others also using AI. With that said, **please make sure you understand what your code does before submitting a PR.** AI is great as a tool but lousy as a decision maker.

When opening PRs with AI assistance, follow [Agent PR Guidelines](.github/AGENT_PR_GUIDELINES.md): keep changes small, fill out the PR template, and document validation honestly.

## Reporting Bugs

When opening a bug report, please include:

- What you expected vs. what happened
- Steps to reproduce
- Relevant logs (check the Logs page in the dashboard)
- Your deployment method and any relevant integration versions

## License

PageKeeper is [MIT licensed](LICENSE). By contributing, you agree that your contributions will be licensed under the same terms.
