# PageKeeper

<div align="center">

<img src="static/grimmory-app.png" alt="PageKeeper" width="128">

**Track your reading history across every app and every book format!**

[![License](https://img.shields.io/github/license/serabi/pagekeeper?cacheSeconds=3600)](LICENSE)
[![Release](https://img.shields.io/github/v/release/serabi/pagekeeper)](https://github.com/serabi/pagekeeper/releases)
[![Snyk Security](https://snyk.io/test/github/serabi/pagekeeper/badge.svg)](https://snyk.io/test/github/serabi/pagekeeper)
[![CodeRabbit Reviews](https://img.shields.io/coderabbit/prs/github/serabi/pagekeeper?labelColor=171717&color=FF570A&label=CodeRabbit+Reviews)](https://coderabbit.ai)

</div>

---

## What is PageKeeper?

PageKeeper is a self-hosted reading companion that aligns your books across multiple self-hosted platforms, tracks what you read, and also acts as a reading journal for your notes. PageKeeper is a **reading tracker**: it knows which books you're reading, how far along you are in those books, when you started and finished, and keeps a journal of your progress. On top of that, PageKeeper can **sync your position** between audiobook and ebook platforms by building an alignment map between the audio and the text. Once that map is built, jumping between formats is seamless.

Right now, PageKeeper is managed by one person. Contributions are welcome - you can read more about how to contribute [here](CONTRIBUTING.md).

<div align="center">
<img src="static/2026-03-05 - PageKeeper Preview.png" alt="PageKeeper dashboard preview" width="700">
</div>


### Origin story

This project started as a fork of [abs-kosync-bridge](https://github.com/cporcellijr/abs-kosync-bridge), a neat project that syncs Audiobookshelf positions across ebooks. Major kudos to [cporcellijr](https://github.com/cporcellijr) for the original idea and implementation.

The goal of PageKeeper is to be a full fledged reading tracking and journaling system that includes a BookFusion integration. While PageKeeper has greatly diverged from the original project, it would not have existed without abs-kosync-bridge. If you're only looking to sync your audio and ebooks across platforms, and don't care about tracking your reading, you might find abs-kosync-bridge to be a better fit for you!

---

### Supported platforms

| Platform | What it does |
|---|---|
| [Audiobookshelf](https://www.audiobookshelf.org/) | Main audiobook server |
| [KOReader](https://koreader.rocks/) (via KoSync) | E-ink reader progress (Boox, Kobo, jailbroken Kindle, etc.) |
| [Storyteller](https://storyteller-platform.gitlab.io/storyteller/) | Audiobook companion app with synced EPUB3 support |
| [Grimmory](https://github.com/grimmory) | Ebook library and shelf manager |
| [Hardcover](https://hardcover.app/) | Book tracking service (write-only) |
| [BookFusion](https://bookfusion.com/) | eBook reader, includes excellent EPUB3 support (limited integration) |

You can use as few or as many of the above services as you want. None are required to use the app. If there's another platform you'd liek to see integrated, please open an issue or PR. 

---

## How it works

PageKeeper runs three sync layers simultaneously, from fastest to slowest:

1. **Instant sync** — Listens to Audiobookshelf's Socket.IO stream and KOReader's KoSync updates in real time. When you pause an audiobook or push an update from KoReader via KoSync, PageKeeper picks up the change within seconds.

2. **Per-client polling** — Lightweight checks against individual services (Storyteller, Grimmory) at their own intervals. Only triggers a sync when the position has actually changed.

3. **Scheduled full sync** — A background sweep every few minutes that catches anything the other layers missed.

When a position change is detected, PageKeeper converts it to every other format (timestamp to percentage, percentage to EPUB position, etc.) and pushes updates to all connected clients. A write-tracker prevents feedback loops — if PageKeeper just pushed a position to a client, it ignores the echo that comes back.

---

## Installation

### Quick Start

1. Create a directory for PageKeeper and download the example compose file:

```bash
mkdir pagekeeper && cd pagekeeper
mkdir data
curl -O https://raw.githubusercontent.com/serabi/pagekeeper/main/docker-compose.example.yml
cp docker-compose.example.yml docker-compose.yml
```

2. Edit `docker-compose.yml` to set your timezone and configure any volumes you need (see comments in the file).

3. Start PageKeeper:

```bash
docker compose up -d
```

4. Open the dashboard at `http://localhost:4477` and configure your integrations in **Settings**.

### Securing API secrets at rest

PageKeeper encrypts API keys, tokens, and passwords (e.g. `HARDCOVER_TOKEN`,
`BOOKFUSION_API_KEY`, `KOSYNC_KEY`) before storing them in the SQLite database.
Encryption uses Fernet with a key derived from a master secret that is **never
written to the database**.

The master secret is discovered in this order:

1. `PAGEKEEPER_SETTINGS_ENCRYPTION_KEY` — the recommended, dedicated secret.
2. The persistent Flask secret (`FLASK_SECRET_KEY`, or the auto-generated
   `/data/.flask_secret_key` file) as a fallback.

To set a dedicated key, generate a strong value and pass it as an environment
variable:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```yaml
environment:
  - PAGEKEEPER_SETTINGS_ENCRYPTION_KEY=your-generated-secret
```

Existing plaintext secrets are migrated to encrypted form automatically on the
next startup — no manual steps are needed, and the migration is idempotent.
Before that one-way migration encrypts the first secret, PageKeeper writes a
timestamped backup of the database next to it
(`database.db.pre-settings-encryption-YYYYMMDD-HHMMSS-*`). Backups are created
with owner-only (`0600`) permissions, and older matching backups are tightened
to `0600` on the next settings migration check. If the backup cannot be
written the migration is skipped and your plaintext secrets are left untouched,
so an upgrade never encrypts without leaving a recovery point. The backup is
only created when there is at least one plaintext secret to migrate.

Database upgrades fail closed when migration history or required schema is
missing. Pre-Alembic databases are upgraded automatically only when all four
original baseline tables (`books`, `hardcover_details`, `states`, and `jobs`)
and their baseline columns are present. Partial or manually altered legacy
schemas must be restored from backup or repaired before PageKeeper will start;
they are never stamped as current automatically.

Setting a dedicated `PAGEKEEPER_SETTINGS_ENCRYPTION_KEY` is **strongly
recommended**: it keeps your encryption key independent of the Flask session
secret, so rotating or regenerating the session secret never strands your stored
secrets. The active key source is logged at startup (the source class only,
never the key value) to help diagnose wrong-key errors.

**Key loss still means re-entering secrets.** If you lose the master secret,
encrypted secret *values* cannot be decrypted and must be re-entered in
**Settings**. The pre-encryption backup gives you an upgrade recovery path (the
pre-migration plaintext), but it is not a substitute for backing the key up
alongside your `data/` directory. PageKeeper preserves undecryptable secret rows
rather than overwriting them, and logs which secrets could not be decrypted (by
name only) so the condition is visible.

**Key rotation:** to rotate the master secret, move the old value to
`PAGEKEEPER_SETTINGS_ENCRYPTION_KEY_PREVIOUS` and set the new value in
`PAGEKEEPER_SETTINGS_ENCRYPTION_KEY`. PageKeeper keeps decrypting with the
previous key while re-encrypting writes under the new key. Remove the previous
key once all secrets have been re-saved.

### Updating

```bash
docker compose pull
docker compose up -d
```

### Pinning a Version

By default, `docker-compose.example.yml` uses the `latest` tag. To pin to a specific release:

```yaml
image: ghcr.io/serabi/pagekeeper:0.1.3
```

Available tags are listed on the [packages page](https://github.com/serabi/pagekeeper/pkgs/container/pagekeeper).

---

## Raspberry Pi / ARM64

The core app — sync, dashboard, reading tracker, database — runs on ARM64 (including Raspberry Pi 4/5) with no changes. Expect **~80–150 MB RAM** for normal use.

The one caveat is **audio↔text alignment via local transcription**. PageKeeper bundles `faster-whisper` for speech-to-text, which depends on `ctranslate2` — a library that can be difficult to build on ARM. If you run into build failures on ARM, you have three alternatives:

- **Storyteller native alignment** — If your audiobooks are in [Storyteller](https://storyteller-platform.gitlab.io/storyteller/), PageKeeper can read its pre-computed word-level timelines directly. No local transcription needed.
- **Whisper.cpp server** — Run a [whisper.cpp](https://github.com/ggerganov/whisper.cpp) HTTP server on a more capable machine on your network, then point PageKeeper at it in Settings → Transcription Provider.
- **Deepgram** — Use [Deepgram](https://deepgram.com/) as a cloud transcription provider (configured in Settings). This offloads the work to an API and avoids the ARM build issue entirely.

If you do manage to get `faster-whisper` running on ARM, note that local Whisper transcription is memory-intensive: the `base` model needs **~1 GB RAM**, while `large-v3` can require **4–8 GB**.

---

## App Infrastructure

This app is built with Python 3.13 and Flask, using SQLAlchemy with SQLite for persistence and Alembic for database migrations. It runs in a Docker container based on `python:3.13-slim`, with ffmpeg for audio processing and faster-whisper installed in-container for speech-to-text transcription. The frontend uses vanilla JavaScript, HTML, and CSS. Dependency injection is handled by dependency-injector, and real-time communication with Audiobookshelf uses python-socketio.

This app does rely heavily on AI coding tools. The tools are directed by a human and I do read the code generated, but I do want to be up front that I am not writing most of the code directly. I run Snyk security to help catch security issues, and CodeRabbit.io's free plan on every PR to help catch bugs in the code. I have made every effort to make this app secure and stable, but there may be issues. Please report any issues that you find! 


## License

[MIT](LICENSE)
