from __future__ import annotations

import dataclasses
import random

import pytest

from bethmm.nexus import NexusInfo, parse_nexus_filename


@pytest.mark.parametrize(
    "filename, name, mod_id, version, timestamp",
    [
        ("Unofficial Fallout 4 Patch-4598-2-1-5-1680000000.zip",
         "Unofficial Fallout 4 Patch", 4598, "2.1.5", 1680000000),
        ("Unofficial Skyrim Special Edition Patch-266-4-3-0a-1702509399.7z",
         "Unofficial Skyrim Special Edition Patch", 266, "4.3.0a", 1702509399),
        ("Address Library for SKSE Plugins-32444-11-1704339489.zip",
         "Address Library for SKSE Plugins", 32444, "11", 1704339489),
        ("SkyUI_5_2_SE-12604-5-2SE.7z", "SkyUI 5 2 SE", 12604, "5.2SE", None),
        ("Better Dialogue Controls-1429-1-2.7z", "Better Dialogue Controls", 1429, "1.2", None),
        ("XP32 Maximum Skeleton Special Extended-1988-5-06-1700000000.7z",
         "XP32 Maximum Skeleton Special Extended", 1988, "5.06", 1700000000),
        # Names containing " - " and plain hyphens.
        ("Mod Name - Main File-1234-1-0a-1600000000 (1).7z", "Mod Name - Main File", 1234, "1.0a", 1600000000),
        ("Ordinator - Perks of Skyrim-1137-9-31-0-1600000000.7z", "Ordinator - Perks of Skyrim", 1137, "9.31.0", 1600000000),
        ("Hi-Res Textures-1234-1-0.ZIP", "Hi-Res Textures", 1234, "1.0", None),
        ("Engine Fixes - skse64 plugin-17230-6-2-0-1700000000.7z", "Engine Fixes - skse64 plugin", 17230, "6.2.0", 1700000000),
        # Digits in the name, including hyphen-separated ones.
        ("Skyrim 202X 10.0.1 - Architecture PART 1-2347-10-0-1-1622727919.7z",
         "Skyrim 202X 10.0.1 - Architecture PART 1", 2347, "10.0.1", 1622727919),
        ("Project-22-Remix-1234-1-0-1600000000.7z", "Project-22-Remix", 1234, "1.0", 1600000000),
        ("Project-2-Remix-1234-1-0.7z", "Project-2-Remix", 1234, "1.0", None),
        ("Fallout 4 HD Overhaul 2K-12345-1-0.rar", "Fallout 4 HD Overhaul 2K", 12345, "1.0", None),
        ("2077-1234-1-0.7z", "2077", 1234, "1.0", None),
        # A non-ASCII "digit" token is part of the name, not the ID.
        ("Mod-²-1234-1-0.7z", "Mod-²", 1234, "1.0", None),
        # Versions with letters, multi-digit parts and a leading "v".
        ("Mod-1234-v1-2-1600000000.7Z", "Mod", 1234, "v1.2", 1600000000),
        ("Mod-1234-10-2.7z", "Mod", 1234, "10.2", None),
        ("Mod-1234-1-0-beta-1600000000.rar", "Mod", 1234, "1.0.beta", 1600000000),
        ("Mod-1234-1.0-1600000000.zip", "Mod", 1234, "1.0", 1600000000),
        # Single-digit mod IDs still work when nothing better is there.
        ("Mod-5-1-0.7z", "Mod", 5, "1.0", None),
        # Timestamp but no version.
        ("Mod-1234-1600000000.zip", "Mod", 1234, None, 1600000000),
        # Browser duplicate suffixes, with and without a space.
        ("Static Mesh Improvement Mod-659-2-12-1585519183 (12).7z",
         "Static Mesh Improvement Mod", 659, "2.12", 1585519183),
        ("Mod Name-1234-1-0-1600000000(1).zip", "Mod Name", 1234, "1.0", 1600000000),
        # Underscored names become spaced; names with spaces keep underscores.
        ("Some_Mod_Name-4321-2-0.zip", "Some Mod Name", 4321, "2.0", None),
        ("Some_Mod With Spaces-4321-2-0.zip", "Some_Mod With Spaces", 4321, "2.0", None),
        ("Mod   Name -4321-2-0.zip", "Mod Name", 4321, "2.0", None),
    ],
)
def test_nexus_downloads(filename, name, mod_id, version, timestamp):
    info = parse_nexus_filename(filename)
    assert (info.display_name, info.mod_id, info.version, info.timestamp) == (name, mod_id, version, timestamp)
    assert info.is_nexus


@pytest.mark.parametrize(
    "filename, name, mod_id, version, timestamp",
    [
        # Versions that start with letters ("beta.2", "SE.1.0", "RC.1"): the
        # digits inside the version are not a single-digit mod ID.
        ("Mod-1234-beta-2-1600000000.7z", "Mod", 1234, "beta.2", 1600000000),
        ("Mod-12345-SE-1-0-1600000000.7z", "Mod", 12345, "SE.1.0", 1600000000),
        ("Cool Mod-12345-RC-1-1600000000.zip", "Cool Mod", 12345, "RC.1", 1600000000),
        ("Mod-12345-Alpha-3.7z", "Mod", 12345, "Alpha.3", None),
        # A version without digits is fine when the timestamp proves the format.
        ("Mod-1234-beta-1600000000.7z", "Mod", 1234, "beta", 1600000000),
        ("Some Mod-56789-Final-1600000000.7z", "Some Mod", 56789, "Final", 1600000000),
        # The earlier rules still hold.
        ("Project-22-Remix-1234-1-0-1600000000.7z", "Project-22-Remix", 1234, "1.0", 1600000000),
        ("Mod-1234-10-2-1600000000.7z", "Mod", 1234, "10.2", 1600000000),
        ("Mod-5-1-0-1600000000.7z", "Mod", 5, "1.0", 1600000000),
        # 9- and 11-digit trailing numbers are not timestamps.
        ("Mod-1234-1-0-999999999.7z", "Mod", 1234, "1.0.999999999", None),
        ("Mod-1234-1-0-16000000000.7z", "Mod", 1234, "1.0.16000000000", None),
    ],
)
def test_nexus_downloads_with_unusual_versions(filename, name, mod_id, version, timestamp):
    info = parse_nexus_filename(filename)
    assert (info.display_name, info.mod_id, info.version, info.timestamp) == (name, mod_id, version, timestamp)


@pytest.mark.parametrize(
    "filename",
    [
        "Mod-" + "1" * 5000 + "-1-0.7z",
        "X-" + "1" * 5000 + "-1600000000.7z",
        "Mod-1234-" + "9" * 5000 + ".7z",
    ],
    ids=["huge-id", "huge-id-before-timestamp", "huge-version"],
)
def test_huge_digit_runs_do_not_raise(filename):
    # int() refuses strings over 4300 digits; such a token is never a mod ID.
    info = parse_nexus_filename(filename)
    assert info.mod_id is None or info.mod_id < 10**9


def test_stem_drops_extension_and_duplicate_suffix_only():
    info = parse_nexus_filename("Mod Name - Main File-1234-1-0a-1600000000 (1).7z")
    assert info.stem == "Mod Name - Main File-1234-1-0a-1600000000"


@pytest.mark.parametrize(
    "path",
    [
        "/home/user/Downloads/SkyUI_5_2_SE-12604-5-2SE.7z",
        "C:\\Users\\me\\Downloads\\SkyUI_5_2_SE-12604-5-2SE.7z",
        "Z:\\home\\user\\Downloads\\SkyUI_5_2_SE-12604-5-2SE.7z",
        "../mods/SkyUI_5_2_SE-12604-5-2SE.7z",
        # An already extracted folder, with or without a trailing slash.
        "/home/user/.local/share/beth-mod-manager/tmp/SkyUI_5_2_SE-12604-5-2SE",
        "/home/user/.local/share/beth-mod-manager/tmp/SkyUI_5_2_SE-12604-5-2SE/",
        "  SkyUI_5_2_SE-12604-5-2SE.7z  ",
    ],
)
def test_paths_and_folders_use_the_last_component(path):
    assert parse_nexus_filename(path) == NexusInfo(
        display_name="SkyUI 5 2 SE",
        mod_id=12604,
        version="5.2SE",
        timestamp=None,
        stem="SkyUI_5_2_SE-12604-5-2SE",
    )


def test_hyphens_in_folder_names_of_the_path_are_irrelevant():
    info = parse_nexus_filename("/mnt/my-disk-2-1/Downloads/Mod-1234-1-0.7z")
    assert (info.display_name, info.mod_id, info.version) == ("Mod", 1234, "1.0")


@pytest.mark.parametrize(
    "filename, display, stem",
    [
        ("my_random_archive.zip", "my random archive", "my_random_archive"),
        ("Some Mod v1.2.zip", "Some Mod v1.2", "Some Mod v1.2"),
        ("Cool Mod-v2.7z", "Cool Mod-v2", "Cool Mod-v2"),
        ("skse64_2_02_06.7z", "skse64 2 02 06", "skse64_2_02_06"),
        ("2K Textures.7z", "2K Textures", "2K Textures"),
        # "-2" alone (no version, no timestamp) is not enough to call it Nexus.
        ("Textures-2.zip", "Textures-2", "Textures-2"),
        ("Patch-12345.zip", "Patch-12345", "Patch-12345"),
        # Without a timestamp a digit-less "version" is just part of the name.
        ("Patch-2-Final.zip", "Patch-2-Final", "Patch-2-Final"),
        # No name in front of the ID.
        ("-1234-1-0.7z", "-1234-1-0", "-1234-1-0"),
        ("2019-05-12 backup.zip", "2019-05-12 backup", "2019-05-12 backup"),
        ("Some-Name-1600000000.7z", "Some-Name-1600000000", "Some-Name-1600000000"),
        ("Mod (2).zip", "Mod", "Mod"),
        ("readme.txt", "readme.txt", "readme.txt"),
        ("My Mod Folder", "My Mod Folder", "My Mod Folder"),
        ("", "", ""),
        (".7z", ".7z", ".7z"),
        (".ZIP", ".ZIP", ".ZIP"),
        ("/", "", ""),
        # Non-ASCII digits are never mod IDs (and must not crash int()).
        ("Mod-²-1.7z", "Mod-²-1", "Mod-²-1"),
        ("Mod-١٢٣٤-1.7z", "Mod-١٢٣٤-1", "Mod-١٢٣٤-1"),
    ],
)
def test_non_nexus_names(filename, display, stem):
    info = parse_nexus_filename(filename)
    assert info == NexusInfo(display_name=display, mod_id=None, version=None, timestamp=None, stem=stem)
    assert not info.is_nexus


def test_nexus_info_is_frozen():
    info = parse_nexus_filename("Mod-1234-1-0.7z")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.mod_id = 1  # type: ignore[misc]


def test_never_raises_on_odd_strings():
    rng = random.Random(7)
    alphabet = "ab_ -.()0123456789²٣vVZIP7zr\\/\t"
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25)))
        info = parse_nexus_filename(text)
        assert isinstance(info.display_name, str)
        if info.mod_id is not None:
            assert info.mod_id >= 0
            assert info.display_name
