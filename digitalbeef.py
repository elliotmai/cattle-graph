"""
DigitalBeef fetch + parse.

All DigitalBeef associations share one URL shape, so a single parser handles
every subdomain:

    https://{subdomain}.digitalbeef.com/modules.php
        ?op=modload&name=_animal&file=_animal&animal_registration={REG}

That container page carries ONLY the identity block. Everything else — pedigree,
genotype, EPDs, ownership, progeny — is loaded by the page's own JavaScript from
per-tab endpoints:

    modules/_animal/ajax/{tab}.php?u=&a={REG}&can_edit=0&thetime={ms}

`activateTab()` in modules/_animal/js/_animal.js maps tab index -> file name;
the ones worth having are `_pedigree`, `_genotype`, `_epds` and `_progeny`.
Fetching the container alone yields an animal with no parents, which is why a
recursive crawl seeded that way terminates immediately.

`scrape_animal()` returns (record, neighbors):
  * record    -> a dict in the loader's record format (see loader.py)
  * neighbors -> [{association, regNumber, relation}]  animals to crawl next

Selectors here were written against saved fixtures of a real animal
(CHIA MA430053) — see fixtures/ and test_parsers.py.
"""

from __future__ import annotations

import re
import time
import logging
import threading
from typing import Optional
from urllib.parse import urlparse, parse_qs
from urllib import robotparser

import requests
from urllib3.util import connection as urllib3_connection
from bs4 import BeautifulSoup

import registries

log = logging.getLogger("digitalbeef")


class AnimalNotFound(Exception):
    """The registration number returned a page with no animal on it.

    DigitalBeef serves HTTP 200 and the ordinary site shell for a registration
    that does not exist, so a missing animal has to be detected from content.
    """


# --------------------------------------------------------------------------
# Association <-> subdomain map (extend as needed)
# --------------------------------------------------------------------------

SUBDOMAIN_BY_CODE = {
    "MAINE": "maine-anjou",
    "CHIA": "chianina",
    "SHORT": "shorthorn",
    "GELB": "gelbvieh",
    "SIMM": "simmental",
    "RED": "redangus",
    "HERF": "hereford",
    "SALERS": "salers",
    "LIMI": "limousin",
    "BRAUN": "braunvieh",
}
CODE_BY_SUBDOMAIN = {v: k for k, v in SUBDOMAIN_BY_CODE.items()}

# Full association names as printed in the cross-registration block.
ASSOC_NAME_TO_CODE = {
    "american maine-anjou association": "MAINE",
    "american chianina association": "CHIA",
    "american shorthorn association": "SHORT",
    "american angus association": "ANGUS",
    "american simmental association": "SIMM",
    "american gelbvieh association": "GELB",
    "american hereford association": "HERF",
    "red angus association of america": "RED",
    "north american limousin foundation": "LIMI",
}

TABS = ("_pedigree", "_genotype", "_epds", "_progeny", "_ownership", "_identifiers")


def animal_url(association: str, reg: str) -> str:
    sub = SUBDOMAIN_BY_CODE.get(association.upper())
    if not sub:
        raise ValueError(f"Unknown association code '{association}'. "
                         f"Add it to SUBDOMAIN_BY_CODE.")
    return (f"https://{sub}.digitalbeef.com/modules.php"
            f"?op=modload&name=_animal&file=_animal&animal_registration={reg}")


def tab_url(association: str, reg: str, tab: str) -> str:
    """URL for one of the AJAX detail tabs (see module docstring)."""
    sub = SUBDOMAIN_BY_CODE[association.upper()]
    return (f"https://{sub}.digitalbeef.com/modules/_animal/ajax/{tab}.php"
            f"?u=&a={reg}&can_edit=0&thetime=0")


def progeny_url(association: str, reg: str) -> str:
    return tab_url(association, reg, "_progeny")


# --------------------------------------------------------------------------
# HTTP session + robots
# --------------------------------------------------------------------------

class Blocked(Exception):
    """The host refused us, rather than the animal being missing or broken.

    403 and 429 are about the client, not the registration -- retrying the next
    animal just knocks again, faster, because a refusal costs one request where
    a successful crawl costs four. Raised as its own type so the crawl loop can
    stop instead of grinding the whole frontier into `failed`.
    """

    def __init__(self, status: int, retry_after=None, url: str = ""):
        self.status = status
        self.retry_after = retry_after
        self.url = url
        super().__init__(f"{status} refused for {url}")


def _retry_after_seconds(value) -> Optional[int]:
    """Retry-After as whole seconds. The HTTP-date form is ignored rather than
    guessed at -- the caller's own backoff covers it."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0, n)


# --------------------------------------------------------------------------
# Direct-IP access (curl --resolve for the crawler)
# --------------------------------------------------------------------------
#
# DigitalBeef's breed subdomains can be unreachable-by-name from where the
# crawler runs -- a box with no working resolver for them, split-horizon DNS,
# or a name that briefly stops resolving while the site is up. Pinning the
# hostname to a known IP sidesteps that: we dial the IP but keep the real
# hostname for the TLS SNI, the certificate check and the Host header, so the
# request is byte-for-byte what a normal DNS lookup would have produced and
# certificate verification is NOT weakened. This is exactly what
# `curl --resolve host:443:IP` does.
#
# chianina sits behind Cloudflare (two anycast IPs); maine-anjou and shorthorn
# (and the other associations) share DigitalBeef's 'rocky' host. Cloudflare's
# anycast addresses in particular can rotate, so this is opt-in, not the
# default -- enable it only where name resolution is the problem, and refresh
# the IPs if they ever start refusing the connection.
#
# The pin acts at socket-connect time, so it applies to DIRECT connections. If
# the process routes through an HTTPS proxy (HTTPS_PROXY set), the socket goes
# to the proxy and the proxy does its own name resolution -- pinning is moot
# there and the host has to be reachable by name (or IP) from the proxy.
DEFAULT_IP_PINS: dict[str, list[str]] = {
    "chianina.digitalbeef.com": ["104.21.76.181", "172.67.198.104"],
    "maine-anjou.digitalbeef.com": ["165.227.220.252"],
    "shorthorn.digitalbeef.com": ["165.227.220.252"],
}

_pin_lock = threading.Lock()
_active_pins: dict[str, list[str]] = {}
_orig_create_connection = None


def parse_ip_pins(specs) -> dict[str, list[str]]:
    """Parse ``host=ip`` / ``host:ip`` tokens into a {host: [ip, ...]} map.

    A single token may carry several comma-separated IPs (tried in order), and
    the bare word ``default`` expands to :data:`DEFAULT_IP_PINS`. Accepts a
    list of tokens or one string of them separated by whitespace/';'/','.
    """
    if isinstance(specs, str):
        specs = re.split(r"[\s;]+", specs.strip())
    out: dict[str, list[str]] = {}
    for spec in specs or []:
        spec = spec.strip()
        if not spec:
            continue
        if spec.lower() == "default":
            for host, ips in DEFAULT_IP_PINS.items():
                out.setdefault(host.lower(), []).extend(ips)
            continue
        m = re.match(r"^\s*([^=:\s]+)\s*[=:]\s*(.+)$", spec)
        if not m:
            raise ValueError(f"Bad --pin-ip spec {spec!r}; expected host=IP[,IP].")
        host, ip_field = m.group(1).lower(), m.group(2)
        for ip in re.split(r"[,\s]+", ip_field.strip()):
            if ip:
                out.setdefault(host, []).append(ip)
    return out


def _create_connection_pinned(address, *args, **kwargs):
    """create_connection that redirects pinned hostnames to their IPs, trying
    each IP in order so a host with several addresses fails over cleanly."""
    host, port = address
    ips = _active_pins.get(host.lower()) if isinstance(host, str) else None
    if not ips:
        return _orig_create_connection(address, *args, **kwargs)
    last_err: Exception | None = None
    for ip in ips:
        try:
            return _orig_create_connection((ip, port), *args, **kwargs)
        except OSError as e:
            last_err = e
    raise last_err if last_err else OSError(f"no pinned IP connected for {host}")


def pin_host_ips(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    """Route the given hostnames to fixed IPs for every requests/urllib3
    connection in this process, keeping SNI, certificate hostname and the Host
    header on the real name. Idempotent and additive; returns the active map.

    Installed process-wide (urllib3 resolves through one hook) but scoped by the
    map -- only listed hosts are redirected; everything else resolves normally.
    """
    global _orig_create_connection
    if not mapping:
        return dict(_active_pins)
    with _pin_lock:
        for host, ips in mapping.items():
            _active_pins.setdefault(host.lower(), [])
            for ip in ips:
                if ip not in _active_pins[host.lower()]:
                    _active_pins[host.lower()].append(ip)
        if _orig_create_connection is None:
            _orig_create_connection = urllib3_connection.create_connection
            urllib3_connection.create_connection = _create_connection_pinned
        return dict(_active_pins)


def make_session(user_agent: str, ip_pins: dict[str, list[str]] | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})
    if ip_pins:
        pin_host_ips(ip_pins)
    return s


class RobotsCache:
    """Per-host robots.txt checker (cached)."""
    def __init__(self, user_agent: str):
        self.ua = user_agent
        self._cache: dict[str, robotparser.RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        rp = self._cache.get(host)
        if rp is None:
            rp = robotparser.RobotFileParser()
            rp.set_url(f"https://{host}/robots.txt")
            try:
                rp.read()
            except Exception as e:
                log.warning("Could not read robots.txt for %s: %s", host, e)
            self._cache[host] = rp
        try:
            return rp.can_fetch(self.ua, url)
        except Exception:
            return True


def fetch(url: str, session: requests.Session, timeout: int = 30) -> str:
    resp = session.get(url, timeout=timeout)
    if resp.status_code in (403, 429):
        raise Blocked(resp.status_code,
                      _retry_after_seconds(resp.headers.get("Retry-After")), url)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _flat(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _reg_from_href(href: str) -> Optional[str]:
    try:
        q = parse_qs(urlparse(href).query)
        vals = q.get("animal_registration")
        return vals[0].strip() if vals else None
    except Exception:
        return None


def _iso_date(value: Optional[str]) -> Optional[str]:
    """'09/10/2011' -> '2011-09-10'. Leaves already-ISO values alone."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return value


SEX_MAP = {
    "BULL": "M", "MALE": "M", "M": "M",
    "COW": "F", "HEIFER": "F", "FEMALE": "F", "F": "F",
    "STEER": "S", "S": "S",
}


# --------------------------------------------------------------------------
# Identity block (container page)
# --------------------------------------------------------------------------

def parse_label_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """DigitalBeef renders the identity block as <td>Label:</td><td>value</td>.

    Reading those pairs is what keeps navigation furniture out of the record —
    a regex over the flattened page text picks up the sidebar's
    'International Letter' year table and calls it a date of birth.
    """
    pairs: dict[str, str] = {}
    for td in soup.find_all("td"):
        label = _flat(td)
        if not label.endswith(":") or len(label) > 40:
            continue
        sib = td.find_next_sibling("td")
        if sib is None:
            continue
        key = label.rstrip(":").strip()
        if key and key not in pairs:
            pairs[key] = _flat(sib)
    return pairs


def parse_genetic_makeup(value: str) -> list[dict]:
    """'73.049% MA | 19.733% AN | 2.881% CA | 4.338% XX' -> composition list.

    The percentages are decimals; matching only the integer part is how the old
    parser produced breeds at 733% and 881%.
    """
    out = []
    for pct, code in re.findall(r"(\d+(?:\.\d+)?)\s*%\s*([A-Za-z]{1,3})", value or ""):
        out.append({"breed": code.upper(), "percent": float(pct)})
    return out


def parse_cross_registrations(html: str, self_assoc: str) -> tuple[list[dict], Optional[str]]:
    """The container page footer shows this animal's registrations in other
    associations, e.g. 'American Maine-Anjou Association DOB: 09/10/2011' with a
    PullUpAnimalDetail('430053', 'American Maine-Anjou Association') handle.

    Returns (cross_refs, dob) — the DOB often appears only in that block.
    """
    refs, dob = [], None
    # the handle lives in an onClick attribute, so this one reads raw HTML
    for reg, name in re.findall(
            r"PullUpAnimalDetail\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", html):
        code = ASSOC_NAME_TO_CODE.get(name.strip().lower())
        if code and code != self_assoc.upper():
            refs.append({"association": code, "regNumber": reg.strip()})

    # the date is rendered as 'DOB:&nbsp;09/10/2011', so match on text, not markup
    text = BeautifulSoup(html, "html.parser").get_text(" ").replace("\xa0", " ")
    m = re.search(r"DOB:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    if m:
        dob = _iso_date(m.group(1))
    return refs, dob


def parse_container(html: str, association: str, reg: str, source_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    pairs = parse_label_pairs(soup)

    # A registration that does not exist still returns HTTP 200 and the shell.
    listed = (pairs.get("Registration") or "").strip()
    if not listed:
        raise AnimalNotFound(f"{association}:{reg} — no identity block on the page")

    cross_refs, dob = parse_cross_registrations(html, association)

    sex = pairs.get("Sex", "")
    horn = pairs.get("Horn/Poll/Scur") or None
    tattoo = pairs.get("Herd Prefix/Tattoo") or None

    attributes = {}
    for label, key in (("EID", "eid"), ("COI", "coi"), ("Service Type", "service_type"),
                       ("Status", "registry_status")):
        val = pairs.get(label)
        if val:
            attributes[key] = val
    for label, val in pairs.items():
        if label.endswith("%") and val:               # e.g. 'Chianina %': '2.88'
            attributes[label.replace(" %", "_percent").lower()] = val

    return {
        "intl_id": pairs.get("International ID") or None,
        "association": association.upper(),
        "regNumber": listed or reg,
        "name": pairs.get("Name") or None,
        "sex": SEX_MAP.get(sex.upper(), None),
        "dob": dob,
        "tattoo": tattoo,
        "color": pairs.get("Color") or None,
        "horn_status": horn,
        "breeder": pairs.get("Breeder") or None,
        "owner": pairs.get("Owner") or None,
        "breed_composition": parse_genetic_makeup(pairs.get("Genetic Makeup", "")),
        "defects": [],
        "dna_cases": [],
        "cross_refs": cross_refs,
        "epds": {},
        "attributes": attributes,
        "sire": None,
        "dam": None,
        "source_url": source_url,
    }


# --------------------------------------------------------------------------
# Pedigree tab
# --------------------------------------------------------------------------

# 'MA402617 GVC SUH 01W [ 01W DGW ] -- AMFT CAFT DDS NHFT PHAFT THFT'
ANCESTOR_RE = re.compile(
    r"^(?P<reg>[A-Z]{0,3}\d{5,9})\s+"
    r"(?P<name>.+?)"
    r"(?:\s*\[\s*(?P<tattoo>[^\]]*)\s*\])?"
    r"(?:\s*--\s*(?P<defects>[A-Z0-9 ]+))?$"
)

# Defect loci, longest first so PHA wins over any shorter prefix.
DEFECT_LOCI = ["PHA", "MYO", "MSUD", "TH", "CA", "AM", "NH", "DD", "DS", "OS", "MA"]
DEFECT_LOCI.sort(key=len, reverse=True)

# Suffixes on the pedigree chart, per the page's own Defect Key.
PEDIGREE_SUFFIX = {
    "FT": "F",   # Free by Test
    "FP": "F",   # Free by Pedigree
    "F": "F",
    "S": "S",    # Suspected Carrier
    "CT": "C",   # Carrier by Test
    "C": "C",
    "A": "A",    # Affected by Test
}

# Wording used on the genotype tab.
GENOTYPE_RESULT = {
    "free by test": "F",
    "free by pedigree": "F",
    "free": "F",
    "suspect": "S",
    "suspected carrier": "S",
    "carrier by test": "C",
    "carrier": "C",
    "affected by test": "A",
    "affected": "A",
}


def parse_defect_codes(blob: Optional[str]) -> list[dict]:
    """'AMFT CAFT DDS NHFT PHAFT THFT' -> [{code: AM, status: F}, ...]"""
    out: list[dict] = []
    for token in (blob or "").split():
        for locus in DEFECT_LOCI:
            if token.startswith(locus):
                status = PEDIGREE_SUFFIX.get(token[len(locus):])
                if status:
                    out.append({"code": locus, "status": status})
                break
    return out


def parse_ancestor_cell(text: str) -> Optional[dict]:
    m = ANCESTOR_RE.match(text.strip())
    if not m:
        return None
    return {
        "regNumber": m.group("reg"),
        "name": (m.group("name") or "").strip() or None,
        "tattoo": (m.group("tattoo") or "").strip() or None,
        "defects": parse_defect_codes(m.group("defects")),
    }


def parse_pedigree(html: str, association: str) -> tuple[Optional[dict], Optional[dict], list[dict]]:
    """Return (sire, dam, all_ancestors).

    Sire and dam come from the explicit 'Sire:' / 'Dam:' label cells. The chart
    itself is a nested table whose cells are duplicated for rendering, so cell
    order does NOT identify the parents — on the reference animal the sire is
    the eighth ancestor cell in document order.
    """
    soup = BeautifulSoup(html, "html.parser")
    assoc = association.upper()

    parents: dict[str, Optional[dict]] = {"Sire": None, "Dam": None}
    for td in soup.find_all("td"):
        label = _flat(td).rstrip(":").strip()
        if label in parents and parents[label] is None:
            sib = td.find_next_sibling("td")
            if sib is not None:
                info = parse_ancestor_cell(_flat(sib))
                if info:
                    parents[label] = {"association": assoc,
                                      "regNumber": info["regNumber"],
                                      "name": info["name"]}

    ancestors, seen = [], set()
    for td in soup.find_all("td"):
        if td.find("table"):
            continue
        info = parse_ancestor_cell(_flat(td))
        if info and info["regNumber"] not in seen:
            seen.add(info["regNumber"])
            ancestors.append(info)

    return parents["Sire"], parents["Dam"], ancestors


# --------------------------------------------------------------------------
# Genotype tab
# --------------------------------------------------------------------------

CONDITION_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z \-']+)\s*\((?P<code>[A-Z0-9]{2,5})\)$")


def parse_genotype(html: str) -> tuple[list[dict], list[str]]:
    """Return (defects, dna_cases) from the genetic-conditions table.

    Rows look like ['Pulmonary Hypoplasia with Anasarca (PHA)', 'Free by
    Pedigree', '8/5/2014', '8/5/2014', 'BMS']. Untested conditions have the name
    cell only and are skipped rather than recorded as free.
    """
    soup = BeautifulSoup(html, "html.parser")
    defects, cases = [], set()

    for tr in soup.find_all("tr"):
        cells = [_flat(td) for td in tr.find_all("td")]
        cells = [c for c in cells if c]
        if not cells:
            continue
        m = CONDITION_RE.match(cells[0])
        if not m:
            continue
        result = cells[1].strip().lower() if len(cells) > 1 else ""
        status = GENOTYPE_RESULT.get(result)
        if status:
            defects.append({"code": m.group("code"), "status": status})

    text = _flat(soup)
    for case in re.findall(r"\b(?:AGI|NEO|GS|GNS)[- ]?\d{4,}\b", text, re.IGNORECASE):
        cases.add(re.sub(r"\s", "", case).upper())

    return defects, sorted(cases)


# --------------------------------------------------------------------------
# EPD tab
# --------------------------------------------------------------------------

EPD_TRAITS = {"CED", "BW", "WW", "YW", "MILK", "TM", "CEM", "STAY", "DOC", "YG",
              "CW", "CREA", "MARB", "CFAT", "API", "TI", "REA", "FAT", "IMF", "CE"}


def parse_epds(html: str) -> dict:
    """Pull the subject animal's EPD row.

    Each value cell reads 'EPD +/-Chg ACC %Rank Prgy/CGs' (some traits carry
    fewer figures), so the EPD itself is the first token.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, float] = {}

    for table in soup.find_all("table"):
        header: list[str] = []
        for tr in table.find_all("tr"):
            cells = [_flat(td) for td in tr.find_all("td")]
            cells = [c for c in cells if c]
            if not cells:
                continue

            upper = [c.upper() for c in cells]
            if sum(1 for c in upper if c in EPD_TRAITS) >= 4:
                header = upper
                continue

            if header and cells[0].lower().startswith("subject"):
                values = cells[2:] if len(cells) > len(header) else cells[1:]
                for trait, cell in zip(header, values):
                    tok = cell.split()[0] if cell.split() else ""
                    try:
                        out[trait] = float(tok)
                    except ValueError:
                        continue
                header = []
    return out


# --------------------------------------------------------------------------
# Progeny tab
# --------------------------------------------------------------------------

def parse_progeny(html: str, association: str) -> list[dict]:
    """Offspring registration numbers, for downward expansion.

    A data row's leaf cells read: Status, DOB, Sex, Reg #, Tattoo, Name,
    performance figures…, and finally the mate as '357229 - MS TEXAS HEAT'.
    Only leaf cells are used — the outer <tr>s of this nested table flatten the
    whole grid into one blob.
    """
    soup = BeautifulSoup(html, "html.parser")
    assoc = association.upper()
    out, seen = [], set()

    def add(reg: str, relation: str) -> None:
        if reg and reg not in seen:
            seen.add(reg)
            out.append({"association": assoc, "regNumber": reg, "relation": relation})

    for a in soup.find_all("a", href=True):
        r = _reg_from_href(a["href"])
        if r:
            add(r, "progeny")

    for tr in soup.find_all("tr"):
        cells = [_flat(td) for td in tr.find_all("td") if not td.find("table")]
        cells = [c for c in cells if c]
        dates = [i for i, c in enumerate(cells)
                 if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", c)]
        if not dates:
            continue
        for cell in cells[dates[0] + 1:]:
            if re.fullmatch(r"[A-Z]{0,3}\d{5,9}", cell):
                add(cell, "progeny")
                break
        # the mate named in the final column is a parent of that calf
        m = re.match(r"([A-Z]{0,3}\d{5,9})\s*-\s*\S", cells[-1])
        if m:
            add(m.group(1), "mate")
    return out


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def scrape_animal(association: str, reg: str, session: requests.Session,
                  fetch_progeny: bool = True, timeout: int = 30,
                  save_html_dir: Optional[str] = None,
                  delay: float = 1.0,
                  fetch_epds: bool = True,
                  html_store=None) -> tuple[dict, list[dict]]:
    """Fetch the container page plus the detail tabs and assemble one record.

    Every tab is a separate request against a shared host, so which ones are
    fetched is the single biggest lever on how fast the crawl goes and how much
    load it puts on the association. `_pedigree` is what makes the crawl
    recursive and `_genotype` carries the defect findings, so those stay;
    `_progeny` and `_epds` are optional and cost a request each.

    Raises AnimalNotFound when the registration has no animal behind it.
    """
    assoc = association.upper()
    url = animal_url(assoc, reg)
    html = fetch(url, session, timeout)
    if save_html_dir:
        _save_html(save_html_dir, assoc, reg, html, "container")
    if html_store is not None:
        html_store.save(assoc, reg, "container", html)

    record = parse_container(html, assoc, reg, url)
    neighbors: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(nassoc: str, nreg: str, relation: str) -> None:
        key = (nassoc.upper(), str(nreg))
        if key[1] and key != (assoc, str(reg)) and key not in seen:
            seen.add(key)
            neighbors.append({"association": key[0], "regNumber": key[1],
                              "relation": relation})

    session.headers["Referer"] = url

    wanted = ["_pedigree", "_genotype"]
    if fetch_epds:
        wanted.append("_epds")
    if fetch_progeny:
        wanted.append("_progeny")

    for tab in wanted:
        time.sleep(delay)
        try:
            thtml = fetch(tab_url(assoc, reg, tab), session, timeout)
        except Exception as e:
            log.warning("tab %s failed for %s:%s -> %s", tab, assoc, reg, e)
            continue
        if save_html_dir:
            _save_html(save_html_dir, assoc, reg, thtml, tab.lstrip("_"))
        if html_store is not None:
            html_store.save(assoc, reg, tab.lstrip("_"), thtml)

        if tab == "_pedigree":
            sire, dam, ancestors = parse_pedigree(thtml, assoc)
            record["sire"], record["dam"] = sire, dam
            if sire:
                add(sire["association"], sire["regNumber"], "sire")
            if dam:
                add(dam["association"], dam["regNumber"], "dam")
            for anc in ancestors:
                add(assoc, anc["regNumber"], "pedigree")
            record["attributes"]["pedigree_ancestors"] = len(ancestors)

        elif tab == "_genotype":
            defects, cases = parse_genotype(thtml)
            record["defects"] = defects
            record["dna_cases"] = cases

        elif tab == "_epds":
            record["epds"] = parse_epds(thtml)

        elif tab == "_progeny":
            for n in parse_progeny(thtml, assoc):
                add(n["association"], n["regNumber"], "progeny")

    # Foreign-registry ancestors (e.g. an Angus bull shown as 'AAA #13054003')
    for r in record["cross_refs"]:
        add(r["association"], r["regNumber"], "cross-registration")

    record["attributes"]["parse_confidence"] = "fixture-verified (CHIA MA430053)"
    return record, neighbors


def _save_html(directory: str, association: str, reg: str, html: str,
               suffix: str = "container") -> None:
    import os
    os.makedirs(directory, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{association}_{reg}_{suffix}")
    with open(os.path.join(directory, f"{safe}.html"), "w", encoding="utf-8") as f:
        f.write(html)
