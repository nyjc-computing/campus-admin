# Testing Guide

This guide covers testing tools and workflows for the Campus Admin Portal.

## Test Scripts Overview

The project includes two test scripts located in the `scripts/` directory:

1. **[test_oauth_flow.py](../scripts/test_oauth_flow.py)** - End-to-end OAuth flow testing with Playwright
2. **[test_auth.py](../scripts/test_auth.py)** - Comprehensive authentication diagnostics

## 1. End-to-End OAuth Flow Testing

**Script:** `test_oauth_flow.py`
**Purpose:** Tests the complete OAuth authentication flow from login to dashboard access using browser automation.

### Features

- Automated browser testing with Playwright
- Session persistence across test runs
- Manual Google sign-in on first run, then automated
- Follows complete redirect chain
- Gathers Railway logs for debugging

### Prerequisites

Install Playwright and browser binaries:
```bash
poetry install --with dev
poetry run playwright install chromium
```

See [browser-automation.md](browser-automation.md) for detailed Playwright setup instructions.

### Usage

**First run** (requires manual Google sign-in):
```bash
python scripts/test_oauth_flow.py
```

This will:
1. Open a browser window
2. Navigate to `/login?next=/dashboard`
3. Wait for you to sign in with Google
4. Save your session for future runs
5. Verify you reach the dashboard

**Subsequent runs** (automated):
```bash
python scripts/test_oauth_flow.py
```

Once you've signed in once, the script will:
1. Load your saved Google session
2. Run in headless mode (no visible browser)
3. Automatically complete the OAuth flow
4. Verify dashboard access

### Command-Line Options

```bash
# Clear saved session and re-authenticate
python scripts/test_oauth_flow.py --reset

# Force headless mode (even without saved session)
python scripts/test_oauth_flow.py --headless

# Combine options
python scripts/test_oauth_flow.py --reset --headless
```

### Session Storage

The script saves your Google authentication state in:
```
scripts/.playwright_auth_state.json
```

This file contains cookies and local storage from your browser session. **Never commit this file to git** (it's already in .gitignore).

### What It Tests

1. Initial redirect from `/login` to Campus auth
2. Campus auth session creation
3. Redirect to Google OAuth
4. Google authentication (manual on first run)
5. OAuth callback handling
6. Session validation
7. Final redirect to dashboard

### Troubleshooting

**"Saved session seems expired or invalid"**
- Run with `--reset` to clear the session and sign in again

**"Cannot complete Google sign-in in headless mode"**
- Don't use `--headless` on first run
- Authenticate once with visible browser first

**Stuck in redirect loop**
- Check that your OAuth client is properly configured
- Verify CLIENT_ID and CLIENT_SECRET in `.env`
- Run `test_auth.py` for diagnostics

## 2. Authentication Diagnostics

**Script:** `test_auth.py`
**Purpose:** Comprehensive diagnostic tool for troubleshooting authentication setup.

### Usage

```bash
poetry run python scripts/test_auth.py
```

### What It Tests

The script runs five diagnostic checks:

1. **Environment Variables**
   - Verifies `ENV`, `CLIENT_ID`, `CLIENT_SECRET` are set
   - Shows which environment will be used

2. **Campus Client Initialization**
   - Tests that `campus_python.Campus()` initializes correctly
   - Displays the auth service base URL being used

3. **Auth Service Connectivity**
   - Attempts to connect to the Campus auth service
   - Tests the health check at the root URL (`/`)
   - Verifies network connectivity

4. **Flask App Initialization**
   - Tests that `create_app()` works
   - Lists registered blueprints
   - Verifies app configuration

5. **Auth Routes**
   - Tests the `/login` endpoint
   - Checks for proper redirect behavior
   - Validates OAuth flow initialization

### Example Output

```
================================================================================
ENVIRONMENT DIAGNOSTICS
================================================================================

ENV: development
CAMPUS_ENV: NOT SET
PORT: NOT SET
CLIENT_ID: uid-client-f293a42a
CLIENT_SECRET: SET
DEPLOY: NOT SET

================================================================================
TESTING CAMPUS CLIENT INITIALIZATION
================================================================================

✓ Campus client initialized successfully
✓ Auth service client created
  Base URL: https://campusauth-development.up.railway.app

================================================================================
TESTING AUTH SERVICE CONNECTION
================================================================================

Environment detected: development
Testing connection to: https://campusauth-development.up.railway.app

✓ Health check (GET /): 200
  Body: {"deployment":"campus.auth","environment":"development","status":"healthy"}

... (and so on)
```

### When to Use

Run this script when:
- Setting up the project for the first time
- Troubleshooting authentication issues
- Verifying environment configuration
- Debugging connection problems
- After changing OAuth credentials

## Testing Workflow

### Initial Setup

1. Configure environment variables in `.env`
2. Run diagnostics:
   ```bash
   poetry run python scripts/test_auth.py
   ```
3. Test OAuth flow (first time with visible browser):
   ```bash
   python scripts/test_oauth_flow.py
   ```

### Regular Testing

After making changes to authentication code:

1. Run diagnostics to verify configuration:
   ```bash
   poetry run python scripts/test_auth.py
   ```

2. Test OAuth flow (automated with saved session):
   ```bash
   python scripts/test_oauth_flow.py
   ```

### CI/CD Integration

For automated testing in CI/CD pipelines:

```bash
# Run diagnostics (no browser required)
poetry run python scripts/test_auth.py

# For OAuth flow testing in CI, you'll need to:
# 1. Use a test account
# 2. Save auth state in CI environment
# 3. Or mock the OAuth provider
```

Note: Full OAuth flow testing in CI is complex due to Google sign-in requirements. Consider using mock authentication or dedicated test accounts.

## Common Issues and Solutions

### Issue: "CLIENT_ID or CLIENT_SECRET not found"

**Solution:** Check your `.env` file contains:
```bash
CLIENT_ID=your-client-id
CLIENT_SECRET=your-client-secret
```

### Issue: "Connection failed" to auth service

**Solutions:**
1. Check your internet connection
2. Verify the `ENV` variable is set correctly
3. Ensure the auth service is running (for local testing)
4. Check Railway deployment status (for development/staging)

### Issue: OAuth flow redirects to login repeatedly

**Solutions:**
1. Clear browser session: `python scripts/test_oauth_flow.py --reset`
2. Verify OAuth client configuration in Campus auth
3. Check redirect URI is `http://localhost:5000/auth/callback`
4. Run `test_auth.py` for detailed diagnostics

### Issue: "Executable doesn't exist" (Playwright)

**Solution:** Install Chromium browser:
```bash
poetry run playwright install chromium
```

See [browser-automation.md](browser-automation.md) for details.

## Further Reading

- [Browser Automation Setup](browser-automation.md) - Playwright installation and configuration
- [Main README](../README.md) - Project overview and setup
- [Playwright Python Docs](https://playwright.dev/python/) - Official Playwright documentation
