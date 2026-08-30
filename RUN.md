# Running the full scrape

All commands run from this directory. Verified working (parser tests + loader
dry-run pass).

## 0. One-time setup

```bash
pip install -r requirements.txt
```

You need a running Neo4j. Easiest options (no cypher-shell required):

- **Neo4j Aura** (cloud, nothing to install): create a free DB at neo4j.com/aura,
  note the `neo4j+s://...` URI and password.
- **Neo4j Desktop** (local app): neo4j.com/download, create a DB, set a password.

Apply the schema once — pick either:

```powershell
# A) Neo4j Browser (simplest): open the browser UI that comes with Aura/Desktop,
#    paste the whole contents of schema.cypher, press Run.

# B) Or script it via the Python driver (Windows/macOS/Linux, no cypher-shell):
python init_schema.py --uri bolt://localhost:7687 --user neo4j --password YOURPASSWORD
#    (Aura: use your neo4j+s://... URI)
```

## 1. Crawl (choose your strategy — you can combine them)

Recursive crawl from seeds — follows pedigree up and progeny down, filling the
connected family graph. Frontier state lives in `crawl.db` (resumable).

```bash
# seed one or more animals (ASSOC:REG, or bare reg with --association)
python crawl.py --association CHIA --seed MA430053 --out records.jsonl --delay 2
```

Add more seeds / other associations anytime — same `crawl.db`, only new animals
are fetched (gap-filling), and Angus routes to its own adapter automatically:

```bash
python crawl.py --seed MAINE:402303 --seed ANGUS:13054003 --out records.jsonl
```

Systematic sweep of an association's registration-number range — the reliable
way to reach EVERY animal, including ones no pedigree links to:

```bash
python crawl.py --association CHIA --enumerate 1 500000 --out records.jsonl --delay 2
```

Resume an interrupted run (no new seeds needed — it drains the frontier):

```bash
python crawl.py --out records.jsonl
```

Retry anything that errored:

```bash
python crawl.py --retry-failed --out records.jsonl
```

Useful flags: `--limit N` (stop after N, frontier preserved), `--max-depth N`,
`--no-progeny` (ancestors only), `--save-html DIR` (dump HTML to tune parsers).

## 2. Load into Neo4j (dedupes on International ID)

```bash
python loader.py records.jsonl --neo4j \
    --uri bolt://localhost:7687 --user neo4j --password changeme
```

Or load live *while* crawling (JSONL + Neo4j at once):

```bash
python crawl.py --association CHIA --seed MA430053 --out records.jsonl \
    --neo4j --uri bolt://localhost:7687 --user neo4j --password changeme --delay 2
```

## Start completely fresh

`records.jsonl` appends and `crawl.db` remembers what's done. For a clean run,
point at new files:

```bash
python crawl.py --association CHIA --seed MA430053 \
    --out data_run2.jsonl --db frontier_run2.db --delay 2
```

## Be a good citizen

Respect each association's Terms of Service and robots.txt (honored by default).
Keep `--delay` at 2s+ for large sweeps; set a real contact in `--user-agent`.
See CRAWLER.md for details, and README.md for the graph schema + queries.
```
