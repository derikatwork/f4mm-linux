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
   installer UI decides the mappings. ``candidate_roots`` lists the sub-folders
   that look installable on their own (fallback: pick sub-folders). A ``fomod``
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
* OS junk anywhere: ``__MACOSX/``, ``.DS_Store``, ``Thumbs.db``, ``desktop.ini``;
* the ``fomod/`` folder;
* loose docs and images directly in the chosen root (``DOC_EXTS``,
  ``IMAGE_EXTS``) and doc folders directly in the root (``DOC_DIRS``);
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

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

from bethmm.nexus import NexusInfo
from bethmm.plugin_header import PluginHeader

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
    "enbseries", "enbcache", "reshade-shaders", "reshade", "injector",
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
DOC_DIRS = frozenset({"docs", "documentation", "readme", "readmes", "screenshots"})
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
    #: Source path of fomod/ModuleConfig.xml when the mod is a FOMOD installer.
    fomod_config: str | None = None
    candidate_roots: list[RootCandidate] = field(default_factory=list)
    game: GameGuess = field(default_factory=lambda: GameGuess(None, Confidence.NONE))
    nexus: NexusInfo | None = None

    @property
    def is_fomod(self) -> bool:
        return self.fomod_config is not None

    def to_dict(self) -> dict:
        """JSON-friendly representation (enums as their values)."""
        raise NotImplementedError


#: Signature of the callback used to read plugin headers: receives a source
#: path (relative to the extraction folder) and returns its header, or None if
#: it can't be read.
HeaderReader = Callable[[str], "PluginHeader | None"]


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
    raise NotImplementedError


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
    ``warnings``. ``rule=Rule.MANUAL``, confidence HIGH (the user chose).
    Raises ValueError if *target* is UNKNOWN or a root does not exist in
    *files*; when *target* is None and a root's layout can't be detected,
    raises ValueError naming that root.
    """
    raise NotImplementedError


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
    raise NotImplementedError


def analyze_directory(
    path: str | Path,
    *,
    installed_games: Sequence[str] = (),
    archive_name: str | None = None,
) -> PlacementPlan:
    """Walk an extracted mod folder (without following symlinks) and run
    ``analyze_files`` with a header reader backed by
    ``plugin_header.read_plugin_header`` (unreadable plugins -> None).
    Symlinks inside the extraction are ignored and reported in warnings.
    """
    raise NotImplementedError
