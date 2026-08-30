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

## Be a good citizen

Respect each association's Terms of Service and robots.txt (honored by default).
Keep `--delay` at 1s or higher, set a real contact in the crawler's user-agent,
and don't raise per-host concurrency. If you plan a truly exhaustive pull, it's
worth asking the associations about a data-access agreement.
