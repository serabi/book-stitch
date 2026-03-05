# Book Sync

<div align="center">

<img src="static/icon.png" alt="Book Sync" width="128">

**Sync your audiobooks with your ebooks across your various self-hosted services.**

[![License](https://img.shields.io/github/license/serabi/book-sync?cacheSeconds=3600)](LICENSE)
[![Release](https://img.shields.io/github/v/release/serabi/book-sync)](https://github.com/serabi/book-sync/releases)
[![Snyk Security](https://snyk.io/test/github/serabi/book-sync/badge.svg)](https://snyk.io/test/github/serabi/book-sync)
[![CodeRabbit Reviews](https://img.shields.io/coderabbit/prs/github/serabi/book-sync?labelColor=171717&color=FF570A&label=CodeRabbit+Reviews)](https://coderabbit.ai)

</div>

---

## What is this?

If you listen to audiobooks on a regular basis on [Audiobookshelf](https://www.audiobookshelf.org/) or [Storyteller](https://storyteller-platform.gitlab.io/storyteller/) during the day and then pick up the same book via KoReader or Kobo before bed, you know how frustrating it can be to find your place in the book. 

The goal of Book Sync is to keep your books in sync across multiple platforms so that you can resume reading where you left off, no matter which app you use. It's a self-hosted, Docker based sync engine that links your audiobook position to the matching spot in the ebook (and vice versa), then pushes that position to every app you use. It works by transcribing a segment of the audiobook audio and fuzzy-matching it against the EPUB text. Once that alignment map is built, converting between a timestamp and a page position simply takes a sync. 

Major kudos and credit goes to [abs-kosync-bridge](https://github.com/cporcellijr/abs-kosync-bridge) for being the inspiration and original jumping-off point for this project. A big thing I love about the open source community is the ability for us to contribute to projects, fork projects, and give back to the community through those efforts. In the spirit of open source, I'm sharing Book Sync in case anyone else finds it useful, and I'm open to suggestions and contributions.  

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
  book-sync:
    build: .
    container_name: book_sync
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

Book Sync runs three sync layers simultaneously, from fastest to slowest:

1. **Instant sync** — Listens to Audiobookshelf's Socket.IO stream and KOReader's KoSync updates in real time. When you pause an audiobook or push an update from KoReader via KoSync, Book Sync picks up the change within seconds.

2. **Per-client polling** — Lightweight checks against individual services (Storyteller, Booklore) at their own intervals. Only triggers a sync when the position has actually changed.

3. **Scheduled full sync** — A background sweep every few minutes that catches anything the other layers missed.

When a position change is detected, Book Sync converts it to every other format (timestamp to percentage, percentage to EPUB position, etc.) and pushes updates to all connected clients. A write-tracker prevents feedback loops — if Book Sync just pushed a position to a client, it ignores the echo that comes back.

---

## The alignment process

The first time you link an audiobook to its EPUB, Book Sync needs to build an alignment map — a lookup table that converts between audio timestamps and ebook character positions. It tries three sources in priority order:

1. **Storyteller native** — If the book is linked to Storyteller and you've mounted the Storyteller data directory, Book Sync reads Storyteller's word-level timing data (`wordTimeline`) directly. No transcription needed — fastest option.
2. **SMIL** — If the EPUB contains embedded SMIL timing data (common in publisher-produced audiobooks), it's extracted and used for alignment. Also fast.
3. **Whisper** — Falls back to transcribing a segment of the audiobook audio using [Whisper](https://github.com/openai/whisper) (local), [Deepgram](https://deepgram.com/) (cloud), or [Whisper.cpp](https://github.com/ggerganov/whisper.cpp) (external server), then fuzzy-matching the transcript against the EPUB text.

After the map is built it's cached in the database. All subsequent position conversions are instant.

### Storyteller native alignment

If you run Storyteller, you can skip Whisper entirely for books it has already processed. Mount Storyteller's processing directory as a read-only volume and set the **Assets Directory** in Settings:

```yaml
volumes:
  - /path/to/storyteller/processing:/storyteller-data:ro
```

Then in Settings → Audiobooks → Storyteller, set **Assets Directory** to `/storyteller-data`.

---

## Split-port mode

Book Sync can expose the KoSync API on a separate port from the admin dashboard. This keeps the sync endpoint available to your e-reader over the internet while the dashboard stays on your local network.

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
3. In Book Sync settings, enter the public URL
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

Book Sync needs access to your EPUB files for alignment. Three options, in order of simplicity:

- **Mount a volume** — Point `/books` at your EPUB directory. Simplest approach.
- **Booklore** — Book Sync fetches EPUBs through the Booklore API. No volume mount needed.
- **Calibre-Web Automated (CWA)** — Same idea, fetches EPUBs through CWA's API.

---

## Pairing suggestions

When Book Sync detects an audiobook you're actively listening to in Audiobookshelf, it can automatically search your ebook services (Booklore, Calibre-Web Automated, Storyteller) for a matching title and suggest a pairing. This works in two directions:

- **Forward:** Audiobooks with progress in ABS trigger searches across your ebook sources.
- **Reverse:** Books with progress in Storyteller or Booklore trigger searches for matching audiobooks in ABS.

Suggestions appear on the **Suggestions** page (`/suggestions`), where you can link, dismiss, or permanently ignore them. Real-time discovery also happens via Socket.IO — when you start playing an unmapped audiobook, a suggestion is queued automatically.

Enable suggestions in **Settings → General → Pairing Suggestions**.

---

## Diagnostic test buttons

Each service section on the Settings page includes a **Test** button that verifies connectivity and authentication in one click. Supported services: Audiobookshelf, Storyteller, Booklore, Calibre-Web Automated, Hardcover, and Telegram.

---

## Building

### Docker (recommended)
_Docker image coming soon_

Clone the repository and build the image:

```bash
git clone https://github.com/serabi/book-sync.git
cd book-sync
docker build -t book-sync .
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
docker build --build-arg INSTALL_GPU=true -t book-sync .
```

You'll also need to uncomment the `deploy.resources` section in your `docker-compose.yml` to expose the GPU to the container. See the example compose file for details.

#### Version tagging

The build accepts an `APP_VERSION` arg that controls the version displayed in the dashboard. Defaults to `dev` if not set.

```bash
docker build --build-arg APP_VERSION=1.0.0 -t book-sync .
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

If the `book-sync` container is running, tests execute there via `docker exec`
(fastest). Otherwise the script falls back to `docker compose -f docker-compose.test.yml run --rm test`.

---

## License

[MIT](LICENSE)
