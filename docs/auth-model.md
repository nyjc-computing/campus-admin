# Access model — who may do what in the portal

The portal is a UI over three services. Each service owns its own
authorization, and the portal takes every access decision from the
service that owns it — there is no parallel allowlist for anything a
service already decides. This page states the model and how the portal
implements it; `apps/admin/authz.py` is the code that answers the
questions, and `tests/test_auth_model.py` pins the behaviour.

## The model

| Service | What it holds | Who may act | Enforced by |
|---|---|---|---|
| **campus.audit** | request traces, grouped into traces and journeys | whoever holds an audit API key with the right scopes | API-key scope on every call |
| **campus.auth** | authentication and authorization data that is admin-edited: clients, users, vaults, sessions, credentials | the operator; designated admins, by access grant | the access-grant store, on every call |
| **campus.api** | API resources, with permissions scoped hierarchically | users, according to their own circle access | circles, on every call |

**campus.audit** authorizes by API key and does not authenticate
anyone. **campus.auth** is admin-edited only: a regular user never acts
on it as a principal — the operator handles their sign-in on their
behalf — and an admin's authority is a *grant* that fixes what they may
do (read / write / admin) to each kind of auth resource. **campus.api**
authorizes through a tree of circles: a circle's membership in another
circle carries an access value, which fixes the member's permission
over the group's resources and over its own.

Two consequences the portal has to respect:

- campus.auth never consults circles, campus.api never consults the
  access-grant store, and campus.audit never consults either. A grant
  confers no campus.api authority, and a circle membership confers no
  campus.auth authority.
- Client principals hold no authority where accountability matters:
  campus.api denies them on gated routes (domain administration must be
  attributable to a user), and campus.auth reserves its most powerful
  actions for the operator and for users.

Reference: campus `docs/circles-access-control.md` (ratified
2026-11-23) and `docs/auth-token-invariants.md` (A8/A9/A10).

## How the portal implements it

Sign-in is the portal's own campus.auth OAuth login. The portal's OAuth
client is also its deployment's *operator* on campus.auth (its id is
listed in the auth service's `AUTH_OPERATOR_CLIENT_IDS`), which is what
lets it run the sign-in flow on a user's behalf.

### Admin section — campus.auth authority

**Who may enter.** A signed-in user holding either

- **platform-operator status** — `admin` on the operator circle
  (`ADMIN_CIRCLE`, default `campus`, the platform wildcard circle the
  campus.api bootstrap seeds); or
- **at least one access grant** in the campus.auth grant store — a
  *designated admin*.

This is the 2026-11-17 ruling (campus#1129, campus-admin#72): any
account holding one or more access grants may use the portal. Someone
holding neither gets a 403 page saying so; if neither authority can be
reached, the gate refuses with 503 rather than guessing.

**How they act.** Each view runs in one of two lanes, and the wire
principal always matches the authority that admitted the user:

| Lane | Admitted by | Wire principal | Reach |
|---|---|---|---|
| Operator | platform-operator status | the portal's operator client | everything the operator may do |
| Designated | an access grant | the signed-in user's own campus token | exactly their grants |

A designated admin's calls carry *their* grant and token scopes, so
campus.auth authorizes every one against the two-leg model (a grant row
**and** the matching scope on the token) and attributes the action to
them. The portal only withholds what campus.auth would refuse:

- **Per vocabulary.** Clients, Users, Sessions, Credentials, Vaults and
  Migrations each follow their own grant row. Without one the page is a
  403 naming the missing grant; the sidebar link is disabled with the
  reason.
- **Per level.** A `read` grant lists; a `write` grant also changes.
  A read-level admin sees no write controls, and a write post is
  refused before any call. Vault rows are per label, so a designated
  admin opens only the labels they hold.
- **Operator-only actions are not offered.** Registering, deleting and
  rotating clients are `clients:admin` — operator-equivalent upstream,
  which rejects grant rows for them — so they appear only in the
  operator lane. A client's scope caps are the same `clients:admin`
  upstream; the portal does not edit them at all, campus-cli does.
- **The token must carry the scope.** Sign-in requests only the
  structural baseline (`campus.profile`, `users:read`), so a designated
  admin's session starts without the management vocabularies. The page
  then offers **Enable … access**: one re-authentication (`/admin/enable`)
  that requests exactly the scopes their grants cover, capped by the
  portal client's `allowed_scopes`.

Platform operators get the operator console: every control, every
vocabulary, as before. Because the operator client is a shared secret,
upstream attributes those actions to the portal's client rather than to
the person; the portal's own audit spans (when tracing is enabled)
carry the signed-in user.

**Administering the store itself.** The Grants page (`/admin/grants`)
is the access-grant store — the "who may administer what" matrix — and
follows the store's own gate (`/grants/` in campus.auth), not the
per-vocabulary one:

| Who | May administer |
|---|---|
| a platform operator (operator lane) | every resource's rows, for user and client grantees |
| a designated admin holding `admin` on a resource — grant row and token scope | that resource's rows only |

Upstream's shape rules leave `admin` reachable for `users` alone:
clients, sessions and credentials admin rows are rejected as
operator-equivalent or inert, migrations has no vocabulary-level rows,
and vault grant administration is operator-only (#889). A designated
`users:admin` therefore sees and manages `users` rows alone. Nobody
administers their own rows — campus.auth refuses it (#883) — so the
page lists a user's own rows without controls. The sidebar link
disables with the reason for a designated admin without an
`admin`-level `users` row; one whose token lacks the scope gets the
step-up offer.

A row is either a user's designation — a level over a resource, one
vault label or one service's migrations — or a client's access to a
vault label, as CRUD bitflags (what the Clients page's Access modal
shows). **grant** creates a row or raises it (a level never lowers:
revoke, then grant again); **revoke** removes a row, or single bits of
a client's vault access. The form offers exactly the shapes the store
accepts — no `admin` row for clients, sessions or credentials, a
vault row names its label, a migrations row names its service — and
campus.auth re-checks each on the wire.

### Resources section — campus.api authority

Every call rides the signed-in user's own campus session — the section
has no operator lane, because campus.api denies client principals on
gated routes. What a user can read and change is their circle access:

- **Reading a family** (Circles, Venues, Timetable, Assignments,
  Submissions, Subj Combi, Files, Hosted Apps) needs its group-scope
  read. Every staff account has that as a baseline (`staff:viewer`).
  A student's baseline (`student:guest`) covers only their own
  resources, so the section stays disabled for them unless a circle
  membership grants access to some family.
- **Changing a family's resources** needs its group-scope create. A
  read-only user sees no write controls, and a write post is refused
  before any call.

The portal reads each family's answer from campus.api's gate-equivalent
probe (`GET /circles/{id}/access/{member}`), so a page never offers
what the service would refuse. Access values are the five ratified
constants — `admin` 126, `editor` 54, `viewer` 18, `self` 14,
`guest` 2 — and the member forms offer only those.

### Audit section — campus.audit authority

The section is authorized by the audit service's API key alone; the
service never sees who is signed in. The portal holds one shared UI key
(`AUDIT_UI_API_KEY`) and needs its own answer to "who may drive it",
which it takes from two deployment lists:

| List | May | Environment |
|---|---|---|
| `AUDIT_ADMINS` | read traces | admin-only on production; may include students on **development** |
| `AUDIT_KEY_ADMINS` | additionally manage API keys (audit superadmins) | admins only, on every environment |

`AUDIT_KEY_ADMINS` falls back to `AUDIT_ADMINS` when unset, so a
deployment whose viewer list is already the admin list needs no second
variable. Key management never opens to students.

## Granting access

| To let someone… | Do this |
|---|---|
| use the full operator console | make them `admin` on the operator circle (`campus` by default) — a user-principal action in campus.api, e.g. `campus circle members add --circle-id <id> --member-id <email> --access 126` |
| administer some auth resources | grant them a row in the campus.auth grant store — on the portal's **Grants** page as a platform operator, or e.g. `campus grant grant --grantee-type user --grantee-id <email> --resource sessions --level write` |
| administer the grants of one resource | give them an `admin`-level row on it (reachable for `users` alone); they then use the Grants page as themselves |
| use a vocabulary after being granted | nothing to configure: they click **Enable … access** once per session |
| manage a campus.api resource family | give them a circle membership carrying the access value they need |
| read traces on development | add them to the dev portal's `AUDIT_ADMINS` |

Two deployment prerequisites for designated admins:

1. The portal client's `allowed_scopes` cap must cover each management
   vocabulary they are expected to use — the step-up cannot request
   what the cap excludes, and the page then says the cap must be
   widened. Widen it with campus-cli (`campus client update`); the
   portal has no scope-cap editor.
2. Set `AUDIT_KEY_ADMINS` wherever `AUDIT_ADMINS` is wider than the
   admin list.

Changes apply within about a minute: every answer is cached briefly
(process-local), and a step-up's wider token applies on the next
request.

## What each piece answers

| Question | Answered by | Used by |
|---|---|---|
| May this user enter the Admin section? | `authz.admin_access` | the `admin_required` gate, the Admin pill |
| May they act on one vocabulary, at a level? | `authz.vocabulary_denial` | the resource views, the sidebar, `can_write()` |
| May they administer access grants, and for which resources? | `authz.grant_admin_denial`, `authz.admin_access(...)["grant_admin"]` | the Grants page, the sidebar |
| Which lane does this view run in? | `authz.admin_lane` | the resource views' upstream calls |
| Which scopes can a step-up add? | `authz.admin_access(...)["stepup"]` | `/admin/enable`, the access page |
| May this user read or change a campus.api family? | `authz.api_access`, `authz.family_denial` | the Resources views, `can()`, the sidebar |
| May they read traces / manage keys? | `audit.gate.can_view_traces` / `can_manage_keys` | the audit pages and data endpoints |

Every answer fails closed with a visible reason: a probe that cannot
answer disables the affordance ("check unavailable") and makes the gate
refuse with 503. None of it is the security boundary — the services
enforce on every call — but the portal never claims an affordance a
service would refuse.

## Deliberately not the portal's decision

- **Who the super-admin root is.** campus.auth's `AUTH_SUPER_ADMIN`
  account holds full authority with no grant rows. The portal cannot
  see that setting, so the root enters the Admin section by holding a
  grant (it may grant itself) or platform-operator status — and is
  admitted to the Grants page on the same terms as any designated
  admin, although campus.auth would serve the root unconditionally.
- **Production data.** The access policy reserves production for the
  superadmin; nothing here changes who may touch production
  deployments (campus `docs/environment-access-policy.md`).
