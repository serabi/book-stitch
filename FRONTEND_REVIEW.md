# Frontend simplification review

Date: 2026-07-15
Branch: `prototype/frontend-cleanup-review`

## Recommendation

Keep the dashboard's dark palette, typography, book-forward presentation, and
single teal action color. Use **Direction A: Dashboard + sidebar** as the
implementation base: move navigation into the sidebar while keeping the current
dashboard card design as the visual source of truth.

The main problem is information architecture rather than styling. PageKeeper
currently exposes seven peer destinations in the header, while several are
different modes of the same task. The cleanest map is:

1. **Dashboard** — library home; keep the current card-led design.
2. **Reading** — log, Want to Read, stats, and book detail.
3. **Pairings** — suggestions, add one, and batch matching.
4. **Settings** — sources, sync behavior, and system configuration.

**Activity** should be a secondary utility opened from a persistent health
indicator. Raw logs and KoSync document management remain available inside it.
The global **Add book** action can remain visible, but it should open Pairings
in its single-book mode rather than represent another destination.

## What is making the current frontend feel busy

- The header mixes destinations, external service shortcuts, status counts, and
  the primary Add Book action at one visual level.
- Add Book, Batch Match, and Pairing Suggestions each present a separate model
  for assembling the same audiobook/ebook relationship.
- Reading is one navigation item but contains a log, Want to Read, stats,
  two detail models, goals, journals, highlights, and alignment tools.
- Settings has eleven top-level panels. Basic source setup, sync behavior,
  maintenance tools, and advanced tuning look equally important.
- Logs presents implementation detail first. Most visits should answer either
  “is sync healthy?” or “what needs me?” before exposing raw events.
- Page-specific CSS and markup repeatedly rebuild headers, toolbars, tabs,
  lists, grids, badges, and empty states. That makes otherwise-related flows
  feel unrelated.

## Screen-by-screen direction

### Dashboard

Keep it. Treat its palette, typography, spacing, and book cards as the visual
source of truth. Simplify the top bar around the four-destination map and move
external service shortcuts into a Connected Sources popover.

### Reading

Keep Log, Want to Read, and Stats as one surface. Default to active/recent books,
put search and sorting in one toolbar, and move status filtering into secondary
controls. A book detail should lead with progress and the next likely action;
journal, metadata, highlights, and alignment can follow as sections rather than
competing hero actions.

### Pairings

Merge Suggestions, Add Book, and Batch under one route and data model:

- **Review suggestions** for matches PageKeeper already found.
- **Search one book** for manual audiobook/ebook selection.
- **Build a batch** for queue-based work.

All three paths should end in the same pairing review and confirmation pattern.
This removes three navigation concepts without removing capability.

### Settings

Lead with connected-source health, then show fields only for the selected
source. Reduce the top level to:

- **Sources** — Audiobookshelf, Storyteller, Grimmory, KoSync, CWA, BookFusion,
  Hardcover, and Telegram.
- **Sync** — source priority, polling, instant sync, and reading-date behavior.
- **System** — transcription, maintenance tools, and genuinely advanced values.

Do not put every integration form on the page at once. Disabled integrations
should collapse to one status row and an Enable action.

### Activity

Replace Logs as a primary destination with a human-readable activity and health
surface. Show actionable failures, recent syncs, and client health first. Keep
raw application logs and the Hardcover audit table as diagnostic tabs. Bring
KoSync's Healthy / Needs Attention / Stale grouping into this surface.

## Prototype directions

- **A — Dashboard + sidebar:** selected. Four stable destinations in a sidebar,
  with the current dashboard cards, controls, spacing, and density preserved.
- **B — Dashboard native:** lowest implementation risk. Retains the top bar and
  standardizes every page around dashboard-style headers, toolbars, and cards.
- **C — Guided workflows:** useful for first-run setup and empty states, but too
  opinionated as the only navigation model for experienced users.

The selected production direction is A's sidebar around the existing dashboard
visual system. Guided setup can still inform empty states without becoming the
app's primary navigation model.

## Implementation order

1. Consolidate the global navigation and shared page-header/toolbar patterns.
2. Merge Add Book, Suggestions, and Batch into Pairings without changing their
   backend routes initially.
3. Reshape Settings around Sources / Sync / System using the existing form and
   save behavior.
4. Add the health-led Activity surface, then demote raw Logs and KoSync documents.
5. Simplify Reading detail after the global patterns have settled.

This order changes presentation before backend behavior, so each step can ship
independently and be compared against the current app.
