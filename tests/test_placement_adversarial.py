"""Adversarial review tests for bethmm.placement: realistic Nexus layouts,
overlapping manual roots, ordering guarantees and determinism."""

from __future__ import annotations

import json
import random

import pytest

from bethmm.placement import (
    Confidence,
    PlacementPlan,
    Rule,
    Target,
    analyze_files,
    plan_for_roots,
)
from bethmm.plugin_header import PluginHeader


def dests(plan: PlacementPlan) -> dict[str, str]:
    return {m.source: m.dest for m in plan.mappings}


def assert_partition(plan: PlacementPlan, files: list[str]) -> None:
    sources = [m.source for m in plan.mappings]
    assert len(sources) == len(set(sources)), "a source file is deployed twice"
    assert set(sources).isdisjoint(plan.skipped)
    assert set(sources) | set(plan.skipped) == set(files)


SSE = PluginHeader(masters=("Skyrim.esm", "Update.esm"), hedr_version=1.71, form_version=44, header_size=24, flags=0)
FO4 = PluginHeader(masters=("Fallout4.esm",), hedr_version=1.0, form_version=131, header_size=24, flags=0)


# --- plan_for_roots with overlapping roots -----------------------------------


def test_overlapping_roots_deploy_each_file_once_later_root_wins():
    files = ["A/x.dds", "A/B/y.dds", "A/B/readme.txt"]

    plan = plan_for_roots(files, ["A", "A/B"], Target.DATA)
    assert dests(plan) == {"A/x.dds": "Data/x.dds", "A/B/y.dds": "Data/y.dds"}
    # A/B was chosen last, and its readme sits at that root, so it is a doc.
    assert plan.skipped == ["A/B/readme.txt"]
    assert_partition(plan, files)
    assert any("overlap" in w and "'A/B'" in w for w in plan.warnings)

    plan = plan_for_roots(files, ["A/B", "A"], Target.DATA)
    assert dests(plan) == {
        "A/x.dds": "Data/x.dds",
        "A/B/y.dds": "Data/B/y.dds",
        "A/B/readme.txt": "Data/B/readme.txt",
    }
    assert plan.skipped == []
    assert_partition(plan, files)


def test_fomod_fallback_top_level_plus_option_folder():
    # A FOMOD whose core sits next to fomod/; the user picks the top level and
    # one option folder. The option's plugin must be linked once, not twice.
    files = ["fomod/ModuleConfig.xml", "Main.esp", "Main - Main.ba2", "Optional/Patch.esp"]
    plan = plan_for_roots(files, ["", "Optional"])
    assert dests(plan) == {
        "Main.esp": "Data/Main.esp",
        "Main - Main.ba2": "Data/Main - Main.ba2",
        "Optional/Patch.esp": "Data/Patch.esp",
    }
    assert plan.skipped == ["fomod/ModuleConfig.xml"]
    assert_partition(plan, files)
    assert any("overlap" in w for w in plan.warnings)


def test_disjoint_roots_have_no_overlap_warning():
    plan = plan_for_roots(["A/x.dds", "AB/y.dds"], ["A", "AB"], Target.DATA)
    assert plan.warnings == []


# --- Confidence ordering ------------------------------------------------------


def test_confidence_supports_every_comparison():
    assert Confidence.MEDIUM >= Confidence.MEDIUM
    assert Confidence.HIGH >= Confidence.MEDIUM
    assert not Confidence.LOW >= Confidence.MEDIUM
    assert Confidence.NONE <= Confidence.LOW <= Confidence.LOW
    assert Confidence.HIGH > Confidence.LOW
    assert sorted([Confidence.HIGH, Confidence.NONE, Confidence.MEDIUM]) == [
        Confidence.NONE, Confidence.MEDIUM, Confidence.HIGH,
    ]
    with pytest.raises(TypeError):
        Confidence.LOW >= 1  # noqa: B015


# --- realistic Nexus layouts (regression coverage) ---------------------------


def test_data_folder_in_versioned_wrapper_with_docs():
    files = [
        "Cool Mod 1.2/Data/CoolMod.esp",
        "Cool Mod 1.2/Data/Textures/a.dds",
        "Cool Mod 1.2/readme.txt",
        "Cool Mod 1.2/Docs/manual.pdf",
        "Cool Mod 1.2/Thumbs.db",
    ]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["Cool Mod 1.2"]
    assert plan.rule is Rule.DATA_FOLDER
    assert plan.target is Target.GAME_ROOT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {
        "Cool Mod 1.2/Data/CoolMod.esp": "Data/CoolMod.esp",
        "Cool Mod 1.2/Data/Textures/a.dds": "Data/Textures/a.dds",
    }
    assert_partition(plan, files)
    assert plan.warnings == []


@pytest.mark.parametrize("prefix", ["", "MyPlugin-1234-1-0/"])
def test_skse_plugin_dll_goes_to_data_not_game_root(prefix):
    files = [f"{prefix}SKSE/Plugins/MyPlugin.dll", f"{prefix}SKSE/Plugins/MyPlugin.ini", f"{prefix}MyPlugin readme.txt"]
    plan = analyze_files(files, installed_games=["skyrimse"])
    assert plan.target is Target.DATA
    assert plan.rule is Rule.DATA_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan)[f"{prefix}SKSE/Plugins/MyPlugin.dll"] == "Data/SKSE/Plugins/MyPlugin.dll"
    assert plan.warnings == []
    assert plan.game.game_id == "skyrimse"
    assert plan.game.confidence is Confidence.MEDIUM


def test_address_library():
    plan = analyze_files(["SKSE/Plugins/versionlib-1-6-1170-0.bin"])
    assert plan.target is Target.DATA
    assert dests(plan) == {"SKSE/Plugins/versionlib-1-6-1170-0.bin": "Data/SKSE/Plugins/versionlib-1-6-1170-0.bin"}


def test_engine_fixes_part_2_goes_to_game_root():
    files = ["tbb.dll", "tbbmalloc.dll", "d3dx9_42.dll"]
    plan = analyze_files(files, installed_games=["skyrimse"])
    assert plan.target is Target.GAME_ROOT
    assert plan.rule is Rule.GAME_ROOT_CONTENT
    assert plan.confidence is Confidence.HIGH
    assert dests(plan) == {f: f for f in files}
    assert plan.game.game_id == "skyrimse"
    assert plan.game.reasons == ["only installed game"]


def test_textures_entries_in_two_cases_inside_a_wrapper():
    files = ["Wrap/Textures/a.dds", "Wrap/textures/b.dds", "Wrap/Textures/c.dds", "Wrap/textures/c.dds", "Wrap/x.esp"]
    plan = analyze_files(files)
    assert plan.wrapper_dirs == ["Wrap"]
    assert plan.target is Target.DATA
    assert plan.confidence is Confidence.HIGH
    assert [m.dest.lower() for m in plan.mappings].count("data/textures/c.dds") == 1
    assert any("both go to" in w for w in plan.warnings)
    assert_partition(plan, files)


def test_bodyslide_caliente_tools_and_meshes():
    files = [
        "CalienteTools/BodySlide/SliderSets/x.osp",
        "CalienteTools/BodySlide/ShapeData/x/x.nif",
        "meshes/actors/character/x.nif",
    ]
    plan = analyze_files(files)
    assert plan.target is Target.DATA
    assert plan.confidence is Confidence.HIGH
    assert plan.warnings == []
    assert dests(plan) == {f: f"Data/{f}" for f in files}


def test_ini_only_mod_is_unknown_and_can_be_placed_manually():
    files = ["MyMod_DISTR.ini"]
    plan = analyze_files(files)
    assert plan.target is Target.UNKNOWN
    assert plan.confidence is Confidence.NONE
    assert plan.mappings == [] and plan.skipped == []
    plan = plan_for_roots(files, [""], Target.DATA)
    assert dests(plan) == {"MyMod_DISTR.ini": "Data/MyMod_DISTR.ini"}


@pytest.mark.parametrize("files", [
    ["readme.txt", "Docs/manual.pdf", "preview.jpg"],
    ["Wrap/readme.txt", "Wrap/changelog.md"],
    ["__MACOSX/._x.esp", ".DS_Store"],
])
def test_nothing_deployable_is_unknown_and_everything_is_skipped(files):
    plan = analyze_files(files)
    assert plan.target is Target.UNKNOWN
    assert plan.confidence is Confidence.NONE
    assert plan.mappings == [] and plan.candidate_roots == []
    assert sorted(plan.skipped) == sorted(files)


# --- determinism ---------------------------------------------------------------


ARCHIVES = [
    [
        "skse64_2_02_06/skse64_loader.exe", "skse64_2_02_06/skse64_1_6_1170.dll",
        "skse64_2_02_06/skse64_readme.txt", "skse64_2_02_06/Data/Scripts/Actor.pex",
        "skse64_2_02_06/Data/Scripts/Source/Actor.psc", "skse64_2_02_06/src/common/IPrefix.h",
    ],
    [
        "fomod/info.xml", "fomod/ModuleConfig.xml", "00 Core/MyMod.esp", "00 Core/MyMod - Main.ba2",
        "01 Option A/Textures/a.dds", "02 Option B/textures/a.dds", "02 Option B/F4SE/Plugins/b.dll",
        "Options/Deep/meshes/x.nif", "Options/Other/d3d11.dll",
    ],
    ["Textures/a.dds", "textures/a.dds", "x.esp", "y.esp", "MyMod.ini", "Extras/z.ini", "d3d11.dll", "readme.txt"],
    ["00 Core/Data/Core.esp", "01 Optional/Data/Opt.esp", "B/textures/x.dds", "a/deep/er/x.esp", "readme.txt"],
]


@pytest.mark.parametrize("files", ARCHIVES)
def test_plan_does_not_depend_on_input_order(files):
    def reader(source):
        return FO4 if "Core" in source else SSE

    expected = json.dumps(analyze_files(files, header_reader=reader).to_dict())
    rng = random.Random(42)
    for _ in range(5):
        shuffled = files + files[:2]
        rng.shuffle(shuffled)
        assert json.dumps(analyze_files(shuffled, header_reader=reader).to_dict()) == expected


def test_manual_plan_does_not_depend_on_input_order():
    files = ARCHIVES[1]
    roots = ["00 Core", "02 Option B", "01 Option A"]
    expected = plan_for_roots(files, roots).to_dict()
    shuffled = list(reversed(files))
    assert plan_for_roots(shuffled, roots).to_dict() == expected


def test_a_name_with_a_control_character_is_never_installed():
    """No Windows file name can hold one, and a newline would split a line of plugins.txt."""
    files = ["Data/Cool.esp", "Data/Evil.esp\n*Other.esp", "Data/textures/a\tb.dds"]
    for plan in (analyze_files(files, header_reader=lambda _p: FO4),
                 plan_for_roots(files, [""], Target.GAME_ROOT, header_reader=lambda _p: FO4)):
        assert [m.source for m in plan.mappings] == ["Data/Cool.esp"]
        assert sum("ignored unsafe path" in w for w in plan.warnings) == 2
