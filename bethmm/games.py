"""Static knowledge about the supported Bethesda games.

Nothing in here touches the filesystem; it is the single source of truth that
discovery, placement and (later) plugins.txt management read from.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PluginsTxtFormat(Enum):
    #: ``*Enabled.esp`` / ``Disabled.esp``; order in the file is the load order.
    ASTERISK = "asterisk"
    #: One active plugin per line; load order comes from file timestamps.
    PLAIN = "plain"


@dataclass(frozen=True)
class GameDef:
    id: str
    name: str
    #: Executables that identify the game root (matched case-insensitively).
    exe_names: tuple[str, ...]
    #: The game's own master, used to recognise mods made for it.
    main_master: str
    #: Base game + official DLC masters (lowercase comparisons are done by callers).
    official_masters: tuple[str, ...]
    #: Masters the engine loads without them being listed in plugins.txt.
    implicit_plugins: tuple[str, ...]
    #: Script extender short name, e.g. "F4SE"; also its folder name under Data.
    script_extender: str
    #: Script extender loader executable.
    se_loader: str
    #: Prefix of the script extender's runtime DLLs at the game root, e.g. "f4se_".
    se_dll_prefix: str
    #: Folder under ``AppData/Local`` holding plugins.txt, keyed by store.
    #: Key ``"default"`` is always present.
    appdata_folders: tuple[tuple[str, str], ...]
    plugins_format: PluginsTxtFormat
    #: True when load order is decided by plugin file modification times.
    timestamp_load_order: bool
    #: Bethesda archive extensions the game reads (".ba2" or ".bsa").
    archive_exts: tuple[str, ...]
    supports_esl: bool
    #: HEDR "version" floats written by this game's Creation Kit / construction set.
    hedr_versions: tuple[float, ...]
    #: Record-header form versions seen in this game's plugins (empty = unknown).
    form_versions: tuple[int, ...]
    #: Size of a record header: 20 for Oblivion, 24 for everything later.
    record_header_size: int
    nexus_domain: str
    data_dir_name: str = "Data"

    def appdata_folder(self, store: str = "default") -> str:
        folders = dict(self.appdata_folders)
        return folders.get(store, folders["default"])

    def appdata_folder_candidates(self, store: str = "default") -> list[str]:
        """All known plugins.txt folder names, the one for *store* first."""
        first = self.appdata_folder(store)
        rest = [f for _, f in self.appdata_folders if f != first]
        return [first] + list(dict.fromkeys(rest))


_FO4_DLC = (
    "DLCRobot.esm",
    "DLCworkshop01.esm",
    "DLCCoast.esm",
    "DLCworkshop02.esm",
    "DLCworkshop03.esm",
    "DLCNukaWorld.esm",
    "DLCUltraHighResolution.esm",
)
_SKYRIM_DLC = ("Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm")
_FNV_DLC = (
    "DeadMoney.esm",
    "HonestHearts.esm",
    "OldWorldBlues.esm",
    "LonesomeRoad.esm",
    "GunRunnersArsenal.esm",
    "CaravanPack.esm",
    "ClassicPack.esm",
    "MercenaryPack.esm",
    "TribalPack.esm",
)
_FO3_DLC = ("Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm", "PointLookout.esm", "Zeta.esm")
_OBLIVION_DLC = (
    "DLCShiveringIsles.esp",
    "Knights.esp",
    "DLCBattlehornCastle.esp",
    "DLCFrostcrag.esp",
    "DLCHorseArmor.esp",
    "DLCMehrunesRazor.esp",
    "DLCOrrery.esp",
    "DLCSpellTomes.esp",
    "DLCThievesDen.esp",
    "DLCVileLair.esp",
)
_STARFIELD_DLC = (
    "Constellation.esm",
    "OldMars.esm",
    "BlueprintShips-Starfield.esm",
    "SFBGS003.esm",
    "SFBGS004.esm",
    "SFBGS006.esm",
    "SFBGS007.esm",
    "SFBGS008.esm",
    "ShatteredSpace.esm",
)


GAMES: dict[str, GameDef] = {
    g.id: g
    for g in (
        GameDef(
            id="fallout4",
            name="Fallout 4",
            exe_names=("Fallout4.exe",),
            main_master="Fallout4.esm",
            official_masters=("Fallout4.esm",) + _FO4_DLC,
            implicit_plugins=("Fallout4.esm",) + _FO4_DLC,
            script_extender="F4SE",
            se_loader="f4se_loader.exe",
            se_dll_prefix="f4se_",
            appdata_folders=(("default", "Fallout4"),),
            plugins_format=PluginsTxtFormat.ASTERISK,
            timestamp_load_order=False,
            archive_exts=(".ba2",),
            supports_esl=True,
            hedr_versions=(0.95, 1.0),
            form_versions=(131,),
            record_header_size=24,
            nexus_domain="fallout4",
        ),
        GameDef(
            id="skyrimse",
            name="Skyrim Special Edition",
            exe_names=("SkyrimSE.exe",),
            main_master="Skyrim.esm",
            official_masters=("Skyrim.esm",) + _SKYRIM_DLC,
            implicit_plugins=("Skyrim.esm",) + _SKYRIM_DLC,
            script_extender="SKSE",
            se_loader="skse64_loader.exe",
            se_dll_prefix="skse64_",
            appdata_folders=(
                ("default", "Skyrim Special Edition"),
                ("gog", "Skyrim Special Edition GOG"),
                ("epic", "Skyrim Special Edition EPIC"),
                ("ms", "Skyrim Special Edition MS"),
            ),
            plugins_format=PluginsTxtFormat.ASTERISK,
            timestamp_load_order=False,
            archive_exts=(".bsa",),
            supports_esl=True,
            hedr_versions=(1.7, 1.71),
            form_versions=(44,),
            record_header_size=24,
            nexus_domain="skyrimspecialedition",
        ),
        GameDef(
            id="skyrim",
            name="Skyrim (Legendary Edition)",
            exe_names=("TESV.exe",),
            main_master="Skyrim.esm",
            official_masters=("Skyrim.esm",) + _SKYRIM_DLC,
            implicit_plugins=("Skyrim.esm", "Update.esm"),
            script_extender="SKSE",
            se_loader="skse_loader.exe",
            se_dll_prefix="skse_",
            appdata_folders=(("default", "Skyrim"),),
            plugins_format=PluginsTxtFormat.PLAIN,
            timestamp_load_order=True,
            archive_exts=(".bsa",),
            supports_esl=False,
            hedr_versions=(0.94, 1.7),
            form_versions=(40, 41, 42, 43),
            record_header_size=24,
            nexus_domain="skyrim",
        ),
        GameDef(
            id="falloutnv",
            name="Fallout: New Vegas",
            exe_names=("FalloutNV.exe",),
            main_master="FalloutNV.esm",
            official_masters=("FalloutNV.esm",) + _FNV_DLC,
            implicit_plugins=(),
            script_extender="NVSE",
            se_loader="nvse_loader.exe",
            se_dll_prefix="nvse_",
            appdata_folders=(("default", "FalloutNV"),),
            plugins_format=PluginsTxtFormat.PLAIN,
            timestamp_load_order=True,
            archive_exts=(".bsa",),
            supports_esl=False,
            hedr_versions=(1.32, 1.33, 1.34),
            form_versions=(),
            record_header_size=24,
            nexus_domain="newvegas",
        ),
        GameDef(
            id="fallout3",
            name="Fallout 3",
            exe_names=("Fallout3.exe",),
            main_master="Fallout3.esm",
            official_masters=("Fallout3.esm",) + _FO3_DLC,
            implicit_plugins=(),
            script_extender="FOSE",
            se_loader="fose_loader.exe",
            se_dll_prefix="fose_",
            appdata_folders=(("default", "Fallout3"),),
            plugins_format=PluginsTxtFormat.PLAIN,
            timestamp_load_order=True,
            archive_exts=(".bsa",),
            supports_esl=False,
            hedr_versions=(0.94,),
            form_versions=(),
            record_header_size=24,
            nexus_domain="fallout3",
        ),
        GameDef(
            id="oblivion",
            name="Oblivion",
            exe_names=("Oblivion.exe",),
            main_master="Oblivion.esm",
            official_masters=("Oblivion.esm",) + _OBLIVION_DLC,
            implicit_plugins=(),
            script_extender="OBSE",
            se_loader="obse_loader.exe",
            se_dll_prefix="obse_",
            appdata_folders=(("default", "Oblivion"),),
            plugins_format=PluginsTxtFormat.PLAIN,
            timestamp_load_order=True,
            archive_exts=(".bsa",),
            supports_esl=False,
            hedr_versions=(0.8, 1.0),
            form_versions=(),
            record_header_size=20,
            nexus_domain="oblivion",
        ),
        GameDef(
            id="starfield",
            name="Starfield",
            exe_names=("Starfield.exe",),
            main_master="Starfield.esm",
            official_masters=("Starfield.esm",) + _STARFIELD_DLC,
            implicit_plugins=("Starfield.esm",) + _STARFIELD_DLC,
            script_extender="SFSE",
            se_loader="sfse_loader.exe",
            se_dll_prefix="sfse_",
            appdata_folders=(("default", "Starfield"),),
            plugins_format=PluginsTxtFormat.ASTERISK,
            timestamp_load_order=False,
            archive_exts=(".ba2",),
            supports_esl=True,
            hedr_versions=(0.96,),
            form_versions=(),
            record_header_size=24,
            nexus_domain="starfield",
        ),
    )
}

#: Lowercased executable name -> game id.
EXE_TO_GAME: dict[str, str] = {
    exe.lower(): g.id for g in GAMES.values() for exe in g.exe_names
}

#: Lowercased script-extender loader name -> game id.
LOADER_TO_GAME: dict[str, str] = {g.se_loader.lower(): g.id for g in GAMES.values()}

#: Lowercased official master name -> game ids that ship it.
OFFICIAL_MASTER_TO_GAMES: dict[str, tuple[str, ...]] = {}
for _g in GAMES.values():
    for _m in _g.official_masters:
        OFFICIAL_MASTER_TO_GAMES[_m.lower()] = OFFICIAL_MASTER_TO_GAMES.get(_m.lower(), ()) + (_g.id,)
del _g, _m


def get_game(game_id: str) -> GameDef:
    try:
        return GAMES[game_id]
    except KeyError:
        raise KeyError(f"unknown game id {game_id!r}; known: {', '.join(GAMES)}") from None


#: Games whose mods are often interchangeable or mislabelled as each other.
#: Used to fall back to an installed sibling when a mod targets the other one.
SIBLINGS: dict[str, tuple[str, ...]] = {
    "skyrim": ("skyrimse",),
    "skyrimse": ("skyrim",),
}
