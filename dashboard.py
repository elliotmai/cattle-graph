"""
Cattle crawl dashboard — a lightweight Tkinter UI to run and monitor everything.

    python dashboard.py

Tkinter ships with standard Python, so there's nothing to install (the neo4j
driver is only needed for the graph-stats panel / load buttons). The dashboard:
  * starts/stops one crawler process per association (hidden, logs to file),
  * shows live pending/done/failed/skipped counts + crawl rate per association,
  * shows a totals row and a live graph-stats panel (animals, registrations,
    animals in >1 association, and duplicate groups — your dedup health check),
  * one-click Apply schema / Load to Neo4j / Reconcile / Retry failed,
  * tails logs.

It only drives crawl.py / loader.py / init_schema.py / reconcile.py — no new
logic lives here.
"""

from __future__ import annotations

import os
import sys
import time
import sqlite3
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))

ASSOCIATIONS = [
    ("CHIA",  "Chianina",    "MA430053"),
    ("MAINE", "Maine-Anjou", "402303"),
    ("SHORT", "Shorthorn",   "3790685"),
    ("ANGUS", "Angus",       "13054003"),
]

REFRESH_MS = 2000
GRAPH_REFRESH_MS = 15000


def frontier_path(code): return os.path.join(HERE, f"frontier_{code}.db")
def data_path(code):     return os.path.join(HERE, f"data_{code}.jsonl")
def log_path(name):      return os.path.join(HERE, f"crawl_{name}.log")


def read_counts(code):
    db = frontier_path(code)
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute("SELECT status, COUNT(*) FROM queue GROUP BY status").fetchall()
        finally:
            con.close()
        return {s: c for s, c in rows}
    except Exception:
        return None


def count_lines(path, cache):
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (st.st_mtime, st.st_size)
    if cache.get("key") == key:
        return cache.get("n", 0)
    n = 0
    try:
        with open(path, "rb") as f:
            for _ in f:
                n += 1
    except OSError:
        return cache.get("n", 0)
    cache["key"] = key
    cache["n"] = n
    return n


def tail(path, lines=200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


def neo4j_env():
    """(uri, user, password) from env, with +s -> +ssc so it works through the
    TLS-intercepting network (matches the loader's --insecure)."""
    uri = os.environ.get("NEO4J_URI")
    if uri:
        uri = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
    return uri, os.environ.get("NEO4J_USERNAME", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")


GRAPH_QUERIES = {
    "animals": "MATCH (a:Animal) RETURN count(a) AS n",
    "registrations": "MATCH (:Registration) RETURN count(*) AS n",
    "multi_assoc": (
        "MATCH (a:Animal)-[:HAS_REGISTRATION]->(r) "
        "WITH a, count(DISTINCT r.association) AS c WHERE c > 1 RETURN count(a) AS n"
    ),
    # genuine duplicate groups only (excludes breed/foundation placeholders)
    "dup_groups": (
        "MATCH (a:Animal) WHERE a.name_norm CONTAINS ' ' AND NOT a.name_norm CONTAINS ' X ' "
        "AND a.dob IS NOT NULL AND NOT a.dob ENDS WITH '-01-01' "
        "WITH a.name_norm AS nn, a.dob AS d, count(*) AS c WHERE c > 1 RETURN count(*) AS n"
    ),
}


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cattle Crawl Dashboard")
        self.geometry("1040x720")
        self.minsize(900, 600)

        self.procs: dict[str, subprocess.Popen] = {}
        self.rows: dict[str, dict] = {}
        self.line_cache = {c: {} for c, _, _ in ASSOCIATIONS}
        self.prev_done: dict[str, tuple[int, float]] = {}

        self._build_settings()
        self._build_table()
        self._build_controls()
        self._build_graph_stats()
        self._build_log()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(500, self.refresh)
        self.after(1500, self.refresh_graph_stats)

    # ---------------- UI ----------------
    def _build_settings(self):
        f = ttk.LabelFrame(self, text="Settings")
        f.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(f, text="Contact (User-Agent):").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.contact = tk.StringVar(value=os.environ.get("CRAWL_CONTACT", "eli.mai12932@gmail.com"))
        ttk.Entry(f, textvariable=self.contact, width=30).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(f, text="Delay (s):").grid(row=0, column=2, sticky="e", padx=6)
        self.delay = tk.StringVar(value="1.5")
        ttk.Spinbox(f, from_=0.5, to=30, increment=0.5, width=6,
                    textvariable=self.delay).grid(row=0, column=3, sticky="w", padx=6)
        self.ignore_robots = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Ignore robots.txt (authorized)",
                        variable=self.ignore_robots).grid(row=0, column=4, sticky="w", padx=12)

    def _build_table(self):
        f = ttk.LabelFrame(self, text="Associations")
        f.pack(fill="x", padx=10, pady=4)
        self.table = f
        headers = ["Association", "Seed", "State", "Rate/min",
                   "Pending", "Done", "Failed", "Skipped", "Records", ""]
        for j, h in enumerate(headers):
            ttk.Label(f, text=h, font=("", 9, "bold")).grid(row=0, column=j, padx=6, pady=4, sticky="w")

        for i, (code, name, seed) in enumerate(ASSOCIATIONS, start=1):
            r = {}
            ttk.Label(f, text=f"{name} ({code})").grid(row=i, column=0, sticky="w", padx=6, pady=3)
            r["seed"] = tk.StringVar(value=seed)
            ttk.Entry(f, textvariable=r["seed"], width=11).grid(row=i, column=1, padx=6)
            r["state"] = ttk.Label(f, text="idle", width=9)
            r["state"].grid(row=i, column=2, sticky="w", padx=6)
            r["rate"] = ttk.Label(f, text="-", width=8)
            r["rate"].grid(row=i, column=3, sticky="w", padx=6)
            for j, key in enumerate(["pending", "done", "failed", "skipped", "records"], start=4):
                lbl = ttk.Label(f, text="-", width=8)
                lbl.grid(row=i, column=j, sticky="w", padx=6)
                r[key] = lbl
            bf = ttk.Frame(f)
            bf.grid(row=i, column=9, padx=6)
            r["start"] = ttk.Button(bf, text="Start", width=6, command=lambda c=code: self.start(c))
            r["start"].pack(side="left")
            r["stop"] = ttk.Button(bf, text="Stop", width=6, state="disabled",
                                   command=lambda c=code: self.stop(c))
            r["stop"].pack(side="left", padx=(4, 0))
            self.rows[code] = r

        # totals row
        tr = len(ASSOCIATIONS) + 1
        ttk.Separator(f, orient="horizontal").grid(row=tr, column=0, columnspan=10, sticky="ew", pady=2)
        ttk.Label(f, text="Total", font=("", 9, "bold")).grid(row=tr + 1, column=0, sticky="w", padx=6)
        self.tot = {}
        for j, key in enumerate(["pending", "done", "failed", "skipped", "records"], start=4):
            lbl = ttk.Label(f, text="-", width=8, font=("", 9, "bold"))
            lbl.grid(row=tr + 1, column=j, sticky="w", padx=6)
            self.tot[key] = lbl

    def _build_controls(self):
        f = ttk.Frame(self)
        f.pack(fill="x", padx=10, pady=6)
        ttk.Button(f, text="Start all", command=self.start_all).pack(side="left")
        ttk.Button(f, text="Stop all", command=self.stop_all).pack(side="left", padx=6)
        ttk.Separator(f, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(f, text="Apply schema", command=self.apply_schema).pack(side="left")
        ttk.Button(f, text="Load to Neo4j", command=self.load_neo4j).pack(side="left", padx=6)
        ttk.Button(f, text="Reconcile dups", command=self.reconcile).pack(side="left")
        ttk.Button(f, text="Retry failed", command=self.retry_failed).pack(side="left", padx=6)
        self.status = ttk.Label(f, text="ready")
        self.status.pack(side="right")

    def _build_graph_stats(self):
        f = ttk.LabelFrame(self, text="Graph (Neo4j)")
        f.pack(fill="x", padx=10, pady=4)
        self.gs = {}
        labels = [("animals", "Animals"), ("registrations", "Registrations"),
                  ("multi_assoc", "In >1 association"), ("dup_groups", "Duplicate groups")]
        for j, (key, text) in enumerate(labels):
            ttk.Label(f, text=text + ":").grid(row=0, column=j * 2, sticky="e", padx=(12, 2), pady=6)
            self.gs[key] = ttk.Label(f, text="-", width=10, font=("", 10, "bold"))
            self.gs[key].grid(row=0, column=j * 2 + 1, sticky="w")
        ttk.Button(f, text="Refresh", command=self.refresh_graph_stats).grid(
            row=0, column=len(labels) * 2, padx=10)
        self.gs_note = ttk.Label(f, text="")
        self.gs_note.grid(row=1, column=0, columnspan=9, sticky="w", padx=12)

    def _build_log(self):
        f = ttk.LabelFrame(self, text="Log")
        f.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="View:").pack(side="left", padx=6, pady=4)
        self.log_choice = tk.StringVar(value=ASSOCIATIONS[0][0])
        opts = [c for c, _, _ in ASSOCIATIONS] + ["_ops", "_load"]
        ttk.OptionMenu(top, self.log_choice, ASSOCIATIONS[0][0], *opts).pack(side="left")
        self.log_text = tk.Text(f, height=12, wrap="none", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    # ---------------- process control ----------------
    def _hidden(self):
        return {"creationflags": 0x08000000} if os.name == "nt" else {}

    def _crawl_cmd(self, code, seed):
        cmd = [sys.executable, os.path.join(HERE, "crawl.py"),
               "--association", code, "--seed", seed,
               "--out", data_path(code), "--db", frontier_path(code),
               "--reset-skipped", "--delay", self.delay.get(),
               "--user-agent", f"cattle-graph-crawler/1.0 (authorized; contact: {self.contact.get()})",
               "-v"]
        if self.ignore_robots.get():
            cmd.append("--ignore-robots")
        return cmd

    def start(self, code):
        if self.procs.get(code) and self.procs[code].poll() is None:
            return
        seed = self.rows[code]["seed"].get().strip()
        if not seed:
            messagebox.showwarning("Missing seed", f"Enter a seed for {code}.")
            return
        logf = open(log_path(code), "a", encoding="utf-8")
        try:
            self.procs[code] = subprocess.Popen(
                self._crawl_cmd(code, seed), cwd=HERE, stdout=logf,
                stderr=subprocess.STDOUT, **self._hidden())
        except Exception as e:
            messagebox.showerror("Failed to start", str(e))
            return
        self.status.config(text=f"started {code}")

    def stop(self, code):
        p = self.procs.get(code)
        if p and p.poll() is None:
            p.terminate()
            self.status.config(text=f"stopping {code}…")

    def start_all(self):
        for code, _, _ in ASSOCIATIONS:
            self.start(code)

    def stop_all(self):
        for code, _, _ in ASSOCIATIONS:
            self.stop(code)

    def retry_failed(self):
        for code, _, _ in ASSOCIATIONS:
            if not os.path.exists(frontier_path(code)):
                continue
            try:
                subprocess.Popen(
                    [sys.executable, os.path.join(HERE, "crawl.py"),
                     "--db", frontier_path(code), "--retry-failed", "--add-seeds-only"],
                    cwd=HERE, **self._hidden())
            except Exception:
                pass
        self.status.config(text="re-queued failed items")

    # ---------------- one-shot script runners (Neo4j) ----------------
    def _run_script(self, argv, label, confirm=None):
        uri, user, pw = neo4j_env()
        if not uri:
            messagebox.showwarning("Neo4j not configured",
                                   "Set NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD "
                                   "in this shell before launching the dashboard.")
            return
        if confirm and not messagebox.askyesno(label, confirm):
            return
        full = [sys.executable, os.path.join(HERE, argv[0]), *argv[1:],
                "--uri", uri, "--user", user, "--password", pw, "--insecure"]

        def worker():
            self.after(0, lambda: self.status.config(text=f"{label}…"))
            with open(log_path("_ops"), "a", encoding="utf-8") as logf:
                logf.write(f"\n=== {label} ===\n"); logf.flush()
                subprocess.run(full, cwd=HERE, stdout=logf,
                               stderr=subprocess.STDOUT, **self._hidden())
            self.after(0, lambda: self.status.config(text=f"{label} done (see log '_ops')"))
            self.after(0, self.refresh_graph_stats)

        threading.Thread(target=worker, daemon=True).start()

    def apply_schema(self):
        self._run_script(["init_schema.py"], "Apply schema")

    def reconcile(self):
        self._run_script(["reconcile.py"], "Reconcile duplicates",
                         confirm="Merge duplicate animals (real names sharing name+DOB)?\n"
                                 "Foundation/commercial placeholders are excluded.")

    def load_neo4j(self):
        uri, user, pw = neo4j_env()
        if not uri:
            messagebox.showwarning("Neo4j not configured", "Set NEO4J_* env vars first.")
            return
        files = [data_path(c) for c, _, _ in ASSOCIATIONS if os.path.exists(data_path(c))]
        if not files:
            self.status.config(text="no data_*.jsonl files yet")
            return

        def worker():
            for i, fp in enumerate(files, 1):
                self.after(0, lambda i=i, fp=fp: self.status.config(
                    text=f"loading {i}/{len(files)}: {os.path.basename(fp)}…"))
                with open(log_path("_load"), "a", encoding="utf-8") as logf:
                    subprocess.run(
                        [sys.executable, os.path.join(HERE, "loader.py"), fp, "--neo4j",
                         "--uri", uri, "--user", user, "--password", pw, "--insecure"],
                        cwd=HERE, stdout=logf, stderr=subprocess.STDOUT, **self._hidden())
            self.after(0, lambda: self.status.config(text="Neo4j load complete"))
            self.after(0, self.refresh_graph_stats)

        threading.Thread(target=worker, daemon=True).start()
        self.status.config(text="loading into Neo4j (sequential)…")

    # ---------------- graph stats ----------------
    def refresh_graph_stats(self):
        uri, user, pw = neo4j_env()
        if not uri:
            for k in self.gs:
                self.gs[k].config(text="n/a")
            self.gs_note.config(text="Set NEO4J_* env vars before launching to see graph stats.")
            self.after(GRAPH_REFRESH_MS, self.refresh_graph_stats)
            return

        def worker():
            try:
                from neo4j import GraphDatabase
                drv = GraphDatabase.driver(uri, auth=(user, pw))
                out = {}
                with drv.session() as s:
                    for key, q in GRAPH_QUERIES.items():
                        out[key] = s.run(q).single()["n"]
                drv.close()
                self.after(0, lambda: self._apply_graph_stats(out))
            except Exception as e:
                msg = str(e).splitlines()[0][:120]
                self.after(0, lambda: self.gs_note.config(text=f"graph query failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()
        self.after(GRAPH_REFRESH_MS, self.refresh_graph_stats)

    def _apply_graph_stats(self, out):
        for k, lbl in self.gs.items():
            lbl.config(text=str(out.get(k, "-")))
        dup = out.get("dup_groups", 0)
        self.gs_note.config(
            text=("✓ no duplicate groups" if dup == 0
                  else f"⚠ {dup} duplicate group(s) — click 'Reconcile dups'"))

    # ---------------- periodic refresh ----------------
    def refresh(self):
        totals = {k: 0 for k in ("pending", "done", "failed", "skipped", "records")}
        now = time.time()
        for code, _, _ in ASSOCIATIONS:
            r = self.rows[code]
            p = self.procs.get(code)
            running = bool(p and p.poll() is None)
            r["state"].config(text="running" if running else ("stopped" if p else "idle"))
            r["start"].config(state="disabled" if running else "normal")
            r["stop"].config(state="normal" if running else "disabled")

            counts = read_counts(code) or {}
            done = counts.get("done", 0)
            for key in ("pending", "done", "failed", "skipped"):
                v = counts.get(key, 0) if counts else 0
                r[key].config(text=str(v) if counts else "-")
                totals[key] += v
            recs = count_lines(data_path(code), self.line_cache[code])
            r["records"].config(text=str(recs))
            totals["records"] += recs

            # rate/min from done delta
            prev = self.prev_done.get(code)
            if prev and running:
                d_done, d_t = done - prev[0], now - prev[1]
                rate = (d_done / d_t * 60) if d_t > 0 else 0
                r["rate"].config(text=f"{rate:.0f}")
            elif not running:
                r["rate"].config(text="-")
            self.prev_done[code] = (done, now)

        for key, lbl in self.tot.items():
            lbl.config(text=str(totals[key]))

        sel = self.log_choice.get()
        content = tail(log_path(sel))
        if content != self.log_text.get("1.0", "end-1c"):
            at_bottom = self.log_text.yview()[1] > 0.99
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", content)
            if at_bottom:
                self.log_text.see("end")

        self.after(REFRESH_MS, self.refresh)

    def on_close(self):
        alive = [c for c, p in self.procs.items() if p and p.poll() is None]
        if alive and not messagebox.askyesno(
                "Crawlers running", f"{len(alive)} crawler(s) still running. Stop and quit?"):
            return
        for p in self.procs.values():
            try:
                if p and p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    Dashboard().mainloop()
