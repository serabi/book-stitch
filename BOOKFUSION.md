# BookFusion Integration

PageKeeper includes an optional [BookFusion](https://www.bookfusion.com) integration that allows you to upload books from Booklore to BookFusion and then syncs your reading highlights back to PageKeeper. This integration is disabled by default and must be enabled in the settings. This document explains how the integration works. 

## Acknowledgements

This integration would not exist without the work of the BookFusion team, who
publish open-source plugins for their platform. We studied two of these plugins
to understand BookFusion's API protocols:

- **[BookFusion Calibre Plugin](https://github.com/BookFusion/calibre-plugin)**
  (GPL v3) — Used as reference for the book upload API, including the three-step
  upload flow, authentication, multipart form encoding, and metadata field names.

- **[BookFusion Obsidian Plugin](https://github.com/BookFusion/obsidian-plugin)**
  (GPL v3) — Used as reference for the highlight sync API, including the sync
  endpoint, cursor-based pagination, and the response data structure.

Thank you to the BookFusion team for making these plugins open source. I love the BookFusion platform and I hope this integration will bring more people to your product! 

## How It Works

PageKeeper contains its own Python implementation of the BookFusion API protocol
in `src/api/bookfusion_client.py`. No code from either plugin is included or
vendored — this is an independent reimplementation built with
Python's `requests` library.

The following technical details were derived from reading the plugin source code:

- **Authentication** — The Calibre upload API uses HTTP Basic Auth (API key as
  the username, empty password). The Obsidian highlight API uses an `X-Token`
  header.
- **Book uploads** — A three-step flow: `POST /uploads/init` with the filename
  and file digest, then a direct upload to BookFusion's servers using pre-signed form parameters,
  then `POST /uploads/finalize` with the storage key, digest, and book metadata.
- **Multipart encoding** — The Calibre API expects multipart form parts with
  only a `Content-Disposition` header (no per-part `Content-Type`), matching the
  format produced by Qt's `QHttpMultiPart`. PageKeeper builds this manually to
  ensure compatibility.
- **File digest** — SHA-256 of the file size (as raw bytes), a null byte, and
  the file content — matching the Calibre plugin's `calculate_digest` method.
- **Metadata** — Book metadata is sent using Rails nested parameter notation
  (e.g. `metadata[title]`, `metadata[author_list][]`).
- **Highlight sync** — Cursor-based pagination over `POST /obsidian-api/sync`,
  yielding pages of type `book` that contain highlight blocks with content,
  chapter headings, and embedded timestamps.

## Future Plans

The BookFusion team is hard at work on integrating KoSync compatibility soon, and later this year, OPDS support. Please follow them at r/bookfusion and give them your support. It's a fantastic product with a great team. 

## Licenses

| Project | License | Usage |
|---------|---------|-------|
| PageKeeper | [MIT](LICENSE) | This project |
| [BookFusion Calibre Plugin](https://github.com/BookFusion/calibre-plugin) | [GPL v3](https://github.com/BookFusion/calibre-plugin/blob/master/LICENSE) | Referenced for API protocol |
| [BookFusion Obsidian Plugin](https://github.com/BookFusion/obsidian-plugin) | [GPL v3](https://github.com/BookFusion/obsidian-plugin/blob/master/LICENSE) | Referenced for API protocol |
