"""Builders for fake plugins and fake folder trees used across the tests."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable, Mapping


def subrecord(sub_type: bytes, payload: bytes) -> bytes:
    """A TES4 subrecord; payloads over 65535 bytes get an XXXX prefix."""
    if len(payload) > 0xFFFF:
        return b"XXXX" + struct.pack("<HI", 4, len(payload)) + sub_type + struct.pack("<H", 0) + payload
    return sub_type + struct.pack("<H", len(payload)) + payload


def make_plugin(
    masters: Iterable[str] = (),
    *,
    hedr_version: float = 0.95,
    form_version: int = 131,
    header_size: int = 24,
    flags: int = 0,
    num_records: int = 1,
    author: str | None = "tester",
    description: str | None = None,
    extra: bytes = b"",
    magic: bytes = b"TES4",
) -> bytes:
    """Bytes of a minimal plugin: TES4 record with HEDR, CNAM, SNAM, MAST/DATA pairs.

    ``header_size=20`` builds an Oblivion-style record header (no form version).
    ``extra`` is appended inside the TES4 record after the masters.
    """
    body = subrecord(b"HEDR", struct.pack("<fII", hedr_version, num_records, 0x800))
    if author is not None:
        body += subrecord(b"CNAM", author.encode("cp1252") + b"\0")
    if description is not None:
        body += subrecord(b"SNAM", description.encode("cp1252") + b"\0")
    for master in masters:
        body += subrecord(b"MAST", master.encode("cp1252") + b"\0")
        body += subrecord(b"DATA", b"\0" * 8)
    body += extra
    header = magic + struct.pack("<IIII", len(body), flags, 0, 0)
    if header_size == 24:
        header += struct.pack("<HH", form_version, 0)
    elif header_size != 20:
        raise ValueError("header_size must be 20 or 24")
    return header + body


def make_tree(base: Path, files: Iterable[str] | Mapping[str, bytes | str]) -> Path:
    """Create files under *base*. A mapping gives contents; an iterable makes
    small placeholder files. Paths ending in "/" create empty directories."""
    items = files.items() if isinstance(files, Mapping) else ((f, None) for f in files)
    for rel, content in items:
        path = base / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = f"placeholder for {rel}\n"
        if isinstance(content, str):
            content = content.encode()
        path.write_bytes(content)
    return base
