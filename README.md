# BETH Mod Manager

A mod manager for Bethesda games on Linux: **Fallout 4, Skyrim Special Edition,
Fallout: New Vegas, Fallout 3, Oblivion, Starfield and Skyrim LE**, installed
through **Lutris** (GOG or any other store) and run with Wine.

## What it's for

Modding these games on Linux usually means one of two things: running a Windows
mod manager (Mod Organizer 2, Vortex) inside Wine, which is fiddly and fragile, or
unpacking mods into the game folder by hand, which is quick until you want to undo
it, update a mod, or work out which of 40 mods broke the game.

BETH Mod Manager is a native Linux app that does the bookkeeping for you:

* **Install a mod in one step.** Give it an archive you downloaded from Nexus Mods
  (with "Manual download"). It works out which game the mod is for and where its
  files go: into `Data`, next to the game's `.exe`, or through the mod's own
  installer (FOMOD).
* **Keep the game folder clean.** Mod files stay in the app's own folder and are
  *linked* into the game folder, one link per file. Removing a mod removes its
  links, and the game folder can always go back to exactly how it was.
* **Sort out conflicts.** When two mods have the same file, the one lower in your
  list wins. Drag a mod to change that; the game folder follows straight away.
* **Look after `plugins.txt`.** A new mod's plugins are switched on in the game's
  load order, and the app remembers their place when you turn a mod off.
* **Never break what isn't its own.** The app never deletes or edits a file it
  didn't create. A game file a mod replaces is backed up first and put back
  when the mod goes.
* **Show you first.** Every install shows exactly what will change in the game
  folder, and dry-run mode lets you try anything without changing a thing.

There is a desktop app and a `bethmm` command. Both work on the same mods and
records, so you can use whichever you like, or both.

## How it works

```
~/Downloads/Cool Armor-1234-1-0-1700000000.7z
        │  install: unpacked, checked, and its files stored in the app's folder
        ▼
~/.local/share/beth-mod-manager/mods/fallout4/Cool Armor/Data/CoolArmor.esp
        ▲
        │  deploy: a link per file in the game folder points back at the stored file
        │
~/Games/fallout-4/drive_c/GOG Games/Fallout 4/Data/CoolArmor.esp
```

The game sees an ordinary file. The app records every link, backup and folder it
makes, before it makes it, so it can always undo its changes, even after a
crash or a power cut.

---

## Walkthrough

This walks through setting up the app and installing your first mods with the
desktop app. The [command line](#using-the-command-line) section shows the same
steps as commands.

### 1. Install the app

On Ubuntu or an Ubuntu-based distro (Linux Mint, Pop!_OS, Zorin, ...), open a
terminal and run:

```bash
sudo apt install git                      # if you don't have it yet
git clone https://github.com/derikatwork/f4mm-linux.git
cd f4mm-linux
./install.sh
```

Run `install.sh` as your normal user, not with `sudo`. It asks for your password
if it needs to install something with `apt`:

1. It checks for Python 3.11+, the `7z` program that unpacks archives (with RAR
   support), and the libraries the desktop app needs, and offers to install
   whatever is missing.
2. It installs the app into its own environment in
   `~/.local/share/beth-mod-manager/venv`. The first time, this downloads the
   Qt toolkit (about 100 MB).
3. It adds a **BETH Mod Manager** entry to your applications menu, plus the
   `bethmm` and `bethmm-gui` commands in `~/.local/bin`. After the first install,
   log out and back in so your shell finds them.

**Keep the cloned folder:** the app runs from it. To update the app later, run
`git pull && ./install.sh` in that folder.

### 2. Open it and find your game

Open **BETH Mod Manager** from your applications menu. The app looks for your
games in `~/Games` (where Lutris installs them) and in Lutris's own records, so
games installed elsewhere are found too.

Pick your game in the **Game** list at the top. The status bar at the bottom of the
window shows the game folder the app found.

Is your game missing, or is the folder wrong? Open *Game ▸ Game settings...*: set
the **Game folder** (the one with the game's `.exe`) and the **Wine prefix** (the
folder that contains `drive_c`), then *Game ▸ Rescan for games*. If your Lutris
games live somewhere other than `~/Games`, set that in *Settings ▸ Settings...*.

### 3. Check that the game will see your mods (once per game)

Choose *Game ▸ Check links*. The app puts a test file into the game's `Data`
folder through a link, asks Wine (the same one Lutris uses for the game) to read
it, and removes it again.

* **It works:** nothing to do, the default (symlinks) is right for you.
* **It fails:** this is likely with the **Flatpak** version of Lutris, whose sandbox
  can't follow the links. Open *Game ▸ Game settings...*, set **Link mode** to
  `hardlink`, and run the check again. Hardlinks only work when the game and your
  home folder are on the same disk; the app tells you if they aren't.

### 4. Fallout 4 only: let the game load mods (once)

Fallout 4 ignores mods unless two INI files say otherwise, and the app doesn't edit
INI files. They are in the game's Wine prefix, under
`drive_c/users/<you>/Documents/My Games/Fallout4/` (`My Documents` with older
Wine). Add these lines, creating `Fallout4Custom.ini` if it isn't there:

```ini
; Fallout4Prefs.ini
[Launcher]
bEnableFileSelection=1

; Fallout4Custom.ini
[Archive]
bInvalidateOlderFiles=1
sResourceDataDirsFinal=
```

The other games load mods without this. For loose textures and meshes that
replace the ones in the game's `.bsa` archives, Fallout 3, New Vegas and Oblivion
need *archive invalidation*; see those games' modding guides.

### 5. Download a mod

On the mod's page on Nexus Mods, open **Files** and click **Manual download**.
"Mod manager download" needs a Nexus login, which the app doesn't do. Keep the
file name as Nexus gives it, e.g. `Cool Armor-1234-1-0-1700000000.7z`: the app reads
the mod's name, its Nexus page and its version from it. `.zip`, `.7z` and `.rar`
all work.

### 6. Install it

Drag the archive from your file manager onto the app's window, or use
*File ▸ Install...* (Ctrl+O). The install dialog shows:

* **Name**: how the mod appears in your list. Change it if you like.
* **Game**: which game the app thinks the mod is for, and why (for example, the
  plugin needs `Fallout4.esm`).
* **Goes into**: where its files go (the `Data` folder or the game folder), and how
  sure the app is.
* The **Files** tab: every file and where it goes. It says whether a file is new,
  replaces an original game file (which is backed up first), or overrides
  another mod. Readmes and similar files are not installed.
* The **Changes in the game folder** tab: exactly what will be linked and backed up.
* The **Notes** tab: anything worth knowing, such as files skipped or odd layouts.

Click **Install**. The mod goes to the bottom of your list, its files are linked
into the game folder, and its plugins are switched on in `plugins.txt`. The
**Messages** tab at the bottom says what was done.

If the app can't tell where a mod's files go, tick **Choose the folders myself**:
tick the folders of the archive to install, say whether they go into the Data
folder or the game folder, and click **Use this choice**.

An archive you already unpacked works too: *File ▸ Install extracted folder...*.

### 7. Mods with an installer (FOMOD)

Many larger mods come with a guided installer: options like "2K or 4K textures",
or patches for other mods you have. The app opens it first:

* Go through its steps and pick your options. Point at an option to see its
  description and picture. **Next** moves on, and **Back** goes back.
* Options are labelled when the installer marks them as required or recommended,
  or as unusable with what you have. The installer sees the files and active
  plugins in your game's `Data` folder (other mods' too), as it would in other mod
  managers.
* After the last step, the install dialog shows exactly the files your choices
  install. Only those files are kept. **Run its installer again...** there lets
  you change your mind.

When you update the mod later, the choices you made last time are already picked.

### 8. More mods: order and conflicts

Install more mods the same way. The **Mods** list is in priority order: **the lower
a mod is in the list, the more conflicts it wins.** A new mod goes to the bottom, so
it wins over everything you had.

* **Change the order:** drag a mod up or down, or use Ctrl+Up / Ctrl+Down, or the
  *Mod* menu (*Move to top* / *Move to bottom*).
* **See conflicts:** the **Conflicts** column counts the mods a mod wins over and loses
  to. Select a mod and open the **Conflicts** tab on the right to see which files.
* **See a mod's details:** the **Files** tab lists its files and which are overridden,
  and **Info** shows where it came from, with a link to its Nexus page.

Every change applies to the game folder right away. If a file you put in the game
folder yourself is in a mod's way, the app leaves it alone, skips that one file,
and says so in **Messages**.

### 9. Plugins and load order

The **Plugins** tab shows the load order the game uses (later plugins win), which mod
each plugin came from, and, in red, plugins whose masters are missing or load
after them.

* Tick or untick a plugin to switch it on or off. A plugin you switched off stays
  off, even when its mod is updated.
* Drag a plugin to move it in the load order.
* When you turn a mod off, its plugins are taken out of `plugins.txt` and their
  place is remembered. Turning the mod back on puts them back where they were.

The app keeps `plugins.txt` in the game's Wine prefix, at
`drive_c/users/<you>/AppData/Local/<game>/plugins.txt`. It backs the file up before
every change. If you edited it by hand, *Game ▸ Update plugins.txt* brings it back in
line with your mods.

Fallout: New Vegas, Fallout 3, Oblivion and Skyrim LE load plugins in the order of
their *file dates*. There the app dates the plugins it installed so they load in
the order shown, after the game's own. It never changes the date of a file it
didn't install, so only its own plugins can be moved.

The app doesn't sort plugins for you yet (LOOT isn't built in). You can run LOOT
yourself and then *Game ▸ Update plugins.txt*: the app keeps the order LOOT set.

### 10. Play

Start the game from Lutris as usual. The mods are in the game folder, so there is
nothing else to launch.

**Script extenders** (F4SE, SKSE, NVSE, ...) install like any other mod. To use one,
start the game through its loader: in Lutris, right-click the game, choose
*Configure ▸ Game options*, and set **Executable** to the loader (`f4se_loader.exe`,
`skse64_loader.exe`, ...).

### 11. Update a mod

Download the new version and install it as before. When the archive looks like a
new version of a mod you have, the install dialog says so and the button reads
**Update**. The mod keeps its place in the list and whether it is enabled, and
its old files are replaced. To pick which mod it updates, or to install it
separately, use **New mod or update** in the dialog.

You can also right-click a mod and choose *Update from archive...*.

### 12. Turn mods off, or remove them

* **Turn a mod off:** untick its box. Its links are removed, and any game files it
  replaced come back. Tick it again to turn it back on.
* **Remove a mod:** select it and press Del (or right-click ▸ *Uninstall...*). The
  app shows what will change before it does it, then deletes the mod's stored files.

Right-click a mod for the rest: *Rename...* (F2), *Open its folder*, *Open its Nexus
page*.

### 13. Back to vanilla

*Game ▸ Purge...* removes every link the app made and puts back every game file a
mod replaced. Your mods stay installed. *Game ▸ Deploy* (Ctrl+D) puts them all back.
Purge before you reinstall the game, or before you repair its files with the
store's tools.

### 14. Try things without changing anything (dry run)

Tick **Dry-run mode** in *Settings ▸ Settings...*. While it is on, every action shows
exactly what it would do in the game folder, and nothing is changed. **DRY-RUN
MODE** shows in the window. Untick it to go back to normal.

### 15. When something looks wrong

*Game ▸ Verify...* compares the game folder with what the app recorded: missing
links, links replaced by other files, originals not where they should be, and
mods that weren't deployed after a change. **Repair** fixes what is safe to fix
and deploys again.

Every step is recorded before it happens, so if the app or the computer stopped
in the middle of a change, *Verify* and **Repair** finish or undo it.

The **History** tab lists the latest file operations, and *Help ▸ Open the log
file* shows every one of them. See also [Troubleshooting](#troubleshooting).

---

## Using the command line

Everything the desktop app does can also be done with the `bethmm` command. Both
use the same mods and records, so you can switch between them at any time. Only
one change runs at a time: if another window or command is busy, `bethmm` stops
straight away with "another bethmm process is changing mods right now" and
changes nothing.

### The walkthrough as commands

```bash
bethmm games                         # 2. which games were found, and where
bethmm use fallout4                  #    pick the game once (or add --game to each command)
bethmm check-links fallout4          # 3. does Wine see the app's links?
bethmm install ~/Downloads/"Cool Armor-1234-1-0-1700000000.7z"   # 6. shows the plan, asks, installs
bethmm list                          # 8. your mods, in priority order (lower wins)
bethmm move "Cool Armor" --down      #    --up, --top, --bottom, --to N
bethmm conflicts --files             #    which mod wins which file
bethmm plugins                       # 9. the load order
bethmm disable "Cool Armor"          # 12. or its number from 'list', or part of its name
bethmm enable 3
bethmm uninstall "Cool Armor"
bethmm purge                         # 13. back to vanilla (mods stay installed)
bethmm deploy                        #     put them back
bethmm verify --repair               # 15. check, and fix what is safe to fix
```

Add `--dry-run` to any command that changes something to see exactly what it
would do.

### Installing

`bethmm install` unpacks the archive into a temporary folder, works out the game
and where each file goes, and shows the plan. With your OK, it moves the files to
`~/.local/share/beth-mod-manager/mods/<game>/<mod>/`, adds the mod at the bottom of
the list, and links every file into the game folder. Folder names are matched
regardless of case, so a mod's `Textures/` goes into your existing `textures/`,
and new folders follow the game's naming style.

Options:

* `--yes`: don't ask before installing.
* `--dry-run`: only show what would change.
* `--no-deploy`: add the mod turned off; nothing of it is linked until you `enable` it.
* `--name NAME`: the name in the list. On an update, it renames the mod.
* `--new`: install separately, even if it looks like an update.
* `--replace MOD`: install as the new version of that mod.
* `--root FOLDER` / `--target data|game`: choose yourself which folders of the
  archive to install and where they go. `--root` can be given several times.

**FOMOD installers in the terminal:** `install` asks for each step's options (Enter
keeps the preselected ones, `b` goes back, `q` cancels). With `--yes`, or when not
run in a terminal, it takes the choices made when the mod being updated was
installed, else the installer's defaults. `--root`/`--target` skip the installer
and install the folders you name. That's also the way to install the rare old
installers written in C# (`fomod/script.cs`), which the app can't run.

**When is it an update?** An archive counts as a new version of an installed mod
when one of these holds:

* It has the same name as the mod, or as the archive the mod was installed from (so
  a mod you renamed still counts), and either comes from the same Nexus page or
  looks like the same mod: the same plugins, or at least half of its files. Two
  unrelated mods may both be called "2K" or "main", and a patch or retexture
  shares only a few files.
* It comes from the same Nexus page, and its Nexus file name matches apart from
  version numbers ("Cool Mod 1.2" → "Cool Mod 1.3"). Other files on the same page,
  such as an optional patch or "PART 2", are separate mods.

The plan says "This updates ..." before you confirm.

### Back to vanilla, checking and repairing

```bash
bethmm purge              # remove every link, restore every original, remove the folders the app made
bethmm purge --all        # every game
bethmm deploy             # link the enabled mods again (also re-creates links that went missing)
bethmm verify             # compare the game folder with the records and the mod list
bethmm verify --repair    # fix what is safe to fix, then redeploy (or purge again, if purged)
bethmm status             # per game: mods, links, backups
bethmm status --deployed-only   # what the app still has in game folders (exit status 3 if anything)
bethmm history            # the latest file operations
```

After a purge, nothing is linked until you run `bethmm deploy` again. Every operation
is recorded in `~/.local/share/beth-mod-manager/manifest.db` *before* it happens,
so even if the app is killed halfway, the next `deploy`, `purge` or
`verify --repair` cleans up. `verify` also says when the game folder isn't up to
date with the mod list (the app was stopped after saving a change but before
deploying it), and `verify --repair` deploys it.

`verify --repair`:

* removes stray links into the app's mods folder, and half-made links of an
  interrupted run;
* puts back unrecorded originals from the backups folder, and forgets records of
  things that are gone;
* moves files left by an interrupted move to `backups/<game>/.superseded/`;
* deletes mod files nothing uses any more: the previous version of an updated mod
  once no link uses it, and what an interrupted install or uninstall left.

It never deletes a folder in the mods folder that the app didn't mark as its own;
it only reports it. While the game folder is missing or empty (say, on a drive
that isn't mounted), it changes nothing.

When a game update (or anything else) puts a new file where a mod had replaced the
original, that file is left alone and the old original stays in the backups.
`verify` reports it. Deleting the new file and running `purge` brings the old
original back. Or `verify --repair --release-held` stops tracking the old original:
it is moved to `backups/<game>/.superseded/`, never deleted.

**If you moved or reinstalled the game:**
* If you moved it and pointed the app at the new folder with
  `bethmm override --root`, `purge` cleans up the new folder; deploy again
  afterwards. If you installed the game again somewhere else, `purge` cleans up the
  old folder first.
* If the folder the mods were deployed into is missing or empty, or its Data folder
  is empty or links to nowhere, `purge` refuses and changes nothing: the folder may
  only be on a drive that isn't mounted. When that install really is gone,
  `purge --forget-missing` forgets the app's links there and moves the originals it
  had backed up to `backups/<game>/.superseded/`; nothing is deleted. The desktop
  app asks you the same question when you purge a game whose folder is gone.

### Dry run

Add `--dry-run` to `install`, `enable`, `disable`, `move`, `uninstall`, `deploy`,
`purge`, `verify --repair`, `check-links` or the `plugins` commands to see exactly
which files would be linked, backed up or restored, without changing anything.
`verify --repair --dry-run` lists the repairs; run `deploy --dry-run` after the
repair to see the links. `bethmm config --dry-run on` makes dry run the default for
everything (the desktop app too) until you switch it off.

### Does the game see the links?

```bash
bethmm check-links fallout4
bethmm override fallout4 --link-mode hardlink    # if it fails, then:
bethmm deploy
```

Wine follows symlinks normally. The check can fail with the Flatpak version of
Lutris, whose sandbox may not see `~/.local/share`. Hardlinks are real entries
inside the game folder, so sandboxes see them, but they only work when the game
and `~/.local/share` are on the same disk. The app checks this when you switch,
and before every change.

If the game's folder name contains one of `& | < > ^ %`, Wine's `cmd` would read
it as part of a command, so the check doesn't run and says so. Test by hand
instead: install a small mod, start the game, and see if it shows up.

### Plugins

```bash
bethmm plugins                                # the load order: on/off, which mod, missing masters
bethmm plugins disable "Some Plugin.esp"      # or its number, or part of its name
bethmm plugins enable 12
bethmm plugins move "Patch.esp" --after "Some Plugin.esp"   # --up, --down, --top, --bottom, --to N, --before X
bethmm plugins sync                           # bring plugins.txt in line now (e.g. after editing it by hand)
```

After every deploy and purge, the app keeps `plugins.txt` in line with the
plugins it put into `Data`:

* A newly installed mod's plugins are added at the end, switched on; masters go
  after the last master.
* When a mod is turned off or uninstalled (or the game purged), its plugins' lines
  are taken out and remembered, with their place and whether they were on.
* A plugin you switched off stays off. Lines the app didn't add, comments, and the
  file's encoding and line endings are left as they are.
* Before every change, `plugins.txt` is copied to
  `~/.local/share/beth-mod-manager/plugins-backups/<game>/` (the first copy and the
  latest 30 are kept). The new file then replaces the old one in one step. When
  `plugins.txt` is a link to a file you keep elsewhere, that file is updated and the
  link stays.

Fallout 4, Skyrim SE and Starfield use the `*Plugin.esp` format, where the order of
the lines is the load order; their own masters, DLCs and Creation Club content
load anyway and aren't listed. New Vegas, Fallout 3, Oblivion and Skyrim LE list
only active plugins (the game's own masters included) and load them in the order
of their file dates, as described in [step 9](#9-plugins-and-load-order).

If the app doesn't know the game's Wine prefix (or Wine user), it leaves
`plugins.txt` alone and says so when a deploy has plugins to add. Set them with
`bethmm override <game> --prefix ... --wine-user ...` (or in *Game settings*),
then run `bethmm plugins sync`.

`bethmm plugins sort` is where a sorter such as LOOT will plug in (see `SORTERS` in
`bethmm/plugins.py`).

### Finding games

```bash
bethmm games            # uses the cached result after the first run
bethmm games --rescan   # search again
```

Games are found by:

* scanning `~/Games` (Lutris's default install folder) for `Fallout4.exe`,
  `SkyrimSE.exe`, `TESV.exe`, `FalloutNV.exe`, `Fallout3.exe`, `Oblivion.exe` and
  `Starfield.exe`;
* reading Lutris's own records: `~/.local/share/lutris/games/*.yml`,
  `~/.config/lutris/games/*.yml`, `~/.local/share/lutris/pga.db`, and the same files
  of the Flatpak Lutris under `~/.var/app/net.lutris.Lutris/`.

For each game it shows the game folder, Data folder, Wine prefix, Wine user, where
`plugins.txt` is (GOG Skyrim SE uses `AppData/Local/Skyrim Special Edition GOG`) and
the store it detected. If something is wrong or missing, set it by hand:

```bash
bethmm override fallout4 --root "~/Games/fallout-4/drive_c/GOG Games/Fallout 4 GOTY"
bethmm override fallout4 --prefix ~/Games/fallout-4
bethmm override skyrimse --ignore          # hide a game
bethmm override fallout4 --clear           # back to automatic
bethmm config --games-dir /mnt/games       # look for Lutris games in another folder
```

### Looking inside archives and plugins

```bash
bethmm analyze ~/Downloads/"Some Mod-1234-1-2-1700000000.7z"
bethmm analyze ~/Downloads/SomeMod.zip --game fallout4   # check against a specific game
bethmm analyze ~/Downloads/SomeMod.zip --json
bethmm plugin-info SomeMod.esp                           # masters, header version, which game
```

`analyze` shows what `install` would do without installing anything:

* the mod's name, version and Nexus ID from the file name;
* which game it is for and why;
* which placement rule applies, and how sure it is;
* every file's destination in your real game folder, and which files would replace
  original game files;
* the files that won't be installed (readmes, screenshots, script extender source
  code).

When the layout isn't recognised, it shows the archive's folders and the ones that
look installable. Preview your own choice with
`--root "00 Core" --root "01 Optional" --target data`. Later roots win where files
overlap. `--target game` means next to the game's `.exe`. Without `--target`, each
root's place is detected on its own.

---

## Troubleshooting

* **A game isn't found:** run *Game ▸ Rescan for games* (or `bethmm games --rescan`).
  If it's still missing, set it by hand in *Game ▸ Game settings...* (or with
  `bethmm override`); see [Finding games](#finding-games).
* **A mod doesn't show up in the game.** Check in this order:
  1. *Game ▸ Verify...* (`bethmm verify`): is the game folder up to date with the mod list?
  2. *Game ▸ Check links* (`bethmm check-links`): does Wine see the links? If not,
     switch to hardlinks ([step 3](#3-check-that-the-game-will-see-your-mods-once-per-game)).
  3. Is its plugin switched on in the **Plugins** tab, and loaded after its masters?
     Fallout 4 also needs [the INI lines](#4-fallout-4-only-let-the-game-load-mods-once).
  4. Is it a script extender plugin (`F4SE/`, `SKSE/` folders)? Start the game
     through the loader ([step 10](#10-play)).
* **The app (or the computer) stopped in the middle of a change:** *Game ▸ Verify...*
  then **Repair** (`bethmm verify --repair`).
* **"another bethmm process is changing mods right now":** another window or command
  is making a change. Wait for it, then try again.
* **You want an older `plugins.txt` back:** copy one from
  `~/.local/share/beth-mod-manager/plugins-backups/<game>/` over the game's
  `plugins.txt`, then *Game ▸ Update plugins.txt*. The copies are named by date and
  time; `plugins-first.txt` is how it was before the app first changed it.
* **"plugins.txt wasn't updated: the game's Wine prefix (or Wine user) isn't
  known":** set them in *Game ▸ Game settings...*, then *Game ▸ Update plugins.txt*.
* **`.rar` archives don't open:** RAR support comes from `7zip-rar` (`p7zip-rar`
  on 22.04-based releases), which is in Ubuntu's multiverse repository. Run
  `sudo add-apt-repository multiverse`, then `./install.sh` again.
* **The desktop app doesn't start:** run `bethmm-gui` in a terminal to see why. An
  error about the `xcb` platform plugin means a system library is missing:
  `sudo apt install libxcb-cursor0`, or run `./install.sh` again, which checks for
  the others too.
* **The app stopped working after a distro upgrade:** run `./install.sh` again; it
  recreates the app's environment.
* **What did it do?** The **History** tab (`bethmm history`) lists the latest file
  operations. The log, `~/.local/share/beth-mod-manager/logs/bethmm.log`, has every
  one of them.

## Known limitations

* **No LOOT sorting yet.** Run LOOT yourself (it has a Linux version), then
  *Game ▸ Update plugins.txt*; the app keeps the order LOOT set.
* **FOMOD installers written in C#** (`fomod/script.cs`, rare and old) can't run.
  Choose the folders to install yourself instead.
* **Load order by file date** (Oblivion, Fallout 3, New Vegas, Skyrim LE): the app
  can't move the game's own plugins or ones you put into `Data` yourself. It dates
  its plugins in the order of their lines in `plugins.txt`, so a tool that changes
  only their dates, without rewriting `plugins.txt`, is undone at the next deploy.
* **No INI editing.** Fallout 4's lines ([step 4](#4-fallout-4-only-let-the-game-load-mods-once))
  and archive invalidation in the older games are yours to set.
* **Downloads are manual:** no "Mod manager download" (`nxm://` links) and no
  Nexus login.
* **One mod list per game:** no profiles.
* **Files that tools write into `Data`** (xEdit, BodySlide, ...) are real files the
  app doesn't manage; it leaves them alone.
* **Very large mods** (tens of thousands of files) take a while: every file is its
  own link, and the install dialog takes a few seconds to show such an archive.
* **Hardlinks** need the game and `~/.local/share` on the same disk.

---

## Reference

### What the app touches

* **The game folder:** only links to its mods' files, folders it creates for them
  (removed again once empty), and the original game files a mod replaces. Those are
  moved to the backups folder first and moved back when no enabled mod provides
  them any more.
* **`plugins.txt`:** only the lines of the plugins it installed, backed up before
  every change.
* **Never:** a file it didn't create is never deleted or edited. If you (or a tool
  such as xEdit or BethINI) replace one of its links with a real file, that file
  is left alone and reported.

### Where things are kept

| What | Where |
|---|---|
| Settings | `~/.config/beth-mod-manager/settings.json` |
| Installed mods (their files) | `~/.local/share/beth-mod-manager/mods/<game>/<mod>/` |
| Originals of game files replaced by mods | `~/.local/share/beth-mod-manager/backups/<game>/` |
| Copies of `plugins.txt`, made before each change | `~/.local/share/beth-mod-manager/plugins-backups/<game>/` |
| Records: the mod list, every link, backup and folder the app made, the `plugins.txt` lines it manages, history | `~/.local/share/beth-mod-manager/manifest.db` |
| Log of every file operation | `~/.local/share/beth-mod-manager/logs/bethmm.log` |
| The games found last time | `~/.local/share/beth-mod-manager/games.json` |

*Help ▸ Open the data folder* opens `~/.local/share/beth-mod-manager`.

### How mods are placed

The rules live in `bethmm/placement.py` and are applied in order:

1. **Wrapper folders:** a single unrecognised top-level folder such as `CoolMod v1.2/`
   is stepped into, repeatedly. Loose readmes next to it are ignored.
2. **FOMOD:** `fomod/ModuleConfig.xml` means a guided installer. It runs, and the
   files of the options you choose go into `Data/` (`bethmm/fomod.py`). Or you pick
   which folders to install.
3. **Data folder:** a `Data/` folder at the top means the top is the game folder
   (typical for script extenders: loader `.exe` + `.dll`s + `Data/`).
4. **Data content:** plugins (`.esp/.esm/.esl`), archives (`.ba2/.bsa`) or known Data
   folders (`meshes`, `textures`, `scripts`, `F4SE`, `SKSE`, `MCM`, ...) mean the top
   goes into `Data/`.
5. **Game-folder content:** `.exe`/`.dll` files and ENB/ReShade files and folders mean
   the top goes next to the game's `.exe`.
6. **Unknown:** you choose; the app shows the archive's folders and suggests the
   ones that look installable.

If the top has both Data content and game-folder-only files, rule 4 wins but the
confidence is marked low and the odd files are listed, so you can correct it.

The game is identified from the masters listed in the plugins' headers
(`Fallout4.esm` → Fallout 4, `Skyrim.esm` + form version 44 → Skyrim SE, ...), then
from script extender loaders and folders, and finally from which games you have
installed. You always confirm.

### Install script options

* `--yes`: don't ask before installing system packages.
* `--dev`: also install the test tools and run the tests.
* `--no-gui`: command line only, without Qt or the menu entry.
* `--no-system-packages`: never call `apt`; only check what is there.
* `--uninstall`: see [Uninstalling](#uninstalling).
* `--force`: install even when run as root, or uninstall even though mods are still
  deployed.

What the script installs with `apt`, if missing:
* the Python venv module;
* `7zip` and `7zip-rar` (`p7zip-full` and `p7zip-rar` on 22.04-based releases, plus
  `python3.11`, because those ship Python 3.10);
* the libraries Qt needs, usually just `libxcb-cursor0`.

<details>
<summary>Manual install (what the script does)</summary>

```bash
sudo apt install python3-venv 7zip 7zip-rar          # 22.04-based: python3.11 python3.11-venv p7zip-full p7zip-rar
sudo apt install libxcb-cursor0                      # for the desktop app
python3 -m venv ~/.local/share/beth-mod-manager/venv  # 22.04-based: python3.11 -m venv ...
~/.local/share/beth-mod-manager/venv/bin/pip install -e '.[gui]'   # '.' alone: command line only
#   (tests: '.[gui,dev,gui-dev]', or '.[dev]' without the desktop app)
mkdir -p ~/.local/bin
ln -s ~/.local/share/beth-mod-manager/venv/bin/bethmm ~/.local/bin/bethmm
ln -s ~/.local/share/beth-mod-manager/venv/bin/bethmm-gui ~/.local/bin/bethmm-gui
```
</details>

### Uninstalling

```bash
bethmm purge --all            # first: take every mod out of every game folder
./install.sh --uninstall      # then remove the app, its commands and its menu entry
```

`--uninstall` refuses while the app still has files in a game folder, and shows
what is left. Your mods, backups, settings and records stay in
`~/.local/share/beth-mod-manager` and `~/.config/beth-mod-manager`. Delete those
folders by hand if you no longer want them.

### Running the tests

```bash
./install.sh --dev        # installs pytest (and pytest-qt for the desktop app's tests) and runs the tests
# or, afterwards:
~/.local/share/beth-mod-manager/venv/bin/python -m pytest
```

The tests build fake game folders, fake Lutris configs and fake mods in temporary
folders; they never touch your real home folder or games.
