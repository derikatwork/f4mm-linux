from __future__ import annotations

import os
import random
import struct

import pytest

from helpers import make_plugin, subrecord

from bethmm.plugin_header import (
    FLAG_COMPRESSED,
    MAX_TES4_DATA_SIZE,
    WEIGHT_DLC_MASTER,
    WEIGHT_HEADER_LAYOUT,
    WEIGHT_HEDR_EXACT,
    WEIGHT_HEDR_LOOSE,
    WEIGHT_MAIN_MASTER,
    PluginHeader,
    PluginHeaderError,
    candidate_games,
    parse_plugin_header,
    read_plugin_header,
)


def games_of(data: bytes) -> list[str]:
    return [v.game_id for v in candidate_games(parse_plugin_header(data))]


def raw_record(body: bytes, *, flags: int = 0, form_version: int = 131, data_size: int | None = None) -> bytes:
    """A TES4 record with a hand-built subrecord body (24-byte header)."""
    size = len(body) if data_size is None else data_size
    return b"TES4" + struct.pack("<IIIIHH", size, flags, 0, 0, form_version, 0) + body


HEDR = subrecord(b"HEDR", struct.pack("<fII", 0.95, 7, 0x800))


# --------------------------------------------------------------------------
# Parsing: the happy path for every supported game


def test_parses_fallout4_plugin_fields():
    data = make_plugin(
        ["Fallout4.esm", "DLCRobot.esm"],
        hedr_version=1.0,
        form_version=131,
        flags=0x1,
        num_records=42,
        author="Jane",
        description="Adds things",
    )
    header = parse_plugin_header(data)
    assert header == PluginHeader(
        masters=("Fallout4.esm", "DLCRobot.esm"),
        hedr_version=1.0,
        form_version=131,
        header_size=24,
        flags=0x1,
        num_records=42,
        author="Jane",
        description="Adds things",
        truncated=False,
    )


def test_hedr_version_is_rounded_from_float32():
    # 0.95 and 1.71 are not exact in float32; callers compare against literals.
    assert parse_plugin_header(make_plugin(hedr_version=0.95)).hedr_version == 0.95
    assert parse_plugin_header(make_plugin(hedr_version=1.71, form_version=44)).hedr_version == 1.71
    assert parse_plugin_header(make_plugin(hedr_version=0.94, form_version=43)).hedr_version == 0.94


@pytest.mark.parametrize(
    "masters, hedr, form, header_size, expected",
    [
        (["Fallout4.esm"], 0.95, 131, 24, ["fallout4"]),
        (["Fallout4.esm", "DLCCoast.esm", "DLCNukaWorld.esm"], 1.0, 131, 24, ["fallout4"]),
        (["Skyrim.esm", "Update.esm"], 1.71, 44, 24, ["skyrimse"]),
        (["Skyrim.esm", "Update.esm", "Dawnguard.esm"], 1.7, 43, 24, ["skyrim"]),
        (["FalloutNV.esm", "DeadMoney.esm"], 1.34, 15, 24, ["falloutnv"]),
        (["Fallout3.esm", "Anchorage.esm", "BrokenSteel.esm"], 0.94, 15, 24, ["fallout3"]),
        (["Oblivion.esm", "DLCShiveringIsles.esp"], 1.0, 0, 20, ["oblivion"]),
        (["Starfield.esm", "Constellation.esm"], 0.96, 555, 24, ["starfield"]),
    ],
)
def test_identifies_each_game_from_its_masters(masters, hedr, form, header_size, expected):
    data = make_plugin(masters, hedr_version=hedr, form_version=form, header_size=header_size)
    header = parse_plugin_header(data)
    assert header.masters == tuple(masters)
    assert header.header_size == header_size
    votes = candidate_games(header)
    assert [v.game_id for v in votes] == expected
    assert votes[0].weight == WEIGHT_MAIN_MASTER


def test_oblivion_header_has_no_form_version():
    header = parse_plugin_header(make_plugin(["Oblivion.esm"], hedr_version=1.0, header_size=20))
    assert header.header_size == 20
    assert header.form_version is None
    assert header.hedr_version == 1.0
    assert header.masters == ("Oblivion.esm",)


def test_master_names_match_case_insensitively():
    assert games_of(make_plugin(["FALLOUT4.ESM"])) == ["fallout4"]
    assert games_of(make_plugin(["skyrim.esm"], hedr_version=1.71, form_version=44)) == ["skyrimse"]


def test_dlc_master_alone_votes_with_dlc_weight():
    votes = candidate_games(parse_plugin_header(make_plugin(["DLCCoast.esm"])))
    assert [(v.game_id, v.weight) for v in votes] == [("fallout4", WEIGHT_DLC_MASTER)]


def test_one_vote_per_game_keeps_the_strongest_reason():
    votes = candidate_games(parse_plugin_header(make_plugin(["DLCCoast.esm", "Fallout4.esm"])), "Cool.esp")
    assert len(votes) == 1
    assert votes[0].weight == WEIGHT_MAIN_MASTER
    assert votes[0].reason == "Cool.esp requires Fallout4.esm"


# --------------------------------------------------------------------------
# Skyrim LE vs SE


@pytest.mark.parametrize(
    "form, expected",
    [
        (44, ["skyrimse"]),
        (43, ["skyrim"]),
        (40, ["skyrim"]),
        # Form versions that belong to neither: both stay possible, equal weight.
        (0, ["skyrim", "skyrimse"]),
        (45, ["skyrim", "skyrimse"]),
        (131, ["skyrim", "skyrimse"]),
        (0xFFFF, ["skyrim", "skyrimse"]),
    ],
)
def test_skyrim_esm_is_split_by_form_version(form, expected):
    votes = candidate_games(parse_plugin_header(make_plugin(["Skyrim.esm"], hedr_version=1.71, form_version=form)))
    assert [v.game_id for v in votes] == expected
    assert {v.weight for v in votes} == {WEIGHT_MAIN_MASTER}
    assert all(f"form version {form}" in v.reason for v in votes)


def test_skyrim_dlc_masters_are_split_by_form_version_too():
    assert games_of(make_plugin(["Update.esm"], hedr_version=1.71, form_version=44)) == ["skyrimse"]
    assert games_of(make_plugin(["Dawnguard.esm"], hedr_version=0.94, form_version=43)) == ["skyrim"]


def test_se_creation_club_master_beats_le_form_version():
    # A form-43 plugin that needs an SE-only Creation Club master can only be
    # for Skyrim SE, whatever its form version says.
    data = make_plugin(["Skyrim.esm", "ccBGSSSE001-Fish.esm"], hedr_version=1.71, form_version=43)
    votes = candidate_games(parse_plugin_header(data))
    assert [(v.game_id, v.weight) for v in votes] == [("skyrimse", WEIGHT_MAIN_MASTER)]


# --------------------------------------------------------------------------
# Tale of Two Wastelands


@pytest.mark.parametrize(
    "masters",
    [
        ["FalloutNV.esm", "Fallout3.esm"],
        ["Fallout3.esm", "FalloutNV.esm", "Anchorage.esm", "ThePitt.esm"],
        ["DeadMoney.esm", "Fallout3.esm"],
    ],
)
def test_ttw_plugins_are_new_vegas_only(masters):
    votes = candidate_games(parse_plugin_header(make_plugin(masters, hedr_version=1.34, form_version=15)))
    assert [v.game_id for v in votes] == ["falloutnv"]


# --------------------------------------------------------------------------
# Creation Club


@pytest.mark.parametrize(
    "master, game",
    [
        ("ccBGSFO4044-HellfirePowerArmor.esl", "fallout4"),
        ("ccBGSFO4115-X02.esl", "fallout4"),
        # Non-Bethesda creators, shipped free with the Fallout 4 next-gen update.
        ("ccOTMFO4001-Remnants.esl", "fallout4"),
        ("ccFSVFO4007-Halloween.esl", "fallout4"),
        ("ccSBJFO4003-Grenade.esl", "fallout4"),
        ("ccZSEF04001-BHouse.esm", "fallout4"),  # really spelled with a zero
        ("ccBGSSSE001-Fish.esm", "skyrimse"),
        ("ccQDRSSE001-SurvivalMode.esl", "skyrimse"),
        ("ccBGSSSE025-AdvDSGS.esm", "skyrimse"),
        ("CCEDHSSE002-SPLKNTSET.ESL", "skyrimse"),
        ("SFBGS003.esm", "starfield"),
        ("SFBGS00A.esm", "starfield"),
    ],
)
def test_creation_club_masters_identify_the_game(master, game):
    data = make_plugin([master], hedr_version=1.71, form_version=44)
    votes = candidate_games(parse_plugin_header(data))
    assert votes[0].game_id == game
    assert votes[0].weight == WEIGHT_DLC_MASTER
    assert master in votes[0].reason


@pytest.mark.parametrize("master", ["ccAssets.esm", "ccMissedSSEPatches.esp", "CCOR.esp", "cc.esp", "sse001.esm"])
def test_names_that_only_resemble_creation_club_are_ignored(master):
    # No master evidence, so the header version (Fallout 4's) decides.
    votes = candidate_games(parse_plugin_header(make_plugin([master], hedr_version=0.95, form_version=131)))
    assert [(v.game_id, v.weight) for v in votes] == [("fallout4", WEIGHT_HEDR_EXACT)]


# --------------------------------------------------------------------------
# Master-less plugins: header layout and HEDR version


@pytest.mark.parametrize(
    "hedr, form, expected",
    [
        (0.95, 131, [("fallout4", WEIGHT_HEDR_EXACT)]),
        (1.0, 131, [("fallout4", WEIGHT_HEDR_EXACT)]),
        (1.71, 44, [("skyrimse", WEIGHT_HEDR_EXACT)]),
        (1.7, 44, [("skyrimse", WEIGHT_HEDR_EXACT)]),
        (1.7, 43, [("skyrim", WEIGHT_HEDR_EXACT)]),
        (0.94, 43, [("skyrim", WEIGHT_HEDR_EXACT), ("fallout3", WEIGHT_HEDR_LOOSE)]),
        (0.94, 15, [("fallout3", WEIGHT_HEDR_LOOSE)]),
        (1.34, 15, [("falloutnv", WEIGHT_HEDR_LOOSE)]),
        (1.32, 15, [("falloutnv", WEIGHT_HEDR_LOOSE)]),
        (0.96, 555, [("starfield", WEIGHT_HEDR_LOOSE)]),
        (2.5, 131, []),
        (0.95, 44, []),
    ],
)
def test_masterless_plugins_fall_back_to_hedr(hedr, form, expected):
    votes = candidate_games(parse_plugin_header(make_plugin([], hedr_version=hedr, form_version=form)))
    assert [(v.game_id, v.weight) for v in votes] == expected


def test_hedr_reason_mentions_versions():
    votes = candidate_games(parse_plugin_header(make_plugin([], hedr_version=1.71, form_version=44)), "Thing.esp")
    assert votes[0].reason == "Thing.esp header version 1.71, form version 44"


def test_masterless_oblivion_plugin_is_identified_by_layout():
    votes = candidate_games(parse_plugin_header(make_plugin([], hedr_version=0.8, header_size=20)), "x.esp")
    assert [(v.game_id, v.weight) for v in votes] == [("oblivion", WEIGHT_HEADER_LAYOUT)]
    assert "20-byte" in votes[0].reason


def test_hedr_votes_are_suppressed_when_a_master_names_the_game():
    # Skyrim SE header values, but the plugin needs Fallout4.esm.
    data = make_plugin(["Fallout4.esm"], hedr_version=1.71, form_version=44)
    assert [(v.game_id, v.weight) for v in candidate_games(parse_plugin_header(data))] == [
        ("fallout4", WEIGHT_MAIN_MASTER)
    ]


def test_unknown_masters_do_not_suppress_hedr_votes():
    data = make_plugin(["SomeFramework.esm", "OtherMod.esp"], hedr_version=1.71, form_version=44)
    assert [(v.game_id, v.weight) for v in candidate_games(parse_plugin_header(data))] == [
        ("skyrimse", WEIGHT_HEDR_EXACT)
    ]


def test_conflicting_masters_give_several_votes_strongest_first():
    data = make_plugin(["DLCCoast.esm", "Starfield.esm"], hedr_version=0.95)
    votes = candidate_games(parse_plugin_header(data))
    assert [(v.game_id, v.weight) for v in votes] == [
        ("starfield", WEIGHT_MAIN_MASTER),
        ("fallout4", WEIGHT_DLC_MASTER),
    ]


def test_nan_hedr_version_does_not_break_identification():
    data = make_plugin([], hedr_version=float("nan"))
    header = parse_plugin_header(data)
    assert header.hedr_version != header.hedr_version  # NaN
    assert candidate_games(header) == []


# --------------------------------------------------------------------------
# Strings, XXXX and other subrecord details


def test_large_subrecords_use_xxxx():
    description = "d" * 70_000
    data = make_plugin(["Fallout4.esm", "DLCRobot.esm"], description=description)
    assert b"XXXX" in data
    header = parse_plugin_header(data)
    assert header.description == description
    assert header.masters == ("Fallout4.esm", "DLCRobot.esm")
    assert header.truncated is False


def test_unknown_large_subrecord_is_skipped():
    blob = subrecord(b"ONAM", b"\x01" * 100_000)
    data = make_plugin(["Fallout4.esm"], extra=blob + subrecord(b"MAST", b"Late.esm\0"))
    assert parse_plugin_header(data).masters == ("Fallout4.esm", "Late.esm")


def test_malformed_xxxx_is_rejected():
    body = HEDR + b"XXXX" + struct.pack("<H", 2) + b"\0\0"
    with pytest.raises(PluginHeaderError, match="XXXX"):
        parse_plugin_header(raw_record(body))


def test_cp1252_strings_are_decoded():
    masters = ["Ünofficial Pätch.esm", "Café – “Déjà vu”.esp"]
    data = make_plugin(masters, author="Jérôme", description="Ça marche — très bien")
    header = parse_plugin_header(data)
    assert header.masters == tuple(masters)
    assert header.author == "Jérôme"
    assert header.description == "Ça marche — très bien"


def test_bytes_undefined_in_cp1252_are_replaced_not_fatal():
    data = make_plugin(["Fallout4.esm"], extra=subrecord(b"MAST", b"Odd\x81Name.esp\0"))
    assert parse_plugin_header(data).masters == ("Fallout4.esm", "Odd�Name.esp")


def test_mast_without_nul_terminator():
    data = make_plugin([], extra=subrecord(b"MAST", b"Fallout4.esm") + subrecord(b"DATA", b"\0" * 8))
    header = parse_plugin_header(data)
    assert header.masters == ("Fallout4.esm",)
    assert games_of(data) == ["fallout4"]


def test_empty_master_names_and_strings_are_ignored():
    data = make_plugin(["Fallout4.esm"], author="", extra=subrecord(b"MAST", b"\0") + subrecord(b"MAST", b"  \0"))
    header = parse_plugin_header(data)
    assert header.masters == ("Fallout4.esm",)
    assert header.author is None
    assert header.description is None


def test_text_after_nul_and_surrounding_spaces_are_dropped():
    data = make_plugin([], extra=subrecord(b"MAST", b" Fallout4.esm \0garbage"))
    assert parse_plugin_header(data).masters == ("Fallout4.esm",)


def test_bytes_after_the_record_are_ignored():
    record = make_plugin(["Fallout4.esm"])
    grup = b"GRUP" + struct.pack("<I", 24) + b"\xff" * 16 + b"MAST" + b"\x10\x00"
    header = parse_plugin_header(record + grup)
    assert header.masters == ("Fallout4.esm",)
    assert header.truncated is False


def test_accepts_bytearray():
    data = bytearray(make_plugin(["Fallout4.esm"]))
    assert parse_plugin_header(data).masters == ("Fallout4.esm",)


# --------------------------------------------------------------------------
# Flags


@pytest.mark.parametrize(
    "flags, master, light",
    [
        (0x0, False, False),
        (0x1, True, False),
        (0x200, False, True),
        (0x201, True, True),
        (0x80, False, False),  # localized
    ],
)
def test_flag_properties(flags, master, light):
    header = parse_plugin_header(make_plugin(["Fallout4.esm"], flags=flags))
    assert header.flags == flags
    assert header.is_master_flag is master
    assert header.is_light_flag is light


def test_compressed_flag_is_rejected():
    with pytest.raises(PluginHeaderError, match="compressed"):
        parse_plugin_header(make_plugin(["Fallout4.esm"], flags=FLAG_COMPRESSED | 0x1))


# --------------------------------------------------------------------------
# Truncated buffers


def test_buffer_cut_inside_masters_keeps_complete_ones():
    data = make_plugin(["Fallout4.esm", "DLCRobot.esm", "DLCCoast.esm"])
    cut = data.index(b"DLCCoast") + 3
    header = parse_plugin_header(data[:cut])
    assert header.truncated is True
    assert header.masters == ("Fallout4.esm", "DLCRobot.esm")


def test_buffer_cut_right_after_hedr():
    data = make_plugin(["Fallout4.esm"])
    header = parse_plugin_header(data[: 24 + 6 + 12])
    assert header.truncated is True
    assert header.hedr_version == 0.95
    assert header.masters == ()
    assert header.author is None


def test_every_cut_point_is_an_error_or_a_truncated_prefix():
    full_masters = ("Fallout4.esm", "DLCRobot.esm", "DLCCoast.esm")
    data = make_plugin(full_masters, description="x" * 300)
    hedr_end = 24 + 6 + 12
    for cut in range(len(data)):
        if cut < hedr_end:
            with pytest.raises(PluginHeaderError):
                parse_plugin_header(data[:cut])
            continue
        header = parse_plugin_header(data[:cut])
        assert header.truncated is True, cut
        assert full_masters[: len(header.masters)] == header.masters, cut
    assert parse_plugin_header(data).truncated is False


def test_oblivion_buffer_cut_after_hedr_is_truncated():
    data = make_plugin(["Oblivion.esm"], hedr_version=1.0, header_size=20)
    header = parse_plugin_header(data[: 20 + 6 + 12])
    assert header.truncated is True
    assert header.header_size == 20


@pytest.mark.parametrize("length", [0, 3, 4, 8, 20, 23, 25, 26, 27, 28, 30, 41])
def test_incomplete_header_is_an_error(length):
    data = make_plugin(["Fallout4.esm"])
    with pytest.raises(PluginHeaderError, match="small|truncated"):
        parse_plugin_header(data[:length])


# --------------------------------------------------------------------------
# Things that are not TES4 plugins


def test_morrowind_plugin_is_rejected():
    data = make_plugin(["Morrowind.esm"], magic=b"TES3")
    with pytest.raises(PluginHeaderError, match="Morrowind"):
        parse_plugin_header(data)


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04" + b"\0" * 60,  # a zip file
        b"7z\xbc\xaf\x27\x1c" + b"\0" * 60,
        b"BSA\x00" + b"\0" * 60,
        b"BTDX" + b"\0" * 60,
        b"tes4" + make_plugin()[4:],  # magic is case-sensitive
        b"hello world, this is not a plugin at all",
    ],
)
def test_other_files_are_rejected(data):
    with pytest.raises(PluginHeaderError, match="not a plugin"):
        parse_plugin_header(data)


@pytest.mark.parametrize("seed", range(20))
def test_random_garbage_is_rejected(seed):
    rng = random.Random(seed)
    data = rng.randbytes(rng.randint(0, 4096))
    with pytest.raises(PluginHeaderError):
        parse_plugin_header(data)


@pytest.mark.parametrize("seed", range(5))
def test_random_garbage_behind_tes4_magic_is_rejected_cleanly(seed):
    rng = random.Random(seed)
    data = b"TES4" + rng.randbytes(rng.randint(0, 4096))
    with pytest.raises(PluginHeaderError):
        parse_plugin_header(data)


def test_mutated_plugins_never_raise_anything_else():
    rng = random.Random(1234)
    base = make_plugin(["Skyrim.esm", "Update.esm", "ccBGSSSE001-Fish.esm"], description="desc",
                       hedr_version=1.71, form_version=44)
    for _ in range(3000):
        data = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.3:
            del data[rng.randint(0, len(data)):]
        try:
            header = parse_plugin_header(bytes(data))
        except PluginHeaderError:
            continue
        candidate_games(header)


def test_record_without_hedr_is_rejected():
    body = subrecord(b"CNAM", b"me\0") + HEDR
    with pytest.raises(PluginHeaderError, match="HEDR"):
        parse_plugin_header(raw_record(body))


def test_empty_record_is_rejected():
    data = raw_record(HEDR, data_size=0)
    with pytest.raises(PluginHeaderError):
        parse_plugin_header(data)


def test_unbelievable_record_size_is_rejected():
    data = raw_record(HEDR, data_size=MAX_TES4_DATA_SIZE + 1)
    with pytest.raises(PluginHeaderError, match="not believable"):
        parse_plugin_header(data)


def test_hedr_too_short_is_rejected():
    body = subrecord(b"HEDR", b"\0\0\0\0")
    with pytest.raises(PluginHeaderError, match="HEDR"):
        parse_plugin_header(raw_record(body))


def test_subrecord_running_past_the_record_is_rejected():
    bad = b"MAST" + struct.pack("<H", 500) + b"Fallout4.esm\0"
    with pytest.raises(PluginHeaderError, match="runs past"):
        parse_plugin_header(raw_record(HEDR + bad))


def test_subrecord_running_past_the_record_is_rejected_even_in_a_short_buffer():
    # The record claims 100 bytes, the buffer holds fewer, and the MAST size
    # points beyond the record's end: corrupt, not merely truncated.
    bad = b"MAST" + struct.pack("<H", 500) + b"Fallout4.esm\0"
    data = raw_record(HEDR + bad, data_size=100)
    with pytest.raises(PluginHeaderError, match="runs past"):
        parse_plugin_header(data)


def test_hedr_size_past_the_record_is_rejected():
    data = raw_record(HEDR, data_size=10)
    with pytest.raises(PluginHeaderError, match="runs past"):
        parse_plugin_header(data)


def test_record_ending_inside_a_subrecord_header_is_rejected():
    with pytest.raises(PluginHeaderError, match="inside a subrecord header"):
        parse_plugin_header(make_plugin(["Fallout4.esm"], extra=b"MA"))


def test_error_is_a_value_error():
    assert issubclass(PluginHeaderError, ValueError)


# --------------------------------------------------------------------------
# read_plugin_header


def test_read_plugin_header_from_file(tmp_path):
    data = make_plugin(["Fallout4.esm", "DLCCoast.esm"], flags=0x201, author="Me")
    path = tmp_path / "Cool Mod.esp"
    path.write_bytes(data + b"GRUP" + b"\xff" * 5000)
    header = read_plugin_header(path)
    assert header == parse_plugin_header(data)
    assert header.truncated is False
    assert read_plugin_header(str(path)) == header


def test_read_plugin_header_leaves_the_file_alone(tmp_path):
    data = make_plugin(["Fallout4.esm"])
    path = tmp_path / "x.esp"
    path.write_bytes(data)
    os.utime(path, (1_000_000_000, 1_000_000_000))
    read_plugin_header(path)
    assert path.read_bytes() == data
    assert path.stat().st_mtime == 1_000_000_000


def test_read_oblivion_plugin(tmp_path):
    path = tmp_path / "Old.esp"
    path.write_bytes(make_plugin(["Oblivion.esm"], hedr_version=0.8, header_size=20))
    header = read_plugin_header(path)
    assert header.header_size == 20
    assert header.masters == ("Oblivion.esm",)
    assert header.truncated is False


def test_read_plugin_header_respects_max_bytes(tmp_path):
    data = make_plugin(["Fallout4.esm"], description="d" * 70_000)
    path = tmp_path / "Big.esp"
    path.write_bytes(data)

    short = read_plugin_header(path, max_bytes=1000)
    assert short.truncated is True
    assert short.masters == ()  # MAST comes after the huge description
    assert short.hedr_version == 0.95

    full = read_plugin_header(path)
    assert full.truncated is False
    assert full.masters == ("Fallout4.esm",)


def test_read_plugin_header_max_bytes_too_small_for_hedr(tmp_path):
    path = tmp_path / "x.esp"
    path.write_bytes(make_plugin(["Fallout4.esm"]))
    with pytest.raises(PluginHeaderError):
        read_plugin_header(path, max_bytes=30)


def test_read_truncated_file(tmp_path):
    data = make_plugin(["Fallout4.esm", "DLCRobot.esm"])
    path = tmp_path / "Cut.esp"
    path.write_bytes(data[: data.index(b"DLCRobot")])
    header = read_plugin_header(path)
    assert header.truncated is True
    assert header.masters == ("Fallout4.esm",)


def test_read_missing_file(tmp_path):
    missing = tmp_path / "nope.esp"
    with pytest.raises(PluginHeaderError, match="nope.esp"):
        read_plugin_header(missing)


def test_read_directory(tmp_path):
    folder = tmp_path / "Folder.esp"
    folder.mkdir()
    with pytest.raises(PluginHeaderError):
        read_plugin_header(folder)


def test_read_broken_symlink(tmp_path):
    link = tmp_path / "Dangling.esp"
    link.symlink_to(tmp_path / "gone.esp")
    with pytest.raises(PluginHeaderError):
        read_plugin_header(link)


def test_read_empty_file(tmp_path):
    path = tmp_path / "Empty.esp"
    path.write_bytes(b"")
    with pytest.raises(PluginHeaderError):
        read_plugin_header(path)


def test_read_non_plugin_file(tmp_path):
    path = tmp_path / "Fake.esp"
    path.write_bytes(b"PK\x03\x04" + b"\0" * 100)
    with pytest.raises(PluginHeaderError, match="not a plugin"):
        read_plugin_header(path)


def test_read_morrowind_file(tmp_path):
    path = tmp_path / "Morrowind.esm"
    path.write_bytes(make_plugin([], magic=b"TES3"))
    with pytest.raises(PluginHeaderError, match="TES3"):
        read_plugin_header(path)
