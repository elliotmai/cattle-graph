"""
Cattle pipeline dashboard — a clean, modern browser UI (stdlib only).

    python dashboard_web.py

Opens http://localhost:8787 : one live card per association showing the animal
currently being read (reg, name, colour, DOB), a completion bar and stats, plus
a Neo4j panel with graph totals, the Free/Carrier/Suspect/Unknown distribution,
and a sequential "Load to Neo4j" view. It launches/stops the crawlers and the
loader for you. Set NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD before launching
to enable the graph panel and loading.
"""

from __future__ import annotations

import os
import re
import sys
import glob
import json
import time
import socket
import threading
import subprocess
import webbrowser
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DASH_PORT", "8787"))
HOST = os.environ.get("DASH_HOST", "0.0.0.0")   # 0.0.0.0 = reachable from other devices on your WiFi


def lan_ip():
    """Best-effort LAN IP of this machine (for the phone URL)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def load_neo4j_creds_file():
    """Load Neo4j credentials from the NEWEST credentials file in this folder
    (Aura download 'Neo4j-xxxx-Created-....txt', neo4j.env, or
    neo4j-credentials.txt) and OVERRIDE any environment vars. This means the file
    you dropped in wins over stale NEO4J_* left over in the terminal from an old
    instance — the #1 cause of 'it connects standalone but the dashboard can't'.
    Delete old creds files so only the current one remains."""
    files = glob.glob(os.path.join(HERE, "[Nn]eo4j*.txt"))
    for extra in ("neo4j.env", "neo4j-credentials.txt"):
        p = os.path.join(HERE, extra)
        if os.path.exists(p):
            files.append(p)
    if not files:
        return
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)   # newest first
    path = files[0]
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*(NEO4J_[A-Z_]+|AURA_[A-Z_]+)\s*=\s*(.+?)\s*$", line)
                if m:
                    os.environ[m.group(1)] = m.group(2)      # override, not setdefault
        if os.environ.get("NEO4J_URI"):
            print(f"Using Neo4j credentials from {os.path.basename(path)}")
    except Exception:
        pass

ASSOCIATIONS = [
    ("CHIA",  "Chianina",    "MA430053"),
    ("MAINE", "Maine-Anjou", "402303"),
    ("SHORT", "Shorthorn",   "3790685"),
    ("ANGUS", "Angus",       "13054003"),
]
CONTACT = os.environ.get("CRAWL_CONTACT", "eli.mai12932@gmail.com")
DELAY = os.environ.get("CRAWL_DELAY", "1.5")

procs: dict[str, subprocess.Popen] = {}
stopped: set = set()   # codes the user explicitly stopped this session
auto_load = {"on": True}                 # incremental auto-load every 60s
load_offsets: dict = {}                  # per-file byte offset already loaded
load_counts: dict = {}                   # per-file records already written to Neo4j
OFFSETS_FILE = None                      # set in main()
_line_cache: dict = {}                   # path -> ((mtime,size), line_count)
load_state = {"running": False, "current": "", "index": 0, "total": 0, "done_msg": ""}
ops_state = {"running": False, "label": "", "result": ""}
neo_stats = {"ok": False, "note": "connect NEO4J_* env vars", "animals": 0,
             "registrations": 0, "multi_assoc": 0, "dup_groups": 0, "defects": {},
             "by_assoc": {}}   # live count of real registrations per association


def frontier_path(c): return os.path.join(HERE, f"frontier_{c}.db")
def data_path(c):     return os.path.join(HERE, f"data_{c}.jsonl")
def status_path(c):   return os.path.join(HERE, f"status_{c}.json")
def log_path(n):      return os.path.join(HERE, f"crawl_{n}.log")


def hidden():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def neo4j_env():
    uri = os.environ.get("NEO4J_URI")
    if uri:
        uri = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
    return uri, os.environ.get("NEO4J_USERNAME", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")


_driver = {"d": None, "key": None}


def get_driver():
    """One long-lived driver for the dashboard's own reads (stats + pedigree).
    Recreated only if the credentials change or it errored last time."""
    uri, user, pw = neo4j_env()
    if not uri:
        return None
    key = (uri, user, pw)
    if _driver["d"] is None or _driver["key"] != key:
        from neo4j import GraphDatabase
        try:
            if _driver["d"]:
                _driver["d"].close()
        except Exception:
            pass
        _driver["d"] = GraphDatabase.driver(uri, auth=(user, pw))
        _driver["key"] = key
    return _driver["d"]


def reset_driver():
    try:
        if _driver["d"]:
            _driver["d"].close()
    except Exception:
        pass
    _driver["d"] = None


def count_lines(path):
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (st.st_mtime, st.st_size)
    cached = _line_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return cached[1] if cached else 0
    _line_cache[path] = (key, n)
    return n


def _count_lines_upto(path, off):
    try:
        with open(path, "rb") as f:
            return f.read(off).count(b"\n")
    except OSError:
        return 0


# --- distinct (association, regNumber) actually in the local files, per assoc,
#     excluding steers. Built incrementally so it stays cheap. This is the honest
#     denominator for "how many local records should be in Neo4j" (each unique
#     registration = one node), regardless of which crawler file it came from.
_distinct: dict = {}          # association -> set(regNumbers)
_distinct_off: dict = {}      # path -> bytes scanned
_REC_RE = re.compile(rb'"association"\s*:\s*"([^"]+)"[\s\S]*?"regNumber"\s*:\s*"([^"]*)"')


def update_distinct():
    for code, _, _ in ASSOCIATIONS:
        path = data_path(code)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        off = _distinct_off.get(path, 0)
        if size < off:
            off = 0
        if size <= off:
            continue
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read()
        nl = chunk.rfind(b"\n")
        if nl == -1:
            continue
        consumed = chunk[:nl + 1]
        for line in consumed.split(b"\n"):
            if not line.strip():
                continue
            if b'"sex": "S"' in line or b'"sex":"S"' in line:
                continue                       # steers don't count
            m = _REC_RE.search(line)
            if m:
                a = m.group(1).decode("ascii", "replace")
                r = m.group(2).decode("ascii", "replace")
                _distinct.setdefault(a, set()).add(r)
        _distinct_off[path] = off + len(consumed)


def remove_steers():
    if ops_state["running"]:
        ops_state.update(result="busy — another operation is running")
        return
    ops_state.update(running=True, label="Remove steers", result="deleting steers…")
    try:
        drv = get_driver()
        with drv.session() as s:
            n = s.run("MATCH (a:Animal {sex:'S'}) RETURN count(a) AS n").single()["n"]
            try:
                s.run("MATCH (a:Animal {sex:'S'}) CALL { WITH a DETACH DELETE a } "
                      "IN TRANSACTIONS OF 1000 ROWS").consume()
            except Exception:
                s.run("MATCH (a:Animal {sex:'S'}) DETACH DELETE a").consume()
        ops_state.update(running=False, result=f"Removed {n} steer(s) ✓")
    except Exception as e:
        ops_state.update(running=False, result="Remove steers failed: " + str(e).splitlines()[0][:120])


def read_status(code):
    try:
        with open(status_path(code), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def heartbeat_fresh(st, secs=20):
    """True if a crawler is actively writing this status file right now."""
    try:
        t = datetime.fromisoformat(st["updated_at"])
        return (datetime.now(timezone.utc) - t).total_seconds() < secs
    except Exception:
        return False


def kill_pid(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, **hidden())
        else:
            os.kill(int(pid), 15)
    except Exception:
        pass


# ---------------- incremental load bookkeeping ----------------

def _save_offsets():
    try:
        with open(OFFSETS_FILE, "w", encoding="utf-8") as f:
            json.dump({"offsets": load_offsets, "counts": load_counts}, f)
    except Exception:
        pass


def _load_offsets():
    global load_offsets, load_counts
    try:
        with open(OFFSETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "offsets" in data:
            load_offsets = data.get("offsets", {})
            load_counts = data.get("counts", {})
        else:                       # legacy format: just offsets
            load_offsets = data or {}
            load_counts = {}
    except Exception:
        load_offsets, load_counts = {}, {}
    # reconcile any missing record counts from the stored byte offset (one-time)
    for code, off in load_offsets.items():
        if code not in load_counts and off:
            load_counts[code] = _count_lines_upto(data_path(code), int(off))


MAX_DELTA_LINES = 20000   # cap per file per pass, so a big backlog loads in chunks


def incremental_delta(code):
    """Return (new_offset, [new lines]) for records appended since the last load,
    capped at MAX_DELTA_LINES so a large backlog is loaded in resumable chunks.
    Only whole lines are consumed, so a record the crawler is mid-writing is left
    for next time. Returns None when there's nothing new."""
    path = data_path(code)
    off = int(load_offsets.get(code, 0))
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < off:            # file was rebuilt/truncated -> start over
        off = 0
    if size == off:
        return None
    with open(path, "rb") as f:
        f.seek(off)
        chunk = f.read()
    parts = chunk.split(b"\n")
    complete = parts[:-1]     # the final part is incomplete (no trailing \n yet)
    if not complete:
        return None
    taken = complete[:MAX_DELTA_LINES]
    consumed_bytes = sum(len(p) + 1 for p in taken)   # +1 for each newline
    lines = [p.decode("utf-8", "replace") for p in taken if p.strip()]
    return off + consumed_bytes, lines


# ---------------- crawler control ----------------

def crawl_cmd(code, seed):
    cmd = [sys.executable, os.path.join(HERE, "crawl.py"),
           "--association", code, "--seed", seed,
           "--out", data_path(code), "--db", frontier_path(code),
           "--status-file", status_path(code),
           "--reset-skipped", "--ignore-robots", "--skip-steers", "--delay", DELAY,
           "--user-agent", f"cattle-graph-crawler/1.0 (authorized; contact: {CONTACT})", "-v"]
    return cmd


def start(code, seed=None):
    stopped.discard(code)
    if procs.get(code) and procs[code].poll() is None:
        return
    seed = seed or dict((c, s) for c, _, s in ASSOCIATIONS)[code]
    logf = open(log_path(code), "a", encoding="utf-8")
    procs[code] = subprocess.Popen(crawl_cmd(code, seed), cwd=HERE, stdout=logf,
                                   stderr=subprocess.STDOUT, **hidden())


def stop(code):
    stopped.add(code)
    p = procs.get(code)
    if p and p.poll() is None:
        p.terminate()
        return
    # No handle (e.g. dashboard was restarted). If a crawler is still actively
    # writing this status file, it's an orphan — kill it by its PID.
    st = read_status(code) or {}
    if st.get("pid") and heartbeat_fresh(st):
        kill_pid(st["pid"])


def running(code):
    p = procs.get(code)
    return bool(p and p.poll() is None)


def add_seed(assoc, reg):
    """Inject a seed into a breed's frontier as PENDING, without disturbing its
    done/pending/failed state (INSERT OR IGNORE via crawl.py --add-seeds-only).
    Works whether or not that crawler is running; if it's running it'll pick the
    new item up, otherwise it waits there until Start."""
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "crawl.py"),
                        "--association", assoc, "--seed", reg,
                        "--db", frontier_path(assoc), "--add-seeds-only", "--front"],
                       cwd=HERE, capture_output=True, text=True, **hidden())
    except Exception:
        pass


# ---------------- Neo4j ----------------

NEO_QUERIES = {
    "animals": "MATCH (a:Animal) RETURN count(a) AS n",
    "registrations": "MATCH (:Registration) RETURN count(*) AS n",
    "multi_assoc": ("MATCH (a:Animal)-[:HAS_REGISTRATION]->(r) "
                    "WITH a, count(DISTINCT r.association) AS c WHERE c>1 RETURN count(a) AS n"),
    "dup_groups": ("MATCH (a:Animal) WHERE a.name_norm CONTAINS ' ' AND NOT a.name_norm CONTAINS ' X ' "
                   "AND a.dob IS NOT NULL AND NOT a.dob ENDS WITH '-01-01' "
                   "WITH a.name_norm AS nn, a.dob AS d, count(*) AS c WHERE c>1 RETURN count(*) AS n"),
}


def neo_refresh_loop():
    while True:
        uri, user, pw = neo4j_env()
        if uri:
            try:
                drv = get_driver()
                with drv.session() as s:
                    for k, q in NEO_QUERIES.items():
                        neo_stats[k] = s.run(q).single()["n"]
                    dist = {}
                    for row in s.run("MATCH (:Animal)-[t:TESTED]->(:Defect) "
                                     "RETURN t.status AS st, count(*) AS n"):
                        dist[row["st"] or "Unknown"] = row["n"]
                    neo_stats["defects"] = dist
                    # real (non-stub) registrations per association = local records
                    # actually present in the graph. This is the ground truth for
                    # "how much of my crawled data is written".
                    by = {}
                    for row in s.run(
                            "MATCH (an:Animal)-[:HAS_REGISTRATION]->(r:Registration) "
                            "WHERE coalesce(r.stub,false)=false AND coalesce(an.sex,'') <> 'S' "
                            "RETURN r.association AS assoc, count(r) AS n"):
                        if row["assoc"]:
                            by[row["assoc"]] = row["n"]
                    neo_stats["by_assoc"] = by
                neo_stats["ok"] = True
                neo_stats["note"] = ""
            except Exception as e:
                neo_stats["ok"] = False
                neo_stats["note"] = str(e).splitlines()[0][:140]
                reset_driver()   # force a fresh connection next cycle
        else:
            neo_stats["ok"] = False
            neo_stats["note"] = "connect NEO4J_* env vars before launching"
        time.sleep(8)


GRAPH_CYPHER = """
MATCH (a:Animal)
WHERE a.uid = $q OR toLower(coalesce(a.name,'')) CONTAINS $ql
   OR EXISTS { MATCH (a)-[:HAS_REGISTRATION]->(rr) WHERE toUpper(rr.regNumber) = $qu }
WITH a LIMIT 1
CALL apoc.path.subgraphAll(a, {relationshipFilter:'<SIRE_OF|<DAM_OF', maxLevel:4})
     YIELD nodes AS an, relationships AS ar
OPTIONAL MATCH (a)-[ke:SIRE_OF|DAM_OF]->(kid:Animal)
WITH a, an, ar,
     collect(DISTINCT kid)[0..60] AS kids,
     collect(DISTINCT {from:a.uid, to:kid.uid, type:type(ke)})[0..60] AS krels
RETURN a.uid AS root,
  [n IN an + kids WHERE n IS NOT NULL |
     {uid:n.uid, name:n.name, sex:n.sex, color:n.color}] AS nodes,
  [r IN ar | {from:startNode(r).uid, to:endNode(r).uid, type:type(r)}] + krels AS rels
"""


def neo_graph(q):
    uri, user, pw = neo4j_env()
    if not uri:
        return {"error": "connect NEO4J_* env vars"}
    try:
        drv = get_driver()
        with drv.session() as s:
            rec = s.run(GRAPH_CYPHER, q=q, ql=q.lower(), qu=q.upper()).single()
        if not rec:
            return {"root": None, "nodes": [], "edges": []}
        nodes, edges = {}, {}
        for n in rec["nodes"]:
            if n and n.get("uid"):
                nodes[n["uid"]] = n
        for r in rec["rels"]:
            if r and r.get("from") and r.get("to"):
                edges[(r["from"], r["to"], r["type"])] = r
        return {"root": rec["root"], "nodes": list(nodes.values())[:200],
                "edges": list(edges.values())}
    except Exception as e:
        reset_driver()
        return {"error": str(e).splitlines()[0][:160]}


def _run_loader(fp, uri, user, pw):
    p = subprocess.run([sys.executable, os.path.join(HERE, "loader.py"), fp, "--neo4j",
                        "--uri", uri, "--user", user, "--password", pw, "--insecure", "--skip-steers"],
                       cwd=HERE, capture_output=True, text=True, **hidden())
    out = (p.stdout or "") + (p.stderr or "")
    with open(log_path("_load"), "a", encoding="utf-8") as logf:
        logf.write(out)
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    tail = lines[-1][:180] if lines else ""
    return p.returncode, tail


def load_deltas():
    """Load ONLY records appended since the last load (never re-reads whole
    files). Writes each file's new lines to a temp delta and loads that."""
    uri, user, pw = neo4j_env()
    if not uri:
        load_state.update(running=False, done_msg="No NEO4J_* env vars set.")
        return
    if load_state["running"]:
        return
    load_state.update(running=True, index=0, total=len(ASSOCIATIONS), done_msg="")
    total_new = 0
    had_error = False
    last_error = ""
    try:
        for i, (code, _, _) in enumerate(ASSOCIATIONS, 1):
            load_state.update(index=i, current=code)
            d = incremental_delta(code)
            if not d:
                continue
            new_off, lines = d
            if not lines:
                load_offsets[code] = new_off
                _save_offsets()
                continue
            tmp = os.path.join(HERE, f"data_{code}.__delta.jsonl")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            load_state.update(current=f"{code} (+{len(lines)})")
            rc, tail = _run_loader(tmp, uri, user, pw)
            if rc == 0:
                load_offsets[code] = new_off   # advance only on success
                load_counts[code] = load_counts.get(code, 0) + len(lines)
                _save_offsets()
                total_new += len(lines)
            else:
                had_error = True
                last_error = tail or f"{code} loader exited {rc}"
                load_state.update(current=f"{code} load failed — will retry")
    finally:
        if total_new:
            msg = f"Loaded {total_new} new record(s)."
        elif had_error:
            msg = "Load failed — " + (last_error or "check crawl__load.log")
        else:
            msg = "Up to date — no new records."
        load_state.update(running=False, current="", done_msg=msg)
    return total_new


def auto_load_loop():
    """Every 60s, if enabled and nothing else is loading, load just the new
    records. Runs once right away to start catching up the overnight backlog."""
    while True:
        n = 0
        if auto_load["on"] and not load_state["running"] and not ops_state["running"]:
            try:
                n = load_deltas() or 0
            except Exception:
                n = 0
        # keep draining fast while catching up; relax once up to date
        time.sleep(3 if n > 0 else 60)


def wipe_graph():
    uri, user, pw = neo4j_env()
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        with drv.session() as s:
            try:
                s.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS").consume()
            except Exception:
                s.run("MATCH (n) DETACH DELETE n").consume()
    finally:
        drv.close()


def rebuild_worker():
    uri, user, pw = neo4j_env()
    if not uri:
        ops_state.update(running=False, label="Rebuild", result="no NEO4J_* env vars set")
        return
    if ops_state["running"]:
        ops_state.update(result="busy — another operation is already running")
        return
    # Claim the op first so auto-load won't kick off a new load, then wait for any
    # load already in flight to finish (up to ~3 min) before we wipe.
    ops_state.update(running=True, label="Rebuild", result="waiting for in-flight load to finish…")
    waited = 0
    while load_state["running"] and waited < 180:
        time.sleep(1)
        waited += 1
    ops_state.update(result="wiping graph…")
    try:
        wipe_graph()
        ops_state.update(result="applying schema…")
        with open(log_path("_ops"), "a", encoding="utf-8") as logf:
            logf.write("\n=== Rebuild: schema ===\n")
            subprocess.run([sys.executable, os.path.join(HERE, "init_schema.py"),
                            "--uri", uri, "--user", user, "--password", pw, "--insecure"],
                           cwd=HERE, stdout=logf, stderr=subprocess.STDOUT, **hidden())
        ops_state.update(result="loading data…")
        load_offsets.clear()             # graph was wiped -> load everything again
        load_counts.clear()
        _save_offsets()
        load_deltas()                    # incremental from offset 0 = full load
        ops_state.update(running=False, result="Rebuild complete ✓")
    except Exception as e:
        load_state.update(running=False)
        ops_state.update(running=False, result="Rebuild failed: " + str(e).splitlines()[0][:120])


def run_script(script, label):
    uri, user, pw = neo4j_env()
    if not uri:
        ops_state.update(running=False, label=label, result="no NEO4J_* env vars set")
        return
    if ops_state["running"]:
        return
    ops_state.update(running=True, label=label, result="")
    proc = subprocess.run([sys.executable, os.path.join(HERE, script),
                           "--uri", uri, "--user", user, "--password", pw, "--insecure"],
                          cwd=HERE, capture_output=True, text=True, **hidden())
    out = (proc.stdout or "") + (proc.stderr or "")
    with open(log_path("_ops"), "a", encoding="utf-8") as logf:
        logf.write(f"\n=== {label} ===\n{out}\n")
    lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    result = lines[-1] if lines else ("done" if proc.returncode == 0 else "failed")
    ops_state.update(running=False, result=result[:160])


# ---------------- HTTP ----------------

def build_status():
    update_distinct()
    cards = []
    for code, name, seed in ASSOCIATIONS:
        st = read_status(code) or {}
        # Judge "running" by a live handle OR a fresh heartbeat (an orphan crawler
        # from a previous dashboard session). Never trust the file's saved state,
        # which is stale after a kill. An explicit Stop wins immediately.
        handle = procs.get(code)
        alive = bool(handle and handle.poll() is None)
        if alive:
            stopped.discard(code)
            state = "running"
        elif code in stopped:
            state = "idle"
        elif handle:
            state = "stopped"
        elif heartbeat_fresh(st):
            state = "running"
        else:
            state = "idle"
        records = count_lines(data_path(code))                      # raw crawl lines
        loadable = len(_distinct.get(code, set()))                  # unique, non-steer regs
        written_real = int((neo_stats.get("by_assoc") or {}).get(code, 0))  # non-steer regs in Neo4j
        written = min(written_real, loadable)                       # never render backwards
        remaining = max(loadable - written_real, 0)
        cards.append({
            "code": code, "name": name,
            "state": state,
            "current": st.get("current"),
            "counts": st.get("counts", {}),
            "pct": st.get("pct", 0),
            "rate": st.get("rate_per_min", 0),
            "records": records,
            "written": written,
            "loadable": loadable,
            "remaining": remaining,
        })
    tot = sum(c["loadable"] for c in cards)
    wrt = sum(c["written"] for c in cards)
    load_progress = {"written": wrt, "total": tot, "remaining": max(tot - wrt, 0),
                     "pct": round(min(wrt / tot * 100, 100), 1) if tot else 0}
    return {"cards": cards, "neo": neo_stats, "load": load_state, "ops": ops_state,
            "auto_load": auto_load["on"], "load_progress": load_progress}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self._json(build_status())
        elif path == "/api/graph":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0].strip()
            self._json(neo_graph(q) if q else {"root": None, "nodes": [], "edges": []})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        assoc = (q.get("assoc", [""])[0]).upper()
        if u.path == "/api/start":
            start(assoc)
        elif u.path == "/api/stop":
            stop(assoc)
        elif u.path == "/api/startall":
            for c, _, _ in ASSOCIATIONS:
                start(c)
        elif u.path == "/api/stopall":
            for c, _, _ in ASSOCIATIONS:
                stop(c)
        elif u.path == "/api/load":
            if not load_state["running"]:
                threading.Thread(target=load_deltas, daemon=True).start()
        elif u.path == "/api/autoload":
            auto_load["on"] = not auto_load["on"]
        elif u.path == "/api/schema":
            threading.Thread(target=lambda: run_script("init_schema.py", "Apply schema"), daemon=True).start()
        elif u.path == "/api/reconcile":
            threading.Thread(target=lambda: run_script("reconcile.py", "Reconcile"), daemon=True).start()
        elif u.path == "/api/rebuild":
            threading.Thread(target=rebuild_worker, daemon=True).start()
        elif u.path == "/api/remove-steers":
            threading.Thread(target=remove_steers, daemon=True).start()
        elif u.path == "/api/seed":
            reg = q.get("reg", [""])[0].strip()
            if assoc and reg:
                threading.Thread(target=lambda: add_seed(assoc, reg), daemon=True).start()
        else:
            self.send_response(404); self.end_headers(); return
        self._json({"ok": True})


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cattle Pipeline</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#2a3140;
    --txt:#e6edf3; --dim:#8b949e; --accent:#3b82f6;
    --free:#22c55e; --carrier:#ef4444; --suspect:#f59e0b; --unknown:#6b7280;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px}
  header{position:sticky;top:0;background:rgba(13,17,23,.9);backdrop-filter:blur(6px);
    border-bottom:1px solid var(--line);padding:14px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;z-index:5}
  header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
  .spacer{flex:1}
  button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    padding:7px 12px;border-radius:8px;cursor:pointer;font-size:13px}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button.ghost{background:transparent}
  main{padding:20px;max-width:1200px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
  @media(max-width:760px){.grid{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;
    display:flex;flex-direction:column;gap:12px}
  .card .top{display:flex;align-items:center;gap:10px}
  .card h2{font-size:15px;margin:0;font-weight:600}
  .badge{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
  .badge.running{color:#c6f6d5;border-color:#22c55e55;background:#22c55e18}
  .rate{margin-left:auto;color:var(--dim);font-size:12px}
  .bar{height:8px;background:var(--panel2);border-radius:999px;overflow:hidden}
  .bar > i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);width:0%}
  .animal{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px;min-height:78px}
  .animal .reg{color:var(--accent);font-variant-numeric:tabular-nums;font-weight:600}
  .animal .nm{font-weight:600;margin-left:6px}
  .animal .row{color:var(--dim);font-size:12.5px;margin-top:4px}
  .chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
  .chip{font-size:10.5px;padding:2px 7px;border-radius:999px;border:1px solid var(--line)}
  .Free{color:var(--free);border-color:#22c55e55}
  .Carrier{color:var(--carrier);border-color:#ef444455}
  .Suspect{color:var(--suspect);border-color:#f59e0b55}
  .Unknown{color:var(--unknown)}
  .stats{display:flex;gap:16px;color:var(--dim);font-size:12px}
  .stats b{color:var(--txt)}
  .card .controls{display:flex;gap:8px}
  .neo{margin-top:22px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
  .neo h3{margin:0 0 12px;font-size:14px;font-weight:600}
  .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  @media(max-width:760px){.tiles{grid-template-columns:repeat(2,1fr)}}
  .tile{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px}
  .tile .k{color:var(--dim);font-size:12px}
  .tile .v{font-size:22px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
  .dist{display:flex;height:14px;border-radius:999px;overflow:hidden;margin:14px 0 6px;border:1px solid var(--line)}
  .dist > span{display:block;height:100%}
  .legend{display:flex;gap:16px;color:var(--dim);font-size:12px;flex-wrap:wrap}
  .dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
  .loadbar{margin-top:14px;color:var(--dim);font-size:13px}
  .note{color:var(--dim);font-size:12px;margin-top:8px}
  .warn{color:var(--suspect)}
  .ok{color:var(--free)}
  #conn{width:11px;height:11px;border-radius:50%;background:#6b7280;display:inline-block;
    margin-left:4px;transition:background .3s}
  #conn.up{background:var(--free);box-shadow:0 0 8px #22c55eaa}
  #conn.down{background:var(--carrier);box-shadow:0 0 8px #ef4444aa}
  #conn-label{color:var(--dim);font-size:12px;margin-right:6px}
  .ge{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
  .ge input{flex:1;min-width:200px;background:var(--panel2);border:1px solid var(--line);
    color:var(--txt);border-radius:8px;padding:8px 10px;font-size:13px}
  #graph{height:480px;background:var(--panel2);border:1px solid var(--line);border-radius:10px}
  .seedbar{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap;
    background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .seedbar select,.seedbar input{background:var(--panel2);border:1px solid var(--line);
    color:var(--txt);border-radius:8px;padding:7px 10px;font-size:13px}
  .seedbar input{min-width:200px}
  .seedbar .lbl{color:var(--dim);font-size:13px}
</style>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
</head>
<body>
<header>
  <h1>🐂 Cattle Pipeline</h1>
  <span id="conn" title="Neo4j"></span>
  <span id="conn-label">Neo4j</span>
  <button class="primary" onclick="post('/api/startall')">Start all</button>
  <button onclick="post('/api/stopall')">Stop all</button>
  <div class="spacer"></div>
  <button class="ghost" onclick="post('/api/schema')">Apply schema</button>
  <button class="ghost" onclick="post('/api/load')">Load new now</button>
  <button class="ghost" id="autobtn" onclick="post('/api/autoload')">Auto-load: on</button>
  <button class="ghost" onclick="post('/api/reconcile')">Reconcile</button>
  <button class="ghost" onclick="if(confirm('Delete all steers (castrated males) from the graph? New crawls/loads already skip them.'))post('/api/remove-steers')">Remove steers</button>
  <button onclick="if(confirm('Wipe the graph, re-apply the schema, and reload every data_*.jsonl? This deletes the current graph and rebuilds it clean.'))post('/api/rebuild')">Rebuild graph</button>
</header>
<main>
  <div class="seedbar">
    <span class="lbl">Add seed to queue:</span>
    <select id="seed-assoc">
      <option value="CHIA">Chianina</option>
      <option value="MAINE">Maine-Anjou</option>
      <option value="SHORT">Shorthorn</option>
      <option value="ANGUS">Angus</option>
    </select>
    <input id="seed-reg" placeholder="registration # (e.g. MA430053)"
           onkeydown="if(event.key==='Enter')addSeed()">
    <button class="primary" onclick="addSeed()">Add to queue</button>
    <span id="seed-note" class="note"></span>
  </div>
  <div class="grid" id="grid"></div>
  <div class="neo" id="neo"></div>
  <div class="neo">
    <h3>Pedigree explorer</h3>
    <div class="ge">
      <input id="gq" placeholder="animal name, uid (CHIA:337003), or registration number"
             onkeydown="if(event.key==='Enter')loadGraph()">
      <button class="primary" onclick="loadGraph()">Show pedigree</button>
      <span id="gnote" class="note"></span>
    </div>
    <div id="graph"></div>
  </div>
</main>
<script>
async function post(url){ await fetch(url,{method:'POST'}); tick(); }
async function addSeed(){
  const a=document.getElementById('seed-assoc').value;
  const r=document.getElementById('seed-reg').value.trim();
  const note=document.getElementById('seed-note');
  if(!r){ note.textContent='enter a registration number'; return; }
  note.textContent='queuing…';
  await fetch('/api/seed?assoc='+encodeURIComponent(a)+'&reg='+encodeURIComponent(r),{method:'POST'});
  note.textContent='queued '+r+' → '+a+' (added to pending)';
  document.getElementById('seed-reg').value='';
}
async function ctl(url,code){ await fetch(url+'?assoc='+code,{method:'POST'}); tick(); }
function esc(s){ return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function card(c){
  const cur=c.current;
  const cnt=c.counts||{};
  let animal='<div class="row">— waiting —</div>';
  if(cur){
    const chips=(cur.defects||[]).filter(d=>d.status&&d.status!=='Unknown')
      .map(d=>`<span class="chip ${esc(d.status)}">${esc(d.code)} ${esc(d.status)}</span>`).join('');
    animal=`<div><span class="reg">${esc(cur.reg)}</span><span class="nm">${esc(cur.name)||''}</span></div>
      <div class="row">color: ${esc(cur.color)||'—'} &nbsp;·&nbsp; dob: ${esc(cur.dob)||'—'} &nbsp;·&nbsp; sex: ${esc(cur.sex)||'—'}</div>
      <div class="chips">${chips}</div>`;
  }
  const isRun=c.state==='running';
  return `<div class="card">
    <div class="top">
      <h2>${esc(c.name)}</h2>
      <span class="badge ${isRun?'running':''}">${esc(c.state)}</span>
      <span class="rate">${isRun?(c.rate||0)+'/min':''}</span>
    </div>
    <div class="bar"><i style="width:${Math.min(c.pct||0,100)}%"></i></div>
    <div class="animal">${animal}</div>
    <div class="stats">
      <span>done <b>${cnt.done||0}</b></span>
      <span>pending <b>${cnt.pending||0}</b></span>
      <span>failed <b>${cnt.failed||0}</b></span>
      <span>records <b>${c.records||0}</b></span>
    </div>
    <div class="stats" style="opacity:.85">
      <span>→ Neo4j <b>${c.written||0}</b> / ${c.loadable||0}</span>
      <span><b>${c.remaining||0}</b> to write</span>
    </div>
    <div class="controls">
      ${isRun?`<button onclick="ctl('/api/stop','${c.code}')">Stop</button>`
             :`<button class="primary" onclick="ctl('/api/start','${c.code}')">Start</button>`}
    </div>
  </div>`;
}

function neoPanel(n,load,ops,lp){
  const d=n.defects||{};
  const order=['Free','Carrier','Suspect','Unknown'];
  const tot=order.reduce((a,k)=>a+(d[k]||0),0)||1;
  const seg=order.map(k=>`<span class="${k}" style="width:${(d[k]||0)/tot*100}%;background:var(--${k.toLowerCase()})"></span>`).join('');
  const leg=order.map(k=>`<span><span class="dot" style="background:var(--${k.toLowerCase()})"></span>${k}: <b>${d[k]||0}</b></span>`).join('');
  let loadline='';
  if(load.running) loadline=`<div class="loadbar">Loading ${esc(load.current)} (${load.index}/${load.total})…</div>`;
  else if(load.done_msg) loadline=`<div class="loadbar ok">${esc(load.done_msg)}</div>`;
  let opsline='';
  if(ops && ops.running) opsline=`<div class="loadbar">⏳ ${esc(ops.label)} running…</div>`;
  else if(ops && ops.result) opsline=`<div class="loadbar ok">${esc(ops.label)}: ${esc(ops.result)}</div>`;
  const dupClass=(n.dup_groups>0)?'warn':'ok';
  const dupMsg=n.ok?(n.dup_groups>0?`⚠ ${n.dup_groups} duplicate group(s)`:'✓ no duplicate groups'):'';
  lp = lp || {written:0,total:0,remaining:0,pct:0};
  const prog = `<div class="loadbar">Written to Neo4j: <b>${lp.written.toLocaleString()}</b> / ${lp.total.toLocaleString()} records
      &nbsp;·&nbsp; <b>${lp.remaining.toLocaleString()}</b> remaining (${lp.pct}%)</div>
    <div class="bar" style="margin-top:6px"><i style="width:${Math.min(lp.pct,100)}%"></i></div>`;
  return `<h3>Graph (Neo4j) ${n.ok?'':'<span class="note">— '+esc(n.note)+'</span>'}</h3>
    ${prog}
    <div class="tiles" style="margin-top:14px">
      <div class="tile"><div class="k">Animals</div><div class="v">${n.animals||0}</div></div>
      <div class="tile"><div class="k">Registrations</div><div class="v">${n.registrations||0}</div></div>
      <div class="tile"><div class="k">In &gt;1 association</div><div class="v">${n.multi_assoc||0}</div></div>
      <div class="tile"><div class="k ${dupClass}">Duplicate groups</div><div class="v ${dupClass}">${n.dup_groups||0}</div></div>
    </div>
    <div class="dist">${seg}</div>
    <div class="legend">${leg}</div>
    <div class="note ${dupClass}">${dupMsg}</div>
    ${loadline}${opsline}`;
}

async function tick(){
  try{
    const r=await fetch('/api/status'); const s=await r.json();
    document.getElementById('grid').innerHTML=s.cards.map(card).join('');
    document.getElementById('neo').innerHTML=neoPanel(s.neo,s.load,s.ops,s.load_progress);
    const ab=document.getElementById('autobtn');
    if(ab){ ab.textContent='Auto-load: '+(s.auto_load?'on':'off'); ab.classList.toggle('primary',!!s.auto_load); }
    const conn=document.getElementById('conn'), cl=document.getElementById('conn-label');
    const up=!!(s.neo&&s.neo.ok);
    if(conn){ conn.className=up?'up':'down'; conn.title='Neo4j: '+(up?'connected':((s.neo&&s.neo.note)||'disconnected')); }
    if(cl){ cl.textContent=up?'Neo4j connected':'Neo4j disconnected'; }
  }catch(e){}
}
let gnet=null;
async function loadGraph(){
  const q=document.getElementById('gq').value.trim();
  const note=document.getElementById('gnote');
  if(!q){ note.textContent='enter an animal'; return; }
  note.textContent='loading…';
  try{
    const r=await fetch('/api/graph?q='+encodeURIComponent(q));
    const g=await r.json();
    if(g.error){ note.textContent=g.error; return; }
    if(!g.nodes || !g.nodes.length){ note.textContent='no match'; return; }
    note.textContent=g.nodes.length+' animals · '+g.edges.length+' links (double-click a node to recenter)';
    const col=s=>({M:'#3b82f6',F:'#ef4444'}[(s||'').toUpperCase()]||'#6b7280');
    const nodes=g.nodes.map(n=>({
      id:n.uid, label:(n.name||n.uid),
      shape:'dot', size:(n.uid===g.root?20:12),
      color:{background:col(n.sex), border:(n.uid===g.root?'#e6edf3':col(n.sex))},
      font:{color:'#e6edf3',size:12}}));
    const edges=g.edges.map(e=>({from:e.from,to:e.to,arrows:'to',
      color:{color:(e.type==='SIRE_OF'?'#3b82f6':'#ef4444')}, width:1.3}));
    const data={nodes:new vis.DataSet(nodes),edges:new vis.DataSet(edges)};
    const opts={physics:{stabilization:true,barnesHut:{springLength:130,gravitationalConstant:-4000}},
      interaction:{hover:true},nodes:{borderWidth:2},edges:{smooth:{type:'cubicBezier'}}};
    gnet=new vis.Network(document.getElementById('graph'),data,opts);
    gnet.on('doubleClick',p=>{ if(p.nodes.length){ document.getElementById('gq').value=p.nodes[0]; loadGraph(); }});
  }catch(e){ note.textContent='error: '+e; }
}
tick(); setInterval(tick,1500);
</script>
</body></html>"""


def main():
    global OFFSETS_FILE
    OFFSETS_FILE = os.path.join(HERE, "load_offsets.json")
    _load_offsets()
    load_neo4j_creds_file()
    print(f"Neo4j target: {os.environ.get('NEO4J_URI', '<none — set NEO4J_* or drop a creds file>')}")
    threading.Thread(target=neo_refresh_loop, daemon=True).start()
    threading.Thread(target=auto_load_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    ip = lan_ip()
    print("=" * 56)
    print(f"  Dashboard (this computer): http://localhost:{PORT}")
    print(f"  On your phone (same WiFi): http://{ip}:{PORT}")
    print("=" * 56)
    print("Ctrl+C to stop.  (If the phone can't connect, allow Python")
    print("through Windows Firewall on Private networks — see notes.)")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        for c in list(procs):
            stop(c)


if __name__ == "__main__":
    main()
