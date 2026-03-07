# PageKeeper — Full Installation Guide

This guide walks you through installing, configuring, and running PageKeeper from scratch.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [1. Clone and Build](#1-clone-and-build)
- [2. Docker Compose Setup](#2-docker-compose-setup)
- [3. First Launch](#3-first-launch)
- [4. Core Configuration (Settings Page)](#4-core-configuration-settings-page)
- [5. Integration Setup](#5-integration-setup)
  - [Audiobookshelf](#audiobookshelf)
  - [KOReader (KoSync)](#koreader-kosync)
  - [Storyteller](#storyteller)
  - [Booklore](#booklore)
  - [Calibre-Web Automated (CWA)](#calibre-web-automated-cwa)
  - [Hardcover](#hardcover)
  - [BookFusion](#bookfusion)
- [6. Cross-Format Sync (Alignment)](#6-cross-format-sync-alignment)
  - [Ebook Sources](#ebook-sources)
  - [Whisper Transcription](#whisper-transcription)
  - [GPU Acceleration](#gpu-acceleration)
  - [Deepgram (Cloud Transcription)](#deepgram-cloud-transcription)
  - [Whisper.cpp (External Server)](#whispercpp-external-server)
  - [Storyteller Native Alignment](#storyteller-native-alignment)
- [7. Split-Port Mode (Exposing KoSync)](#7-split-port-mode-exposing-kosync)
- [8. Telegram Notifications](#8-telegram-notifications)
- [9. Environment Variable Reference](#9-environment-variable-reference)
- [10. Volume Reference](#10-volume-reference)
- [11. Updating](#11-updating)
- [12. Troubleshooting](#12-troubleshooting)
- [13. Local Development (Non-Docker)](#13-local-development-non-docker)

---

## Prerequisites

- **Docker** and **Docker Compose** (v2+)
- A machine on your local network (Linux, macOS, or Windows with WSL2)
- (Optional) An NVIDIA GPU with [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for Whisper acceleration

> **Security note:** PageKeeper's dashboard has no built-in authentication. Do not expose port 4477 to the internet. See [Split-Port Mode](#7-split-port-mode-exposing-kosync) if you need to expose the KoSync API.

---

## 1. Clone and Build

```bash
git clone https://github.com/serabi/pagekeeper.git
cd pagekeeper
docker build -t pagekeeper .
```

To tag with a specific version:

```bash
docker build --build-arg APP_VERSION=0.1.2 -t pagekeeper .
```

---

## 2. Docker Compose Setup

Copy the example compose file and edit it:

```bash
cp docker-compose.example.yml docker-compose.yml
```

Here is a minimal working configuration:

```yaml
services:
  pagekeeper:
    build: .
    container_name: pagekeeper
    restart: unless-stopped
    environment:
      - TZ=America/New_York      # Your timezone
    volumes:
      - ./data:/data             # Database, cache, logs (required)
    ports:
      - "4477:4477"              # Web dashboard
```

And here is a full-featured configuration with all optional volumes and settings:

```yaml
services:
  pagekeeper:
    build:
      context: .
      args:
        INSTALL_GPU: "false"       # Set to "true" for NVIDIA GPU support
        APP_VERSION: "0.1.2"       # Version shown in dashboard
    container_name: pagekeeper
    restart: unless-stopped

    environment:
      - TZ=America/New_York
      # - LOG_LEVEL=INFO           # DEBUG, INFO, WARNING, ERROR
      # - KOSYNC_PORT=5758         # Enable split-port mode

    volumes:
      # === REQUIRED ===
      - ./data:/data                              # App data (database, cache, logs)

      # === OPTIONAL — Ebook Sources ===
      # Only needed for cross-format sync if you're NOT using Booklore or CWA
      # - /path/to/ebooks:/books:ro

      # === OPTIONAL — Storyteller ===
      # Mount Storyteller's processing directory for native alignment (skips Whisper)
      # - /path/to/storyteller/processing:/storyteller-data:ro

    ports:
      - "4477:4477"                # Dashboard — LAN only, do NOT forward
      # - "5758:5758"              # KoSync API — enable with KOSYNC_PORT

    # === OPTIONAL — GPU Support ===
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: 1
    #           capabilities: [gpu]
```

---

## 3. First Launch

```bash
docker compose up -d
```

PageKeeper will:
1. Run database migrations automatically via Alembic
2. Start the web server on port 4477
3. Start the background sync daemon

Open `http://<your-server-ip>:4477` in your browser. You should see the dashboard. All further configuration is done from the **Settings** page in the web UI.

### Verifying the container is running

```bash
docker compose logs -f          # Watch logs in real time
docker compose ps               # Check container status
curl http://localhost:4477/healthcheck   # Quick health check
```

---

## 4. Core Configuration (Settings Page)

Navigate to **Settings** (`http://your-server:4477/settings`) to configure everything. Settings are saved to the database and persist across restarts — you don't need to set environment variables for most things.

### General Settings

| Setting | Description | Default |
|---|---|---|
| **Sync Period** | Minutes between full sync sweeps | `5` |
| **Pairing Suggestions** | Auto-suggest audiobook/ebook pairings | Off |
| **Instant Sync** | Real-time sync via ABS Socket.IO | On |

### Sync Sensitivity

These control how much a position must change before PageKeeper pushes an update:

| Setting | Env Var | Default |
|---|---|---|
| ABS position delta | `SYNC_DELTA_ABS_SECONDS` | `60` seconds |
| KoSync position delta | `SYNC_DELTA_KOSYNC_PERCENT` | `0.5`% |
| Cross-client delta | `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5`% |
| KoSync word delta | `SYNC_DELTA_KOSYNC_WORDS` | `400` words |

---

## 5. Integration Setup

Each integration is configured from the Settings page. None are required — use as many or as few as you like.

### Audiobookshelf

Your main audiobook server. Configure in **Settings > Audiobookshelf**:

| Field | Description |
|---|---|
| **Server URL** | e.g. `http://audiobookshelf:13378` |
| **API Key** | Generate from ABS > Settings > Users > your user |
| **Library IDs** | Comma-separated ABS library IDs to monitor (leave blank for all) |
| **Collection Name** | ABS collection for synced books (default: `Synced with KOReader`) |

Click **Test** to verify connectivity.

### KOReader (KoSync)

PageKeeper includes a full KoSync-compatible server for syncing e-reader progress.

Configure in **Settings > KoSync**:

| Field | Description |
|---|---|
| **Username** | KoSync username (used in KOReader) |
| **Password** | KoSync password/key |
| **Hash Method** | `content` (default) — how KOReader identifies books |

Then in KOReader on your e-reader:
1. Go to **Settings > Cloud storage > Progress sync**
2. Choose **Custom sync server**
3. Enter `http://<pagekeeper-ip>:4477` (or the split-port URL — see [Section 7](#7-split-port-mode-exposing-kosync))
4. Enter the same username and password

### Storyteller

An audiobook companion app with EPUB3 read-along support.

Configure in **Settings > Storyteller**:

| Field | Description |
|---|---|
| **Server URL** | e.g. `http://storyteller:8001` |
| **Username** | Storyteller login username |
| **Password** | Storyteller login password |
| **Assets Directory** | Path inside the container to Storyteller's processing data (for native alignment) |

If using native alignment, add the volume mount:

```yaml
volumes:
  - /path/to/storyteller/processing:/storyteller-data:ro
```

Then set **Assets Directory** to `/storyteller-data` in the settings.

### Booklore

An ebook library manager. PageKeeper can fetch ebook metadata and files through Booklore's API, removing the need for a direct volume mount.

Configure in **Settings > Booklore**:

| Field | Description |
|---|---|
| **Server URL** | e.g. `http://booklore:5000` |
| **Username** | Booklore login |
| **Password** | Booklore password |
| **Library ID** | Target library ID in Booklore |
| **Shelf Name** | Shelf to use for tracking (default: `Kobo`) |
| **Label** | Display name in PageKeeper UI (default: `Booklore`) |

### Calibre-Web Automated (CWA)

An alternative ebook source. PageKeeper searches CWA via its OPDS feed.

Configure in **Settings > CWA**:

| Field | Description |
|---|---|
| **Server URL** | e.g. `http://calibre-web:8083` |
| **Username** | CWA login |
| **Password** | CWA password |

### Hardcover

A book tracking service. PageKeeper syncs reading status to Hardcover (write-only — it pushes updates but doesn't pull from Hardcover).

Configure in **Settings > Hardcover**:

| Field | Description |
|---|---|
| **API Token** | Your Hardcover API bearer token |

### BookFusion

An eBook reader with EPUB3 support. Limited integration — PageKeeper can import highlights and reading data.

Configure in **Settings > BookFusion**:

| Field | Description |
|---|---|
| **API Key** | BookFusion API key |
| **Upload API Key** | BookFusion upload API key (if applicable) |

---

## 6. Cross-Format Sync (Alignment)

The core feature: syncing your position between audiobooks and ebooks. This requires an **alignment map** — a lookup table that converts between audio timestamps and ebook character positions.

### Ebook Sources

PageKeeper needs access to your EPUB files to build alignment maps. Three options, in order of simplicity:

1. **Booklore** — PageKeeper fetches EPUBs through the Booklore API. No volume mount needed. Just configure the Booklore integration.

2. **CWA** — Same idea, fetches EPUBs through CWA's OPDS API.

3. **Mount a volume** — Point `/books` at your EPUB directory:
   ```yaml
   volumes:
     - /path/to/your/ebooks:/books:ro
   ```

### Whisper Transcription

When Storyteller native alignment and SMIL data aren't available, PageKeeper falls back to transcribing a segment of the audiobook using Whisper, then fuzzy-matching it against the EPUB text.

Configure in **Settings > Whisper**:

| Setting | Env Var | Default | Description |
|---|---|---|---|
| **Model** | `WHISPER_MODEL` | `tiny` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |
| **Device** | `WHISPER_DEVICE` | `auto` | `auto`, `cpu`, or `cuda` |
| **Compute Type** | `WHISPER_COMPUTE_TYPE` | `auto` | `auto`, `int8`, `float16`, `float32` |

Larger models are more accurate but slower and use more memory:

| Model | Size | Speed | VRAM |
|---|---|---|---|
| `tiny` | 39 MB | Fastest | < 1 GB |
| `base` | 74 MB | Fast | ~ 1 GB |
| `small` | 244 MB | Moderate | ~ 2 GB |
| `medium` | 769 MB | Slow | ~ 5 GB |
| `large` | 1550 MB | Slowest | ~ 10 GB |

### GPU Acceleration

To use an NVIDIA GPU for Whisper transcription:

1. Install [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on your host

2. Build the image with GPU support:
   ```bash
   docker build --build-arg INSTALL_GPU=true -t pagekeeper .
   ```
   This adds ~800 MB to the image for CUDA libraries.

3. Uncomment the GPU section in your `docker-compose.yml`:
   ```yaml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: 1
             capabilities: [gpu]
   ```

4. Set the device to `cuda` in Settings, or let `auto` detect it.

### Deepgram (Cloud Transcription)

As an alternative to local Whisper, you can use Deepgram's cloud transcription API:

| Setting | Env Var | Default |
|---|---|---|
| **API Key** | `DEEPGRAM_API_KEY` | (none) |
| **Model** | `DEEPGRAM_MODEL` | `nova-2` |

### Whisper.cpp (External Server)

You can also offload transcription to an external [Whisper.cpp](https://github.com/ggerganov/whisper.cpp) server:

| Setting | Env Var | Description |
|---|---|---|
| **Server URL** | `WHISPER_CPP_URL` | URL of your Whisper.cpp server |

### Storyteller Native Alignment

The fastest alignment method — no transcription needed. If a book exists in Storyteller and has been processed, PageKeeper reads Storyteller's word-level timing data directly.

Requirements:
1. Mount Storyteller's processing directory:
   ```yaml
   volumes:
     - /path/to/storyteller/processing:/storyteller-data:ro
   ```
2. Set **Assets Directory** to `/storyteller-data` in Settings > Storyteller

PageKeeper checks for Storyteller data first, then SMIL data in the EPUB, and only falls back to Whisper if neither is available.

---

## 7. Split-Port Mode (Exposing KoSync)

By default, PageKeeper serves everything on port 4477. If you want to expose the KoSync sync API to the internet (so your e-reader can sync from anywhere), use split-port mode to separate the sync endpoint from the dashboard.

### Setup

1. Set the `KOSYNC_PORT` environment variable:
   ```yaml
   environment:
     - KOSYNC_PORT=5758
   ports:
     - "4477:4477"    # Dashboard — LAN only, do NOT forward
     - "5758:5758"    # Sync API — safe to expose with TLS
   ```

2. Set up a reverse proxy (nginx, Caddy, Traefik) with TLS in front of the sync port. **KoSync credentials travel in HTTP headers — without TLS they are sent in plaintext.**

3. In PageKeeper Settings > KoSync, enter your **Public URL** (e.g. `https://sync.example.com`)

4. In KOReader, point the custom sync server at your public URL

### Exposed Paths

Only these paths need to be forwarded through your reverse proxy:

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthcheck` | Connectivity check (no auth) |
| GET | `/users/auth` | Authenticate KOReader |
| POST | `/users/create` | KOReader registration |
| POST | `/users/login` | KOReader login |
| GET | `/syncs/progress/<document>` | Retrieve reading position |
| PUT | `/syncs/progress` | Update reading position |

All paths are also available under the `/koreader/` prefix.

### Example Caddy Configuration

```
sync.example.com {
    reverse_proxy localhost:5758
}
```

### Example nginx Configuration

```nginx
server {
    listen 443 ssl;
    server_name sync.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:5758;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 8. Telegram Notifications

PageKeeper can send log notifications to a Telegram chat.

Configure in **Settings > Telegram**:

| Setting | Env Var | Description |
|---|---|---|
| **Bot Token** | `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/botfather) |
| **Chat ID** | `TELEGRAM_CHAT_ID` | Your Telegram chat or group ID |
| **Log Level** | `TELEGRAM_LOG_LEVEL` | Minimum level to send: `ERROR` (default), `WARNING`, `INFO` |

---

## 9. Environment Variable Reference

All settings are configurable from the web UI and persist in the database. Environment variables are **optional overrides** — if set, they take precedence over database values.

### Core

| Variable | Default | Description |
|---|---|---|
| `TZ` | `America/New_York` | Container timezone |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DATA_DIR` | `/data` | Data directory inside the container |
| `BOOKS_DIR` | `/books` | Ebook directory inside the container |
| `KOSYNC_PORT` | (none) | Enable split-port mode on this port |

### Audiobookshelf

| Variable | Default | Description |
|---|---|---|
| `ABS_SERVER` | (none) | Audiobookshelf server URL |
| `ABS_KEY` | (none) | ABS API key |
| `ABS_ENABLED` | `true` | Enable ABS integration |
| `ABS_LIBRARY_IDS` | (none) | Comma-separated library IDs to monitor |
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | ABS collection name |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Offset applied to ABS progress |
| `ABS_SOCKET_ENABLED` | `true` | Enable Socket.IO listener |
| `INSTANT_SYNC_ENABLED` | `true` | Enable real-time sync |

### KoSync

| Variable | Default | Description |
|---|---|---|
| `KOSYNC_ENABLED` | `false` | Enable KoSync server |
| `KOSYNC_USER` | (none) | KoSync username |
| `KOSYNC_KEY` | (none) | KoSync password |
| `KOSYNC_PUBLIC_URL` | (none) | Public URL for KoSync |
| `KOSYNC_HASH_METHOD` | `content` | Book identification method |

### Storyteller

| Variable | Default | Description |
|---|---|---|
| `STORYTELLER_ENABLED` | `false` | Enable Storyteller integration |
| `STORYTELLER_API_URL` | `http://localhost:8001` | Storyteller server URL |
| `STORYTELLER_USER` | (none) | Storyteller username |
| `STORYTELLER_PASSWORD` | (none) | Storyteller password |
| `STORYTELLER_ASSETS_DIR` | (none) | Path to Storyteller processing data |

### Booklore

| Variable | Default | Description |
|---|---|---|
| `BOOKLORE_ENABLED` | `false` | Enable Booklore integration |
| `BOOKLORE_SERVER` | (none) | Booklore server URL |
| `BOOKLORE_USER` | (none) | Booklore username |
| `BOOKLORE_PASSWORD` | (none) | Booklore password |
| `BOOKLORE_LIBRARY_ID` | (none) | Target library ID |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Shelf name for tracking |
| `BOOKLORE_LABEL` | `Booklore` | Display label in UI |

### Calibre-Web Automated (CWA)

| Variable | Default | Description |
|---|---|---|
| `CWA_ENABLED` | `false` | Enable CWA integration |
| `CWA_SERVER` | (none) | CWA server URL |
| `CWA_USERNAME` | (none) | CWA username |
| `CWA_PASSWORD` | (none) | CWA password |

### Hardcover

| Variable | Default | Description |
|---|---|---|
| `HARDCOVER_ENABLED` | `false` | Enable Hardcover sync |
| `HARDCOVER_TOKEN` | (none) | Hardcover API token |

### BookFusion

| Variable | Default | Description |
|---|---|---|
| `BOOKFUSION_ENABLED` | `false` | Enable BookFusion integration |
| `BOOKFUSION_API_KEY` | (none) | BookFusion API key |
| `BOOKFUSION_UPLOAD_API_KEY` | (none) | BookFusion upload API key |

### Whisper / Transcription

| Variable | Default | Description |
|---|---|---|
| `WHISPER_MODEL` | `tiny` | Whisper model size |
| `WHISPER_DEVICE` | `auto` | `auto`, `cpu`, or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | Compute precision |
| `DEEPGRAM_API_KEY` | (none) | Deepgram cloud API key |
| `DEEPGRAM_MODEL` | `nova-2` | Deepgram model |
| `WHISPER_CPP_URL` | (none) | External Whisper.cpp server URL |

### Telegram

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_BOT_TOKEN` | (none) | Telegram bot token |
| `TELEGRAM_CHAT_ID` | (none) | Telegram chat ID |
| `TELEGRAM_LOG_LEVEL` | `ERROR` | Minimum log level to send |

### Sync Tuning

| Variable | Default | Description |
|---|---|---|
| `SYNC_PERIOD_MINS` | `5` | Minutes between full sync sweeps |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Min ABS position change to trigger sync |
| `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Min KoSync position change (%) |
| `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Min cross-client position change (%) |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Min KoSync word count change |
| `FUZZY_MATCH_THRESHOLD` | `80` | Fuzzy match score threshold for suggestions |
| `SUGGESTIONS_ENABLED` | `false` | Enable automatic pairing suggestions |
| `EBOOK_CACHE_SIZE` | `3` | Number of ebooks to cache locally |
| `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Debounce for Socket.IO events |

### Advanced

| Variable | Default | Description |
|---|---|---|
| `JOB_MAX_RETRIES` | `5` | Max retries for failed background jobs |
| `JOB_RETRY_DELAY_MINS` | `15` | Minutes between retries |
| `MONITOR_INTERVAL` | `3600` | Health monitor interval (seconds) |
| `REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT` | `true` | Re-process alignment when progress is cleared |

---

## 10. Volume Reference

| Container Path | Purpose | Required | Mode |
|---|---|---|---|
| `/data` | Database, cache, logs, transcripts | Yes | `rw` |
| `/books` | EPUB ebook files (for alignment) | No* | `ro` |
| `/storyteller-data` | Storyteller processing data (native alignment) | No | `ro` |

\* Not needed if you use Booklore or CWA to fetch ebooks via API.

### What lives in `/data`

```
/data/
  database.db          # SQLite database (settings, books, alignment maps)
  audio_cache/         # Cached audio segments for transcription
  logs/                # Application logs
  transcripts/         # Whisper transcription output
  covers/              # Cached book covers
```

---

## 11. Updating

```bash
cd pagekeeper
git pull
docker compose build
docker compose up -d
```

Database migrations run automatically on startup. Your data in `./data` is preserved across updates.

---

## 12. Troubleshooting

### Container won't start

```bash
docker compose logs --tail=50    # Check for error messages
```

Common issues:
- **Port conflict**: Another service is using port 4477. Change the host port in your compose file (e.g. `8080:4477`).
- **Permission error on `/data`**: Ensure the directory exists and is writable: `mkdir -p ./data && chmod 777 ./data`

### Internal server error on dashboard

Check the logs for the specific Python traceback:
```bash
docker compose logs --tail=100 | grep -A 10 "ERROR"
```

### Integration won't connect

1. Go to Settings and click the **Test** button next to the service
2. Ensure the service URL is reachable from inside the Docker container (use Docker network names, not `localhost`)
3. Check that API keys and credentials are correct

### Alignment fails

- Ensure PageKeeper can access the EPUB file (via `/books` mount, Booklore, or CWA)
- Check that `ffmpeg` is working: `docker exec pagekeeper ffmpeg -version`
- Try a larger Whisper model if transcription quality is poor
- Check `/data/logs/` for detailed error output

### Database issues

The database is a SQLite file at `/data/database.db`. To reset:
```bash
docker compose down
rm ./data/database.db
docker compose up -d
```
This will lose all settings and data. Migrations will recreate the schema on next startup.

### KoSync not syncing

1. Verify KoSync is enabled in Settings
2. Check that the username and password match between PageKeeper and KOReader
3. If using split-port mode, ensure the correct port is exposed and your KOReader points to the right URL
4. Test connectivity: `curl http://localhost:4477/healthcheck`

---

## 13. Local Development (Non-Docker)

For development without Docker:

### Prerequisites

- Python 3.11+
- `ffmpeg` installed and on PATH

### Setup

```bash
git clone https://github.com/serabi/pagekeeper.git
cd pagekeeper
pip install -r requirements.txt
mkdir -p data
alembic upgrade head
python -m src.web_server
```

The server starts at `http://localhost:4477` with the database at `data/database.db`.

### Running Tests

Tests require Docker (for `epubcfi` and `ffmpeg` dependencies):

```bash
./run-tests.sh                              # Full suite
./run-tests.sh tests/test_sync_manager.py   # Single file
./run-tests.sh -k "test_name" -v            # Filtered + verbose
```
