"""Integration tests against real ZFS. Opt-in: ZMAN_INTEGRATION=1, as root, with ZFS loaded.

They build a throwaway pool on sparse files in a temporary folder (no disks, no
network), check that zman reads every list and decision correctly from the real
tools, then destroy everything they made.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zmanlib import model as M  # noqa: E402
from zmanlib import runner as R  # noqa: E402
from zmanlib import zfs as Z  # noqa: E402

ENABLED = os.environ.get("ZMAN_INTEGRATION") == "1"


def sh(*args: str, ok: bool = True) -> R.Result:
    r = R.run(list(args), timeout=120)
    if ok and not r.ok:
        raise AssertionError(f"{' '.join(args)} failed: {r.err or r.out}")
    return r


@unittest.skipUnless(ENABLED, "set ZMAN_INTEGRATION=1 to run against real ZFS (needs root)")
class RealZfs(unittest.TestCase):
    pool = ""
    tmp = ""

    @classmethod
    def setUpClass(cls) -> None:
        target, err = R.startup_check()
        if err:
            raise unittest.SkipTest(err)
        cls.tmp = tempfile.mkdtemp(prefix="zmanit-")
        cls.pool = f"zmanit{os.getpid()}"
        cls.files = []
        for i in range(5):
            f = os.path.join(cls.tmp, f"d{i}")
            with open(f, "wb") as fh:
                fh.truncate(256 * 1024 * 1024)
            cls.files.append(f)
        f0, f1, f2, f3, _ = cls.files
        sh("zpool", "create", "-o", "ashift=12", "-R", os.path.join(cls.tmp, "mnt"), "-O", "compression=lz4",
           cls.pool, "mirror", f0, f1, "spare", f2)
        p = cls.pool
        sh("zfs", "create", f"{p}/a")
        sh("zfs", "create", f"{p}/a/b")
        sh("zfs", "create", "-s", "-V", "64M", f"{p}/vol")
        sh("zfs", "create", "-o", "quota=10M", f"{p}/small")
        with open(os.path.join(cls.tmp, "mnt", p, "a", "file.txt"), "w") as fh:
            fh.write("hello\n")
        sh("zfs", "snapshot", f"{p}/a@s1")
        with open(os.path.join(cls.tmp, "mnt", p, "a", "file2.txt"), "w") as fh:
            fh.write("more\n")
        sh("zfs", "snapshot", f"{p}/a@s2")
        sh("zfs", "snapshot", f"{p}/a@s3")
        sh("zfs", "hold", "keep", f"{p}/a@s1")
        sh("zfs", "clone", f"{p}/a@s2", f"{p}/a-clone")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.pool:
            R.run(["zfs", "release", "keep", f"{cls.pool}/a@s1"])
            r = R.run(["zpool", "destroy", "-f", cls.pool], timeout=120)
            if not r.ok and "no such pool" not in r.err:
                R.run(["zpool", "import", "-d", cls.tmp, cls.pool], timeout=120)
                R.run(["zpool", "destroy", "-f", cls.pool], timeout=120)
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ reading

    def test_01_pool_list_and_status(self) -> None:
        pools, err = Z.list_pools()
        self.assertEqual(err, "")
        p = next(x for x in pools if x.name == self.pool)
        self.assertEqual(p.health, "ONLINE")
        self.assertGreater(p.size or 0, 0)
        st = p.status
        self.assertEqual(st.state, "ONLINE")
        mirror = [v for v in st.vdevs if v.kind == "mirror"]
        self.assertEqual(len(mirror), 1)
        kids = st.children(mirror[0].idx)
        self.assertEqual([k.path for k in kids], self.files[:2])
        spares = [v for v in st.vdevs if v.section == "spares" and v.is_leaf]
        self.assertEqual([(v.path, v.state) for v in spares], [(self.files[2], "AVAIL")])
        self.assertEqual(M.pool_problems(st), [])
        self.assertTrue(M.pool_redundant(st))

    def test_02_guids(self) -> None:
        p = Z.get_pool(self.pool)
        leaves = p.status.leaves()
        self.assertTrue(all(v.guid.isdecimal() for v in leaves), [v.guid for v in leaves])
        r = sh("zpool", "get", "-H", "-p", "-o", "value", "guid", self.pool)
        self.assertEqual(p.guid, r.out.strip())

    def test_03_props(self) -> None:
        props = Z.pool_props(self.pool)
        self.assertEqual(props["ashift"][0], "12")
        self.assertIn(props["autoexpand"][0], ("on", "off"))
        self.assertFalse(M.upgrade_available(props))

    def test_04_datasets(self) -> None:
        ds, err = Z.list_datasets(root=self.pool)
        self.assertEqual(err, "")
        names = {d.name: d for d in ds}
        p = self.pool
        self.assertEqual(set(names), {p, f"{p}/a", f"{p}/a/b", f"{p}/vol", f"{p}/small", f"{p}/a-clone"})
        self.assertTrue(names[f"{p}/vol"].is_volume)
        self.assertEqual(names[f"{p}/vol"].volsize, 64 * 1024 * 1024)
        self.assertEqual(names[f"{p}/a"].compression, "lz4")
        self.assertTrue(names[f"{p}/a"].mounted)
        self.assertEqual(names[f"{p}/a-clone"].origin, f"{p}/a@s2")
        self.assertEqual(names[f"{p}/small"].quota, 10 * 1024 * 1024)
        self.assertEqual(M.dataset_state(names[f"{p}/a"]), ("mounted", "ok"))
        one = Z.get_dataset(f"{p}/a")
        self.assertEqual(one.guid, names[f"{p}/a"].guid)
        self.assertIsNone(Z.get_dataset(f"{p}/nope"))
        props = Z.dataset_props(f"{p}/a/b", ["compression", "mountpoint"])
        self.assertEqual(props["compression"], ("lz4", f"inherited from {p}"))

    def test_05_snapshots_and_decisions(self) -> None:
        p = self.pool
        snaps, err = Z.list_snapshots(dataset=f"{p}/a")
        self.assertEqual(err, "")
        self.assertEqual([s.short for s in snaps], ["s1", "s2", "s3"])
        s1, s2, s3 = snaps
        self.assertEqual(s1.holds, 1)
        self.assertEqual(Z.holds(s1.name), ["keep"])
        self.assertEqual(s2.clones, [f"{p}/a-clone"])
        self.assertEqual(s3.clones, [])
        newer, blockers = M.rollback_plan(s1, snaps)
        self.assertEqual([s.short for s in newer], ["s2", "s3"])
        self.assertTrue(any("clone" in b for b in blockers))
        self.assertEqual([s.short for s in M.prune_candidates(snaps)], ["s3"])
        names, reclaim, err = Z.destroy_preview(s3.name)
        self.assertEqual(err, "")
        self.assertEqual(names, [s3.name])
        self.assertIsInstance(reclaim, int)
        names, _, _ = Z.destroy_preview(f"{p}/a/b", recursive=True)
        self.assertEqual(names, [f"{p}/a/b"])
        a = next(d for d in Z.list_datasets(root=p)[0] if d.name == f"{p}/a")
        tree, _ = Z.list_snapshots(dataset=f"{p}/a", recursive=True)
        from zmanlib.screens.datasets import destroy_blockers
        reasons = destroy_blockers(a, tree)
        self.assertTrue(any("has a hold" in r for r in reasons), reasons)
        self.assertTrue(any("is a clone of" in r for r in reasons), reasons)
        self.assertIsNotNone(Z.get_snapshot(s3.name))
        self.assertIsNotNone(Z.get_snapshot(s3.name))     # the dry run destroyed nothing
        all_snaps, _ = Z.list_snapshots()
        self.assertTrue(any(s.name == s2.name for s in all_snaps))

    def test_06_diff(self) -> None:
        r = sh("zfs", "diff", "-F", f"{self.pool}/a@s1")
        self.assertIn("file2.txt", r.out)

    def test_07_history_events(self) -> None:
        r = sh("zpool", "history", self.pool)
        hist = M.parse_history(r.out)
        self.assertTrue(any("zpool create" in line for _, line in hist))
        self.assertTrue(all(ts is not None for ts, _ in hist))
        r = R.run(["zpool", "events", self.pool])
        self.assertEqual(r.rc, 0, r.err)

    def test_08_disks_parse(self) -> None:
        disks, err = Z.list_disks()
        self.assertEqual(err, "")
        _, uses, _, _ = Z.disk_inventory()
        self.assertEqual(set(uses), {d.name for d in disks})

    # ------------------------------------------------------------ changes

    def wait_scan(self, kind: str, limit: float = 60) -> M.ScanInfo:
        end = time.time() + limit
        while time.time() < end:
            st = Z.get_pool(self.pool).status
            if st.scan.kind == kind and st.scan.state == "finished":
                return st.scan
            time.sleep(0.5)
        return Z.get_pool(self.pool).status.scan

    def test_10_scrub(self) -> None:
        sh("zpool", "scrub", self.pool)
        scan = self.wait_scan("scrub")
        self.assertEqual((scan.kind, scan.state, scan.errors), ("scrub", "finished", 0))
        self.assertIsNotNone(scan.when)

    def test_11_offline_online(self) -> None:
        p = Z.get_pool(self.pool)
        dev = p.status.find(self.files[1])
        sh("zpool", "offline", self.pool, dev.guid)
        st = Z.get_pool(self.pool).status
        self.assertEqual(st.state, "DEGRADED")
        self.assertEqual(st.find(self.files[1]).state, "OFFLINE")
        self.assertIn("degraded", M.pool_problems(st))
        sh("zpool", "online", self.pool, dev.guid)
        self.wait_scan("resilver", 30)
        self.assertEqual(Z.get_pool(self.pool).status.find(self.files[1]).state, "ONLINE")

    def test_12_replace(self) -> None:
        p = Z.get_pool(self.pool)
        dev = p.status.find(self.files[1])
        sh("zpool", "replace", self.pool, dev.guid, self.files[3])
        scan = self.wait_scan("resilver")
        self.assertEqual(scan.state, "finished")
        end = time.time() + 30
        while time.time() < end and Z.get_pool(self.pool).status.find(self.files[1]) is not None:
            time.sleep(0.5)
        st = Z.get_pool(self.pool).status
        self.assertIsNotNone(st.find(self.files[3]))
        self.assertIsNone(st.find(self.files[1]))
        self.assertEqual(st.state, "ONLINE")

    def test_13_snapshot_several_rollback_destroy(self) -> None:
        p = self.pool
        sh("zfs", "snapshot", f"{p}/a@multi", f"{p}/small@multi")
        snaps, _ = Z.list_snapshots(dataset=f"{p}/small")
        self.assertEqual([s.short for s in snaps], ["multi"])
        a_snaps, _ = Z.list_snapshots(dataset=f"{p}/a")
        target = next(s for s in a_snaps if s.short == "s3")
        newer, blockers = M.rollback_plan(target, a_snaps)
        self.assertEqual([s.short for s in newer], ["multi"])
        self.assertEqual(blockers, [])
        sh("zfs", "rollback", "-r", target.name)
        self.assertIsNone(Z.get_snapshot(f"{p}/a@multi"))
        sh("zfs", "destroy", f"{p}/small@multi")
        self.assertEqual(Z.list_snapshots(dataset=f"{p}/small")[0], [])

    def test_20_export_import(self) -> None:
        sh("zpool", "export", self.pool)
        cands, err = Z.import_candidates(search_dir=self.tmp)
        self.assertEqual(err, "")
        c = next(x for x in cands if x.name == self.pool)
        self.assertTrue(c.id.isdecimal())
        self.assertFalse(c.needs_force)
        sh("zpool", "import", "-d", self.tmp, "-R", os.path.join(self.tmp, "mnt"), c.id)
        self.assertIsNotNone(Z.get_pool(self.pool))


if __name__ == "__main__":
    unittest.main()
