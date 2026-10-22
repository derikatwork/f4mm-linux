"""FOMOD installers: read ``fomod/ModuleConfig.xml`` and work out which of a
mod's files it installs, for the choices made in its steps.

Pure logic over the extracted mod (no GUI): :func:`load` reads the XML (and
``fomod/info.xml``) into a :class:`Module`; a :class:`Wizard` walks through its
steps -- which are visible, which options each group offers and preselects,
whether a selection is allowed -- and :meth:`Wizard.outcome` gives the files
to install as ``placement.FileMapping`` (``dest`` under ``Data/``).

What is supported (the FOMOD schema, versions 1-5, as mod managers read it):

* ``requiredInstallFiles``; ``installSteps`` with ``visible`` conditions;
  groups of every ``type`` (SelectAny, SelectAll, SelectExactlyOne,
  SelectAtMostOne, SelectAtLeastOne); ``order`` (Ascending -- the schema's
  default -- Descending, Explicit) for steps, groups and options;
* per option: description, image, files and folders, ``conditionFlags``, and a
  ``typeDescriptor`` (Required, Recommended, Optional, CouldBeUsable,
  NotUsable), also chosen by ``dependencyType`` patterns;
* ``conditionalFileInstalls``; conditions on flags, on files (Active,
  Inactive or Missing -- the caller says which, see ``FileState``), nested
  ``dependencies`` with And/Or; game, FOMM and script extender version
  conditions count as met (the app can't check versions);
* ``priority`` (higher wins when two entries install to the same path; at
  equal priority the later one wins), ``alwaysInstall`` and
  ``installIfUsable``; a missing ``destination`` means the source's own path.

Paths in the XML use backslashes and any case; they are matched
case-insensitively against the extracted files, relative to the folder that
holds ``fomod/``. Destinations are relative to the game's Data folder; one
that would leave it is refused. The older C# script installers
(``fomod/script.cs``) are not supported: :func:`load` raises
:class:`FomodError` and the caller falls back to choosing folders.
"""

from __future__ import annotations

import bisect
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

from bethmm.placement import FileMapping, _is_junk

#: Larger than any real installer; refuses to parse anything bigger.
MAX_XML_BYTES = 16 * 1024 * 1024


class FomodError(ValueError):
    """The installer can't be read (or is a kind the app doesn't run)."""


class GroupType(Enum):
    ANY = "SelectAny"
    ALL = "SelectAll"
    EXACTLY_ONE = "SelectExactlyOne"
    AT_MOST_ONE = "SelectAtMostOne"
    AT_LEAST_ONE = "SelectAtLeastOne"


class OptionType(Enum):
    REQUIRED = "Required"
    RECOMMENDED = "Recommended"
    OPTIONAL = "Optional"
    COULD_BE_USABLE = "CouldBeUsable"
    NOT_USABLE = "NotUsable"


class FileState(Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    MISSING = "Missing"


#: How a condition on a file is answered: its path relative to Data (as the
#: installer spells it) -> its state. Default: every file is missing.
FileStateFn = Callable[[str], FileState]


def _missing(_path: str) -> FileState:
    return FileState.MISSING


# ---------------------------------------------------------------- the model


@dataclass(frozen=True)
class Condition:
    """``operator`` over the leaves and nested conditions in ``parts``."""

    operator: str = "And"
    #: ("flag", name, value) | ("file", path, FileState) | ("version", what, text) | Condition
    parts: tuple = ()

    def holds(self, flags: dict[str, str], file_state: FileStateFn) -> bool:
        results = []
        for part in self.parts:
            if isinstance(part, Condition):
                results.append(part.holds(flags, file_state))
            elif part[0] == "flag":
                results.append(flags.get(part[1], "") == part[2])
            elif part[0] == "file":
                results.append(file_state(part[1]) is part[2])
            else:
                results.append(True)  # a game/FOMM/script extender version: can't be checked
        if not results:
            return True
        return any(results) if self.operator.lower() == "or" else all(results)

    def describe(self) -> str:
        words = []
        for part in self.parts:
            if isinstance(part, Condition):
                words.append(f"({part.describe()})")
            elif part[0] == "flag":
                words.append(f"{part[1]} is {part[2]!r}")
            elif part[0] == "file":
                words.append(f"{part[1]} is {part[2].value.lower()}")
            else:
                words.append(f"{part[1]} {part[2]}")
        return f" {self.operator.lower()} ".join(words) or "always"


@dataclass(frozen=True)
class FileItem:
    #: As written in the XML, separators normalised to "/".
    source: str
    #: Relative to Data; None: the source's own path.
    destination: str | None
    is_folder: bool = False
    priority: int = 0
    always_install: bool = False
    install_if_usable: bool = False


@dataclass(frozen=True)
class TypeDescriptor:
    default: OptionType = OptionType.OPTIONAL
    patterns: tuple[tuple[Condition, OptionType], ...] = ()

    def resolve(self, flags: dict[str, str], file_state: FileStateFn) -> OptionType:
        for condition, kind in self.patterns:
            if condition.holds(flags, file_state):
                return kind
        return self.default


@dataclass(frozen=True)
class Option:
    name: str
    description: str = ""
    #: Path of the option's picture as written (relative to the mod's root).
    image: str | None = None
    files: tuple[FileItem, ...] = ()
    flags: tuple[tuple[str, str], ...] = ()
    type: TypeDescriptor = TypeDescriptor()


@dataclass(frozen=True)
class Group:
    name: str
    type: GroupType
    options: tuple[Option, ...]


@dataclass(frozen=True)
class Step:
    name: str
    groups: tuple[Group, ...]
    visible: Condition | None = None


@dataclass(frozen=True)
class Info:
    name: str | None = None
    author: str | None = None
    version: str | None = None
    website: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Module:
    name: str
    #: The folder that holds fomod/ (relative to the extraction folder; "" = top).
    root: str
    image: str | None = None
    required: tuple[FileItem, ...] = ()
    steps: tuple[Step, ...] = ()
    conditional: tuple[tuple[Condition, tuple[FileItem, ...]], ...] = ()
    #: What the installer needs before it can be installed at all.
    dependencies: Condition | None = None
    info: Info = Info()
    #: Problems found while reading (unknown group types, ...); the installer still runs.
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------- reading


def decode_xml(data: bytes) -> str:
    """The text of an XML file in whatever encoding installers use: UTF-8 or
    UTF-16 (with or without a byte order mark, whatever the declaration
    says), else Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    head = data[:200]
    if head[1::2].count(0) > len(head) // 4 and head[0::2].count(0) < len(head) // 8:
        return data.decode("utf-16-le", errors="replace")
    if head[0::2].count(0) > len(head) // 4:
        return data.decode("utf-16-be", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _parse(data: bytes, what: str) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise FomodError(f"{what} is too large to be an installer ({len(data)} bytes)")
    text = decode_xml(data)
    # The declaration may name an encoding the text no longer has.
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"), count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FomodError(f"{what} is not valid XML: {exc}") from None
    for el in root.iter():  # namespaces and case don't matter to mod managers
        if isinstance(el.tag, str):
            el.tag = el.tag.rsplit("}", 1)[-1].lower()
            el.attrib = {k.rsplit("}", 1)[-1].lower(): v for k, v in el.attrib.items()}
    return root


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _child(el: ET.Element | None, tag: str) -> ET.Element | None:
    return el.find(tag) if el is not None else None


def _norm(path: str) -> str:
    """A path from the XML with "/" separators and no leading "./" or "/"."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts)


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _int(value: str | None) -> int:
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


def _ordered(items: list, el: ET.Element | None, name: Callable[[object], str]) -> list:
    order = (el.get("order") if el is not None else None) or "Ascending"
    if order.lower() == "explicit":
        return items
    return sorted(items, key=lambda item: name(item).casefold(), reverse=order.lower() == "descending")


class _Reader:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def condition(self, el: ET.Element | None) -> Condition | None:
        if el is None:
            return None
        parts: list = []
        for child in el:
            if child.tag == "flagdependency":
                parts.append(("flag", child.get("flag", ""), child.get("value", "")))
            elif child.tag == "filedependency":
                state = child.get("state", "Active")
                try:
                    kind = FileState(state[:1].upper() + state[1:].lower())
                except ValueError:
                    self.warnings.append(f"unknown file state {state!r}; treated as Active")
                    kind = FileState.ACTIVE
                parts.append(("file", _norm(child.get("file", "")), kind))
            elif child.tag in ("gamedependency", "fommdependency", "fosedependency"):
                parts.append(("version", child.tag.replace("dependency", ""), child.get("version", "")))
            elif child.tag == "dependencies":
                nested = self.condition(child)
                if nested is not None:
                    parts.append(nested)
        return Condition(el.get("operator", "And"), tuple(parts))

    def files(self, el: ET.Element | None) -> tuple[FileItem, ...]:
        items = []
        for child in el if el is not None else ():
            if child.tag not in ("file", "folder"):
                continue
            source = _norm(child.get("source", ""))
            if child.tag == "file" and not source:
                self.warnings.append("a file entry without a source is ignored")
                continue
            destination = child.get("destination")
            items.append(FileItem(source, None if destination is None else destination.replace("\\", "/"),
                                  child.tag == "folder", _int(child.get("priority")),
                                  _bool(child.get("alwaysinstall")), _bool(child.get("installifusable"))))
        return tuple(items)

    def type_descriptor(self, el: ET.Element | None) -> TypeDescriptor:
        def kind(name: str | None) -> OptionType:
            found = next((t for t in OptionType if t.value.lower() == (name or "Optional").strip().lower()), None)
            if found is None:
                self.warnings.append(f"unknown option type {name!r}; treated as Optional")
                return OptionType.OPTIONAL
            return found

        if el is None:
            return TypeDescriptor()
        simple = _child(el, "type")
        if simple is not None:
            return TypeDescriptor(kind(simple.get("name")))
        dependency = _child(el, "dependencytype")
        if dependency is None:
            return TypeDescriptor()
        default = kind(_child(dependency, "defaulttype").get("name")) \
            if _child(dependency, "defaulttype") is not None else OptionType.OPTIONAL
        patterns = []
        for pattern in _child(dependency, "patterns") if _child(dependency, "patterns") is not None else ():
            condition = self.condition(_child(pattern, "dependencies"))
            if condition is not None and _child(pattern, "type") is not None:
                patterns.append((condition, kind(_child(pattern, "type").get("name"))))
        return TypeDescriptor(default, tuple(patterns))

    def option(self, el: ET.Element) -> Option:
        image = _child(el, "image")
        flags = tuple((flag.get("name", ""), _text(flag)) for flag in
                      (_child(el, "conditionflags") if _child(el, "conditionflags") is not None else ())
                      if flag.tag == "flag")
        return Option(el.get("name", "").strip() or "(unnamed option)", _text(_child(el, "description")),
                      _norm(image.get("path", "")) or None if image is not None else None,
                      self.files(_child(el, "files")), flags, self.type_descriptor(_child(el, "typedescriptor")))

    def group(self, el: ET.Element) -> Group:
        raw = el.get("type", "SelectAny")
        try:
            kind = GroupType(raw)
        except ValueError:
            kind = next((t for t in GroupType if t.value.lower() == raw.lower()), GroupType.ANY)
            if kind.value.lower() != raw.lower():
                self.warnings.append(f"group {el.get('name')!r} has an unknown type {raw!r}; any choice is allowed")
        plugins = _child(el, "plugins")
        options = [self.option(p) for p in (plugins if plugins is not None else ()) if p.tag == "plugin"]
        return Group(el.get("name", "").strip(), kind, tuple(_ordered(options, plugins, lambda o: o.name)))

    def step(self, el: ET.Element) -> Step:
        groups_el = _child(el, "optionalfilegroups")
        groups = [self.group(g) for g in (groups_el if groups_el is not None else ()) if g.tag == "group"]
        return Step(el.get("name", "").strip(), tuple(_ordered(groups, groups_el, lambda g: g.name)),
                    self.condition(_child(el, "visible")))


def read_info(data: bytes) -> Info:
    """``fomod/info.xml``: the mod's name, author, version and website."""
    root = _parse(data, "info.xml")

    def field_(tag: str) -> str | None:
        return _text(root.find(tag)) or None

    version = root.find("version")
    return Info(field_("name"), field_("author"),
                (_text(version) or version.get("machineversion")) if version is not None else None,
                field_("website"), field_("description"))


def parse_module(data: bytes, root: str = "", info: Info = Info()) -> Module:
    """A Module from the bytes of ModuleConfig.xml; *root* is the folder holding fomod/."""
    config = _parse(data, "ModuleConfig.xml")
    if config.tag != "config":
        raise FomodError(f"ModuleConfig.xml starts with <{config.tag}>, not <config>")
    reader = _Reader()
    steps_el = _child(config, "installsteps")
    steps = [reader.step(s) for s in (steps_el if steps_el is not None else ()) if s.tag == "installstep"]
    conditional = []
    patterns = _child(_child(config, "conditionalfileinstalls"), "patterns")
    for pattern in patterns if patterns is not None else ():
        condition = reader.condition(_child(pattern, "dependencies"))
        conditional.append((condition or Condition(), reader.files(_child(pattern, "files"))))
    image = _child(config, "moduleimage")
    name = _text(_child(config, "modulename")) or info.name or "FOMOD installer"
    return Module(name, root, _norm(image.get("path", "")) or None if image is not None else None,
                  reader.files(_child(config, "requiredinstallfiles")),
                  tuple(_ordered(steps, steps_el, lambda s: s.name)), tuple(conditional),
                  reader.condition(_child(config, "moduledependencies")), info, tuple(reader.warnings))


def load(files_dir: Path, config: str) -> Module:
    """Read the installer whose ModuleConfig.xml is at *config* (relative to
    the extraction folder *files_dir*, as ``PlacementPlan.fomod_config`` has it)."""
    if config.lower().endswith(".cs"):
        raise FomodError("this mod uses the old C# script installer (fomod/script.cs), which the app can't run; "
                         "choose which of its folders to install instead")
    fomod_dir = str(Path(config).parent)
    root = str(Path(fomod_dir).parent) if fomod_dir not in ("", ".") else ""
    root = "" if root == "." else root
    info = Info()
    folder = files_dir / fomod_dir
    try:
        info_file = next((p for p in folder.iterdir() if p.name.lower() == "info.xml" and p.is_file()), None)
    except OSError:
        info_file = None
    if info_file is not None:
        try:
            info = read_info(info_file.read_bytes())
        except (OSError, FomodError):
            pass  # only the name and version would come from it
    try:
        data = (files_dir / config).read_bytes()
    except OSError as exc:
        raise FomodError(f"can't read {config}: {exc.strerror or exc}") from None
    return parse_module(data, root, info)


# ---------------------------------------------------------------- choosing

#: Step index -> group index -> the indices of the chosen options.
Selections = dict[int, dict[int, list[int]]]


class Wizard:
    """The state of one run of an installer: which options are chosen in each
    step. Steps are numbered as in ``module.steps``; only visible ones count.
    A step's visibility, and its options' types, depend on the flags the
    visible steps before it set."""

    def __init__(self, module: Module, file_state: FileStateFn | None = None) -> None:
        self.module = module
        self.file_state = file_state or _missing
        self.selections: Selections = {}

    # -- flags and visibility

    def flags_before(self, step: int) -> dict[str, str]:
        flags: dict[str, str] = {}
        for index in range(step):
            if self._visible(index, flags):
                for g, chosen in self.selection(index, flags).items():
                    for o in chosen:
                        flags.update(self.module.steps[index].groups[g].options[o].flags)
        return flags

    def _visible(self, step: int, flags: dict[str, str]) -> bool:
        condition = self.module.steps[step].visible
        return condition is None or condition.holds(flags, self.file_state)

    def is_visible(self, step: int) -> bool:
        return self._visible(step, self.flags_before(step))

    def visible_steps(self) -> list[int]:
        return [i for i in range(len(self.module.steps)) if self.is_visible(i)]

    def next_step(self, step: int | None) -> int | None:
        """The visible step after *step* (None: the first), or None at the end."""
        start = 0 if step is None else step + 1
        return next((i for i in range(start, len(self.module.steps)) if self.is_visible(i)), None)

    def previous_step(self, step: int) -> int | None:
        return next((i for i in range(step - 1, -1, -1) if self.is_visible(i)), None)

    # -- options

    def option_type(self, step: int, group: int, option: int, flags: dict[str, str] | None = None) -> OptionType:
        flags = self.flags_before(step) if flags is None else flags
        return self.module.steps[step].groups[group].options[option].type.resolve(flags, self.file_state)

    def locked(self, step: int, group: int, option: int) -> bool:
        """Whether the option can't be changed: always chosen (Required, or a
        SelectAll group) or never (NotUsable)."""
        kind = self.option_type(step, group, option)
        return kind in (OptionType.REQUIRED, OptionType.NOT_USABLE) \
            or self.module.steps[step].groups[group].type is GroupType.ALL

    def default(self, step: int, group: int, flags: dict[str, str] | None = None) -> list[int]:
        """What the installer preselects in a group."""
        flags = self.flags_before(step) if flags is None else flags
        g = self.module.steps[step].groups[group]
        kinds = [o.type.resolve(flags, self.file_state) for o in g.options]
        usable = [i for i, k in enumerate(kinds) if k is not OptionType.NOT_USABLE]
        if g.type is GroupType.ALL:
            return usable
        required = [i for i, k in enumerate(kinds) if k is OptionType.REQUIRED]
        recommended = [i for i, k in enumerate(kinds) if k is OptionType.RECOMMENDED]
        if g.type in (GroupType.EXACTLY_ONE, GroupType.AT_MOST_ONE):
            pick = (required or recommended)[:1]
            if not pick and g.type is GroupType.EXACTLY_ONE:
                pick = usable[:1]
            return pick
        chosen = sorted(set(required) | set(recommended))
        if not chosen and g.type is GroupType.AT_LEAST_ONE:
            chosen = usable[:1]
        return chosen

    def selection(self, step: int, flags: dict[str, str] | None = None) -> dict[int, list[int]]:
        """The chosen options of each group of *step*: the user's choice, or
        the installer's default for a group not chosen yet."""
        flags = self.flags_before(step) if flags is None else flags
        chosen = self.selections.get(step, {})
        return {g: list(chosen[g]) if g in chosen else self.default(step, g, flags)
                for g in range(len(self.module.steps[step].groups))}

    def problem(self, step: int, group: int, options: Sequence[int]) -> str | None:
        """Why choosing *options* in a group isn't allowed (None: it is)."""
        g = self.module.steps[step].groups[group]
        options = sorted(set(options))
        if any(not 0 <= o < len(g.options) for o in options):
            return "no such option"
        flags = self.flags_before(step)
        kinds = [o.type.resolve(flags, self.file_state) for o in g.options]
        usable = [i for i, k in enumerate(kinds) if k is not OptionType.NOT_USABLE]
        name = g.name or "this group"
        for o in options:
            if kinds[o] is OptionType.NOT_USABLE:
                return f"{g.options[o].name!r} can't be used here"
        for i, k in enumerate(kinds):
            if k is OptionType.REQUIRED and i not in options:
                return f"{g.options[i].name!r} is required"
        if not usable:
            return None  # nothing to choose from: any (empty) choice
        if g.type is GroupType.EXACTLY_ONE and len(options) != 1:
            return f"choose exactly one option in {name}"
        if g.type is GroupType.AT_MOST_ONE and len(options) > 1:
            return f"choose at most one option in {name}"
        if g.type is GroupType.AT_LEAST_ONE and not options:
            return f"choose at least one option in {name}"
        if g.type is GroupType.ALL and options != usable:
            return f"every option in {name} is installed"
        return None

    def choose(self, step: int, group: int, options: Sequence[int]) -> None:
        why = self.problem(step, group, options)
        if why is not None:
            raise FomodError(why)
        self.selections.setdefault(step, {})[group] = sorted(set(options))

    def step_problems(self, step: int) -> list[str]:
        out = []
        for g, chosen in self.selection(step).items():
            why = self.problem(step, g, chosen)
            if why is not None:
                out.append(why)
        return out

    # -- remembered choices

    def choices(self) -> list[dict]:
        """The choices made in the visible steps, by name, to remember with
        the mod (and offer again when it is updated)."""
        out = []
        for i in self.visible_steps():
            step = self.module.steps[i]
            out.append({"step": step.name, "groups": [
                {"group": step.groups[g].name, "options": [step.groups[g].options[o].name for o in chosen]}
                for g, chosen in self.selection(i).items()]})
        return out

    def apply_choices(self, choices: Iterable[dict]) -> list[str]:
        """Choose what *choices* (from :meth:`choices`, maybe of an earlier
        version of the mod) names, where those steps, groups and options
        still exist and the choice is allowed; returns what couldn't be used."""
        skipped = []
        remembered = list(choices or [])
        used: set[int] = set()
        for entry in remembered:
            if not isinstance(entry, dict):
                continue
            index = next((i for i, s in enumerate(self.module.steps)
                          if s.name == entry.get("step") and i not in used), None)
            if index is None:
                skipped.append(f"step {entry.get('step')!r}")
                continue
            used.add(index)
            step = self.module.steps[index]
            for gentry in entry.get("groups") or ():
                if not isinstance(gentry, dict):
                    continue
                g = next((j for j, grp in enumerate(step.groups) if grp.name == gentry.get("group")), None)
                if g is None:
                    skipped.append(f"group {gentry.get('group')!r}")
                    continue
                names = [n for n in gentry.get("options") or () if isinstance(n, str)]
                options = [o for o, opt in enumerate(step.groups[g].options) if opt.name in names]
                if len(options) != len(set(names)) or self.problem(index, g, options) is not None:
                    skipped.append(f"the choice in {gentry.get('group')!r}")
                    continue
                self.selections.setdefault(index, {})[g] = options
        return skipped

    # -- the result

    def flags(self) -> dict[str, str]:
        return self.flags_before(len(self.module.steps))

    def unmet_dependencies(self) -> str | None:
        """What the installer needs that isn't there (None: nothing)."""
        dep = self.module.dependencies
        if dep is None or dep.holds({}, self.file_state):
            return None
        return dep.describe()

    def outcome(self, files: Iterable[str]) -> "Outcome":
        """The files to install for the current choices. *files* are the
        extracted files (relative to the extraction folder)."""
        flags = self.flags()
        items: list[FileItem] = list(self.module.required)
        chosen_names = []
        for i, step in enumerate(self.module.steps):
            visible = self.is_visible(i)
            before = self.flags_before(i) if visible else {}
            selection = self.selection(i, before) if visible else {}
            for g, group in enumerate(step.groups):
                for o, option in enumerate(group.options):
                    picked = o in selection.get(g, ())
                    usable = visible and option.type.resolve(before, self.file_state) is not OptionType.NOT_USABLE
                    if picked:
                        items.extend(option.files)
                        chosen_names.append(option.name)
                    else:
                        items.extend(f for f in option.files
                                     if f.always_install or (f.install_if_usable and usable))
        for condition, conditional_files in self.module.conditional:
            if condition.holds(flags, self.file_state):
                items.extend(conditional_files)
        mappings, warnings = resolve_files(items, self.module.root, files)
        return Outcome(mappings, warnings, flags, chosen_names)


@dataclass
class Outcome:
    #: Source (relative to the extraction folder) -> "Data/<destination>".
    mappings: list[FileMapping]
    warnings: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    #: Names of the options chosen, in order.
    chosen: list[str] = field(default_factory=list)


def _folded(path: str) -> str:
    """Case-folded like Windows, keeping the length (so a prefix of the folded
    path is the folded prefix)."""
    return "".join(c.upper() if len(c.upper()) == 1 else c for c in path)


def _destination(dest: str) -> str | None:
    """A destination relative to Data, or None when it would leave it."""
    parts = [p for p in dest.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or re.match(r"^[A-Za-z]:", dest):
        return None
    return "/".join(parts)


def resolve_files(items: Iterable[FileItem], root: str, files: Iterable[str]) -> tuple[list[FileMapping], list[str]]:
    """The mappings for *items* (sources relative to *root*, destinations to
    Data) against the extracted *files*. Of two entries installing to the same
    path, the higher priority wins, then the later one."""
    index: dict[str, str] = {}
    for f in files:
        index.setdefault(_folded(f), f)
    keys = sorted(index)  # a folder's files are one run of these
    prefix = f"{root}/" if root else ""
    warnings: list[str] = []
    entries: dict[str, tuple[int, int, str, str]] = {}  # folded dest -> (priority, seq, source, dest)
    seq = 0

    def add(source: str, dest: str, priority: int) -> None:
        nonlocal seq
        seq += 1
        key = _folded(dest)
        current = entries.get(key)
        if current is None or (priority, seq) >= current[:2]:
            entries[key] = (priority, seq, source, dest)

    for item in items:
        if any(p == ".." for p in item.source.split("/")):
            warnings.append(f"{item.source!r} is outside the mod; not installed")
            continue
        wanted = item.destination if item.destination is not None else item.source
        dest = _destination(wanted)
        if dest is None:
            warnings.append(f"{item.source!r} would be installed outside Data ({wanted!r}); not installed")
            continue
        if item.is_folder:
            folder = _folded(prefix + item.source).rstrip("/")
            start = f"{folder}/" if folder else ""
            found = []
            for key in keys[bisect.bisect_left(keys, start):]:
                if not key.startswith(start):
                    break
                found.append((key, index[key]))
            found = [(k, a) for k, a in found
                     if not (not item.source and k[len(start):].startswith("FOMOD/"))
                     and not any(_is_junk(part) for part in a.split("/"))]
            if not found:
                warnings.append(f"the folder {item.source!r} is not in the archive")
                continue
            for _key, actual in found:
                rest = actual[len(start):]
                add(actual, f"{dest}/{rest}" if dest else rest, item.priority)
        else:
            actual = index.get(_folded(prefix + item.source))
            if actual is None:
                warnings.append(f"the file {item.source!r} is not in the archive")
                continue
            name = actual.rsplit("/", 1)[-1]
            if item.destination is not None and (not dest or item.destination.replace("\\", "/").endswith("/")):
                dest = f"{dest}/{name}" if dest else name
            add(actual, dest, item.priority)
    mappings = [FileMapping(source, f"Data/{dest}") for _p, _s, source, dest in
                sorted(entries.values(), key=lambda e: e[3].casefold())]
    return mappings, warnings
