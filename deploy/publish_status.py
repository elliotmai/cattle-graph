#!/usr/bin/env python3
"""Push crawl progress to the Netlify board.

The crawl box has no inbound ports open, so it publishes rather than being
polled. Run from a systemd timer every 60s.

Status only. The status files carry a `current` block -- one animal's name,
registration, birth date, sex and genetic defects -- and this projection drops
it, along with `pid`. That is the whole reason the projection lives here
rather than in the function: the data never leaves the box in the first place,
so nothing downstream has to be trusted to strip it.

    ./publish_status.py            # publish
    ./publish_status.py --dry-run  # print exactly what would be sent
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

import requests

APP_DIR = os.environ.get("APP_DIR", "/opt/cattle-graph")

# The only keys that may leave this machine. Anything the crawler adds to its
# status file later is dropped by default rather than published by accident --
# the function rejects unknown fields too, so a mistake here fails loudly.
ALLOWED = ("association", "state", "done", "pending", "failed", "skipped",
           "rate_per_min", "pct", "updated_at")


def project(path: str) -> dict | None:
    """One status file down to publishable numbers, or None to skip it."""
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError) as e:
        print(f"skip {os.path.basename(path)}: {e}", file=sys.stderr)
        return None

    counts = st.get("counts") or {}
    board = {
        "association": st.get("association"),
        "state": st.get("state"),
        "done": counts.get("done", 0),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "rate_per_min": st.get("rate_per_min", 0),
        "pct": st.get("pct", 0),
        "updated_at": st.get("updated_at"),
    }
    if not board["association"]:
        return None

    # An association that has never crawled anything (ANGUS, blocked by a JS
    # challenge angus.py cannot clear) would sit on the board as a permanently
    # red card nobody can act on. Leave it off until it actually runs.
    if not board["done"] and board["state"] != "running":
        return None

    return {k: v for k, v in board.items() if k in ALLOWED}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the payload instead of sending it")
    ap.add_argument("--source", default=os.environ.get("CATTLE_SOURCE", "lightsail"),
                    help="publisher name; one blob per source (default: lightsail)")
    args = ap.parse_args()

    boards = [b for b in (project(p) for p in sorted(glob.glob(f"{APP_DIR}/status_*.json"))) if b]

    usage = shutil.disk_usage(APP_DIR)
    payload = {
        "source": args.source,
        "boards": boards,
        "disk": {"used_gb": round(usage.used / 1e9, 1),
                 "free_gb": round(usage.free / 1e9, 1)},
    }

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    endpoint = os.environ.get("CATTLE_ENDPOINT")
    token = os.environ.get("CATTLE_INGEST_TOKEN")
    if not endpoint or not token:
        print("CATTLE_ENDPOINT and CATTLE_INGEST_TOKEN must be set "
              "(see /etc/cattle-graph.env)", file=sys.stderr)
        return 2

    if not boards:
        # The function refuses this anyway, to stop a restarting crawler from
        # blanking a good board. Failing here keeps that out of the logs.
        print("nothing to publish; not sending an empty board", file=sys.stderr)
        return 0

    try:
        r = requests.post(endpoint, json=payload, timeout=30,
                          headers={"authorization": f"Bearer {token}"})
    except requests.RequestException as e:
        print(f"publish failed: {e}", file=sys.stderr)
        return 1

    if r.status_code >= 400:
        print(f"publish rejected {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 1

    print(f"published {len(boards)} board(s): "
          + ", ".join(f"{b['association']}={b['done']}" for b in boards))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
