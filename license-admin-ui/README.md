# License Admin Console

A **standalone** single-page admin frontend for the License Server. It is fully
self-contained — its own directory, its own `package.json` and build, and **no
shared code with the backend**. It talks to the license server purely over HTTP.

## Features

- **Generate** — log in, create a license for a customer (name, email, org,
  plan), and capture the resulting `license_key` + `organization_token` from a
  credential card (copy / download `.json` / `.txt`).
- **Manage** — list, search, and export (CSV) issued licenses; open a detail
  view with usage bars, expiry countdown, timestamps, and the bound VM
  fingerprint; revoke (deactivate) a license.
- **Test** — exercise the public `activate` / `validate` endpoints to verify a
  generated license and demonstrate VM-fingerprint binding.
- **Guide** — built-in documentation of the fingerprint, token structure, and
  security model, derived from the server source.

## Requirements

- Node 18+ and a running [license server](../license_server) (default
  `http://localhost:8001`).

## Develop

```bash
npm install
npm run dev        # http://localhost:5173
```

In dev, Vite proxies `/api` and `/health` to the license server. Point it at a
different server with an env var:

```bash
LICENSE_SERVER_URL=http://localhost:8001 npm run dev
```

Sign in with the server's admin credentials (`ADMIN_USERNAME` /
`ADMIN_PASSWORD` from the license server's `.env`).

## Build

```bash
npm run build      # outputs to dist/
npm run preview     # serve the production build locally
```

For a production build, set the license server origin (CORS on the server is
already open) by copying `.env.example` to `.env` and setting:

```
VITE_LICENSE_API_URL=https://license.example.com
```

When `VITE_LICENSE_API_URL` is empty (dev), requests use same-origin paths and
the Vite proxy handles forwarding.

## Notes / limitations

- The admin API supports **create, list, view, and revoke (soft deactivate)**
  only. There is no reactivate / edit / hard-delete endpoint, so the Reactivate
  control is intentionally disabled.
- The JWT is stored in `sessionStorage` and cleared when the tab closes; a `401`
  from any admin call returns you to the login screen.
