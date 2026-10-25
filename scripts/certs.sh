#!/usr/bin/env bash
# Creates the pipeline CA and one certificate per node with the step CLI.
#
# Idempotent: existing files are kept. A leaf certificate is only re-issued when it is
# missing or close to expiry (step certificate needs-renewal). Everything goes into
# certs/, which is gitignored: no private key ever enters the repository.
#
# Usage: scripts/certs.sh   (reads instance ids from teams.txt)
set -euo pipefail
cd "$(dirname "$0")/.."

CERTS=certs
SERVER_NAME=${SERVER_NAME:-tactical-server}  # the name instances dial (see README, "Toxiproxy and the server name")
LEAF_VALIDITY=${LEAF_VALIDITY:-2160h}         # 90 days
CA_VALIDITY=${CA_VALIDITY:-87600h}            # 10 years

mkdir -p "$CERTS"
chmod 700 "$CERTS"

if command -v step >/dev/null 2>&1; then
  step() { (cd "$CERTS" && command step "$@"); }
else
  step() {
    docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/$CERTS:/certs" -w /certs \
      smallstep/step-cli:0.28.7 step "$@"
  }
fi

# --no-password keeps the demo non-interactive. In production the CA key stays offline
# or in an HSM, and instances get certificates from step-ca with short lifetimes.
if [[ ! -f $CERTS/ca/root_ca.crt ]]; then
  mkdir -p "$CERTS/ca"
  step certificate create "Telemetry Pipeline CA" ca/root_ca.crt ca/root_ca.key \
    --profile root-ca --not-after "$CA_VALIDITY" --no-password --insecure
  echo "created CA"
fi

# issue <dir> <common name> [ca-dir]: a leaf certificate signed by the CA in ca-dir.
issue() {
  local dir=$1 name=$2 ca=${3:-ca}
  if [[ -f $CERTS/$dir/tls.crt ]] && ! step certificate needs-renewal "$dir/tls.crt" >/dev/null 2>&1; then
    return 0  # present and not expiring soon
  fi
  mkdir -p "$CERTS/$dir"
  rm -f "$CERTS/$dir/tls.crt" "$CERTS/$dir/tls.key"
  step certificate create "$name" "$dir/tls.crt" "$dir/tls.key" \
    --profile leaf --ca "$ca/root_ca.crt" --ca-key "$ca/root_ca.key" \
    --san "$name" --not-after "$LEAF_VALIDITY" --no-password --insecure
  cp "$CERTS/$ca/root_ca.crt" "$CERTS/$dir/ca.crt"
  echo "issued $dir ($name)"
}

issue server "$SERVER_NAME"
while read -r team; do
  issue "$team" "$team"
done < <(grep -Ev '^\s*(#|$)' teams.txt)

# Two certificates for the rejection test (scripts/test-rogue.sh):
#  rogue-impersonator  claims to be team 1, but is signed by a different CA (rogue-ca).
#  rogue-unlisted      is signed by our CA, but its identity is not in teams.txt.
if [[ ! -f $CERTS/rogue-ca/root_ca.crt ]]; then
  mkdir -p "$CERTS/rogue-ca"
  step certificate create "Rogue CA" rogue-ca/root_ca.crt rogue-ca/root_ca.key \
    --profile root-ca --no-password --insecure
fi
issue rogue-impersonator "$(grep -Ev '^\s*(#|$)' teams.txt | head -1)" rogue-ca
cp "$CERTS/ca/root_ca.crt" "$CERTS/rogue-impersonator/ca.crt"  # it does trust our server
issue rogue-unlisted team-unlisted

find "$CERTS" -name '*.key' -exec chmod 600 {} +
