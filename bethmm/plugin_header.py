"""Read the TES4 header record of a Bethesda plugin (.esp/.esm/.esl).

Binary layout (all little-endian)::

    Record header
      0   char[4]  type          "TES4" ("TES3" = Morrowind -> unsupported)
      4   uint32   data_size     bytes of subrecord data after the header
      8   uint32   flags         0x1 = master (ESM flag); 0x200 = light (FO4/SSE)
                                 0x40000 = compressed (never valid on TES4)
      12  uint32   form_id       always 0 for TES4
      16  uint32   vc_info
      -- Oblivion stops here: 20-byte header --
      20  uint16   form_version  e.g. 43 = Skyrim LE, 44 = Skyrim SE, 131 = Fallout 4
      22  uint16   unknown
      -- everything after Oblivion: 24-byte header --

    Subrecords (repeat until data_size is consumed)
      char[4] type, uint16 size, byte[size] data
      Special case "XXXX": size == 4, data = uint32 real size of the NEXT
      subrecord; that next subrecord's own uint16 size field is ignored.

    Relevant subrecords inside TES4
      HEDR  float32 version, uint32 num_records, uint32 next_object_id
      CNAM  zstring author
      SNAM  zstring description
      MAST  zstring master filename (one per master, each followed by DATA)
      DATA  uint64 (ignore)

The header size is detected by looking for "HEDR" at offset 20 (Oblivion) or
24 (everything else). Strings are cp1252, NUL-terminated.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from bethmm.games import GAMES, OFFICIAL_MASTER_TO_GAMES

PLUGIN_EXTS = (".esp", ".esm", ".esl")

#: Upper bound on a believable TES4 data size; anything larger is corruption.
MAX_TES4_DATA_SIZE = 256 * 1024 * 1024
FLAG_COMPRESSED = 0x40000

#: How much of a plugin read_plugin_header reads at most. MAST subrecords come
#: right after HEDR/CNAM/SNAM, so this is plenty even for huge TES4 records.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024


class PluginHeaderError(ValueError):
    """The data is not a readable TES4 plugin header."""


@dataclass(frozen=True)
class PluginHeader:
    masters: tuple[str, ...]
    hedr_version: float | None
    #: Record-header form version; ``None`` for 20-byte (Oblivion) headers.
    form_version: int | None
    #: 20 (Oblivion) or 24 (everything later).
    header_size: int
    flags: int
    num_records: int | None = None
    author: str | None = None
    description: str | None = None
    #: True when the buffer ended before data_size was consumed.
    truncated: bool = False

    @property
    def is_master_flag(self) -> bool:
        return bool(self.flags & 0x1)

    @property
    def is_light_flag(self) -> bool:
        return bool(self.flags & 0x200)


def parse_plugin_header(data: bytes) -> PluginHeader:
    """Parse the TES4 record at the start of *data*.

    Must tolerate a buffer that ends early, as long as the fixed record header
    and HEDR are complete: parse every complete subrecord and set
    ``truncated=True``. Raise :class:`PluginHeaderError` for anything that is
    not a TES4 record (wrong magic, TES3, no HEDR, nonsense sizes, compressed
    flag set).
    """
    if len(data) < 4:
        raise PluginHeaderError("file is too small to be a plugin")
    magic = bytes(data[:4])
    if magic == b"TES3":
        raise PluginHeaderError("Morrowind (TES3) plugins are not supported")
    if magic != b"TES4":
        raise PluginHeaderError(f"not a plugin: expected a TES4 record, found {magic!r}")
    if len(data) < 26:
        raise PluginHeaderError("plugin header is truncated")
    data_size, flags = struct.unpack_from("<II", data, 4)
    if flags & FLAG_COMPRESSED:
        raise PluginHeaderError("TES4 record claims to be compressed")
    if data_size > MAX_TES4_DATA_SIZE:
        raise PluginHeaderError(f"TES4 record size {data_size} is not believable")

    if data[20:24] == b"HEDR":
        header_size, form_version = 20, None
    elif data[24:28] == b"HEDR":
        header_size = 24
        (form_version,) = struct.unpack_from("<H", data, 20)
    else:
        raise PluginHeaderError("TES4 record has no HEDR subrecord")

    record_end = header_size + data_size
    end = min(record_end, len(data))
    truncated = len(data) < record_end

    masters: list[str] = []
    hedr_version: float | None = None
    num_records: int | None = None
    author: str | None = None
    description: str | None = None
    next_size: int | None = None
    pos = header_size
    while pos < end:
        if pos + 6 > end:
            if not truncated:
                raise PluginHeaderError("TES4 record ends inside a subrecord header")
            break
        sub_type = bytes(data[pos:pos + 4])
        (size,) = struct.unpack_from("<H", data, pos + 4)
        pos += 6
        if next_size is not None:
            size, next_size = next_size, None
        if pos + size > end:
            if pos + size > record_end:
                raise PluginHeaderError(f"subrecord {sub_type!r} runs past the end of the TES4 record")
            truncated = True
            break
        payload = data[pos:pos + size]
        pos += size
        if sub_type == b"XXXX":
            if size != 4:
                raise PluginHeaderError("malformed XXXX subrecord")
            (next_size,) = struct.unpack_from("<I", payload, 0)
        elif sub_type == b"HEDR":
            if size < 8:
                raise PluginHeaderError("HEDR subrecord is too short")
            version, num_records = struct.unpack_from("<fI", payload, 0)
            hedr_version = round(version, 4)
        elif sub_type == b"MAST":
            name = _zstring(payload)
            if name:
                masters.append(name)
        elif sub_type == b"CNAM":
            author = _zstring(payload) or None
        elif sub_type == b"SNAM":
            description = _zstring(payload) or None

    if hedr_version is None:
        raise PluginHeaderError("plugin header is truncated before HEDR")
    return PluginHeader(
        masters=tuple(masters),
        hedr_version=hedr_version,
        form_version=form_version,
        header_size=header_size,
        flags=flags,
        num_records=num_records,
        author=author,
        description=description,
        truncated=truncated,
    )


def _zstring(payload: bytes) -> str:
    return bytes(payload).split(b"\0", 1)[0].decode("cp1252", errors="replace").strip()


def read_plugin_header(path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES) -> PluginHeader:
    """Read and parse the header of the plugin file at *path*.

    Reads only what is needed: the fixed header, then ``min(data_size,
    max_bytes)`` bytes. Raises :class:`PluginHeaderError` for unreadable or
    invalid files (wrapping OSError).
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
            if len(head) >= 8 and head[:4] == b"TES4":
                (data_size,) = struct.unpack_from("<I", head, 4)
                want = min(24 + data_size, max(max_bytes, 24)) - len(head)
                if want > 0:
                    head += fh.read(want)
    except OSError as exc:
        raise PluginHeaderError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return parse_plugin_header(head)


@dataclass(frozen=True)
class GameVote:
    game_id: str
    weight: int
    reason: str


#: Vote weights shared with placement.identify_game.
WEIGHT_MAIN_MASTER = 10
WEIGHT_DLC_MASTER = 8
WEIGHT_HEADER_LAYOUT = 6
WEIGHT_HEDR_EXACT = 4
WEIGHT_HEDR_LOOSE = 2


def _hedr_matches(version: float, candidates: tuple[float, ...]) -> bool:
    return any(abs(version - c) < 0.005 for c in candidates)


def candidate_games(header: PluginHeader, plugin_name: str = "plugin") -> list[GameVote]:
    """Which games this plugin was made for, strongest evidence first.

    At most one vote per game (the strongest). Evidence, in order of strength:

    * a master that is an official game/DLC master (Fallout4.esm, Skyrim.esm,
      FalloutNV.esm ...). Skyrim.esm is shared by Skyrim LE and SE, so the
      record form version splits them (44 = SE, 40-43 = LE). A plugin with
      FalloutNV.esm as a master is New Vegas even if it also lists
      Fallout3.esm (Tale of Two Wastelands).
    * Creation Club masters by naming convention (ccBGSFO4*, cc*SSE*, SFBGS*).
    * a 20-byte record header -> Oblivion.
    * the HEDR version, narrowed by form version where the game's form
      versions are known (only when no master identified a game).
    """
    votes: dict[str, GameVote] = {}

    def vote(game_id: str, weight: int, reason: str) -> None:
        current = votes.get(game_id)
        if current is None or weight > current.weight:
            votes[game_id] = GameVote(game_id, weight, reason)

    form = header.form_version
    for master in header.masters:
        key = master.lower()
        games = OFFICIAL_MASTER_TO_GAMES.get(key, ())
        if not games:
            if key.startswith("ccbgsfo4"):
                games = ("fallout4",)
            elif key.startswith("cc") and "sse" in key:
                games = ("skyrimse",)
            elif key.startswith("sfbgs"):
                games = ("starfield",)
            else:
                continue
            weight = WEIGHT_DLC_MASTER
        else:
            weight = WEIGHT_MAIN_MASTER if any(GAMES[g].main_master.lower() == key for g in games) else WEIGHT_DLC_MASTER
        if len(games) > 1 and form is not None:
            narrowed = tuple(g for g in games if form in GAMES[g].form_versions)
            if narrowed:
                games = narrowed
        for game_id in games:
            detail = f" (form version {form})" if len(OFFICIAL_MASTER_TO_GAMES.get(key, ())) > 1 and form is not None else ""
            vote(game_id, weight, f"{plugin_name} requires {master}{detail}")

    if "falloutnv" in votes and "fallout3" in votes:
        del votes["fallout3"]

    if header.header_size == 20:
        vote("oblivion", WEIGHT_HEADER_LAYOUT, f"{plugin_name} uses Oblivion's 20-byte record header")

    # Header versions are weak evidence; only use them when no master names the game.
    if header.hedr_version is not None and not votes:
        for game in GAMES.values():
            if game.record_header_size != header.header_size:
                continue
            if not _hedr_matches(header.hedr_version, game.hedr_versions):
                continue
            if form is not None and game.form_versions:
                if form not in game.form_versions:
                    continue
                weight = WEIGHT_HEDR_EXACT
            else:
                weight = WEIGHT_HEDR_LOOSE
            vote(game.id, weight, f"{plugin_name} header version {header.hedr_version:.2f}"
                 + (f", form version {form}" if form is not None else ""))

    return sorted(votes.values(), key=lambda v: (-v.weight, v.game_id))
