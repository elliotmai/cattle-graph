# Recursive DigitalBeef crawler

Crawls outward from seed registration numbers, following **pedigree (ancestor)**
and **progeny (descendant)** links, and writes records in the loader's format.
The frontier is stored in SQLite, so runs are **resumable** and you can **inject
new registration numbers any time to fill gaps**.

## Files

- `digitalbeef.py` — fetch + parse a DigitalBeef animal page into a record; extracts neighbor links.
- `crawl.py` — the recursive crawler + resumable/injectable frontier + CLI.
- `loader.py` — consumes the crawler's output (JSONL or JSON) and dedups into the graph.
- `requirements.txt` — `pip install -r requirements.txt`.

## How "get every animal" actually works

There are two complementary strategies, and for true completeness you'll use both:

1. **Recursive graph crawl (default).** From each seed the crawler follows sire/dam
   links *upward* and progeny links *downward*, discovering the whole connected
   family graph. This reaches every animal reachable through relationships from
   your seeds. It will **not** reach animals with no crawled relatives.
2. **Registration-number enumeration** (`--enumerate START END`). Sweeps a numeric
   reg-number range for an association. This is the reliable way to hit animals
   the pedigree graph never links to. Combine it with the recursive crawl and the
   dedup loader collapses the overlap automatically.

> Ancestor-only recursion (`--no-progeny`) climbs to founders but never reaches
> descendants — that's why progeny expansion is on by default.

## Filling gaps later

The frontier uses `INSERT OR IGNORE` keyed on (association, reg_number), so:

- Re-running with new `--seed`s queues only the unknown ones; anything already
  `done` is skipped. Start a fresh recursive run just by adding seeds.
- `--retry-failed` resets previously failed pages to pending.
- Because `loader.py` keys animals on the International ID (and falls back to
  DNA/registration matching), records pulled in a later run merge onto the
  existing nodes rather than duplicating them.

## Typical workflow

```bash
pip install -r requirements.txt

# 1) Recursive crawl from a few seeds -> JSONL
python crawl.py --association CHIA --seed MA430053 --seed MA555000 --out records.jsonl

# 2) (later) add more seeds / other associations to fill gaps — same frontier
python crawl.py --seed SHORT:3790685 --seed MAINE:402303 --out records.jsonl

# 3) systematic sweep to catch unlinked animals
python crawl.py --association CHIA --enumerate 400000 460000 --out records.jsonl

# 4) load everything into Neo4j (dedups on International ID)
python loader.py records.jsonl --neo4j --uri bolt://localhost:7687 --user neo4j --password secret
```

Or skip the file and load live while crawling:

```bash
python crawl.py --association CHIA --seed MA430053 --out records.jsonl \
    --neo4j --uri bolt://localhost:7687 --user neo4j --password secret
```

## Be a good citizen (and read this before large runs)

- **Terms of Service / robots.txt:** several registries prohibit bulk scraping and
  some data is members-only. The crawler honors `robots.txt` by default (override
  with `--ignore-robots`, which you should not do lightly). Check each
  association's terms; ask about a data-access agreement or API for anything
  ongoing or large-scale.
- **Rate limiting:** default `--delay 2.0` seconds between requests. Associations
  running on shared DigitalBeef infrastructure — don't hammer them. Raise the
  delay for big sweeps.
- Set a real contact address in `--user-agent`.

## Cross-registration linking (one node across platforms)

`registries.py` maps registry prefixes to association codes (`AAA`→ANGUS,
`AMAA`→MAINE, `ACA`→CHIA, `RAAA`→RED, …). Both parsers use it so the same animal
referenced from different platforms resolves to one graph node:

- **Dual-registered animal.** When a DigitalBeef animal's own identity block
  lists a foreign number (e.g. an animal also carrying `AAA #13054003`), the
  parser emits it as a `cross_ref`. The loader creates a registration stub, and
  when the Angus crawl later ingests `ANGUS:13054003`, its Tier-1 registration
  match merges the two onto the **same node** — now holding both registrations.
- **Foreign ancestor in a pedigree.** An Angus bull shown as `AAA #13054003`
  inside a Maine-Anjou pedigree is emitted as a neighbor with association
  `ANGUS`, so it's crawled and created as a single `ANGUS:13054003` node. Every
  pedigree that references that bull emits the same `(ANGUS, 13054003)` pair, so
  they all converge to one node.

Safety: numbers that appear in pedigree tables are treated as ancestors and are
excluded from an animal's own `cross_refs`, so a sire's foreign number is never
mistaken for the calf's own alternate registration (which would wrongly merge
calf and sire). Extend `PREFIX_TO_ASSOC` in `registries.py` for more registries.

Two honest limits: if a site renders the identity block *inside* a table, own
`cross_refs` detection is conservative (it may miss a dual-registration link, but
never mismerges) — point detection at the identity container to tighten. And a
foreign-only ancestor becomes its own correct node, but the explicit
`SIRE_OF`/`DAM_OF` edge to it is only set when the parent is a resolvable link.

## The parser (verified against real pages)

Selectors in `digitalbeef.py` were written against saved fixtures of a live
animal — CHIA `MA430053`, *ZNT MOVES LIKE JAGGER*. `fixtures/` holds those pages
and `test_parsers.py` asserts against them with no network:

```bash
python test_parsers.py
```

Three things about DigitalBeef drive the design, and getting any of them wrong
produces a crawl that appears to succeed while collecting nothing:

**1. The container page is only the identity block.** Pedigree, genotype, EPDs,
ownership and progeny each load from their own endpoint:

```
modules/_animal/ajax/{tab}.php?u=&a={REG}&can_edit=0&thetime={ms}
```

`activateTab()` in `modules/_animal/js/_animal.js` maps tab index → file:
`_epds`, `_performance`, `_performance_stats`, `_progeny`, `_pedigree`,
`_breeding`, `_genotype`, `_ownership`, … `scrape_animal()` fetches
`_pedigree`, `_genotype`, `_epds` and (unless `--no-progeny`) `_progeny`.
Fetching only the container yields an animal with no parents, so a recursive
crawl finds no neighbours and stops dead at its seeds.

**2. A missing registration returns HTTP 200 and the ordinary site shell** — no
error, no "not found". Absence of the `Registration:` cell is the only signal, so
`parse_container()` raises `AnimalNotFound` and `crawl.py` skips it rather than
writing an empty record.

**3. The values live in `<td>Label:</td><td>value</td>` pairs.** Regexing the
flattened page text scrapes the sidebar instead — the "International Letter" year
table becomes a date of birth, `Owner` becomes a coat colour. Note also that
`Genetic Makeup` percentages are decimals (`73.049% MA`): matching only the
integer part yields breeds at 733%.

Sire and dam come from the explicit `Sire:` / `Dam:` label cells on the pedigree
tab. Do **not** infer them from cell order — the chart is a nested table whose
cells are duplicated for rendering, and on the reference animal the sire is the
eighth ancestor cell in document order.

Defect results use a four-level vocabulary, not a binary one: `Free by Test`,
`Free by Pedigree`, `Suspect`, `Carrier by Test`, `Affected by Test`, which the
parser maps to `F` / `S` / `C` / `A`. On the pedigree chart the same information
is suffixed onto the locus — `PHAFT` is PHA free-by-test, `AMS` is AM suspect.
Watch the collisions: `CA` is Chianina as a breed code and Contractural
Arachnodactyly as a defect locus; `DDS` is DD-suspect, not the DS locus.

To capture fresh fixtures after a site change:

```bash
python crawl.py --association CHIA --seed MA430053 --limit 1 --save-html ./fixtures
```

## Angus adapter (`angus.py`)

`angus.org` is a different platform, so it has its own adapter that emits the
same record format and plugs into the same frontier + loader. `crawl.py` routes
automatically: association code `ANGUS` → `angus.py`, everything else →
`digitalbeef.py`. So you can mix them:

```bash
python crawl.py --seed CHIA:MA430053 --seed ANGUS:19876543 --out records.jsonl
python crawl.py --association ANGUS --enumerate 19000000 19100000 --out records.jsonl
```

### How the Angus adapter works (confirmed against the live site)

angus.org is a server-rendered ASP.NET MVC site — the animal data is in the HTML,
there is no JSON API. It uses opaque, per-request `aid`/`time` tokens that can't
be built from a reg number, so the adapter reaches an animal by search:

```
GET  /find-an-animal            -> form with antiforgery hidden fields + cookie
POST /find-an-animal  EpdPedSearchRequest.sAnimalRegNum=<reg>  (+ hidden fields)
     -> 302 redirect to /Animal/EpdPedDtl?aid=...&time=...  (the detail HTML)
```

The adapter echoes the form's antiforgery token automatically (it re-submits
every hidden input), so you don't configure anything. It parses identity,
sex, genetic conditions (`[AMF-CAF-...]` → code + F/C/A status), birth date,
tattoo, breeder/owners, the EPD panel, and the `table.pedigree`. Every ancestor
reg number in the pedigree becomes a neighbor, resolved the same reg→detail way —
so recursion works exactly like DigitalBeef. The frontier keys on the stable
**registration number**, never on the ephemeral aid token.

Two parses are heuristic (flagged in `angus.py`): which pedigree cells are the
direct sire vs dam, and splitting the compact EPD cells. The full EPD section is
also stored verbatim in `attributes.epd_raw`, so nothing is lost even where the
structured parse needs tuning. Capture a page with `--save-html ./html_samples`
to refine.
