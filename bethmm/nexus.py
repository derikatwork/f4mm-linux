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
    "Project-22-Remix-1234-1-0.7z"
        -> name "Project-22-Remix", mod_id 1234, version "1.0"
    "my_random_archive.zip"
        -> name "my random archive", everything else None

A name only counts as a Nexus download when an all-digit mod ID follows a
non-empty name and is itself followed by a version or a timestamp, so
"Textures-2.zip" stays a plain name. Without a timestamp the version must
contain a digit ("Patch-2-Final.zip" is a plain name, "Mod-1234-Final-<ts>"
is not).
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
#: Mod IDs are well below a billion; longer digit runs are never IDs (and
#: int() refuses runs over 4300 digits).
_NUMBER = re.compile(r"^[0-9]{1,9}$")
_TIMESTAMP = re.compile(r"^[0-9]{10}$")
_VERSION_TOKEN = re.compile(r"^[0-9A-Za-z_.+]+$")
#: Version tokens that start like a version number ("1", "0a", "2SE", "v1").
_STRICT_VERSION_TOKEN = re.compile(r"^[vV]?[0-9]")


def parse_nexus_filename(filename: str) -> NexusInfo:
    """Parse *filename* (a bare name or a path; only the last component counts).

    Never raises for ordinary strings: unrecognised names give a NexusInfo with
    only ``display_name`` and ``stem`` filled in.
    """
    name = filename.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
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

    mod_index = _find_mod_id(tokens, timestamp is not None)
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


def _find_mod_id(tokens: list[str], has_timestamp: bool) -> int | None:
    """Index of the token holding the mod ID, or None.

    The mod ID is an all-digit token after a non-empty name, followed by at
    least one version-looking token (or nothing, when a timestamp was found).
    Without a timestamp the version must contain a digit. Among the
    candidates, multi-digit IDs win over single digits (versions are mostly
    single digits, IDs almost never), then those whose version tokens all
    start with a digit (or "v" + digit) over looser ones, then the leftmost:
    "Project-22-Remix-1234-1-0" -> ID 1234, "Mod-1234-beta-2-<ts>" -> ID 1234,
    version beta.2, and "Mod-1234-10-2" -> ID 1234, version 10.2.
    """
    if not tokens or not tokens[0].strip():
        return None
    best: int | None = None
    best_rank: tuple[bool, bool] | None = None
    for i in range(1, len(tokens)):
        if not _NUMBER.match(tokens[i]):
            continue
        rest = tokens[i + 1:]
        if not rest and not has_timestamp:
            continue
        if not all(_VERSION_TOKEN.match(t) for t in rest):
            continue
        if rest and not has_timestamp and not any(ch.isdigit() for t in rest for ch in t):
            continue
        strict = all(_STRICT_VERSION_TOKEN.match(t) for t in rest)
        rank = (len(tokens[i]) < 2, not strict)
        if best_rank is None or rank < best_rank:
            best, best_rank = i, rank
    return best


def _display(name: str) -> str:
    name = name.strip()
    if " " not in name:
        name = name.replace("_", " ")
    return " ".join(name.split())
