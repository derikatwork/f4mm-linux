"""Decide where the files of an extracted mod archive belong.

Pure logic: works on a list of relative file paths (POSIX separators, as they
appear inside the archive / extraction folder) and never touches the game
folder. ``analyze_directory`` is the only function that reads the disk, and
only the extracted mod's own files.

Rules, applied in order (see ``analyze_files``):

1. **Wrapper folders.** While the current root contains exactly one directory,
   no other files except ignorable ones (top-level docs/images, OS junk), and
   that directory's name is not recognised (``Data``, ``fomod``, a known Data
   sub-folder, or a game-root folder like ``enbseries``), descend into it.
   Repeats, so ``CoolMod v1.2/CoolMod/Data/...`` works.
2. **FOMOD.** If ``<root>/fomod/ModuleConfig.xml`` exists (any case), the plan
   gets ``rule=Rule.FOMOD``, ``fomod_config`` set and ``target=UNKNOWN``; the
   installer UI decides the mappings. ``candidate_roots`` lists the folders
   that look installable on their own (fallback: pick sub-folders), starting
   with the root itself when the files next to ``fomod/`` match rules 3-5. A ``fomod``
   folder with only ``info.xml`` is not an installer: it is skipped and the
   remaining rules apply.
3. **Data folder.** Root contains a ``Data`` directory (any case) ->
   ``target=GAME_ROOT``; files keep their relative paths. (Script extender
   archives: loader exe + DLLs at root plus ``Data/``.)
4. **Data content.** Root contains plugins (.esp/.esm/.esl), Bethesda archives
   (.ba2/.bsa) or a known Data sub-folder -> ``target=DATA``; destinations get a
   ``Data/`` prefix.
5. **Game-root content.** Root contains game-root files (any ``.exe``/``.dll``/
   ``.asi`` directly in the root, ENB/ReShade ini files, ``enbseries/``,
   ``reshade-shaders/`` ...) -> ``target=GAME_ROOT``.
6. **Unknown.** ``target=UNKNOWN``, ``confidence=NONE``, no mappings;
   ``candidate_roots`` lists deeper folders that would match rules 3-5.

Mixed signals: when rule 4 fires but the root *also* has game-root-only files
(rule-5 signals), rule 4 still wins but confidence drops to LOW and a warning
names the files that would end up inside Data. When rule 3 fires and the root
also has loose plugins or Data sub-folders next to ``Data/``, confidence is
MEDIUM with a warning.

Not deployed (listed in ``skipped`` with nothing else happening to them):

* everything outside the chosen root (and wrapper-level docs);
* OS junk anywhere: ``__MACOSX/``, ``.DS_Store``, ``Thumbs.db``, ``desktop.ini``
  and macOS ``._*`` resource-fork files;
* the ``fomod/`` folder;
* loose docs and images directly in the chosen root (``DOC_EXTS``,
  ``IMAGE_EXTS``, extension-less ``DOC_NAMES`` such as ``README``) and doc
  folders directly in the root (``DOC_DIRS``). Exceptions for ``GAME_ROOT``
  targets, because game-folder mods ship such files next to the exe: a
  ``.txt`` file is only skipped when its name looks like documentation
  (``DOC_TXT_WORDS``; ENB's ``effect.txt`` and Engine Fixes' preload list are
  kept), and images whose name starts with ``enb`` (ENB textures) are kept;
* for ``GAME_ROOT`` targets when a script-extender loader is present: a
  top-level ``src/`` folder (script extender source code).

Confidence (``Confidence``): HIGH for a clean single-rule match, MEDIUM when the
match comes with oddities (unrecognised top-level folders that will be placed
under the target, plugins next to a Data folder ...), LOW for conflicting
signals, NONE for unknown.

Game identification (``identify_game``) is separate from layout and combines
votes from: plugin headers (via ``plugin_header.candidate_games``), script
extender loader names and runtime DLL prefixes, script-extender folders under
Data (``F4SE``/``SKSE``/...; SKSE is shared by Skyrim LE and SE), archive
extensions (.ba2 -> Fallout 4/Starfield, .bsa -> the rest; weak) and .esl
files (weak). Installed games break ties and let a plugin made for one sibling
(Skyrim LE <-> SE) fall back to the installed one. It always proposes; the user
confirms.
"""

from __future__ import annotations

import os
import stat
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from bethmm.games import GAMES, LOADER_TO_GAME, SIBLINGS
from bethmm.nexus import NexusInfo, parse_nexus_filename
from bethmm.plugin_header import (
    WEIGHT_DLC_MASTER,
    PluginHeader,
    PluginHeaderError,
    candidate_games,
    read_plugin_header,
)

# --------------------------------------------------------------------------
# Knowledge tables (all lowercase)

PLUGIN_EXTS = frozenset({".esp", ".esm", ".esl"})
BSA_EXTS = frozenset({".ba2", ".bsa"})

#: Folders that live directly under Data in at least one supported game.
KNOWN_DATA_DIRS = frozenset({
    "meshes", "textures", "scripts", "sound", "music", "interface", "materials",
    "strings", "seq", "vis", "lodsettings", "video", "shadersfx", "grass",
    "dialogueviews", "terrain", "trees", "lod", "distantlod", "facegen",
    "fonts", "menus", "shaders", "source", "programs", "particles",
    "geometries", "planetdata", "tools", "calientetools", "nemesis_engine",
    "netscriptframework", "mcm", "f4se", "skse", "nvse", "fose", "obse", "sfse",
    "config", "platform", "misc",
})

#: Folders that belong in the game root (next to the exe).
GAME_ROOT_DIRS = frozenset({
    "enbseries", "enbcache", "reshade-shaders",
})

#: Exact file names that belong in the game root.
GAME_ROOT_FILES = frozenset({
    "enbseries.ini", "enblocal.ini", "enbbloom.fx", "enbeffect.fx",
    "enbeffectprepass.fx", "enbadaptation.fx", "enblens.fx", "enbdepthoffield.fx",
    "enbsunsprite.fx", "enbunderwaterfog.fx", "enbpalette.bmp",
    "reshade.ini", "reshadepreset.ini", "dxvk.conf",
})

#: Extensions of files that, directly in the root, mean "game root".
GAME_ROOT_EXTS = frozenset({".exe", ".dll", ".asi"})

DOC_EXTS = frozenset({".txt", ".md", ".pdf", ".url", ".htm", ".html", ".rtf", ".doc", ".docx", ".odt", ".nfo"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"})
DOC_DIRS = frozenset({
    "docs", "documentation", "readme", "readmes", "screenshots", "screens", "images", "previews", "pictures", "pics",
})
#: Extension-less documentation files.
DOC_NAMES = frozenset({"readme", "license", "licence", "changelog", "changes", "credits", "authors", "copying"})
#: Words that mark a .txt file in a game-folder mod as documentation.
DOC_TXT_WORDS = (
    "readme", "read me", "read_me", "changelog", "change log", "changes", "whatsnew", "what's new",
    "license", "licence", "credits", "install", "instruction", "permission", "faq", "manual", "history",
    "notes",
)
JUNK_NAMES = frozenset({"__macosx", ".ds_store", "thumbs.db", "desktop.ini"})


class Target(Enum):
    DATA = "data"
    GAME_ROOT = "game_root"
    UNKNOWN = "unknown"


class Rule(Enum):
    FOMOD = "fomod"
    DATA_FOLDER = "data_folder"
    DATA_CONTENT = "data_content"
    GAME_ROOT_CONTENT = "game_root_content"
    UNKNOWN = "unknown"
    MANUAL = "manual"


class Confidence(Enum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    def __lt__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value < other.value

    def __le__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value <= other.value

    def __gt__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value > other.value

    def __ge__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value >= other.value


@dataclass(frozen=True)
class FileMapping:
    #: Path relative to the extraction folder, exactly as in the archive.
    source: str
    #: Path relative to the game root, e.g. "Data/Textures/x.dds" or
    #: "f4se_loader.exe". Case is the mod's; deployment resolves it.
    dest: str


@dataclass(frozen=True)
class RootCandidate:
    #: Folder relative to the extraction folder ("" = top).
    path: str
    target: Target
    rule: Rule


@dataclass
class GameGuess:
    game_id: str | None
    confidence: Confidence
    #: Every game with any evidence, best first.
    candidates: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlacementPlan:
    #: Chosen mod root relative to the extraction folder ("" = top).
    root: str
    target: Target
    rule: Rule
    confidence: Confidence
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mappings: list[FileMapping] = field(default_factory=list)
    #: Source files that will not be deployed.
    skipped: list[str] = field(default_factory=list)
    #: Wrapper folders descended through, outermost first.
    wrapper_dirs: list[str] = field(default_factory=list)
    #: For FOMOD/unknown plans: files under the root still waiting for the
    #: user's choice (neither mapped nor skipped).
    unplaced: list[str] = field(default_factory=list)
    #: Source path of fomod/ModuleConfig.xml when the mod is a FOMOD installer.
    fomod_config: str | None = None
    #: The choices made in the installer (``fomod.Wizard.choices``) once it
    #: has run and the mappings are its files; None before.
    fomod_choices: list[dict] | None = None
    candidate_roots: list[RootCandidate] = field(default_factory=list)
    game: GameGuess = field(default_factory=lambda: GameGuess(None, Confidence.NONE))
    nexus: NexusInfo | None = None

    @property
    def is_fomod(self) -> bool:
        return self.fomod_config is not None

    def to_dict(self) -> dict:
        """JSON-friendly representation (enums as their values)."""
        return {
            "root": self.root,
            "target": self.target.value,
            "rule": self.rule.value,
            "confidence": self.confidence.value,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "mappings": [{"source": m.source, "dest": m.dest} for m in self.mappings],
            "skipped": list(self.skipped),
            "wrapper_dirs": list(self.wrapper_dirs),
            "unplaced": list(self.unplaced),
            "fomod_config": self.fomod_config,
            "is_fomod": self.is_fomod,
            "fomod_choices": self.fomod_choices,
            "candidate_roots": [
                {"path": c.path, "target": c.target.value, "rule": c.rule.value}
                for c in self.candidate_roots
            ],
            "game": {
                "game_id": self.game.game_id,
                "confidence": self.game.confidence.value,
                "candidates": list(self.game.candidates),
                "reasons": list(self.game.reasons),
            },
            "nexus": asdict(self.nexus) if self.nexus is not None else None,
        }


#: Signature of the callback used to read plugin headers: receives a source
#: path (relative to the extraction folder) and returns its header, or None if
#: it can't be read.
HeaderReader = Callable[[str], "PluginHeader | None"]


# --------------------------------------------------------------------------
# Private tables

#: Folder names that stop wrapper descent.
_RECOGNISED_DIRS = frozenset({"data", "fomod"}) | KNOWN_DATA_DIRS | GAME_ROOT_DIRS
_DOC_OR_IMAGE_EXTS = DOC_EXTS | IMAGE_EXTS

#: Folder levels below the root searched for candidate roots.
_CANDIDATE_DEPTH = 4

#: Lowercased script-extender folder under Data -> games using it.
_SE_FOLDERS: dict[str, tuple[str, ...]] = {}
for _game in GAMES.values():
    _key = _game.script_extender.lower()
    _SE_FOLDERS[_key] = _SE_FOLDERS.get(_key, ()) + (_game.id,)
del _game, _key

_GAME_ORDER = {game_id: i for i, game_id in enumerate(GAMES)}

#: identify_game weights for non-header evidence (header votes use
#: plugin_header's weights: 10/8 for masters, 6 and below for header layout).
_W_LOADER = 8
_W_SE_DLL = 6
_W_SE_FOLDER = 5
_W_WEAK = 1

_MAX_REASONS = 6
_MAX_LISTED_COLLISIONS = 10


# --------------------------------------------------------------------------
# File-list helpers


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _is_junk(name: str) -> bool:
    low = name.lower()
    return low in JUNK_NAMES or low.startswith("._")


def _is_doc_file(name: str, target: Target | None = None) -> bool:
    """Whether a file directly in the mod root is documentation (not deployed).

    *target* GAME_ROOT keeps ``.txt`` files unless their name looks like docs.
    """
    low = name.lower()
    if low in GAME_ROOT_FILES:
        return False
    ext = _ext(low)
    if target is Target.GAME_ROOT:
        if ext == ".txt":
            return any(word in low for word in DOC_TXT_WORDS)
        # ENB presets ship their textures (enbsunsprite.bmp, enblensmask.png ...)
        # next to the exe.
        if ext in IMAGE_EXTS and low.startswith("enb"):
            return False
    return ext in _DOC_OR_IMAGE_EXTS or low in DOC_NAMES


def _sort_key(path: str) -> tuple[str, str]:
    return path.lower(), path


def _names(items: Sequence[str], limit: int = 4) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} and {len(items) - limit} more"


def has_control_char(path: str) -> bool:
    """Whether *path* has a control character (a newline, ...): no Windows
    file name can, and one would split a line of plugins.txt or the log."""
    return any(ord(c) < 32 or ord(c) == 127 for c in path)


def _normalize_files(files: Iterable[str]) -> tuple[list[str], list[str]]:
    """POSIX-style, de-duplicated, sorted file paths, plus warnings.

    Entries ending in a separator are directories and are dropped, as are
    empty ones; entries with ``..`` components or control characters are
    unsafe and dropped with a warning.
    """
    seen: dict[str, None] = {}
    warnings: list[str] = []
    for raw in files:
        path = str(raw).replace("\\", "/")
        if not path.strip() or path.endswith("/"):
            continue
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if not parts:
            continue
        if ".." in parts or has_control_char(path):
            warnings.append(f"ignored unsafe path {str(raw)!r}")
            continue
        seen.setdefault("/".join(parts), None)
    return sorted(seen, key=_sort_key), warnings


def _normalize_root(root: str) -> str:
    parts = [p for p in str(root).replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"invalid root folder {root!r}")
    return "/".join(parts)


@dataclass
class _Dir:
    """One folder of the extracted mod, built from the file list."""

    name: str
    #: Relative to the extraction folder ("" = top).
    path: str
    #: File name -> source path.
    files: dict[str, str] = field(default_factory=dict)
    dirs: dict[str, "_Dir"] = field(default_factory=dict)

    def child(self, name: str) -> "_Dir":
        sub = self.dirs.get(name)
        if sub is None:
            sub = self.dirs[name] = _Dir(name, f"{self.path}/{name}" if self.path else name)
        return sub

    def walk(self, prefix: str = "") -> Iterator[tuple[str, str]]:
        """(source, path relative to this folder) for every file below it."""
        for name, source in self.files.items():
            yield source, prefix + name
        for name, sub in self.dirs.items():
            yield from sub.walk(f"{prefix}{name}/")

    def subdirs(self) -> list["_Dir"]:
        """Child folders worth searching (no OS junk, doc folders or fomod)."""
        return [
            sub for name, sub in self.dirs.items()
            if not _is_junk(name) and name.lower() not in DOC_DIRS and name.lower() != "fomod"
        ]

    def is_empty(self) -> bool:
        return not self.files and not self.dirs


def _build_tree(files: Sequence[str]) -> _Dir:
    top = _Dir("", "")
    for source in files:
        *dirs, name = source.split("/")
        node = top
        for d in dirs:
            node = node.child(d)
        node.files[name] = source
    return top


def _find_dir(top: _Dir, path: str) -> _Dir | None:
    """The folder at *path*; components fall back to a unique case-insensitive match."""
    node = top
    for part in path.split("/") if path else ():
        sub = node.dirs.get(part)
        if sub is None:
            matches = [d for n, d in node.dirs.items() if n.lower() == part.lower()]
            if len(matches) != 1:
                return None
            sub = matches[0]
        node = sub
    return node


# --------------------------------------------------------------------------
# Layout rules


@dataclass
class _Signals:
    """What a candidate root folder contains, by rule."""

    data_dirs: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    known_dirs: list[str] = field(default_factory=list)
    root_files: list[str] = field(default_factory=list)
    root_dirs: list[str] = field(default_factory=list)
    unknown_dirs: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    fomod_config: str | None = None

    @property
    def data_content(self) -> list[str]:
        return self.plugins + self.archives + [d + "/" for d in self.known_dirs]

    @property
    def game_root_content(self) -> list[str]:
        return self.root_files + [d + "/" for d in self.root_dirs]


def _signals(node: _Dir) -> _Signals:
    s = _Signals()
    for name in node.files:
        low = name.lower()
        if _is_junk(name):
            continue
        ext = _ext(low)
        if ext in PLUGIN_EXTS:
            s.plugins.append(name)
        elif ext in BSA_EXTS:
            s.archives.append(name)
        elif ext in GAME_ROOT_EXTS or low in GAME_ROOT_FILES:
            s.root_files.append(name)
        if low in LOADER_TO_GAME:
            s.loaders.append(name)
    for name, sub in node.dirs.items():
        low = name.lower()
        if _is_junk(name) or low in DOC_DIRS:
            continue
        if low == "data":
            s.data_dirs.append(name)
        elif low == "fomod":
            # The XML installer, or the older C# one (FOMM): either chooses what to install.
            config = next((src for n, src in sub.files.items() if n.lower() == "moduleconfig.xml"), None) \
                or next((src for n, src in sub.files.items() if n.lower() == "script.cs"), None)
            if s.fomod_config is None:
                s.fomod_config = config
        elif low in KNOWN_DATA_DIRS:
            s.known_dirs.append(name)
        elif low in GAME_ROOT_DIRS:
            s.root_dirs.append(name)
        else:
            s.unknown_dirs.append(name)
    return s


def _detect(s: _Signals) -> tuple[Target, Rule] | None:
    """Rules 3-5 for one folder."""
    if s.data_dirs:
        return Target.GAME_ROOT, Rule.DATA_FOLDER
    if s.data_content:
        return Target.DATA, Rule.DATA_CONTENT
    if s.game_root_content:
        return Target.GAME_ROOT, Rule.GAME_ROOT_CONTENT
    return None


def _ignorable_file(name: str) -> bool:
    """Top-level files that don't stop wrapper descent: docs, images, OS junk."""
    return _is_junk(name) or _is_doc_file(name)


def _wrapper_child(node: _Dir) -> _Dir | None:
    """The folder to descend into when *node* is only a wrapper (rule 1)."""
    dirs = [d for n, d in node.dirs.items() if not _is_junk(n) and n.lower() not in DOC_DIRS]
    if len(dirs) != 1:
        return None
    # A recognised folder (textures/, F4SE/ with Plugins/ ...) is the mod's
    # content -- unless it wraps a script extender's loader (a repacked
    # "F4SE/f4se_loader.exe") or a Data folder ("Tools/Data/x.esp"): no
    # folder of that name holds those.
    low = dirs[0].name.lower()
    if low in _RECOGNISED_DIRS:
        inner = _signals(dirs[0])
        if low in ("data", "fomod") or not (inner.loaders or inner.data_dirs):
            return None
    if not all(_ignorable_file(name) for name in node.files):
        return None
    return dirs[0]


def _candidate_roots(node: _Dir, max_depth: int = _CANDIDATE_DEPTH) -> list[RootCandidate]:
    """Folders below *node* that match rules 3-5 on their own, shallowest first.

    A matching folder is not searched further.
    """
    found: list[RootCandidate] = []
    queue = deque((sub, 1) for sub in node.subdirs())
    while queue:
        sub, depth = queue.popleft()
        detected = _detect(_signals(sub))
        if detected is not None:
            found.append(RootCandidate(sub.path, *detected))
        elif depth < max_depth:
            queue.extend((d, depth + 1) for d in sub.subdirs())
    return found


def _is_skipped(rel: str, target: Target, loader: bool) -> bool:
    """Whether the file at *rel* (relative to the mod root) is not deployed."""
    parts = rel.split("/")
    if any(_is_junk(p) for p in parts):
        return True
    first = parts[0].lower()
    if len(parts) == 1:
        return _is_doc_file(parts[0], target)
    if first == "fomod" or first in DOC_DIRS:
        return True
    return target is Target.GAME_ROOT and loader and first == "src"


def _map_root(node: _Dir, target: Target, loader: bool) -> tuple[list[FileMapping], list[str]]:
    """Mappings and skipped sources for the files below *node*.

    With ``Target.UNKNOWN`` nothing is mapped; skipped files are still listed.
    """
    mappings: list[FileMapping] = []
    skipped: list[str] = []
    for source, rel in node.walk():
        if _is_skipped(rel, target, loader):
            skipped.append(source)
        elif target is Target.DATA:
            mappings.append(FileMapping(source, "Data/" + rel))
        elif target is Target.GAME_ROOT:
            mappings.append(FileMapping(source, rel))
    return mappings, skipped


def _drop_collisions(mappings: Sequence[FileMapping]) -> tuple[list[FileMapping], list[str]]:
    """Keep the last mapping for each destination (compared case-insensitively)."""
    by_dest: dict[str, FileMapping] = {}
    replaced: list[str] = []
    for m in mappings:
        key = m.dest.lower()
        previous = by_dest.pop(key, None)
        if previous is not None and previous.source != m.source:
            replaced.append(f"{m.source} replaces {previous.source} (both go to {m.dest})")
        by_dest[key] = m
    warnings = replaced[:_MAX_LISTED_COLLISIONS]
    if len(replaced) > _MAX_LISTED_COLLISIONS:
        warnings.append(f"... and {len(replaced) - _MAX_LISTED_COLLISIONS} more files replaced by later ones")
    return sorted(by_dest.values(), key=lambda m: _sort_key(m.source)), warnings


def _where(path: str) -> str:
    return f"'{path}'" if path else "the top level"


def _target_text(target: Target) -> str:
    return "Data" if target is Target.DATA else "the game folder"


def _apply_rules(plan: PlacementPlan, node: _Dir) -> list[str]:
    """Rules 2-6 on the (unwrapped) root *node*; fills *plan* and returns the
    skipped sources below the root for plans without mappings."""
    s = _signals(node)
    where = _where(node.path)

    if s.fomod_config is not None:
        plan.rule, plan.target, plan.confidence = Rule.FOMOD, Target.UNKNOWN, Confidence.HIGH
        plan.fomod_config = s.fomod_config
        plan.candidate_roots = _candidate_roots(node)
        here = _detect(s)
        if here is not None:
            plan.candidate_roots.insert(0, RootCandidate(node.path, *here))
        plan.reasons.append(f"FOMOD installer found ({s.fomod_config}); choose what to install")
        if plan.candidate_roots:
            plan.reasons.append(f"{len(plan.candidate_roots)} folder(s) inside look installable on their own")
        return _map_root(node, Target.UNKNOWN, False)[1]

    detected = _detect(s)
    if detected is None:
        plan.rule, plan.target, plan.confidence = Rule.UNKNOWN, Target.UNKNOWN, Confidence.NONE
        plan.candidate_roots = _candidate_roots(node)
        plan.reasons.append(
            f"no Data folder, plugins, archives, known Data folders or game-folder files at {where}"
        )
        if plan.candidate_roots:
            plan.reasons.append(f"{len(plan.candidate_roots)} folder(s) inside could be installed")
        return _map_root(node, Target.UNKNOWN, False)[1]

    target, rule = detected
    loader = bool(s.loaders)
    confidence = Confidence.HIGH
    if rule is Rule.DATA_FOLDER:
        plan.reasons.append(
            f"{where} contains a {s.data_dirs[0]} folder, so it is the game folder (files keep their paths)"
        )
        if s.data_content:
            confidence = Confidence.MEDIUM
            plan.warnings.append(
                f"Data content next to the {s.data_dirs[0]} folder ({_names(s.data_content)}) will be "
                "placed in the game folder, not in Data"
            )
    elif rule is Rule.DATA_CONTENT:
        found = []
        if s.plugins:
            found.append(f"plugins ({_names(s.plugins, 3)})")
        if s.archives:
            found.append(f"archives ({_names(s.archives, 3)})")
        if s.known_dirs:
            found.append(f"Data folders ({_names([d + '/' for d in s.known_dirs], 3)})")
        plan.reasons.append(f"{where} contains {', '.join(found)}, so it goes into Data")
        if s.game_root_content:
            confidence = Confidence.LOW
            plan.warnings.append(
                f"game-folder files ({_names(s.game_root_content)}) will be placed inside Data; they "
                "normally sit next to the game's .exe, so choose the game folder if that is wrong"
            )
    else:
        plan.reasons.append(
            f"{where} contains game-folder files ({_names(s.game_root_content, 3)}), "
            "so it goes next to the game's .exe"
        )

    unknown = [d for d in s.unknown_dirs if not (target is Target.GAME_ROOT and loader and d.lower() == "src")]
    if unknown:
        if confidence is Confidence.HIGH:
            confidence = Confidence.MEDIUM
        plan.warnings.append(
            f"unrecognised folder(s) {_names([d + '/' for d in unknown])} will be placed in {_target_text(target)}"
        )
    if target is Target.GAME_ROOT and loader and any(d.lower() == "src" for d in s.unknown_dirs):
        plan.reasons.append(f"script extender source code (src/) next to {s.loaders[0]} is not installed")

    plan.target, plan.rule, plan.confidence = target, rule, confidence
    plan.mappings = _map_root(node, target, loader)[0]
    return []


def _evidence_mappings(top: _Dir, node: _Dir, candidates: Sequence[RootCandidate],
                       skipped: set[str]) -> list[FileMapping]:
    """Provisional mappings used to identify the game of a FOMOD/unknown mod."""
    found: dict[str, FileMapping] = {}
    for c in candidates:
        sub = _find_dir(top, c.path)
        if sub is None:
            continue
        for m in _map_root(sub, c.target, bool(_signals(sub).loaders))[0]:
            found.setdefault(m.source, m)
    for source, rel in node.walk():
        if source not in skipped:
            found.setdefault(source, FileMapping(source, rel))
    return list(found.values())


def analyze_files(
    files: Iterable[str],
    *,
    header_reader: HeaderReader | None = None,
    installed_games: Sequence[str] = (),
    archive_name: str | None = None,
) -> PlacementPlan:
    """Apply rules 1-6, then ``identify_game``, then parse *archive_name* with
    ``nexus.parse_nexus_filename`` (rule 8). *files* are file paths only
    (directories are implied); backslashes are treated as separators and
    leading "./" or "/" stripped. Duplicate or empty entries are ignored.
    """
    paths, warnings = _normalize_files(files)
    top = _build_tree(paths)

    node, wrappers = top, []
    while (inner := _wrapper_child(node)) is not None:
        wrappers.append(inner.name)
        node = inner

    plan = PlacementPlan(
        root=node.path,
        target=Target.UNKNOWN,
        rule=Rule.UNKNOWN,
        confidence=Confidence.NONE,
        warnings=warnings,
        wrapper_dirs=wrappers,
    )
    plan.reasons.extend(f"stepped into wrapper folder '{w}'" for w in wrappers)

    if not paths:
        plan.reasons.append("there are no files")
        skipped_in_root: list[str] = []
    else:
        skipped_in_root = _apply_rules(plan, node)

    if plan.target is Target.UNKNOWN:
        in_root = {source for source, _ in node.walk()}
        skip_set = set(skipped_in_root) | (set(paths) - in_root)
        evidence = _evidence_mappings(top, node, plan.candidate_roots, skip_set)
    else:
        plan.mappings, collisions = _drop_collisions(plan.mappings)
        plan.warnings.extend(collisions)
        skip_set = set(paths) - {m.source for m in plan.mappings}
        evidence = plan.mappings
    plan.skipped = [p for p in paths if p in skip_set]
    if plan.target is Target.UNKNOWN:
        plan.unplaced = [p for p in paths if p not in skip_set]

    plan.game = identify_game(evidence, header_reader=header_reader, installed_games=installed_games)
    plan.nexus = parse_nexus_filename(archive_name) if archive_name else None
    return plan


def _nested(outer: str, inner: str) -> bool:
    """Whether folder *inner* is *outer* or lies below it ("" = top)."""
    return not outer or inner == outer or inner.startswith(outer + "/")


def _common_parent(paths: Sequence[str]) -> str:
    split = [p.split("/") if p else [] for p in paths]
    common: list[str] = []
    for parts in zip(*split):
        if any(p != parts[0] for p in parts):
            break
        common.append(parts[0])
    return "/".join(common)


def plan_for_fomod(plan: PlacementPlan, files: Iterable[str], mappings: Sequence[FileMapping],
                   choices: list[dict], *, warnings: Sequence[str] = (), chosen: Sequence[str] = ()) -> PlacementPlan:
    """*plan* (a FOMOD plan) with the files its installer chose: they go into
    Data; everything else in the archive is not installed."""
    mapped = {m.source for m in mappings}
    reasons = ["you ran its FOMOD installer"
               + (f" and chose {', '.join(repr(c) for c in chosen[:8])}" + (", ..." if len(chosen) > 8 else "")
                  if chosen else "")]
    return replace(plan, target=Target.DATA, rule=Rule.FOMOD, confidence=Confidence.HIGH,
                   reasons=reasons, warnings=list(plan.warnings) + list(warnings), mappings=list(mappings),
                   skipped=sorted(f for f in files if f not in mapped), unplaced=[], candidate_roots=[],
                   fomod_choices=list(choices))


def plan_for_roots(
    files: Iterable[str],
    roots: Sequence[str],
    target: Target | None = None,
    *,
    header_reader: HeaderReader | None = None,
    installed_games: Sequence[str] = (),
    archive_name: str | None = None,
) -> PlacementPlan:
    """The user's manual choice: install the given *roots* (sub-folders of the
    extraction folder, "" = top) with *target*, or with each root's own
    auto-detected target (rules 3-5) when *target* is None. Used for "pick a
    different root folder", for unknown layouts and for the FOMOD fallback
    ("choose which sub-folders to install"). Later roots win when two roots map
    to the same destination (case-insensitively); that's recorded in
    ``warnings``. Overlapping roots (one inside the other) also resolve to the
    later root, so every file is deployed at most once; that's warned about too.
    ``rule=Rule.MANUAL``, confidence HIGH (the user chose).
    Raises ValueError if *target* is UNKNOWN or a root does not exist in
    *files*; when *target* is None and a root's layout can't be detected,
    raises ValueError naming that root.
    """
    if target is Target.UNKNOWN:
        raise ValueError("target must be Data or the game folder, not unknown")
    if isinstance(roots, str):
        roots = [roots]
    if not roots:
        raise ValueError("no root folder given")
    paths, warnings = _normalize_files(files)
    top = _build_tree(paths)

    chosen: list[tuple[_Dir, Target]] = []
    for raw in roots:
        node = _find_dir(top, _normalize_root(raw))
        if node is None or node.is_empty():
            raise ValueError(f"{raw!r} is not a folder in this mod")
        if any(node is n for n, _ in chosen):
            continue
        root_target = target
        if root_target is None:
            detected = _detect(_signals(node))
            if detected is None:
                raise ValueError(
                    f"can't tell where the files in {_where(node.path)} belong; choose Data or the game folder"
                )
            root_target = detected[0]
        chosen.append((node, root_target))

    targets = {t for _, t in chosen}
    plan = PlacementPlan(
        root=chosen[0][0].path if len(chosen) == 1 else _common_parent([n.path for n, _ in chosen]),
        target=targets.pop() if len(targets) == 1 else Target.GAME_ROOT,
        rule=Rule.MANUAL,
        confidence=Confidence.HIGH,
        warnings=warnings,
    )
    #: Source -> its mapping (None = skipped). With nested roots a file belongs
    #: to several of them; the later root's verdict replaces the earlier one.
    verdicts: dict[str, FileMapping | None] = {}
    for node, root_target in chosen:
        mappings, skipped = _map_root(node, root_target, bool(_signals(node).loaders))
        for source, verdict in [(s, None) for s in skipped] + [(m.source, m) for m in mappings]:
            verdicts.pop(source, None)
            verdicts[source] = verdict
        plan.reasons.append(f"you chose to install {_where(node.path)} into {_target_text(root_target)}")
    for i, (outer, _) in enumerate(chosen):
        for later, _ in chosen[i + 1:]:
            if _nested(outer.path, later.path) or _nested(later.path, outer.path):
                plan.warnings.append(
                    f"{_where(outer.path)} and {_where(later.path)} overlap; files in both are installed "
                    f"as part of {_where(later.path)}, the later choice"
                )
    plan.mappings, collisions = _drop_collisions([m for m in verdicts.values() if m is not None])
    plan.warnings.extend(collisions)
    mapped = {m.source for m in plan.mappings}
    plan.skipped = [p for p in paths if p not in mapped]
    plan.game = identify_game(plan.mappings, header_reader=header_reader, installed_games=installed_games)
    plan.nexus = parse_nexus_filename(archive_name) if archive_name else None
    return plan


# --------------------------------------------------------------------------
# Game identification


@dataclass
class _Evidence:
    score: int = 0
    kinds: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    unlisted: int = 0

    def add(self, weight: int, kind: str, reason: str) -> None:
        self.score += weight
        self.kinds.add(kind)
        if reason in self.reasons:
            return
        if len(self.reasons) < _MAX_REASONS:
            self.reasons.append(reason)
        else:
            self.unlisted += 1


def _read_header(reader: HeaderReader | None, source: str) -> PluginHeader | None:
    if reader is None:
        return None
    try:
        return reader(source)
    except (OSError, ValueError):
        return None


def _se_dll_games(name: str) -> list[str]:
    """Games whose script-extender runtime DLL prefix *name* (lowercase) has;
    the longest matching prefix wins so "skse64_" isn't mistaken for "skse"."""
    matches = [(len(g.se_dll_prefix), g.id) for g in GAMES.values() if name.startswith(g.se_dll_prefix.lower())]
    if not matches:
        return []
    longest = max(n for n, _ in matches)
    return [game_id for n, game_id in matches if n == longest]


def _game_names(game_ids: Iterable[str]) -> str:
    names = [GAMES[g].name for g in game_ids]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def identify_game(
    mappings: Sequence[FileMapping],
    *,
    header_reader: HeaderReader | None = None,
    installed_games: Sequence[str] = (),
) -> GameGuess:
    """Guess the game from the files that will be deployed (see module doc).

    Confidence: HIGH when the best game has master-file evidence and no other
    game has master evidence; MEDIUM when the best has loader/SE-folder
    evidence, or header-only evidence, or won via the installed-games
    tie-break or sibling fallback; LOW for weak evidence only (archive
    extensions, .esl) or conflicting master evidence; NONE without evidence --
    except that with no evidence and exactly one installed game, that game is
    proposed with LOW confidence and the reason "only installed game".
    """
    installed = [g for g in dict.fromkeys(installed_games) if g in GAMES]
    evidence: dict[str, _Evidence] = {}
    once: set[tuple[str, str]] = set()
    #: (plugin name, games its masters point to), one per plugin with master evidence.
    master_sets: list[tuple[str, frozenset[str]]] = []

    def vote(game_id: str, weight: int, kind: str, reason: str, once_key: str | None = None) -> None:
        if once_key is not None:
            if (game_id, once_key) in once:
                return
            once.add((game_id, once_key))
        evidence.setdefault(game_id, _Evidence()).add(weight, kind, reason)

    for m in mappings:
        parts = [p for p in m.dest.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts:
            continue
        name = parts[-1]
        low = name.lower()
        ext = _ext(low)
        if ext in PLUGIN_EXTS:
            header = _read_header(header_reader, m.source)
            if header is not None:
                votes = candidate_games(header, name)
                masters = frozenset(v.game_id for v in votes if v.weight >= WEIGHT_DLC_MASTER)
                if masters:
                    master_sets.append((name, masters))
                for v in votes:
                    vote(v.game_id, v.weight, "master" if v.weight >= WEIGHT_DLC_MASTER else "header", v.reason)
            if ext == ".esl":
                for g in GAMES.values():
                    if g.supports_esl:
                        vote(g.id, _W_WEAK, "weak", "contains .esl plugins", once_key="esl")
        elif ext in BSA_EXTS:
            for g in GAMES.values():
                if ext in g.archive_exts:
                    vote(g.id, _W_WEAK, "weak", f"contains {ext} archives", once_key=ext)
        loader_game = LOADER_TO_GAME.get(low)
        if loader_game is not None:
            se = GAMES[loader_game].script_extender
            vote(loader_game, _W_LOADER, "loader", f"contains the {se} loader {name}", once_key="loader")
        if len(parts) == 1 and ext == ".dll":
            for game_id in _se_dll_games(low):
                se = GAMES[game_id].script_extender
                vote(game_id, _W_SE_DLL, "loader", f"contains the {se} runtime {name}", once_key="se_dll")
        if len(parts) >= 3 and parts[0].lower() == "data":
            for game_id in _SE_FOLDERS.get(parts[1].lower(), ()):
                vote(game_id, _W_SE_FOLDER, "loader", f"has a Data/{parts[1]} folder", once_key="se_folder")

    if not evidence:
        if len(installed) == 1:
            return GameGuess(installed[0], Confidence.LOW, [installed[0]], ["only installed game"])
        return GameGuess(None, Confidence.NONE)

    ranked = sorted(evidence, key=lambda g: (-evidence[g].score, _GAME_ORDER[g]))
    top = [g for g in ranked if evidence[g].score == evidence[ranked[0]].score]
    best = top[0]
    notes: list[str] = []
    tie_break = unresolved = sibling = False
    if len(top) > 1:
        top_installed = [g for g in top if g in installed]
        if len(top_installed) == 1:
            best, tie_break = top_installed[0], True
            others = [g for g in top if g != best]
            notes.append(f"chosen over {_game_names(others)} because it is installed")
        else:
            if top_installed:
                best = top_installed[0]
            unresolved = True
            notes.append(f"the evidence fits {_game_names(top)} equally well")

    proposed = best
    if installed and best not in installed:
        sib = next((s for s in SIBLINGS.get(best, ()) if s in installed), None)
        if sib is not None:
            proposed, sibling = sib, True
            notes.append(f"{GAMES[best].name} is not installed; proposing {GAMES[sib].name}, which is")

    conflict = len(master_sets) > 1 and not frozenset.intersection(*(games for _, games in master_sets))
    if conflict:
        detail = [f"{plugin} -> {_game_names(sorted(games, key=_GAME_ORDER.get))}" for plugin, games in master_sets]
        more = f"; and {len(detail) - 4} more" if len(detail) > 4 else ""
        notes.append(f"plugins need masters from different games: {'; '.join(detail[:4])}{more}")

    kinds = evidence[best].kinds
    master_games = {g for g, ev in evidence.items() if "master" in ev.kinds}
    if conflict or unresolved or kinds <= {"weak"}:
        confidence = Confidence.LOW
    elif tie_break or sibling:
        confidence = Confidence.MEDIUM
    elif "master" in kinds and master_games == {best}:
        confidence = Confidence.HIGH
    else:
        confidence = Confidence.MEDIUM

    reasons = list(evidence[best].reasons)
    if evidence[best].unlisted:
        reasons.append(f"... and {evidence[best].unlisted} more")
    candidates = [proposed] + [g for g in ranked if g != proposed]
    return GameGuess(proposed, confidence, candidates, reasons + notes)


# --------------------------------------------------------------------------
# Disk


def scan_directory(path: str | Path) -> tuple[list[str], HeaderReader, list[str]]:
    """List an extracted mod folder for ``analyze_files`` / ``plan_for_roots``.

    Returns ``(files, header_reader, warnings)``: relative POSIX paths of the
    regular files (walked without following symlinks), a reader backed by
    ``plugin_header.read_plugin_header`` (unreadable plugins -> None), and
    warnings about symlinks, special files and unreadable entries, which are
    left out.
    """
    base = Path(path)
    if not base.is_dir():
        raise NotADirectoryError(f"{base} is not a folder")
    files: list[str] = []
    warnings: list[str] = []

    def on_error(exc: OSError) -> None:
        warnings.append(f"could not read {exc.filename}: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(base, onerror=on_error):
        rel_dir = Path(dirpath).relative_to(base)
        for name in sorted(dirnames):
            if Path(dirpath, name).is_symlink():
                warnings.append(f"ignored symlink inside the mod: {(rel_dir / name).as_posix()}")
        dirnames[:] = sorted(d for d in dirnames if not Path(dirpath, d).is_symlink())
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            try:
                mode = Path(dirpath, name).lstat().st_mode
            except OSError as exc:
                warnings.append(f"could not read {rel}: {exc.strerror or exc}")
                continue
            if stat.S_ISLNK(mode):
                warnings.append(f"ignored symlink inside the mod: {rel}")
            elif not stat.S_ISREG(mode):
                warnings.append(f"ignored special file inside the mod: {rel}")
            else:
                if "\\" in name:
                    warnings.append(f"file name contains a backslash, treated as a folder separator: {rel}")
                files.append(rel)

    def reader(source: str) -> PluginHeader | None:
        full = base / source
        if full.is_symlink():
            return None
        try:
            return read_plugin_header(full)
        except PluginHeaderError:
            return None

    return files, reader, warnings


def analyze_directory(
    path: str | Path,
    *,
    installed_games: Sequence[str] = (),
    archive_name: str | None = None,
) -> PlacementPlan:
    """Walk an extracted mod folder (see ``scan_directory``) and run
    ``analyze_files`` on it. Symlinks inside the extraction are ignored and
    reported in warnings.
    """
    files, reader, warnings = scan_directory(path)
    plan = analyze_files(files, header_reader=reader, installed_games=installed_games, archive_name=archive_name)
    plan.warnings[:0] = warnings
    return plan
