# Dashboard-with-sidebar frontend design plan

Date: 2026-07-15
Branch: `prototype/frontend-cleanup-review`
Status: Ready for implementation

## Goal

Keep the existing dashboard's visual design and move PageKeeper's primary
navigation into a desktop sidebar. Then bring Reading, Pairings, Settings, and
Activity into that same visual language without changing their backend
behavior.

The current dashboard is the source of truth. This plan does **not** introduce
a second card system, new colors, or a new design library.

## Visual contract

Preserve these existing dashboard choices:

- Square book covers and the current `book-card` hierarchy.
- The purple surfaces, subtle borders, Outfit headings, and IBM Plex Sans body
  text already defined in `variables.css`, `layout.css`, and `components.css`.
- The current controls bar, teal progress treatment, section spacing, status
  colors, hover behavior, and responsive card grid.
- The dashboard's existing data, card actions, filtering, sorting, and modals.

The sidebar changes the app shell only. The dashboard should look like the
current dashboard after implementation, with less horizontal space and no top
navigation bar.

## Navigation map

| Sidebar item | Existing destination(s) | Implementation rule |
|---|---|---|
| Dashboard | `/` | Keep the current dashboard template and cards. |
| Reading | `/reading`, `/reading/tbr`, `/reading/stats`, detail routes | Keep the existing routes and in-page tabs. |
| Pairings | `/suggestions`, `/match`, `/batch-match` | One sidebar item; keep all three routes and present them as modes. |
| Settings | `/settings` | Keep the current form names, tab IDs, and POST behavior. |
| Add book | `/match` | Persistent primary sidebar action, not another destination. |
| Activity | `/logs`, `/kosync-documents` | Secondary utility at the bottom of the sidebar. |

External service shortcuts remain available as a compact icon group near the
top of the sidebar. They do not become primary destinations.

## Non-goals

- No route consolidation in the first implementation.
- No backend service, repository, database, or API response changes unless a
  later Activity requirement proves one is necessary.
- No frontend framework, component library, icon package, or build step.
- No rewrite of working page JavaScript.
- No forced reuse of the dashboard book-card macro where a page has different
  data or actions. Match its visual contract instead.
- No fake global health count. The Activity link stays neutral until the app
  has a truthful, cheap definition of “needs attention.”

## Phase 1 — Replace the top bar with the shared sidebar

### Files

- Modify `templates/partials/navbar.html`
- Modify `static/css/layout.css`
- Modify `static/css/components.css` only if a shared badge/button rule is
  genuinely missing
- Add `tests/test_navigation_shell.py`

### Work

1. Reuse `partials/navbar.html`, which every full page already includes. Change
   its markup from a top header to a fixed desktop sidebar; do not create a
   second shell partial.
2. Keep the PageKeeper brand and DEV badge at the top.
3. Render configured external services with the existing URL helpers and icon
   assets in a compact grid.
4. Render Dashboard, Reading, Pairings, and Settings as the only primary links.
   Pairings is active for `/suggestions`, `/match`, and `/batch-match`.
5. Keep the existing suggestion-count badge on Pairings.
6. Add the existing `/match` action as the sidebar's teal Add Book button.
7. Put a neutral Activity link to `/logs` at the bottom. Do not claim “healthy”
   or show an attention count yet.
8. Offset `.container` on desktop without changing its 1800px maximum, padding,
   or the dashboard grid/card rules.
9. At the existing mobile breakpoint, convert the same four primary links into
   a bottom navigation bar. Keep Add Book and Activity reachable through a
   compact overflow/menu control.
10. Add a visible keyboard-focus state and `aria-current="page"` to the active
    destination.

### Acceptance

- All ten templates that include `partials/navbar.html` render inside the new
  shell without template-specific wrapper edits.
- Dashboard cards, controls, modals, and card menus behave as before.
- Pairings is active on all three existing pairing routes.
- Every destination is keyboard reachable at desktop and mobile widths.
- No Flask route or API snapshot changes.

## Phase 2 — Establish the dashboard visual contract on shared page chrome

### Files

- Modify `static/css/layout.css`
- Modify `static/css/components.css`
- Modify page-specific CSS only when a collision is found

### Work

1. Use the existing `.page-header`, `.controls-bar`, buttons, inputs, tabs,
   badges, surfaces, and spacing before adding a class.
2. Add at most these missing shared patterns after two pages need them:
   `page-subnav`, `content-card`, and `list-row`.
3. Remove page-specific declarations only when the shared rule is a direct
   replacement; avoid a broad CSS cleanup during the redesign.
4. Keep `variables.css` unchanged unless a real dashboard token is missing.

### Acceptance

- New shared rules visually match the dashboard and reuse its tokens.
- No duplicate “v2” card, button, input, or color system appears.
- The dashboard itself needs no markup redesign in this phase.

## Phase 3 — Reading

### Files

- Modify `templates/reading.html`
- Modify `templates/reading_detail.html`
- Modify `templates/tbr_detail.html` only for shared shell/chrome alignment
- Modify `static/css/reading.css`
- Modify `static/js/reading.js` only where markup selectors must move
- Extend `tests/test_reading_routes.py`

### Work

1. Keep Reading Now, Want to Read, and Stats on their existing routes and tabs.
2. Make active-book cards use the dashboard's square cover, surface, title,
   author, progress, status, and hover treatment.
3. Keep reading-only metadata and actions in the Reading card; do not adapt the
   dashboard macro with conditionals for a different data shape.
4. Use one dashboard-style controls bar for search, status, sort, and view.
5. Keep book detail focused on current progress and the next likely action,
   followed by journal, metadata, highlights, and alignment sections.
6. Preserve all existing API calls, element IDs required by `reading.js`, form
   behavior, and detail URLs.

### Acceptance

- Existing Reading, TBR, Stats, and detail route tests pass unchanged unless a
  rendered-markup assertion intentionally changes.
- Grid/list switching, search, sorting, goal editing, journal actions, and
  detail modals still work.
- Cards clearly belong to the dashboard family without losing Reading data.

## Phase 4 — Pairings

### Files

- Add `templates/partials/pairings_nav.html`
- Modify `templates/suggestions.html`
- Modify `templates/match.html`
- Modify `templates/batch_match.html`
- Modify `static/css/suggestions.css`
- Modify `static/css/match.css`
- Modify page JavaScript only for selector changes
- Extend `tests/test_suggestions_feature.py` and existing matching tests

### Work

1. Add one small shared sub-navigation with Suggestions, Add One, and Batch.
2. Keep `/suggestions`, `/match`, and `/batch-match`; do not merge route handlers
   or backend data models during the design pass.
3. Restyle suggestion rows and selectable source results with dashboard
   surfaces, borders, cover ratios, badges, and teal selection/progress cues.
4. Give all three modes the same header and controls-bar rhythm.
5. Keep all existing form field names, POST actions, queue behavior, rescan
   behavior, confirmation modals, and redirect targets.

### Acceptance

- One sidebar destination and one local mode switch explain the whole pairing
  area.
- Direct links to all three existing routes still work.
- Single, audio-only, ebook-only, attach, suggestion, and batch paths retain
  their current behavior.

## Phase 5 — Settings

### Files

- Modify `templates/settings.html`
- Modify `static/css/settings.css`
- Modify `static/js/settings.js` only where navigation markup requires it
- Extend `tests/test_settings_comprehensive.py` or
  `tests/test_apply_settings_integration.py`

### Work

1. Avoid a second permanent sidebar inside the global sidebar shell.
2. Present three content-level groups: Sources, Sync, and System.
3. Preserve every current tab ID, panel ID, input name, enable toggle, secret
   fetch, connection test, deep link, and save path underneath the new grouping.
4. In Sources, show compact connection summaries; open one existing integration
   panel at a time.
5. Move polling, instant sync, and reading-date behavior under Sync.
6. Move transcription, maintenance tools, and genuinely advanced settings under
   System.
7. Keep disabled services collapsed to their summary and enable action.

### Acceptance

- Saving the form produces the same POST payload as before.
- Existing connection tests, secret reveal behavior, enable/disable toggles,
  deep links, and unsaved-changes guard still work.
- No settings field or backend configuration key is renamed.

## Phase 6 — Activity

### Files

- Modify `templates/logs.html`
- Modify `static/css/logs.css`
- Modify `static/js/logs.js`
- Reuse `GET /api/kosync-documents` if Needs Attention includes KoSync items
- Extend `tests/test_logs_routes.py`

### Work

1. Keep `/logs` but label the surface Activity in the UI.
2. Order the page as Needs Attention, Recent Activity, then Raw Logs.
3. Reuse the existing application-log and Hardcover-log APIs and live mode.
4. Link KoSync attention items to `/kosync-documents`; do not duplicate its
   mutation UI inside Activity in the first pass.
5. Only add a sidebar attention badge after a truthful count can be derived from
   existing APIs without a new database query on every page. Otherwise keep the
   neutral Activity link.

### Acceptance

- Existing log filtering, pagination, live mode, and Hardcover audit behavior
  remain available.
- The default view answers “what needs me?” before showing raw log lines.
- No invented or stale health state appears in the global shell.

## Phase 7 — Verification and visual review

### Automated checks

- Run focused route/template tests after each phase.
- Run `tests/test_route_inventory.py` and `tests/test_fetch_url_inventory.py` to
  prove that the UI redesign did not change contracts.
- Run the full non-Docker test suite before the branch is ready for review.
- Add one navigation-shell test rather than a large snapshot suite.

### Browser checks

Exercise the real app with representative data at:

- 1440px and wider: fixed sidebar, four-column dashboard where space permits.
- 1024px: fixed sidebar, responsive two/three-column content.
- 768px: mobile navigation transition.
- 390px: bottom navigation, reachable Add Book and Activity, no covered actions.

Check keyboard navigation, focus visibility, active destination state, scroll,
modals, menus, empty states, long titles, missing covers, disabled integrations,
and attention badges. Compare dashboard screenshots before and after Phase 1;
the expected difference is the shell and available width, not the card design.

## Implementation sequence

Implement and review one phase at a time on this branch. Phase 1 is the tracer
bullet: it proves the dashboard-with-sidebar direction across every page while
touching only the shared shell. Do not begin the Reading, Pairings, Settings, or
Activity redesign until the real dashboard still looks right in that shell.
