"""Make the README's screenshots from the real app, with made-up demo mods.

    QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py docs/screenshots [HOME]

Everything happens in a demo home folder: a temporary one, or HOME, which
must not exist yet (the paths in the pictures show it, so /home/player
reads better than /tmp/...). It gets a GOG Fallout 4 in a Wine prefix and
invented mods installed with 'bethmm install'. Your own home folder, games
and mods are never read or touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1280, 760


def main(out_dir: str, home_dir: str | None = None) -> int:
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if home_dir is not None:
        home = Path(home_dir).resolve()
        home.mkdir(parents=True)  # refuses a folder that exists: it must be a new, empty demo home
    else:
        home = Path(tempfile.mkdtemp(prefix="bethmm-screenshots-"))
    work = home / "Downloads"
    work.mkdir()
    # Before anything of the app is imported: it must only ever see the demo,
    # with its settings and mods in the usual places under the demo home.
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "BETHMM_CONFIG_DIR", "BETHMM_DATA_DIR"):
        os.environ.pop(var, None)
    os.environ.update(HOME=str(home), USER="player")
    os.chdir(work)
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtWidgets import QApplication, QSplitter

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Inter", 10))

    from bethmm import fomod, service, staging
    from bethmm.cli import main as bethmm
    from bethmm.discovery import get_games
    from bethmm.gui.dialogs import FomodDialog, InstallDialog
    from bethmm.gui.main_window import MainWindow
    from bethmm.settings import load_settings
    from helpers import make_plugin, make_tree

    def plugin(*masters: str) -> bytes:
        return make_plugin(masters or ("Fallout4.esm",), author="demo")

    def archive(name: str, entries: dict) -> Path:
        path = work / name
        with zipfile.ZipFile(path, "w") as zf:
            for rel, data in entries.items():
                zf.writestr(rel, data)
        return path

    def wait(ms: int = 300) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def shoot(widget, name: str) -> None:
        widget.show()
        wait()
        widget.grab().save(str(out / name))
        print("wrote", out / name)

    # ------------------------------------------------------------ the demo game
    prefix = home / "Games" / "fallout-4"
    make_tree(prefix, ["system.reg", "user.reg", "drive_c/users/player/AppData/Local/"])
    game_root = prefix / "drive_c" / "GOG Games" / "Fallout 4 GOTY"
    make_tree(game_root, {
        "Fallout4.exe": b"exe", "goggame-1998527297.info": b"{}",
        "Data/Fallout4.esm": make_plugin(()), "Data/DLCRobot.esm": plugin(), "Data/DLCCoast.esm": plugin(),
        "Data/Fallout4 - Textures1.ba2": b"ba2", "Data/textures/armor/leather.dds": b"vanilla",
    })
    assert bethmm(["use", "fallout4"]) == 0

    # ------------------------------------------------------------ demo mods (all invented)
    mods = [
        ("Wasteland Weather Overhaul-41001-2-3-1700000000.zip",
         {"WastelandWeather.esp": plugin(), "textures/sky/clouds01.dds": b"w", "textures/sky/clouds02.dds": b"w"}),
        ("Scavengers Armor Pack-41002-1-4-1700000000.zip",
         {"Scavengers Armor Pack/ScavengersArmor.esp": plugin(),
          "Scavengers Armor Pack/Textures/Armor/leather.dds": b"s",
          "Scavengers Armor Pack/Textures/Armor/scavenger_d.dds": b"s",
          "Scavengers Armor Pack/Meshes/Armor/scavenger.nif": b"s",
          "Scavengers Armor Pack/readme.txt": b"hi"}),
        ("Scavengers Armor 4K Retexture-41003-1-0-1700000000.zip",
         {"textures/armor/scavenger_d.dds": b"4k", "textures/armor/leather.dds": b"4k"}),
        ("Better Settlement Lights-41004-3-0-1700000000.zip",
         {"BetterSettlementLights.esp": plugin("Fallout4.esm", "DLCRobot.esm"), "meshes/lights/lamp.nif": b"l"}),
        ("Armor and Lights Patch-41005-1-1-1700000000.zip",
         {"ArmorLightsPatch.esp": plugin("Fallout4.esm", "ScavengersArmor.esp", "BetterSettlementLights.esp")}),
        # Needs a DLC this demo game doesn't have: the Plugins tab shows it in red.
        ("Nuka Park Raiders-41008-0-9-1700000000.zip",
         {"NukaParkRaiders.esp": plugin("Fallout4.esm", "DLCNukaWorld.esm")}),
    ]
    for name, entries in mods:
        assert bethmm(["install", str(archive(name, entries)), "--yes"]) == 0, name
    assert bethmm(["plugins", "disable", "BetterSettlementLights.esp"]) == 0
    settings = load_settings()

    # ------------------------------------------------------------ the main window
    window = MainWindow(settings)
    window.run_dialog = lambda dialog: True  # nothing here waits for a person
    window.ask = lambda *a, **k: True
    window.tell = lambda *a, **k: None
    window.resize(WIDTH, HEIGHT)
    window.show()
    wait()
    window.set_plugin_active("BetterSettlementLights.esp", True)  # something for the Messages tab
    while window.busy:
        wait(100)
    row = next(i for i, r in enumerate(window.model.rows) if r.mod.name == "Scavengers Armor 4K Retexture")
    window.table.selectRow(row)
    window.details.setCurrentIndex(1)  # Conflicts
    top = next(sp for sp in window.findChildren(QSplitter) if sp.orientation() == Qt.Orientation.Horizontal)
    top.setSizes([WIDTH * 9 // 20, WIDTH * 11 // 20])
    wait()
    window.conflicts_tree.resizeColumnToContents(0)
    shoot(window, "main-window.png")

    window.lists.setCurrentIndex(1)  # Plugins
    top.setSizes([WIDTH * 13 // 20, WIDTH * 7 // 20])
    wait()
    for column in range(window.plugin_model.columnCount() - 1):
        window.plugin_table.resizeColumnToContents(column)
    shoot(window, "plugins-tab.png")
    window.lists.setCurrentIndex(0)

    # ------------------------------------------------------------ the install dialog
    games, _ = get_games(settings)
    new_mod = archive("Raider Outfits Expanded-41006-2-0-1700000000.zip", {
        "Raider Outfits Expanded/RaiderOutfits.esp": plugin(),
        "Raider Outfits Expanded/Textures/Armor/leather.dds": b"r",
        "Raider Outfits Expanded/Textures/Armor/raider_spikes_d.dds": b"r",
        "Raider Outfits Expanded/Meshes/Armor/raider_spikes.nif": b"r",
        "Raider Outfits Expanded/Readme.txt": b"hi",
    })
    prepared = staging.prepare(new_mod, installed_games=[g.game_id for g in games])
    try:
        dialog = InstallDialog(prepared, games, settings, current="fallout4", run_dialog=lambda d: True)
        dialog.resize(WIDTH - 60, HEIGHT - 80)
        dialog.tabs.setCurrentIndex(0)
        dialog.files_tree.expandAll()
        shoot(dialog, "install-dialog.png")
        dialog.close()
    finally:
        staging.discard(prepared)

    # ------------------------------------------------------------ a FOMOD installer
    picture = QImage(480, 270, QImage.Format.Format_RGB32)
    painter = QPainter(picture)
    for y in range(270):
        painter.setPen(QColor(40 + y // 6, 55 + y // 5, 70 + y // 4))
        painter.drawLine(0, y, 480, y)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Inter", 40, QFont.Weight.Bold))
    painter.drawText(picture.rect(), Qt.AlignmentFlag.AlignCenter, "4K")
    painter.end()
    image_path = work / "4k.png"
    picture.save(str(image_path))
    config = """<?xml version="1.0" encoding="utf-8"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <moduleName>Commonwealth Foliage</moduleName>
  <requiredInstallFiles><folder source="00 Core" destination="" /></requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Textures">
      <optionalFileGroups order="Explicit">
        <group name="Texture resolution" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="2K">
              <description>Sharp textures that run well on most graphics cards (4 GB of video memory).</description>
              <files><folder source="01 Textures/2K" destination="" /></files>
              <typeDescriptor><type name="Recommended" /></typeDescriptor>
            </plugin>
            <plugin name="4K">
              <description>Twice the resolution for close-ups. Needs a graphics card with 8 GB of video memory or more.</description>
              <image path="fomod/images/4k.png" />
              <files><folder source="01 Textures/4K" destination="" /></files>
              <typeDescriptor><type name="Optional" /></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Extras" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Denser grass">
              <description>More grass in the wilderness.</description>
              <files><folder source="02 Extras/Grass" destination="" /></files>
              <typeDescriptor><type name="Optional" /></typeDescriptor>
            </plugin>
            <plugin name="Autumn leaves">
              <description>Red and orange trees.</description>
              <files><folder source="02 Extras/Autumn" destination="" /></files>
              <typeDescriptor><type name="Optional" /></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Patches">
      <optionalFileGroups order="Explicit">
        <group name="Patches" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Better Settlement Lights patch">
              <description>Makes the two mods work together.</description>
              <files><file source="03 Patches/FoliageLightsPatch.esp" destination="FoliageLightsPatch.esp" /></files>
              <typeDescriptor><type name="Optional" /></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>"""
    foliage = archive("Commonwealth Foliage-41007-1-0-1700000000.zip", {
        "fomod/info.xml": "<fomod><Name>Commonwealth Foliage</Name><Author>Example Modder</Author>"
                          "<Version>1.0</Version></fomod>",
        "fomod/ModuleConfig.xml": config,
        "fomod/images/4k.png": image_path.read_bytes(),
        "00 Core/CommonwealthFoliage.esp": plugin(),
        "01 Textures/2K/textures/landscape/trees/oak.dds": b"2k",
        "01 Textures/4K/textures/landscape/trees/oak.dds": b"4k",
        "02 Extras/Grass/meshes/landscape/grass.nif": b"g",
        "02 Extras/Autumn/textures/landscape/trees/oak_autumn.dds": b"a",
        "03 Patches/FoliageLightsPatch.esp": plugin("Fallout4.esm", "BetterSettlementLights.esp"),
    })
    prepared = staging.prepare(foliage, installed_games=[g.game_id for g in games])
    try:
        game = next(g for g in games if g.game_id == "fallout4")
        wizard = fomod.Wizard(service.fomod_module(prepared), service.fomod_file_state(game))
        dialog = FomodDialog(wizard, prepared)
        dialog.resize(WIDTH - 320, HEIGHT - 180)
        dialog.show()
        wait()
        four_k = dialog.widgets[0][1]
        dialog._describe(dialog._hover[four_k])  # as when pointing at "4K"
        dialog.widgets[1][1].setChecked(True)
        shoot(dialog, "fomod-installer.png")
        dialog.close()
    finally:
        staging.discard(prepared)

    window.tasks.wait()
    window.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
