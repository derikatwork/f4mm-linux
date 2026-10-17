# BETH Mod Manager

A mod manager for Bethesda games (Fallout 4, Skyrim Special Edition, Fallout: New
Vegas, Fallout 3, Oblivion, Starfield, Skyrim LE) installed through **Lutris/Wine**
on Linux. You pick an archive you downloaded from Nexus Mods with "Manual
Download"; the app works out which game it is for and where its files belong,
and (from phase 2 on) installs it by linking files into the game folder, so the
game folder can always be returned to vanilla.

## Status

The project is built in phases:

| Phase | What | State |
|---|---|---|
| 1 | Game discovery, placement rules, tests, read-only CLI | **done** |
| 2 | Staging, deploy/undeploy with symlinks, manifest, CLI | next |
| 3 | Desktop GUI (PySide6) | |
| 4 | FOMOD installer, plugins.txt management | |

**Phase 1 never changes your game folders.** Every command in this phase only reads.

## Install (Ubuntu and Ubuntu-based: Mint, Pop!_OS, Zorin, ...)

```bash
sudo apt install git                      # if you don't have it yet
git clone https://github.com/derikatwork/f4mm-linux.git
cd f4mm-linux
./install.sh
```

`install.sh` (run it as your normal user, not with sudo):

1. checks for Python 3.11+ and the `7z` program and offers to install what is
   missing with `apt` — the Python venv module, `7zip` and `7zip-rar` for `.rar`
   files (`p7zip-full`/`p7zip-rar` on 22.04-based releases, plus `python3.11` there
   because they ship Python 3.10). It asks before running `sudo apt`;
2. creates a private Python environment in `~/.local/share/beth-mod-manager/venv`
   with the app installed from this folder;
3. puts a `bethmm` command in `~/.local/bin` (Ubuntu adds that folder to your
   `PATH` at login once it exists — log out and back in after the first install).

Keep the cloned folder: the app runs from it. To update, `git pull && ./install.sh`.

Options: `--yes` (don't ask), `--dev` (also install the test tools and run the
tests), `--no-system-packages` (never call apt), `--uninstall` (removes the app
and the `bethmm` command; your mods, backups and settings stay — it refuses while
mods are still deployed in a game folder).

`.rar` support comes from `7zip-rar`, which lives in Ubuntu's multiverse repository.
If the script says it isn't available: `sudo add-apt-repository multiverse` and run
it again. `.zip` and `.7z` work without it.

<details>
<summary>Manual install (what the script does)</summary>

```bash
sudo apt install python3-venv 7zip 7zip-rar          # 22.04-based: python3.11 python3.11-venv p7zip-full p7zip-rar
python3 -m venv ~/.local/share/beth-mod-manager/venv  # 22.04-based: python3.11 -m venv ...
~/.local/share/beth-mod-manager/venv/bin/pip install -e .
mkdir -p ~/.local/bin
ln -s ~/.local/share/beth-mod-manager/venv/bin/bethmm ~/.local/bin/bethmm
```
</details>

## Using phase 1

### Find your games

```bash
bethmm games            # uses the cached result after the first run
bethmm games --rescan   # search again
```

Games are found by:

* scanning `~/Games` (Lutris's default install folder) for `Fallout4.exe`,
  `SkyrimSE.exe`, `TESV.exe`, `FalloutNV.exe`, `Fallout3.exe`, `Oblivion.exe` and
  `Starfield.exe`;
* reading Lutris's own records — `~/.local/share/lutris/games/*.yml`,
  `~/.config/lutris/games/*.yml`, `~/.local/share/lutris/pga.db`, and the same
  files of the **Flatpak** Lutris under `~/.var/app/net.lutris.Lutris/` — so games
  installed elsewhere are found too.

For each game it shows the game folder, Data folder, Wine prefix, Wine user, the
`plugins.txt` location inside the prefix (GOG Skyrim SE uses
`AppData/Local/Skyrim Special Edition GOG`), and the store it detected.

If something is wrong or missing, set it by hand:

```bash
bethmm override fallout4 --root "~/Games/fallout-4/drive_c/GOG Games/Fallout 4 GOTY"
bethmm override fallout4 --prefix ~/Games/fallout-4
bethmm override skyrimse --ignore          # hide a game
bethmm override fallout4 --clear           # back to automatic
bethmm config --games-dir /mnt/games       # scan a different Lutris folder
```

### See where a mod would go

```bash
bethmm analyze ~/Downloads/"Some Mod-1234-1-2-1700000000.7z"
bethmm analyze ~/Downloads/SomeMod.zip --game fallout4   # check against a specific game
bethmm analyze ~/Downloads/SomeMod.zip --json
```

This extracts the archive to a temporary folder inside
`~/.local/share/beth-mod-manager/tmp`, then prints:

* the mod name, version and Nexus mod ID taken from the file name;
* which game it is for, how sure it is, and why (plugin masters, script extender
  loader names, ...);
* which rule placed it (Data folder / Data content / game-root content / FOMOD /
  unknown) and how confident that is;
* every file's destination **in your real game folder**, with case matched to the
  folders that already exist (a mod's `Textures/` goes into your existing
  `textures/`), and which files would replace vanilla files;
* files that won't be installed (readmes, screenshots, script extender source code).

The temporary extraction is deleted afterwards.

When the layout isn't recognised (or the archive is a FOMOD installer), the mod's
files are shown as a tree together with the folders that look installable. Preview
your own choice of root folder and target — still without changing anything:

```bash
bethmm analyze SomeMod.7z --root "00 Core" --root "01 Optional" --target data
bethmm analyze SomeMod.7z --target game     # detected root, but into the game folder
```

`--root` can be given several times (later ones win where files overlap);
`--target` is `data` (the Data folder) or `game` (next to the game's `.exe`).
Without `--target`, each root's place is detected on its own.

### Look inside a plugin

```bash
bethmm plugin-info SomeMod.esp
```

Prints the plugin's masters, header version and the game it was made for.

## Where things are kept

| What | Where |
|---|---|
| Settings | `~/.config/beth-mod-manager/settings.json` |
| Game discovery cache | `~/.local/share/beth-mod-manager/games.json` |
| Log of every file operation (settings/cache writes, extractions, temp folders) | `~/.local/share/beth-mod-manager/logs/bethmm.log` |
| Staged mods (phase 2) | `~/.local/share/beth-mod-manager/mods/<game>/<mod>/` |
| Backups of replaced vanilla files (phase 2) | `~/.local/share/beth-mod-manager/backups/<game>/` |

## Running the tests

```bash
./install.sh --dev        # installs pytest into the app's environment and runs the tests
# or, afterwards:
~/.local/share/beth-mod-manager/venv/bin/python -m pytest
```

The tests build fake game folders, fake Lutris configs and fake mod layouts in
temporary directories; they never touch your real home folder or games.

## How mods are placed

The rules live in `bethmm/placement.py` and are applied in order:

1. **Wrapper folders** — a single unrecognised top-level folder such as
   `CoolMod v1.2/` is stepped into (repeatedly). Loose readmes next to it are ignored.
2. **FOMOD** — `fomod/ModuleConfig.xml` means a guided installer (phase 4); for now
   you pick which sub-folders to install.
3. **Data folder** — a `Data/` folder at the root means the root is the game folder
   (typical for script extenders: loader `.exe` + `.dll`s + `Data/`).
4. **Data content** — plugins (`.esp/.esm/.esl`), archives (`.ba2/.bsa`) or known Data
   folders (`meshes`, `textures`, `scripts`, `F4SE`, `SKSE`, `MCM`, ...) mean the root
   goes into `Data/`.
5. **Game-root content** — `.exe`/`.dll` files, ENB/ReShade files and folders mean the
   root goes next to the game's `.exe`.
6. **Unknown** — you choose (`--root`/`--target`); the app shows the file tree and
   suggests folders inside the archive that look installable.

If a root has both Data content and game-root-only files, rule 4 wins but the
confidence is marked low and the odd files are listed so you can correct it.

The game is identified from the masters listed in the plugins' headers
(`Fallout4.esm` → Fallout 4, `Skyrim.esm` + form version 44 → Skyrim SE, ...), then
script extender loaders and folders, and finally by which games you have installed.
You always confirm.
