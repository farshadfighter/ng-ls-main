#!/usr/bin/env bash
# CI check of the port 80 -> HTTPS redirect in license-admin-ui/nginx.conf, with the real nginx image.
#
# Runs nginx with the repository's config three ways (no redirect file, off.conf, on.conf) and asks
# port 80 and 443 the questions that matter: with the redirect on, only plain-HTTP requests for
# lic.ngcorion.com move to HTTPS; /internal/ (the portal allow list), /health, the bare IP, other
# names and requests a TLS-ending forwarder marks X-Forwarded-Proto: https are served as before.
# The upstream API is not running here, so a proxied path answers 502 - what matters is that it is
# not a redirect.
#
#   scripts/test-https-redirect.sh        (needs docker; uses nginx:alpine, as Dockerfile.prod does)
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE=${NGINX_IMAGE:-nginx:alpine}
NAME=ls-redirect-test
work=$(mktemp -d)
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

mkdir -p "$work/certs" "$work/html"
echo '<!doctype html><title>spa</title>' > "$work/html/index.html"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=test \
    -keyout "$work/certs/license-server.key" -out "$work/certs/license-server.crt" 2>/dev/null
chmod 644 "$work/certs/"*

start() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --add-host license-server:127.0.0.1 -p 127.0.0.1:18080:80 -p 127.0.0.1:18443:443 \
        -v "$PWD/license-admin-ui/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
        -v "$work/certs:/etc/nginx/certs:ro" -v "$work/html:/usr/share/nginx/html:ro" "$@" "$IMAGE" >/dev/null
    for _ in $(seq 1 20); do
        curl -s -o /dev/null http://127.0.0.1:18080/ && return 0
        sleep 0.5
    done
    docker logs "$NAME"
    echo "nginx did not start" >&2
    exit 1
}

failures=0
expect() {  # expect <want> <host> <path> [x-forwarded-proto] [scheme]
    local want=$1 host=$2 path=$3 xfp=${4:-} scheme=${5:-http} got
    local port=18080; [ "$scheme" = https ] && port=18443
    local args=(-s -k -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $host")
    [ -n "$xfp" ] && args+=(-H "X-Forwarded-Proto: $xfp")
    got=$(curl "${args[@]}" "$scheme://127.0.0.1:$port$path")
    got=${got% }
    if [ "$got" = "$want" ]; then
        echo "  ok   $scheme $host$path ${xfp:+(X-Forwarded-Proto $xfp) }-> $got"
    else
        echo "  FAIL $scheme $host$path ${xfp:+(X-Forwarded-Proto $xfp) }-> '$got', expected '$want'"
        failures=$((failures + 1))
    fi
}

unchanged() {
    expect "200" lic.ngcorion.com /
    expect "200" 203.0.113.5 /
    expect "403" lic.ngcorion.com /internal/portal/x
}

echo "without the Let's Encrypt override (no redirect file):"
start
unchanged

echo "HTTPS_REDIRECT=off:"
start -v "$PWD/license-admin-ui/https-redirect/off.conf:/etc/nginx/https-redirect/redirect.conf:ro"
unchanged

echo "HTTPS_REDIRECT=on:"
start -v "$PWD/license-admin-ui/https-redirect/on.conf:/etc/nginx/https-redirect/redirect.conf:ro"
expect "308 https://lic.ngcorion.com/" lic.ngcorion.com /
expect "308 https://lic.ngcorion.com/api/licenses/validate?x=1" lic.ngcorion.com "/api/licenses/validate?x=1"
expect "308 https://lic.ngcorion.com/" LIC.NGCORION.COM /
expect "308 https://lic.ngcorion.com/" lic.ngcorion.com / http
expect "308 https://lic.ngcorion.com/healthz" lic.ngcorion.com /healthz
expect "502" lic.ngcorion.com /health
expect "403" lic.ngcorion.com /internal/portal/x
expect "200" lic.ngcorion.com / https
expect "200" 203.0.113.5 /
expect "200" license.ngcorion.local /
expect "200" lic.ngcorion.com / "" https

[ "$failures" -eq 0 ] || { echo "$failures check(s) failed" >&2; exit 1; }
echo "all checks passed"
