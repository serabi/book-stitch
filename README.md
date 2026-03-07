# PageKeeper

<div align="center">

<img src="static/icon.png" alt="PageKeeper" width="128">

**Keep your place across every book, every app, every format - and keep a record of everything you read, too!**

[![License](https://img.shields.io/github/license/serabi/pagekeeper?cacheSeconds=3600)](LICENSE)
[![Release](https://img.shields.io/github/v/release/serabi/pagekeeper)](https://github.com/serabi/pagekeeper/releases)
[![Snyk Security](https://snyk.io/test/github/serabi/pagekeeper/badge.svg)](https://snyk.io/test/github/serabi/pagekeeper)
[![CodeRabbit Reviews](https://img.shields.io/coderabbit/prs/github/serabi/pagekeeper?labelColor=171717&color=FF570A&label=CodeRabbit+Reviews)](https://coderabbit.ai)

</div>

---

## What is PageKeeper?

PageKeeper is a self-hosted reading companion that keeps your place across platforms, tracks what you read, and also acts as a reading journal for your notes. Whether you listen to an audiobook during your commute on [Audiobookshelf](https://www.audiobookshelf.org/) and pick up the same book on your e-reader before bed, or just want a single place to see your reading progress across services — PageKeeper handles it.

At its core, PageKeeper is a **reading tracker**: it knows which books you're reading, how far along you are in those books, when you started and finished, and keeps a journal of your progress. On top of that, PageKeeper can **sync your position** between audiobook and ebook platforms by building an alignment map between the audio and the text. Once that map is built, jumping between formats is seamless.

<div align="center">
<img src="static/2026-03-05 - PageKeeper Preview.png" alt="PageKeeper dashboard preview" width="700">
</div>


### Origin story

This project started as a fork of [abs-kosync-bridge](https://github.com/cporcellijr/abs-kosync-bridge), a neat project that syncs Audiobookshelf positions across ebooks. Major kudos to [cporcellijr](https://github.com/cporcellijr) for the original idea and implementation.

The goal of PageKeeper is to be a full fledged reading tracking and journaling system that includes a BookFusion integration. At this point it's essentially a new application, but  it would not have existed without the original project. If you find PageKeeper useful, contributions and suggestions are always welcome - and if you're only looking to sync your audio and ebooks across platforms, and don't care about tracking your reading, you might find abs-kosync-bridge to be a better fit for you!


### App Infrastructure 

This app is built with Python 3.13 and Flask, using SQLAlchemy with SQLite for persistence and Alembic for database migrations. It runs in a Docker container based on `python:3.13-slim`, with ffmpeg for audio processing and faster-whisper installed in-container for speech-to-text transcription. The frontend uses vanilla JavaScript, HTML, and CSS. Dependency injection is handled by dependency-injector, and real-time communication with Audiobookshelf uses python-socketio.

This app does rely heavily on AI coding tools. The tools are directed by a human and I do read the code generated, but I do want to be up front that I am not writing most of the code directly. I run Snyk security to help catch security issues, and CodeRabbit.io's free plan on every PR to help catch bugs in the code. I have made every effort to make this app secure and stable, but there may be issues. Please report any issues that you find! 


### Supported platforms

| Platform | What it does |
|---|---|
| [Audiobookshelf](https://www.audiobookshelf.org/) | Main audiobook server |
| [KOReader](https://koreader.rocks/) (via KoSync) | E-ink reader progress (Boox, Kobo, jailbroken Kindle, etc.) |
| [Storyteller](https://storyteller-platform.gitlab.io/storyteller/) | Audiobook companion app with synced EPUB3 support |
| [Booklore](https://github.com/booklore) | Ebook library and shelf manager |
| [Hardcover](https://hardcover.app/) | Book tracking service (write-only) |
| [BookFusion](https://bookfusion.com/) | eBook reader, includes excellent EPUB3 support (limited integration) |

You can use as few or as many of the above services as you want. None are required to use the app.

---

## How it works

PageKeeper runs three sync layers simultaneously, from fastest to slowest:

1. **Instant sync** — Listens to Audiobookshelf's Socket.IO stream and KOReader's KoSync updates in real time. When you pause an audiobook or push an update from KoReader via KoSync, PageKeeper picks up the change within seconds.

2. **Per-client polling** — Lightweight checks against individual services (Storyteller, Booklore) at their own intervals. Only triggers a sync when the position has actually changed.

3. **Scheduled full sync** — A background sweep every few minutes that catches anything the other layers missed.

When a position change is detected, PageKeeper converts it to every other format (timestamp to percentage, percentage to EPUB position, etc.) and pushes updates to all connected clients. A write-tracker prevents feedback loops — if PageKeeper just pushed a position to a client, it ignores the echo that comes back.

---

## License

[MIT](LICENSE)
