"""
Apply schema.cypher to Neo4j WITHOUT cypher-shell (uses the Python driver you
already installed for the loader). Works the same on Windows, macOS, Linux.

    pip install neo4j
    python init_schema.py --uri bolt://localhost:7687 --user neo4j --password changeme

Preview the statements without touching a database:

    python init_schema.py --dry-run
"""

from __future__ import annotations

import argparse
import sys


def statements(cypher_text: str):
    """Split a .cypher file into runnable statements: strip // comments, split
    on ';'. (schema.cypher has no string literals containing ; or //.)"""
    cleaned = []
    for line in cypher_text.splitlines():
        i = line.find("//")
        if i != -1:
            line = line[:i]
        cleaned.append(line)
    for stmt in "\n".join(cleaned).split(";"):
        s = stmt.strip()
        if s:
            yield s


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply a .cypher schema via the Neo4j driver.")
    ap.add_argument("--file", default="schema.cypher")
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="neo4j")
    ap.add_argument("--dry-run", action="store_true", help="Print statements, don't connect.")
    ap.add_argument("--insecure", action="store_true",
                    help="Encrypt but skip certificate verification (neo4j+s -> neo4j+ssc). "
                         "Use when a corporate proxy/antivirus intercepts TLS.")
    args = ap.parse_args(argv)

    with open(args.file, encoding="utf-8") as f:
        stmts = list(statements(f.read()))
    print(f"{len(stmts)} statement(s) in {args.file}")

    if args.dry_run:
        for i, s in enumerate(stmts, 1):
            print(f"\n--- [{i}] ---\n{s}")
        return 0

    from neo4j import GraphDatabase
    uri = args.uri
    if args.insecure:
        uri = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
        print(f"insecure mode: connecting with {uri.split('://', 1)[0]}://")
    driver = GraphDatabase.driver(uri, auth=(args.user, args.password))
    driver.verify_connectivity()  # fail fast with a clear message
    try:
        with driver.session() as sess:
            for i, s in enumerate(stmts, 1):
                sess.run(s)
                print(f"  [{i}/{len(stmts)}] ok")
        print("schema applied.")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
