# zman

A menu-driven manager for ZFS on Debian. Run `zman` and pick from numbered menus
to look after pools, disks, datasets, volumes and snapshots on this machine. It
runs the `zfs` and `zpool` tools for you, shows the exact command before anything
long or destructive, and asks before it changes anything.

```
zman  ZFS on this host - 2 pools, 11 datasets, 1 volume, 1 degraded

1  Pools             5  Disks
2  Datasets          6  Space & cleanup
3  Snapshots         7  System info
4  Volumes           h  Help
b back · q quit
>
```

There are no subcommands and nothing to remember: every screen lists what you can
do. Type a number or a letter and press Enter. Enter alone refreshes, `b` goes
back, `m` returns to the main menu, `q` quits. zman runs no background service and
keeps no history; every screen reads the live state again.

## Requirements

- Debian 12 or 13 (or a close relative such as Ubuntu) with OpenZFS 2.1 or newer:
  `apt install zfsutils-linux` (on Debian, from `contrib`, with `zfs-dkms`).
- Python 3.9 or newer, standard library only.
- `lsblk` (util-linux, always installed). `sfdisk` (package `fdisk`) only for the
  "leave headroom" disk option.

Reading works as a normal user. Changes need root: run `sudo zman`, or delegate
with `zfs allow`.

## Install

```sh
git clone <this repository> zman
cd zman
sudo ./install.sh          # or: sudo ./install.sh --yes
zman
```

| Path | What |
| --- | --- |
| `/usr/local/bin/zman` | the launcher |
| `/usr/local/lib/zman/` | the program, `VERSION` and an install marker |
| `/etc/zman/zman.conf` | config, written only if it does not exist |

If `./install.sh` says `Permission denied`, the files lost their executable bit
(this happens when a zip is unpacked or committed by some tools). Run
`sudo sh install.sh` instead, and restore the bit in git so CI's check passes:
`git update-index --chmod=+x install.sh uninstall.sh zman.py`.

Reinstalling (for example after `git pull`) replaces the program in place and keeps
the config. The installed copy does not depend on the folder you installed from.
Both scripts refuse to touch any file at those paths that they did not create.

To remove it: `sudo ./uninstall.sh` (asks, and asks separately about the config),
or `sudo ./uninstall.sh --yes --purge` to remove the config too. Neither script
ever touches ZFS or your data.

`ZMAN_PREFIX` and `ZMAN_CONF_DIR` change where the scripts install, which is how
the tests run them without root.

## What it does

**Pools.** List with health, space, fragmentation and the last scrub. Each pool
shows its problem and fix (in ZFS's own words), layout, scrub state and errors.
From there:

- Start, pause, resume and stop a scrub, and watch it with auto-refresh.
- Clear errors, trim, change autoexpand and autotrim.
- Add a hot spare. Expand onto bigger disks after you replace them.
- Upgrade pool features (with a warning that older systems may then not import it).
- Read the pool's history and events, including following events live.
- Snapshot several datasets at the same moment (one atomic `zfs snapshot`).
- Export, import (with a clear warning for pools last used by another system), and destroy.

**Creating a pool** (`c` on the Pools list): pick a name, a layout and free disks.

- Layouts: single disk, mirror (2 or more disks), RAIDZ1/2/3, striped mirrors
  (pairs, like RAID 10; disks are paired by size).
- Only free disks are offered: not mounted, not swap, not in a pool, not used by
  LVM, RAID or an open LUKS device. A disk that still holds a filesystem,
  partitions or an old ZFS label is shown in yellow and needs an extra typed
  confirmation.
- Disks are always given to ZFS by their stable `/dev/disk/by-id` name.
- Defaults: `ashift=12`, `compression=lz4`, `atime=off`, `autotrim=on`,
  `xattr=sa`, `autoexpand=on`. Encryption with a passphrase is optional; ZFS
  asks for the passphrase itself.
- **Disks of different sizes.** A mirror or RAIDZ is as big as its smallest
  disk. Choose either:
  - *Whole disks* (default): ZFS partitions each disk. With `autoexpand=on` the
    pool grows once the smaller disks are replaced with bigger ones.
  - *Leave headroom*: zman gives every disk one GPT partition a little below the
    smallest disk (0.5%, at least 1 GiB), so a future replacement that is a few MB
    smaller still fits. zman then gives replacements the same partition size.
- You see the exact `sfdisk` and `zpool create` commands, then type the pool's name.

**Devices** (pool → Devices): the pool's layout as a tree, with each disk's state,
error counts, size and serial number. A faulted or missing disk is red. For a disk:

- **Replace with another disk.** Pick a free disk at least as large; zman previews
  `zpool replace`, you type the new disk's name, and the resilver progress
  screen opens.
- **Replace in place**, when the new disk sits at the old path.
- **Offline / Online** (offline only while the rest of its mirror or RAIDZ is healthy).
- **Attach a mirror disk** to turn a single disk into a mirror, or a mirror into a 3-way mirror.
- **Detach** a disk from a mirror (or cancel a replacement in progress).
- **Remove** a spare, cache or log device. Clear errors.

The usual way to deal with a bad disk: Pools → the degraded pool → Devices → the
red disk (note its serial) → Offline → swap the disk → Replace with another disk
→ watch the resilver → Clear errors if the pool still counts old errors.

**Datasets and volumes.** Lists with state (mounted, locked, near quota...), used,
available and mountpoint. A dataset's view shows space split into data, snapshots
and children. From there: take a snapshot, list its snapshots, create a child
dataset or volume, change settings (compression, quota, reservation, record size,
access times, mountpoint, read-only, or reset to inherited), mount and unmount,
load and unload encryption keys, rename, and destroy. Volumes can grow (never
shrink, which would cut off data).

**Snapshots.** All snapshots or one dataset's, newest first, with the space each
one alone holds, holds and clones. For a snapshot: see what changed since
(`zfs diff`), hold and release, clone into a new dataset, rename, roll back and
destroy. The header shows where to browse its files under `.zfs/snapshot`, which is
how to get single files back.

**Disks.** Every disk with size, how it is used, type, serial, model and by-id
name, with its partitions in the disk's view.

**Space & cleanup.** Space per pool split into data, snapshots and reservations,
the datasets whose snapshots hold the most, space by dataset, and **Prune
snapshots**.

**System info.** zman and ZFS versions (including a warning when the tools and
the kernel module differ), ARC cache size and hit rate, the scheduled scrub job
Debian ships, and whether an automatic snapshot tool is installed.

## Safety

Every change goes preview → confirm → re-check → run:

- The preview says what is affected, what is kept, and the exact command.
- Confirmation matches the risk: `[Y/n]` for harmless actions (Enter means yes),
  `[y/N]` for destructive ones (Enter means no), and typing a name for anything
  that erases data: creating a pool or using a disk (the disk's name), destroying a
  pool or dataset, rolling back (the dataset's name), and bulk pruning (the count).
- Right before running, zman reads the state again. If anything changed, it runs
  nothing and says so.
- zman refuses where ZFS would quietly take more than you asked: it does not
  destroy a dataset whose snapshots are held or cloned elsewhere, does not roll
  back past a held or cloned snapshot, and pruning never offers held or cloned
  snapshots.
- No command goes through a shell. Text from ZFS and disks is cleaned of control
  characters before it is shown.

## Left out on purpose

- Automatic snapshot schedules: they need a background service. Use sanoid or
  zfs-auto-snapshot; their snapshots show up in zman as usual.
- Send/receive replication, adding vdevs to an existing pool (`zpool add`, often
  impossible to undo), dRAID, dedup, special, log and cache devices, pool split,
  checkpoints and RAIDZ expansion. Use the ZFS tools directly for these.
- Pools striped across several disks without redundancy.
- Blinking a drive's LED: zman shows the serial number instead.
- Live I/O monitoring: use `zpool iostat -v -l` or `iostat`.

## Configuration

An INI file; the first that exists wins: `--config FILE`, `$ZMAN_CONFIG`,
`~/.config/zman/zman.conf`, `/etc/zman/zman.conf`. A bad value shows a warning in
the app and the default is used.

```ini
[zman]
page_size = 0          # rows per list page; 0 fits the terminal
command_timeout = 60   # seconds for quick zfs/zpool calls
long_timeout = 600     # seconds for slow reads (zfs diff, history) and big changes
refresh_interval = 2   # seconds between auto-refresh redraws
snapshot_prefix = manual   # suggested names: manual-2026-10-27-1422
hold_tag = keep        # tag used by Hold
```

The only command-line options are `--config FILE`, `--version` and `--help`.
`NO_COLOR` turns colors off. zman works at 80×24 and over SSH and in tmux.

## Development

```
zman.py               entry script (run it; never imported)
zmanlib/
  app.py              options, terminal check, startup check
  runner.py           run / run_fg, the pinned ZFS install, error hints
  model.py            data classes, parsers, pure decisions
  zfs.py              reading ZFS and disk state (runner passed in)
  ui.py               menus, lists, prompts, confirmations, paging
  textfmt.py, config.py, errors.py
  screens/            main menu, pools, devices, datasets, snapshots, disks, space, info
tests/
  test_model.py, test_core.py, test_decisions.py   unit tests, nothing installed
  test_install.py + install_cycle.sh                install → reinstall → uninstall
  test_integration.py                               real ZFS, opt-in
  test_walkthrough.py + term.py + mock/             screens in a pseudo-terminal
```

Run the tests:

```sh
python3 -m unittest discover -s tests -t .          # unit + install cycle (+ walkthrough if pyte is installed)
pip install pyte && python3 -m unittest tests.test_walkthrough
sudo ZMAN_INTEGRATION=1 python3 -m unittest tests.test_integration -v
shellcheck install.sh uninstall.sh tests/install_cycle.sh
```

The integration tests need root and the ZFS module. They build a throwaway pool on
sparse files in a temporary folder, check every list and decision against it, and
destroy it afterwards. They never touch existing pools or disks and never run
unless `ZMAN_INTEGRATION=1` is set.

The walkthrough drives zman in a pseudo-terminal against a mock ZFS (fake `zfs`,
`zpool`, `lsblk` and `sfdisk` that keep state in a JSON file) containing a faulted
disk, a missing disk, a running scrub, held and cloned snapshots, a locked
encrypted dataset, a busy dataset, a full quota, and free, partitioned, LVM and
system disks. It checks every list, view, cancel path and the keys at 80×24 and
160×40. `WALK_OUT=dir` saves every rendered screen. To try the screens on any
machine without ZFS: `python3 tests/mock/run_mock.py`.

CI (`.github/workflows/ci.yml`) runs the unit tests on Python 3.9 to 3.14, the
walkthrough, the integration tests on a real ZFS module, ShellCheck and the
install cycle.

`VERSION` holds one whole number; raise it by one for each release.
