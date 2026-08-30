"""
Reconcile duplicate animals after loading.

The loader prevents creating a duplicate at ingest time, but it can't merge two
nodes that were *already* created — which happens when the same animal is stubbed
from pedigrees under different association registration numbers (so each stub is
matched only by its own reg number and never by name+DOB). This pass finds
animals that share a normalized name + date of birth and merges them into one,
carrying every registration/relationship along.

Run it after a load:

    python reconcile.py --uri ... --user ... --password ... --insecure
    python reconcile.py ... --dry-run      # just report what would merge

Requires APOC (built in on Neo4j Aura).
"""

from __future__ import annotations

import argparse
import sys

# Only reconcile REAL animal names. Excludes parser artefacts:
#   * breed-composition descriptors ("AN X MA", "CA X AN", …) via ' X '
#   * bare breed words ("ANGUS", "SIMMENTAL", …) via a guard list
#   * placeholder foundation dates (YYYY-01-01)
#   * single-token names (real names have a herd prefix + id, i.e. a space)
# so genuine cross-association duplicates (e.g. ZNT JENNA 707T) merge, and the
# breed/foundation junk never does.
BREED_WORDS = ['ANGUS', 'RED ANGUS', 'SIMMENTAL', 'CHIANINA', 'MAINE ANJOU',
               'MAINEANJOU', 'SHORTHORN', 'HEREFORD', 'GELBVIEH', 'LIMOUSIN',
               'SALERS', 'BRAUNVIEH', 'CHIANGUS']

_FILTER = """
    a.name_norm CONTAINS ' '
    AND NOT a.name_norm CONTAINS ' X '
    AND a.dob IS NOT NULL
    AND NOT a.dob ENDS WITH '-01-01'
    AND NOT a.name_norm IN $breed_words
"""

PREVIEW = f"""
MATCH (a:Animal)
WHERE {_FILTER}
WITH a.name_norm AS name, a.dob AS dob, collect(a) AS nodes
WHERE size(nodes) > 1
RETURN name, dob, size(nodes) AS copies, [x IN nodes | x.uid] AS uids
ORDER BY copies DESC
"""

MERGE = f"""
MATCH (a:Animal)
WHERE {_FILTER}
WITH a.name_norm AS name, a.dob AS dob, collect(a) AS nodes
WHERE size(nodes) > 1
CALL apoc.refactor.mergeNodes(nodes, {{properties: 'discard', mergeRels: true}}) YIELD node
RETURN count(*) AS groups_merged
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Merge duplicate animals by name+DOB.")
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="neo4j")
    ap.add_argument("--insecure", action="store_true",
                    help="Encrypt but skip cert verification (neo4j+s -> neo4j+ssc).")
    ap.add_argument("--dry-run", action="store_true", help="Report duplicates, don't merge.")
    args = ap.parse_args(argv)

    from neo4j import GraphDatabase
    uri = args.uri
    if args.insecure:
        uri = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
    driver = GraphDatabase.driver(uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    try:
        with driver.session() as s:
            groups = list(s.run(PREVIEW, breed_words=BREED_WORDS))
            if not groups:
                print("No duplicate name+DOB groups found. Nothing to reconcile.")
                return 0
            print(f"{len(groups)} duplicate group(s):")
            for r in groups[:50]:
                print(f"  {r['name']!r:40} {r['dob']}  x{r['copies']}  {r['uids']}")
            if len(groups) > 50:
                print(f"  … and {len(groups) - 50} more")

            if args.dry_run:
                print("\n--dry-run: no changes made.")
                return 0

            merged = s.run(MERGE, breed_words=BREED_WORDS).single()["groups_merged"]
            print(f"\nMerged {merged} group(s) into single nodes.")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
