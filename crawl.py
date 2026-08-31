"""
Recursive DigitalBeef crawler with a resumable, injectable frontier.

Crawls outward from seed registration numbers, following pedigree (ancestor) and
progeny (descendant) links, until the frontier is empty. The frontier lives in
SQLite so you can stop/resume at any time, and you can inject NEW registration
numbers later to start another recursive run that fills gaps — already-crawled
animals are skipped automatically, new ones are pulled in and (via loader.py)
merged/deduped onto the existing graph.

Examples
--------
# Start a run from two seeds, write records to JSONL:
python crawl.py --association CHIA --seed MA430053 --seed MA555000 --out records.jsonl

# Resume the same run later (state is in crawl.db):
python crawl.py --out records.jsonl

# Fill gaps: add new seeds to the existing frontier and keep going:
python crawl.py --seed SHORT:3790685 --seed MAINE:402303 --out records.jsonl

# Just inject seeds without crawling (queue them for later):
python crawl.py --seed CHIA:MA430053 --add-seeds-only

# Retry everything that previously failed:
python crawl.py --retry-failed --out records.jsonl

# Systematic sweep of an association by registration-number range
# (the reliable way to get EVERY animal, not just connected pedigrees):
python crawl.py --association CHIA --enumerate 400000 460000 --out records.jsonl

# Load straight into Neo4j as it crawls (in addition to JSONL):
python crawl.py --out records.jsonl --neo4j --uri bolt://localhost:7687 \
    --user neo4j --password secret
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import digitalbeef as db
import angus
from loader import norm_defect_status

log = logging.getLogger("crawl")

# Exit code for "the host is refusing us". The systemd unit pairs this with
# RestartPreventExitStatus, so a blocked crawler stays down instead of being
# restarted into the same wall every RestartSec.
EXIT_BLOCKED = 75


def classify_record(record: dict) -> dict:
    """Normalize genetic-test results to Free/Carrier/Suspect/Unknown up front,
    so records are clean before they ever reach the database."""
    for d in record.get("defects") or []:
        d["status"] = norm_defect_status(d.get("status"))
    return record


def _summarize(assoc: str, reg: str, record: dict) -> dict:
    return {
        "association": assoc, "reg": reg,
        "name": record.get("name"), "color": record.get("color"),
        "dob": record.get("dob"), "sex": record.get("sex"),
        "defects": [{"code": d.get("code"), "status": d.get("status")}
                    for d in (record.get("defects") or [])],
    }


def write_status(path, association, state, current, counts, processed, start_ts):
    """Write a small JSON heartbeat the dashboard reads."""
    if not path:
        return
    done, pending = counts.get("done", 0), counts.get("pending", 0)
    total = done + pending
    elapsed = max(time.time() - start_ts, 1e-6)
    payload = {
        "association": association, "state": state, "current": current,
        "counts": counts, "processed": processed,
        "rate_per_min": round(processed / elapsed * 60, 1),
        "pct": round(done / total * 100, 1) if total else 0.0,
        "pid": os.getpid(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass


def scrape_dispatch(assoc, reg, session, args):
    """Route to the right platform adapter and return (url, record, neighbors).
    ANGUS uses the angus.org adapter; everything else is DigitalBeef."""
    if assoc.upper() == "ANGUS":
        url = angus.representative_url(reg)
        record, neighbors = angus.scrape_animal(
            reg, session, timeout=args.timeout, save_html_dir=args.save_html)
        return url, record, neighbors
    url = db.animal_url(assoc, reg)
    record, neighbors = db.scrape_animal(
        assoc, reg, session,
        fetch_progeny=not args.no_progeny,
        fetch_epds=not args.no_epds,
        timeout=args.timeout,
        save_html_dir=args.save_html,
        delay=args.delay)
    return url, record, neighbors

DEFAULT_UA = ("cattle-graph-crawler/1.0 (research; contact: you@example.com) "
              "python-requests")


# --------------------------------------------------------------------------
# Resumable frontier (SQLite)
# --------------------------------------------------------------------------

class Frontier:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                association     TEXT NOT NULL,
                reg_number      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed|skipped
                depth           INTEGER NOT NULL DEFAULT 0,
                priority        INTEGER NOT NULL DEFAULT 0,        -- higher = crawled sooner
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                discovered_from TEXT,
                updated_at      TEXT,
                PRIMARY KEY (association, reg_number)
            )""")
        # migrate older frontier DBs that predate the priority column
        try:
            self.conn.execute("ALTER TABLE queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pick ON queue(status, priority, depth, attempts)")
        self.conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def add(self, association: str, reg: str, depth: int = 0, source: str = None,
            priority: int = 0) -> bool:
        """Queue an animal. Normally INSERT OR IGNORE (re-seeding a known animal
        is a no-op). When priority>0 (a hand-added 'crawl this next' seed) it
        upserts: a new item is inserted at that priority, and an already-pending
        item is bumped up so it jumps the queue — without disturbing anything else."""
        if priority > 0:
            cur = self.conn.execute(
                "INSERT INTO queue (association, reg_number, depth, priority, discovered_from, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(association, reg_number) DO UPDATE SET "
                "priority = MAX(queue.priority, excluded.priority)",
                (association.upper(), reg.strip(), depth, priority, source, self._now()))
        else:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO queue (association, reg_number, depth, priority, discovered_from, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (association.upper(), reg.strip(), depth, source, self._now()))
        self.conn.commit()
        return cur.rowcount > 0

    def add_many(self, items: list[tuple[str, str]], depth: int = 0, source: str = None,
                 priority: int = 0) -> int:
        added = 0
        for assoc, reg in items:
            if self.add(assoc, reg, depth, source, priority):
                added += 1
        return added

    def next_pending(self):
        row = self.conn.execute(
            "SELECT association, reg_number, depth FROM queue "
            "WHERE status='pending' ORDER BY priority DESC, depth ASC, attempts ASC LIMIT 1").fetchone()
        return row  # (assoc, reg, depth) or None

    def mark_done(self, association: str, reg: str) -> None:
        self.conn.execute(
            "UPDATE queue SET status='done', updated_at=? WHERE association=? AND reg_number=?",
            (self._now(), association.upper(), reg))
        self.conn.commit()

    def mark_skipped(self, association: str, reg: str) -> None:
        self.conn.execute(
            "UPDATE queue SET status='skipped', updated_at=? WHERE association=? AND reg_number=?",
            (self._now(), association.upper(), reg))
        self.conn.commit()

    def mark_failed(self, association: str, reg: str, error: str, max_attempts: int) -> None:
        self.conn.execute(
            "UPDATE queue SET attempts=attempts+1, last_error=?, updated_at=?, "
            "status=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END "
            "WHERE association=? AND reg_number=?",
            (error[:500], self._now(), max_attempts, association.upper(), reg))
        self.conn.commit()

    def retry_failed(self) -> int:
        cur = self.conn.execute(
            "UPDATE queue SET status='pending', attempts=0 WHERE status='failed'")
        self.conn.commit()
        return cur.rowcount

    def reset_skipped(self) -> int:
        cur = self.conn.execute(
            "UPDATE queue SET status='pending', attempts=0 WHERE status='skipped'")
        self.conn.commit()
        return cur.rowcount

    def park_foreign(self, association: str) -> int:
        """Mark every pending animal from another association as skipped.

        A pedigree cites registrations in other registries, and DigitalBeef
        serves ten of them, so following those neighbours walks the crawler out
        of the breed it was started for and into the next one. Parked rather
        than deleted: --reset-skipped puts them all back if the scope is ever
        widened again.
        """
        cur = self.conn.execute(
            "UPDATE queue SET status='skipped', updated_at=? "
            "WHERE status='pending' AND association <> ?",
            (self._now(), association.upper()))
        self.conn.commit()
        return cur.rowcount

    def pending_by_association(self) -> list[tuple[str, int]]:
        return self.conn.execute(
            "SELECT association, COUNT(*) FROM queue WHERE status='pending' "
            "GROUP BY association ORDER BY COUNT(*) DESC").fetchall()

    def counts(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) FROM queue GROUP BY status").fetchall()
        return {s: c for s, c in rows}


# --------------------------------------------------------------------------
# Output sink(s)
# --------------------------------------------------------------------------

class JsonlWriter:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")  # append -> resumable

    def write(self, record: dict) -> None:
        self.f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


# --------------------------------------------------------------------------
# Crawl loop
# --------------------------------------------------------------------------

def parse_seed(token: str, default_assoc: str) -> tuple[str, str]:
    """Accept 'CHIA:MA430053' or 'CHIA,MA430053' or bare 'MA430053' (uses --association)."""
    for sep in (":", ","):
        if sep in token:
            a, r = token.split(sep, 1)
            return a.strip().upper(), r.strip()
    if not default_assoc:
        raise SystemExit(f"Seed '{token}' has no association; pass --association or use ASSOC:REG.")
    return default_assoc.upper(), token.strip()


def run(args) -> int:
    frontier = Frontier(args.db)

    # --- seed injection (the "fill gaps" entry point) ---
    seeds: list[tuple[str, str]] = []
    for tok in args.seed or []:
        seeds.append(parse_seed(tok, args.association))
    if args.seeds_file:
        with open(args.seeds_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    seeds.append(parse_seed(line, args.association))
    if args.enumerate:
        start, end = args.enumerate
        if not args.association:
            raise SystemExit("--enumerate requires --association.")
        for n in range(start, end + 1):
            seeds.append((args.association.upper(), str(n)))
    if seeds:
        prio = 100 if args.front else 0
        added = frontier.add_many(seeds, depth=0, source="seed", priority=prio)
        log.info("Injected %d seed(s)%s.", len(seeds),
                 " at FRONT of the queue" if args.front else f" ({len(seeds) - added} already known)")

    if args.retry_failed:
        n = frontier.retry_failed()
        log.info("Reset %d failed item(s) to pending.", n)

    if args.reset_skipped:
        n = frontier.reset_skipped()
        log.info("Reset %d skipped item(s) to pending.", n)

    if args.skip_foreign:
        if not args.association:
            raise SystemExit("--skip-foreign requires --association.")
        n = frontier.park_foreign(args.association)
        log.info("Parked %d pending item(s) from other associations.", n)

    if args.add_seeds_only:
        log.info("Seeds queued. Frontier: %s", frontier.counts())
        return

    # --- outputs ---
    writer = JsonlWriter(args.out) if args.out else None
    loader = None
    if args.neo4j:
        from loader import Loader, Neo4jBackend  # reuse the loader's dedup + writes
        loader = Loader(Neo4jBackend(args.uri, args.user, args.password, insecure=args.insecure),
                        skip_steers=args.skip_steers)

    session = db.make_session(args.user_agent)
    robots = None if args.ignore_robots else db.RobotsCache(args.user_agent)

    status_assoc = (args.association or "").upper() or "MIXED"
    # None means "crawl whatever the frontier holds" -- the original behaviour.
    scope = status_assoc if (args.stay_in_association and args.association) else None
    if args.stay_in_association and not args.association:
        log.warning("--stay-in-association needs --association; crawling unscoped.")
    if scope:
        log.info("Scoped to %s. Pending by association: %s",
                 scope, dict(frontier.pending_by_association()))
    start_ts = time.time()
    last_current = None
    processed = 0
    blocked = 0                  # consecutive refusals
    final_state = "idle"
    write_status(args.status_file, status_assoc, "running", None, frontier.counts(), 0, start_ts)
    try:
        while True:
            row = frontier.next_pending()
            if row is None:
                log.info("Frontier empty — done.")
                break
            assoc, reg, depth = row

            if args.max_depth is not None and depth > args.max_depth:
                frontier.mark_skipped(assoc, reg)
                continue

            # Left over from before the scope existed, or queued by another
            # process. Parking is local and instant -- no request, no delay.
            if scope and assoc.upper() != scope:
                frontier.mark_skipped(assoc, reg)
                continue

            url = (angus.representative_url(reg) if assoc.upper() == "ANGUS"
                   else db.animal_url(assoc, reg))
            if robots and not robots.allowed(url):
                log.warning("robots.txt disallows %s — skipping.", url)
                frontier.mark_skipped(assoc, reg)
                continue

            try:
                _url, record, neighbors = scrape_dispatch(assoc, reg, session, args)
                record = classify_record(record)      # Free/Carrier/Suspect/Unknown up front

                is_steer = args.skip_steers and (record.get("sex") or "").upper() == "S"
                if not is_steer:
                    if writer:
                        writer.write(record)
                    if loader:
                        loader.ingest(record)
                    # enqueue discovered relatives for the next depth level
                    if args.max_depth is None or depth + 1 <= args.max_depth:
                        found = [(n["association"], n["regNumber"]) for n in neighbors]
                        if scope:
                            # Cross-registry animals still reach the graph: the
                            # record carries its cross_refs, and the loader
                            # builds the Registration and the edge from those
                            # without anyone having to fetch the page. What is
                            # dropped here is only the animal's own detail --
                            # and the entire subtree hanging off it, which is
                            # what turns one breed's crawl into all of them.
                            found = [n for n in found if n[0].upper() == scope]
                        frontier.add_many(found, depth=depth + 1, source=f"{assoc}:{reg}")

                frontier.mark_done(assoc, reg)
                processed += 1
                blocked = 0      # a success clears the streak
                if not is_steer:
                    last_current = _summarize(assoc, reg, record)
                write_status(args.status_file, status_assoc, "running", last_current,
                             frontier.counts(), processed, start_ts)
                if processed % 25 == 0:
                    log.info("Processed %d | frontier: %s", processed, frontier.counts())

            except db.Blocked as e:
                # Deliberately not mark_failed: the animal is fine, we are the
                # problem, and burning its attempts would quietly turn a block
                # into a frontier full of dead rows. It stays pending.
                blocked += 1
                wait = e.retry_after if e.retry_after is not None else min(30 * blocked, 300)
                log.warning("Refused (%s) on %s:%s -- %d in a row", e.status, assoc, reg, blocked)
                if blocked >= args.max_blocked:
                    log.error(
                        "Stopping: %d consecutive refusals from the host. This is a "
                        "block, not a bad animal -- the frontier is untouched and "
                        "resumes when access is sorted out.", blocked)
                    final_state = "blocked"
                    return EXIT_BLOCKED
                log.info("Backing off %ds before trying again.", wait)
                time.sleep(wait)
                continue

            except db.AnimalNotFound:
                # DigitalBeef serves HTTP 200 + the site shell for a registration
                # that doesn't exist. Skip it rather than recording an empty animal.
                log.info("No animal behind %s:%s — skipping.", assoc, reg)
                frontier.mark_skipped(assoc, reg)

            except Exception as e:
                log.warning("Failed %s:%s -> %s", assoc, reg, e)
                frontier.mark_failed(assoc, reg, str(e), args.max_attempts)

            time.sleep(args.delay)
            if args.limit and processed >= args.limit:
                log.info("Hit --limit %d; stopping (frontier preserved for resume).", args.limit)
                break
    finally:
        if writer:
            writer.close()
        if loader:
            loader.backend.close()
        write_status(args.status_file, status_assoc, final_state, last_current,
                     frontier.counts(), processed, start_ts)
        log.info("Final frontier: %s", frontier.counts())
        if loader:
            log.info("Load stats:\n%s", loader.stats.report())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--association", help="Default association code for bare seeds (e.g. CHIA).")
    p.add_argument("--seed", action="append", help="Seed reg (ASSOC:REG or bare). Repeatable.")
    p.add_argument("--seeds-file", help="File with one ASSOC:REG (or bare reg) per line.")
    p.add_argument("--enumerate", nargs=2, type=int, metavar=("START", "END"),
                   help="Sweep a numeric reg-number range for --association (inclusive).")
    p.add_argument("--add-seeds-only", action="store_true", help="Queue seeds and exit (no crawl).")
    p.add_argument("--front", action="store_true",
                   help="Add these seeds at the FRONT of the pending queue (crawl them next).")
    p.add_argument("--retry-failed", action="store_true", help="Reset failed items to pending.")
    p.add_argument("--reset-skipped", action="store_true",
                   help="Reset skipped items (e.g. robots-skipped) back to pending.")

    p.add_argument("--out", help="JSONL output path (append; feed to loader.py).")
    p.add_argument("--status-file", help="Write a live JSON status heartbeat here (for the dashboard).")
    p.add_argument("--db", default="crawl.db", help="SQLite frontier file (default crawl.db).")

    p.add_argument("--neo4j", action="store_true", help="Also load into Neo4j live via loader.py.")
    p.add_argument("--uri", default="bolt://localhost:7687")
    p.add_argument("--user", default="neo4j")
    p.add_argument("--password", default="neo4j")
    p.add_argument("--insecure", action="store_true",
                   help="Encrypt but skip cert verification (neo4j+s -> neo4j+ssc).")

    p.add_argument("--max-depth", type=int, default=None, help="Limit recursion depth (default: unlimited).")
    p.add_argument("--limit", type=int, default=None, help="Stop after N animals (frontier preserved).")
    p.add_argument("--delay", type=float, default=2.0, help="Seconds between requests (be polite).")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--max-attempts", type=int, default=3, help="Retries before marking failed.")
    p.add_argument("--max-blocked", type=int, default=5,
                   help="Consecutive 403/429 refusals before stopping (default 5). A "
                        "refusal is about the client, not the animal, so continuing "
                        "just knocks harder; the frontier is left untouched.")
    p.add_argument("--stay-in-association", action="store_true",
                   help="Only follow relatives within --association. A pedigree cites "
                        "other registries and DigitalBeef serves ten of them, so "
                        "unscoped the crawl walks out of its own breed and into the "
                        "rest. Cross-registry links still reach the graph via the "
                        "record's cross_refs; only the foreign animal's own page is "
                        "skipped.")
    p.add_argument("--skip-foreign", action="store_true",
                   help="One-off: park every pending animal from another association "
                        "as skipped. Use with --stay-in-association to drain a "
                        "frontier that already sprawled. Reversible with "
                        "--reset-skipped.")
    p.add_argument("--no-epds", action="store_true",
                   help="Skip the EPDs tab. One request per animal saved (a fifth of "
                        "the crawl's traffic) at the cost of the EPD figures on the "
                        "Registration node; the pedigree walk is unaffected.")
    p.add_argument("--no-progeny", action="store_true",
                   help="Ancestors only (don't expand downward to descendants).")
    p.add_argument("--skip-steers", action="store_true",
                   help="Ignore steers (castrated males): don't store or expand them.")
    p.add_argument("--ignore-robots", action="store_true", help="Skip robots.txt checks (not recommended).")
    p.add_argument("--save-html", help="Directory to dump raw HTML (for refining selectors).")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (args.seed or args.seeds_file or args.enumerate or args.retry_failed
            or args.reset_skipped or args.skip_foreign):
        # No new work specified — resume whatever is already pending.
        log.info("No seeds given; resuming existing frontier in %s", args.db)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
