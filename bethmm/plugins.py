"""plugins.txt: which plugins a game loads (and, for most games, in which order).

The file lives in the game's Wine prefix
(``drive_c/users/<user>/AppData/Local/<game folder>/plugins.txt``) and comes
in two formats (``games.PluginsTxtFormat``):

* **asterisk** (Fallout 4, Skyrim SE, Starfield): every plugin the game knows,
  in load order; ``*Name.esp`` is active, ``Name.esp`` is not. The game's own
  masters and DLCs are loaded anyway and aren't listed;
* **plain** (Fallout: New Vegas, Fallout 3, Oblivion, Skyrim LE): one active
  plugin per line, the game's own masters included; the load order comes from
  the plugin files' modification times.

Lines that aren't plugins (``#`` comments, blank lines) are kept where they
are, and so are the file's encoding (Windows-1252 or UTF-8) and line endings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bethmm.games import GameDef, PluginsTxtFormat
from bethmm.plugin_header import PLUGIN_EXTS


def is_plugin_name(name: str) -> bool:
    return "/" not in name and "\\" not in name and name.lower().endswith(PLUGIN_EXTS)


def key(name: str) -> str:
    """The case-insensitive key of a plugin name."""
    return name.strip().lower()


@dataclass
class Entry:
    name: str
    active: bool = True


@dataclass
class PluginsFile:
    """plugins.txt as read: its plugin entries in order, and the other lines
    (kept as text) where they were."""

    path: Path | None
    format: PluginsTxtFormat
    lines: list[Entry | str] = field(default_factory=list)
    encoding: str = "cp1252"
    newline: str = "\r\n"
    #: False: there was no file (nothing written yet).
    exists: bool = False

    @property
    def entries(self) -> list[Entry]:
        return [line for line in self.lines if isinstance(line, Entry)]

    def find(self, name: str) -> Entry | None:
        wanted = key(name)
        return next((e for e in self.entries if key(e.name) == wanted), None)

    def index(self, name: str) -> int | None:
        """Position of *name* among the plugin entries."""
        wanted = key(name)
        return next((i for i, e in enumerate(self.entries) if key(e.name) == wanted), None)

    def active(self) -> list[str]:
        return [e.name for e in self.entries if e.active]


def parse(data: bytes, fmt: PluginsTxtFormat, path: Path | None = None) -> PluginsFile:
    try:
        text = data.decode("utf-8")
        encoding = "utf-8" if any(ord(c) > 127 for c in text) else "cp1252"
    except UnicodeDecodeError:
        text, encoding = data.decode("cp1252", errors="replace"), "cp1252"
    text = text.lstrip("﻿")
    newline = "\r\n" if "\r\n" in text or not text else "\n"
    pf = PluginsFile(path, fmt, encoding=encoding, newline=newline, exists=True)
    seen: set[str] = set()
    for raw in re.split(r"\r?\n", text):
        line = raw.strip()
        if not line or line.startswith("#"):
            pf.lines.append(raw.rstrip("\r"))
            continue
        active = True
        if fmt is PluginsTxtFormat.ASTERISK:
            active = line.startswith("*")
            line = line.lstrip("*").strip()
        if not is_plugin_name(line) or key(line) in seen:
            pf.lines.append(raw.rstrip("\r"))  # not a plugin, or listed twice: kept as it is
            continue
        seen.add(key(line))
        pf.lines.append(Entry(line, active))
    while pf.lines and pf.lines[-1] == "":
        pf.lines.pop()  # the final newline
    return pf


def read(path: Path | None, fmt: PluginsTxtFormat) -> PluginsFile:
    """The plugins.txt at *path*; an empty one (``exists`` False) if there is none."""
    if path is None:
        return PluginsFile(None, fmt)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return PluginsFile(path, fmt)
    return parse(data, fmt, path)


def render(pf: PluginsFile) -> bytes:
    out = []
    for line in pf.lines:
        if isinstance(line, Entry):
            if pf.format is PluginsTxtFormat.ASTERISK:
                out.append(("*" if line.active else "") + line.name)
            elif line.active:
                out.append(line.name)
        else:
            out.append(line)
    text = pf.newline.join(out) + (pf.newline if out else "")
    try:
        return text.encode(pf.encoding)
    except UnicodeEncodeError:
        return text.encode("utf-8")


def implicit(gdef: GameDef) -> set[str]:
    """Keys of the plugins the game loads without them being listed."""
    return {key(n) for n in gdef.implicit_plugins}


def active_keys(pf: PluginsFile, gdef: GameDef) -> set[str]:
    """Keys of the plugins the game loads (given they exist in Data)."""
    return {key(e.name) for e in pf.entries if e.active} | implicit(gdef)
