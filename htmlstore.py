"""
A gzip-compressed archive of the raw pages the crawler fetched.

Why this exists: the crawler parses each page once, for the handful of fields
the graph needs, and throws the HTML away. The moment you want a field the
parser didn't take -- ownership, an identifier, an EPD you skipped -- the only
way to get it is to fetch all ~116k animals again. That is the single largest
avoidable load on the association's servers.

Stored here, a new field is a local reparse: zero requests. That is the point.
It is not a cache the crawler reads from to skip fetches (a pedigree crawl
visits each animal once and never returns, so a read cache would be all misses);
it is an archive that makes the *next* question free.

Layout:
    <root>/<ASSOC>/<shard>/<ASSOC>_<reg>_<suffix>.html.gz

`shard` is two hex characters of a hash of the registration, so 116k animals
times four pages -- close to half a million files -- spread ~1,800 to a
directory instead of piling into one that the filesystem chokes on. gzip takes
a 141 KB page to about 10 KB, so the whole Chianina run is a few GB rather than
seventy.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
from typing import Iterator, Optional

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(part: str) -> str:
    """A filesystem-safe token. Registration numbers are already tame, but a
    stray slash in a suffix must never escape the store's directory."""
    return _SAFE.sub("_", part)


class HtmlStore:
    """A place to keep, and later reread, the pages a crawl fetched."""

    def __init__(self, root: str):
        self.root = root

    def _path(self, association: str, reg: str, suffix: str) -> str:
        assoc = _safe(association.upper())
        reg_s = _safe(str(reg))
        # Shard on the registration so an animal's pages land together, and so
        # the spread does not depend on the suffix.
        shard = hashlib.sha1(reg_s.encode()).hexdigest()[:2]
        name = f"{assoc}_{reg_s}_{_safe(suffix)}.html.gz"
        return os.path.join(self.root, assoc, shard, name)

    def has(self, association: str, reg: str, suffix: str) -> bool:
        return os.path.exists(self._path(association, reg, suffix))

    def save(self, association: str, reg: str, suffix: str, html: str) -> None:
        """Write one page, compressed. Written to a temp file and renamed, so a
        crawler killed mid-write never leaves a truncated .gz that a later
        reparse would choke on."""
        path = self._path(association, reg, suffix)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                f.write(html)
            os.replace(tmp, path)
        except OSError:
            # An archive write must never take the crawl down with it: a full
            # disk should stall the store, not lose the animal in flight.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, association: str, reg: str, suffix: str) -> Optional[str]:
        """The stored page, or None if it was never saved."""
        path = self._path(association, reg, suffix)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return f.read()
        except (OSError, EOFError):
            return None

    def iter_pages(self, association: Optional[str] = None) -> Iterator[tuple[str, str, str]]:
        """Walk the store as (association, reg, suffix) for every page held --
        the entry point a reparse tool would drive off. Reconstructs the three
        parts from the filename rather than trusting the caller to know them."""
        base = self.root if association is None else os.path.join(self.root, _safe(association.upper()))
        if not os.path.isdir(base):
            return
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".html.gz"):
                    continue
                stem = fn[: -len(".html.gz")]
                assoc, _, rest = stem.partition("_")
                reg, _, suffix = rest.rpartition("_")
                if reg and suffix:
                    yield assoc, reg, suffix
