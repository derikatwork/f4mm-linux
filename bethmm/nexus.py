"""Parse Nexus Mods "Manual Download" file names.

Nexus names downloads ``<Name>-<ModID>-<Version>-<Timestamp>.<ext>`` where the
version has its dots replaced by hyphens and the timestamp is a 10-digit Unix
time. Older uploads omit the timestamp. Examples::

    "Unofficial Fallout 4 Patch-4598-2-1-5-1680000000.zip"
        -> name "Unofficial Fallout 4 Patch", mod_id 4598, version "2.1.5",
           timestamp 1680000000
    "SkyUI_5_2_SE-12604-5-2SE.7z"
        -> name "SkyUI 5 2 SE", mod_id 12604, version "5.2SE", timestamp None
    "Mod Name - Main File-1234-1-0a-1600000000 (1).7z"
        -> browser duplicate suffix " (1)" is ignored; version "1.0a"
    "my_random_archive.zip"
        -> name "my random archive", everything else None
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NexusInfo:
    #: Human-friendly name (underscores turned into spaces when the name has
    #: no spaces of its own; trimmed).
    display_name: str
    mod_id: int | None
    #: Dotted version, e.g. "2.1.5"; ``None`` when the name didn't match.
    version: str | None
    timestamp: int | None
    #: File name without extension or duplicate suffix, as given.
    stem: str

    @property
    def is_nexus(self) -> bool:
        return self.mod_id is not None


ARCHIVE_EXTS = (".7z", ".zip", ".rar")

_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)$")
_TIMESTAMP = re.compile(r"^\d{10}$")
_VERSION_TOKEN = re.compile(r"^[0-9A-Za-z_.+]+$")


def parse_nexus_filename(filename: str) -> NexusInfo:
    """Parse *filename* (a bare name or a path; only the last component counts).

    Never raises for ordinary strings: unrecognised names give a NexusInfo with
    only ``display_name`` and ``stem`` filled in.
    """
    name = filename.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    lower = name.lower()
    for ext in ARCHIVE_EXTS:
        if lower.endswith(ext) and len(name) > len(ext):
            name = name[: -len(ext)]
            break
    stem = _DUPLICATE_SUFFIX.sub("", name).strip()

    tokens = stem.split("-")
    timestamp: int | None = None
    if len(tokens) >= 3 and _TIMESTAMP.match(tokens[-1]):
        timestamp = int(tokens[-1])
        tokens = tokens[:-1]

    mod_index = _find_mod_id(tokens)
    if mod_index is None:
        return NexusInfo(_display(stem), None, None, None, stem)

    version_tokens = tokens[mod_index + 1:]
    return NexusInfo(
        display_name=_display("-".join(tokens[:mod_index])) or _display(stem),
        mod_id=int(tokens[mod_index]),
        version=".".join(version_tokens) if version_tokens else None,
        timestamp=timestamp,
        stem=stem,
    )


def _find_mod_id(tokens: list[str]) -> int | None:
    """Index of the token holding the mod ID, or None.

    The mod ID is an all-digit token after a non-empty name whose remaining
    tokens look like a version. Multi-digit IDs are preferred so that a name
    such as "Project-2-Remix" doesn't swallow the real ID.
    """
    candidates = []
    for i in range(1, len(tokens)):
        if not tokens[i].isdigit() or not "-".join(tokens[:i]).strip():
            continue
        rest = tokens[i + 1:]
        if rest and not all(_VERSION_TOKEN.match(t) for t in rest):
            continue
        if rest and not any(ch.isdigit() for t in rest for ch in t):
            continue
        candidates.append(i)
    if not candidates:
        return None
    multi = [i for i in candidates if len(tokens[i]) >= 2]
    return (multi or candidates)[0]


def _display(name: str) -> str:
    name = name.strip()
    if " " not in name:
        name = name.replace("_", " ")
    return " ".join(name.split())
