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
   At minimum, set `SECRET_KEY`, `ADMINS`, `CLIENT_ID`, `CLIENT_SECRET`,
   and `PUBLIC_URL`.

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
│       ├── __init__.py     # App factory, all routes, templates/static
│       └── templates/      # Jinja templates
├── scripts/                # Utility and test scripts
│   ├── test_auth.py           # Authentication diagnostics
│   ├── test_oauth_flow.py     # End-to-end OAuth testing
│   └── refresh-dependencies.sh # Dependency refresh utility
├── docs/                   # Documentation
│   ├── browser-automation.md  # Playwright setup guide
│   └── TESTING.md          # Testing guide
├── .env.example            # Environment template (copy to .env)
├── .github/workflows/ci.yml # Ruff + smoke tests (Python 3.11–3.13)
├── pyproject.toml          # Poetry dependencies
└── README.md               # This file
```

## Authentication Flow

1. User visits `/login?next=/dashboard`
2. Application redirects to Campus auth service with OAuth parameters
3. Campus auth redirects to Google Workspace sign-in
4. User authenticates with Google (nyjc.edu.sg account)
5. Google redirects back to Campus auth with authorization code
6. Campus auth creates session and redirects to `{PUBLIC_URL}/finalize_login`
7. Application validates session and redirects to requested page (`/dashboard`)

## Environment Variables

Copy `.env.example` to `.env` and fill in the values. The app reads
`SECRET_KEY`, `ADMINS`, and `PUBLIC_URL` directly; `CLIENT_ID`,
`CLIENT_SECRET`, `ENV`/`CAMPUS_ENV`, and `CAMPUS_AUTH_URL`/`CAMPUS_API_URL`
are read inside campus-python.

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | Yes | Flask session signing key. The app refuses to start without it. |
| `ADMINS` | Yes | Semicolon-separated campus user ids allowed to use the admin routes (`/clients`, `/users`). A campus user id **is the user's full email address** (e.g. `jane_doe@nyjc.edu.sg`); signed-in users not listed get 403. |
| `CLIENT_ID` | Yes | OAuth client ID of this portal, from the Campus auth service (read by campus-python in "server" mode). |
| `CLIENT_SECRET` | Yes | OAuth client secret for `CLIENT_ID`. |
| `PUBLIC_URL` | Recommended | Public origin of this portal. The OAuth callback is `{PUBLIC_URL}/finalize_login`, and that exact URI must be registered as a redirect URI on the client. Falls back to `https://{HOSTNAME}` (deprecated). |
| `ENV` | No | Campus environment tier: `development` (default), `staging`, or `production`. Determines the auth/API service URLs campus-python uses. `CAMPUS_ENV` is accepted as an alias. |
| `CAMPUS_AUTH_URL` | No | Explicit Campus auth service base URL; takes precedence over the `ENV` tier. Set this for non-standard deployments. |
| `CAMPUS_API_URL` | No | Explicit Campus API service base URL; same precedence rules. |
| `HOSTNAME`, `DEPLOY` | — | Deprecated, ignored for URL resolution since campus-api-python#53 (a `DeprecationWarning` is emitted if they would previously have been used). |

Service URL resolution order (campus-python): explicit `CAMPUS_AUTH_URL` /
`CAMPUS_API_URL`, then the `ENV` tier — `development` → Railway dev
deployments, `staging` → `{service}.campus.nyjc.dev`, `production` →
`{service}.campus.nyjc.app`.

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
- **Security:** every admin route is gated by Campus OAuth sign-in plus
  the `ADMINS` allowlist, so a deployed instance exposes admin
  functionality only to listed emails.
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

### 403 on `/clients` or `/users` after signing in

The signed-in user is not listed in `ADMINS`. Entries are campus user
ids, which are full email addresses (e.g.
`jane_doe@nyjc.edu.sg`), separated by semicolons. Remember to restart
`flask run` after changing `.env`.

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
