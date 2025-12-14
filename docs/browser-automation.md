# Browser Automation Setup

This guide covers setting up Playwright for automated browser testing in the campus-admin project.

## What is Playwright?

Playwright is a browser automation library that allows you to programmatically control Chromium, Firefox, and WebKit browsers. We use it to test the OAuth login flow end-to-end, including interaction with Google's sign-in page.

## Installation

### Step 1: Install Python Dependencies

The Playwright Python package is listed in the `dev` dependency group. Install it using Poetry:

```bash
poetry install --with dev
```

This installs the `playwright` Python package (~5MB) into your virtual environment.

### Step 2: Install Browser Binaries

Playwright requires browser binaries to run. We use Chromium for testing.

**All Platforms:**
```bash
poetry run playwright install chromium
```

This downloads and installs Chromium (~300MB) to your system.

### Step 3: Install System Dependencies

Playwright's browsers require certain system libraries to run.

#### Linux (Debian/Ubuntu)

```bash
# Automatic installation (recommended)
poetry run playwright install-deps chromium

# Or manual installation
sudo apt-get update
sudo apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2
```

#### macOS

No additional system dependencies required. The browser should work out of the box after Step 2.

#### Windows

No additional system dependencies required. The browser should work out of the box after Step 2.

## Verification

Verify the installation by running:

```bash
poetry run playwright --version
```

You should see output like `Version 1.57.0`.

## Browser Storage Location

By default, browser binaries are stored in:

- **Linux/macOS:** `~/.cache/ms-playwright/`
- **Windows:** `%USERPROFILE%\AppData\Local\ms-playwright\`

### Custom Installation Location (Optional)

If you prefer to install browsers elsewhere (e.g., in `/tmp` for ephemeral storage):

```bash
export PLAYWRIGHT_BROWSERS_PATH=/path/to/custom/location
poetry run playwright install chromium
```

Add the `export` line to your shell profile (`.bashrc`, `.zshrc`, etc.) to make it permanent.

## Disk Space Requirements

- Python package: ~5MB
- Chromium browser: ~300MB
- System dependencies (Linux only): ~50MB
- **Total:** ~355MB (Linux), ~305MB (macOS/Windows)

## Running Tests

After installation, you can run browser automation scripts:

```bash
python scripts/test_oauth_flow.py
```

See the script itself for usage details.

## Cleanup

### Uninstall Browser Binaries

To remove the Chromium browser:

```bash
poetry run playwright uninstall chromium
```

To remove all browsers:

```bash
poetry run playwright uninstall --all
```

**Note:** Uninstalling the Python package (`poetry remove playwright`) does NOT automatically remove browser binaries. You must run the commands above to free up disk space.

### Remove Python Package

To remove Playwright from the dev dependencies:

```bash
poetry remove --group dev playwright
```

## Troubleshooting

### "Executable doesn't exist" Error

If you see an error like:
```
playwright._impl._errors.Error: Executable doesn't exist at /path/to/chromium
```

Run:
```bash
poetry run playwright install chromium
```

### Browser Crashes on Linux

If the browser crashes immediately on Linux, you may be missing system dependencies:

```bash
poetry run playwright install-deps chromium
```

### Headless Mode Issues

If you encounter issues in headless mode, try running with a visible browser window for debugging:

```python
# In your script
browser = playwright.chromium.launch(headless=False)
```

### Permission Errors (Linux)

If you get permission errors installing system dependencies:

```bash
sudo poetry run playwright install-deps chromium
```

## Further Reading

- [Playwright Python Documentation](https://playwright.dev/python/)
- [Playwright System Requirements](https://playwright.dev/python/docs/browsers#system-requirements)
