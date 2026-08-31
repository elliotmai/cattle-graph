#!/usr/bin/env python3
"""Where the run is, and where Neo4j is, in one command.

Two separate questions that are easy to confuse:

  * The crawl is `crawl@CHIA` and friends. They read animals and append to
    data_<ASSOC>.jsonl. Nothing in that path touches Neo4j -- the unit does
    not pass --neo4j.
  * The load is a thread inside dashboard_web.py (cattle-dashboard.service),
    which tails each JSONL from a saved byte offset every 60s and shells out
    to loader.py.

So the crawl can be healthy while the graph is frozen: stop cattle-dashboard,
or toggle Auto-load off, and the crawlers keep filling JSONL with nobody
reading it. The published board would not show it -- its counts come from the
crawl frontier, not from the graph. That blind spot is what this prints.

    ./state.py           # crawl, loader lag, graph counts
    ./state.py --json    # same, machine-readable
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

APP_DIR = os.environ.get("APP_DIR", "/opt/cattle-graph")
ENV_FILE = os.environ.get("CATTLE_ENV_FILE", "/etc/cattle-graph.env")

# A status file is rewritten on every animal (~17s at 3.5/min), and the loader
# runs every 60s. Past these the thing has stopped, it is not merely slow.
CRAWL_STALE_SEC = 300
LOAD_STALE_SEC = 600


def load_env() -> None:
    """Credentials come from the same file the units use. Read, never exported
    onto a command line -- an argument is visible to anyone who can run `ps`."""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#"):
                    val = m.group(2)
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    os.environ.setdefault(m.group(1), val)
    except OSError:
        pass  # not on the box, or run as a user who cannot read it


def age_sec(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def unit_state(unit: str) -> str:
    """`systemctl is-active`, or "?" anywhere there is no systemd to ask.

    Only stdout is trusted: is-active exits non-zero for a unit that is merely
    inactive, and prints its complaint about a missing bus to stderr. Reading
    stderr would paste "Failed to connect to bus" into every line of output on
    a laptop, which is noise, not a finding.
    """
    try:
        p = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "?"
    return (p.stdout or "").strip() or "?"


def crawl_state() -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(APP_DIR, "status_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError) as e:
            out.append({"association": os.path.basename(path), "error": str(e)})
            continue
        counts = st.get("counts") or {}
        code = st.get("association") or ""
        out.append({
            "association": code,
            "state": st.get("state"),
            "done": counts.get("done", 0),
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "rate_per_min": st.get("rate_per_min", 0),
            "pct": st.get("pct", 0),
            "status_age_sec": age_sec(st.get("updated_at")),
            "unit": unit_state(f"crawl@{code}.service") if code else "?",
        })
    return out


def loader_state(codes: list[str]) -> dict:
    """How far behind the loader is, in bytes of JSONL it has not read yet.

    Bytes rather than records because it is exact and cheap: the offset is a
    byte position and the file size is a stat. The record estimate beside it
    is derived from the average line length of the part already loaded, which
    is close enough to answer "is it minutes or days behind".
    """
    offsets_file = os.path.join(APP_DIR, "load_offsets.json")
    offsets, counts = {}, {}
    try:
        with open(offsets_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "offsets" in data:
            offsets, counts = data.get("offsets") or {}, data.get("counts") or {}
        else:                                   # legacy format: bare offsets
            offsets = data or {}
    except (OSError, ValueError):
        pass

    files = []
    for code in codes:
        path = os.path.join(APP_DIR, f"data_{code}.jsonl")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        off = int(offsets.get(code, 0) or 0)
        loaded = int(counts.get(code, 0) or 0)
        behind_bytes = None if size is None else max(0, size - off)
        est_records = None
        if behind_bytes and loaded and off:
            est_records = int(behind_bytes / (off / loaded))
        files.append({
            "association": code, "bytes_total": size, "bytes_loaded": off,
            "behind_bytes": behind_bytes, "behind_records_est": est_records,
            "records_loaded": loaded,
        })

    return {
        "unit": unit_state("cattle-dashboard.service"),
        "offsets_age_sec": age_sec(datetime.fromtimestamp(
            os.path.getmtime(offsets_file), timezone.utc).isoformat())
            if os.path.exists(offsets_file) else None,
        "files": files,
    }


def graph_state(codes: list[str]) -> dict:
    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return {"error": f"NEO4J_URI not set (looked in {ENV_FILE})"}
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {"error": "neo4j driver not installed in this interpreter"}

    # Same +ssc swap the dashboard and check_neo4j.py use, so a self-signed
    # Aura chain does not read as "the graph is down".
    dial = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    pw = os.environ.get("NEO4J_PASSWORD")
    if not pw:
        return {"error": f"NEO4J_PASSWORD not set (looked in {ENV_FILE})"}

    driver = None
    try:
        driver = GraphDatabase.driver(dial, auth=(user, pw))
        driver.verify_connectivity()
        with driver.session() as s:
            animals = s.run("MATCH (a:Animal) RETURN count(a) AS n").single()["n"]
            regs = s.run("MATCH (r:Registration) RETURN count(r) AS n").single()["n"]
            # Per association, Registration is the unit that lines up with a
            # crawled record; Animal does not, because animals registered in
            # two associations collapse onto one node.
            by_assoc = {
                code: s.run("MATCH (r:Registration {association:$c}) "
                            "RETURN count(r) AS n", c=code).single()["n"]
                for code in codes
            }
        return {"uri": uri, "animals": animals, "registrations": regs,
                "by_association": by_assoc}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"}
    finally:
        if driver is not None:
            driver.close()


def human_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "K", "M"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def report(crawl: list[dict], loader: dict, graph: dict) -> list[str]:
    """Everything that is not as it should be. Empty means healthy."""
    problems = []
    for c in crawl:
        if c.get("error"):
            problems.append(f"{c['association']}: unreadable status file")
            continue
        age = c["status_age_sec"]
        if age is None or age > CRAWL_STALE_SEC:
            problems.append(
                f"{c['association']}: crawler silent for "
                f"{'ever' if age is None else f'{age/60:.0f}m'} (unit {c['unit']})")
        elif c["unit"] not in ("active", "?"):
            problems.append(f"{c['association']}: unit is {c['unit']}")

    if loader["unit"] not in ("active", "?"):
        problems.append(f"loader is not running (cattle-dashboard is {loader['unit']}) "
                        "-- nothing is writing to Neo4j")
    elif loader["offsets_age_sec"] is not None and loader["offsets_age_sec"] > LOAD_STALE_SEC:
        problems.append(f"loader has not advanced in "
                        f"{loader['offsets_age_sec']/60:.0f}m -- check crawl__load.log")
    for f in loader["files"]:
        if f["behind_records_est"] and f["behind_records_est"] > 5000:
            problems.append(f"{f['association']}: ~{f['behind_records_est']:,} records "
                            "crawled but not yet in the graph")
    if graph.get("error"):
        problems.append("graph: " + graph["error"])
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    load_env()
    crawl = crawl_state()
    codes = [c["association"] for c in crawl if c.get("association") and not c.get("error")]
    loader = loader_state(codes)
    graph = graph_state(codes)
    problems = report(crawl, loader, graph)

    if args.json:
        print(json.dumps({"crawl": crawl, "loader": loader, "graph": graph,
                          "problems": problems}, indent=2))
        return 1 if problems else 0

    print("CRAWL   (frontier -> data_*.jsonl)")
    for c in crawl:
        if c.get("error"):
            print(f"  {c['association']:6} unreadable: {c['error']}")
            continue
        age = c["status_age_sec"]
        seen = "never" if age is None else f"{age:.0f}s ago"
        print(f"  {c['association']:6} {str(c['state']):8} done={c['done']:>7} "
              f"pending={c['pending']:>7} failed={c['failed']:>4} "
              f"{c['rate_per_min']}/min {c['pct']}%  read {seen}  [{c['unit']}]")

    print(f"\nLOAD    (data_*.jsonl -> Neo4j, inside cattle-dashboard) [{loader['unit']}]")
    for f in loader["files"]:
        est = "" if f["behind_records_est"] is None else f" (~{f['behind_records_est']:,} records)"
        print(f"  {f['association']:6} loaded={f['records_loaded']:>7} "
              f"behind={human_bytes(f['behind_bytes'])}{est}")
    if loader["offsets_age_sec"] is not None:
        print(f"  last advanced {loader['offsets_age_sec']/60:.0f}m ago")

    print("\nGRAPH   (what is actually in Neo4j)")
    if graph.get("error"):
        print(f"  UNREACHABLE: {graph['error']}")
    else:
        print(f"  {graph['uri']}")
        print(f"  animals={graph['animals']:,}  registrations={graph['registrations']:,}")
        for code, n in graph["by_association"].items():
            crawled = next((c["done"] for c in crawl if c["association"] == code), 0)
            print(f"  {code:6} registrations={n:>7}  crawled={crawled:>7}")
        # The loader runs with --skip-steers, so a gap here is expected and is
        # not by itself evidence of lag. `behind` above is the honest signal:
        # it counts JSONL bytes nobody has read, which no filter affects.
        print("  (registrations < crawled is normal: the loader skips steers)")

    print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print(f"  ! {p}")
    else:
        print("All three moving, graph within one load cycle of the crawl.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
