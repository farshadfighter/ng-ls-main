# Security — NGCorion License Server

How the license server protects licenses and itself. The same controls are
mapped to the AFTA / Common Criteria requirements in
`docs/SECURITY_TARGET.md`.

## Reporting a vulnerability

Report it privately to the NGCorion security contact (see the product's
`SECURITY.md`), not in a public issue. Include the version (`GET /`), what you
did and what happened.

## Licensing

| Control | Where |
|---|---|
| Every licensing answer (activate, validate, heartbeat, offline file) is signed with Ed25519; NGCorion has the public key built in and acts only on a valid signature, with its own nonce inside. | `app/utils/license_token.py` |
| The private signing key exists only in this server's environment (`LICENSE_SIGNING_PRIVATE_KEY`); it is never logged, never returned, never in git or an image. `GET /api/signing-key` returns the public half. | `app/core/config.py`, `app/main.py` |
| One license, one machine: the fingerprint NGCorion computes is bound at the first activation; another machine is refused until an admin releases the binding. | `app/crud.py` |
| Validation needs the license key, the organization token (compared in constant time) and the bound fingerprint. An unknown key and a wrong token get the same answer. | `app/crud.py` |
| Offline license files: signed, bound to one machine, valid at most one year, every issue recorded. | `app/offline.py` |
| Client input is bounded (key ≤ 64, fingerprint 16–128 hex, token ≤ 128, request files ≤ 8 KB). | `app/schemas.py` |

## Admin console

| Control | Where |
|---|---|
| Personal accounts; roles admin / operator / auditor, checked on every call. | `app/core/security.py`, `app/accounts.py` |
| Passwords: ≥ 12 characters, 3 character classes, not the user name, salted scrypt. New and reset passwords must be changed at the next sign-in. | `app/core/passwords.py` |
| 5 failed sign-ins → locked 15 minutes; refusals all look the same and take the same time. | `app/accounts.py` |
| Server-side sessions: ended by sign-out, after 15 minutes idle, 8 hours after sign-in; password change and account disable end them. JWT (HS256, ≥ 256-bit key) with issuer and audience. | `app/core/security.py` |
| Cloudflare Turnstile on sign-in (bots). | `app/routers/admin_auth.py` |
| Login banner before sign-in. | `ADMIN_LOGIN_BANNER` |
| Recovery on the server (`python -m app.cli`), audited. | `app/cli.py` |

## Audit trail

Every security-relevant event — sign-ins and failures, lockouts, account and
license changes, offline files, portal orders, NGCorion activations and
refusals, audit exports, start-up — is a row in `audit_events` with time,
actor, role, address, action, target, outcome and detail. A database trigger
refuses UPDATE and DELETE on it. Readable by auditors and admins (search, CSV
export); also written to the log as JSON lines. Never contains a password,
token or key. (`app/audit.py`)

## Transport and network

* TLS 1.2+ only (nginx; with `docker-compose.acme.yml`, a Let's Encrypt
  certificate for lic.ngcorion.com via Traefik). HSTS, `nosniff`,
  `X-Frame-Options: DENY`, a strict Content-Security-Policy,
  Permissions-Policy, no server version, 64 KB request body limit.
* The API container is never published; only nginx (and Traefik) are. The
  client address is taken from nginx's `X-Real-IP`, never from a
  client-supplied `X-Forwarded-For`.
* Per-address rate limit (60/min, Redis), atomic; `/internal/portal/*` is
  restricted by IP (nginx and the app), signed single-use 5-minute tokens.
* Redis requires a password; PostgreSQL and Redis are on the internal network
  only.
* The API description (`/docs`, `/openapi.json`) is off unless
  `ENABLE_API_DOCS=true`.

## Supply chain

Pinned Python dependencies (`requirements.txt`) and a lockfile for the UI;
CI runs the tests, ruff (with the bandit security rules), `pip-audit` and
`npm audit` on every push; Dependabot proposes updates weekly. The images run
as an unprivileged user.
