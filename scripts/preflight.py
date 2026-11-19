#!/usr/bin/env python3
"""Preflight: verify infrastructure access before an ops session.

The 2026-11-19 outage diagnosis burned hours on credential
archaeology — stale env vars shadowing a valid Railway login, rotated
repo `.env` secrets treated as live, a headless Google session that
now hits a password challenge. This script checks the whole access
map mechanically (docs/ops-access.md) and reports what works.

Read-only; makes no configuration changes. Exit code 0 when every
critical check passes, 1 otherwise.

Usage:
    poetry run python scripts/preflight.py            # human-readable
    poetry run python scripts/preflight.py --json     # for agents
    poetry run python scripts/preflight.py \
        --auth-base-url https://campusauth.up.railway.app   # retarget
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Stale variables that shadow the valid stored Railway login
# (docs/ops-access.md). Their presence is itself a finding.
SHADOWING_RAILWAY_VARS = ("RAILWAY_TOKEN", "RAILWAY_API_TOKEN")

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYWRIGHT_STATE = REPO_ROOT / "scripts" / ".playwright_auth_state.json"
# Google's re-trust lapses after a few weeks: an old state file means
# headless sign-in will likely hit the password challenge.
PLAYWRIGHT_STATE_MAX_AGE_DAYS = 21

DEFAULT_AUTH_BASE_URL = "https://campusauth-development.up.railway.app"


@dataclass
class Check:
    """One access-map probe: ok (True/False) + a human explanation."""

    name: str
    ok: bool
    detail: str
    critical: bool = True
    hints: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "critical": self.critical,
            "hints": self.hints,
        }


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command without the stale shadowing variables.

    Windows shims (npm installs `railway` as a .CMD next to an
    extensionless bash script) need explicit resolution: subprocess
    without a shell does not walk PATHEXT.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in SHADOWING_RAILWAY_VARS}
    exe = shutil.which(cmd[0])
    if exe is None:
        return 127, f"{cmd[0]} not found on PATH (resolved from Python)"
    try:
        if exe.lower().endswith((".cmd", ".bat")):
            # Batch shims require a shell; quote every argument.
            quoted = " ".join([f'"{exe}"'] + [f'"{arg}"' for arg in cmd[1:]])
            result = subprocess.run(
                quoted, capture_output=True, text=True,
                timeout=timeout, env=env, shell=True,
            )
        else:
            result = subprocess.run(
                [exe] + cmd[1:], capture_output=True, text=True,
                timeout=timeout, env=env,
            )
        return result.returncode, (result.stdout + result.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout}s"


def check_shadowing_vars() -> Check:
    present = [v for v in SHADOWING_RAILWAY_VARS if os.environ.get(v)]
    if not present:
        return Check(
            "railway-env-shadowing", True,
            "no stale RAILWAY_* token vars in the environment",
            critical=False,
        )
    return Check(
        "railway-env-shadowing", False,
        f"stale vars set in the environment: {', '.join(present)} — "
        "they shadow the valid stored Railway login; this script "
        "unsets them per-check, but shells/tools that do not may "
        "still see 'Unauthorized'",
        critical=False,
        hints=[
            "remove them from wherever they are exported "
            "(user env vars / shell profile)",
        ],
    )


def check_railway() -> Check:
    code, output = _run(["railway", "whoami"])
    if code == 0 and "Logged in as" in output:
        who = output.strip().splitlines()[0]
        return Check("railway-login", True, who)
    return Check(
        "railway-login", False,
        f"railway whoami failed (exit {code}): {output[:200]}",
        hints=[
            "the stored login lives in ~/.railway/config.json; "
            "`railway login` needs a browser with a Railway session",
        ],
    )


def check_campus_cli() -> Check:
    code, output = _run(["campus", "auth", "status", "--json"])
    if code != 0:
        return Check(
            "campus-cli", False,
            f"campus auth status failed (exit {code}): {output[:200]}",
            hints=["campus auth login runs a device flow"],
        )
    try:
        status = json.loads(output)
    except json.JSONDecodeError:
        return Check("campus-cli", False, f"unparsable status: {output[:200]}")
    if not status.get("authenticated"):
        return Check("campus-cli", False, "no stored campus credentials")
    details = [
        f"principal {status.get('user_id')}",
        f"issuer {status.get('token_auth_url')}",
        f"scopes {','.join(status.get('scopes') or [])}",
        f"expires {status.get('expires_at', '')[:10]}",
    ]
    hints = []
    if not status.get("endpoint_match"):
        details.append(
            f"(CLI targets {status.get('auth_url')} — endpoint MISMATCH: "
            "the stored token was issued elsewhere; run campus auth login "
            "against the environment you need, remembering the slot is "
            "replaced)"
        )
    if status.get("token_validity") != "valid":
        hints.append("stored token failed its server-side validity check; "
                     "try `campus auth refresh`")
    return Check(
        "campus-cli", status.get("token_validity") == "valid",
        "; ".join(details), hints=hints,
    )


def check_dotenv_client(auth_base_url: str) -> Check:
    """Probe the local .env client credentials against an auth service.

    The client reading its own record is the cheapest authenticated
    call; "Invalid credentials" = rotated secret. This is the check
    that exposes the repo-.env staleness documented in
    docs/ops-access.md.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return Check(
            "dotenv-client-creds", False,
            "no .env file in the repo root",
            critical=False,
        )
    client_id = secret = None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("CLIENT_ID="):
            client_id = line.split("=", 1)[1].strip()
        elif line.startswith("CLIENT_SECRET="):
            secret = line.split("=", 1)[1].strip()
    if not client_id or not secret:
        return Check(
            "dotenv-client-creds", False,
            ".env exists but CLIENT_ID/CLIENT_SECRET are not both set",
            critical=False,
        )

    os.environ["CLIENT_ID"] = client_id
    os.environ["CLIENT_SECRET"] = secret
    os.environ["BASE_URL"] = auth_base_url
    for var in ("CAMPUS_AUTH_URL", "CAMPUS_API_URL", "AUTH_URL", "API_URL"):
        os.environ.pop(var, None)
    try:
        import campus_python

        client = campus_python.Campus(timeout=30).auth.clients[client_id].get()
    except Exception as exc:  # noqa: BLE001 - the check IS the probe
        name = type(exc).__name__
        return Check(
            "dotenv-client-creds", False,
            f"{client_id} against {auth_base_url}: {name} "
            f"({str(exc)[:120]}) — treat the .env secret as rotated",
            critical=False,
            hints=["authoritative secrets live in the Railway service "
                   "variables"],
        )
    return Check(
        "dotenv-client-creds", True,
        f"{client_id} ({getattr(client, 'name', '?')}) authenticated "
        f"against {auth_base_url}",
        critical=False,
    )


def check_playwright_state() -> Check:
    if not PLAYWRIGHT_STATE.exists():
        return Check(
            "playwright-google-state", False,
            "no saved Google session state "
            "(headless sign-in cannot complete)",
            critical=False,
        )
    age_days = (time.time() - PLAYWRIGHT_STATE.stat().st_mtime) / 86400
    if age_days > PLAYWRIGHT_STATE_MAX_AGE_DAYS:
        return Check(
            "playwright-google-state", False,
            f"saved Google session is {age_days:.0f} days old — Google's "
            "re-trust has lapsed and headless sign-in hits the password "
            "challenge; refresh it via a real sign-in, or use the "
            "browser holding live sessions",
            critical=False,
        )
    return Check(
        "playwright-google-state", True,
        f"saved Google session state is {age_days:.0f} days old "
        "(recent mtime does not guarantee Google skips the password "
        "challenge — probe before relying on headless sign-in)",
        critical=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output",
    )
    parser.add_argument(
        "--auth-base-url", default=DEFAULT_AUTH_BASE_URL,
        help="auth service origin the .env credentials are probed against",
    )
    args = parser.parse_args()

    checks = [
        check_shadowing_vars(),
        check_railway(),
        check_campus_cli(),
        check_dotenv_client(args.auth_base_url),
        check_playwright_state(),
    ]

    if args.json:
        print(json.dumps(
            {"checks": [c.as_dict() for c in checks],
             "ok": all(c.ok for c in checks if c.critical)},
            indent=2,
        ))
    else:
        print("Preflight (docs/ops-access.md)\n" + "=" * 30)
        for check in checks:
            flag = "PASS" if check.ok else "FAIL"
            criticality = "" if check.critical else " (advisory)"
            print(f"[{flag}]{criticality} {check.name}: {check.detail}")
            for hint in check.hints:
                print(f"       hint: {hint}")
        print("=" * 30)

    return 0 if all(c.ok for c in checks if c.critical) else 1


if __name__ == "__main__":
    sys.exit(main())
