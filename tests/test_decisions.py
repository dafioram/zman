"""Pure decisions behind the screens, and the command line."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as F  # noqa: E402
from zmanlib import app  # noqa: E402
from zmanlib import model as M  # noqa: E402
from zmanlib.screens import datasets as DS  # noqa: E402
from zmanlib.screens import devices as DV  # noqa: E402
from zmanlib.screens import diskops as DO  # noqa: E402
from zmanlib.screens import pools as PL  # noqa: E402
from zmanlib.screens import space as SP  # noqa: E402


class CommandLine(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(app.parse_args([]), ("run", None))
        self.assertEqual(app.parse_args(["--config", "x.conf"]), ("run", "x.conf"))
        self.assertEqual(app.parse_args(["--config=x.conf"]), ("run", "x.conf"))
        self.assertEqual(app.parse_args(["--version"])[0], "version")
        self.assertEqual(app.parse_args(["--help"])[0], "help")
        self.assertTrue(app.parse_args(["scrub"])[0].startswith("zman: unknown option"))
        self.assertTrue(app.parse_args(["--config"])[0].startswith("zman: --config needs"))

    def test_version_and_terminal_check(self) -> None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "zman.py"), "--version"], capture_output=True,
                           text=True, env=env)
        with open(os.path.join(ROOT, "VERSION")) as f:
            v = f.read().strip()
        self.assertEqual(r.stdout.strip(), f"zman {v}")
        self.assertTrue(v.isdecimal())
        r = subprocess.run([sys.executable, os.path.join(ROOT, "zman.py")], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip(), "zman is interactive; run it in a terminal.")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "zman.py"), "list"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 2)

    def test_startup_failure_is_one_line(self) -> None:
        with mock.patch.object(app, "startup_check", return_value=(None, "zman: ZFS is missing. Hint: x")), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(app.main([]), 1)
        self.assertEqual(err.getvalue(), "zman: ZFS is missing. Hint: x\n")


def snap(name, holds=0, clones=(), txg=1, used=0):
    return M.Snapshot(name=name, holds=holds, clones=list(clones), txg=txg, used=used)


class DatasetDecisions(unittest.TestCase):
    def test_destroy_blockers(self) -> None:
        ds = M.Dataset(name="tank/a", type="filesystem")
        snaps = [snap("tank/a@1", holds=1), snap("tank/a/b@2", clones=["tank/a/c"]),
                 snap("tank/a@3", clones=["tank/x"]), snap("tank/a@4")]
        reasons = DS.destroy_blockers(ds, snaps)
        self.assertEqual(reasons, ["tank/a@1 has a hold", "tank/x is a clone of tank/a@3"])

    def test_volume_alignment(self) -> None:
        self.assertEqual(DS._align(1), DS.VOL_ALIGN)
        self.assertEqual(DS._align(DS.VOL_ALIGN), DS.VOL_ALIGN)
        self.assertEqual(DS._align(20 * 10 ** 9) % DS.VOL_ALIGN, 0)

    def test_fmt_prop(self) -> None:
        self.assertEqual(DS._fmt_prop("quota", "0"), "none")
        self.assertEqual(DS._fmt_prop("quota", "500000000000"), "500GB")
        self.assertEqual(DS._fmt_prop("recordsize", "131072"), "128K")
        self.assertEqual(DS._fmt_prop("recordsize", "1048576"), "1M")
        self.assertEqual(DS._fmt_prop("compression", "lz4"), "lz4")

    def test_apply(self) -> None:
        sets = {"compression": "zstd"}
        DS._apply(sets, "compression", None)
        self.assertEqual(sets, {"compression": "zstd"})
        DS._apply(sets, "compression", "")
        self.assertEqual(sets, {})
        DS._apply(sets, "quota", "5")
        self.assertEqual(sets, {"quota": "5"})


class PoolDecisions(unittest.TestCase):
    def setUp(self) -> None:
        self.st = M.parse_status(F.STATUS_TWO_POOLS)

    def test_layout_text(self) -> None:
        self.assertEqual(PL.describe_layout(self.st["rpool"]), "mirror of 2 devices")
        text = PL.describe_layout(self.st["tank"])
        self.assertTrue(text.startswith("raidz2 of 4 devices + 2 spares"), text)
        self.assertIn("1 log device", text)
        self.assertIn("1 cache device", text)
        plain = M.parse_status(F.STATUS_FAULTED_NEVER)["small"]
        self.assertEqual(PL.describe_layout(plain), "2 disks striped, no redundancy")

    def test_health_word(self) -> None:
        p = M.Pool(name="tank", health="DEGRADED", status=self.st["tank"])
        word, style = PL.pool_health(p)
        self.assertTrue(word.startswith("DEGRADED, "))
        self.assertEqual(style, "bad")
        ok = M.Pool(name="rpool", health="ONLINE", status=self.st["rpool"])
        self.assertEqual(PL.pool_health(ok), ("ONLINE", "ok"))

    def test_scan_lines(self) -> None:
        lines = PL.scan_lines(self.st["tank"])
        self.assertTrue(lines[0].startswith("State:     running, 25.0% done, 00:40:00 to go"))
        self.assertTrue(any("ata-D-part1" in x for x in lines))
        never = M.parse_status(F.STATUS_FAULTED_NEVER)["small"]
        self.assertEqual(PL.scan_lines(never), ["No scrub has run on this pool yet."])

    def test_roles(self) -> None:
        st = self.st["tank"]
        rep_child = next(v for v in st.vdevs if v.state == "UNAVAIL")
        self.assertIn("being replaced", DV.role_text(st, rep_child))
        spare = next(v for v in st.vdevs if v.section == "spares" and v.is_leaf)
        self.assertEqual(DV.role_text(st, spare), "hot spare")
        log = next(v for v in st.vdevs if v.section == "logs" and v.is_leaf)
        self.assertEqual(DV.role_text(st, log), "write log (SLOG)")
        member = st.find("/dev/disk/by-id/ata-A-part1")
        self.assertEqual(DV.role_text(st, member), "member of raidz2-0 (4 devices)")
        single = M.parse_status(F.STATUS_FAULTED_NEVER)["small"].find("/var/tmp/f1")
        self.assertEqual(DV.role_text(M.parse_status(F.STATUS_FAULTED_NEVER)["small"], single),
                         "single-disk vdev, no redundancy")

    def test_device_key_prefers_guid(self) -> None:
        v = M.Vdev(idx=1, parent=0, depth=1, section="data", kind="leaf", name="/dev/x", path="/dev/x", guid="42")
        self.assertEqual(DV._key(v), "42")
        v.guid = ""
        self.assertEqual(DV._key(v), "/dev/x")


class DiskDecisions(unittest.TestCase):
    def test_free_disks_largest_first(self) -> None:
        disks, _ = M.parse_lsblk(F.LSBLK)
        uses = {d.name: M.disk_use(d, {"sdb1": ("tank", "ONLINE")}) for d in disks}
        free = DO.free_disks(disks, uses)
        self.assertEqual([d.name for d in free], ["sdc", "nvme0n1", "sdf"])
        self.assertEqual([d.name for d in DO.free_disks(disks, uses, min_size=2 * 10 ** 12)], ["sdc"])
        self.assertEqual([d.name for d in DO.free_disks(disks, uses, exclude=["sdc"])], ["nvme0n1", "sdf"])

    def test_partition_paths(self) -> None:
        d = M.Disk(name="sdb", path="/dev/sdb", by_id="/dev/disk/by-id/ata-X")
        self.assertEqual(DO.partition_path(d), "/dev/disk/by-id/ata-X-part1")
        n = M.Disk(name="nvme0n1", path="/dev/nvme0n1")
        self.assertEqual(DO.partition_path(n), "/dev/nvme0n1p1")
        s = M.Disk(name="sdc", path="/dev/sdc")
        self.assertEqual(DO.partition_path(s), "/dev/sdc1")

    def test_size_need(self) -> None:
        st = M.parse_status(F.STATUS_TWO_POOLS)["rpool"]
        a = st.find("/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S1-part3")
        disk = M.Disk(name="nvme0n1", path="/dev/nvme0n1", size=1000 * 10 ** 9,
                      parts=[M.Part(name="nvme0n1p3", path="/dev/nvme0n1p3", size=900 * 10 ** 9)])
        with mock.patch("zmanlib.zfs.kname_of", side_effect=lambda p: "nvme0n1p3" if p.endswith("S1-part3") else ""):
            need, part = DO.size_need(st, a, [disk])
        # No ZFS whole-disk layout (no small partition 9): new disks get the same partition size.
        self.assertEqual(part, 900 * 10 ** 9)
        self.assertGreater(need, 900 * 10 ** 9)
        disk.parts.append(M.Part(name="nvme0n1p9", path="/dev/nvme0n1p9", size=8 * M.MIB))
        with mock.patch("zmanlib.zfs.kname_of", side_effect=lambda p: "nvme0n1p3" if p.endswith("S1-part3") else ""):
            need, part = DO.size_need(st, a, [disk])
        self.assertIsNone(part)
        self.assertEqual(need, 900 * 10 ** 9)


class Pruning(unittest.TestCase):
    def test_destroy_commands_group_by_dataset(self) -> None:
        snaps = [snap("tank/a@1"), snap("tank/b@x"), snap("tank/a@2")]
        cmds = SP.destroy_commands(snaps)
        self.assertEqual(cmds, [["zfs", "destroy", "tank/a@1,2"], ["zfs", "destroy", "tank/b@x"]])

    def test_destroy_commands_chunk(self) -> None:
        snaps = [snap(f"tank/a@s{i}") for i in range(SP.CHUNK + 5)]
        cmds = SP.destroy_commands(snaps)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(len(SP._cmd_groups(snaps)[1][1]), 5)


if __name__ == "__main__":
    unittest.main()
