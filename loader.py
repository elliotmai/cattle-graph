"""
Cattle multi-association graph loader with entity resolution / dedup.

Ingests parsed animal records (one per scraped page) and merges them into a
graph where each *real* animal is a single node, keyed by its International ID,
even when it is registered in several breed associations.

Two backends implement the same interface:
  * Neo4jBackend     -> writes to a live Neo4j database (pip install neo4j)
  * InMemoryBackend  -> pure-python graph, no DB needed (dry runs / tests)

Run:
    python loader.py sample_records.json                 # dry run, in-memory
    python loader.py sample_records.json --neo4j \
        --uri bolt://localhost:7687 --user neo4j --password secret

------------------------------------------------------------------
Record schema  (capture EVERYTHING — modeled fields are queryable,
anything else goes in `attributes` so nothing is ever dropped):

    {
      "intl_id":     "USAM20180211Z001",   # International ID -> canonical uid
      "association": "CHIA",                # required
      "regNumber":   "MA430053",            # required
      "name":        "MAB Sherwood Ace 7X",
      "sex":         "M",
      "dob":         "2018-02-11",
      "tattoo":      "MAB7X",
      "color":       "Black",
      "horn_status": "Polled",              # Horned | Polled | Scurred
      "breeder":     "...",
      "owner":       "...",

      "breed_composition": [                # -> (:Animal)-[:HAS_COMPOSITION{percent}]->(:Breed)
        {"breed":"MA","percent":50}, {"breed":"AN","percent":50}
      ],
      "defects": [                          # -> (:Animal)-[:TESTED{status}]->(:Defect)
        {"code":"TH","status":"F"},         #   F = Free, C = Carrier
        {"code":"PHA","status":"C"}
      ],

      "dna_cases":  ["AGI-778812"],         # fallback merge keys
      "cross_refs": [{"association":"MAINE","regNumber":"402303"}],
      "epds":       {"CED":9,"BW":0.8},     # stored on the Registration
      "attributes": {                       # EVERYTHING else, free-form, stored on Registration
        "eid": "840003123456789",
        "progeny_count": 142,
        "genomic_tested": true,
        "ownership_history": ["Sherwood Cattle Co", "Rolling Hills Ranch"],
        "birth_weight_lb": 82,
        "ww_ratio": 105
      },

      "sire": {"intl_id":"...","association":"SHORT","regNumber":"3790685","name":"Big Sky 88"},
      "dam":  {"intl_id":"...","association":"CHIA","regNumber":"400001","name":"MAB Duchess 22"},
      "source_url": "https://chianina.digitalbeef.com/...",
      "scraped_at": "2026-08-12"
    }
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------

def norm_name(name: Optional[str]) -> str:
    """Normalize an animal name for fuzzy matching. Strips a leading bracketed
    tattoo/herd prefix like '[ZNT707T]' that some registries prepend, so
    '[ZNT707T] ZNT JENNA 707T' and 'ZNT JENNA 707T' compare equal."""
    if not name:
        return ""
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", name)          # drop leading [PREFIX]
    s = re.sub(r"[^A-Z0-9 ]", "", s.upper())
    return re.sub(r"\s+", " ", s).strip()


def norm_reg(assoc: str, reg: str) -> tuple[str, str]:
    return (assoc.strip().upper(), reg.strip().upper())


# Breed / composition tokens that appear as the "name" of anonymous commercial
# or foundation parents (e.g. a commercial Angus cow with no papers, shown as
# "ANGUS" born 1988-01-01). These are legitimate DISTINCT animals — two calves
# out of two such cows are two different cows — so they must never be merged by
# name+DOB. A composition string like "AN X MA" is detected by the " X ".
BREED_TOKENS = {
    "ANGUS", "RED ANGUS", "SIMMENTAL", "SIM", "CHIANINA", "CHIA", "MAINE ANJOU",
    "MAINEANJOU", "MAINE", "SHORTHORN", "SHORT", "HEREFORD", "HERF", "GELBVIEH",
    "GELB", "LIMOUSIN", "LIMI", "SALERS", "BRAUNVIEH", "BRAUN", "CHIANGUS",
    "COMMERCIAL", "AN", "MA", "CA", "SM", "SH", "HH", "XX", "UNKNOWN",
}


def is_generic_name(name_norm: str) -> bool:
    """True if the name is a breed/foundation placeholder rather than a real
    registered animal name — used to keep anonymous commercial parents from
    being merged together by the name+DOB tier."""
    if not name_norm:
        return True
    if name_norm in BREED_TOKENS:
        return True
    if " X " in name_norm:                 # composition, e.g. "AN X MA", "CA X AN"
        return True
    # e.g. "1/2 AN 1/2 MA" style fraction descriptors
    if re.match(r"^[0-9/ ]*(AN|MA|CA|SM|SH)( |$)", name_norm) and not re.search(r"[A-Z]{3,}", name_norm):
        return True
    return False


def norm_defect_status(raw) -> str:
    """Collapse any raw genetic-test result to one of:
        Free    - tested free, or 'Free by Pedigree'
        Carrier - confirmed carrier
        Suspect - has a carrier in its pedigree but is untested (DigitalBeef 'S')
        Unknown - blank / not listed / anything else
    """
    s = (raw or "").strip().upper()
    if "FREE" in s or "CLEAR" in s or "NORMAL" in s or s.startswith("F"):
        return "Free"
    if "CARRIER" in s or s.startswith("C"):
        return "Carrier"
    if "SUSPECT" in s or s.startswith("S"):
        return "Suspect"
    return "Unknown"


def is_specific_name(name_norm: str) -> bool:
    """A distinctive registered name (herd prefix + a numeric tag, e.g.
    'ZNT JENNA 707T') that is safe to match on name alone — used only to attach
    un-crawled parent references to the animal they clearly name. Generic or
    short names never qualify, so anonymous commercial animals are never fused."""
    if is_generic_name(name_norm):
        return False
    return any(ch.isdigit() for ch in name_norm) and len(name_norm.split()) >= 2


def norm_id(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def norm_horn(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    if not v:
        return None
    if v.startswith("horn"):
        return "Horned"
    if v.startswith("poll") or v in ("p", "pp"):
        return "Polled"
    if v.startswith("scur"):
        return "Scurred"
    return value.strip().title()


# --------------------------------------------------------------------------
# Backend interface
# --------------------------------------------------------------------------

class GraphBackend(ABC):
    # ---- lookups (return an existing Animal uid or None) ----
    @abstractmethod
    def find_by_intl_id(self, intl_id: str) -> Optional[str]: ...
    @abstractmethod
    def find_by_registration(self, assoc: str, reg: str) -> Optional[str]: ...
    @abstractmethod
    def find_by_dna(self, case_ids: list[str]) -> Optional[str]: ...
    @abstractmethod
    def find_by_tattoo_dob_sex(self, tattoo: str, dob: str, sex: str) -> Optional[str]: ...
    @abstractmethod
    def find_by_name_dob(self, name_norm: str, dob: str) -> Optional[str]: ...
    @abstractmethod
    def find_all_by_name_dob(self, name_norm: str, dob: str) -> list[str]: ...
    @abstractmethod
    def find_real_by_name(self, name_norm: str) -> Optional[str]:
        """A fully-crawled animal (has a DOB) with this exact normalized name."""
    @abstractmethod
    def find_bare_stubs_by_name(self, name_norm: str) -> list[str]:
        """Un-crawled placeholder nodes (no DOB) with this exact normalized name."""

    # ---- writes ----
    @abstractmethod
    def create_animal(self, uid: str, props: dict) -> None: ...
    @abstractmethod
    def enrich_animal(self, uid: str, props: dict) -> None: ...
    @abstractmethod
    def attach_registration(self, uid: str, assoc: str, reg: str, props: dict) -> None: ...
    @abstractmethod
    def attach_dna(self, uid: str, case_id: str) -> None: ...
    @abstractmethod
    def attach_breed(self, uid: str, breed_code: str, percent: float) -> None: ...
    @abstractmethod
    def attach_defect(self, uid: str, defect_code: str, status: str) -> None: ...
    @abstractmethod
    def link_parent(self, child_uid: str, parent_uid: str, role: str) -> None: ...
    @abstractmethod
    def merge_animals(self, keep_uid: str, drop_uid: str) -> None:
        """Fold drop_uid's registrations/edges/props into keep_uid and remove it."""

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Resolver + ingest
# --------------------------------------------------------------------------

@dataclass
class Stats:
    records: int = 0
    animals_created: int = 0
    merges: dict[str, int] = field(default_factory=lambda: {
        "intl_id": 0, "registration": 0, "dna": 0, "cross_ref": 0,
        "tattoo_dob_sex": 0, "name_dob": 0,
    })

    def report(self) -> str:
        merged = sum(self.merges.values())
        lines = [
            f"records ingested : {self.records}",
            f"animals created  : {self.animals_created}",
            f"records merged   : {merged}  (collapsed onto an existing animal)",
        ]
        for k, v in self.merges.items():
            if v:
                lines.append(f"    via {k:<15}: {v}")
        return "\n".join(lines)


class Loader:
    def __init__(self, backend: GraphBackend, skip_steers: bool = False):
        self.backend = backend
        self.stats = Stats()
        self.skip_steers = skip_steers

    # ---- entity-resolution ladder, strongest key first ----
    def resolve_animal(self, rec: dict) -> tuple[str, bool]:
        b = self.backend

        # Tier 0: International ID — the standardized cross-association identifier.
        intl = norm_id(rec.get("intl_id"))
        if intl:
            uid = b.find_by_intl_id(intl)
            if uid:
                self.stats.merges["intl_id"] += 1
                return uid, False

        # Tier 1: this exact registration already maps to an animal.
        assoc, reg = norm_reg(rec["association"], rec["regNumber"])
        uid = b.find_by_registration(assoc, reg)
        if uid:
            self.stats.merges["registration"] += 1
            return uid, False

        # Tier 2: DNA case number.
        dna = [norm_id(c) for c in rec.get("dna_cases", []) if norm_id(c)]
        if dna:
            uid = b.find_by_dna(dna)
            if uid:
                self.stats.merges["dna"] += 1
                return uid, False

        # Tier 3: cross-referenced registration in another association.
        for xref in rec.get("cross_refs", []):
            xa, xr = norm_reg(xref["association"], xref["regNumber"])
            uid = b.find_by_registration(xa, xr)
            if uid:
                self.stats.merges["cross_ref"] += 1
                return uid, False

        # Tier 4: tattoo + DOB + sex.
        tattoo = norm_id(rec.get("tattoo"))
        dob = (rec.get("dob") or "").strip()
        sex = norm_id(rec.get("sex"))
        if tattoo and dob and sex:
            uid = b.find_by_tattoo_dob_sex(tattoo, dob, sex)
            if uid:
                self.stats.merges["tattoo_dob_sex"] += 1
                return uid, False

        # Tier 5: normalized name + DOB — but ONLY for real registered names.
        # Generic breed/foundation placeholders ("ANGUS", "AN X MA", …) are
        # distinct animals and must not be merged this way.
        nm = norm_name(rec.get("name"))
        if nm and dob and not is_generic_name(nm):
            uid = b.find_by_name_dob(nm, dob)
            if uid:
                self.stats.merges["name_dob"] += 1
                return uid, False

        # No match -> new animal with a DETERMINISTIC, registration-based uid
        # ("CHIA:MA430053"). Not the per-association International ID (unreliable
        # across registries) and never a random id, so a repeated or concurrent
        # load of the same registration collapses onto one node instead of
        # duplicating it.
        uid = f"{assoc}:{reg}"
        b.create_animal(uid, self._animal_props(rec))
        self.stats.animals_created += 1
        return uid, True

    @staticmethod
    def _animal_props(rec: dict) -> dict:
        return {
            "intl_id": norm_id(rec.get("intl_id")) or None,
            "name": rec.get("name"),
            "name_norm": norm_name(rec.get("name")),
            "sex": norm_id(rec.get("sex")) or None,
            "dob": (rec.get("dob") or "").strip() or None,
            "tattoo": norm_id(rec.get("tattoo")) or None,
            "color": (rec.get("color") or "").strip() or None,
            "horn_status": norm_horn(rec.get("horn_status")),
        }

    def _ensure_parent(self, parent: Optional[dict]) -> Optional[str]:
        if not parent:
            return None
        intl = norm_id(parent.get("intl_id"))
        if intl:
            uid = self.backend.find_by_intl_id(intl)
            if uid:
                return uid
        if parent.get("regNumber"):
            pa, pr = norm_reg(parent["association"], parent["regNumber"])
            uid = self.backend.find_by_registration(pa, pr)
            if uid:
                return uid
        else:
            pa = pr = None
        if not intl and not pr:
            # A parent known only by a breed + year (anonymous commercial dam)
            # has no stable identifier and can't be reliably linked or deduped.
            # Skip the node/edge rather than mint a random one that would look
            # like a duplicate. (When such a parent is itself crawled, it enters
            # as a full record with its own registration.)
            return None
        # If this parent reference clearly names an already-crawled animal, reuse
        # that node (and add this registration to it) instead of creating a new
        # stub — this is what stops an un-crawled parent showing up as a separate
        # copy of a real animal.
        pname = norm_name(parent.get("name"))
        if is_specific_name(pname):
            real = self.backend.find_real_by_name(pname)
            if real:
                if pr:
                    self.backend.attach_registration(real, pa, pr, {"stub": True})
                return real
        # Deterministic uid: registration-based when known, else International ID.
        uid = f"{pa}:{pr}" if pr else intl
        self.backend.create_animal(uid, {
            "intl_id": intl or None,
            "name": parent.get("name"),
            "name_norm": norm_name(parent.get("name")),
            "sex": None, "dob": None, "tattoo": None, "color": None, "horn_status": None,
        })
        if pr:
            self.backend.attach_registration(uid, pa, pr, {"stub": True})
        self.stats.animals_created += 1
        return uid

    def _unify_by_name(self, uid: str, rec: dict) -> str:
        """After a record gives this node a real name + DOB, fold in any OTHER
        existing node that shares that name+DOB (e.g. a stub crawled earlier
        under a different association's number). This merges duplicates that the
        registration tier can't catch, and only fires for real registered names
        so anonymous commercial/foundation animals are never fused."""
        nn = norm_name(rec.get("name"))
        dob = (rec.get("dob") or "").strip()
        if not (nn and dob) or is_generic_name(nn):
            return uid
        for other in self.backend.find_all_by_name_dob(nn, dob):
            if other != uid:
                self.backend.merge_animals(uid, other)
                self.stats.merges["name_dob"] += 1
        return uid

    def _absorb_stubs(self, uid: str, rec: dict) -> str:
        """A real record (has DOB, distinctive name) absorbs any un-crawled
        parent stubs that name the same animal under a different registration."""
        nn = norm_name(rec.get("name"))
        dob = (rec.get("dob") or "").strip()
        if not dob or not is_specific_name(nn):
            return uid
        for stub in self.backend.find_bare_stubs_by_name(nn):
            if stub != uid:
                self.backend.merge_animals(uid, stub)
                self.stats.merges["name_dob"] += 1
        return uid

    def ingest(self, rec: dict):
        if self.skip_steers and norm_id(rec.get("sex")) == "S":
            return None                      # ignore castrated males entirely
        self.stats.records += 1
        uid, created = self.resolve_animal(rec)

        if not created:
            self.backend.enrich_animal(uid, self._animal_props(rec))

        # Fold in any other node that now proves to be the same animal.
        uid = self._unify_by_name(uid, rec)
        uid = self._absorb_stubs(uid, rec)

        # Registration node holds association-specific data + EVERYTHING else.
        assoc, reg = norm_reg(rec["association"], rec["regNumber"])
        self.backend.attach_registration(uid, assoc, reg, {
            "name_as_recorded": rec.get("name"),
            "breeder": rec.get("breeder"),
            "owner": rec.get("owner"),
            "epds": json.dumps(rec.get("epds", {})),
            "attributes": json.dumps(rec.get("attributes", {})),  # full free-form bag
            "source_url": rec.get("source_url"),
            "scraped_at": rec.get("scraped_at"),
            "stub": False,
        })

        # DNA keys.
        for c in rec.get("dna_cases", []):
            if norm_id(c):
                self.backend.attach_dna(uid, norm_id(c))

        # Breed composition (percentages).
        for comp in rec.get("breed_composition", []):
            code = norm_id(comp.get("breed"))
            if code:
                self.backend.attach_breed(uid, code, float(comp.get("percent", 0)))

        # Genetic defect result, normalized to Free / Carrier / Unknown.
        for d in rec.get("defects", []):
            code = norm_id(d.get("code"))
            if code:
                self.backend.attach_defect(uid, code, norm_defect_status(d.get("status")))

        # Cross-referenced registrations become registration nodes too.
        for xref in rec.get("cross_refs", []):
            xa, xr = norm_reg(xref["association"], xref["regNumber"])
            if self.backend.find_by_registration(xa, xr) is None:
                self.backend.attach_registration(uid, xa, xr, {"stub": True, "via_cross_ref": True})

        # Parentage edges (Animal -> Animal).
        sire_uid = self._ensure_parent(rec.get("sire"))
        if sire_uid:
            self.backend.link_parent(uid, sire_uid, "SIRE")
        dam_uid = self._ensure_parent(rec.get("dam"))
        if dam_uid:
            self.backend.link_parent(uid, dam_uid, "DAM")

        return uid

    def load_all(self, records: list[dict]) -> None:
        for rec in records:
            self.ingest(rec)


# --------------------------------------------------------------------------
# In-memory backend (no database) — used for dry runs and tests
# --------------------------------------------------------------------------

class InMemoryBackend(GraphBackend):
    def __init__(self):
        self.animals: dict[str, dict] = {}
        self.intl_index: dict[str, str] = {}
        self.reg_index: dict[tuple[str, str], str] = {}
        self.dna_index: dict[str, str] = {}
        self.registrations: dict[tuple[str, str], dict] = {}
        self.breed_edges: list[tuple[str, str, float]] = []   # (uid, breed, pct)
        self.defect_edges: dict[tuple[str, str], str] = {}    # (uid, defect) -> status
        self.parent_edges: list[tuple[str, str, str]] = []    # (child, parent, role)

    def find_by_intl_id(self, intl_id):
        return self.intl_index.get(intl_id)

    def find_by_registration(self, assoc, reg):
        return self.reg_index.get((assoc, reg))

    def find_by_dna(self, case_ids):
        for c in case_ids:
            if c in self.dna_index:
                return self.dna_index[c]
        return None

    def find_by_tattoo_dob_sex(self, tattoo, dob, sex):
        for uid, p in self.animals.items():
            if p.get("tattoo") == tattoo and p.get("dob") == dob and p.get("sex") == sex:
                return uid
        return None

    def find_by_name_dob(self, name_norm, dob):
        for uid, p in self.animals.items():
            if name_norm and p.get("name_norm") == name_norm and p.get("dob") == dob:
                return uid
        return None

    def find_all_by_name_dob(self, name_norm, dob):
        if not name_norm or not dob:
            return []
        return [uid for uid, p in self.animals.items()
                if p.get("name_norm") == name_norm and p.get("dob") == dob]

    def find_real_by_name(self, name_norm):
        if not name_norm:
            return None
        for uid, p in self.animals.items():
            if p.get("name_norm") == name_norm and p.get("dob"):
                return uid
        return None

    def find_bare_stubs_by_name(self, name_norm):
        if not name_norm:
            return []
        return [uid for uid, p in self.animals.items()
                if p.get("name_norm") == name_norm and not p.get("dob")]

    def merge_animals(self, keep_uid, drop_uid):
        if keep_uid == drop_uid or drop_uid not in self.animals:
            return
        keep = self.animals.setdefault(keep_uid, {})
        drop = self.animals.pop(drop_uid)
        for k, v in drop.items():                      # fill gaps on the kept node
            if v and not keep.get(k):
                keep[k] = v
        for idx in (self.reg_index, self.dna_index, self.intl_index):
            for key, u in list(idx.items()):
                if u == drop_uid:
                    idx[key] = keep_uid
        self.breed_edges = list({(keep_uid if u == drop_uid else u, b, p)
                                 for (u, b, p) in self.breed_edges})
        self.defect_edges = {((keep_uid if u == drop_uid else u), c): s
                             for (u, c), s in self.defect_edges.items()}
        self.parent_edges = list({(keep_uid if c == drop_uid else c,
                                   keep_uid if pa == drop_uid else pa, role)
                                  for (c, pa, role) in self.parent_edges
                                  if not (c == pa == drop_uid)})

    def create_animal(self, uid, props):
        self.animals[uid] = dict(props)
        if props.get("intl_id"):
            self.intl_index[props["intl_id"]] = uid

    def enrich_animal(self, uid, props):
        cur = self.animals.setdefault(uid, {})
        for k, v in props.items():
            if v and not cur.get(k):
                cur[k] = v
        if props.get("intl_id"):
            self.intl_index.setdefault(props["intl_id"], uid)

    def attach_registration(self, uid, assoc, reg, props):
        key = (assoc, reg)
        existing = self.registrations.get(key, {})
        if existing.get("stub") and not props.get("stub"):
            existing.update(props)
        else:
            self.registrations.setdefault(key, {}).update(props)
        self.reg_index[key] = uid

    def attach_dna(self, uid, case_id):
        self.dna_index[case_id] = uid

    def attach_breed(self, uid, breed_code, percent):
        edge = (uid, breed_code, percent)
        if edge not in self.breed_edges:
            self.breed_edges.append(edge)

    def attach_defect(self, uid, defect_code, status):
        self.defect_edges[(uid, defect_code)] = status

    def link_parent(self, child_uid, parent_uid, role):
        edge = (child_uid, parent_uid, role)
        if edge not in self.parent_edges:
            self.parent_edges.append(edge)

    # convenience for tests / dry-run printout
    def registrations_for(self, uid):
        return [k for k, v in self.reg_index.items() if v == uid]

    def composition_for(self, uid):
        return [(b, p) for (u, b, p) in self.breed_edges if u == uid]

    def defects_for(self, uid):
        return {d: s for (u, d), s in self.defect_edges.items() if u == uid}


# --------------------------------------------------------------------------
# Neo4j backend
# --------------------------------------------------------------------------

class Neo4jBackend(GraphBackend):
    def __init__(self, uri, user, password, insecure=False):
        from neo4j import GraphDatabase
        if insecure:
            uri = uri.replace("neo4j+s://", "neo4j+ssc://").replace("bolt+s://", "bolt+ssc://")
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver.verify_connectivity()  # fail fast with a clear message

    def _run(self, cypher, **params):
        with self.driver.session() as s:
            return list(s.run(cypher, **params))

    def find_by_intl_id(self, intl_id):
        rows = self._run(
            "MATCH (a:Animal {intl_id:$id}) RETURN a.uid AS uid LIMIT 1", id=intl_id)
        return rows[0]["uid"] if rows else None

    def find_by_registration(self, assoc, reg):
        rows = self._run(
            "MATCH (a:Animal)-[:HAS_REGISTRATION]->(:Registration "
            "{association:$assoc, regNumber:$reg}) RETURN a.uid AS uid LIMIT 1",
            assoc=assoc, reg=reg)
        return rows[0]["uid"] if rows else None

    def find_by_dna(self, case_ids):
        rows = self._run(
            "MATCH (a:Animal)-[:HAS_DNA]->(d:DnaCase) WHERE d.caseId IN $ids "
            "RETURN a.uid AS uid LIMIT 1", ids=case_ids)
        return rows[0]["uid"] if rows else None

    def find_by_tattoo_dob_sex(self, tattoo, dob, sex):
        rows = self._run(
            "MATCH (a:Animal {tattoo:$t, dob:$d, sex:$s}) RETURN a.uid AS uid LIMIT 1",
            t=tattoo, d=dob, s=sex)
        return rows[0]["uid"] if rows else None

    def find_by_name_dob(self, name_norm, dob):
        rows = self._run(
            "MATCH (a:Animal {name_norm:$n, dob:$d}) RETURN a.uid AS uid LIMIT 1",
            n=name_norm, d=dob)
        return rows[0]["uid"] if rows else None

    def find_all_by_name_dob(self, name_norm, dob):
        if not name_norm or not dob:
            return []
        rows = self._run(
            "MATCH (a:Animal {name_norm:$n, dob:$d}) RETURN a.uid AS uid", n=name_norm, d=dob)
        return [r["uid"] for r in rows]

    def find_real_by_name(self, name_norm):
        if not name_norm:
            return None
        rows = self._run(
            "MATCH (a:Animal {name_norm:$n}) WHERE a.dob IS NOT NULL "
            "RETURN a.uid AS uid LIMIT 1", n=name_norm)
        return rows[0]["uid"] if rows else None

    def find_bare_stubs_by_name(self, name_norm):
        if not name_norm:
            return []
        rows = self._run(
            "MATCH (a:Animal {name_norm:$n}) WHERE a.dob IS NULL RETURN a.uid AS uid", n=name_norm)
        return [r["uid"] for r in rows]

    def merge_animals(self, keep_uid, drop_uid):
        if keep_uid == drop_uid:
            return
        # APOC (built in on Aura) folds drop's registrations/edges/props into keep.
        self._run(
            "MATCH (k:Animal {uid:$keep}), (d:Animal {uid:$drop}) "
            "CALL apoc.refactor.mergeNodes([k, d], "
            "{properties:'discard', mergeRels:true}) YIELD node RETURN node.uid",
            keep=keep_uid, drop=drop_uid)

    def create_animal(self, uid, props):
        # MERGE (not CREATE) so a repeated/interleaved load can never create a
        # second node for the same uid — idempotent and constraint-safe.
        self._run("MERGE (a:Animal {uid:$uid}) SET a += $props", uid=uid, props=_clean(props))

    def enrich_animal(self, uid, props):
        self._run(
            "MATCH (a:Animal {uid:$uid}) "
            "SET a.intl_id     = coalesce(a.intl_id, $intl_id), "
            "    a.name        = coalesce(a.name, $name), "
            "    a.name_norm   = coalesce(a.name_norm, $name_norm), "
            "    a.sex         = coalesce(a.sex, $sex), "
            "    a.dob         = coalesce(a.dob, $dob), "
            "    a.tattoo      = coalesce(a.tattoo, $tattoo), "
            "    a.color       = coalesce(a.color, $color), "
            "    a.horn_status = coalesce(a.horn_status, $horn_status)",
            uid=uid, **{k: props.get(k) for k in
                        ("intl_id", "name", "name_norm", "sex", "dob", "tattoo", "color", "horn_status")})

    def attach_registration(self, uid, assoc, reg, props):
        self._run(
            "MATCH (a:Animal {uid:$uid}) "
            "MERGE (r:Registration {association:$assoc, regNumber:$reg}) "
            "SET r += $props "
            "MERGE (a)-[:HAS_REGISTRATION]->(r) "
            "MERGE (assoc:Association {code:$assoc}) "
            "MERGE (r)-[:IN_ASSOCIATION]->(assoc)",
            uid=uid, assoc=assoc, reg=reg, props=_clean(props))

    def attach_dna(self, uid, case_id):
        self._run(
            "MATCH (a:Animal {uid:$uid}) MERGE (d:DnaCase {caseId:$c}) "
            "MERGE (a)-[:HAS_DNA]->(d)", uid=uid, c=case_id)

    def attach_breed(self, uid, breed_code, percent):
        self._run(
            "MATCH (a:Animal {uid:$uid}) MERGE (b:Breed {code:$code}) "
            "MERGE (a)-[c:HAS_COMPOSITION]->(b) SET c.percent = $pct",
            uid=uid, code=breed_code, pct=percent)

    def attach_defect(self, uid, defect_code, status):
        self._run(
            "MATCH (a:Animal {uid:$uid}) MERGE (x:Defect {code:$code}) "
            "MERGE (a)-[t:TESTED]->(x) SET t.status = $status",
            uid=uid, code=defect_code, status=status)

    def link_parent(self, child_uid, parent_uid, role):
        rel = "SIRE_OF" if role == "SIRE" else "DAM_OF"
        self._run(
            f"MATCH (c:Animal {{uid:$c}}), (p:Animal {{uid:$p}}) "
            f"MERGE (p)-[:{rel}]->(c)", c=child_uid, p=parent_uid)

    def close(self):
        self.driver.close()


def _clean(props: dict) -> dict:
    return {k: v for k, v in props.items() if v is not None}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_records(path: str) -> list[dict]:
    """Accept either a JSON array (.json) or one-record-per-line JSONL (.jsonl),
    the latter being what crawl.py appends."""
    with open(path, encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            out = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # tolerate a partially-written final line if a crawler is
                    # still appending to this file (load-while-crawling is safe)
                    pass
            return out
        text = f.read().strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # tolerate JSONL even without the extension
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return data if isinstance(data, list) else [data]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Load cattle records into the graph.")
    ap.add_argument("records", help="Path to JSON file (list of records).")
    ap.add_argument("--neo4j", action="store_true", help="Write to a live Neo4j DB.")
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="neo4j")
    ap.add_argument("--insecure", action="store_true",
                    help="Encrypt but skip cert verification (neo4j+s -> neo4j+ssc), "
                         "for networks that intercept TLS.")
    ap.add_argument("--skip-steers", action="store_true",
                    help="Ignore records for steers (castrated males, sex=S).")
    args = ap.parse_args(argv)

    records = load_records(args.records)

    backend: GraphBackend = (
        Neo4jBackend(args.uri, args.user, args.password, insecure=args.insecure) if args.neo4j
        else InMemoryBackend()
    )
    loader = Loader(backend, skip_steers=args.skip_steers)
    loader.load_all(records)

    print("=== load complete ===")
    print(loader.stats.report())

    if isinstance(backend, InMemoryBackend):
        print("\n=== canonical animals (dry run) ===")
        for uid, p in backend.animals.items():
            regs = ", ".join(f"{a}:{r}" for a, r in backend.registrations_for(uid))
            comp = ", ".join(f"{b} {pct:g}%" for b, pct in backend.composition_for(uid))
            defs = ", ".join(f"{d}={s}" for d, s in backend.defects_for(uid).items())
            print(f"  {p.get('name') or '(unnamed)':<26} uid={uid}")
            print(f"      color={p.get('color')}  horn={p.get('horn_status')}  regs=[{regs}]")
            if comp:
                print(f"      breed=[{comp}]")
            if defs:
                print(f"      defects=[{defs}]")

    backend.close()


if __name__ == "__main__":
    sys.exit(main())
