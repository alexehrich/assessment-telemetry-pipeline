#!/usr/bin/env bash
# Proves that unknown instances are rejected. Three attempts, each against team 1's proxy:
#   1. rogue-impersonator  claims team 1's identity, certificate from a different CA
#   2. rogue-unlisted      certificate from our CA, identity not in teams.txt
#   3. no client certificate at all
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p results
exec > >(tee results/rogue.txt) 2>&1

start_s=$(date +%s)
echo "== starting rogue clients"
docker compose --profile rogue up -d --no-deps rogue-impersonator rogue-unlisted
docker compose --profile rogue run --rm --no-deps --entrypoint python3 rogue-unlisted -c '
import socket, ssl
ctx = ssl.create_default_context(cafile="/certs/ca.crt")  # trusts the server, sends no certificate
try:
    s = ctx.wrap_socket(socket.create_connection(("tactical-server", 7001)), server_hostname="tactical-server")
    s.sendall(b"\0\0\0\1x"); print("no-cert client got:", s.recv(16) or "connection closed")
except (OSError, ssl.SSLError) as e:
    print("no-cert client:", e)'
sleep 15
docker compose --profile rogue rm -sf rogue-impersonator rogue-unlisted >/dev/null 2>&1

echo
echo "== receiver log"
log=$(docker compose logs --no-log-prefix --since "$start_s" receiver)
echo "$log" | grep -o 'rejected .*' | sed -E "s/^rejected \('[0-9.]+', [0-9]+\)/rejected/" | sort | uniq -c

fail=0
check() {
  if echo "$log" | grep -q "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi
}
check "certificate from another CA is rejected" "CERTIFICATE_VERIFY_FAILED"
check "identity not in teams.txt is rejected" "team-unlisted' is not an allowed instance"
check "client without certificate is rejected" "PEER_DID_NOT_RETURN_A_CERTIFICATE"
if docker compose exec -T receiver python3 -m pipeline.report --minutes 2 | grep -q unlisted; then
  echo "FAIL rogue data was stored"; fail=1
else
  echo "PASS no rogue data stored"
fi
exit $fail
