#!/usr/bin/env bash
# Port 80 -> HTTPS for lic.ngcorion.com, turned on only once HTTPS works with a valid certificate.
#
# With the Let's Encrypt setup (docker-compose.acme.yml, ACME.md) nginx can answer plain-HTTP
# requests for lic.ngcorion.com on port 80 with a redirect to https://lic.ngcorion.com. This script
# turns that on, but only after checking on this host that:
#   * https://lic.ngcorion.com/health answers through Traefik with a certificate the system trusts
#     for that name (ordinary curl verification, no -k), and
#   * the certificate is valid for at least another 14 days.
# Then it sets HTTPS_REDIRECT=on in .env, recreates the nginx container and checks the result,
# switching back to off if port 80 does not behave as expected.
#
# The redirect never applies to /internal/ (the portal), /health, the bare IP, other names or
# requests a TLS-ending forwarder sends with X-Forwarded-Proto: https (license-admin-ui/nginx.conf).
#
#   scripts/enable-https-redirect.sh         check, then turn on
#   scripts/enable-https-redirect.sh --off   turn off
#
# Run it from anywhere on the license server host; it works in the repository root.
set -euo pipefail

NAME=lic.ngcorion.com
MIN_DAYS=14
cd "$(dirname "$0")/.."

die() { echo "error: $*" >&2; exit 1; }

[ -f .env ] || die ".env not found in $(pwd)"
command -v curl >/dev/null || die "curl is needed"
command -v openssl >/dev/null || die "openssl is needed"

env_value() { sed -n "s/^$1=//p" .env | tail -1; }

set_env() {
    if grep -q "^$1=" .env; then
        sed -i "s|^$1=.*|$1=$2|" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

# The host ports, as .env publishes them ("80" or "127.0.0.1:80").
http_port=$(env_value FRONTEND_PORT); http_port=${http_port:-80}; http_port=${http_port##*:}
https_port=$(env_value HTTPS_PORT); https_port=${https_port:-443}; https_port=${https_port##*:}

recreate_front() { docker compose up -d --force-recreate front >/dev/null; }

wait_front() {
    for _ in $(seq 1 30); do
        curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${http_port}/health" 2>/dev/null && return 0
        sleep 2
    done
    return 1
}

if [ "${1:-}" = "--off" ]; then
    set_env HTTPS_REDIRECT off
    recreate_front
    wait_front || die "nginx did not come back on port ${http_port}; see: docker compose logs front"
    echo "Port 80 redirect is off."
    exit 0
fi
[ -z "${1:-}" ] || die "unknown option: $1 (use --off to turn the redirect off)"

case ":$(env_value COMPOSE_FILE):" in
    *docker-compose.acme.yml*) ;;
    *) die "the Let's Encrypt override is not on: set COMPOSE_FILE=docker-compose.yml:docker-compose.acme.yml in .env first (ACME.md)" ;;
esac

echo "Checking https://${NAME} on this host (port ${https_port}) ..."
curl -fsS -o /dev/null --max-time 15 --resolve "${NAME}:${https_port}:127.0.0.1" "https://${NAME}:${https_port}/health" \
    || die "https://${NAME}/health does not answer with a trusted certificate yet; leaving the redirect off. Check: docker compose logs traefik"

cert=$(echo | openssl s_client -connect "127.0.0.1:${https_port}" -servername "$NAME" 2>/dev/null | openssl x509 2>/dev/null) \
    || die "could not read the certificate"
echo "$cert" | openssl x509 -noout -issuer -enddate
echo "$cert" | openssl x509 -noout -checkend $((MIN_DAYS * 86400)) >/dev/null \
    || die "the certificate ends within ${MIN_DAYS} days; leaving the redirect off until it is renewed"

set_env HTTPS_REDIRECT on
recreate_front
wait_front || { set_env HTTPS_REDIRECT off; recreate_front; die "nginx did not come back; redirect turned off again"; }

code=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: ${NAME}" "http://127.0.0.1:${http_port}/")
health=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${NAME}" "http://127.0.0.1:${http_port}/health")
if [ "$code" != "308 https://${NAME}/" ] || [ "$health" != "200" ]; then
    set_env HTTPS_REDIRECT off
    recreate_front
    die "port 80 answered '$code' (/ ) and '$health' (/health) instead of a redirect and 200; turned off again"
fi
echo "Port 80 now sends http://${NAME}/ to https://${NAME}/ (308). /health, /internal/, the IP and other names are unchanged."
