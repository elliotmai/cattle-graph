"""
Cross-registry helpers.

Beef animals are frequently referenced across associations by a prefixed
registration number, e.g. an Angus bull shows up in a Maine-Anjou pedigree as
"AAA #13054003". This module maps those registry prefixes to the association
codes used in the graph, so both parsers can emit the SAME (association,
regNumber) pair for the same animal — which is exactly what the loader keys on
to merge them into one node.

Extend PREFIX_TO_ASSOC as you encounter more prefixes. Where a prefix is
ambiguous across breeds it's noted; adjust to your data.
"""

from __future__ import annotations

import re

# Registry prefix (as printed on the pages) -> association code in the graph.
PREFIX_TO_ASSOC = {
    "AAA": "ANGUS",     # American Angus Association
    "RAAA": "RED",      # Red Angus Association of America
    "AMAA": "MAINE",    # American Maine-Anjou Association
    "ACA": "CHIA",      # American Chianina Association
    "ASA": "SIMM",      # American Simmental Association (NOTE: some pages use ASA
                        # for American Shorthorn — verify against your data)
    "AHA": "HERF",      # American Hereford Association
    "AGA": "GELB",      # American Gelbvieh Association
    "NALF": "LIMI",     # North American Limousin Foundation
    "ASN": "SALERS",    # American Salers
}

# Reverse: association code -> its own prefix(es), used to exclude self-references.
ASSOC_TO_PREFIXES: dict[str, set[str]] = {}
for _pfx, _assoc in PREFIX_TO_ASSOC.items():
    ASSOC_TO_PREFIXES.setdefault(_assoc, set()).add(_pfx)

# Matches "AAA #13054003", "AAA 13054003", "AAA #+13054003", "RAAA 1234567", etc.
_REF_RE = re.compile(r"\b([A-Z]{2,5})\s*#?\+?\s*(\d{4,9})\b")


def map_prefix(prefix: str) -> str | None:
    return PREFIX_TO_ASSOC.get(prefix.upper())


def find_registry_refs(text: str, exclude_assoc: str | None = None,
                       exclude_reg: str | None = None) -> list[dict]:
    """Return [{association, regNumber}] for every recognized foreign-registry
    reference in `text`.

    - Only prefixes present in PREFIX_TO_ASSOC are returned (unknown prefixes are
      ignored, so stray uppercase tokens don't create noise).
    - References to `exclude_assoc` (the current animal's own association) are
      dropped, as is `exclude_reg` (the animal's own number).
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    exclude_prefixes = ASSOC_TO_PREFIXES.get(exclude_assoc or "", set())
    for prefix, digits in _REF_RE.findall(text):
        assoc = PREFIX_TO_ASSOC.get(prefix.upper())
        if not assoc:
            continue
        if prefix.upper() in exclude_prefixes or assoc == exclude_assoc:
            continue
        if exclude_reg and digits == str(exclude_reg):
            continue
        key = (assoc, digits)
        if key not in seen:
            seen.add(key)
            out.append({"association": assoc, "regNumber": digits})
    return out
