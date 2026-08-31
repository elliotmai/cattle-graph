#!/usr/bin/env python3
"""Push crawl progress to the Netlify board.

The crawl box has no inbound ports open, so it publishes rather than being
polled. Run from a systemd timer every 30s.

Progress, plus the identity of the animal being read right now. The status
files carry a full `current` record -- name, registration, colour, birth date,
sex and genetic defect findings -- and this projection keeps only the three
identity fields, dropping the rest along with `pid`. That is why the projection
lives here rather than in the function: what is not published never leaves the
box, so nothing downstream has to be trusted to strip it.

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
           "rate_per_min", "pct", "updated_at", "current")

# The status file's `current` block is a full animal record: name, reg, colour,
# date of birth, sex and genetic defect codes with their status. Only the three
# identity fields are published -- enough to show what is being read right now,
# without putting birth dates and defect findings on a public URL. Widening
# this is a deliberate act, not an oversight: add the key here and to the
# allowlist in publish.mjs, which rejects anything it does not recognise.
CURRENT_FIELDS = ("association", "reg", "name")

# The graph half of the board. dashboard_web.py's refresh loop writes
# neo_stats.json every 8s; this reads it rather than opening its own Aura
# connection, so the figures on the board are the same ones the local
# dashboard shows and Aura is not queried twice for them.
#
# Counts only. Every value here is an aggregate over the whole graph or one
# association -- there is no per-animal anything to leak, which is what makes
# the graph safe to put on a public page at all.
GRAPH_FILE = os.path.join(APP_DIR, "neo_stats.json")
GRAPH_FIELDS = ("animals", "registrations", "multi_assoc")
DEFECT_STATUSES = ("Free", "Carrier", "Suspect", "Unknown")


def _load_lag() -> dict[str, int]:
    """Records crawled into JSONL that the loader has not written yet.

    Estimated from the loader's saved byte offset against the file size:
    bytes are exact and cheap, and the average line length of the part already
    loaded turns them into a record count that is close enough to answer "is
    the graph a minute behind or a day behind".

    This is the honest lag signal. Comparing registrations to crawled counts
    is not -- the loader runs with --skip-steers, so the graph is legitimately
    smaller than the crawl and always will be.
    """
    try:
        with open(os.path.join(APP_DIR, "load_offsets.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    offsets = data.get("offsets", data) or {}
    counts = data.get("counts") or {}

    lag = {}
    for code, off in offsets.items():
        try:
            size = os.path.getsize(os.path.join(APP_DIR, f"data_{code}.jsonl"))
        except OSError:
            continue
        off, loaded = int(off or 0), int(counts.get(code, 0) or 0)
        behind_bytes = max(0, size - off)
        if not behind_bytes:
            lag[code] = 0
        elif off and loaded:
            lag[code] = int(behind_bytes / (off / loaded))
    return lag


def graph_state() -> dict | None:
    """neo_stats.json projected down to publishable counts, or None if the
    dashboard has never written one."""
    try:
        with open(GRAPH_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError) as e:
        print(f"skip graph: {e}", file=sys.stderr)
        return None

    graph = {"ok": bool(st.get("ok")), "read_at": st.get("read_at")}
    for k in GRAPH_FIELDS:
        graph[k] = int(st.get(k) or 0)

    defects = st.get("defects") or {}
    graph["defects"] = {k: int(defects.get(k) or 0) for k in DEFECT_STATUSES
                        if defects.get(k)}

    lag = _load_lag()
    by = {}
    for code, n in (st.get("by_assoc") or {}).items():
        by[str(code)] = {"registrations": int(n or 0), "behind": int(lag.get(code, 0))}
    graph["by_association"] = by

    # st["note"] is the driver's own error text, which can name the Aura host.
    # `ok: false` is enough for a public board to say "the graph is not being
    # read"; why it is not being read is a question for the box.
    return graph


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

    cur = st.get("current") or {}
    if cur.get("reg"):
        board["current"] = {k: cur.get(k) for k in CURRENT_FIELDS}

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
        "graph": graph_state(),
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

    g = payload["graph"]
    print(f"published {len(boards)} board(s): "
          + ", ".join(f"{b['association']}={b['done']}" for b in boards)
          + (f"; graph animals={g['animals']}" if g and g["ok"] else "; graph unread"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
