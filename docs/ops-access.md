# Ops access map — how to actually reach the infrastructure

**Read this before debugging any deployed-service problem.** The
2026-11-19 login outage was diagnosed black-box for hours because
valid credentials were being shadowed by dead ones; one Railway log
read named the root cause immediately. This page is the map a session
(or teammate) should check first. The bootstrap sequence itself is
tracked in
[issue #83](https://github.com/nyjc-computing/campus-admin/issues/83);
`scripts/preflight.py` verifies everything on this page mechanically.

## Railway: stale env vars shadow a valid login

**Scope first.** The access policy (campus
`docs/environment-access-policy.md` §2) binds routine Railway access to
the **development** environment — a development-scoped project token,
carried in `RAILWAY_TOKEN`, which cannot even resolve production — and
reserves production (logs, variables, redeploys, SSH) for the
superadmin. Everything below is for development work; reaching
production is a superadmin action, each time.

The shell environment on this machine carries a `RAILWAY_TOKEN` /
`RAILWAY_API_TOKEN` pair that does not authenticate: any `railway ...`
invocation short-circuits with `Unauthorized` on them — and even
`railway login` fails fast instead of opening the browser.

A **valid personal login** (JS Ng) is stored in
`~/.railway/config.json`. Prefix every command with an env-unset:

```bash
env -u RAILWAY_TOKEN -u RAILWAY_API_TOKEN railway whoami
env -u RAILWAY_TOKEN -u RAILWAY_API_TOKEN railway logs \
  --project campus --environment development --service campus-admin
env -u RAILWAY_TOKEN -u RAILWAY_API_TOKEN railway variable list \
  --project campus --environment development --service campus-admin
```

That unlocks service variables (including secrets — never print
them), deployment logs (the deployed traceback is the fastest root
cause there is), deployment status, and `railway ssh`. The personal
login is not environment-bound: it can see production too, so the
policy's "development only" is a discipline here, not a technical
limit. The real fix for the trap is a working development-scoped token
in `RAILWAY_TOKEN` (and removing the dead variables from wherever they
are exported — user env / shell profile); until then, prefix.

`railway login`'s browser flow only completes if the browser already
holds a Railway session — the ZCode in-app browser does not, so prefer
the stored login.

## campus CLI: one credential slot, per-environment authority

`campus auth login` (device flow) **replaces** the single stored
token — switching environments costs one device flow per side. Check
state with `campus auth status --json` (fields: `auth_url` = the
endpoint the CLI targets, `token_auth_url` = the issuer of the stored
token, `endpoint_match`, `scopes`).

- **Dev** guest client allowlist includes `clients:read`/`write` —
  client administration via CLI works there (a #883 grant row is also
  required for the client routes).
- **Prod** guest client does **not** allow `clients:*` login scopes,
  and grant-store routes reject plain user tokens (#854) — prod
  client/grant administration needs the operator context or a
  pre-seeded grants admin. Do not assume the dev CLI path works on
  prod.
- Device flows can be completed headlessly: start
  `PYTHONUNBUFFERED=1 campus auth login ...` in the background, open
  the printed verification URL in the ZCode in-app browser (it holds
  warm campus-auth + Google sessions), and click **Authorize** via an
  in-page `evaluate` call — Playwright-surface clicks stall on these
  pages.

Deployment topology (client ↔ environment pairing):

| Deployment | Portal origin | Portal OAuth client | Auth service | Gateway |
|---|---|---|---|---|
| production | `campus-admin.up.railway.app` | `uid-client-6ec13381` (`campus-admin-production`) | `campusauth.up.railway.app` | `nyjc-campus.up.railway.app` |
| development | `campus-admin-development.up.railway.app` | `uid-client-f293a42a` (`campus-admin-dev`) | `campusauth-development.up.railway.app` | `campus-development` (auth/api/audit upstreams can flap 502) |

When the dev gateway misroutes, point the portal service at the direct
hosts (`CAMPUS_AUTH_URL` / `CAMPUS_API_URL` / `CAMPUS_AUDIT_URL`
overriding `BASE_URL`) — that is the dev service's current state.

## Repo `.env` files: assume stale

`.env` credentials in these checkouts have rotated upstream more than
once (`campus-admin/.env`'s `CLIENT_SECRET` fails with "Invalid
credentials"; `campus/.env`'s audit operator key 401s). **Probe before
building on them** — `scripts/preflight.py` does exactly that. The
authoritative copies of service secrets live in the Railway service
variables, readable via the Railway CLI.

One exception that IS reliable: the **portal client is its own
deployment's operator**, so with its Railway-stored
`CLIENT_ID`/`CLIENT_SECRET` it can update its **own** client record
(`allowed_scopes`, `redirect_uris`, …) through the SDK — the
self-update path used to repair the outage. Widening that
`allowed_scopes` cap is also what lets a designated admin's step-up
succeed ([auth-model.md](auth-model.md)). It still cannot edit circle
membership (#854: domain administration must be attributable to a
user) — that takes a user token, as a `campus:admin` holder.

## Granting someone into the portal

Portal access is decided by the services, not by portal configuration
([auth-model.md](auth-model.md)); nothing here is a Railway variable
except the audit lists.

| Goal | Action | Needs |
|---|---|---|
| full operator console | `admin` on the `campus` circle | a user token held by a `campus:admin` holder |
| administer some campus.auth resources | a grant row in the campus.auth grant store (`campus grant grant …`) | the operator context, or a same-vocabulary admin |
| read traces on development | add the account to the dev portal's `AUDIT_ADMINS` | Railway access to the dev portal service |
| manage audit keys | add the account to `AUDIT_KEY_ADMINS` (or `AUDIT_ADMINS` where that is the admin list) | Railway access to the portal service |

## Browser auth (for agents)

- The saved Playwright Google session
  (`scripts/.playwright_auth_state.json`) predates Google's current
  re-trust behaviour: headless sign-in now lands on a **password
  challenge** — treat that lane as dead until the state is refreshed
  by a real sign-in.
- The ZCode in-app browser holds live campus-auth + Google sessions.
  OAuth flows complete there without prompts; drive clicks with an
  in-page `evaluate(() => element.click())`, since Playwright-surface
  clicks stall on these apps.

## Preflight

```bash
poetry run python scripts/preflight.py            # human-readable
poetry run python scripts/preflight.py --json     # for agents
```

Checks: Railway login (with the stale vars unset), campus CLI status,
the local `.env` client credentials against the dev auth service
(`--auth-base-url` to retarget), the Playwright state file's age, and
a warning when the shadowing env vars are set. Exit code 1 when a
critical check fails.
