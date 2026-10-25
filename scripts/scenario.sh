#!/usr/bin/env bash
# Runs one network scenario from NETWORK-SCENARIOS.md, then shows how the pipeline coped.
#
# Usage: scripts/scenario.sh <short-outage|long-outage|high-latency|low-bandwidth> [proxy ...]
#   Without proxies, the scenario hits every team in teams.txt.
#   Output is also saved to results/<scenario>.txt.
set -euo pipefail
cd "$(dirname "$0")/.."

API=${TOXIPROXY_URL:-http://localhost:8474}
SETTLE_S=${SETTLE_S:-60}  # time after the scenario for buffers to drain before reporting
name=${1:?usage: $0 <short-outage|long-outage|high-latency|low-bandwidth> [proxy ...]}
shift
proxies=${*:-$(grep -Ev '^\s*(#|$)' teams.txt | tr '\n' ' ')}

now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
api() { curl -fsS -X "$1" "$API$2" ${3:+-d "$3"} >/dev/null; }

restore() {  # back to a clean link, also after Ctrl-C or a failure
  for p in $proxies; do
    curl -fsS -X POST "$API/proxies/$p" -d '{"enabled": true}' >/dev/null || true
    for t in latency_up latency_down bandwidth_up; do
      curl -fsS -X DELETE "$API/proxies/$p/toxics/$t" >/dev/null 2>&1 || true
    done
  done
}

case $name in
  short-outage)  duration=30;  expect="no loss; data of the outage arrives up to ~30 s late plus the reconnect backoff" ;;
  long-outage)   duration=600; expect="no loss (10 min is ~1 MB, cap is 8 MB); buffer drains within seconds of reconnecting" ;;
  high-latency)  duration=300; expect="no loss, no reconnects; delay about 2-3 s higher for the duration" ;;
  low-bandwidth) duration=300; expect="no loss, no reconnects; compressed stream (~2 KB/s per team) fits under 10 KB/s" ;;
  *) echo "unknown scenario $name" >&2; exit 2 ;;
esac

mkdir -p results
exec > >(tee "results/$name.txt") 2>&1
restore
trap restore EXIT

echo "== $name on: $proxies"
echo "   expected: $expect"
start_s=$(date +%s); t0=$(now_ms)
echo "   $(date +%T) start"

case $name in
  short-outage|long-outage)
    for p in $proxies; do api POST "/proxies/$p" '{"enabled": false}'; done
    sleep "$duration"
    for p in $proxies; do api POST "/proxies/$p" '{"enabled": true}'; done ;;
  high-latency)
    for p in $proxies; do
      api POST "/proxies/$p/toxics" '{"name":"latency_up","type":"latency","stream":"upstream","attributes":{"latency":2000,"jitter":500}}'
      api POST "/proxies/$p/toxics" '{"name":"latency_down","type":"latency","stream":"downstream","attributes":{"latency":2000,"jitter":500}}'
    done
    sleep "$duration"
    for p in $proxies; do
      api DELETE "/proxies/$p/toxics/latency_up"
      api DELETE "/proxies/$p/toxics/latency_down"
    done ;;
  low-bandwidth)
    for p in $proxies; do
      api POST "/proxies/$p/toxics" '{"name":"bandwidth_up","type":"bandwidth","stream":"upstream","attributes":{"rate":10}}'
    done
    sleep "$duration"
    for p in $proxies; do api DELETE "/proxies/$p/toxics/bandwidth_up"; done ;;
esac

t1=$(now_ms)
echo "   $(date +%T) restored, waiting ${SETTLE_S}s for buffers to drain"
sleep "$SETTLE_S"

for p in $proxies; do
  echo
  echo "-- agent-$p, every 10 s"
  docker compose logs --no-log-prefix --since "$start_s" "agent-$p" 2>/dev/null \
    | grep -E 'connected=|ended|connected to' \
    | sed -E 's/^[0-9-]+ ([0-9:]+),[0-9]+ agent [A-Z]+ /\1 /' \
    | sed -E 's/ received=[0-9]+ acked=[0-9]+//'
done

echo
echo "-- receiver: telemetry built during the scenario (+-10 s)"
docker compose exec -T receiver python3 -m pipeline.report --from-ms $((t0 - 10000)) --to-ms $((t1 + 10000))
echo
docker compose logs --no-log-prefix --since "$start_s" receiver 2>/dev/null | grep -E 'duplicates=' | tail -n "$(echo $proxies | wc -w)"
