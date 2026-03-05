# PageKeeper

<div align="center">

<img src="static/icon.png" alt="PageKeeper" width="128">

**Keep your place across every book, every app, every format.**

<img src="static/2026-03-05 - PageKeeper Preview.png" alt="PageKeeper dashboard preview" width="700">

[![License](https://img.shields.io/github/license/serabi/pagekeeper?cacheSeconds=3600)](LICENSE)
[![Release](https://img.shields.io/github/v/release/serabi/pagekeeper)](https://github.com/serabi/pagekeeper/releases)
[![Snyk Security](https://snyk.io/test/github/serabi/pagekeeper/badge.svg)](https://snyk.io/test/github/serabi/pagekeeper)
[![CodeRabbit Reviews](https://img.shields.io/coderabbit/prs/github/serabi/pagekeeper?labelColor=171717&color=FF570A&label=CodeRabbit+Reviews)](https://coderabbit.ai)

</div>

---

## What is PageKeeper?

PageKeeper is a self-hosted reading companion that tracks what you read and keeps your place across platforms. Whether you listen to an audiobook during your commute on [Audiobookshelf](https://www.audiobookshelf.org/) and pick up the same book on your e-reader before bed, or just want a single place to see your reading progress across services — PageKeeper handles it.

At its core, PageKeeper is a **reading tracker**: it knows which books you're reading, how far along you are, when you started and finished, and keeps a journal of your progress. On top of that, it can **sync your position** between audiobook and ebook platforms by building an alignment map between the audio and the text. Once that map is built, jumping between formats is seamless.

### Origin story

This project started as a fork of [abs-kosync-bridge](https://github.com/cporcellijr/abs-kosync-bridge), a clever tool that synced Audiobookshelf positions to KOReader via the KoSync protocol. Major kudos to [cporcellijr](https://github.com/cporcellijr) for the original idea and implementation.

Over time, the scope grew well beyond that bridge: multi-platform sync, reading tracking, auto-completion, suggestion discovery, alignment from multiple sources, and a full web dashboard. At this point it's essentially a new application, but the spirit of open source that made it possible is the same. If you find PageKeeper useful, contributions and suggestions are always welcome.

### Supported platforms

| Platform | What it does |
|---|---|
| [Audiobookshelf](https://www.audiobookshelf.org/) | Main audiobook server |
| [KOReader](https://koreader.rocks/) (via KoSync) | E-ink reader progress (Boox, Kobo, jailbroken Kindle, etc.) |
| [Storyteller](https://storyteller-platform.gitlab.io/storyteller/) | Audiobook companion app with synced EPUB |
| [Booklore](https://github.com/booklore) | Ebook library and shelf manager |
| [Hardcover](https://hardcover.app/) | Book tracking service (write-only) |

You can use as few or as many of the above services as you want. None are required to use the app.


---

## Quick start

```yaml
services:
  pagekeeper:
    build: .
    container_name: pagekeeper
    restart: unless-stopped
    environment:
      - TZ=America/New_York
    volumes:
      - ./data:/data           # Database, cache, logs
      # - /path/to/ebooks:/books:ro  # Optional — only needed for cross-format sync without Booklore/CWA
    ports:
      - "4477:4477"            # Web dashboard
```

Start the container, open `http://your-server:4477`, and configure everything from the settings page. No environment variables needed beyond `TZ` — all settings live in the web UI and persist in the database.

**Please note that this service is not designed to be exposed outside a local area network. It does not have authentication (except for KoSync). Please be careful deploying it and follow best security practices.**

> **Full installation guide** including GPU setup, split-port security, and advanced options coming soon.

---

## How it works

PageKeeper runs three sync layers simultaneously, from fastest to slowest:

1. **Instant sync** — Listens to Audiobookshelf's Socket.IO stream and KOReader's KoSync updates in real time. When you pause an audiobook or push an update from KoReader via KoSync, PageKeeper picks up the change within seconds.

2. **Per-client polling** — Lightweight checks against individual services (Storyteller, Booklore) at their own intervals. Only triggers a sync when the position has actually changed.

3. **Scheduled full sync** — A background sweep every few minutes that catches anything the other layers missed.

When a position change is detected, PageKeeper converts it to every other format (timestamp to percentage, percentage to EPUB position, etc.) and pushes updates to all connected clients. A write-tracker prevents feedback loops — if PageKeeper just pushed a position to a client, it ignores the echo that comes back.

---

## The alignment process

The first time you link an audiobook to its EPUB, PageKeeper needs to build an alignment map — a lookup table that converts between audio timestamps and ebook character positions. It tries three sources in priority order:

1. **Storyteller native** — If the book is linked to Storyteller and you've mounted the Storyteller data directory, PageKeeper reads Storyteller's word-level timing data (`wordTimeline`) directly. No transcription needed — fastest option.
2. **SMIL** — If the EPUB contains embedded SMIL timing data (common in publisher-produced audiobooks), it's extracted and used for alignment. Also fast.
3. **Whisper** — Falls back to transcribing a segment of the audiobook audio using [Whisper](https://github.com/openai/whisper) (local), [Deepgram](https://deepgram.com/) (cloud), or [Whisper.cpp](https://github.com/ggerganov/whisper.cpp) (external server), then fuzzy-matching the transcript against the EPUB text.

After the map is built it's cached in the database. All subsequent position conversions are instant.

### Storyteller native alignment

If you run Storyteller, you can skip Whisper entirely for books it has already processed. Mount Storyteller's processing directory as a read-only volume and set the **Assets Directory** in Settings:

```yaml
volumes:
  - /path/to/storyteller/processing:/storyteller-data:ro
```

Then in Settings > Audiobooks > Storyteller, set **Assets Directory** to `/storyteller-data`.

---

## Split-port mode

PageKeeper can expose the KoSync API on a separate port from the admin dashboard. This keeps the sync endpoint available to your e-reader over the internet while the dashboard stays on your local network.

**Important:** Port 4477 (the dashboard) must stay on your LAN. Only the `KOSYNC_PORT` can be exposed via a reverse proxy as it does have an authentication layer built in.

### Setup

```yaml
environment:
  - KOSYNC_PORT=5758
ports:
  - "4477:4477"   # Dashboard — LAN only, do NOT forward
  - "5758:5758"   # Sync API — has authentication
```

### TLS requirement

KOSync credentials travel in HTTP headers (`x-auth-key`). Before exposing the sync port to the internet, put a reverse proxy with TLS in front of it (nginx, Caddy, Traefik, etc.). Without TLS, credentials are sent in plaintext.

### Public URL configuration

After setting up your reverse proxy, go to **Settings > KOSync** and enter your public URL (e.g. `https://sync.example.com`) in the **Public URL** field. This value is saved to the database and displayed on the settings page for easy copying into KOReader.

The **LAN Address** field shows `http://<server-ip>:<KOSYNC_PORT>` automatically — use this for devices on the same local network.

### KOReader setup

1. Set `KOSYNC_PORT` in your Docker environment
2. Configure your reverse proxy to forward `https://your-domain` to port `KOSYNC_PORT`
3. In PageKeeper settings, enter the public URL
4. In KOReader: Settings > Cloud storage > Progress sync > Custom server > enter your public URL

### Exposed paths

Only the following paths need to be forwarded through your reverse proxy. All other paths (including `/api/*` admin endpoints) can be blocked at the proxy level for defense in depth, though the app already protects admin endpoints with IP-based access control — exposing the full port won't grant access to the dashboard or management API from outside your network.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthcheck` | Connectivity check (no auth) |
| GET | `/users/auth` | Authenticate KOReader credentials |
| POST | `/users/create` | KOReader registration |
| POST | `/users/login` | KOReader login |
| GET | `/syncs/progress/<document>` | Retrieve reading position |
| PUT | `/syncs/progress` | Update reading position |

All paths are also available under the `/koreader/` prefix (e.g. `/koreader/users/auth`).

### Security features

The sync endpoint includes rate limiting, input validation, and MD5-hashed authentication per the KOSync protocol spec. Admin/management endpoints require credentials when accessed from public IPs.

---

## Ebook sources

PageKeeper needs access to your EPUB files for alignment. Three options, in order of simplicity:

- **Mount a volume** — Point `/books` at your EPUB directory. Simplest approach.
- **Booklore** — PageKeeper fetches EPUBs through the Booklore API. No volume mount needed.
- **Calibre-Web Automated (CWA)** — Same idea, fetches EPUBs through CWA's API.

---

## Pairing suggestions

When PageKeeper detects an audiobook you're actively listening to in Audiobookshelf, it can automatically search your ebook services (Booklore, Calibre-Web Automated, Storyteller) for a matching title and suggest a pairing. This works in two directions:

- **Forward:** Audiobooks with progress in ABS trigger searches across your ebook sources.
- **Reverse:** Books with progress in Storyteller or Booklore trigger searches for matching audiobooks in ABS.

Suggestions appear on the **Suggestions** page (`/suggestions`), where you can link, dismiss, or permanently ignore them. Real-time discovery also happens via Socket.IO — when you start playing an unmapped audiobook, a suggestion is queued automatically.

Enable suggestions in **Settings > General > Pairing Suggestions**.

---

## Diagnostic test buttons

Each service section on the Settings page includes a **Test** button that verifies connectivity and authentication in one click. Supported services: Audiobookshelf, Storyteller, Booklore, Calibre-Web Automated, Hardcover, and Telegram.

---

## Building

### Docker (recommended)
_Docker image coming soon_

Clone the repository and build the image:

```bash
git clone https://github.com/serabi/pagekeeper.git
cd pagekeeper
docker build -t pagekeeper .
```

Copy the example compose file and edit it for your setup:

```bash
cp docker-compose.example.yml docker-compose.yml
# Edit docker-compose.yml with your volume paths, timezone, etc.
docker compose up -d
```

The dashboard will be available at `http://localhost:4477`. All service configuration is done from the Settings page in the web UI.

#### GPU support

To enable NVIDIA GPU acceleration for Whisper transcription, pass the `INSTALL_GPU` build arg. This adds ~800MB to the image for the CUDA libraries.

```bash
docker build --build-arg INSTALL_GPU=true -t pagekeeper .
```

You'll also need to uncomment the `deploy.resources` section in your `docker-compose.yml` to expose the GPU to the container. See the example compose file for details.

#### Version tagging

The build accepts an `APP_VERSION` arg that controls the version displayed in the dashboard. Defaults to `dev` if not set.

```bash
docker build --build-arg APP_VERSION=1.0.0 -t pagekeeper .
```

### Local development

Prerequisites: Python 3.11+, ffmpeg

```bash
pip install -r requirements.txt
mkdir -p data
alembic upgrade head
python -m src.web_server
```

The server starts at `http://localhost:4477`. The database is created at `data/database.db`.

### Running tests

Tests must run inside Docker because the suite depends on `epubcfi` (a C-extension
package installed in the image) and `ffmpeg`. Use the included wrapper script:

```bash
./run-tests.sh                                    # full suite
./run-tests.sh tests/test_abs_socket_listener.py -v  # single file, verbose
./run-tests.sh -k "test_sync_cycle"               # filter by name
```

If the `pagekeeper` container is running, tests execute there via `docker exec`
(fastest). Otherwise the script falls back to `docker compose -f docker-compose.test.yml run --rm test`.

---

## License

[MIT](LICENSE)
