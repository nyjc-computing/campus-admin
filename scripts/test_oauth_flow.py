#!/usr/bin/env python3
"""
Test the OAuth login flow using Playwright.

On first run: Opens a browser for you to manually sign in with Google.
After signing in, the session is saved for future automated runs.

On subsequent runs: Reuses the saved Google session to test the full flow automatically.

Usage:
    python scripts/test_oauth_flow.py              # Run the OAuth test
    python scripts/test_oauth_flow.py --reset      # Clear saved session
"""

import argparse
import contextlib
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# Configuration
BASE_URL = "http://localhost:5000"
AUTH_STATE_FILE = Path(__file__).parent / ".playwright_auth_state.json"
TIMEOUT = 30000  # 30 seconds


def gather_railway_logs() -> str:
    """Gather Railway logs for debugging."""
    print("\n" + "=" * 80)
    print("GATHERING RAILWAY LOGS")
    print("=" * 80)

    try:
        result = subprocess.run(
            ["railway", "logs", "--lines", "100"],
            cwd="/workspaces/nyjc-computing/campus",
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            return result.stdout
        else:
            return f"Error gathering logs: {result.stderr}"
    except FileNotFoundError:
        return "Railway CLI not found. Install it to gather logs."
    except subprocess.TimeoutExpired:
        return "Timeout gathering Railway logs."
    except Exception as e:
        return f"Error gathering logs: {e}"


def test_oauth_with_saved_session(headless: bool = True) -> bool:
    """
    Test OAuth flow using saved browser session.

    Args:
        headless: Whether to run browser in headless mode

    Returns:
        True if flow succeeded, False otherwise
    """
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=headless)

        # Create context with saved auth state if it exists
        context_kwargs = {}
        if AUTH_STATE_FILE.exists():
            print(f"Loading saved session from {AUTH_STATE_FILE}")
            context_kwargs["storage_state"] = str(AUTH_STATE_FILE)
        else:
            print("No saved session found. You'll need to sign in manually.")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            # Start the OAuth flow
            print(f"\nNavigating to {BASE_URL}/login?next=/dashboard")
            page.goto(f"{BASE_URL}/login?next=/dashboard", timeout=TIMEOUT)

            # Track navigation
            redirect_count = 0
            max_redirects = 20

            while redirect_count < max_redirects:
                current_url = page.url
                print(f"  [{redirect_count}] {current_url}")

                # Check if we've reached the final destination
                if "/dashboard" in current_url:
                    print("\n✅ SUCCESS: Reached dashboard!")

                    # Save auth state for future runs
                    if not AUTH_STATE_FILE.exists():
                        context.storage_state(path=str(AUTH_STATE_FILE))
                        print(f"Saved authentication state to {AUTH_STATE_FILE}")

                    return True

                # Check if we need manual Google sign-in
                if "accounts.google.com" in current_url:
                    if AUTH_STATE_FILE.exists():
                        # We have saved auth but still at Google - might be expired
                        print(
                            "\n⚠️  WARNING: Saved session seems "
                            "expired or invalid"
                        )
                        print("    Waiting at Google sign-in page...")
                        print(
                            "    If running headless, re-run with "
                            "--reset to re-authenticate"
                        )
                    else:
                        # First time - need manual auth
                        print(
                            "\n⏸️  WAITING: Please sign in with your "
                            "Google account in the browser"
                        )
                        print(
                            "    The script will continue automatically "
                            "after you sign in"
                        )

                    # If not headless, wait for navigation away from Google
                    if not headless:
                        try:
                            # Wait for navigation away from Google (up to 5 minutes)
                            page.wait_for_url(
                                lambda url: "accounts.google.com" not in url,
                                timeout=300000
                            )
                            print("    Sign-in detected, continuing...")
                            redirect_count += 1
                            continue
                        except PlaywrightTimeout:
                            print("\n❌ TIMEOUT: Waited 5 minutes for sign-in")
                            return False
                    else:
                        # In headless mode, we can't proceed
                        print(
                            "\n❌ FAILED: Cannot complete Google sign-in "
                            "in headless mode"
                        )
                        print("    Run with --reset to re-authenticate")
                        return False

                # Check for redirect loop (stuck at login)
                if "/login" in current_url and redirect_count > 2:
                    print("\n❌ FAILED: Stuck in redirect loop at login page")
                    return False

                # Wait a bit for any redirects to happen
                with contextlib.suppress(PlaywrightTimeout):
                    page.wait_for_load_state("networkidle", timeout=5000)

                # Check if URL changed
                new_url = page.url
                if new_url == current_url:
                    # No redirect, we're done
                    break

                redirect_count += 1

            # If we got here, we didn't reach dashboard
            print("\n❌ FAILED: Did not reach dashboard")
            print(f"    Final URL: {page.url}")
            return False

        except PlaywrightTimeout as e:
            print(f"\n❌ TIMEOUT: {e}")
            return False
        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            # Close browser
            context.close()
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="Test OAuth flow with Playwright")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear saved session and re-authenticate"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless mode (no visible browser)"
    )
    args = parser.parse_args()

    # Reset auth state if requested
    if args.reset:
        if AUTH_STATE_FILE.exists():
            AUTH_STATE_FILE.unlink()
            print(f"Cleared saved session from {AUTH_STATE_FILE}\n")
        else:
            print("No saved session to clear\n")

    # Determine if we should run headless
    # Default: headless if we have saved auth, visible if we don't
    if args.headless:
        headless = True
    elif AUTH_STATE_FILE.exists():
        headless = True  # We have saved auth, can run headless
    else:
        headless = False  # No saved auth, need visible browser for manual login

    if not headless:
        print("Running with visible browser for authentication")
    else:
        print("Running in headless mode with saved session")

    # Run the test
    success = test_oauth_with_saved_session(headless=headless)

    # Gather Railway logs
    logs = gather_railway_logs()
    print(logs)

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
