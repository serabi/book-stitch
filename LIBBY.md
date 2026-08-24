# Libby (OverDrive) Integration Research

Exploration of what it would take to pull Libby activity into PageKeeper.
Findings from static analysis of the Libby web app (`dewey-22.1.0`) plus live
HAR captures of real audiobook + ebook reader sessions (Aug 2026).

## TL;DR

Libby has no official patron-facing API, but its private API is fully mapped
by the community and stable since 2023. Reading **positions** are not exposed
by the documented community endpoints, but this research located them:

- **Position store:** `GET https://sentry.libbyapp.com/card/{cardId}/{format}/data/{titleId}`
- **Reader session state:** `GET {passport.urls.possession}` returns saved
  position, bookmarks, highlights, reading time
- **Position writes:** `POST {passport.urls.activity}` `_d/activity` — full
  payload schema captured for both audiobook and ebook

A read-only Libby client (auth → passport → poll possession) is enough to
make Libby a **leader-capable source** in PageKeeper's sync model today.
Write-back to Libby is also feasible (schema known) but riskier.

---

## Architecture

Libby's web app ("dewey") is an AMD shell that delegates to named service
"bosses":

| Boss | Role |
|---|---|
| `sentry` | Auth + catalog + circulation API |
| `lectern` | Reader/player orchestration |
| `vandal` | Tags |
| `scribe` | Telemetry |
| `stasher`, `bank` | Local storage/migrations |

Reader content never touches the shell: the shell acquires a short-lived
**"passport"** containing pre-authorized URLs, then loads the actual reader
("bifocal") in an iframe hosted on a per-session subdomain
(`dewey-{sessionHash}.read.libbyapp.com` for ebooks,
`dewey-{sessionHash}.listen.libbyapp.com` for audio). The session hash in
the hostname *is* the credential — those endpoints require no auth header.

## API surface

### Identity / auth

```
POST https://sentry.libbyapp.com/chip?client=dewey        → identity chip
GET  https://sentry.libbyapp.com/chip/clone/code          → generate code (authenticated)
POST https://sentry.libbyapp.com/chip/clone/code          ← pair via 8-digit setup code
     (body: form-encoded code=NNNNNNNN; JSON body also accepted by pylibby)
GET  https://sentry.libbyapp.com/chip/sync                → loans, holds, cards, tags
     (also: chip/revoke)
```

- **Code generation is a GET, not a POST** — POSTing `/chip/clone/code`
  (even authenticated) returns 404. Verified live Aug 2026; matches
  libby-calibre-plugin (`generate_clone_code` sends no params → its request
  helper defaults to GET).
- Two pairing directions share this endpoint:
  - *Device-generate* (Libby's "Copy To Another Device" on the source
    device): a paired chip GETs a code; another device submits it.
  - *Target-generate* (what PageKeeper does): a fresh blank chip GETs a
    code, the user enters it in their Libby app under Copy To Another
    Device, and Libby clones the library INTO our chip. Completion is
    detected by polling `/chip/sync` until `cards` is non-empty.
  - The Sonos-style web flow at
    `https://libbyapp.com/interview/authenticate/setup-code` is just the
    source-device generator wrapped in an interview UI (~minutes validity).
- All authenticated sentry calls use `Authorization: Bearer {identity}`
  and a browser-like User-Agent with `Accept: application/json`
- `/chip/sync` does **not** include checkout history or per-loan progress

### Circulation (all on sentry, Bearer auth)

```
GET    card/{cardId}/loan/{titleId}/periods
GET    card/{cardId}/loan/{titleId}/fulfill/{subtype}
POST   card/{cardId}/loan/{titleId}          borrow   (PUT = renew)
DELETE card/{cardId}/loan/{titleId}          return early
POST   card/{cardId}/hold/{titleId}          place hold
DELETE card/{cardId}/hold/{titleId}          cancel hold
```

### Reader passport

```
GET /open/book/card/{cardId}/title/{titleId}?t={base64 tData}&website_id={key}
```

`tData` (base64 JSON): `{codex: {title: {titleId, ...}, loan: {psnKey},
library: {key, ...}}, "dewey-url": ..., spec: "V22", locale}`

Response:

```json
{
  "urls": {
    "web":        "https://dewey-{hash}.read.libbyapp.com/",
    "possession": "https://dewey-{hash}.read.libbyapp.com/_d/possession",
    "activity":   "https://dewey-{hash}.read.libbyapp.com/_d/activity"
  },
  "message":   "m={signed blob}",
  "bankscope": "title-{hash8}",
  "expires":   1789395929,
  "leeway":    3600
}
```

`expires` ≈ 21 days. Audio passports use the same shape against a
`.listen.libbyapp.com` host.

### Position read

```
GET {urls.possession}
→ {
  "timestamps": {"generated":…, "created":…, "updated":…, "accessed":…,
                 "stamped":…, "expires":…},
  "position":   null | {…},          // last saved spot (null = never opened)
  "marks":      {"bookmarks": [], "highlights": []},
  "statistics": {"positions": N, "accesses": N, "readingTime": seconds}
}
```

Legacy/shell variant (404 `"result":"missing_stamp"` when empty):

```
GET    https://sentry.libbyapp.com/card/{cardId}/{format}/data/{titleId}
DELETE https://sentry.libbyapp.com/card/any/{format}/data/{titleId}   (reset)
```

`{format}` ∈ `book` (observed) / `audiobook` / `magazine`.

### Position write

Both formats POST to `{urls.activity}` (`_d/activity`) with no auth header:

```json
{
  "environment": {"deviceId": "{uuid}", "timezoneOffset": -14400,
                  "display": {"width": 746, "height": 1039}},
  "activities": {
    "playback-playing": [{"syncstamp": 1787577676577}],
    "position": [ /* one or more */ ],
    "playback-paused": [{"syncstamp": …}]
  }
}
→ {"playback-playing":1,"position":2,"playback-paused":1}   // accepted counts
```

**Audiobook position entry:**

```json
{"uuid":"…","timestamp":1787577677,
 "spinePosition":12,"componentMilliseconds":1492644,
 "percentageOfComponent":0.3226,"percentageOfBook":0.7703,
 "syncstamp":1787577676835,"condensing":24}
```

**Ebook position entry:**

```json
{"uuid":"…","timestamp":1787578136,
 "percentageOfBook":0.03482,"spinePosition":5,
 "percentageOfComponent":0.184,"pageSizePercentage":0.0526,
 "chapterIndex":5,"chapterTitle":"Introduction","citation":"Introduction",
 "syncstamp":1787578135680,"condensing":9}
```

`percentageOfBook` (0–1 float) is present in both formats — the natural key
for PageKeeper's cross-format alignment maps. `spinePosition` indexes spine
components (xhtml files for ebooks, mp3 parts for audio); the reader host
serves them directly, e.g. `/xhtml/{ISBN}_epub3_c007_r1.xhtml`.

### Other observed hosts

| Host | Purpose |
|---|---|
| `thunder.api.overdrive.com` | Catalog metadata, availability, notes |
| `audioclips.cdn.overdrive.com` | Signed audiostream URLs (`expiretime`,`ctime`,`badurl` anti-hotlink) |
| `ic.od-cdn.com` / `img1.od-cdn.com` | Covers |
| `libbyapp.com/api/inscribe` | Telemetry |

## Integration options for PageKeeper

| | Approach | Progress? | Risk |
|---|---|---|---|
| A | Shelf client: `/chip/sync` → books, statuses, due dates, holds/TBR feed | No | Low — community-proven since 2023 |
| B | Official Libby activity CSV export importer → journal/history backfill | Dates only | None |
| C | KOReader bridge: `acsm.koplugin` fulfills Libby ebooks → existing KoSync client tracks progress | Ebooks only | Low |
| D | Native reader-position client (this research): passport → poll possession; optional `_d/activity` write-back | Yes, both formats | Medium — private API |

D subsumes C for progress purposes and works for audiobooks too. Recommended
build order: **A + D-read first** (Libby as leader-capable, read-only),
write-back later if wanted.

## Implementation sketch (D)

1. Settings: setup-code pairing flow → store identity chip (revokeable)
2. Poll loop (existing `client_poller` pattern):
   `/chip/sync` → active loans → per loan: cached passport or re-issue →
   `GET urls.possession` → `position.percentageOfBook` (+ component data)
3. Map to `ServiceState`: percentage → leader election like other clients;
   audio ↔ text alignment can reuse percentage when no alignment map exists
4. Write-back (optional, phase 2): re-issue passport, POST position entries
   to `urls.activity`; always record writes via `WriteTracker`

## Caveats

- Private, undocumented API; endpoints verified Aug 2026 against dewey
  22.1.0 — could change without notice
- Passport/session hosts are short-lived (~hours for reader sessions,
  ~21 days for passport expiry); long-lived polling needs passport refresh
- Path strings inside the shell bundle are routed through an obfuscation
  module (`obf/shib.js`, not directly fetchable); call-site extraction used here instead
- ToS posture: same territory as libby-calibre-plugin / odmpy; personal-use
  automation against one's own library card

## Sources

- Static RE: `libbyapp.com` shell bundle (`/dewey-22.1.0/src/main.js`)
- Live captures: audiobook playback + fresh ebook open/page turns (HAR)
- Community: sgmoore/libby-calibre-plugin (auth + circulation), lullius/pylibby,
  ping/odmpy, andylbrummer/booklife-mcp (confirmed `/chip/sync` lacks progress)
