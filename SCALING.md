# Scaling the crawl across the 4 associations

## The two-command version

```powershell
# 1. launch 4 parallel crawlers (Chianina, Maine-Anjou, Shorthorn, Angus)
powershell -ExecutionPolicy Bypass -File .\scale.ps1

# 2. load whatever they've collected into Neo4j (run any time, repeatable)
.\load_all.ps1
```

`scale.ps1` opens one window per association, each with its own frontier
(`frontier_CHIA.db`, …) and output (`data_CHIA.jsonl`, …). Leave them running.
Re-running `scale.ps1` **resumes** — done animals are skipped.

## Why one process per association

The three DigitalBeef breeds share infrastructure and Angus is its own host.
One process per association keeps each host at ~1 request/second — parallel across
hosts, polite to each. Going faster *per host* risks their servers and their ToS,
so this is the responsible maximum. Don't add more concurrency against a single
host.

## How far it reaches

- **Ancestors** — every animal's sire/dam are followed up the tree.
- **Progeny (DigitalBeef)** — offspring are followed *down*, so the crawl covers
  the whole connected family graph, not just one branch. This is what makes a
  single seed fan out to thousands.
- **Cross-association merges** — a foreign registration (e.g. an Angus `AAA`
  number in a Chianina pedigree) is emitted with the right association, so the
  four datasets converge onto shared nodes when loaded.

To also catch animals that no pedigree links to, add a systematic sweep of a
registration-number range (one association at a time):

```powershell
python crawl.py --association CHIA --enumerate 1 500000 --out data_CHIA.jsonl --db frontier_CHIA.db --delay 1.5
```

## Angus is shallower by nature

The Angus adapter climbs ancestors from each seed but does **not** fetch progeny
(angus.org has no confirmed progeny listing), so Angus breadth comes from your
seeds, from `AAA` numbers discovered in the other breeds' pedigrees, and from
enumeration. To broaden Angus, seed it with many known registration numbers:

```powershell
# one ASSOC:REG (or bare reg) per line in angus_seeds.txt
python crawl.py --association ANGUS --seeds-file angus_seeds.txt --out data_ANGUS.jsonl --db frontier_ANGUS.db --delay 1.5
```

## Running over time / monitoring

- Each window prints `frontier: {'pending': N, 'done': M, 'failed': K}` as it goes.
- Stop any time (close the window / Ctrl+C); re-run `scale.ps1` to continue.
- Run in chunks if you prefer: add `--limit 5000` to stop after N animals
  (frontier is preserved).
- Retry errors: `python crawl.py --retry-failed --out data_CHIA.jsonl --db frontier_CHIA.db`
- Progress in the graph: in Aura Browser, `MATCH (a:Animal) RETURN count(a);`

## Expectations

"As far as possible" across four registries is potentially hundreds of thousands
of animals and **days** of polite crawling — that's normal and expected. It's
fully resumable, so treat it as a background process you top up over time, not a
single run. Load into Neo4j periodically with `load_all.ps1`; the dedup makes
re-loading harmless.

## The cost of an animal

Each animal is **five requests**, not one: the container page plus `_pedigree`,
`_genotype`, `_epds` and `_progeny`. With a delay before each, the tabs dominate
both the wall clock and the load — which makes *which tabs you fetch* a bigger
lever than concurrency, and the only lever that can make the crawl faster and
lighter at the same time.

- `_pedigree` — required. It is what makes the crawl recursive.
- `_genotype` — required for the defect findings.
- `_progeny` — what makes a seed fan out to thousands. `--no-progeny` to skip.
- `_epds` — the EPD figures, stored on the Registration node. `--no-epds` to
  skip: a fifth of the traffic gone, and nothing else changes.

Dropping `_epds` at the same delay leaves the request rate exactly where it was
while raising throughput about a quarter — the saved sleep and the saved
round-trip both come back as animals. Lowering `--delay` on top of that trades
in the other direction: it buys speed by spending request rate.

## Keep each crawler in its own breed

DigitalBeef serves ten associations, and a pedigree freely cites registrations
in the others. Unscoped, following those citations walks a crawler clean out of
the breed it was started for: the Chianina run picks up a Simmental sire, then
that animal's pedigree and progeny, and three crawlers end up racing each other
through the same neighbouring registries. `--association` alone does **not**
prevent this — it only sets the default for bare seeds.

The symptom is a frontier that grows faster than it drains:

```bash
sqlite3 frontier_CHIA.db "select status, count(*) from queue group by status"
sleep 600
sqlite3 frontier_CHIA.db "select status, count(*) from queue group by status"
```

If `pending` holds steady or rises while `done` climbs, every animal is
discovering at least one more and the crawl has no finish line. Break it down
by registry — `select association, count(*) from queue where status='pending'
group by association order by 2 desc` — and the foreign breeds are usually most
of it.

`--stay-in-association` follows only relatives in the crawler's own registry.
Cross-breed joins are unaffected: the record still carries its `cross_refs`,
and the loader builds the Registration node and the edge from those without
anyone fetching the page. What is dropped is the foreign animal's own detail,
and the entire subtree hanging off it.

For a frontier that already sprawled, park the foreign rows once:

```bash
sudo systemctl stop crawl@CHIA
python crawl.py --association CHIA --db frontier_CHIA.db --skip-foreign --add-seeds-only
sudo systemctl start crawl@CHIA
```

Parked, not deleted — `--reset-skipped` puts them back if you ever widen the
scope again.

## Be a good citizen

Respect each association's Terms of Service and robots.txt (honored by default).
Keep `--delay` at 1s or higher, set a real contact in the crawler's user-agent,
and don't raise per-host concurrency.

**The three DigitalBeef breeds are subdomains, not separate servers** —
`chianina`, `maine-anjou` and `shorthorn` all under `digitalbeef.com`. Check
with `dig +short chianina.digitalbeef.com maine-anjou.digitalbeef.com` before
assuming otherwise. If they resolve to one address, the three crawlers share a
single host's budget and their rates add up: at `--delay 1.0` with `--no-epds`,
three crawlers come to roughly 1 request/second against that one host, which is
the ceiling this file has always set. Speeding one breed up spends the same
budget the other two are drawing on.

If you need to go materially faster, the answer is access rather than
infrastructure: ask the association about a bulk export or a data-access
agreement. Standing up more machines to spread the same traffic over more
addresses is evasion, not scaling — and a platform-wide block would cost you
all three breeds at once.
