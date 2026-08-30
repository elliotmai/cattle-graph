"""
Traced run of the cattle graph loader -> event stream for the constellation UI.

Subclasses InMemoryBackend / Loader so every graph mutation is recorded in the
order it actually happened, with the wall-clock offset at which it happened.
loader.py itself is untouched: this is a pure decorator around it.

    python trace_loader.py                      # uses ../sample_records.json
    python trace_loader.py path/to/records.json
    python trace_loader.py ../records.jsonl     # output of crawl.py

Writes:
    events.json        the raw event stream
    constellation.html template + events, inlined into one standalone page
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent
sys.path.insert(0, str(OUTPUTS))

from loader import InMemoryBackend, Loader, load_records  # noqa: E402


# --------------------------------------------------------------------------
# Backend that narrates itself
# --------------------------------------------------------------------------

class TracingBackend(InMemoryBackend):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []
        self._t0 = time.perf_counter()
        self._stub_ctx = False

    def emit(self, **ev) -> dict:
        ev["t"] = round((time.perf_counter() - self._t0) * 1000, 4)
        self.events.append(ev)
        return ev

    # ---- writes ----

    def create_animal(self, uid, props):
        super().create_animal(uid, props)
        self.emit(type="animal", uid=uid, stub=self._stub_ctx,
                  name=props.get("name"), sex=props.get("sex"), dob=props.get("dob"),
                  color=props.get("color"), horn=props.get("horn_status"),
                  intl_id=props.get("intl_id"), tattoo=props.get("tattoo"))

    def enrich_animal(self, uid, props):
        before = dict(self.animals.get(uid, {}))
        super().enrich_animal(uid, props)
        after = self.animals.get(uid, {})
        gained = {k: v for k, v in after.items() if v and not before.get(k)}
        self.emit(type="enrich", uid=uid, gained=gained,
                  name=after.get("name"), color=after.get("color"),
                  horn=after.get("horn_status"), sex=after.get("sex"),
                  dob=after.get("dob"), intl_id=after.get("intl_id"))

    def attach_registration(self, uid, assoc, reg, props):
        fresh = (assoc, reg) not in self.reg_index
        super().attach_registration(uid, assoc, reg, props)
        self.emit(type="registration", uid=uid, assoc=assoc, reg=reg, new=fresh,
                  stub=bool(props.get("stub")),
                  via_cross_ref=bool(props.get("via_cross_ref")),
                  owner=props.get("owner"), breeder=props.get("breeder"),
                  epds=_maybe_json(props.get("epds")),
                  attributes=_maybe_json(props.get("attributes")),
                  source_url=props.get("source_url"))

    def attach_dna(self, uid, case_id):
        fresh = case_id not in self.dna_index
        super().attach_dna(uid, case_id)
        self.emit(type="dna", uid=uid, case=case_id, new=fresh)

    def attach_breed(self, uid, breed_code, percent):
        n = len(self.breed_edges)
        super().attach_breed(uid, breed_code, percent)
        self.emit(type="breed", uid=uid, code=breed_code, percent=percent,
                  new=len(self.breed_edges) > n)

    def attach_defect(self, uid, defect_code, status):
        fresh = (uid, defect_code) not in self.defect_edges
        super().attach_defect(uid, defect_code, status)
        self.emit(type="defect", uid=uid, code=defect_code, status=status, new=fresh)

    def link_parent(self, child_uid, parent_uid, role):
        n = len(self.parent_edges)
        super().link_parent(child_uid, parent_uid, role)
        self.emit(type="parent", child=child_uid, parent=parent_uid, role=role,
                  new=len(self.parent_edges) > n)


class TracingLoader(Loader):
    """Marks record boundaries and reports which resolution tier won."""

    def resolve_animal(self, rec):
        before = dict(self.stats.merges)
        uid, created = super().resolve_animal(rec)
        if not created:
            via = next((k for k, v in self.stats.merges.items() if v > before[k]), "unknown")
            self.backend.emit(type="merge", uid=uid, via=via)
        return uid, created

    def _ensure_parent(self, parent):
        self.backend._stub_ctx = True
        try:
            return super()._ensure_parent(parent)
        finally:
            self.backend._stub_ctx = False

    def ingest(self, rec):
        self.backend.emit(type="record_start", index=self.stats.records,
                          assoc=rec["association"], reg=rec["regNumber"],
                          name=rec.get("name"), intl_id=rec.get("intl_id") or None,
                          source_url=rec.get("source_url"))
        uid = super().ingest(rec)
        self.backend.emit(type="record_end", index=self.stats.records - 1, uid=uid,
                          stats={"records": self.stats.records,
                                 "animals": self.stats.animals_created,
                                 "merges": dict(self.stats.merges)})
        return uid


def _maybe_json(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed or None


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(records_path: Path) -> dict:
    # load_records handles both a JSON array and the JSONL that crawl.py appends
    records = load_records(str(records_path))

    backend = TracingBackend()
    loader = TracingLoader(backend)
    wall = time.perf_counter()
    loader.load_all(records)
    elapsed_ms = (time.perf_counter() - wall) * 1000

    animals = []
    for uid, p in backend.animals.items():
        animals.append({
            "uid": uid,
            "name": p.get("name"),
            "color": p.get("color"),
            "horn": p.get("horn_status"),
            "registrations": [f"{a}:{r}" for a, r in backend.registrations_for(uid)],
            "composition": [{"breed": b, "percent": pct} for b, pct in backend.composition_for(uid)],
            "defects": backend.defects_for(uid),
        })

    return {
        "source": records_path.name,
        "elapsed_ms": round(elapsed_ms, 3),
        "summary": {
            "records": loader.stats.records,
            "animals": len(backend.animals),
            "registrations": len(backend.reg_index),
            "associations": len({a for a, _ in backend.reg_index}),
            "breeds": len({b for _, b, _ in backend.breed_edges}),
            "defects": len({d for _, d in backend.defect_edges}),
            "dna_cases": len(backend.dna_index),
            "parent_edges": len(backend.parent_edges),
            "merges": dict(loader.stats.merges),
        },
        "animals": animals,
        "events": backend.events,
    }, loader, backend


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    records_path = Path(argv[0]) if argv else OUTPUTS / "sample_records.json"

    data, loader, backend = build(records_path)

    (HERE / "events.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    template = HERE / "constellation.template.html"
    if template.exists():
        html = template.read_text(encoding="utf-8")
        payload = json.dumps(data, separators=(",", ":"))
        if "/*__DATA__*/null" not in html:
            raise SystemExit("template is missing the /*__DATA__*/null placeholder")
        (HERE / "constellation.html").write_text(
            html.replace("/*__DATA__*/null", payload), encoding="utf-8")

    print("=== traced load complete ===")
    print(loader.stats.report())
    print(f"\nreal loader time : {data['elapsed_ms']:.3f} ms")
    print(f"events captured  : {len(data['events'])}")
    s = data["summary"]
    print(f"constellation    : {s['animals']} animals, {s['registrations']} registrations, "
          f"{s['associations']} associations, {s['breeds']} breeds, "
          f"{s['defects']} defects, {s['dna_cases']} DNA cases, "
          f"{s['parent_edges']} parentage links")

    print("\n=== canonical animals ===")
    for a in data["animals"]:
        comp = ", ".join(f"{c['breed']} {c['percent']:g}%" for c in a["composition"])
        defs = ", ".join(f"{d}={s}" for d, s in a["defects"].items())
        print(f"  {a['name'] or '(unnamed)':<26} uid={a['uid']}")
        print(f"      color={a['color']}  horn={a['horn']}  regs=[{', '.join(a['registrations'])}]")
        if comp:
            print(f"      breed=[{comp}]")
        if defs:
            print(f"      defects=[{defs}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
