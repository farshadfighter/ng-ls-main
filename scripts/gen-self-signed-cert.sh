#!/usr/bin/env sh
# Generate the self-signed TLS certificate nginx serves on :443.
#
# This server is internal (172.16.120.81) and is reached BY IP, not by a public
# hostname, so there is no public CA / ACME path available: the certificate has
# to be self-signed. Because it is reached by IP, the address must appear in the
# Subject Alternative Name as an IP entry — a CN alone is ignored by every
# modern TLS client, and a DNS-type SAN does not match a numeric address.
#
# The cert is self-signed with CA:TRUE so the app server can use this one file
# directly as its trust anchor (LICENSE_SERVER_CA_BUNDLE) instead of disabling
# verification. Same shape as the app server's traefik/certs/ngcorion.local.crt.
#
# 127.0.0.1/localhost are always added to the SAN as well. The nginx container's
# healthcheck probes its own HTTPS listener over loopback and verifies the cert
# properly (curl --cacert); without a loopback SAN entry that check could only
# pass with verification turned off, which is not a trade worth making even on
# a loopback probe.
#
#   ./scripts/gen-self-signed-cert.sh                 # SANs from .env
#   LICENSE_SERVER_IP=10.0.0.20 ./scripts/gen-self-signed-cert.sh
#   ./scripts/gen-self-signed-cert.sh --force         # replace an existing cert
#
# Output (gitignored, never baked into an image — mounted at runtime):
#   certs/license-server.crt
#   certs/license-server.key
set -e

cd "$(dirname "$0")/.."

# .env is the single source of configuration for the stack; read the address
# from it so the SAN cannot drift away from the address the stack is actually
# published on. Explicit environment wins, as everywhere else in this repo.
#
# Read with grep rather than `. ./.env`: the file holds secrets containing shell
# metacharacters (the admin password has a `<` in it), and sourcing it either
# errors out or silently executes them.
env_get() {
    [ -f .env ] || return 0
    sed -n "s/^[[:space:]]*$1=//p" .env | tail -n 1
}

LICENSE_SERVER_IP="${LICENSE_SERVER_IP:-$(env_get LICENSE_SERVER_IP)}"
LICENSE_SERVER_DNS="${LICENSE_SERVER_DNS:-$(env_get LICENSE_SERVER_DNS)}"
LICENSE_SERVER_IP="${LICENSE_SERVER_IP:-172.16.120.81}"
# Comma-separated: every hostname the server is reached by gets a DNS SAN.
LICENSE_SERVER_DNS="${LICENSE_SERVER_DNS:-license.ngcorion.local,lic.ngcorion.co}"
CERT_DAYS="${CERT_DAYS:-825}"

CERT_DIR=certs
CRT="$CERT_DIR/license-server.crt"
KEY="$CERT_DIR/license-server.key"

if [ "$1" != "--force" ] && [ -f "$CRT" ]; then
    echo "$CRT already exists. Re-run with --force to replace it."
    echo "Replacing the cert means every client's LICENSE_SERVER_CA_BUNDLE copy"
    echo "must be updated too, or validation starts failing."
    exit 1
fi

# An older docker-compose.yml mounted ./certs with the short syntax, which makes
# docker create a missing ./certs as an empty root-owned directory. openssl would
# then fail half-way with a bare "Permission denied"; say what actually happened.
if [ -d "$CERT_DIR" ] && [ ! -w "$CERT_DIR" ]; then
    echo "error: $CERT_DIR/ exists but is not writable by $(id -un)." >&2
    echo "It was most likely created empty by docker. Fix its ownership and re-run:" >&2
    echo "  sudo chown \"$(id -u):$(id -g)\" $CERT_DIR" >&2
    exit 1
fi

mkdir -p "$CERT_DIR"

SAN="IP:$LICENSE_SERVER_IP"
old_ifs=$IFS
IFS=,
for name in $LICENSE_SERVER_DNS; do
    name=$(printf '%s' "$name" | tr -d '[:space:]')
    [ -n "$name" ] && SAN="$SAN,DNS:$name"
done
IFS=$old_ifs
SAN="$SAN,IP:127.0.0.1,DNS:localhost"

echo "[cert] Generating self-signed certificate"
echo "[cert]   SAN     : $SAN"
echo "[cert]   valid   : $CERT_DAYS days"

openssl req -x509 -newkey rsa:2048 -sha256 -days "$CERT_DAYS" -nodes \
    -keyout "$KEY" -out "$CRT" \
    -subj "/C=IR/ST=Tehran/L=Tehran/O=NGCorion/CN=$LICENSE_SERVER_IP" \
    -addext "subjectAltName=$SAN" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth"

# The private key is mounted into the nginx container; keep it unreadable by
# anyone but the operator on the host side.
chmod 600 "$KEY"
chmod 644 "$CRT"

echo "[cert] Wrote $CRT and $KEY"
echo
echo "Next: copy $CRT to the app server and point LICENSE_SERVER_CA_BUNDLE at it."
