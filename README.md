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
| 2 | Staging, deploy/undeploy with symlinks, manifest, CLI | **done** |
| 3 | Desktop GUI (PySide6) | next |
| 4 | FOMOD installer, plugins.txt management | |

Until phase 4, plugins (`.esp/.esm/.esl`) are linked into `Data` but **not** added to
`plugins.txt` — enable them in the game's launcher or edit `plugins.txt` by hand.

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
mods are still deployed in a game folder and shows what is left; run
`bethmm purge --all` first), `--force` (install even as root, or uninstall even
though mods are still deployed). If an update of your distro broke the app's
environment, running `./install.sh` again recreates it.

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

## Managing mods

```bash
bethmm use fallout4                                   # pick the game once (or add --game to each command)
bethmm install ~/Downloads/"Some Mod-1234-1-2-1700000000.7z"
bethmm list                                           # lower in the list = wins conflicts
bethmm disable "Some Mod"        # or its number from 'list', or part of its name
bethmm enable 3
bethmm move "Some Mod" --down    # --up, --top, --bottom, --to N
bethmm conflicts --files
bethmm uninstall "Some Mod"
```

`install` options: `--dry-run` (show exactly what would change in the game folder),
`--no-deploy` (add the mod disabled: nothing of it is linked until you `enable` it),
`--new` (install separately even if it looks like an update), `--replace MOD`
(install as the new version of that mod), `--name NAME` (the name in the list; on
an update it renames the mod), `--root`/`--target` (choose what goes where).

What `install` does:

1. extracts the archive into a temporary folder and works out the game and where
   each file goes (same rules as `analyze`; `--root`/`--target` to choose yourself);
2. shows the plan and asks you to confirm (`--yes` to skip, `--dry-run` to only look);
3. moves the files to `~/.local/share/beth-mod-manager/mods/<game>/<mod>/` and adds
   the mod at the bottom of the list (so it wins conflicts);
4. links every file into the game folder: one **symlink per file**, so several mods
   can share a folder. Folder names are matched case-insensitively (a mod's
   `Textures/` goes into your existing `textures/`), and new folders follow the
   game's naming style.

If a mod replaces an original game file, the original is moved to
`~/.local/share/beth-mod-manager/backups/<game>/` first and moved back when no
enabled mod provides that file any more.

Installing a newer version of a mod you already have updates it in place, keeping
its position and enabled state. An archive counts as a newer version when

* it has the same name as an installed mod (or as the archive that mod was
  installed from, so a mod you renamed still counts) and either comes from the
  same Nexus mod page or looks like the same mod: the same plugins, or at least
  half of its files. Two unrelated mods may both be called "2K" or "main", and a
  patch or retexture shares only a few files; or
* it comes from the same Nexus mod page and its Nexus file name is the same apart
  from version numbers ("Cool Mod 1.2" → "Cool Mod 1.3"). Other files of the same
  page — an optional patch, "PART 2" — are separate mods.

The plan says "This updates ..." before you confirm. `--new` installs it
separately instead; `--replace MOD` makes it the new version of a mod the rules
didn't match. `--name NAME` on an update renames the mod.

**The app never deletes or edits a file it didn't create.** If you (or a tool such as
xEdit or BethINI) replace one of its links with a real file, that file is left alone
and reported.

### Back to vanilla, checking and repairing

```bash
bethmm purge              # remove every link, restore every original, remove the folders it made
bethmm purge --all        # every game
bethmm deploy             # put the enabled mods back (also re-creates links that went missing)
bethmm verify             # compare the game folder with what the app recorded
bethmm verify --repair    # fix what is safe to fix, then redeploy (or purge again if purged)
bethmm status             # per game: mods, links, backups
bethmm status --deployed-only   # what the app still has in game folders (exit status 3 if anything)
bethmm history            # the latest file operations
```

`purge` keeps your mods installed; after a purge nothing is linked until you run
`bethmm deploy` again. Every operation is recorded in
`~/.local/share/beth-mod-manager/manifest.db` *before* it happens, so even if the app
is killed halfway, the next `deploy`/`purge` or `verify --repair` cleans up.

`verify --repair` removes stray links into the app's mods folder and half-made
links of an interrupted run, puts back unrecorded originals from the backups
folder, forgets records of things that are gone, moves files left by an
interrupted move to `backups/<game>/.superseded/`, and deletes the previous version
of an updated mod once no link uses it. It never deletes a folder in the mods
folder that the app didn't mark as its own — it only reports it. While the game
folder is missing or empty (say, on a drive that isn't mounted) it changes nothing.

When a game update (or anything else) puts a new file where a mod had replaced the
original, that file is left alone and the old original stays in the backups.
`verify` reports it; deleting the new file and running `purge` brings the old
original back, or `verify --repair --release-held` stops tracking the old original
(it is moved to `backups/<game>/.superseded/`, never deleted).

If you moved the game to another folder (and pointed the app there with
`bethmm override --root`), `purge` cleans up the new folder; deploy again
afterwards (the app checks that its links really are in the new folder). If you
installed the game again somewhere else, `purge` cleans up the old folder first.
If the folder the mods were deployed into is missing or empty, `purge` refuses
and changes nothing — it may only be on a drive that isn't mounted, even if
another copy of the game is found. When that install is really gone,
`purge --forget-missing` forgets the app's links there and moves the originals it
had backed up to `backups/<game>/.superseded/` (nothing is deleted); this works
for a game that is no longer found, too.

### Dry run

Add `--dry-run` to `install`, `enable`, `disable`, `move`, `uninstall`, `deploy`,
`purge`, `verify --repair` or `check-links` to see exactly which files would be
linked, backed up or restored without changing anything. (`verify --repair
--dry-run` lists the repairs; run `deploy --dry-run` after the repair for the links.)
`bethmm config --dry-run on` makes that the default for everything until you switch
it off.

### Does the game see the links?

```bash
bethmm check-links fallout4
```

puts a test file into the game's Data folder through a symlink and asks Wine
(the runner Lutris uses for that game) to read it, then removes it. Wine follows
symlinks normally. If the check fails — most likely with the **Flatpak** version of
Lutris, whose sandbox may not see `~/.local/share` — switch that game to hardlinks:

```bash
bethmm override fallout4 --link-mode hardlink
bethmm deploy
```

Hardlinks are real directory entries inside the game folder, so sandboxes see them,
but they only work when the game and `~/.local/share` are on the same disk; the app
checks this when you switch (and before every change) and tells you if they aren't.

## Using phase 1 commands

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
| Log of every file operation (links, backups, restores, folders, installs, settings/cache writes, extractions) | `~/.local/share/beth-mod-manager/logs/bethmm.log` |
| Installed mods (staged files) | `~/.local/share/beth-mod-manager/mods/<game>/<mod>/` |
| Originals of game files replaced by mods | `~/.local/share/beth-mod-manager/backups/<game>/` |
| Manifest (mod list, every link/backup/folder the app made, history) | `~/.local/share/beth-mod-manager/manifest.db` |

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
