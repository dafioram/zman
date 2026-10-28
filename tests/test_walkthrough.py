"""Walkthrough: drive zman in a pseudo-terminal against the mock system and check
what each screen shows, at 80x24 and at a wide size.

Needs pyte (pip install pyte); skipped without it. Set WALK_OUT=DIR to save every
rendered screen as text for review.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "mock"))

try:
    from term import Term
    HAVE_PYTE = True
except ImportError:  # pragma: no cover
    HAVE_PYTE = False

SIZES = [(80, 24), (160, 40)]


@unittest.skipUnless(HAVE_PYTE, "pyte is not installed")
class Walk(unittest.TestCase):
    cols, rows = 80, 24

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="zman-walk-")
        self.t: Term = None  # type: ignore[assignment]
        self.shots = 0

    def tearDown(self) -> None:
        if self.t:
            self.t.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def start(self, cols: int = 0, rows: int = 0, env=None, argv=None) -> str:
        self.t = Term(os.path.join(self.root, "m"), cols or self.cols, rows or self.rows, env=env, argv=argv)
        self.assertTrue(self.t.wait_for(">"), self.t.text())
        return self.snap("start")

    def snap(self, label: str) -> str:
        text = self.t.text()
        out = os.environ.get("WALK_OUT")
        if out:
            os.makedirs(out, exist_ok=True)
            self.shots += 1
            name = f"{self.id().split('.')[-1]}-{self.t.cols}x{self.t.rows}-{self.shots:02d}-{label}.txt"
            with open(os.path.join(out, name.replace("/", "_").replace(" ", "_")), "w") as f:
                f.write(text + "\n")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("unexpected error", text)
        return text

    def go(self, *keys: str) -> str:
        for k in keys:
            if k == "^C":
                self.t.ctrl_c()
            elif k == "^D":
                self.t.ctrl_d()
            else:
                self.t.key(k)
        return self.snap("-".join(k.replace("^", "c") for k in keys)[:40] or "enter")

    # ------------------------------------------------------------ navigation basics

    def test_main_menu_keys(self) -> None:
        text = self.start()
        self.assertIn("zman  ZFS on this host", text)
        self.assertIn("2 degraded", text)
        self.assertIn("8  System info", text)
        self.assertIn("Unknown key: x", self.go("x"))
        self.assertIn("Unknown key: 42", self.go("42"))
        text = self.go("m")                       # m on the main menu does nothing
        self.assertIn("1  Pools", text)
        self.assertNotIn("Unknown key", text)
        self.assertIn("Quit zman? [Y/n]", self.go("b"))
        self.assertIn("1  Pools", self.go("n"))
        text = self.go("h")
        self.assertTrue("Words ZFS uses" in text or "Help" in text, text)
        self.go("q")
        self.assertIn("1  Pools", self.go(""))
        self.go("^D")
        self.assertEqual(self.t.exit_code(), 0)

    def test_back_main_quit_everywhere(self) -> None:
        self.start()
        text = self.go("1")
        self.assertIn("Pools  2", text)
        self.assertIn("1  Pools", self.go("b"))
        self.go("1", "2")                                  # pool view
        self.assertIn("1  Pools", self.go("m"))
        self.go("1", "2", "1")                             # devices list
        self.assertIn("tank  guid", self.go("^C"))         # Ctrl-C at a prompt = back
        self.go("1")
        self.assertIn("Unknown key: 99", self.go("99"))
        self.go("q")
        self.assertEqual(self.t.exit_code(), 0)

    def test_hidden_action_number(self) -> None:
        self.start()
        self.go("1", "2")                                  # tank: no scrub running
        text = self.go("5")
        self.assertIn("Not available here: 5", text)
        self.assertIn("4  Start scrub", text)

    # ------------------------------------------------------------ pools and devices

    def test_pool_list_problems_first(self) -> None:
        self.start()
        text = self.go("1")
        lines = [ln for ln in text.splitlines() if ln.strip()[:2].strip().isdigit()]
        self.assertIn("backup", lines[0])
        self.assertIn("DEGRADED", text)
        self.assertIn("c create pool", text)

    def test_replace_faulted_disk_and_resilver(self) -> None:
        self.start()
        text = self.go("1", "2", "1")
        self.assertIn("FAULTED, 15 errors", text)
        text = self.go("3")
        self.assertIn("Replace with another disk", text)
        self.assertNotIn("Replace in place", text)
        text = self.go("1")
        self.assertIn("sdj", text)
        self.assertIn("too small", text)
        self.assertNotIn("sdk", text)                     # LVM member never offered
        text = self.go("1")
        self.assertIn("Everything on sdj is erased.", text)
        self.assertIn("Command: zpool replace tank 5022222222222222222", text)
        self.assertIn("Type sdj to", text)
        text = self.go("sdj")
        self.assertIn("Resilver of tank", text)
        self.go("a")
        self.assertTrue(self.t.wait_for("Resilver finished.", 20), self.t.text())
        text = self.snap("resilver-finished")
        self.assertIn("finished", text)
        text = self.go("b")
        # The faulted disk was swapped out: its view closes with the action's result.
        self.assertIn("Resilver started.", text)
        self.assertIn("NEWDISK4TB", text)
        self.assertNotIn("FAULTED", text)

    def test_replace_cancel_with_wrong_name(self) -> None:
        self.start()
        self.go("1", "2", "1", "3", "1", "1")
        text = self.go("sdx")
        self.assertIn("Cancelled. Nothing was changed.", text)

    def test_missing_device_in_raidz(self) -> None:
        self.start()
        text = self.go("1", "1", "1")
        self.assertIn("UNAVAIL", text)
        text = self.go("4")
        self.assertIn("not found on this machine", text)
        self.assertIn("Replace with another disk", text)
        text = self.go("1")
        for d in ("sdj", "sdm", "sdi", "sdl"):
            self.assertIn(d, text)
        self.assertIn("nvme0n1 (too small)", text)

    def test_offline_needs_redundancy(self) -> None:
        self.start()
        text = self.go("1", "2", "1", "2")              # the only healthy side of tank's mirror
        self.assertNotIn("Offline", text)
        self.assertNotIn("Detach", text)
        text = self.go("b", "3")                         # the faulted side
        self.assertIn("Offline", text)
        text = self.go("3")
        self.assertIn("[y/N]", text)
        text = self.go("y")
        self.assertIn("Done.", text)
        self.assertIn("OFFLINE", text)
        text = self.go("4", "")                          # bring it online (Enter = yes)
        self.assertIn("Done.", text)

    def test_scrub_pause_stop(self) -> None:
        self.start()
        text = self.go("1", "1")                         # backup: scrub running
        self.assertIn("5  Pause scrub", text)
        self.assertIn("6  Stop scrub", text)
        text = self.go("5")
        self.assertIn("Pause scrub of backup? [Y/n]", text)
        text = self.go("")
        self.assertIn("Scrub paused.", text)
        self.assertIn("Resume scrub", text)
        text = self.go("6")
        self.assertIn("throws away its progress", text)
        text = self.go("y")
        self.assertIn("Scrub stopped.", text)

    def test_destroy_pool_and_export(self) -> None:
        self.start()
        text = self.go("1", "2", "17")                    # export tank: tank/app is busy
        self.assertIn("Export tank? [y/N]", text)
        text = self.go("y")
        self.assertIn("Failed: cannot unmount", text)
        self.assertIn("Something is using it", text)
        self.go("b", "1", "18")                           # destroy backup
        text = self.snap("destroy-preview")
        self.assertIn("ALL data", text)
        self.assertIn("Type backup to", text)
        text = self.go("backup")
        self.assertIn("Removed pool backup", text)
        self.assertIn("Pools  1", text)

    def test_import_foreign_pool(self) -> None:
        self.start()
        text = self.go("1", "i")
        self.assertIn("archive", text)
        text = self.go("1")
        self.assertIn("last used by another system", text)
        self.assertIn("[y/N]", text)
        self.assertIn("-f", text)
        text = self.go("y")
        self.assertIn("archive", text)
        self.assertIn("Imported archive", text)

    def test_create_pool_mirror_with_headroom(self) -> None:
        self.start()
        self.go("1", "c")
        text = self.go("mirror1")
        self.assertIn("ZFS reserves the name", text)
        text = self.go("fast")
        self.assertIn("layout", text)
        text = self.go("2")                               # mirror
        self.assertIn("sdj", text)
        self.assertNotIn("sdk", text)
        self.assertNotIn("sda ", text)
        lines = [ln for ln in text.splitlines() if " sdj " in ln or " sdm " in ln]
        nums = [ln.split()[0] for ln in lines]
        text = self.go(" ".join(nums))
        self.assertIn("New pool fast", text)
        self.assertIn("Capacity follows the smallest disk", text)
        text = self.go("2", "2")                          # leave headroom
        self.assertIn("leave headroom", text)
        self.assertIn("one partition of", text)
        text = self.go("1")
        self.assertIn("These disks will be erased", text)
        self.assertIn("sfdisk", text)
        self.assertIn("zpool create", text)
        text = self.go("fast", )
        self.assertTrue(self.t.wait_for("Press Enter", 20), self.t.text())
        text = self.go("")
        self.assertIn("fast", text)
        self.assertIn("Created pool fast", text)
        self.assertIn("mirror of 2 devices", text)

    def test_add_spare_and_disks(self) -> None:
        self.start()
        text = self.go("5")
        self.assertIn("system disk (mounted /)", text)
        self.assertIn("LVM member", text)
        self.assertIn("free, has ext4 data", text)
        self.assertIn("pool tank (spare)", text)
        text = self.go("8")                               # sdi
        self.assertIn("ext4", text)
        self.assertIn("erases that", text)

    # ------------------------------------------------------------ datasets and snapshots

    def test_new_child_dataset(self) -> None:
        self.start()
        text = self.go("2", "5")                          # tank/home
        self.assertIn("tank/home", text)
        text = self.go("4", "photos")
        self.assertIn("New dataset tank/home/photos", text)
        self.assertIn("inherited", text)
        text = self.go("2", "2")                          # compression → zstd
        self.assertIn("Compression:  zstd", text)
        text = self.go("1")
        self.assertIn("zfs create -o compression=zstd tank/home/photos", text)
        text = self.go("")
        self.assertIn("tank/home/photos", text)
        self.assertIn("Done.", text)

    def test_take_snapshot_with_children_question(self) -> None:
        self.start()
        self.go("2", "5", "2")
        text = self.snap("snapshot-prompt")
        self.assertIn("Enter keeps this name", text)
        text = self.go("")
        self.assertIn("Include its 1 child dataset? [y/N]", text)
        text = self.go("")
        self.assertIn("Done. Took tank/home@", text)

    def test_held_snapshot_release_then_destroy(self) -> None:
        self.start()
        text = self.go("3")
        row = next(ln for ln in text.splitlines() if "manual-2026-10-01" in ln)
        text = self.go(row.split()[0])
        self.assertIn("Holds:", text)
        self.assertIn("Destroy is not offered because it has 1 hold", text)
        self.assertNotIn("9  Destroy", text)
        text = self.go("3")
        self.assertIn("Release hold 'keep'", text)
        text = self.go("")
        self.assertIn("Done.", text)
        self.assertIn("9  Destroy", text)
        text = self.go("9")
        self.assertIn("Frees about", text)
        self.assertIn("[y/N]", text)
        text = self.go("y")
        self.assertIn("Removed snapshot tank/home@manual-2026-10-01", text)
        self.assertIn("Snapshots  6", text)

    def test_rollback_blocked_by_clone_then_allowed(self) -> None:
        self.start()
        text = self.go("2", "5", "1")
        row = next(ln for ln in text.splitlines() if "manual-2026-10-01" in ln)
        text = self.go(row.split()[0], "8")
        self.assertIn("zman will not roll back", text)
        self.assertIn("clone", text)
        text = self.go("", "b")
        row = next(ln for ln in text.splitlines() if "daily-2026-10-26" in ln)
        text = self.go(row.split()[0], "8")
        self.assertIn("Also destroys 1 newer snapshot", text)
        self.assertIn("daily-2026-10-27", text)
        self.assertIn("Type home to roll back", text)
        text = self.go("home")
        self.assertIn("is back at @daily-2026-10-26", text)

    def test_destroy_dataset_blocked_and_busy(self) -> None:
        self.start()
        text = self.go("2", "5", "17")                    # tank/home: held snapshot and a clone
        self.assertIn("zman will not destroy it", text)
        self.assertIn("has a hold", text)
        self.assertIn("tank/home-restore is a clone", text)
        self.go("", "b", "4", "17")                       # tank/app: busy
        text = self.snap("destroy-app")
        self.assertIn("Type app to destroy", text)
        text = self.go("app")
        self.assertIn("Failed:", text)
        self.assertIn("busy", text)

    def test_locked_dataset_load_key(self) -> None:
        self.start()
        text = self.go("2", "9")
        self.assertIn("key not loaded", text)
        self.assertIn("Load key", text)
        self.assertNotIn("Mount", text.split("Load key")[0].split("\n")[-1])
        self.t.key("9")
        self.assertTrue(self.t.wait_for("Enter passphrase"), self.t.text())
        self.t.key("secret")
        self.assertTrue(self.t.wait_for("Press Enter"), self.t.text())
        text = self.go("")
        self.assertIn("Done.", text)
        self.assertIn("key loaded", text)

    def test_settings_quota_and_inherit(self) -> None:
        self.start()
        text = self.go("2", "8", "6")                     # tank/media settings
        self.assertIn("Quota: 1.0TB (set here)", text)
        self.assertIn("Compression: lz4 (inherited from tank)", text)
        text = self.go("2", "500G")
        self.assertIn("Quota: 1.0TB → 500GB", text)
        text = self.go("")
        self.assertIn("Failed: cannot set property", text)
        text = self.go("1")                               # compression choices
        self.assertIn("zstd (smaller, more CPU)", text)
        self.assertIn("lz4 (fast; the usual choice)  (current)", text)
        self.assertIn("Compression: lz4", self.go("b"))

    def test_volume_view_and_grow(self) -> None:
        self.start()
        text = self.go("4", "1")
        self.assertIn("thin provisioned", text)
        text = self.go("4", "50G")
        self.assertIn("must be larger", text)
        text = self.go("4", "200G")
        self.assertIn("Grows the volume from 100GB to 200GB", text)
        text = self.go("")
        self.assertIn("Done.", text)
        self.assertIn("200GB", text)

    def test_pool_extras(self) -> None:
        self.start()
        self.go("1", "2")                                 # tank
        text = self.go("3", "all")
        self.assertIn("Enter keeps this name", text)
        text = self.go("")
        self.assertIn("Done. Took 10 snapshots", text)
        text = self.go("11")
        self.assertIn("History of tank", text)
        text = self.go("1")                               # opens in less
        self.assertIn("zpool create", text)
        text = self.go("q")
        self.assertIn("Last 100 lines", text)
        text = self.go("6", "nonsense")
        self.assertIn("Not a time", text)
        text = self.go("b", "12", "5")
        self.assertIn("Following events of tank", text)
        text = self.go("^C")
        self.assertIn("Events of tank", text)
        text = self.go("b", "13", "1")
        self.assertIn("Autoexpand for tank", text)
        text = self.go("2")
        self.assertIn("zpool set autoexpand=off tank", text)
        text = self.go("")
        self.assertIn("Autoexpand: off", text)
        text = self.go("b", "16")
        self.assertIn("zpool status -v -P tank", text)
        self.go("q")

    def test_spares_attach_detach(self) -> None:
        self.start()
        self.go("1", "2", "10")
        text = self.snap("spare-pick")
        self.assertIn("sdj", text)
        text = self.go("1")
        self.assertIn("as a hot spare", text)
        text = self.go("sdj")
        self.assertIn("is a spare of tank", text)
        text = self.go("1")
        self.assertIn("NEWDISK4TB", text)
        rows = [ln for ln in text.splitlines() if "AVAIL" in ln]
        self.assertEqual(len(rows), 2)
        text = self.go(rows[0].split()[0], "8")
        self.assertIn("Removes the spare", text)
        text = self.go("y")
        self.assertIn("Removed spare", text)
        text = self.go("2", "6")                          # attach a third side to sdb
        self.assertIn("Attach a mirror disk", text)
        text = self.go("1")
        self.assertIn("3-way mirror", text)
        name = text.split("Type ", 1)[1].split()[0]
        text = self.go(name)
        self.assertIn("Resilver of tank", text)
        text = self.go("b")
        self.assertIn("Resilver started.", text)
        self.assertIn("7  Detach", text)
        text = self.go("7")
        self.assertIn("Removes ata-WDC", text)
        text = self.go("y")
        self.assertIn("Detached", text)

    def test_upgrade_features(self) -> None:
        self.start()
        text = self.go("1", "1", "15")
        self.assertIn("Enables 2 features", text)
        self.assertIn("cannot be undone", text)
        text = self.go("y")
        self.assertIn("Done.", text)
        self.assertNotIn("Upgrade features", text)

    def test_dataset_rename_mount_and_settings(self) -> None:
        self.start()
        self.go("2", "6")                                 # tank/home-restore
        text = self.go("8")
        self.assertIn("Unmounted tank/home-restore", text)
        self.assertIn("7  Mount", text)
        text = self.go("7")
        self.assertIn("Mounted at /tank/home-restore", text)
        text = self.go("11", "restored")
        self.assertIn("files move from /tank/home-restore", text)
        text = self.go("y")
        self.assertIn("tank/restored", text)
        text = self.go("6", "6")
        self.assertIn("Mountpoint for tank/restored", text)
        text = self.go("1", "/srv/restored")
        self.assertIn("anything using the old path", text)
        self.assertIn("[y/N]", text)
        text = self.go("y")
        self.assertIn("Mountpoint: /srv/restored (set here)", text)
        text = self.go("6", "3")                          # inherit again
        self.assertIn("zfs inherit mountpoint tank/restored", text)
        self.go("y")

    def test_new_volume_and_clone(self) -> None:
        self.start()
        self.go("2", "11")                                # tank/vm
        text = self.go("5", "db", "20G")
        self.assertIn("New volume tank/vm/db", text)
        self.assertIn("Size: 20.0GB", text)
        text = self.go("1")
        self.assertIn("zfs create -V", text)
        text = self.go("")
        self.assertIn("tank/vm/db  volume", text)
        self.assertIn("Done.", text)
        self.go("m", "3")
        text = self.snap("snaps")
        row = next(ln for ln in text.splitlines() if "daily-2026-10-27" in ln)
        text = self.go(row.split()[0], "4")
        self.assertIn("Enter keeps tank/home-daily-2026-10-27", text)
        text = self.go("")
        self.assertIn("zfs clone tank/home@daily-2026-10-27 tank/home-daily-2026-10-27", text)
        text = self.go("")
        self.assertIn("tank/home-daily-2026-10-27", text)
        self.assertIn("this is a clone", text)
        text = self.go("14")
        self.assertIn("Clones:  tank/home-daily-2026-10-27", text)

    def test_snapshot_small_actions(self) -> None:
        self.start()
        text = self.go("3", "1")                          # newest snapshot
        text = self.go("1")
        self.assertIn("+ added", text)
        self.go("q")
        text = self.go("2", "")
        self.assertIn("is held (keep)", text)
        text = self.go("5", "renamed")
        self.assertIn("tank/home@renamed", text)
        text = self.go("3", "")
        self.assertIn("Done.", text)

    # ------------------------------------------------------------ overviews

    def test_prune_snapshots(self) -> None:
        self.start()
        text = self.go("7")
        self.assertIn("Prune snapshots (5 can be pruned)", text)
        text = self.go("1")
        self.assertNotIn("manual-2026-10-01", text)       # held
        self.assertNotIn("before-upgrade", text)          # has a clone
        text = self.go("1-2")
        self.assertIn("Destroys 2 snapshots", text)
        self.assertIn("Type 2 to destroy 2 snapshots", text)
        text = self.go("2")
        self.assertIn("Removed 2 snapshots", text)
        self.assertIn("3 can be pruned", text)

    def test_live_io_auto_refresh(self) -> None:
        self.start()
        text = self.go("6")
        self.assertIn("READ OPS", text)
        self.t.key("a", quiet=1.5, wait_prompt=False)
        self.assertIn("Ctrl-C stops", self.snap("auto"))
        text = self.go("^C")
        self.assertIn("a auto-refresh", text)

    def test_config_warning_shown(self) -> None:
        os.makedirs(os.path.join(self.root, "m"), exist_ok=True)
        conf = os.path.join(self.root, "bad.conf")
        with open(conf, "w") as f:
            f.write("[zman]\npage_size = lots\ncolour = yes\n")
        text = self.start(env={"ZMAN_CONFIG": conf})
        self.assertIn("page_size = lots is not a whole number", text)
        self.assertIn("unknown setting colour", text)

    def test_tour_all_screens(self) -> None:
        """Every list and a view of each kind at 80x24 and wide: nothing cut, nothing crashes."""
        for cols, rows in SIZES:
            with self.subTest(size=f"{cols}x{rows}"):
                if self.t:
                    self.t.close()
                shutil.rmtree(os.path.join(self.root, "m"), ignore_errors=True)
                self.start(cols, rows)
                for path in (["1"], ["1", "2"], ["1", "2", "1"], ["1", "2", "1", "3"], ["1", "2", "16"],
                             ["2"], ["2", "5"], ["2", "5", "6"], ["3"], ["3", "1"], ["4"], ["4", "1"],
                             ["5"], ["5", "4"], ["6"], ["7"], ["7", "2"], ["8"]):
                    text = self.go(*path)
                    for line in text.splitlines():
                        self.assertLessEqual(len(line), cols)
                    if path in (["1"], ["5"]):
                        self.assertTrue("DEGRADED" in text or "FAULTED" in text, text)
                    if path[-1] == "16" and "Raw details" not in text:
                        self.go("q")
                    self.go("m")


if __name__ == "__main__":
    unittest.main()
