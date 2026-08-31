#!/bin/sh
# Launcher for the Pi crawl unit (crawl-pi@.service).
#
# It builds the crawl.py invocation and turns on live Neo4j loading ONLY when
# NEO4J_URI is set in /etc/cattle-graph.env. So the one unit covers both modes
# with no ExecStart edit:
#
#   NEO4J_URI unset  -> crawl to data_<ASSOC>.jsonl only (ship/load elsewhere)
#   NEO4J_URI set    -> crawl AND load each record straight into Aura as it is
#                       fetched (no whole-file re-reads; loader.py has no
#                       follow/offset mode, so per-record live load is the way
#                       to keep the graph current from the Pi)
#
# Credentials come from the EnvironmentFile, not the unit. Note that crawl.py
# only accepts --password as an argument, so on a live-load run the Aura
# password is visible in `ps` to a local user -- acceptable on a single-user
# home Pi, and the reason the Lightsail units kept loading in a separate
# process. Leave NEO4J_URI unset and load with loader.py/rsync if that matters.

set -eu

ASSOC="$1"
BASE=/opt/cattle-graph

set -- \
    --association "$ASSOC" \
    --out "$BASE/data_${ASSOC}.jsonl" \
    --db "$BASE/frontier_${ASSOC}.db" \
    --status-file "$BASE/status_${ASSOC}.json" \
    --ignore-robots \
    --skip-steers \
    --no-epds \
    --stay-in-association \
    --html-store "$BASE/html" \
    --delay "${CRAWL_DELAY:-2.0}" \
    --user-agent "${CRAWL_UA:-cattle-graph crawler}"

# Live-load into Aura when an endpoint is configured. --skip-steers is already
# in the crawl args above, so the graph matches how the Lightsail box loaded it.
if [ -n "${NEO4J_URI:-}" ]; then
    set -- "$@" \
        --neo4j \
        --uri "$NEO4J_URI" \
        --user "${NEO4J_USER:-neo4j}" \
        --password "${NEO4J_PASSWORD:-}"
fi

exec "$BASE/.venv/bin/python" "$BASE/crawl.py" "$@"
