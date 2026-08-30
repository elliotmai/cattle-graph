"""
Offline parser tests for digitalbeef.py, run against saved fixtures.

Fixtures in fixtures/ are real pages for CHIA MA430053 (ZNT MOVES LIKE JAGGER)
plus a page for a registration that does not exist. No network required:

    python test_parsers.py
"""

from __future__ import annotations

import pathlib
import sys

import digitalbeef as db

FIX = pathlib.Path(__file__).parent / "fixtures"
failures: list[str] = []


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def main() -> int:
    print("container / identity block")
    rec = db.parse_container(read("CHIA_MA430053_container.html"),
                             "CHIA", "MA430053", "http://example/test")
    check("name", rec["name"], "ZNT MOVES LIKE JAGGER")
    check("intl_id", rec["intl_id"], "RDPUSAM000000430053")
    check("sex", rec["sex"], "M")
    check("regNumber", rec["regNumber"], "MA430053")
    check("dob (from cross-registration block)", rec["dob"], "2011-09-10")
    check("owner", rec["owner"], "(N) NATALIE MAI ( 65020 )")
    check("cross_refs", rec["cross_refs"], [{"association": "MAINE", "regNumber": "430053"}])
    check("registry_status", rec["attributes"].get("registry_status"), "Active")
    # decimals must survive: the old parser turned 19.733% into 733%
    check("breed_composition", rec["breed_composition"], [
        {"breed": "MA", "percent": 73.049},
        {"breed": "AN", "percent": 19.733},
        {"breed": "CA", "percent": 2.881},
        {"breed": "XX", "percent": 4.338},
    ])
    check("composition sums to ~100", round(sum(c["percent"] for c in rec["breed_composition"])), 100)

    print("\nmissing animal")
    try:
        db.parse_container(read("CHIA_ZZ999999_missing.html"),
                           "CHIA", "ZZ999999", "http://example/test")
        print("  FAIL  a nonexistent registration should raise AnimalNotFound")
        failures.append("AnimalNotFound")
    except db.AnimalNotFound:
        print("  ok    nonexistent registration raises AnimalNotFound")

    print("\npedigree")
    sire, dam, ancestors = db.parse_pedigree(read("CHIA_MA430053_pedigree.html"), "CHIA")
    check("sire", sire, {"association": "CHIA", "regNumber": "MA402617", "name": "GVC SUH 01W"})
    check("dam", dam, {"association": "CHIA", "regNumber": "337003", "name": "ZNT JENNA 707T"})
    check("ancestors found", len(ancestors), 27)
    # the sire is NOT the first cell in document order — position is unusable
    check("sire is not the first ancestor cell", ancestors[0]["regNumber"], "MA242107")

    print("\ndefect code grammar")
    check("AMFT CAFT DDS NHFT PHAFT THFT",
          db.parse_defect_codes("AMFT CAFT DDS NHFT PHAFT THFT"),
          [{"code": "AM", "status": "F"}, {"code": "CA", "status": "F"},
           {"code": "DD", "status": "S"}, {"code": "NH", "status": "F"},
           {"code": "PHA", "status": "F"}, {"code": "TH", "status": "F"}])
    check("PHAS -> PHA suspect", db.parse_defect_codes("PHAS"), [{"code": "PHA", "status": "S"}])
    check("DDS is DD+Suspect, not the DS locus",
          db.parse_defect_codes("DDS"), [{"code": "DD", "status": "S"}])

    print("\ngenotype")
    defects, cases = db.parse_genotype(read("CHIA_MA430053_genotype.html"))
    check("defects", defects, [
        {"code": "AM", "status": "S"}, {"code": "DD", "status": "S"},
        {"code": "NH", "status": "S"}, {"code": "PHA", "status": "F"},
        {"code": "TH", "status": "F"},
    ])
    check("untested conditions are omitted, not recorded free",
          [d["code"] for d in defects].count("CA"), 0)
    check("dna cases", cases, [])

    print("\nepds")
    epds = db.parse_epds(read("CHIA_MA430053_epds.html"))
    check("CED", epds.get("CED"), 10.0)
    check("BW", epds.get("BW"), 1.9)
    check("WW", epds.get("WW"), 41.0)
    check("YW", epds.get("YW"), 55.0)
    check("MILK", epds.get("MILK"), 13.0)

    print("\nprogeny")
    prog = db.parse_progeny(read("CHIA_MA430053_progeny.html"), "CHIA")
    regs = {p["regNumber"] for p in prog}
    check("offspring 372766 found", "372766" in regs, True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all parser checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
