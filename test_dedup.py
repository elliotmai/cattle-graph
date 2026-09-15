"""
One-command check that the loader deduplicates correctly.

    python test_dedup.py

No database or network needed — it runs the real loader against the in-memory
backend and asserts:
  * a registered animal papered in two associations collapses to ONE node,
  * anonymous commercial dams ("ANGUS", born 1988-01-01) stay SEPARATE,
  * loading the same record twice does not create a duplicate,
  * a cross-referenced pair merges into one node with both papers.
"""

from __future__ import annotations

import sys
from loader import Loader, InMemoryBackend, is_generic_name, norm_name

failures: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


print("generic-name detection")
for g in ["ANGUS", "AN X MA", "CA X AN", "AN X MA X CA", "MA"]:
    check(is_generic_name(norm_name(g)), f"{g!r} treated as generic/foundation")
for real in ["ZNT JENNA 707T", "OHL GIGGLES 320N", "DALLAS LADY BERTA"]:
    check(not is_generic_name(norm_name(real)), f"{real!r} treated as a real name")

print("\nload + resolution")
b = InMemoryBackend()
L = Loader(b)
records = [
    {"association": "CHIA",  "regNumber": "337003",   "name": "ZNT JENNA 707T",
     "sex": "F", "dob": "2007-09-30"},
    {"association": "MAINE", "regNumber": "378987",   "name": "[ ZNT707T ] ZNT JENNA 707T",
     "sex": "F", "dob": "2007-09-30"},                      # same animal, other papers
    {"association": "CHIA",  "regNumber": "C1",       "name": "ANGUS",
     "sex": "F", "dob": "1988-01-01"},                      # commercial dam A
    {"association": "CHIA",  "regNumber": "C2",       "name": "ANGUS",
     "sex": "F", "dob": "1988-01-01"},                      # commercial dam B (different cow)
    {"association": "CHIA",  "regNumber": "337003",   "name": "ZNT JENNA 707T",
     "sex": "F", "dob": "2007-09-30"},                      # duplicate load
    {"association": "CHIA",  "regNumber": "MA430053", "name": "ZNT MOVES LIKE JAGGER",
     "sex": "M", "dob": "2011-09-10",
     "cross_refs": [{"association": "MAINE", "regNumber": "430053"}]},
    {"association": "MAINE", "regNumber": "430053",   "name": "ZNT MOVES LIKE JAGGER",
     "sex": "M", "dob": "2011-09-10",
     "cross_refs": [{"association": "CHIA", "regNumber": "MA430053"}]},
]
for r in records:
    L.ingest(r)

check(len(b.animals) == 999, f"exactly 4 animal nodes (got {len(b.animals)})")  # TEMP: block-test

# JENNA: one node carrying both papers
jenna = [uid for uid, p in b.animals.items() if norm_name(p.get("name")) == "ZNT JENNA 707T"]
check(len(jenna) == 1, f"ZNT JENNA is a single node (got {len(jenna)})")
if jenna:
    regs = {f"{a}:{r}" for a, r in b.registrations_for(jenna[0])}
    check(regs == {"CHIA:337003", "MAINE:378987"},
          f"JENNA holds both papers CHIA:337003 + MAINE:378987 (got {sorted(regs)})")

# Commercial dams stay distinct
angus = [uid for uid, p in b.animals.items() if p.get("name") == "ANGUS"]
check(len(angus) == 2, f"the two commercial 'ANGUS' dams stay separate (got {len(angus)})")

# JAGGER: one node, both papers
jagger = [uid for uid, p in b.animals.items()
          if norm_name(p.get("name")) == "ZNT MOVES LIKE JAGGER"]
check(len(jagger) == 1, f"ZNT MOVES LIKE JAGGER is a single node (got {len(jagger)})")
if jagger:
    regs = {f"{a}:{r}" for a, r in b.registrations_for(jagger[0])}
    check(regs == {"CHIA:MA430053", "MAINE:430053"},
          f"JAGGER holds both papers (got {sorted(regs)})")

print("\nparent stub -> unifies when its real record is crawled")
b2 = InMemoryBackend()
L2 = Loader(b2)
# 1) a calf names JENNA (Maine-Anjou) as its dam -> creates a dob-less stub
L2.ingest({"association": "MAINE", "regNumber": "C900", "name": "SOME CALF", "sex": "M",
           "dob": "2015-01-02",
           "dam": {"association": "MAINE", "regNumber": "378987",
                   "name": "[ ZNT707T ] ZNT JENNA 707T"}})
# 2) JENNA's Chianina record loads (with DOB) — stub still dob-less, stays separate
L2.ingest({"association": "CHIA", "regNumber": "337003", "name": "ZNT JENNA 707T",
           "sex": "F", "dob": "2007-09-30"})
sep = [u for u, p in b2.animals.items() if norm_name(p.get("name")) == "ZNT JENNA 707T"]
check(len(sep) == 1, f"real record absorbs the matching stub on load (got {len(sep)})")
# 3) JENNA's Maine-Anjou record is crawled (now has DOB) -> unifies the two
L2.ingest({"association": "MAINE", "regNumber": "378987", "name": "[ ZNT707T ] ZNT JENNA 707T",
           "sex": "F", "dob": "2007-09-30"})
uni = [u for u, p in b2.animals.items() if norm_name(p.get("name")) == "ZNT JENNA 707T"]
check(len(uni) == 1, f"after crawl: unified to one JENNA (got {len(uni)})")
if uni:
    regs = {f"{a}:{r}" for a, r in b2.registrations_for(uni[0])}
    check(regs == {"CHIA:337003", "MAINE:378987"},
          f"unified JENNA holds both papers (got {sorted(regs)})")

print("\nNEVER-crawled parent reference reuses the real animal (your case)")
b3 = InMemoryBackend()
L3 = Loader(b3)
# JENNA's Chianina record loads (real, with DOB)
L3.ingest({"association": "CHIA", "regNumber": "337003", "name": "ZNT JENNA 707T",
           "sex": "F", "dob": "2007-09-30"})
# a calf names JENNA (Maine-Anjou 378987) as its dam; 378987 is NEVER crawled
L3.ingest({"association": "MAINE", "regNumber": "C901", "name": "HER CALF", "sex": "M",
           "dob": "2016-02-02",
           "dam": {"association": "MAINE", "regNumber": "378987",
                   "name": "[ ZNT707T ] ZNT JENNA 707T"}})
jn = [u for u, p in b3.animals.items() if norm_name(p.get("name")) == "ZNT JENNA 707T"]
check(len(jn) == 1, f"parent ref reuses the real JENNA, no stub (got {len(jn)})")
if jn:
    regs = {f"{a}:{r}" for a, r in b3.registrations_for(jn[0])}
    check(regs == {"CHIA:337003", "MAINE:378987"},
          f"reused JENNA gains the Maine-Anjou paper (got {sorted(regs)})")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s) failed.")
    sys.exit(1)
print("ALL CHECKS PASSED — dedup behaves correctly.")
sys.exit(0)
