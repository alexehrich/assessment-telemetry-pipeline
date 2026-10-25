#!/usr/bin/env bash
# Proves that telemetry from every instance in teams.txt arrives on the tactical server.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p results
exec > >(tee results/delivery.txt) 2>&1

echo "== telemetry from the last 30 s, per drone"
report=$(docker compose exec -T receiver python3 -m pipeline.report --minutes 0.5)
echo "$report"
fail=0
for team in $(grep -Ev '^\s*(#|$)' teams.txt); do
  if echo "$report" | grep -Eq "^$team-[0-9]+ +[1-9]"; then
    echo "PASS $team: data arrived"
  else
    echo "FAIL $team: no data in the last 30 s"; fail=1
  fi
done
exit $fail
