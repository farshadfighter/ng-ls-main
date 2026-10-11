# Let's Encrypt for lic.ngcorion.com

NGCorion reaches the license server at `https://lic.ngcorion.com` and checks
its certificate against the public CAs. With the override file
`docker-compose.acme.yml` the stack gets that certificate from Let's Encrypt
by itself and renews it by itself (30 days before it ends); nothing else is
needed on the server, and no domain manager in front of it.

## What runs

```
internet ──443──► Traefik ── lic.ngcorion.com: TLS ends here (Let's Encrypt) ──► nginx :8081
                         └─ IP / other names: passed through encrypted ─────────► nginx :8443 (self-signed)
LAN      ───80──► nginx :80  (unchanged; optionally lic.ngcorion.com -> HTTPS, see below)
```

* **Traefik** (`traefik/dynamic.yml`) takes host port 443. It chooses by the
  TLS name (SNI): for `lic.ngcorion.com` it terminates TLS with the Let's
  Encrypt certificate; everything else (the bare IP, `license.ngcorion.local`)
  goes through still encrypted to nginx, which answers with the self-signed
  certificate exactly as before. Clients that connect by IP with
  `LICENSE_SERVER_CA_BUNDLE` keep working.
* **nginx** keeps port 80, unchanged, and serves the same application on all
  listeners.
* **Real client addresses.** Traefik passes the client's address to nginx with
  the PROXY protocol, so the `/internal/` allow list, the per-IP rate limiter
  and the logs see the client, not Traefik.
* **No Docker socket** is mounted into Traefik; its routing is the static file.
* The certificate and the ACME account key are kept in the `acme` volume
  (`/acme/acme.json`, mode 600), so a restart does not request a new one.

The certificate is validated with **TLS-ALPN-01**, which runs entirely on
port 443; port 80 is not involved.

## Before you start

1. DNS: an `A` record `lic.ngcorion.com` → this server's public address. If the
   zone is on a proxying CDN (Cloudflare and the like), set the record to
   **DNS only**: with proxying on, the CDN answers the challenge, not this
   server.
2. Port **443/TCP** reachable from the internet (Let's Encrypt connects to it
   from several places to validate).
3. Docker Compose **2.24.4** or newer (`docker compose version`).

Check DNS from outside: `dig +short lic.ngcorion.com` must print this
server's address.

## Turning it on

In `.env`:

```
ACME_EMAIL=ops@example.com          # expiry notices from Let's Encrypt
COMPOSE_FILE=docker-compose.yml:docker-compose.acme.yml
```

Optionally a dry run first against the staging CA (its certificates are not
trusted, but it has no weekly limit):

```
ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
```

Then:

```
docker compose up -d --build
docker compose logs -f traefik     # "Server responded with a certificate."
```

When the staging run worked, remove `ACME_CA_SERVER` and delete the staging
certificate so the production one is requested:

```
docker compose down
docker volume rm ng-ls-main_acme   # the name is <project>_acme: docker volume ls
docker compose up -d
```

## Checking

```
# the certificate: issuer Let's Encrypt, name lic.ngcorion.com
echo | openssl s_client -connect lic.ngcorion.com:443 -servername lic.ngcorion.com 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates

# the API, with ordinary certificate checking
curl -fsS https://lic.ngcorion.com/health

# the public key NGCorion trusts
curl -fsS https://lic.ngcorion.com/api/signing-key
```

## Redirecting port 80 to HTTPS

Port 80 keeps serving the whole application in cleartext (the LAN, the
portal and a domain manager use it). Once the Let's Encrypt certificate
works, plain-HTTP requests **for `lic.ngcorion.com`** can be sent to HTTPS
instead:

```
scripts/enable-https-redirect.sh
```

It checks on this host that `https://lic.ngcorion.com/health` answers with a
certificate the system trusts for that name and that the certificate is valid
for at least another 14 days; only then it sets `HTTPS_REDIRECT=on` in `.env`,
recreates the nginx container and checks that port 80 now answers `308` for
`http://lic.ngcorion.com/` and still `200` for `/health`. If any step fails it
leaves (or puts) the redirect off.

What is redirected and what is not (`license-admin-ui/nginx.conf`, `$ls_to_https`):

| Request on port 80 | With the redirect on |
|---|---|
| `Host: lic.ngcorion.com`, any path | `308` to `https://lic.ngcorion.com` + the same path (a POST stays a POST) |
| `/internal/…` (portal) | unchanged: the IP allow list still applies |
| `/health` | unchanged: health checks keep working |
| the bare IP, `license.ngcorion.local`, any other name | unchanged |
| forwarded with `X-Forwarded-Proto: https` (a domain manager that ended TLS) | unchanged: no redirect loop |

Turn it off again with `scripts/enable-https-redirect.sh --off`. The same
rules are checked against the real nginx image in CI
(`scripts/test-https-redirect.sh`).

## When something fails

| Symptom | Cause |
|---|---|
| Traefik log: `connection refused` / `timeout` during the challenge | Port 443 is not reachable from the internet, or DNS points elsewhere. |
| Traefik log: `too many certificates already issued` | The weekly Let's Encrypt limit (5 per name). Keep the `acme` volume; use the staging CA for tests. |
| Browser shows "TRAEFIK DEFAULT CERT" | No certificate yet; look at `docker compose logs traefik`. |
| `/internal/portal/*` answers 403 | The portal's address is not in the nginx allow list (`license-admin-ui/nginx.conf`) and `PORTAL_IP_WHITELIST`. Traefik does not change the address nginx sees. |

## Turning it off

Remove `COMPOSE_FILE` (or stop passing the override) and run
`docker compose up -d --remove-orphans`: nginx publishes 443 again with the
self-signed certificate.
