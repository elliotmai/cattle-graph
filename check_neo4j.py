"""
Standalone Neo4j connection test — isolates connection problems from the dashboard.

    python check_neo4j.py

Reads NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD from the environment, or from a
credentials file (Neo4j-*.txt / neo4j.env / neo4j-credentials.txt) in this folder.
Prints the URI it's using, whether the password is set, and either the animal
count (connected) or the exact error (not connected). Paste the output for help.
"""

import os
import re
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))


def load_creds_file():
    if os.environ.get("NEO4J_URI"):
        return
    candidates = (sorted(glob.glob(os.path.join(HERE, "[Nn]eo4j*.txt")))
                  + [os.path.join(HERE, "neo4j.env"),
                     os.path.join(HERE, "neo4j-credentials.txt")])
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$", line)
                    if m:
                        os.environ.setdefault(m.group(1), m.group(2))
        except Exception:
            continue
        if os.environ.get("NEO4J_URI"):
            print(f"[creds] loaded from {os.path.basename(path)}")
            return


load_creds_file()
uri = os.environ.get("NEO4J_URI")
user = os.environ.get("NEO4J_USERNAME", "neo4j")
pw = os.environ.get("NEO4J_PASSWORD")

if not uri:
    print("NO CREDENTIALS FOUND.")
    print("  NEO4J_URI is not set, and no creds file was found in this folder.")
    print("  Fix: set the env vars in this shell, or drop your Aura Neo4j-*.txt file here.")
    sys.exit(1)

print(f"[uri]  {uri}")
print(f"[user] {user}")
print(f"[pass] {'set (' + str(len(pw)) + ' chars)' if pw else 'MISSING'}")

uri_ssc = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
print(f"[connecting as] {uri_ssc.split('://', 1)[0]}://")

try:
    from neo4j import GraphDatabase
except Exception:
    print("neo4j driver not installed. Run:  pip install neo4j")
    sys.exit(1)

driver = GraphDatabase.driver(uri_ssc, auth=(user, pw))
try:
    driver.verify_connectivity()
    with driver.session() as s:
        animals = s.run("MATCH (a:Animal) RETURN count(a) AS n").single()["n"]
        regs = s.run("MATCH (:Registration) RETURN count(*) AS n").single()["n"]
    print(f"CONNECTED  animals={animals}  registrations={regs}")
except Exception as e:
    print("CONNECTION FAILED:")
    print(f"  {type(e).__name__}: {str(e).splitlines()[0]}")
finally:
    driver.close()
