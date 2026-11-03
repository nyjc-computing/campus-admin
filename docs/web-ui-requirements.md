> **Moved from `campus/campus/audit/docs/web-ui-requirements.md`** (audit-web → campus-admin migration): this spec now lives with the app that hosts the UI. The campus.audit service keeps only the versioned API (/audit/v1); the pages, their AUDIT_ADMINS gate, and the data/keys pages are here — data flows through the versioned API instead of in-process reads.

# Audit Web UI Requirements

**Document Version:** 1.5
**Date:** 2026-04-16 (updated 2026-10-21: header nav gains the Keys link for key-managing sessions with current-page marking, and the waterfall gains span-group collapse affordances — see §4.3 and §5.4; the header is now docked and dense tables scroll horizontally instead of crushing their columns — see §2.2 and §3.3)
**Parent Issue:** #429
**Related:** [campus-trace-api-prd-v3.md](./campus-trace-api-prd-v3.md)

---

## 1. Overview

The Audit Web UI is a browser-based dashboard for exploring API traces captured by the Campus Trace API. It enables developers and operators to visualize request flows, debug errors, and review system performance during fortnightly and quarterly reviews.

**Target Users:**
- Campus developers debugging issues
- System operators reviewing performance
- Student cohorts learning about distributed tracing

**Technology Stack:**
- Backend: Flask (Jinja2 templates)
- Frontend: Vanilla JavaScript (no framework)
- Styling: CSS
- Authentication: OAuth browser flow

---

## 2. Pages & Navigation

### 2.1 Page Structure

| Page | Path | Purpose | Access |
|------|------|---------|--------|
| Landing | `/audit/` | Service intro; links to the trace list and login | Public |
| Trace List | `/audit/traces` | Browse and filter traces | `can_view_traces()` (§5.2) |
| Trace Detail | `/audit/traces/<trace_id>` | View waterfall and span details | `can_view_traces()` (§5.2) |
| Login | `/audit/login` | Sign-in page (button starts the OAuth flow) | Public |
| API-key sign-in | `/audit/login/key` | Paste-key sign-in (development only) | Public (development only) |
| API keys | `/audit/keys` | Mint, re-scope, rotate, revoke API keys | `can_manage_keys()` (§5.2) |
| (Future) Metrics | `/audit/metrics` | Performance dashboards | `can_view_traces()` (§5.2) |

> **Changed (2026-10):** the trace list moved from `/audit/` to
> `/audit/traces` when the public landing page was added; `/audit/login`
> became a real page (its button starts the flow at
> `/audit/login/start`), and logout redirects back to the landing page.
> **Changed (2026-10, #915/#928):** the API-key sign-in page was added,
> and "login required" was replaced by the named capabilities of §5.2.

### 2.2 Navigation

- **Header navigation** on all pages
  - Logo/title: "Campus Audit"
  - Links: "Traces", "Metrics" (disabled/coming soon)
  - User profile/logout (when authenticated)
  - Docked: the header sticks to the top of the viewport, so
    navigation and logout stay reachable while scrolling long trace
    and key lists (it layers under the span drawer)

---

## 3. Trace List Page

### 3.1 Purpose

Browse recent traces with filtering capabilities. Primary entry point for investigating issues.

### 3.2 Filter Bar

**Time Range Presets:**
- Quick buttons: "Past hour", "Past 24h", "Past week"
- Custom date range: Since/Until datetime inputs
- Default: Past 24h

**Filter Fields:**

| Field | Type | Description | API Mapping |
|-------|------|-------------|-------------|
| Path | Text input | Filter by endpoint path (e.g., `/api/v1/students`) | `path` query param |
| Status | Dropdown | All, 2xx, 3xx, 4xx, 5xx | `status` query param |
| Client ID | Text input | Filter by OAuth client ID (also filters by deployment) | `client_id` query param |
| User ID | Text input | Filter by specific user | `user_id` query param |
| Show audit events | Checkbox | Include audit-event traces in the list (#941) | `events` query param |

**Audit events are hidden by default (#941).** The audit service's own
events — key mints, sign-ins, health checks — are ingested as spans in
the same table as the request traces, one trace per event, and they
carry a `tags.event_type` that request traces do not. They are noise
for someone debugging a real request flow, so the viewer leaves them
out unless the "Show audit events" box is ticked (`?events=1`). The
**versioned API keeps returning them**: the trail is queryable by
design, and hiding it at the API would make the audit trail harder to
reach, not easier.

**Extensibility:**
- Filter UI should be structured to allow adding new filter types without major refactoring
- Filter state management should support arbitrary query parameters

### 3.3 Trace List Table

**Columns:**

| Column | Description | Formatting |
|--------|-------------|------------|
| Trace ID | Clickable link to detail page | Monospace, full ID on one line (no wrap; table scrolls horizontally) |
| Status | HTTP status code | Color-coded badge (green/yellow/orange/red) |
| Duration | Total trace duration | In milliseconds (e.g., "142.5ms") |
| Method | HTTP method | Badge (GET, POST, etc.) |
| Path | Request path | Truncated with full path in hover tooltip |
| Client | Client ID | (derived from api_key_id or client_id) |
| User | User ID | "—" if null |
| Timestamp | Start time | ISO 8601, localized (e.g., "2026-04-16 14:32:05") |

**Interactions:**
- Clicking Trace ID navigates to trace detail page
- Row hover effect
- Sortable by timestamp (newest first by default)
- At narrow widths the table keeps a width floor and the list scrolls
  horizontally; cells stay on one line instead of re-wrapping into
  cramped slivers (same rule for the keys table, §5.4)

### 3.4 Pagination

- Cursor-based pagination (using `cursor.next` from API)
- "Load more" button at bottom
- Display: "Showing X of Y traces"
- Infinite scroll optional for future

### 3.5 Empty States

| Scenario | Message |
|----------|---------|
| No traces match filter | "No traces found matching your filters. Try adjusting your search criteria." |
| No traces at all | "No traces recorded yet. Traces will appear here as API traffic flows through Campus." |
| Loading | "Loading traces..." with spinner |

---

## 4. Trace Detail Page

### 4.1 Purpose

Display a single trace with waterfall visualization showing all spans and their timing relationships.

### 4.2 Trace Metadata Section

**Header Information:**

| Field | Display |
|-------|---------|
| Trace ID | Full ID, copy-to-clipboard button |
| Status | Root span status badge |
| Duration | Total duration (e.g., "142.5ms") |
| Started At | ISO 8601 timestamp, localized |
| Method + Path | Root span request (e.g., "GET /api/v1/students") |
| Client ID | Client identifier or "—" |
| User ID | User identifier or "—" |

**Actions:**
- "Back to traces" button
- "Copy trace ID" button

### 4.3 Waterfall Visualization

**Layout:**
```
Timeline: 0ms    50ms   100ms   150ms   200ms
         |       |       |       |       |
GET /api/v1/students      [=======================] 200
  +POST /auth/token       [====] 200
  +GET /api/v1/db/query          [=================] 200
    +SELECT ...users              [======] 200
```

**Visual Elements:**
- **Timeline ruler** at top with millisecond marks
- **Span bars** positioned by offset (left) and duration (width)
- **Indentation** based on depth (nested children)
- **Striping per top-level request** (alternate shading), so a trace
  with many requests reads as grouped blocks rather than one flat list
- **Color coding** by status:
  - 2xx: Green (#10b981)
  - 3xx: Yellow (#f59e0b)
  - 4xx: Orange (#f97316)
  - 5xx: Red (#ef4444)
- **Labels** on span bars: method, path, status code

**Interactions:**
- Hover tooltip: shows full span details (offset, duration, all headers summary)
- Click span bar: opens span details drawer (see §4.4)
- Caret on a span with children: collapse/expand its subtree — the row
  shows `+N` nested spans, and the caret's state is exposed to screen
  readers via aria-expanded
- Collapse all / Expand all controls beside the section title
- Zoom controls (optional for future): zoom in/out of timeline

**Responsive Behavior:**
- Horizontal scroll for wide waterfalls
- Collapse to list view on very small screens

### 4.4 Span Details Drawer

**Purpose:**
Show full request/response data for a single span when clicked.

**Trigger:**
- Click any span bar in waterfall

**Layout:**
- Slide-out panel from right side
- Overlay backdrop (click to close)
- Close button (×) in header

**Content Sections:**

1. **Span Summary**
   - Span ID (copyable)
   - Method + Path + Status Code
   - Duration with offset (e.g., "15.2ms (started at +0.1ms)")
   - Timestamp

2. **Request Headers**
   - Formatted as key-value table
   - Monospace font for values
   - Authorization header shown as "Bearer ***" (redacted)

3. **Request Body**
   - Pretty-printed JSON if present
   - "No body" message if null/empty
   - Syntax highlighting (basic coloring)

4. **Response Headers**
   - Formatted as key-value table
   - Monospace font

5. **Response Body**
   - Pretty-printed JSON if present
   - "No body" message if null/empty
   - Truncation notice if `_truncated: true`
   - Syntax highlighting

6. **Error Details** (if status >= 400)
   - Error message
   - **Traceback** (if present in response JSON)
     - Pretty-printed with indentation
     - Monospace font
     - Syntax highlighting for code frames
     - Expand/collapse for long tracebacks

**Responsive:**
- Full-screen drawer on mobile
- 50% width drawer on desktop (max 600px)

---

## 5. Authentication

> **Implemented (2026-10, #696).** This section describes the shipped
> design, which differs from the original draft: the token is held in a
> server-side signed cookie session (never in localStorage), and the
> browser never sees or sends a Bearer token — data endpoints are
> served by the audit service itself under `/audit/api/*` and authorized
> by the session cookie. Reference implementation:
> `campus/audit/web/auth.py`. The login chain it drives is documented
> in [auth-login-flow.md](../../../docs/auth-login-flow.md).

### 5.1 Authentication Method

**Browser OAuth flow via Campus Auth (authorization-code grant):**

1. Unauthenticated access to any gated `/audit/*` page redirects to
   `/audit/login`, which renders a login page. Its button starts the
   flow at `/audit/login/start` (the landing page at `/audit/` and the
   UI's static assets are public and skip the gate).
2. `/audit/login/start` creates a Campus auth session server-to-server
   (`POST /auth/v1/sessions/campus/`) and redirects the browser to
   Campus Auth `GET /auth/v1/authorize` with the session id as the
   OAuth `state`.
3. Campus Auth authenticates the user (Google Workspace) and redirects
   back to `GET /audit/callback?code=...&state=...`.
4. `/audit/callback` validates `state` (CSRF), exchanges the code at
   `POST /auth/v1/token` (confidential client: `client_id` +
   `client_secret`, server-to-server), and stores the token and user
   identity in the signed Flask cookie session. The token is never
   exposed to browser JS; the browser is then sent to the trace list.
5. Subsequent requests are authorized by the session cookie.
6. `/audit/logout` revokes the token (RFC 7009, best-effort), clears
   the session, and redirects to the landing page.

The gate **fails closed**: if `AUDIT_OAUTH_CLIENT_ID` /
`AUDIT_OAUTH_CLIENT_SECRET` are not configured, gated UI pages return
503 and `/audit/api/*` returns 401 — the UI is never silently open.

**The sign-in is audited (#932):** a completed sign-in emits
`audit.ui.auth.success` carrying the campus.auth account and whether it
is a superadmin; a refusal emits `audit.ui.auth.failed` with a reason
(`invalid_state`, `missing_code`, `auth_service_error`,
`auth_service_unavailable`). These event types are distinct from the
API-key sign-in's `audit.apikeys.*` (§5.3): a Campus account is not an
API key, and the trail should say which credential authenticated. The
account is recorded as a label in the span's tags — campus.audit has
no user store, and its spans carry `user_id=None` by design — and the
access token is never part of an event (it is exchanged server-side
and kept in the session).

**Protected Routes:**
- All `/audit/*` UI pages require authentication AND the trace-view
  capability (§5.2)
- Exceptions: the landing page `/audit/`, the UI's static assets
  (needed by the public pages), and `/audit/v1/health`;
  `/audit/v1/*` keeps its API-key authentication (unchanged)

### 5.2 Authorization

> **Implemented (2026-10, temporary stopgap).** `AUDIT_ADMINS` is the
> campus.audit **superadmin** list: a comma-separated list of
> campus.auth user ids (emails), compared case-insensitively, with
> whitespace around entries ignored. Unset or empty means **no one**
> has access (fail-closed). A superadmin holds both web-UI
> capabilities — managing API keys and reading traces. Authenticated
> sessions without the capability get a 403 page (403 JSON on
> `/audit/api/*`) that names what is missing; the allowlist is read
> per-request, so changes take effect on the next request. Keep the
> list small (≤ 5 accounts) — a proper admin/role gate is planned to
> replace this mechanism.

**Capabilities.** The web UI has exactly two capabilities, and a
session must hold the one the surface needs:

| Capability | Held by a Campus account | Held by an API key |
|---|---|---|
| `can_view_traces()` — read the trace list, trace pages and `/audit/api/*` | on the `AUDIT_ADMINS` list | key holding `traces:read` |
| `can_manage_keys()` — manage audit API keys (mint, re-scope, rotate, revoke) | on the `AUDIT_ADMINS` list | key holding `apikeys:write` |

The two session kinds are not interchangeable: an API key is granted
scope by scope, exactly like the versioned API, so a key with
`apikeys:write` gains no trace access and a key with `traces:read`
gains no key management. A Campus account is a superadmin or it is
not — the list is a single class. `apikeys:read` (listing keys) is an
API/CLI privilege and does not admit a browser session on its own.

**Access Control:**
- Only superadmin accounts (or API keys holding `traces:read`) can view traces
- Users can only view traces from clients they have access to (enforced by API)
- In development, API-key sessions (§5.3) may also view traces
- The key-management surface (the page itself is #927) is gated on
  `can_manage_keys()`

### 5.3 API-key sign-in (#915, development only)

> **Implemented (2026-10, #915).** A second login path for holders of
> audit API keys (the Bearer credentials minted via `/audit/v1/apikeys`),
> so scoped keys issued for the dev environment (e.g. to students) can
> use the trace viewer in the browser. campus.audit stays
> self-contained and user-blind — nothing here calls campus.auth.

- `GET/POST /audit/login/key` (CSRF-protected paste form) verifies the
  key in-process with the same `verify()` the versioned API uses
  (rejects revoked/expired keys, updates `last_used`).
- A key may sign in if its scopes cover **either** web-UI capability —
  `traces:read` (to read traces) or `apikeys:write` (to manage keys)
  — wildcard-aware (`traces:*` / `apikeys:*` / `*` qualify). Keys with
  neither are refused at login. Each surface then checks the
  capability it needs, so signing in does not grant both.
- The session stores only the key id, the key's `owner_id` label
  (display-only, never resolved against a user store), and an expiry:
  12h from issue, capped by the key's own `expires_at`. The plaintext
  key is never stored — cookie sessions are signed, not encrypted.
- Every gated request re-resolves the key record by id, so
  revocation, expiry, and scope changes end access on the next
  request, mid-session.
- API-key and OAuth sessions are mutually exclusive; signing in one
  way clears the other. Logout for an API-key session clears the
  local session only (the key itself stays valid for API/CLI use).
- **Every sign-in is audited (#917):** a success emits
  `audit.apikeys.auth.success` with the key id and the owner label; a
  refusal emits `audit.apikeys.auth.failed` with a reason
  (`invalid_key`, `invalid_format`, `insufficient_scope`,
  `csrf_failed`, `missing_key`) and, when the key is known, its id.
  Malformed keys are refused by a format check before verification,
  with the same user-facing 401 as an unknown key. These are the
  same event types the versioned API emits, so one query answers
  "who authenticated with which key" across both surfaces. The
  plaintext key is never part of an event: it travels in the form
  body, and the event's request context reads the body as JSON
  (empty for a form POST). Events are opt-in, like every other audit
  event (`AUDIT_EVENTS_ENABLED`).
- **Development-only by construction:** both methods 404 unless
  `ENV=development`; the login page shows the second button only in
  development. Dev databases are per-deployment, so minted keys do
  not exist in other environments.
- **The session is development-only too (#919).** Outside development
  an API-key session is refused outright: the browser is
  unauthenticated, so a session minted in development cannot keep
  working if the deployment is ever promoted. There is no trace
  access, no key management, and no actor to attribute an action to.

### 5.4 Key-management page (#927)

> **Implemented (2026-10, #927).** A page for the operator who has to
> grant access: list the deployment's API keys, mint one, re-scope it,
> rotate it, revoke it. The page is gated on the key-management
> capability (§5.2), **not** on the trace-view one — a session that may
> read traces cannot mint keys, and a session that may mint keys is not
> automatically allowed to read traces.

**The surface:** `/audit/keys` (list + mint form), with
`POST /audit/keys/new`, `POST /audit/keys/<id>/scopes`,
`POST /audit/keys/<id>/rotate` and `POST /audit/keys/<id>/revoke`. The
POSTs are CSRF-protected with a single-use token, in the session.
The header navigation offers a **Keys** link only to sessions holding
this capability (marked as the current page while on it); trace-viewer
sessions and anonymous visitors never see it.

**The keys table scrolls, not crushes:** its columns size to their
content — the Actions column keeps room for its scopes form and
buttons — and below its width floor the list scrolls horizontally,
like the trace table (§3.3).

**It offers named roles (#918).** The mint form offers the role
catalogue (`traces-viewer`, `traces-searcher`, `traces-producer`,
`key-operator`) alongside a free-text scope field, so the operator
grants a reviewed privilege set by default — and can still hand-assemble
one for anything the catalogue does not cover. A role and a scope list
are mutually exclusive.

**It acts as the session, never as the operator key.** The resources
layer is called in-process, so no key material passes through the
server, and each mutation is attributed to the session that made it:
the key id for an API-key session, the campus.auth account for a
superadmin session.

**The plaintext is shown once.** A minted or rotated key's plaintext
appears in the response that created it, is served `Cache-Control:
no-store`, and is never stored — not in the session, not in the audit
trail, not in a log line. The server keeps only the key's hash, so a
lost key is rotated or replaced, never recovered.

**Everything it does is audited.** The page emits the same event types
the versioned API does — `audit.apikeys.new`, `.update`, `.regenerate`,
`.revoke` — so one query covers both surfaces.

**The page says the quiet part out loud:** an audit API key is a bearer
credential equivalent to the privileges of its scopes, on every trace in
the deployment, and revocation is the only way to withdraw access a
holder was given.

---

## 6. API Integration

> **Implemented (2026-10, #429/#696).** The browser fetches from the
> audit service's own session-authenticated data endpoints
> (`/audit/api/*`), not the API-key-authed versioned API — the browser
> holds no API key and no Bearer token (see §5).

### 6.1 Endpoints Used

| Endpoint | Purpose | Response |
|----------|---------|----------|
| `GET /audit/api/traces` | List traces with filters (session cookie) | `{"traces": [...], "cursor": {...}}` |
| `GET /audit/api/traces/<trace_id>` | Get trace tree (session cookie) | Trace object with nested spans |
| `GET /audit/api/traces/<trace_id>/spans/<span_id>` | Get span details (session cookie) | Full span with headers/bodies |

The versioned API (`GET /audit/v1/traces` etc.) remains available to
server-side callers with an audit API key and is not used by the
browser.

### 6.2 Request Headers

Data requests carry the login session cookie automatically
(same-origin `fetch`); no `Authorization` header is involved.

### 6.3 Error Handling

| Status | Action |
|--------|--------|
| 401 Unauthorized | Redirect to login (the gate does this server-side before pages render) |
| 403 Forbidden | Show "Access denied" message |
| 404 Not Found | Show "Trace not found" error |
| 429 Too Many Requests | Show rate limit message with Retry-After |
| 500+ | Show generic error with retry option |

---

## 7. Data Display Conventions

### 7.1 Timestamps

- **Storage:** ISO 8601 (UTC)
- **Display:** Local timezone (browser's timezone)
- **Format:** `YYYY-MM-DD HH:MM:SS` for list, full ISO in tooltips
- **Relative time:** "2 hours ago" in tooltips (optional)

### 7.2 Durations

- **Display:** In milliseconds with 1 decimal place
- **Format:** `142.5ms`, `1.2s` (if >= 1 second)
- **Color coding:**
  - < 100ms: Green
  - 100-500ms: Yellow
  - > 500ms: Red

### 7.3 Status Codes

| Range | Color | Label |
|-------|-------|-------|
| 2xx | Green | Success |
| 3xx | Yellow | Redirect |
| 4xx | Orange | Client Error |
| 5xx | Red | Server Error |

### 7.4 IDs

- **Trace ID:** 32 hex chars
- **Span ID:** 16 hex chars
- **Display:** Full trace ID in list tables, on one line without
  wrapping (dense table, horizontal scroll); full on detail pages.
  (Until 2026-10 the list truncated to the first 8 chars.)
- **Font:** Monospace

---

## 8. Non-Functional Requirements

### 8.1 Performance

- Trace list page: Initial load < 2 seconds
- Trace detail page: Initial load < 1 second
- Waterfall rendering: < 500ms for 50 spans
- Span details drawer: < 300ms to open

### 8.2 Browser Support

- Modern browsers: Chrome/Edge 90+, Firefox 88+, Safari 14+
- Mobile: iOS Safari 14+, Chrome Mobile
- Graceful degradation for older browsers

### 8.3 Accessibility

- Keyboard navigation support
- ARIA labels for interactive elements
- Sufficient color contrast (WCAG AA)
- Focus indicators

### 8.4 Security

- All API requests over HTTPS
- No sensitive data in localStorage (except access token)
- Authorization headers redacted in display
- XSS prevention (sanitize user input)

---

## 9. Future Enhancements (Out of Scope)

- Metrics dashboard: throughput, latency percentiles, error rates
- Trace comparison: side-by-side view of two traces
- Export: download trace as JSON
- Trace search: full-text search across paths, headers, bodies
- Live tail: real-time trace stream
- Annotations: add notes to traces for later review
- Alerts: webhook notifications for error patterns

---

## 10. Acceptance Criteria

From issue #429:

- [x] Trace list loads and displays recent traces
- [x] Filters work (path, status, time range, client_id, user_id)
      — path/status/time-range filters are wired; client_id/user_id
      display in the table but are not yet filterable (data endpoint
      passes only path/status/since/until; resource layer already
      supports them)
- [x] Trace detail shows waterfall correctly
- [x] Clicking span shows full headers/bodies
- [x] Tracebacks display formatted when present
- [x] Manual browser testing passes
- [x] OAuth login flow works (#696, verified on dev 2026-10)
- [x] UI is responsive on mobile devices
