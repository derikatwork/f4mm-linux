from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bethmm.placement import (
    Confidence,
    FileMapping,
    PlacementPlan,
    RootCandidate,
    Rule,
    Target,
    analyze_directory,
    analyze_files,
    identify_game,
    plan_for_roots,
)
from bethmm.plugin_header import PluginHeader
from helpers import make_plugin, make_tree


def hdr(*masters: str, form: int | None = 131, size: int = 24, hedr: float = 1.0) -> PluginHeader:
    return PluginHeader(
        masters=tuple(masters),
        hedr_version=hedr,
        form_version=None if size == 20 else form,
        header_size=size,
        flags=0,
    )


FO4 = hdr("Fallout4.esm", form=131, hedr=1.0)
SSE = hdr("Skyrim.esm", "Update.esm", form=44, hedr=1.71)
SLE = hdr("Skyrim.esm", form=43, hedr=0.94)


def reader_for(headers: dict[str, PluginHeader]):
    return headers.get


def dests(plan: PlacementPlan) -> dict[str, str]:
    return {m.source: m.dest for m in plan.mappings}


def assert_partition(plan: PlacementPlan, files: list[str]) -> None:
    """Every input file is either deployed or skipped, never both."""
    sources = [m.source for m in plan.mappings]
    assert len(sources) == len(set(sources))
    assert set(sources).isdisjoint(plan.skipped)
    assert set(sources) | set(plan.skipped) == set(files)


# --- input normalisation ---------------------------------------------------


def test_backslashes_dot_slash_duplicates_and_empty_entries():
    plan = analyze_files([
        ".\\MyMod.esp",
        "./textures\\a.dds",
        "/textures/a.dds",
        "textures//a.dds",
        "",
        "   ",
        "textures/",
        "./",
    ])
    assert dests(plan) == {"MyMod.esp": "Data/MyMod.esp", "textures/a.dds": "Data/textures/a.dds"}
    assert plan.skipped == []
    assert plan.warnings == []


def test_parent_components_are_dropped_with_a_warning():
    plan = analyze_files(["MyMod.esp", "../../evil.dll", "textures/../../x.dds"])
    assert dests(plan) == {"MyMod.esp": "Data/MyMod.esp"}
    assert len(plan.warnings) == 2
    assert all("unsafe" in w for w in plan.warnings)


def test_no_files_is_unknown():
    plan = analyze_files([])
    assert plan.target is Target.UNKNOWN
    assert plan.rule is Rule.UNKNOWN
    assert plan.confidence is Confidence.NONE
    assert plan.mappings == [] and plan.skipped == [] and plan.candidate_roots == []


# --- rule 1: wrapper folders -----------------------------------------------


def test_wrapper_folder():
    files = ["CoolMod v1.2/CoolMod.esp", "CoolMod v1.2/CoolMod - Main.ba2", "CoolMod v1.2/textures/a.dds"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["CoolMod v1.2"]
    assert plan.root == "CoolMod v1.2"
    assert plan.target is Target.DATA
    assert plan.rule is Rule.DATA_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {
        "CoolMod v1.2/CoolMod.esp": "Data/CoolMod.esp",
        "CoolMod v1.2/CoolMod - Main.ba2": "Data/CoolMod - Main.ba2",
        "CoolMod v1.2/textures/a.dds": "Data/textures/a.dds",
    }
    assert any("CoolMod v1.2" in r for r in plan.reasons)


def test_nested_wrappers():
    files = ["Outer/Inner v2/Data/x.esp", "Outer/Inner v2/Data/Meshes/a.nif", "Outer/readme.txt"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["Outer", "Inner v2"]
    assert plan.root == "Outer/Inner v2"
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.target is Target.GAME_ROOT
    assert dests(plan) == {
        "Outer/Inner v2/Data/x.esp": "Data/x.esp",
        "Outer/Inner v2/Data/Meshes/a.nif": "Data/Meshes/a.nif",
    }
    assert plan.skipped == ["Outer/readme.txt"]


def test_wrapper_next_to_docs_images_and_junk():
    files = [
        "readme.txt",
        "Changelog.MD",
        "screenshot.JPG",
        "Thumbs.db",
        "Docs/manual.pdf",
        "__MACOSX/CoolMod/._CoolMod.esp",
        "CoolMod/CoolMod.esp",
    ]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["CoolMod"]
    assert dests(plan) == {"CoolMod/CoolMod.esp": "Data/CoolMod.esp"}
    assert sorted(plan.skipped) == sorted(f for f in files if f != "CoolMod/CoolMod.esp")
    assert_partition(plan, files)


def test_no_wrapper_descent_when_another_real_file_sits_next_to_the_folder():
    files = ["tweak.ini", "CoolMod/textures/a.dds"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == []
    assert plan.target is Target.UNKNOWN
    assert plan.candidate_roots == [RootCandidate("CoolMod", Target.DATA, Rule.DATA_CONTENT)]


@pytest.mark.parametrize("top", ["Textures", "TEXTURES", "meshes", "Interface", "F4SE", "SKSE", "MCM", "Sound"])
def test_single_known_data_folder_is_not_a_wrapper(top):
    files = [f"{top}/sub/a.dds", f"{top}/b.dds"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == []
    assert plan.root == ""
    assert plan.target is Target.DATA
    assert plan.rule is Rule.DATA_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {f: f"Data/{f}" for f in files}


def test_texture_only_mod():
    files = ["Textures/Armor/a.dds", "Textures/Armor/a_n.dds"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == []
    assert dests(plan) == {
        "Textures/Armor/a.dds": "Data/Textures/Armor/a.dds",
        "Textures/Armor/a_n.dds": "Data/Textures/Armor/a_n.dds",
    }


def test_single_game_root_folder_is_not_a_wrapper():
    plan = analyze_files(["enbseries/effect.fx", "enbseries/enbsunsprite.fx"])
    assert plan.wrapper_dirs == []
    assert plan.target is Target.GAME_ROOT
    assert plan.rule is Rule.GAME_ROOT_CONTENT


# --- rule 3: Data folder ---------------------------------------------------


@pytest.mark.parametrize("data", ["Data", "data", "DATA"])
def test_data_folder_layout(data):
    files = [f"{data}/MyMod.esp", f"{data}/Textures/a.dds", f"{data}/Scripts/a.pex"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == []  # Data is recognised, not a wrapper
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.target is Target.GAME_ROOT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {f: f for f in files}
    assert plan.warnings == []


def test_data_folder_with_loose_plugins_next_to_it_is_medium():
    files = ["Data/a.esp", "b.esp", "meshes/x.nif"]
    plan = analyze_files(files)
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.confidence is Confidence.MEDIUM
    assert dests(plan) == {f: f for f in files}
    assert len(plan.warnings) == 1
    assert "b.esp" in plan.warnings[0] and "meshes/" in plan.warnings[0]


def test_data_folder_with_unrecognised_folder_is_medium():
    plan = analyze_files(["Data/a.esp", "Tools/thing.exe"])
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.confidence is Confidence.MEDIUM
    assert any("Tools/" in w for w in plan.warnings)


def test_src_is_only_skipped_when_a_loader_is_present():
    plan = analyze_files(["Data/a.esp", "src/main.cpp"])
    assert "src/main.cpp" in dests(plan)
    assert plan.confidence is Confidence.MEDIUM

    plan = analyze_files(["Data/a.esp", "src/main.cpp", "obse_loader.exe"])
    assert "src/main.cpp" in plan.skipped
    assert plan.confidence is Confidence.HIGH


# --- rule 4: Data content --------------------------------------------------


def test_loose_plugins_and_archives():
    files = ["MyMod.esp", "MyMod - Main.ba2", "MyMod - Textures.BA2", "MyMod.esl", "readme.txt"]
    plan = analyze_files(files)
    assert plan.target is Target.DATA
    assert plan.rule is Rule.DATA_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {f: f"Data/{f}" for f in files if f != "readme.txt"}
    assert plan.skipped == ["readme.txt"]


def test_loose_bsa_only():
    plan = analyze_files(["Thing.bsa"])
    assert plan.target is Target.DATA
    assert dests(plan) == {"Thing.bsa": "Data/Thing.bsa"}


def test_unrecognised_folder_next_to_plugins_is_medium():
    plan = analyze_files(["MyMod.esp", "Extras/notes.ini"])
    assert plan.target is Target.DATA
    assert plan.confidence is Confidence.MEDIUM
    assert any("Extras/" in w for w in plan.warnings)
    assert dests(plan)["Extras/notes.ini"] == "Data/Extras/notes.ini"


def test_mixed_signals_rule_4_wins_with_low_confidence():
    files = ["MyMod.esp", "textures/a.dds", "d3d11.dll", "enbseries/x.fx", "enblocal.ini"]
    plan = analyze_files(files)
    assert plan.target is Target.DATA
    assert plan.rule is Rule.DATA_CONTENT
    assert plan.confidence is Confidence.LOW
    assert dests(plan)["d3d11.dll"] == "Data/d3d11.dll"
    assert len(plan.warnings) == 1
    for name in ("d3d11.dll", "enbseries/", "enblocal.ini"):
        assert name in plan.warnings[0]


def test_docs_only_skipped_at_the_root():
    files = [
        "MyMod.esp",
        "MyMod readme.txt",
        "preview.png",
        "Docs/manual.pdf",
        "Screenshots/1.png",
        "Interface/Translations/MyMod_english.txt",
        "textures/readme.txt",
    ]
    plan = analyze_files(files)
    assert dests(plan) == {
        "MyMod.esp": "Data/MyMod.esp",
        "Interface/Translations/MyMod_english.txt": "Data/Interface/Translations/MyMod_english.txt",
        "textures/readme.txt": "Data/textures/readme.txt",
    }
    assert sorted(plan.skipped) == sorted(["MyMod readme.txt", "preview.png", "Docs/manual.pdf", "Screenshots/1.png"])
    assert plan.confidence is Confidence.HIGH  # doc folders are not "unrecognised"


def test_junk_files_are_skipped_everywhere():
    files = [
        "__MACOSX/CoolMod/._CoolMod.esp",
        "__MACOSX/CoolMod/textures/._a.dds",
        "CoolMod/CoolMod.esp",
        "CoolMod/Thumbs.db",
        "CoolMod/.DS_Store",
        "CoolMod/textures/a.dds",
        "CoolMod/textures/.DS_Store",
        "CoolMod/textures/Desktop.ini",
    ]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["CoolMod"]
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {
        "CoolMod/CoolMod.esp": "Data/CoolMod.esp",
        "CoolMod/textures/a.dds": "Data/textures/a.dds",
    }
    assert_partition(plan, files)


def test_case_variant_duplicates_keep_one_mapping_and_warn():
    files = ["Textures/a.dds", "textures/a.dds", "x.esp"]
    plan = analyze_files(files)
    assert [m.dest.lower() for m in plan.mappings].count("data/textures/a.dds") == 1
    assert any("both go to" in w for w in plan.warnings)
    assert_partition(plan, files)


# --- rule 5: game-root content ---------------------------------------------


def test_enb_binaries():
    base = "enbseries_skyrimse_v0503"
    files = [
        f"{base}/readme_en.txt",
        f"{base}/license_en.txt",
        f"{base}/WrapperVersion/d3d11.dll",
        f"{base}/WrapperVersion/d3dcompiler_46e.dll",
        f"{base}/WrapperVersion/enbseries.ini",
        f"{base}/WrapperVersion/enblocal.ini",
        f"{base}/WrapperVersion/enbpalette.bmp",
        f"{base}/WrapperVersion/enbseries/enbbloom.fx.ini",
        f"{base}/WrapperVersion/enbseries/effect.txt",
    ]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == [base, "WrapperVersion"]
    assert plan.root == f"{base}/WrapperVersion"
    assert plan.target is Target.GAME_ROOT
    assert plan.rule is Rule.GAME_ROOT_CONTENT
    assert plan.confidence is Confidence.HIGH
    d = dests(plan)
    assert d[f"{base}/WrapperVersion/d3d11.dll"] == "d3d11.dll"
    assert d[f"{base}/WrapperVersion/d3dcompiler_46e.dll"] == "d3dcompiler_46e.dll"
    assert d[f"{base}/WrapperVersion/enbseries.ini"] == "enbseries.ini"
    assert d[f"{base}/WrapperVersion/enblocal.ini"] == "enblocal.ini"
    assert d[f"{base}/WrapperVersion/enbpalette.bmp"] == "enbpalette.bmp"  # an image, but a game-root file
    assert d[f"{base}/WrapperVersion/enbseries/effect.txt"] == "enbseries/effect.txt"
    assert sorted(plan.skipped) == [f"{base}/license_en.txt", f"{base}/readme_en.txt"]
    assert plan.game.game_id is None


def test_enb_preset_files_only():
    plan = analyze_files(["enbseries.ini", "enblocal.ini", "enbseries/enbbloom.fx"])
    assert plan.target is Target.GAME_ROOT
    assert plan.confidence is Confidence.HIGH


def test_reshade():
    files = ["dxgi.dll", "ReShade.ini", "MyPreset.ini", "reshade-shaders/Shaders/x.fx", "reshade-shaders/Textures/y.png"]
    plan = analyze_files(files)
    assert plan.target is Target.GAME_ROOT
    assert plan.rule is Rule.GAME_ROOT_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {f: f for f in files}


def test_asi_plugin():
    plan = analyze_files(["dinput8.dll", "scripts/x.asi"])
    # "scripts" is also a Data folder name, so rule 4 wins, flagged as mixed
    assert plan.target is Target.DATA
    assert plan.confidence is Confidence.LOW
    plan = analyze_files(["MyFix.asi", "MyFix.ini"])
    assert plan.target is Target.GAME_ROOT


# --- script extenders ------------------------------------------------------


SKSE64 = [
    "skse64_2_02_06/skse64_loader.exe",
    "skse64_2_02_06/skse64_1_6_1170.dll",
    "skse64_2_02_06/skse64_readme.txt",
    "skse64_2_02_06/skse64_whatsnew.txt",
    "skse64_2_02_06/Data/Scripts/Actor.pex",
    "skse64_2_02_06/Data/Scripts/Form.pex",
    "skse64_2_02_06/Data/Scripts/Source/Actor.psc",
    "skse64_2_02_06/src/skse64/skse64/PluginAPI.h",
    "skse64_2_02_06/src/common/IPrefix.h",
]


def test_skse64_archive():
    plan = analyze_files(SKSE64)
    assert plan.wrapper_dirs == ["skse64_2_02_06"]
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.target is Target.GAME_ROOT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {
        "skse64_2_02_06/skse64_loader.exe": "skse64_loader.exe",
        "skse64_2_02_06/skse64_1_6_1170.dll": "skse64_1_6_1170.dll",
        "skse64_2_02_06/Data/Scripts/Actor.pex": "Data/Scripts/Actor.pex",
        "skse64_2_02_06/Data/Scripts/Form.pex": "Data/Scripts/Form.pex",
        "skse64_2_02_06/Data/Scripts/Source/Actor.psc": "Data/Scripts/Source/Actor.psc",
    }
    assert sorted(plan.skipped) == sorted([
        "skse64_2_02_06/skse64_readme.txt",
        "skse64_2_02_06/skse64_whatsnew.txt",
        "skse64_2_02_06/src/skse64/skse64/PluginAPI.h",
        "skse64_2_02_06/src/common/IPrefix.h",
    ])
    assert plan.game.game_id == "skyrimse"
    assert plan.game.confidence is Confidence.MEDIUM
    assert any("skse64_loader.exe" in r for r in plan.game.reasons)
    assert_partition(plan, SKSE64)


def test_f4se_archive():
    files = [
        "f4se_0_06_23/f4se_loader.exe",
        "f4se_0_06_23/f4se_1_10_163.dll",
        "f4se_0_06_23/f4se_steam_loader.dll",
        "f4se_0_06_23/f4se_readme.txt",
        "f4se_0_06_23/f4se_whatsnew.txt",
        "f4se_0_06_23/Data/Scripts/Actor.pex",
        "f4se_0_06_23/Data/Scripts/Source/User/Actor.psc",
        "f4se_0_06_23/src/f4se/f4se/PluginAPI.h",
    ]
    plan = analyze_files(files)
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.confidence is Confidence.HIGH
    assert dests(plan)["f4se_0_06_23/f4se_steam_loader.dll"] == "f4se_steam_loader.dll"
    assert "f4se_0_06_23/src/f4se/f4se/PluginAPI.h" in plan.skipped
    assert plan.game.game_id == "fallout4"
    assert plan.game.confidence is Confidence.MEDIUM
    assert plan.game.candidates == ["fallout4"]


def test_xnvse_flat_archive():
    files = [
        "nvse_loader.exe",
        "nvse_1_4.dll",
        "nvse_editor_1_4.dll",
        "nvse_steam_loader.dll",
        "xNVSE readme.txt",
        "src/nvse/nvse/PluginAPI.h",
    ]
    plan = analyze_files(files)
    assert plan.rule is Rule.GAME_ROOT_CONTENT
    assert plan.target is Target.GAME_ROOT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {f: f for f in files[:4]}
    assert sorted(plan.skipped) == sorted(files[4:])
    assert plan.game.game_id == "falloutnv"
    assert plan.game.confidence is Confidence.MEDIUM


def test_xnvse_with_data_folder():
    files = ["xNVSE 6.3.5/nvse_loader.exe", "xNVSE 6.3.5/nvse_1_4.dll", "xNVSE 6.3.5/Data/NVSE/Plugins/x.dll"]
    plan = analyze_files(files)
    assert plan.rule is Rule.DATA_FOLDER
    assert dests(plan)["xNVSE 6.3.5/Data/NVSE/Plugins/x.dll"] == "Data/NVSE/Plugins/x.dll"
    assert plan.game.game_id == "falloutnv"


# --- rule 6: unknown -------------------------------------------------------


def test_unknown_option_folders_without_candidates():
    files = ["Option A/x.dds", "Option B/x.dds"]
    plan = analyze_files(files)
    assert plan.target is Target.UNKNOWN
    assert plan.rule is Rule.UNKNOWN
    assert plan.confidence is Confidence.NONE
    assert plan.mappings == []
    assert plan.candidate_roots == []
    assert plan.skipped == []
    assert plan.reasons


def test_unknown_with_data_folder_candidates():
    files = ["00 Core/Data/Core.esp", "00 Core/Data/textures/a.dds", "01 Optional/Data/Opt.esp", "readme.txt"]
    plan = analyze_files(files)
    assert plan.target is Target.UNKNOWN
    assert plan.mappings == []
    assert plan.candidate_roots == [
        RootCandidate("00 Core", Target.GAME_ROOT, Rule.DATA_FOLDER),
        RootCandidate("01 Optional", Target.GAME_ROOT, Rule.DATA_FOLDER),
    ]
    assert plan.skipped == ["readme.txt"]


def test_unknown_candidates_shallowest_first_and_not_nested():
    files = [
        "A/deep/er/x.esp",
        "B/textures/x.dds",
        "B/textures/sub/meshes/y.nif",
        "C/d3d11.dll",
        "__MACOSX/A/._x",
        "Docs/textures/z.dds",
    ]
    plan = analyze_files(files)
    assert plan.candidate_roots == [
        RootCandidate("B", Target.DATA, Rule.DATA_CONTENT),
        RootCandidate("C", Target.GAME_ROOT, Rule.GAME_ROOT_CONTENT),
        RootCandidate("A/deep/er", Target.DATA, Rule.DATA_CONTENT),
    ]


def test_candidate_search_is_bounded():
    plan = analyze_files(["a/b/c/d/x.esp", "z/notes.ini"])
    assert [c.path for c in plan.candidate_roots] == ["a/b/c/d"]
    plan = analyze_files(["a/b/c/d/e/x.esp", "z/notes.ini"])
    assert plan.candidate_roots == []


def test_unknown_still_identifies_the_game():
    files = ["Main/Data/Main.esp", "Alt/Data/Alt.esp"]
    plan = analyze_files(files, header_reader=reader_for({"Main/Data/Main.esp": FO4, "Alt/Data/Alt.esp": FO4}))
    assert plan.target is Target.UNKNOWN
    assert plan.game.game_id == "fallout4"
    assert plan.game.confidence is Confidence.HIGH


# --- rule 2: FOMOD ---------------------------------------------------------


FOMOD = [
    "fomod/info.xml",
    "fomod/ModuleConfig.xml",
    "fomod/images/cover.png",
    "00 Core/MyMod.esp",
    "00 Core/MyMod - Main.ba2",
    "01 Option A/Textures/a.dds",
    "02 Option B/textures/a.dds",
    "02 Option B/F4SE/Plugins/b.dll",
    "Images/preview.png",
]


def test_fomod_archive():
    plan = analyze_files(FOMOD, header_reader=reader_for({"00 Core/MyMod.esp": FO4}))
    assert plan.rule is Rule.FOMOD
    assert plan.target is Target.UNKNOWN
    assert plan.is_fomod
    assert plan.fomod_config == "fomod/ModuleConfig.xml"
    assert plan.mappings == []
    assert plan.candidate_roots == [
        RootCandidate("00 Core", Target.DATA, Rule.DATA_CONTENT),
        RootCandidate("01 Option A", Target.DATA, Rule.DATA_CONTENT),
        RootCandidate("02 Option B", Target.DATA, Rule.DATA_CONTENT),
    ]
    assert {"fomod/info.xml", "fomod/ModuleConfig.xml", "fomod/images/cover.png"} <= set(plan.skipped)
    assert "00 Core/MyMod.esp" not in plan.skipped
    assert plan.game.game_id == "fallout4"
    assert plan.game.confidence is Confidence.HIGH


def test_fomod_detection_is_case_insensitive_and_inside_wrappers():
    plan = analyze_files(["My Mod 1.0/FOMOD/moduleconfig.xml", "My Mod 1.0/Core/x.esp", "readme.txt"])
    assert plan.wrapper_dirs == ["My Mod 1.0"]
    assert plan.rule is Rule.FOMOD
    assert plan.fomod_config == "My Mod 1.0/FOMOD/moduleconfig.xml"
    assert [c.path for c in plan.candidate_roots] == ["My Mod 1.0/Core"]


def test_fomod_wins_over_data_folder():
    plan = analyze_files(["fomod/ModuleConfig.xml", "Data/x.esp"])
    assert plan.rule is Rule.FOMOD
    assert plan.mappings == []


def test_fomod_with_only_info_xml_is_not_an_installer():
    files = ["fomod/info.xml", "fomod/images/cover.png", "MyMod.esp", "textures/a.dds"]
    plan = analyze_files(files)
    assert not plan.is_fomod
    assert plan.rule is Rule.DATA_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {"MyMod.esp": "Data/MyMod.esp", "textures/a.dds": "Data/textures/a.dds"}
    assert sorted(plan.skipped) == ["fomod/images/cover.png", "fomod/info.xml"]


def test_fomod_info_only_with_data_folder():
    plan = analyze_files(["fomod/info.xml", "Data/x.esp"])
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.skipped == ["fomod/info.xml"]


# --- plan_for_roots ---------------------------------------------------------


OPTIONS = [
    "Option A/Textures/x.dds",
    "Option A/only_a.dds",
    "Option B/textures/X.dds",
    "Option B/readme.txt",
    "readme.txt",
]


def test_plan_for_single_root():
    plan = plan_for_roots(OPTIONS, ["Option A"], Target.DATA)
    assert plan.rule is Rule.MANUAL
    assert plan.confidence is Confidence.HIGH
    assert plan.target is Target.DATA
    assert plan.root == "Option A"
    assert dests(plan) == {
        "Option A/Textures/x.dds": "Data/Textures/x.dds",
        "Option A/only_a.dds": "Data/only_a.dds",
    }
    assert_partition(plan, OPTIONS)
    assert plan.warnings == []


def test_plan_for_multiple_roots_later_root_wins():
    plan = plan_for_roots(OPTIONS, ["Option A", "Option B"], Target.DATA)
    assert plan.root == ""
    assert dests(plan) == {
        "Option A/only_a.dds": "Data/only_a.dds",
        "Option B/textures/X.dds": "Data/textures/X.dds",
    }
    assert "Option A/Textures/x.dds" in plan.skipped
    assert "Option B/readme.txt" in plan.skipped
    assert len(plan.warnings) == 1
    assert "Option B/textures/X.dds" in plan.warnings[0] and "Option A/Textures/x.dds" in plan.warnings[0]
    assert_partition(plan, OPTIONS)

    plan = plan_for_roots(OPTIONS, ["Option B", "Option A"], Target.DATA)
    assert dests(plan)["Option A/Textures/x.dds"] == "Data/Textures/x.dds"
    assert "Option B/textures/X.dds" in plan.skipped


def test_plan_for_roots_auto_detects_each_root():
    files = ["00 Core/Data/Core.esp", "01 Optional/textures/y.dds", "02 ENB/d3d11.dll"]
    plan = plan_for_roots(files, ["00 Core", "01 Optional", "02 ENB"])
    assert dests(plan) == {
        "00 Core/Data/Core.esp": "Data/Core.esp",
        "01 Optional/textures/y.dds": "Data/textures/y.dds",
        "02 ENB/d3d11.dll": "d3d11.dll",
    }
    assert plan.target is Target.GAME_ROOT  # mixed targets: destinations span the game folder
    assert plan.rule is Rule.MANUAL

    plan = plan_for_roots(files, ["01 Optional"])
    assert plan.target is Target.DATA


def test_plan_for_roots_top_level_and_game_root_target():
    files = ["weird.cfg", "sub/thing.bin", "notes.txt"]
    plan = plan_for_roots(files, [""], Target.GAME_ROOT)
    assert plan.root == ""
    assert dests(plan) == {"weird.cfg": "weird.cfg", "sub/thing.bin": "sub/thing.bin"}
    assert plan.skipped == ["notes.txt"]


def test_plan_for_roots_accepts_backslashes_and_other_case():
    plan = plan_for_roots(["Mods\\Option A/x.dds"], ["mods\\option a"], Target.DATA)
    assert dests(plan) == {"Mods/Option A/x.dds": "Data/x.dds"}
    assert plan.root == "Mods/Option A"


def test_plan_for_roots_skips_script_extender_source():
    files = ["SE/skse_loader.exe", "SE/skse_1_9_32.dll", "SE/src/x.cpp", "SE/Data/Scripts/a.pex"]
    plan = plan_for_roots(files, ["SE"])
    assert plan.target is Target.GAME_ROOT
    assert "SE/src/x.cpp" in plan.skipped
    assert plan.game.game_id == "skyrim"


def test_plan_for_roots_identifies_game_and_parses_name():
    plan = plan_for_roots(
        ["Main/x.esp"], ["Main"], Target.DATA,
        header_reader=reader_for({"Main/x.esp": SSE}),
        archive_name="Cool Mod-1234-1-2-1700000000.7z",
    )
    assert plan.game.game_id == "skyrimse"
    assert plan.nexus is not None and plan.nexus.mod_id == 1234 and plan.nexus.version == "1.2"


def test_plan_for_roots_errors():
    with pytest.raises(ValueError):
        plan_for_roots(OPTIONS, ["Option A"], Target.UNKNOWN)
    with pytest.raises(ValueError, match="Option C"):
        plan_for_roots(OPTIONS, ["Option C"], Target.DATA)
    with pytest.raises(ValueError):
        plan_for_roots(OPTIONS, ["Option A/only_a.dds"], Target.DATA)  # a file, not a folder
    with pytest.raises(ValueError):
        plan_for_roots(OPTIONS, ["../etc"], Target.DATA)
    with pytest.raises(ValueError):
        plan_for_roots([], [""], Target.DATA)
    with pytest.raises(ValueError):
        plan_for_roots(OPTIONS, [], Target.DATA)
    with pytest.raises(ValueError, match="Option A"):
        plan_for_roots(["Option A/x.dds"], ["Option A"])  # can't auto-detect


# --- identify_game ----------------------------------------------------------


def plugin_mappings(*names: str) -> list[FileMapping]:
    return [FileMapping(n, f"Data/{n}") for n in names]


@pytest.mark.parametrize(
    "header, game_id",
    [
        (FO4, "fallout4"),
        (hdr("Fallout4.esm", "DLCCoast.esm", form=131), "fallout4"),
        (SSE, "skyrimse"),
        (SLE, "skyrim"),
        (hdr("FalloutNV.esm", form=15, hedr=1.34), "falloutnv"),
        (hdr("FalloutNV.esm", "Fallout3.esm", form=15, hedr=1.34), "falloutnv"),  # TTW
        (hdr("Fallout3.esm", form=15, hedr=0.94), "fallout3"),
        (hdr("Oblivion.esm", size=20, hedr=1.0), "oblivion"),
        (hdr("Starfield.esm", form=555, hedr=0.96), "starfield"),
    ],
)
def test_identify_each_game_from_masters(header, game_id):
    guess = identify_game(plugin_mappings("a.esp"), header_reader=reader_for({"a.esp": header}))
    assert guess.game_id == game_id
    assert guess.confidence is Confidence.HIGH
    assert guess.candidates[0] == game_id
    assert guess.reasons


@pytest.mark.parametrize(
    "loader, game_id",
    [
        ("f4se_loader.exe", "fallout4"),
        ("skse64_loader.exe", "skyrimse"),
        ("skse_loader.exe", "skyrim"),
        ("nvse_loader.exe", "falloutnv"),
        ("fose_loader.exe", "fallout3"),
        ("obse_loader.exe", "oblivion"),
        ("SFSE_Loader.EXE", "starfield"),
    ],
)
def test_identify_from_loader_name(loader, game_id):
    guess = identify_game([FileMapping(f"x/{loader}", loader)])
    assert guess.game_id == game_id
    assert guess.confidence is Confidence.MEDIUM
    assert guess.candidates == [game_id]


@pytest.mark.parametrize(
    "dll, game_id",
    [
        ("skse64_1_6_1170.dll", "skyrimse"),
        ("skse64_steam_loader.dll", "skyrimse"),
        ("skse_1_9_32.dll", "skyrim"),
        ("f4se_1_10_163.dll", "fallout4"),
        ("nvse_1_4.dll", "falloutnv"),
        ("fose_1_7.dll", "fallout3"),
        ("obse_1_2_416.dll", "oblivion"),
        ("sfse_1_14_70.dll", "starfield"),
    ],
)
def test_identify_from_runtime_dll_at_game_root(dll, game_id):
    guess = identify_game([FileMapping(dll, dll)])
    assert guess.game_id == game_id
    assert guess.candidates == [game_id]
    assert guess.confidence is Confidence.MEDIUM


def test_runtime_dll_prefix_only_counts_at_game_root():
    guess = identify_game([FileMapping("skse64_1_6_1170.dll", "Data/skse64_1_6_1170.dll")])
    assert guess.game_id is None
    guess = identify_game([FileMapping("d3d11.dll", "d3d11.dll")])
    assert guess.game_id is None
    assert guess.confidence is Confidence.NONE


@pytest.mark.parametrize(
    "folder, game_id",
    [("F4SE", "fallout4"), ("nvse", "falloutnv"), ("FOSE", "fallout3"), ("OBSE", "oblivion"), ("SFSE", "starfield")],
)
def test_identify_from_script_extender_folder(folder, game_id):
    guess = identify_game([FileMapping(f"{folder}/Plugins/x.dll", f"Data/{folder}/Plugins/x.dll")])
    assert guess.game_id == game_id
    assert guess.confidence is Confidence.MEDIUM


def test_skse_folder_ambiguity_resolved_by_installed_games():
    mappings = [FileMapping("SKSE/Plugins/x.dll", "Data/SKSE/Plugins/x.dll")]

    guess = identify_game(mappings, installed_games=["skyrim"])
    assert guess.game_id == "skyrim"
    assert guess.confidence is Confidence.MEDIUM
    assert set(guess.candidates) == {"skyrim", "skyrimse"}
    assert any("installed" in r for r in guess.reasons)

    guess = identify_game(mappings, installed_games=["skyrimse", "fallout4"])
    assert guess.game_id == "skyrimse"
    assert guess.confidence is Confidence.MEDIUM

    guess = identify_game(mappings)
    assert guess.game_id in {"skyrim", "skyrimse"}
    assert guess.confidence is Confidence.LOW
    assert set(guess.candidates) == {"skyrim", "skyrimse"}

    guess = identify_game(mappings, installed_games=["skyrim", "skyrimse"])
    assert guess.confidence is Confidence.LOW


def test_sibling_fallback_le_plugin_only_se_installed():
    mappings = plugin_mappings("OldMod.esp")
    reader = reader_for({"OldMod.esp": SLE})

    guess = identify_game(mappings, header_reader=reader, installed_games=["skyrimse"])
    assert guess.game_id == "skyrimse"
    assert guess.confidence is Confidence.MEDIUM
    assert guess.candidates == ["skyrimse", "skyrim"]
    assert any("not installed" in r for r in guess.reasons)

    guess = identify_game(mappings, header_reader=reader, installed_games=["skyrim", "skyrimse"])
    assert guess.game_id == "skyrim"
    assert guess.confidence is Confidence.HIGH

    guess = identify_game(mappings, header_reader=reader, installed_games=["fallout4"])
    assert guess.game_id == "skyrim"
    assert guess.confidence is Confidence.HIGH


def test_conflicting_masters_are_low():
    mappings = plugin_mappings("FO4Mod.esp", "SSEMod.esp")
    reader = reader_for({"FO4Mod.esp": FO4, "SSEMod.esp": SSE})
    guess = identify_game(mappings, header_reader=reader)
    assert guess.confidence is Confidence.LOW
    assert set(guess.candidates) == {"fallout4", "skyrimse"}
    assert any("different games" in r for r in guess.reasons)

    guess = identify_game(mappings, header_reader=reader, installed_games=["skyrimse"])
    assert guess.game_id == "skyrimse"
    assert guess.confidence is Confidence.LOW


def test_conflicting_masters_in_an_archive():
    files = ["FO4Mod.esp", "SSEMod.esp", "textures/a.dds"]
    plan = analyze_files(files, header_reader=reader_for({"FO4Mod.esp": FO4, "SSEMod.esp": SSE}))
    assert plan.target is Target.DATA
    assert plan.game.confidence is Confidence.LOW


def test_shared_master_with_unknown_form_version_needs_tie_break():
    header = hdr("Skyrim.esm", form=99, hedr=1.7)
    mappings = plugin_mappings("x.esp")
    guess = identify_game(mappings, header_reader=reader_for({"x.esp": header}), installed_games=["skyrimse"])
    assert guess.game_id == "skyrimse"
    assert guess.confidence is Confidence.MEDIUM
    guess = identify_game(mappings, header_reader=reader_for({"x.esp": header}))
    assert guess.confidence is Confidence.LOW


def test_master_plus_loader_evidence_stays_high():
    mappings = plugin_mappings("x.esp") + [FileMapping("skse64_loader.exe", "skse64_loader.exe")]
    guess = identify_game(mappings, header_reader=reader_for({"x.esp": SSE}))
    assert guess.game_id == "skyrimse"
    assert guess.confidence is Confidence.HIGH


def test_only_installed_game_fallback():
    guess = identify_game([FileMapping("x.dds", "Data/textures/x.dds")], installed_games=["fallout4"])
    assert guess.game_id == "fallout4"
    assert guess.confidence is Confidence.LOW
    assert guess.reasons == ["only installed game"]

    guess = identify_game([], installed_games=["fallout4", "skyrimse"])
    assert guess.game_id is None
    assert guess.confidence is Confidence.NONE
    assert guess.candidates == []

    guess = identify_game([], installed_games=["fallout4", "not-a-game"])
    assert guess.game_id == "fallout4"


def test_weak_evidence_only():
    guess = identify_game(plugin_mappings("x - Main.ba2"))
    assert guess.confidence is Confidence.LOW
    assert set(guess.candidates) == {"fallout4", "starfield"}

    guess = identify_game(plugin_mappings("x - Main.ba2"), installed_games=["fallout4"])
    assert guess.game_id == "fallout4"
    assert guess.confidence is Confidence.LOW

    guess = identify_game(plugin_mappings("x.esl"))
    assert guess.confidence is Confidence.LOW
    assert set(guess.candidates) == {"fallout4", "skyrimse", "starfield"}

    guess = identify_game(plugin_mappings("x.bsa"), installed_games=["oblivion"])
    assert guess.game_id == "oblivion"
    assert guess.confidence is Confidence.LOW
    assert "fallout4" not in guess.candidates


def test_weak_evidence_breaks_ties_between_strong_votes():
    # A Skyrim.esm plugin of unknown form plus an .esl: only SE supports .esl.
    header = hdr("Skyrim.esm", form=99, hedr=1.7)
    guess = identify_game(plugin_mappings("x.esp", "y.esl"), header_reader=reader_for({"x.esp": header}))
    assert guess.game_id == "skyrimse"
    assert guess.candidates[:2] == ["skyrimse", "skyrim"]


def test_header_only_evidence_is_medium():
    header = hdr(form=131, hedr=0.95)  # no masters
    guess = identify_game(plugin_mappings("Standalone.esm"), header_reader=reader_for({"Standalone.esm": header}))
    assert guess.game_id == "fallout4"
    assert guess.confidence is Confidence.MEDIUM


def test_bad_header_readers_are_tolerated():
    def boom(source):
        raise ValueError("broken")

    assert identify_game(plugin_mappings("x.esp"), header_reader=boom).game_id is None
    assert identify_game(plugin_mappings("x.esp"), header_reader=None).game_id is None
    assert identify_game(plugin_mappings("x.esp"), header_reader=lambda s: None).game_id is None


def test_many_plugins_keep_reasons_short():
    names = [f"p{i}.esp" for i in range(30)]
    guess = identify_game(plugin_mappings(*names), header_reader=lambda s: FO4)
    assert guess.game_id == "fallout4"
    assert guess.confidence is Confidence.HIGH
    assert len(guess.reasons) <= 8


def test_header_reader_receives_source_paths():
    seen = []

    def reader(source):
        seen.append(source)
        return FO4

    plan = analyze_files(["Wrap/x.esp", "Wrap/textures/a.dds"], header_reader=reader)
    assert seen == ["Wrap/x.esp"]
    assert plan.game.game_id == "fallout4"


# --- nexus names and serialisation ------------------------------------------


def test_nexus_info_from_archive_name():
    plan = analyze_files(["x.esp"], archive_name="Unofficial Fallout 4 Patch-4598-2-1-5-1680000000.zip")
    assert plan.nexus is not None
    assert plan.nexus.display_name == "Unofficial Fallout 4 Patch"
    assert plan.nexus.mod_id == 4598
    assert plan.nexus.version == "2.1.5"
    assert plan.nexus.timestamp == 1680000000

    assert analyze_files(["x.esp"]).nexus is None
    plan = analyze_files(["x.esp"], archive_name="my_random_archive.zip")
    assert plan.nexus is not None and plan.nexus.mod_id is None


def test_to_dict_is_json_serialisable():
    plan = analyze_files(
        FOMOD,
        header_reader=reader_for({"00 Core/MyMod.esp": FO4}),
        archive_name="Cool Mod-1234-1-2-1700000000.7z",
    )
    data = json.loads(json.dumps(plan.to_dict()))
    assert data["target"] == "unknown"
    assert data["rule"] == "fomod"
    assert data["confidence"] == Confidence.HIGH.value
    assert data["is_fomod"] is True
    assert data["fomod_config"] == "fomod/ModuleConfig.xml"
    assert data["candidate_roots"][0] == {"path": "00 Core", "target": "data", "rule": "data_content"}
    assert data["game"]["game_id"] == "fallout4"
    assert data["game"]["confidence"] == Confidence.HIGH.value
    assert data["nexus"]["mod_id"] == 1234

    plan = analyze_files(SKSE64)
    data = json.loads(json.dumps(plan.to_dict()))
    assert data["target"] == "game_root"
    assert data["wrapper_dirs"] == ["skse64_2_02_06"]
    assert {"source": "skse64_2_02_06/skse64_loader.exe", "dest": "skse64_loader.exe"} in data["mappings"]
    assert data["nexus"] is None

    assert json.dumps(analyze_files([]).to_dict())


def test_confidence_ordering():
    assert Confidence.NONE < Confidence.LOW < Confidence.MEDIUM < Confidence.HIGH
    assert max([Confidence.LOW, Confidence.HIGH, Confidence.MEDIUM]) is Confidence.HIGH


# --- analyze_directory -------------------------------------------------------


def snapshot(base: Path) -> dict[str, tuple[bool, int, int]]:
    out = {}
    for root, dirs, files in os.walk(base):
        for name in dirs + files:
            p = Path(root, name)
            st = p.lstat()
            out[str(p.relative_to(base))] = (p.is_symlink(), st.st_size, st.st_mtime_ns)
    return out


def test_analyze_directory(tmp_path):
    outside = make_tree(tmp_path / "outside", {"secret.esp": make_plugin(["Skyrim.esm"], form_version=44)})
    mod = make_tree(tmp_path / "mod", {
        "Cool Mod v1/CoolMod.esp": make_plugin(["Fallout4.esm", "DLCCoast.esm"]),
        "Cool Mod v1/CoolMod - Main.ba2": b"BTDX",
        "Cool Mod v1/textures/a.dds": b"DDS ",
        "Cool Mod v1/readme.txt": "hello",
        "Cool Mod v1/Broken.esp": b"not a plugin",
    })
    os.symlink(outside / "secret.esp", mod / "Cool Mod v1" / "Linked.esp")
    os.symlink(outside, mod / "Cool Mod v1" / "meshes")
    before = snapshot(tmp_path)

    plan = analyze_directory(mod, archive_name="Cool Mod-1234-1-0-1700000000.7z")

    assert snapshot(tmp_path) == before
    assert plan.wrapper_dirs == ["Cool Mod v1"]
    assert plan.target is Target.DATA
    assert dests(plan) == {
        "Cool Mod v1/CoolMod.esp": "Data/CoolMod.esp",
        "Cool Mod v1/CoolMod - Main.ba2": "Data/CoolMod - Main.ba2",
        "Cool Mod v1/textures/a.dds": "Data/textures/a.dds",
        "Cool Mod v1/Broken.esp": "Data/Broken.esp",
    }
    assert plan.skipped == ["Cool Mod v1/readme.txt"]
    assert plan.game.game_id == "fallout4"
    assert plan.game.confidence is Confidence.HIGH
    symlink_warnings = [w for w in plan.warnings if "symlink" in w]
    assert len(symlink_warnings) == 2
    assert any("Linked.esp" in w for w in symlink_warnings)
    assert any("meshes" in w for w in symlink_warnings)
    assert all("secret" not in m.source for m in plan.mappings)
    assert plan.nexus is not None and plan.nexus.mod_id == 1234


def test_analyze_directory_reads_oblivion_and_skyrim_headers(tmp_path):
    mod = make_tree(tmp_path / "ob", {"Data/x.esp": make_plugin(["Oblivion.esm"], header_size=20, hedr_version=1.0)})
    plan = analyze_directory(mod)
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.game.game_id == "oblivion"

    mod = make_tree(tmp_path / "le", {"x.esp": make_plugin(["Skyrim.esm"], form_version=43, hedr_version=0.94)})
    plan = analyze_directory(mod, installed_games=["skyrimse"])
    assert plan.game.game_id == "skyrimse"
    assert plan.game.confidence is Confidence.MEDIUM


def test_analyze_directory_empty_and_missing(tmp_path):
    (tmp_path / "empty").mkdir()
    plan = analyze_directory(tmp_path / "empty")
    assert plan.target is Target.UNKNOWN
    assert plan.mappings == []
    with pytest.raises(NotADirectoryError):
        analyze_directory(tmp_path / "nope")
    (tmp_path / "file.txt").write_text("x")
    with pytest.raises(NotADirectoryError):
        analyze_directory(tmp_path / "file.txt")


def test_analyze_directory_does_not_follow_symlinked_root_content(tmp_path):
    outside = make_tree(tmp_path / "outside", {"Data/x.esp": make_plugin(["Fallout4.esm"])})
    mod = tmp_path / "mod"
    mod.mkdir()
    os.symlink(outside / "Data", mod / "Data")
    plan = analyze_directory(mod)
    assert plan.mappings == []
    assert any("symlink" in w for w in plan.warnings)


# --- lead review additions ---------------------------------------------------


def test_enb_preset_keeps_config_txt_but_skips_license():
    files = [
        "Rudy ENB/enbseries.ini",
        "Rudy ENB/enblocal.ini",
        "Rudy ENB/effect.txt",
        "Rudy ENB/license_en.txt",
        "Rudy ENB/enbseries/enbeffect.fx.ini",
    ]
    plan = analyze_files(files)
    assert plan.target is Target.GAME_ROOT
    assert dests(plan)["Rudy ENB/effect.txt"] == "effect.txt"
    assert "Rudy ENB/license_en.txt" in plan.skipped
    assert_partition(plan, files)


def test_engine_fixes_part_2_preload_list_is_installed():
    files = [
        "d3dx9_42.dll",
        "tbb.dll",
        "tbbmalloc.dll",
        "EngineFixes_preload.txt",
        "Engine Fixes Readme.txt",
    ]
    plan = analyze_files(files)
    assert plan.target is Target.GAME_ROOT and plan.rule is Rule.GAME_ROOT_CONTENT
    assert set(dests(plan)) == {"d3dx9_42.dll", "tbb.dll", "tbbmalloc.dll", "EngineFixes_preload.txt"}
    assert plan.skipped == ["Engine Fixes Readme.txt"]


def test_script_extender_readmes_still_skipped():
    files = [
        "skse64_2_02_06/skse64_loader.exe",
        "skse64_2_02_06/skse64_1_6_1170.dll",
        "skse64_2_02_06/skse64_readme.txt",
        "skse64_2_02_06/skse64_whatsnew.txt",
        "skse64_2_02_06/Data/Scripts/Actor.pex",
    ]
    plan = analyze_files(files)
    assert sorted(plan.skipped) == ["skse64_2_02_06/skse64_readme.txt", "skse64_2_02_06/skse64_whatsnew.txt"]


def test_any_txt_at_data_root_is_skipped():
    plan = analyze_files(["MyMod.esp", "MyMod notes.txt", "Settings.txt"])
    assert plan.target is Target.DATA
    assert sorted(plan.skipped) == ["MyMod notes.txt", "Settings.txt"]


def test_macos_resource_forks_and_extensionless_docs_are_skipped():
    files = [
        "CoolMod/._CoolMod.esp",
        "CoolMod/CoolMod.esp",
        "CoolMod/textures/._a.dds",
        "CoolMod/textures/a.dds",
        "CoolMod/README",
        "CoolMod/LICENSE",
        "._CoolMod",
    ]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["CoolMod"] and plan.target is Target.DATA
    assert dests(plan) == {"CoolMod/CoolMod.esp": "Data/CoolMod.esp", "CoolMod/textures/a.dds": "Data/textures/a.dds"}
    assert plan.confidence is Confidence.HIGH
    assert_partition(plan, files)


def test_fomod_offers_core_files_next_to_fomod_folder():
    files = [
        "fomod/ModuleConfig.xml",
        "fomod/info.xml",
        "Main.esp",
        "Main - Main.ba2",
        "Optional/Patch.esp",
    ]
    plan = analyze_files(files)
    assert plan.rule is Rule.FOMOD
    assert plan.candidate_roots[0] == RootCandidate("", Target.DATA, Rule.DATA_CONTENT)
    assert RootCandidate("Optional", Target.DATA, Rule.DATA_CONTENT) in plan.candidate_roots
    chosen = plan_for_roots(files, ["", "Optional"])
    assert dests(chosen) == {
        "Main.esp": "Data/Main.esp",
        "Main - Main.ba2": "Data/Main - Main.ba2",
        "Optional/Patch.esp": "Data/Patch.esp",
    }


# --- final review additions --------------------------------------------------


def test_enb_textures_at_game_root_are_installed():
    files = [
        "Rudy ENB/enbseries.ini",
        "Rudy ENB/d3d11.dll",
        "Rudy ENB/enbsunsprite.bmp",
        "Rudy ENB/enblensmask.png",
        "Rudy ENB/enbunderwaternoise.bmp",
        "Rudy ENB/screenshot.jpg",
    ]
    plan = analyze_files(files)
    assert plan.target is Target.GAME_ROOT
    assert set(dests(plan).values()) == {
        "enbseries.ini", "d3d11.dll", "enbsunsprite.bmp", "enblensmask.png", "enbunderwaternoise.bmp",
    }
    assert plan.skipped == ["Rudy ENB/screenshot.jpg"]


def test_enb_named_images_in_data_mods_are_still_skipped():
    plan = analyze_files(["mod.esp", "enb_preview.png"])
    assert plan.skipped == ["enb_preview.png"]


def test_images_folder_does_not_block_wrapper_descent():
    files = ["Cool Mod/Cool Mod.esp", "Cool Mod/meshes/x.nif", "Images/shot1.png"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["Cool Mod"] and plan.target is Target.DATA
    assert plan.skipped == ["Images/shot1.png"]


def test_unknown_and_fomod_plans_list_unplaced_files():
    files = ["Option A/Stuff/a.nif", "Option B/Stuff/b.nif", "Option A/settings.ini", "readme.txt"]
    plan = analyze_files(files)
    assert plan.target is Target.UNKNOWN
    assert plan.unplaced == ["Option A/settings.ini", "Option A/Stuff/a.nif", "Option B/Stuff/b.nif"]
    assert plan.skipped == ["readme.txt"]
    assert plan.to_dict()["unplaced"] == plan.unplaced

    fomod = analyze_files(["fomod/ModuleConfig.xml", "00 Core/a.esp", "01 Opt/b.esp"])
    assert fomod.unplaced == ["00 Core/a.esp", "01 Opt/b.esp"]
    assert analyze_files(["a.esp"]).unplaced == []


def test_a_script_extender_repacked_in_its_own_named_folder_goes_to_the_game_folder():
    plan = analyze_files(["F4SE/f4se_loader.exe", "F4SE/f4se_1_10_163.dll", "F4SE/Data/Scripts/a.pex"])
    assert plan.target is Target.GAME_ROOT
    assert {m.dest for m in plan.mappings} == {"f4se_loader.exe", "f4se_1_10_163.dll", "Data/Scripts/a.pex"}
    plan = analyze_files(["F4SE/Plugins/x.dll"])  # a plugin for it is Data content as before
    assert [m.dest for m in plan.mappings] == ["Data/F4SE/Plugins/x.dll"]


def test_a_scripted_fomod_installer_is_an_installer():
    """FOMM's C# installers (fomod/script.cs) choose what to install, like ModuleConfig.xml ones."""
    plan = analyze_files(["fomod/info.xml", "fomod/script.cs", "A.esp", "A - Hardcore.esp", "Optional/textures/a.dds"])
    assert plan.is_fomod and plan.fomod_config == "fomod/script.cs" and plan.mappings == []
    plan = analyze_files(["fomod/info.xml", "A.esp"])  # info.xml alone is only a description
    assert not plan.is_fomod and [m.dest for m in plan.mappings] == ["Data/A.esp"]


@pytest.mark.parametrize("wrapper", ["ReShade", "Injector"])
def test_a_shader_injector_in_its_own_folder_goes_next_to_the_exe(wrapper):
    plan = analyze_files([f"{wrapper}/dxgi.dll", f"{wrapper}/ReShade.ini", f"{wrapper}/reshade-shaders/Shaders/a.fx",
                          "Readme.txt"])
    assert plan.target is Target.GAME_ROOT
    assert {m.dest for m in plan.mappings} == {"dxgi.dll", "ReShade.ini", "reshade-shaders/Shaders/a.fx"}


def test_a_known_folder_wrapping_a_data_folder_is_stepped_into():
    plan = analyze_files(["Tools/Data/MyTool.esp"])
    assert [m.dest for m in plan.mappings] == ["Data/MyTool.esp"]
