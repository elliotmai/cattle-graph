#!/usr/bin/env bash
# What the four console windows used to tell you at a glance.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/cattle-graph}"
for f in "$APP_DIR"/status_*.json; do
  [ -e "$f" ] || continue
  "$APP_DIR/.venv/bin/python" - "$f" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
c = s.get("counts", {})
print(f'{s["association"]:6} {s["state"]:8} done={c.get("done",0):>7} '
      f'pending={c.get("pending",0):>7} failed={c.get("failed",0):>4} '
      f'{s.get("rate_per_min",0)}/min  {s.get("pct",0)}%')
PY
done
df -h "$APP_DIR" | tail -1 | awk '{print "disk: "$3" used, "$4" free ("$5")"}'
