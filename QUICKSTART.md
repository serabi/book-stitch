# Quick Start Guide - Book Stitch

## Goal
Get your reading progress syncing across your self-hosted services, and track your reading history. Optionally syncs your reading progress with Hardcover.app.

---

## Step 1: Choose Your Services

Book Stitch syncs progress between any combination of these services:

| Service | Type | What It Does |
|---------|------|-------------|
| **Audiobookshelf** | Audiobook server | Tracks audiobook listening progress |
| **KOSync** | E-reader sync | Syncs KOReader/Calibre reading position |
| **Storyteller** | Ebook server | Tracks ebook reading progress |
| **Booklore** | Library manager | Ebook organization and shelf management |
| **Hardcover** | Social reading | Updates reading status and page progress |

You need **at least two services** to sync between. Common setups:

- **Audiobook + Ebook**: Audiobookshelf + KOSync or Booklore (the classic setup)
- **Audio-only**: Audiobookshelf + Hardcover

---

## Step 2: Clone and Build

There is no published Docker image yet — you'll build from source:

```bash
git clone https://github.com/serabi/book-stitch.git
cd book-stitch
```

---

## Step 3: Create docker-compose.yml

Copy this template. All service credentials (API keys, URLs, passwords) are configured in the **Settings** page after startup — not in this file.

```yaml
services:
  book-stitch:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: book-stitch
    restart: unless-stopped
    environment:
      - TZ=America/New_York
    volumes:
      - ./data:/data
      # Mount your ebooks directory (needed for cross-format sync if not using Booklore):
      # - /path/to/your/ebooks:/books:ro
    ports:
      - "4477:4477"
```

**Volume mounts to consider:**

| Mount | Purpose | When needed |
|-------|---------|-------------|
| `./data:/data` | Database, logs, cache | Always |
| `/path/to/ebooks:/books:ro` | Ebook files (EPUB, etc.) | Cross-format sync without Booklore (Booklore fetches files via API) |

### Optional: Split-port mode (recommended for internet-exposed setups)

If you expose the KOSync sync API to the internet (so KOReader devices can reach it), you should run it on a separate port from the admin dashboard:

```yaml
    environment:
      - TZ=America/New_York
      - KOSYNC_PORT=5858
    ports:
      - "4477:4477"   # Admin dashboard (keep internal - no authentication)
      - "5858:5858"   # KOSync sync API (has authentication)
```

This way you can put only port 5858 behind a reverse proxy, keeping the dashboard private.

---

## Step 4: Start the Container

```bash
docker compose up -d
```

Check if it's running:
```bash
docker compose logs -f
```

Press `Ctrl+C` to exit logs.

---

## Step 5: Open the Web UI

Open your browser to: **http://localhost:4477**

You should see the Book Stitch dashboard.

---

## Step 6: Configure Your Services

1. Go to **Settings** and enable the services you use (Audiobookshelf, KOSync, Storyteller, Booklore, Hardcover, etc.)
2. Enter your server URLs, API keys, and credentials for each service
3. Click **Save** — the app will connect and verify each service

---

## Step 7: Create Your First Book Mapping

1. Click **"Single Match"** on the dashboard
2. Select an audiobook, ebook, or both depending on your setup
3. Click **"Create Mapping"**

That's it! Your mapping is saved and will appear on the dashboard immediately. The background sync picks it up within a few minutes (every 5 minutes by default, configurable in Settings). For audiobook+ebook linked books, an alignment step (Whisper transcription) runs automatically first — this takes additional time depending on book length. You can check transcription progress in the container logs.

---

## Setup Paths

### Audiobook + Ebook (Linked)
1. Enable and configure Audiobookshelf in Settings
2. Enable and configure KOSync (or another ebook service) in Settings
3. Mount your `/books` volume if using local ebook files
4. Use "Single Match" to pair an audiobook with its ebook
5. Progress syncs both directions

### Ebook-Only
1. Enable at least two ebook services in Settings (KOSync, Storyteller, Booklore)
2. Use "Single Match" > "Ebook Only" to import an ebook
3. Progress syncs between all configured ebook services

### Audio-Only
1. Enable Audiobookshelf in Settings
2. Optionally enable Hardcover for social tracking
3. Use "Single Match" > "Audio Only" to import an audiobook
4. Progress syncs to Hardcover (if configured)

---

## Troubleshooting

### Container won't start?
```bash
docker compose logs
```
Look for error messages about missing volumes or startup failures.

### Can't access web UI?
- Check if port 4477 is available: `docker compose ps`
- Try http://localhost:4477 or http://YOUR_SERVER_IP:4477

### Sync not working?
- Check Settings — are your services showing as connected?
- Wait 5 minutes (default sync period)
- Check the dashboard — does it show progress for your mapped books?

---

## What's Next?

Once basic sync is working, explore the Settings page for:
- **Sync tuning** — adjust sync intervals, delta thresholds
- **Instant sync** — real-time sync via Audiobookshelf Socket.IO
- **Hardcover** — social reading tracking
- **Telegram** — notifications for sync events

See the full README.md for advanced features.

---

## Need Help?

- Check the logs: `docker compose logs -f`
- Read the full README.md
- Open an issue on GitHub with your logs
