#!/bin/sh
# Ship the Pi's crawl output to the loader box (e.g. Lightsail), which loads it
# into Aura. The Pi crawls behind a residential IP to get past Cloudflare; the
# loader runs where the network is close to Aura, because loading is chatty
# (~10 round trips per record) and crawls badly over a home connection. See
# deploy/pi-setup.md.
#
# Config via environment (or edit the defaults):
#   SHIP_DEST   user@host of the loader box            (required, e.g. ubuntu@54.1.2.3)
#   SHIP_DIR    remote dir holding the data_*.jsonl     (default /opt/cattle-graph)
#   SHIP_ASSOC  space-separated association codes        (default "CHIA")
#   SSH_KEY     optional ssh identity file
#
# data_*.jsonl is append-only, so `rsync --append-verify` sends only the new
# bytes and checksums the shared prefix -- cheap to run often from cron. Wrap it
# in flock (see pi-setup.md) so overlapping cron runs don't race.

set -eu

BASE=/opt/cattle-graph
DEST="${SHIP_DEST:?set SHIP_DEST=user@loader-host}"
DIR="${SHIP_DIR:-/opt/cattle-graph}"
ASSOC="${SHIP_ASSOC:-CHIA}"

SSH_CMD="ssh -o BatchMode=yes"
[ -n "${SSH_KEY:-}" ] && SSH_CMD="$SSH_CMD -i $SSH_KEY"

for a in $ASSOC; do
    f="$BASE/data_${a}.jsonl"
    if [ ! -f "$f" ]; then
        echo "skip: $f missing"
        continue
    fi
    rsync --append-verify -e "$SSH_CMD" "$f" "$DEST:$DIR/data_${a}.jsonl"
    echo "shipped: data_${a}.jsonl -> $DEST:$DIR/"
done
