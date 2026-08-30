"""
American Angus Association (angus.org) adapter.

Emits the SAME record format as digitalbeef.py so both feed one loader/graph.

Confirmed against the live site (Aug 2026)
------------------------------------------
angus.org is a server-rendered ASP.NET MVC site (BeefRecords.Mvc). There is NO
separate JSON API — the animal data is in the HTML of:

    /Animal/EpdPedDtl?aid=<opaque>&time=<opaque>

The aid/time tokens are generated per request and can't be built from a reg
number, so you reach an animal by SEARCH:

    GET  /find-an-animal                      -> form w/ antiforgery hidden fields + cookie
    POST /find-an-animal
         EpdPedSearchRequest.sAnimalRegNum=<reg>   (+ echo the hidden fields)
    -> 302 redirect to /Animal/EpdPedDtl?aid=...&time=...  (the detail HTML)

Recursion: the detail page's `table.pedigree` lists every ancestor with its
`AAA #<reg>` number. We extract those reg numbers as neighbors and resolve each
the same reg->detail way (tokens are ephemeral, so we key everything on the
stable registration number, never on aid).

Two parses are heuristic and flagged inline: (1) which pedigree cells are the
direct sire vs dam, and (2) splitting the compact EPD cells. Identity, genetic
conditions, and neighbor discovery are solid. The full EPD section is also
stored raw under attributes.epd_raw so nothing is ever lost.
"""

from __future__ import annotations

import re
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

import registries

log = logging.getLogger("angus")

BASE = "https://www.angus.org"
FIND_URL = BASE + "/find-an-animal"
REG_FIELD = "EpdPedSearchRequest.sAnimalRegNum"

# Angus genetic-defect loci seen on the site. Status is the trailing letter of
# each token: <CODE>F = Free, <CODE>C = Carrier, <CODE>A = Affected.
STATUS_LETTER = {"F": "F", "C": "C", "A": "A"}

SEX_MAP = {"BULL": "M", "COW": "F", "HEIFER": "F", "STEER": "S",
           "MALE": "M", "FEMALE": "F"}


def representative_url(reg: str) -> str:
    """A stable URL on the host for robots.txt checks (not the real token URL)."""
    return f"{BASE}/Animal/EpdPedDtl?reg={reg}"


# --------------------------------------------------------------------------
# Reg number -> detail HTML (via the search form)
# --------------------------------------------------------------------------

class AngusBotProtected(RuntimeError):
    """angus.org served an Imperva Incapsula bot-protection challenge instead of
    the page. A plain HTTP client can't clear it. The fix is NOT to defeat the
    challenge but to have the association allowlist this crawler in Incapsula
    (by source IP, or a custom header/User-Agent on an allow rule)."""


def _looks_like_incapsula(resp) -> bool:
    body = resp.text or ""
    markers = ("Incapsula", "_Incapsula_Resource", "/_Incapsula_", "___utmvc")
    if any(m in body for m in markers):
        return True
    # challenge pages are short and carry Incapsula/Imperva cookies
    setc = resp.headers.get("Set-Cookie", "")
    return "incap_ses_" in setc or "visid_incap" in setc


def _get_search_form(session: requests.Session, timeout: int):
    """GET the search page and return (action_url, hidden_fields) with the
    antiforgery token echoed back. The session cookie is retained automatically."""
    resp = session.get(FIND_URL, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    form = None
    for f in soup.find_all("form"):
        if f.find("input", attrs={"name": REG_FIELD}):
            form = f
            break
    if form is None:
        if _looks_like_incapsula(resp):
            raise AngusBotProtected(
                "angus.org is behind Imperva Incapsula bot protection — a plain "
                "HTTP client is served a JS challenge instead of the search form. "
                "Ask the association to allowlist this crawler's IP / User-Agent "
                "in Incapsula; do not attempt to bypass the challenge.")
        raise RuntimeError("Angus search form not found on /find-an-animal.")
    action = form.get("action") or "/find-an-animal"
    action_url = action if action.startswith("http") else BASE + (action if action.startswith("/") else "/" + action)
    # Echo every input the form ships (antiforgery token, 'irs', etc.).
    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return action_url, fields


def resolve_detail(reg: str, session: requests.Session, timeout: int = 30):
    """POST the search for `reg` and return (final_url, html) after the redirect
    to the detail page."""
    action_url, fields = _get_search_form(session, timeout)
    fields[REG_FIELD] = str(reg)
    fields.setdefault("EpdPedSearchRequest.sAnimalName", "")
    resp = session.post(action_url, data=fields, timeout=timeout,
                        headers={"Referer": FIND_URL})
    resp.raise_for_status()
    if "EpdPedDtl" not in resp.url and "Reg:" not in resp.text:
        # Either not found or landed on a results list; try first result link.
        soup = BeautifulSoup(resp.text, "html.parser")
        link = soup.find("a", href=re.compile(r"EpdPedDtl", re.I))
        if not link:
            raise ValueError(f"No Angus animal found for registration {reg}")
        href = link["href"]
        url = href if href.startswith("http") else BASE + href
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    return resp.url, resp.text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _clean(s: Optional[str]) -> Optional[str]:
    return re.sub(r"\s+", " ", s).strip() if s else None


def _parse_reg(text: str, fallback: str) -> str:
    m = re.search(r"Reg:\s*AAA\s*#?\+?\s*(\d{5,9})", text)
    return m.group(1) if m else str(fallback)


def _parse_sex(text: str) -> Optional[str]:
    m = re.search(r"\b(Bull|Cow|Heifer|Steer)\b", text)
    return SEX_MAP.get(m.group(1).upper()) if m else None


def _parse_defects(text: str) -> list[dict]:
    """Genetic conditions render as e.g. [ AMF-CAF-D2F-DDF-M1F-NHF-OHF-OSF ]."""
    m = re.search(r"\[\s*([A-Z0-9\-]+)\s*\]", text)
    if not m:
        return []
    out = []
    for tok in m.group(1).split("-"):
        tok = tok.strip()
        if len(tok) >= 2 and tok[-1] in STATUS_LETTER:
            out.append({"code": tok[:-1], "status": STATUS_LETTER[tok[-1]]})
    return out


def _parse_parents(soup: BeautifulSoup, self_reg: str) -> tuple[Optional[dict], Optional[dict], list[str]]:
    """From table.pedigree collect all ancestor reg numbers (neighbors) and
    infer the direct sire/dam.

    Heuristic for sire/dam: in a standard HTML pedigree the first column holds
    two cells — sire (top) then dam (bottom). We take the reg numbers of the
    first two first-column animal cells. Confirm against a few pages; all
    ancestors are captured as neighbors regardless, so recursion is unaffected."""
    ped = soup.find("table", class_=re.compile("pedigree", re.I))
    all_regs: list[str] = []
    col0_regs: list[str] = []
    if ped:
        for tr in ped.find_all("tr"):
            cells = tr.find_all("td")
            for idx, td in enumerate(cells):
                m = re.search(r"AAA\s*#?\+?\s*(\d{5,9})", td.get_text(" ", strip=True))
                if m:
                    reg = m.group(1)
                    all_regs.append(reg)
                    if idx == 0:
                        col0_regs.append(reg)
    # de-dup preserving order
    seen = set()
    all_regs = [r for r in all_regs if not (r in seen or seen.add(r))]
    col0_regs = [r for r in col0_regs if r != self_reg]

    sire = {"association": "ANGUS", "regNumber": col0_regs[0]} if len(col0_regs) >= 1 else None
    dam = {"association": "ANGUS", "regNumber": col0_regs[1]} if len(col0_regs) >= 2 else None
    return sire, dam, [r for r in all_regs if r != self_reg]


def _parse_labeled(text: str, label: str) -> Optional[str]:
    m = re.search(rf"{label}\s*:\s*([^\n]+?)(?:\s{{2,}}|$)", text)
    return _clean(m.group(1)) if m else None


def _parse_epds(soup: BeautifulSoup) -> tuple[dict, str]:
    """Best-effort structured EPDs + the raw EPD section text (nothing lost).

    Compact cells concatenate value/acc/%/prog (e.g. '+8.8840%1090' ->
    value +8, acc .88, 40%, prog 1090). We pair trait labels with values by
    column. This is heuristic — the raw text is always returned alongside."""
    epds: dict = {}
    raw_parts = []
    for tbl in soup.find_all("table", class_=re.compile("mobile-table", re.I)):
        raw_parts.append(tbl.get_text(" | ", strip=True))
        rows = tbl.find_all("tr")
        # find header row (labels) followed by a value row
        for i in range(len(rows) - 1):
            hdr = [c.get_text(" ", strip=True) for c in rows[i].find_all(["td", "th"])]
            val = [c.get_text(" ", strip=True) for c in rows[i + 1].find_all(["td", "th"])]
            if not hdr or len(hdr) != len(val):
                continue
            for h, v in zip(hdr, val):
                trait = re.match(r"[\$A-Za-z]+", h)
                num = re.match(r"([+\-]?\d*\.?\d+)", v)
                if trait and num and any(ch.isdigit() for ch in h) is False:
                    epds[trait.group(0)] = num.group(1)
    return epds, "\n".join(raw_parts)


def parse_detail(html: str, reg_seed: str, url: str) -> tuple[dict, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    flat = re.sub(r"[ \t]+", " ", text)

    reg = _parse_reg(flat, reg_seed)

    # Name: the heading line, typically just above "Reg:".
    name = None
    m = re.search(r"(.+?)\s*\n\s*Reg:\s*AAA", text)
    if m:
        name = _clean(m.group(1).split("\n")[-1])
    if not name:
        h = soup.find(["h1", "h2"])
        name = _clean(h.get_text()) if h else None

    sire, dam, neighbor_regs = _parse_parents(soup, reg)
    epds, epd_raw = _parse_epds(soup)

    # Cross-registration: a non-Angus registry number listed in this animal's own
    # identity block (not its pedigree) means the same animal is registered
    # elsewhere -> emit it so the nodes merge. Numbers appearing in tables are
    # ancestors, so they're excluded from the animal's own cross-refs.
    table_numbers = set()
    for t in soup.find_all("table"):
        table_numbers.update(re.findall(r"\d{4,9}", t.get_text(" ")))
    id_soup = BeautifulSoup(html, "html.parser")
    for t in id_soup.find_all("table"):
        t.decompose()
    cross_refs = [r for r in registries.find_registry_refs(
                    id_soup.get_text(" "), exclude_assoc="ANGUS", exclude_reg=reg)
                  if r["regNumber"] not in table_numbers]
    foreign_ancestors = registries.find_registry_refs(
        " ".join(t.get_text(" ") for t in soup.find_all("table")),
        exclude_assoc="ANGUS", exclude_reg=reg)

    record = {
        "intl_id": None,                      # AAA reg is the key; loader dedups on registration
        "association": "ANGUS",
        "regNumber": reg,
        "name": name,
        "sex": _parse_sex(flat),
        "dob": (_parse_labeled(flat, "Birth Date") or None),
        "tattoo": (_parse_labeled(flat, "Tattoo") or None),
        "color": "Black",                     # AAA is Black Angus
        "horn_status": "Polled",              # naturally polled breed
        "breeder": _parse_labeled(flat, "Breeder"),
        "owner": _parse_labeled(flat, "First Owner"),
        "breed_composition": [{"breed": "AN", "percent": 100}],
        "defects": _parse_defects(flat),
        "dna_cases": _parse_dna(flat),
        "cross_refs": cross_refs,
        "epds": epds,
        "attributes": {
            "source": "angus.org",
            "aaa_number": reg,
            "genomic": _parse_labeled(flat, "Genomic"),
            "parentage": _parse_labeled(flat, "Parentage"),
            "owners_raw": _parse_owners(text),
            "epd_raw": epd_raw,               # full EPD panel preserved verbatim
        },
        "sire": sire,
        "dam": dam,
        "source_url": representative_url(reg),
    }
    neighbors = [{"association": "ANGUS", "regNumber": r, "relation": "pedigree"} for r in neighbor_regs]
    have = {("ANGUS", r) for r in neighbor_regs}
    for r in foreign_ancestors:
        key = (r["association"], r["regNumber"])
        if key not in have:
            have.add(key)
            neighbors.append({"association": r["association"], "regNumber": r["regNumber"],
                              "relation": "pedigree-xref"})
    return record, neighbors


def _parse_dna(text: str) -> list[str]:
    return sorted(set(re.findall(r"\b(?:AGI|NEO|GS)[- ]?\d{4,}\b", text, re.IGNORECASE)))


def _parse_owners(text: str) -> list[str]:
    m = re.search(r"Owner\(s\):\s*(.+?)(?:\n[A-Z][a-z]+ ?[A-Za-z]*:|\Z)", text, re.DOTALL)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()][:20]


# --------------------------------------------------------------------------
# Public entry point (matches digitalbeef.scrape_animal's contract)
# --------------------------------------------------------------------------

def scrape_animal(reg: str, session: requests.Session, timeout: int = 30,
                  save_html_dir: Optional[str] = None) -> tuple[dict, list[dict]]:
    url, html = resolve_detail(reg, session, timeout)
    if save_html_dir:
        _save(save_html_dir, reg, html)
    return parse_detail(html, reg, url)


def _save(directory: str, reg: str, html: str) -> None:
    import os
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"ANGUS_{reg}.html"), "w", encoding="utf-8") as f:
        f.write(html)
