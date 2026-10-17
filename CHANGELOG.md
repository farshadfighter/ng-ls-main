# Changelog — NGCorion License Server

Versions follow [Semantic Versioning](https://semver.org); each release is an
annotated git tag `vX.Y.Z` equal to `VERSION` in `license_server/app/main.py`.

## Unreleased

### Admin login: Turnstile can be switched off (from SinaBimesl/ng-ls `abd381a`)
- `TURNSTILE_ENABLED` (default `true`). `false` only for a server that cannot
  reach Cloudflare (air-gapped, lab): the login challenge is skipped and the
  widget leaves the login form; account passwords, lockout and sessions remain.
  With the default, an empty `TURNSTILE_SECRET_KEY` still refuses every login
  (503), so compose no longer refuses to start without it.
- `GET /api/admin/login-options` (public) tells the login form whether to show
  the challenge, so the flag lives only in the server's environment:
  `docker compose restart backend`, no rebuild.

### Dependencies
- Admin UI on React 19.3 and Vite 8.3 (with @vitejs/plugin-react 6.1);
  FastAPI 0.142.4, uvicorn 0.54.0, pydantic-settings 2.15.0; ruff 0.16.10 and
  pip-audit 2.10.1 for the checks; actions/setup-python v7. All 181 tests
  pass; npm audit and pip-audit report nothing.

### Security
- Port 80 can send `lic.ngcorion.com` to HTTPS (308) with the Let's Encrypt
  setup. `scripts/enable-https-redirect.sh` turns it on only after checking on
  the host that `https://lic.ngcorion.com` answers with a publicly trusted
  certificate valid for at least 14 more days, and checks the result (it
  switches back off on failure); `--off` turns it off. `/internal/` (portal
  allow list), `/health`, the bare IP, other names and requests forwarded with
  `X-Forwarded-Proto: https` are never redirected. Off by default
  (`HTTPS_REDIRECT=off`); nothing changes without the ACME override. A new CI
  job checks these rules against the real nginx image
  (`scripts/test-https-redirect.sh`).

### Documentation (AFTA)
- The Persian installation/operation guide, security architecture and
  Security Target as PDF (`scripts/docs_pdf.py`): right-to-left A4 in
  Vazirmatn, a cover with version, document id and Jalali date, a linked
  table of contents, bookmarks and page numbers. CI builds them on every push
  (artifact `license-server-docs-fa-pdf`); the Release tag workflow attaches
  them, with `SHA256SUMS`, to a draft release for the tag.
- Persian installation and operation guide for every console role
  (`docs/fa/ADMIN_GUIDE.md`, AGD_PRE/AGD_OPE) and Persian security
  architecture (`docs/fa/ARCHITECTURE.md`, ADV_ARC); the Security Target
  points to them. The older English guides that describe the first version of
  the license system are marked as superseded.
- `.env.example`: the first account's password follows the real policy (12
  characters, three character classes).

## 2.0.0 — 2026-10-11

### Security
- Personal admin accounts with roles (admin, operator, auditor), a password
  policy, lockout after repeated failures, server-side sessions with an idle
  timeout, a login notice and `python -m app.cli` for recovery. The shared
  `ADMIN_USERNAME` / `ADMIN_PASSWORD` only make the first account.
- An append-only audit trail of sign-ins, account and license changes,
  offline files, portal orders and NGCorion activations, with search and CSV
  export for auditors.
- Every licensing answer and offline license file is signed with Ed25519;
  a license is bound to the machine that activated it.
- Rate limiting no longer trusts a client-supplied `X-Forwarded-For`; all
  dependency vulnerabilities fixed (python-jose replaced by PyJWT); client
  input bounded; TLS 1.2 limited to forward-secret AEAD suites.

### Licensing
- Asset-based plans (`NGC-PILOT-5-1M` … `NGC-ENT-ULT-1Y`) with 50-asset add-on
  packs; the reported asset count is shown next to each license's limit.
- The server no longer downgrades a license because its NGCorion was offline.
- Offline (air-gapped) licenses: a one-year signed license file for a
  request file made on NGCorion; every issue recorded. The Test page checks
  request and license files.

### Operations
- Optional Let's Encrypt certificate for lic.ngcorion.com through Traefik
  (`docker-compose.acme.yml`, `ACME.md`).
- CI (tests on PostgreSQL and Redis, migrations both ways, ruff with the
  security rules, pip-audit, npm audit), Dependabot.

## 1.0.0 — 2026-10-05

- First version in this repository: licenses, activation, validation,
  heartbeat, the admin console and the portal integration.
