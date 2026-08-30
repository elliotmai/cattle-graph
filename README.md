# Cattle Multi-Association Graph — Schema & Loader

Builds a Neo4j graph where **each real animal is a single node**, keyed by its
**International ID**, even when it is registered in several breed associations
(Maine-Anjou, Chianina, Shorthorn, Angus, etc.). Association-specific data hangs
off per-association `Registration` nodes so nothing gets overwritten, and
parentage links connect animals directly for clean pedigree traversal.

The loader captures **everything**: identity, coat color, breed composition,
genetic-defect status, horn status, EPDs, parentage — plus an open-ended
`attributes` bag for any other field a page exposes (EID/tag numbers, progeny
counts, ownership history, weights/ratios, AI/embryo flags, indexes, etc.), so
no data is dropped even if it isn't explicitly modeled.

## Files

- `schema.cypher` — constraints, indexes, seed `Association`/`Breed`/`Defect` nodes. Run once.
- `loader.py` — ingests parsed records, does entity resolution/dedup, writes the graph.
- `sample_records.json` — six example records that exercise every merge path and every field.

## Graph model

```
(:Animal {uid=IntlID, intl_id, name, name_norm, sex, dob, tattoo, color, horn_status})
   -[:HAS_REGISTRATION]-> (:Registration {association, regNumber, epds, attributes, owner, source_url, ...})
                              -[:IN_ASSOCIATION]-> (:Association {code, name})
(:Animal)-[:HAS_DNA]-> (:DnaCase {caseId})
(:Animal)-[:HAS_COMPOSITION {percent}]-> (:Breed {code, name})     // breed % breakdown
(:Animal)-[:TESTED {status}]-> (:Defect {code, name})              // status: F=Free, C=Carrier
(sire:Animal)-[:SIRE_OF]->(offspring:Animal)
(dam:Animal) -[:DAM_OF]-> (offspring:Animal)
```

- `color` and `horn_status` (`Horned` / `Polled` / `Scurred`) are scalar properties on `Animal`.
- **Breed composition** and **defect status** are modeled as edges (not properties) so they're
  directly queryable — "everything ≥50% Maine-Anjou", "all TH carriers", etc.
- `epds` and the free-form `attributes` bag are stored as JSON on the `Registration`, so each
  association's version is preserved side by side.

## Entity resolution (how dedup decides two records are the same animal)

Strongest key first, stop at the first hit:

1. **International ID** — the standardized cross-association identifier, and the canonical `uid`.
2. **Same registration** already in the graph.
3. **Shared DNA case number** (Neogen/GeneSeek/AGI).
4. **Cross-referenced registration** in another association.
5. **Tattoo + DOB + sex** composite.
6. **Normalized name + DOB** (weakest).

Records with an International ID become a node keyed by it. Records lacking one
fall back to DNA/registration/etc. and get a temporary `TMP-<uuid>` uid until an
International ID is learned. Parents referenced by a record become stubs that
auto-enrich when their own full record loads, so load order doesn't matter.

## Running it

Dry run (no database — pure in-memory, prints resolved animals with color, breed % and defects):

```bash
python loader.py sample_records.json
```

Against a live Neo4j:

```bash
pip install neo4j
cat schema.cypher | cypher-shell -u neo4j -p secret          # apply schema once
python loader.py sample_records.json --neo4j \
    --uri bolt://localhost:7687 --user neo4j --password secret
```

## Record format

See the docstring at the top of `loader.py` and `sample_records.json`. Only
`association` and `regNumber` are strictly required; everything else is optional
and captured when present. Anything not explicitly modeled goes in `attributes`.

## Useful queries once loaded

```cypher
// Animals registered in more than one association
MATCH (a:Animal)-[:HAS_REGISTRATION]->(r)
WITH a, collect(DISTINCT r.association) AS assocs
WHERE size(assocs) > 1
RETURN a.name, a.uid, assocs;

// Every carrier of a given defect (status is Free | Carrier | Unknown)
MATCH (a:Animal)-[t:TESTED {status:'Carrier'}]->(x:Defect {code:'PHA'})
RETURN a.name, a.uid;

// Everything at least 50% Maine-Anjou
MATCH (a:Animal)-[c:HAS_COMPOSITION]->(:Breed {code:'MA'})
WHERE c.percent >= 50
RETURN a.name, c.percent ORDER BY c.percent DESC;

// Polled black animals
MATCH (a:Animal) WHERE a.horn_status='Polled' AND a.color='Black' RETURN a.name;

// Full pedigree, 5 generations up
MATCH path = (a:Animal {name:'Rolling Hills Bella 3F'})<-[:SIRE_OF|DAM_OF*1..5]-(anc)
RETURN path;
```

## Caveats

Check each association's Terms of Service and `robots.txt` before bulk crawling —
several registries prohibit scraping, and some data is members-only. Rate-limit
politely, and ask associations about data-access agreements or APIs for anything ongoing.
