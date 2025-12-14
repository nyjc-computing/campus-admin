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
- **Python:** 3.11
- **Package Management:** Poetry
- **Dependencies:**
  - `campus-api-python` - Campus API client library
  - `campus-suite` - Campus backend services
  - `flask` - Web framework
  - `python-dotenv` - Environment configuration

## Setup

### Prerequisites

- Python 3.11
- Poetry
- Access to Campus development environment
- OAuth client credentials (CLIENT_ID and CLIENT_SECRET)

### Installation

1. **Clone the repository** (if not already done):
   ```bash
   cd /workspaces/nyjc-computing/campus-admin
   ```

2. **Install dependencies**:
   ```bash
   poetry install
   ```

3. **Configure environment variables**:

   Create a `.env` file in the project root with the following variables:
   ```bash
   # Flask configuration
   SECRET_KEY=your-secret-key-here

   # Campus OAuth credentials
   CLIENT_ID=your-client-id
   CLIENT_SECRET=your-client-secret

   # Environment (development, staging, or production)
   ENV=development
   ```

### Running the Application

Start the Flask development server:

```bash
poetry run flask --app apps.admin run
```

The application will be available at `http://localhost:5000`

## Project Structure

```
campus-admin/
├── apps/
│   └── admin/              # Main application package
│       ├── __init__.py     # App factory
│       ├── authentication.py  # OAuth routes and login logic
│       └── templates/      # HTML templates
├── scripts/                # Utility and test scripts
│   ├── test_auth.py           # Authentication diagnostics
│   ├── test_oauth_flow.py     # End-to-end OAuth testing
│   └── refresh-dependencies.sh # Dependency refresh utility
├── docs/                   # Documentation
│   ├── browser-automation.md  # Playwright setup guide
│   └── TESTING.md          # Testing guide
├── .env                    # Environment configuration (not in git)
├── pyproject.toml          # Poetry dependencies
└── README.md               # This file
```

## Authentication Flow

1. User visits `/login?next=/dashboard`
2. Application redirects to Campus auth service with OAuth parameters
3. Campus auth redirects to Google Workspace sign-in
4. User authenticates with Google (nyjc.edu.sg account)
5. Google redirects back to Campus auth with authorization code
6. Campus auth creates session and redirects to `/auth/callback`
7. Application validates session and redirects to requested page (`/dashboard`)

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | Yes | Flask session encryption key |
| `CLIENT_ID` | Yes | OAuth client ID from Campus auth |
| `CLIENT_SECRET` | Yes | OAuth client secret from Campus auth |
| `ENV` | No | Environment: `development`, `staging`, or `production` (default: `development`) |

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
3. Check that redirect URIs include `http://localhost:5000/auth/callback`

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
