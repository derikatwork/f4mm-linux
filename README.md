# BETH Mod Manager

A mod manager for Bethesda games (Fallout 4, Skyrim Special Edition, Fallout: New
Vegas, Fallout 3, Oblivion, Starfield, Skyrim LE) installed through **Lutris/Wine**
on Linux. You pick an archive you downloaded from Nexus Mods with "Manual
Download"; the app works out which game it is for and where its files belong,
and installs it by linking its files into the game folder, so the game folder can
always be returned to vanilla. It has a desktop app and a `bethmm` command; both
work on the same mods and records.

## Status

The project is built in phases:

| Phase | What | State |
|---|---|---|
| 1 | Game discovery, placement rules, tests, read-only CLI | **done** |
| 2 | Staging, deploy/undeploy with symlinks, manifest, CLI | **done** |
| 3 | Desktop GUI (PySide6) | **done** |
| 4 | FOMOD installer, plugins.txt management | **done** |

## Install (Ubuntu and Ubuntu-based: Mint, Pop!_OS, Zorin, ...)

```bash
sudo apt install git                      # if you don't have it yet
git clone https://github.com/derikatwork/f4mm-linux.git
cd f4mm-linux
./install.sh
```

`install.sh` (run it as your normal user, not with sudo):

1. checks for Python 3.11+, the `7z` program and the libraries the GUI's Qt
   toolkit needs, and offers to install what is missing with `apt` — the Python
   venv module, `7zip` and `7zip-rar` for `.rar` files (`p7zip-full`/`p7zip-rar` on
   22.04-based releases, plus `python3.11` there because they ship Python 3.10),
   and usually `libxcb-cursor0`. It asks before running `sudo apt`;
2. creates a private Python environment in `~/.local/share/beth-mod-manager/venv`
   with the app and Qt (PySide6, about 100 MB to download the first time)
   installed from this folder;
3. puts the `bethmm` and `bethmm-gui` commands in `~/.local/bin` (Ubuntu adds that
   folder to your `PATH` at login once it exists — log out and back in after the
   first install) and a **BETH Mod Manager** entry in your applications menu.

Keep the cloned folder: the app runs from it. To update, `git pull && ./install.sh`.

### First steps

1. Open **BETH Mod Manager** and check that your game is in the list at the top
   (if not: *Game ▸ Game settings*, or `bethmm games` and `bethmm override`).
2. Run *Game ▸ Check links* once per game: it tells you whether Wine sees the
   app's links, or whether that game needs hardlinks (see
   [Does the game see the links?](#does-the-game-see-the-links)).
3. Drag a mod archive from `~/Downloads` onto the window and click **Install**.
4. Fallout 4 only: set the INI lines shown under
   [Plugins and load order](#plugins-and-load-order) once, or the game ignores mods.

Not sure yet? Switch on dry-run mode (*Settings*, or `bethmm config --dry-run on`)
and the app only shows what it would do.

Options: `--yes` (don't ask), `--dev` (also install the test tools and run the
tests), `--no-gui` (command line only: no Qt, no menu entry), `--no-system-packages`
(never call apt), `--uninstall` (removes the app, its commands and its menu entry;
your mods, backups and settings stay — it refuses while
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
sudo apt install libxcb-cursor0                      # for the GUI
python3 -m venv ~/.local/share/beth-mod-manager/venv  # 22.04-based: python3.11 -m venv ...
~/.local/share/beth-mod-manager/venv/bin/pip install -e '.[gui]'   # '.' alone: command line only
#   (tests: '.[gui,dev,gui-dev]', or '.[dev]' without the GUI)
mkdir -p ~/.local/bin
ln -s ~/.local/share/beth-mod-manager/venv/bin/bethmm ~/.local/bin/bethmm
ln -s ~/.local/share/beth-mod-manager/venv/bin/bethmm-gui ~/.local/bin/bethmm-gui
```
</details>

## The desktop app

Open **BETH Mod Manager** from your applications menu (or run `bethmm-gui`, or
`bethmm gui`). Pick the game at the top; the list shows its mods in order, and
**the lower a mod is in the list, the more conflicts it wins**.

* **Install**: *File ▸ Install...* (Ctrl+O), or drag archives from your file
  manager onto the window (`bethmm-gui ~/Downloads/mod.7z` opens straight into
  the install dialog). The dialog shows where every file would go — new, replacing
  an original game file (which is backed up), overriding another mod, or (for an
  update that keeps its place in the list) losing to a mod lower in the list —
  and, on its *Changes in the game folder* tab, exactly what would be linked and
  backed up. When the archive looks like a new version of a mod you have, the button says
  **Update**; you can install it as a new mod instead, or pick which mod it
  updates. If the app can't tell where an archive's files belong, tick *Choose the
  folders myself*, tick the folders to install and where they go, and click *Use
  this choice*.
* **FOMOD installers**: an archive with a guided installer (`fomod/ModuleConfig.xml`)
  opens it first: choose the options of each step (the description and picture
  of an option show when you point at it), then the install dialog shows exactly
  what your choices install. *Run its installer again...* there changes them.
  When you update such a mod, the choices you made last time are preselected.
* **Enable / disable**: tick or untick the box in front of a mod.
* **Order**: drag mods up and down (or Ctrl+Up / Ctrl+Down, or the *Mod* menu).
* Every change is applied to the game folder right away. If the game folder is
  missing or unusable, nothing is changed and the reason is shown. A file the app
  didn't make that is in a mod's way is left alone: that one file isn't linked,
  and the *Messages* tab says so.
* **Plugins** tab: the load order the game uses (later plugins win), which mod each
  plugin comes from, and plugins whose masters are missing or load after them (in
  red). Tick to switch a plugin on or off, drag to move it. See
  [Plugins and load order](#plugins-and-load-order).
* Select a mod to see its files (and which of them another mod overrides), its
  conflicts, and where it came from, with a link to its Nexus page. Right-click
  it to rename (F2), update it from an archive, open its folder or uninstall it (Del).
* *Game* menu: **Deploy** (Ctrl+D), **Purge** (back to vanilla, mods stay
  installed), **Verify** (shows what doesn't match the records or the mod list,
  with a *Repair* button), **Check links** (does Wine see the links?), *Update
  plugins.txt* (after you edited it by hand) and *Game settings* (game
  folder, Wine prefix, link mode, hide the game — and set up a game that wasn't
  found, or bring back a hidden one).
* *Settings*: where your Lutris games are (and how many folder levels to search),
  the `7z` program, and **dry-run mode**: every change then opens a preview of
  exactly what it would do in the game folder, and nothing is changed. Settings
  changed from the command line while the window is open are picked up; the
  window saves only what you changed in it, and never saves over a settings file
  it can't read (fix or delete the file first).
* The *Messages* tab at the bottom says what each action did; *History* lists the
  latest file operations.

The desktop app and the `bethmm` command use the same code and the same records,
so you can switch between them freely. Only one change runs at a time: while one
is running, another (from the command line or a second window) stops at once
with "another bethmm process is changing mods right now" and changes nothing.

If the GUI doesn't start, run `bethmm-gui` in a terminal to see why. An error
about the `xcb` platform plugin means a system library is missing:
`sudo apt install libxcb-cursor0` (or run `./install.sh` again, which checks for
the others too).

## Managing mods on the command line

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

An archive with a FOMOD installer runs it: in a terminal, `install` asks for the
options of each step (Enter keeps the preselected ones, `b` goes back, `q`
cancels); with `--yes`, or when not run in a terminal, it takes the choices made
when the mod being updated was installed, else the installer's defaults. Only the
files your choices install are kept. `--root`/`--target` skip the installer and
install the folders you name instead (also the way to install the rare old
installers written in C#, `fomod/script.cs`, which the app can't run).

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
bethmm verify             # compare the game folder with what the app recorded (and with the mod list)
bethmm verify --repair    # fix what is safe to fix, then redeploy (or purge again if purged)
bethmm status             # per game: mods, links, backups
bethmm status --deployed-only   # what the app still has in game folders (exit status 3 if anything)
bethmm history            # the latest file operations
```

`purge` keeps your mods installed; after a purge nothing is linked until you run
`bethmm deploy` again. Every operation is recorded in
`~/.local/share/beth-mod-manager/manifest.db` *before* it happens, so even if the app
is killed halfway, the next `deploy`/`purge` or `verify --repair` cleans up.
`verify` also says when the game folder isn't up to date with the mod list (the
app was stopped after saving a change but before deploying it); `verify --repair`
deploys it.

`verify --repair` removes stray links into the app's mods folder and half-made
links of an interrupted run, puts back unrecorded originals from the backups
folder, forgets records of things that are gone, moves files left by an
interrupted move to `backups/<game>/.superseded/`, and deletes mod files nothing
uses any more (the previous version of an updated mod once no link uses it, and
what an interrupted install or uninstall left). It never deletes a folder in the mods
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
If the folder the mods were deployed into is missing, empty, or its Data folder is
empty or links to nowhere, `purge` refuses and changes nothing — it may only be on
a drive that isn't mounted, even if another copy of the game is found (`verify`
also lists the app's links it finds in such a copy, without touching them). When that install is really gone,
`purge --forget-missing` forgets the app's links there and moves the originals it
had backed up to `backups/<game>/.superseded/` (nothing is deleted); this works
for a game that is no longer found, too.

### Dry run

Add `--dry-run` to `install`, `enable`, `disable`, `move`, `uninstall`, `deploy`,
`purge`, `verify --repair`, `check-links` or the `plugins` commands to see exactly which files would be
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

When the game's folder name has one of `& | < > ^ %` (Wine's `cmd` would read it as part
of a command), the check doesn't run and says so; test by hand then (install a small
mod, start the game, see if it shows up).

Hardlinks are real directory entries inside the game folder, so sandboxes see them,
but they only work when the game and `~/.local/share` are on the same disk; the app
checks this when you switch (and before every change) and tells you if they aren't.

## Plugins and load order

After every deploy and purge the app keeps the game's `plugins.txt` (in the Wine
prefix: `drive_c/users/<you>/AppData/Local/<game>/plugins.txt`) in line with the
plugins it put into `Data`:

* a newly installed mod's plugins are added, switched on, at the end (masters
  after the last master);
* when a mod is disabled or uninstalled (or the game purged), its plugins' lines
  are taken out — and remembered, with their place and whether they were on, so
  enabling the mod (or deploying again) puts them back exactly where they were;
* a plugin you switched off stays off; lines the app didn't add, comments, the
  file's encoding and line endings are left as they are;
* before every change, `plugins.txt` is copied to
  `~/.local/share/beth-mod-manager/plugins-backups/<game>/` (the first copy and the
  latest 30 are kept), and the new file replaces the old one in one step. When
  `plugins.txt` is a link to a file you keep elsewhere, that file is updated and
  the link stays.

If the app doesn't know the game's Wine prefix (or Wine user), it leaves
`plugins.txt` alone and says so when a deploy has plugins to add: set them in
*Game ▸ Game settings* or with `bethmm override <game> --prefix ... --wine-user ...`,
then run `bethmm plugins sync`.

```bash
bethmm plugins                       # the load order: on/off, which mod, missing masters
bethmm plugins disable "Some Plugin.esp"   # or its number, or part of its name
bethmm plugins enable 12
bethmm plugins move "Patch.esp" --after "Some Plugin.esp"   # --up, --down, --top, --bottom, --to N, --before X
bethmm plugins sync                  # bring plugins.txt in line now (e.g. after editing it by hand)
```

The ones that change something take `--dry-run`. Fallout 4, Skyrim SE and Starfield use the
`*Plugin.esp` format, where the order of the lines is the load order; their own
masters, DLCs and Creation Club content are loaded anyway and aren't listed. New
Vegas, Fallout 3, Oblivion and Skyrim LE list only active plugins (the game's own
masters included), and **load plugins in the order of their file dates**: the app
sets the dates of the plugins it installed so that they load in the order shown,
after all the others — it never changes the date of a file it didn't install, so
only its own plugins can be moved there.

Sorting with LOOT isn't built in yet; `bethmm plugins sort` is where a sorter will
plug in (see `SORTERS` in `bethmm/plugins.py`).

**Fallout 4** only loads the plugins in `plugins.txt` and loose files (textures,
meshes, ...) when these are set (the game's launcher doesn't add them; the app
doesn't edit INI files). The files are in the prefix under
`drive_c/users/<you>/Documents/My Games/Fallout4/` (`My Documents` with older Wine):

```ini
; Fallout4Prefs.ini
[Launcher]
bEnableFileSelection=1

; Fallout4Custom.ini
[Archive]
bInvalidateOlderFiles=1
sResourceDataDirsFinal=
```

## Finding games and looking inside archives

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

## Troubleshooting

* **A game isn't found**: `bethmm games --rescan`; if it's still missing, set it by
  hand with `bethmm override` or *Game ▸ Game settings* (see
  [Find your games](#find-your-games)).
* **A mod doesn't show up in the game**, check in this order:
  1. `bethmm verify` (*Game ▸ Verify*): is the game folder up to date with the mod list?
  2. `bethmm check-links <game>` (*Game ▸ Check links*): does Wine see the links? If
     not, switch the game to hardlinks (see
     [Does the game see the links?](#does-the-game-see-the-links)).
  3. Is its plugin switched on (*Plugins* tab, `bethmm plugins`) and loaded after its
     masters? Fallout 4 also needs the INI lines in
     [Plugins and load order](#plugins-and-load-order).
  4. Script extender plugins (`F4SE/`, `SKSE/` folders) only load when the game is
     started through the loader: in Lutris, set the game's executable
     (*Configure ▸ Game options*) to `f4se_loader.exe`, `skse64_loader.exe`, ...
* **The app (or the computer) stopped in the middle of a change**: run
  `bethmm verify --repair` (or *Game ▸ Verify*, then *Repair*). Every step is
  recorded before it happens, so it can be finished or undone.
* **"another bethmm process is changing mods right now"**: another window or command
  is making a change; only one runs at a time. Wait for it, then try again.
* **You want an older `plugins.txt` back**: copy one from
  `~/.local/share/beth-mod-manager/plugins-backups/<game>/` (named by date and time;
  `plugins-first.txt` is how it was before the app first changed it) over the
  game's `plugins.txt`, then run `bethmm plugins sync`.
* **Back to vanilla**: `bethmm purge` (*Game ▸ Purge*) removes every link and puts
  back every original game file; your mods stay installed for later.
* **What did it do?** `bethmm history` lists the latest file operations, and the log
  (`~/.local/share/beth-mod-manager/logs/bethmm.log`) has every one of them.
* **The desktop app doesn't start**: see the end of [The desktop app](#the-desktop-app).

## Known limitations

* **No LOOT sorting yet.** `bethmm plugins sort` is where a sorter will plug in.
  You can run LOOT yourself (it has a Linux version) and run `bethmm plugins sync`
  afterwards; the app keeps the order of the lines in `plugins.txt` that you or LOOT set.
* **FOMOD installers written in C#** (`fomod/script.cs`, rare and old) can't run:
  choose the folders to install yourself (*Choose the folders myself*, or
  `--root`/`--target`).
* **Load order by file date** (Oblivion, Fallout 3, New Vegas, Skyrim LE): the app
  only changes the dates of plugins it installed, so it can't move the game's own
  plugins or ones you put into `Data` yourself. It dates its plugins in the order of
  their lines in `plugins.txt`: a tool that changes only their dates, without
  rewriting `plugins.txt`, is undone at the next deploy.
* **No INI editing.** Fallout 4's lines (see [Plugins and load order](#plugins-and-load-order))
  and archive invalidation for loose files in the older games are yours to set.
* **Downloads are manual**: no "Mod Manager Download" (`nxm://` links) or Nexus
  login. Use "Manual Download" and install the archive.
* **One mod list per game**: no profiles.
* **Files that tools write into `Data`** (xEdit, BodySlide, ...) are real files the
  app doesn't manage; it leaves them alone.
* **Very large mods** (tens of thousands of files) take a while: every file is its
  own link, and the install dialog takes a few seconds to show such an archive.
* **Hardlinks** need the game and `~/.local/share` on the same disk.

## Where things are kept

| What | Where |
|---|---|
| Settings | `~/.config/beth-mod-manager/settings.json` |
| Game discovery cache | `~/.local/share/beth-mod-manager/games.json` |
| Log of every file operation (links, backups, restores, folders, installs, settings/cache writes, extractions) | `~/.local/share/beth-mod-manager/logs/bethmm.log` |
| Installed mods (staged files) | `~/.local/share/beth-mod-manager/mods/<game>/<mod>/` |
| Originals of game files replaced by mods | `~/.local/share/beth-mod-manager/backups/<game>/` |
| Copies of `plugins.txt`, made before each change | `~/.local/share/beth-mod-manager/plugins-backups/<game>/` |
| Manifest (mod list, every link/backup/folder the app made, plugins.txt lines it manages, history) | `~/.local/share/beth-mod-manager/manifest.db` |

## Running the tests

```bash
./install.sh --dev        # installs pytest (and pytest-qt for the GUI tests) and runs the tests
# or, afterwards:
~/.local/share/beth-mod-manager/venv/bin/python -m pytest
```

The tests build fake game folders, fake Lutris configs and fake mod layouts in
temporary directories; they never touch your real home folder or games.

## How mods are placed

The rules live in `bethmm/placement.py` and are applied in order:

1. **Wrapper folders** — a single unrecognised top-level folder such as
   `CoolMod v1.2/` is stepped into (repeatedly). Loose readmes next to it are ignored.
2. **FOMOD** — `fomod/ModuleConfig.xml` means a guided installer: it runs, and the
   files of the options you choose go into `Data/` (`bethmm/fomod.py`). Or you pick
   which sub-folders to install.
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
