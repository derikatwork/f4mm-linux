"""The FOMOD installer engine: reading ModuleConfig.xml and working out which
files a set of choices installs."""

from __future__ import annotations

import pytest

from bethmm import fomod
from bethmm.fomod import FileState, FomodError, GroupType, OptionType, Wizard
from bethmm.placement import FileMapping

FILES = [
    "fomod/ModuleConfig.xml",
    "00 Core/Cool.esp",
    "00 Core/Textures/Cool/a.dds",
    "01 Options/2K/textures/cool/b.dds",
    "01 Options/4K/textures/cool/b.dds",
    "02 Patches/CoolUSO.esp",
    "readme.txt",
    "images/2k.png",
]


def config(body: str, *, attrs: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd"{attrs}>
  <moduleName>Cool Mod</moduleName>
  {body}
</config>""".encode("utf-16")


STANDARD = config(r"""
  <requiredInstallFiles>
    <folder source="00 Core" destination="" />
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Textures">
      <optionalFileGroups order="Explicit">
        <group name="Resolution" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="2K">
              <description>Smaller textures.</description>
              <image path="images\2k.png" />
              <files><folder source="01 Options\2K" destination="" /></files>
              <conditionFlags><flag name="res">2k</flag></conditionFlags>
              <typeDescriptor><type name="Recommended" /></typeDescriptor>
            </plugin>
            <plugin name="4K">
              <description>Bigger textures.</description>
              <files><folder source="01 options\4k" destination="" /></files>
              <conditionFlags><flag name="res">4k</flag></conditionFlags>
              <typeDescriptor><type name="Optional" /></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Patches">
      <visible><flagDependency flag="res" value="4k" /></visible>
      <optionalFileGroups>
        <group name="Patches" type="SelectAny">
          <plugins>
            <plugin name="USO patch">
              <description>For the unofficial patch.</description>
              <files><file source="02 Patches\CoolUSO.esp" destination="CoolUSO.esp" /></files>
              <typeDescriptor>
                <dependencyType>
                  <defaultType name="Optional" />
                  <patterns>
                    <pattern>
                      <dependencies operator="And"><fileDependency file="Unofficial.esp" state="Active" /></dependencies>
                      <type name="Recommended" />
                    </pattern>
                  </patterns>
                </dependencyType>
              </typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
""")


def module(data: bytes = STANDARD) -> fomod.Module:
    return fomod.parse_module(data)


def dests(outcome: fomod.Outcome) -> dict[str, str]:
    return {m.dest: m.source for m in outcome.mappings}


def test_reads_a_utf16_installer_with_its_steps_groups_and_options():
    m = module()
    assert m.name == "Cool Mod" and [s.name for s in m.steps] == ["Textures", "Patches"]
    group = m.steps[0].groups[0]
    assert group.type is GroupType.EXACTLY_ONE and [o.name for o in group.options] == ["2K", "4K"]
    assert group.options[0].image == "images/2k.png" and group.options[0].description == "Smaller textures."
    assert group.options[0].flags == (("res", "2k"),)


@pytest.mark.parametrize("encode", [
    lambda text: text.encode("utf-8"),
    lambda text: b"\xef\xbb\xbf" + text.encode("utf-8"),
    lambda text: text.encode("utf-16-le"),  # no byte order mark
    lambda text: text.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode("utf-16"),
])
def test_any_encoding_installers_use_is_read(encode):
    text = STANDARD.decode("utf-16")
    assert fomod.parse_module(encode(text)).name == "Cool Mod"


def test_the_default_choices_install_the_recommended_option():
    w = Wizard(module())
    assert w.visible_steps() == [0]  # Patches only for 4K
    out = w.outcome(FILES)
    assert dests(out) == {
        "Data/Cool.esp": "00 Core/Cool.esp",
        "Data/Textures/Cool/a.dds": "00 Core/Textures/Cool/a.dds",
        "Data/textures/cool/b.dds": "01 Options/2K/textures/cool/b.dds",
    }
    assert out.flags == {"res": "2k"} and out.chosen == ["2K"]


def test_a_flag_set_by_one_step_shows_the_next_and_a_file_condition_recommends_an_option():
    states = {"Unofficial.esp": FileState.ACTIVE}
    w = Wizard(module(), lambda path: states.get(path, FileState.MISSING))
    w.choose(0, 0, [1])
    assert w.visible_steps() == [0, 1]
    assert w.option_type(1, 0, 0) is OptionType.RECOMMENDED and w.selection(1) == {0: [0]}
    out = w.outcome(FILES)
    assert dests(out)["Data/textures/cool/b.dds"] == "01 Options/4K/textures/cool/b.dds"  # case-insensitive source
    assert dests(out)["Data/CoolUSO.esp"] == "02 Patches/CoolUSO.esp"
    states.clear()
    assert Wizard(module()).option_type(1, 0, 0) is OptionType.OPTIONAL


def test_group_rules():
    w = Wizard(module())
    assert w.problem(0, 0, []) == "choose exactly one option in Resolution"
    assert w.problem(0, 0, [0, 1]) == "choose exactly one option in Resolution"
    with pytest.raises(FomodError):
        w.choose(0, 0, [])
    w.choose(0, 0, [1])
    assert w.selection(0) == {0: [1]}


GROUPS = config("""
  <installSteps>
    <installStep name="Only">
      <optionalFileGroups order="Explicit">
        <group name="All" type="SelectAll"><plugins>
          <plugin name="a"><typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
          <plugin name="b"><typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
        </plugins></group>
        <group name="AtMost" type="SelectAtMostOne"><plugins>
          <plugin name="c"><typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
          <plugin name="d"><typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
        </plugins></group>
        <group name="AtLeast" type="SelectAtLeastOne"><plugins>
          <plugin name="e"><typeDescriptor><type name="NotUsable" /></typeDescriptor></plugin>
          <plugin name="f"><typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
        </plugins></group>
        <group name="Any" type="SelectAny"><plugins>
          <plugin name="g"><typeDescriptor><type name="required" /></typeDescriptor></plugin>
          <plugin name="h"><typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
        </plugins></group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
""")


def test_defaults_and_rules_of_every_group_type():
    w = Wizard(fomod.parse_module(GROUPS))
    assert w.selection(0) == {0: [0, 1], 1: [], 2: [1], 3: [0]}
    assert w.problem(0, 0, [0]) == "every option in All is installed"
    assert w.problem(0, 1, [0, 1]) == "choose at most one option in AtMost"
    assert w.problem(0, 2, []) == "choose at least one option in AtLeast"
    assert w.problem(0, 2, [0]) == "'e' can't be used here"
    assert w.problem(0, 3, [1]) == "'g' is required"
    assert w.locked(0, 0, 0) and w.locked(0, 2, 0) and w.locked(0, 3, 0) and not w.locked(0, 3, 1)
    assert w.step_problems(0) == []


def test_order_ascending_is_the_default():
    data = config("""
      <installSteps>
        <installStep name="B"><optionalFileGroups><group name="g" type="SelectAny"><plugins>
          <plugin name="zeta" /><plugin name="Alpha" /></plugins></group></optionalFileGroups></installStep>
        <installStep name="a"><optionalFileGroups /></installStep>
      </installSteps>""")
    m = fomod.parse_module(data)
    assert [s.name for s in m.steps] == ["a", "B"]
    assert [o.name for o in m.steps[1].groups[0].options] == ["Alpha", "zeta"]


def test_conditional_installs_priorities_and_always_install():
    data = config(r"""
      <requiredInstallFiles>
        <file source="00 Core\Cool.esp" />
        <file source="01 Options\2K\textures\cool\b.dds" destination="textures\cool\b.dds" priority="1" />
      </requiredInstallFiles>
      <installSteps order="Explicit"><installStep name="s"><optionalFileGroups>
        <group name="g" type="SelectAny"><plugins>
          <plugin name="x">
            <files>
              <file source="01 Options\4K\textures\cool\b.dds" destination="textures\cool\b.dds" />
              <file source="readme.txt" destination="Docs\" alwaysInstall="true" />
            </files>
            <conditionFlags><flag name="x">on</flag></conditionFlags>
            <typeDescriptor><type name="Optional" /></typeDescriptor>
          </plugin>
        </plugins></group>
      </optionalFileGroups></installStep></installSteps>
      <conditionalFileInstalls><patterns>
        <pattern>
          <dependencies operator="Or"><flagDependency flag="x" value="on" /><flagDependency flag="y" value="1" /></dependencies>
          <files><file source="02 Patches\CoolUSO.esp" destination="CoolUSO.esp" priority="5" /></files>
        </pattern>
      </patterns></conditionalFileInstalls>""")
    w = Wizard(fomod.parse_module(data))
    out = w.outcome(FILES)
    assert dests(out) == {"Data/00 Core/Cool.esp": "00 Core/Cool.esp",  # no destination: the source's path
                          "Data/textures/cool/b.dds": "01 Options/2K/textures/cool/b.dds",
                          "Data/Docs/readme.txt": "readme.txt"}  # always installed, even unchosen
    w.choose(0, 0, [0])
    out = w.outcome(FILES)
    assert dests(out)["Data/textures/cool/b.dds"] == "01 Options/2K/textures/cool/b.dds"  # priority 1 beats 0
    assert dests(out)["Data/CoolUSO.esp"] == "02 Patches/CoolUSO.esp"


def test_unsafe_or_missing_paths_are_left_out_with_a_warning():
    data = config(r"""
      <requiredInstallFiles>
        <file source="00 Core\Cool.esp" destination="..\..\evil.esp" />
        <file source="..\outside.esp" />
        <file source="missing.esp" />
        <folder source="Nope" destination="" />
        <file source="00 Core\Cool.esp" destination="C:\Windows\x.esp" />
        <file source="00 Core\Cool.esp" destination="Cool.esp&#10;*Other.esp" />
      </requiredInstallFiles>""")
    out = Wizard(fomod.parse_module(data)).outcome(FILES)
    assert out.mappings == [] and len(out.warnings) == 6


def test_the_whole_root_as_a_folder_leaves_out_the_installer_itself():
    data = config('<requiredInstallFiles><folder source="" destination="" /></requiredInstallFiles>')
    out = Wizard(fomod.parse_module(data)).outcome(FILES)
    assert "Data/fomod/ModuleConfig.xml" not in dests(out) and "Data/readme.txt" in dests(out)


def test_sources_are_relative_to_the_folder_holding_fomod(tmp_path):
    root = tmp_path / "x"
    (root / "Wrapper" / "fomod").mkdir(parents=True)
    (root / "Wrapper" / "fomod" / "ModuleConfig.xml").write_bytes(
        config(r'<requiredInstallFiles><file source="Main\A.esp" destination="A.esp" /></requiredInstallFiles>'))
    (root / "Wrapper" / "fomod" / "info.xml").write_text(
        "<fomod><Name>Cool Mod</Name><Author>Someone</Author><Version>1.2</Version></fomod>")
    (root / "Wrapper" / "Main").mkdir()
    (root / "Wrapper" / "Main" / "a.esp").write_text("plugin")
    m = fomod.load(root, "Wrapper/fomod/ModuleConfig.xml")
    assert m.root == "Wrapper" and m.info.author == "Someone" and m.info.version == "1.2"
    out = Wizard(m).outcome(["Wrapper/fomod/ModuleConfig.xml", "Wrapper/Main/a.esp"])
    assert out.mappings == [FileMapping("Wrapper/Main/a.esp", "Data/A.esp")]


def test_a_csharp_script_installer_is_refused():
    with pytest.raises(FomodError, match="C# script"):
        fomod.load(None, "fomod/script.cs")


@pytest.mark.parametrize("data", [b"not xml at all <", "<other/>".encode(), b"\x00" * 10])
def test_broken_installers_raise(data):
    with pytest.raises(FomodError):
        fomod.parse_module(data)


def test_choices_are_remembered_by_name_and_reapplied():
    w = Wizard(module())
    w.choose(0, 0, [1])
    w.choose(1, 0, [0])
    remembered = w.choices()
    assert remembered == [{"step": "Textures", "groups": [{"group": "Resolution", "options": ["4K"]}]},
                          {"step": "Patches", "groups": [{"group": "Patches", "options": ["USO patch"]}]}]
    again = Wizard(module())
    assert again.apply_choices(remembered) == []
    assert again.selection(0) == {0: [1]} and again.selection(1) == {0: [0]}
    # A newer version renamed an option: that choice falls back to the default.
    renamed = Wizard(fomod.parse_module(STANDARD.decode("utf-16").replace('"4K"', '"4K (new)"').encode("utf-16")))
    assert renamed.apply_choices(remembered) == ["the choice in 'Resolution'"]
    assert renamed.selection(0) == {0: [0]}


def test_module_dependencies_are_reported():
    data = config('<moduleDependencies operator="And"><fileDependency file="Needed.esm" state="Active" />'
                  '</moduleDependencies>')
    assert Wizard(fomod.parse_module(data)).unmet_dependencies() == "Needed.esm is active"
    assert Wizard(fomod.parse_module(data), lambda p: FileState.ACTIVE).unmet_dependencies() is None


CHANGING = config("""
  <installSteps order="Explicit">
    <installStep name="Base">
      <optionalFileGroups><group name="Edition" type="SelectExactlyOne"><plugins order="Explicit">
        <plugin name="Full"><conditionFlags><flag name="ed">full</flag></conditionFlags>
          <typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
        <plugin name="Lite"><conditionFlags><flag name="ed">lite</flag></conditionFlags>
          <typeDescriptor><type name="Optional" /></typeDescriptor></plugin>
      </plugins></group></optionalFileGroups>
    </installStep>
    <installStep name="Extras">
      <optionalFileGroups><group name="Extras" type="SelectAny"><plugins order="Explicit">
        <plugin name="Big extra"><typeDescriptor><dependencyType><defaultType name="Optional" />
          <patterns><pattern><dependencies><flagDependency flag="ed" value="lite" /></dependencies>
          <type name="NotUsable" /></pattern></patterns></dependencyType></typeDescriptor></plugin>
        <plugin name="Core extra"><typeDescriptor><dependencyType><defaultType name="Optional" />
          <patterns><pattern><dependencies><flagDependency flag="ed" value="lite" /></dependencies>
          <type name="Required" /></pattern></patterns></dependencyType></typeDescriptor></plugin>
      </plugins></group></optionalFileGroups>
    </installStep>
  </installSteps>""")


def test_a_choice_that_an_earlier_step_made_impossible_is_not_kept():
    """Big extra chosen with Full; then Lite is chosen in the first step, which
    makes Big extra unusable and Core extra required: the second step falls
    back to what the installer allows instead of a choice nobody can fix."""
    w = Wizard(fomod.parse_module(CHANGING))
    w.choose(0, 0, [0])
    w.choose(1, 0, [0])
    w.choose(0, 0, [1])
    assert w.selection(1) == {0: [1]} and w.step_problems(1) == []
    assert w.choices()[1] == {"step": "Extras", "groups": [{"group": "Extras", "options": ["Core extra"]}]}
