# Campus Admin Portal

Administrative portal for NYJC Campus, providing staff access to campus management features through a secure OAuth-based authentication system.

## Overview

The Campus Admin Portal is a Flask web application that integrates with the Campus authentication service to provide authenticated access to administrative features. It uses the Campus OAuth2 flow with Google Workspace sign-in for secure authentication.

## Features

- OAuth2 authentication via Campus auth service
- Google Workspace integration (nyjc.edu.sg domain)
- Session management with Flask sessions
- Secure login flow with redirect support
- Dashboard for authenticated users
- Audit web UI (`/audit`): trace viewer and audit API-key management,
  reachable to the accounts on the deployment's `AUDIT_ADMINS` list
  (key management: audit superadmins), with data proxied to the audit
  service's versioned API (see `docs/web-ui-requirements.md`)
- Admin section (`/admin`): campus.auth resources — clients, users,
  vaults, sessions, credentials — for platform operators and for the
  admins campus.auth designates through access grants
- Grants view (`/admin/grants`): the campus.auth access-grant store —
  who may administer what — for user grantees (a designation at a
  level, over a resource, one vault label or one service's migrations)
  and client grantees (their access to vault labels), with grant and
  revoke
- Migrations view (`/admin/migrations`, #75): per-service database
  migration plan/ledger with apply, abort and rollback — the manual
  production migration surface (see "Migrations view" under the
  Access Model for its two-leg authority and deployment prerequisites)
- Resources section (`/resources`): management views over the
  campus.api service's resources — circles (the domain access
  control), venues, timetable versions, assignments, submissions,
  subjcombi mappings, files and hosted apps — acting as the signed-in
  user, within their own circle access (see the Access Model)

Each section takes its access decisions from the service it fronts;
[docs/auth-model.md](docs/auth-model.md) states the model in full.

## Tech Stack

- **Framework:** Flask 3.0+
- **Authentication:** Campus OAuth2 + Google Workspace
- **Python:** 3.11, 3.12, 3.13
- **Package Management:** Poetry
- **Dependencies:**
  - `campus-api-python` - Campus API client library
  - `campus-suite` - Campus backend services
  - `flask` - Web framework
  - `python-dotenv` - Environment configuration

## Setup

### Prerequisites

- Python 3.11, 3.12, or 3.13
- Poetry
- Access to Campus development environment
- OAuth client credentials (CLIENT_ID and CLIENT_SECRET)

### Installation

1. **Clone the repository** (if not already done):
   ```bash
   git clone https://github.com/nyjc-computing/campus-admin.git
   cd campus-admin
   ```

2. **Install dependencies**:
   ```bash
   poetry install
   ```

3. **Configure environment variables**:

   Copy the example file and fill in the values (see the
   [Environment Variables](#environment-variables) table below for
   details):
   ```bash
   cp .env.example .env
   ```
   At minimum, set `SECRET_KEY`, `CLIENT_ID`,
   `CLIENT_SECRET`, and `PUBLIC_URL`.

### Running the Application

Start the Flask development server:

```bash
poetry run flask --app apps.admin run
```

The application will be available at `http://localhost:5000`. Sign-in
goes through the Campus auth service (Google Workspace), so the OAuth
client in `CLIENT_ID`/`CLIENT_SECRET` must have
`http://localhost:5000/finalize_login` registered as a redirect URI
(register it through this portal's own Clients page, or with campus-cli).

## Project Structure

```
campus-admin/
├── apps/
│   └── admin/              # Main application package
│       ├── __init__.py     # App factory, admin-section routes, templates/static
│       ├── authz.py       # Access model: who may do what, per service
│       ├── audit/          # Audit web UI (its own /audit namespace + gate)
│       ├── migrations/     # Migrations view (/admin/migrations, #75)
│       ├── grants.py       # Grants view (/admin/grants: the access-grant store)
│       ├── resources/      # Resources section (/resources: campus.api management)
│       └── templates/      # Jinja templates
├── scripts/                # Utility and test scripts
│   ├── test_auth.py           # Authentication diagnostics
│   ├── test_oauth_flow.py     # End-to-end OAuth testing
│   ├── preflight.py           # Infrastructure access checks (docs/ops-access.md)
│   └── refresh-dependencies.sh # Dependency refresh utility
├── docs/                   # Documentation
│   ├── auth-model.md       # Access model: who may do what, per service
│   ├── ops-access.md       # Infrastructure access map (Railway/CLI traps)
│   ├── browser-automation.md  # Playwright setup guide
│   └── TESTING.md          # Testing guide
├── .env.example            # Environment template (copy to .env)
├── .github/workflows/ci.yml # Ruff + smoke tests (Python 3.11–3.13)
├── pyproject.toml          # Poetry dependencies
└── README.md               # This file
```

## Authentication Flow

1. User visits `/login?next=/admin/dashboard`
2. Application redirects to Campus auth service with OAuth parameters
3. Campus auth redirects to Google Workspace sign-in
4. User authenticates with Google (nyjc.edu.sg account)
5. Google redirects back to Campus auth with authorization code
6. Campus auth creates session and redirects to `{PUBLIC_URL}/finalize_login`
7. Application validates session and redirects to requested page (`/admin/dashboard`)

## Environment Variables

Copy `.env.example` to `.env` and fill in the values. The app reads
`SECRET_KEY`, `ADMIN_CIRCLE`, `AUDIT_ADMINS`, `AUDIT_KEY_ADMINS` and
`PUBLIC_URL` directly; `CLIENT_ID`, `CLIENT_SECRET`, `ENV`/`CAMPUS_ENV`,
and `CAMPUS_AUTH_URL`/`CAMPUS_API_URL` are read inside campus-python.

| Variable | Required | Description |
|----------|----------|-----------|
| `SECRET_KEY` | Yes | Flask session signing key. The app refuses to start without it. |
| `ADMIN_CIRCLE` | No | The circle whose `admin` members are this portal's **platform operators**: they drive the portal as its deployment's operator client (the operator console). Defaults to `campus`, the platform-admin wildcard circle the campus.api bootstrap seeds. Entry to the Admin section itself is campus.auth authority — a platform operator, or any account holding at least one access grant in the campus.auth grant store. Membership resolves transitively through member circles, and only the `admin` access value qualifies. |
| `CLIENT_ID` | Yes | OAuth client ID of this portal, from the Campus auth service (read by campus-python in "server" mode). The same client is the deployment's operator on campus.auth (`AUTH_OPERATOR_CLIENT_IDS` there). |
| `CLIENT_SECRET` | Yes | OAuth client secret for `CLIENT_ID`. |
| `AUDIT_ADMINS` | No | Comma-separated campus user ids (emails) who may read traces in `/audit`. Admin-only on production; may include students on **development**. Unset or empty allows no one. |
| `AUDIT_KEY_ADMINS` | No | The audit superadmins, who may additionally manage audit API keys: comma-separated user ids. Unset, falls back to `AUDIT_ADMINS`; set it wherever `AUDIT_ADMINS` is wider than the admin list. Key management never opens to students. |
| `PUBLIC_URL` | Recommended | Public origin of this portal. The OAuth callback is `{PUBLIC_URL}/finalize_login`, and that exact URI must be registered as a redirect URI on the client. Falls back to `https://{HOSTNAME}` (deprecated). |
| `ENV` | No | Campus environment tier: `development` (default), `staging`, or `production`. Determines the auth/API service URLs campus-python uses. `CAMPUS_ENV` is accepted as an alias. |
| `CAMPUS_AUTH_URL` | No | Explicit Campus auth service base URL; takes precedence over the `ENV` tier. Set this for non-standard deployments. |
| `CAMPUS_API_URL` | No | Explicit Campus API service base URL; same precedence rules. |
| `AUDIT_UI_API_KEY` | For `/audit` | Audit API key the portal uses server-side for trace reads and key management (scopes `traces:read`, `apikeys:read`, `apikeys:write`). See `.env.example`. |
| `HOSTNAME`, `DEPLOY` | — | Deprecated, ignored for URL resolution since campus-api-python#53 (a `DeprecationWarning` is emitted if they would previously have been used). |
| `AUTH_ADMINS`, `ADMINS` | — | Obsolete. These portal allowlists gated nothing once the Admin section moved to campus.auth authority; remove them. |

Service URL resolution order (campus-python): explicit `CAMPUS_AUTH_URL` /
`CAMPUS_API_URL`, then the `ENV` tier — `development` → Railway dev
deployments, `staging` → `{service}.campus.nyjc.dev`, `production` →
`{service}.campus.nyjc.app`.

## Access Model (who can do what)

Each section is a UI over one service, and that service is the only
authority for it. [docs/auth-model.md](docs/auth-model.md) states the
model and how the portal implements it; in short:

| Surface | Who may use it | Acting as | Decided by |
|---------|----------------|-----------|------------|
| Auth-ops section (`/admin/auth/*`: dashboard, clients, users, vaults, sessions, credentials, grants, migrations, step-up) | platform operators (`admin` on the `ADMIN_CIRCLE` circle) **or** any account holding at least one campus.auth access grant | operators: the portal's operator client; grant holders: themselves | campus.auth, on every call |
| API section (`/admin/api/*`: circles, venues, timetable, assignments, submissions, subjcombi, files, hosted apps) | anyone with circle access to a resource family (every staff account can read) | always the signed-in user | campus.api, on every call |
| Audit section (`/admin/audit/*`: trace viewer, API-key management) | accounts on `AUDIT_ADMINS`; key management on `AUDIT_KEY_ADMINS` (falls back to `AUDIT_ADMINS`) | the portal's shared audit API key | campus.audit API-key scope |

Surfaces live under the gateway-dispatchable `/admin/<platform>` namespaces
(campus-admin#97): the OAuth login blueprint rides flask_campus's
`login_prefix="/admin"` (`/admin/login`, `/admin/finalize_login`,
`/admin/logout`; campus-admin#95), sessions use the fleet cookie name
`campus_admin_session`, and the old root-level paths (`/`, `/sign-in`,
`/resources/*`, `/audit/*`, and the pre-remap `/admin/*` auth-ops pages)
redirect to their new homes.

- **A grant is worth exactly what it says.** A designated admin's
  calls carry their own grant and token scopes, so they reach the
  vocabularies their grant rows cover and no others: Clients, Users,
  Sessions, Credentials, Vaults and Migrations each follow their own
  row, a `read` grant lists and a `write` grant also changes, and the
  operator-only actions (registering, deleting and rotating clients)
  are offered only to platform operators.
- **Grants are managed on the Grants page.** `/admin/grants` is the
  campus.auth access-grant store — the "who may administer what"
  matrix — with filters, **grant** and **revoke**. A platform operator
  administers every resource's rows. A designated admin administers a
  resource only with `admin` on it (grant row and token scope), which
  upstream's rules leave reachable for `users` alone, and nobody can
  change their own rows: campus.auth enforces both on every call.
  Granting raises a level and never lowers one — revoke, then grant
  again. A client's scope caps (`allowed_scopes`,
  `upstream_scopes`) are registration data, edited with campus-cli
  rather than in the portal.
- **Deleting a user is typed-confirmed.** The Users page's **delete**
  needs `users:admin` — a platform operator, or a designated admin
  holding it — and its button unlocks only once the account's email has
  been typed exactly; the view enforces the same match. It removes the
  account record only: the person's sessions, stored credentials,
  access grants and circle memberships stay, and signing in to that
  deployment again re-creates the account. The account you are signed
  in with is never offered.
- **The Resources section acts as you.** campus.api denies client
  principals on gated routes, so the portal never acts as its own
  client there: what you can read and change is your own circle
  access, and the write controls follow it.
- **The Audit section is one shared key.** campus.audit authorizes by
  API key and never sees who is signed in; the two lists are the
  portal's own decision about who may drive its key. Students may be
  on `AUDIT_ADMINS` on **development only**.
- Answers are cached briefly (about a minute), so grants, circle
  edits and list edits apply within that window; the audit lists are
  re-read per request. The effective gates are logged at startup
  (`access gates: admin section = …, resources section = …, audit
  section = …`).
- Sign-in itself is any `@nyjc.edu.sg` Google Workspace account through
  Campus auth (`flask_campus` OAuth); the services then decide what the
  account may see. A Forbidden page names the signed-in identity and
  the authority that refused.

### Migrations view (`/admin/migrations`, issue #75)

The per-service database migration surface (auth, api, audit) over the
suite's migration management routes (campus#1169): each section shows
the **plan** (pending revisions with their approval metadata — hash,
declared rollback support, docstring summary) and the **ledger
history** (who/when/duration/hash/error), with **apply** (optional
staged *up to* window), **abort** (between migrations), and per-revision
**rollback** actions. The page polls a service while its apply is in
flight and reloads when the run settles.

Authority is the campus#1168 **two-leg model**, enforced by the
services on every call (the buttons are UX, not security):

- **Designation leg** — an access-grant row in campus.auth's grant
  store designating the user for `migrations:read` / `write` /
  `admin` on that service. Production grants are **seeded ahead of a
  migration window**; they are administered via the grant store
  (campus-cli / #883), not this portal. The view probes it
  (operator-readable `grants.check`) and states it plainly: without
  the grant the page says so and offers no enable button, because
  nothing self-serves a designation.
- **Capability leg** — the matching scope on the caller's bearer.
  Sign-in requests only the **structural baseline**
  (`LOGIN_SCOPES_BASE` in `apps/admin/__init__.py`:
  `campus.profile` + `users:read`); the migrations vocabulary is
  acquired on demand through the view's **Enable migrations access**
  step-up (a fast OAuth re-auth requesting exactly that vocabulary —
  no fresh sign-in), and the view renders affordances from the
  session token's scopes.

Deployment prerequisite, narrowed by the same change (the Nov 2026
outage: one cap entry lagging a newly requested scope used to 500
every sign-in at once):

1. The portal client's `allowed_scopes` cap must cover the
   **baseline** (always) and any **feature vocabularies the
   deployment actually uses** — the migrations vocabulary, and every
   management vocabulary its designated admins are expected to use
   (the `/admin/enable` step-up asks for exactly those). An uncovered
   *baseline* scope still breaks every sign-in; an uncovered *feature*
   scope now degrades to a visible UI state — the page explains that
   the cap must be widened, and the nav pill/link renders disabled with
   the reason on hover. Widen caps with campus-cli
   (`campus client update --client-id <id> --allowed-scope <scope>`):
   the portal has no scope-cap editor, and the CLI's scope flags
   replace the whole list, so repeat the flag for every scope to keep.
2. **Audit leg credential**: the audit section of the view calls the
   audit service with `AUDIT_UI_API_KEY` (the /audit UI's key) — the
   audit service authenticates API keys, not campus bearers.

Known limitation (campus#1183): the **api and audit migrations gates
fail closed** until campus.auth surfaces token scopes to services —
their sections 403 even for correctly-granted admins, and the view
renders that as an explicit caveat. The auth section is fully wired
(its gate is the two-leg store check) and works for designated
admins today. On the development deployment migrations auto-apply at
startup, so a healthy section reads everything up to date; the
production deployment is the manual surface.

## Deployment

**Decision (2026-10-01, issue #8): deploy as a Railway service, matching
the rest of the Campus ecosystem.** Local runs remain the supported
development workflow.

- **Why Railway:** the portal is the interface for registering OAuth
  redirect URIs (issue #7), which every Railway-hosted Campus consumer
  needs before campus#651 strict enforcement flips — it must be
  reachable without any particular developer's laptop running. The
  `campus-admin.up.railway.app` project already exists but runs
  pre-refresh code; redeploying the current `main` also resolves #5.
- **Security:** sign-in is Campus OAuth, and every section is then
  authorized by the service it fronts — campus.auth for the Admin
  section (a platform operator or an access-grant holder, acting as
  the operator or as themselves), campus.api for the Resources section
  (the signed-in user's own circle access), and the audit API key plus
  the `AUDIT_ADMINS` list for the Audit section. A deployed instance
  exposes only what those services let the signed-in account do.
- **Deploying:** set the same environment variables from `.env.example`
  in the Railway service (`PUBLIC_URL=https://campus-admin.up.railway.app`,
  and register `https://campus-admin.up.railway.app/finalize_login` as a
  redirect URI on the portal's client). Performing the redeploy needs
  Railway access and is tracked with #5.

For local development, the workflow is: `cp .env.example .env`, fill it
in, `poetry run flask --app apps.admin run`.

## Development

### Refreshing Dependencies

If you make changes to the local `campus-api-python` or `campus-suite` packages:

```bash
./scripts/refresh-dependencies.sh
```

This will update poetry.lock and reinstall dependencies from the latest commits.

### Testing

See [docs/TESTING.md](docs/TESTING.md) for comprehensive testing documentation.

Quick test of OAuth flow:
```bash
poetry run python scripts/test_oauth_flow.py
```

### Browser Automation Setup

For end-to-end testing with Playwright, see [docs/browser-automation.md](docs/browser-automation.md).

## Troubleshooting

### "SECRET_KEY environment variable is required"

Ensure your `.env` file contains a `SECRET_KEY` value. Generate one with:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### OAuth redirect errors

1. Verify `CLIENT_ID` and `CLIENT_SECRET` are correct in `.env`
2. Ensure the OAuth client is configured in Campus auth service
3. Check that redirect URIs include `{PUBLIC_URL}/finalize_login` — for
   local development that is `http://localhost:5000/finalize_login`

### 403 after signing in

The page names the signed-in identity and the authority that refused.
What to change depends on the section:

- **Admin section** — the account is neither a platform operator nor
  an access-grant holder. Give it a row in the campus.auth grant store
  for what it should administer — on the **Grants** page as a platform
  operator, or with campus-cli
  (`campus grant grant --grantee-type user --grantee-id <email>
  --resource <clients|users|sessions|credentials|vault|migrations>
  --level <read|write>`) — or, for the full operator console, `admin`
  on the `ADMIN_CIRCLE` circle (`campus` by default). Grants apply
  within about a minute (short cache); no restart needed.
- **One Admin resource, or "Enable … access"** — a grant holder
  reaches only the vocabularies their grant rows cover, at the level
  granted, and only once their session token carries the scope: click
  **Enable … access** on the page. If the page says the
  `allowed_scopes` cap must be widened instead, widen the portal
  client's cap (see "Migrations view" above).
- **Resources section** — the account holds no campus.api access to
  that resource family. Access follows circle memberships and their
  access values: every staff account reads, and a change needs the
  `admin` access value on the family's grant circle (the service gates
  every mutation on group-scope create).
- **Audit section** — the account is not on the deployment's
  `AUDIT_ADMINS` list (key management: `AUDIT_KEY_ADMINS`).

See [docs/auth-model.md](docs/auth-model.md) for the model behind each.

### Authentication diagnostics

Run the diagnostic script:
```bash
poetry run python scripts/test_auth.py
```

This will test:
- Environment configuration
- Campus client initialization
- Auth service connectivity
- Flask app setup
- Route configuration

## Contributing

1. Follow the existing code style (enforced by Ruff)
2. Test changes with `scripts/test_oauth_flow.py`
3. Update documentation as needed
4. Commit with descriptive messages

## Related Projects

- [campus](https://github.com/nyjc-computing/campus) - Campus backend services
- [campus-api-python](https://github.com/nyjc-computing/campus-api-python) - Python API client

## License

Internal use by NYJC Computing team.
