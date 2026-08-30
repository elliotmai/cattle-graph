#!/usr/bin/env bash
# Port of load_all.ps1. Loads every data_*.jsonl into Neo4j, deduped on
# International ID. Idempotent and safe to run while the crawlers are going.
#
#   sudo systemctl start cattle-load     # or run directly:
#   ./deploy/load_all.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cattle-graph}"
PY="$APP_DIR/.venv/bin/python"

# Creds come from the environment, never from the command line -- an argument
# is visible to anyone who can run `ps`, which is how the Aura password ended
# up exposed on the old Windows box.
[ -f /etc/cattle-graph.env ] && . /etc/cattle-graph.env
: "${NEO4J_URI:?set NEO4J_URI in /etc/cattle-graph.env}"
: "${NEO4J_USERNAME:?set NEO4J_USERNAME in /etc/cattle-graph.env}"
: "${NEO4J_PASSWORD:?set NEO4J_PASSWORD in /etc/cattle-graph.env}"

shopt -s nullglob
for f in "$APP_DIR"/data_*.jsonl; do
  case "$f" in *__delta.jsonl) continue ;; esac
  [ -s "$f" ] || { echo "=== skipping $(basename "$f") (empty) ==="; continue; }
  echo "=== loading $(basename "$f") ==="
  "$PY" "$APP_DIR/loader.py" "$f" --neo4j \
      --uri "$NEO4J_URI" --user "$NEO4J_USERNAME" --password "$NEO4J_PASSWORD" \
      --insecure --skip-steers
done
echo "done. Check counts in Aura Browser:  MATCH (a:Animal) RETURN count(a);"
