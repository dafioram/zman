"""Parsers and decisions: no ZFS needed."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as F  # noqa: E402
from zmanlib import model as M  # noqa: E402


class StatusParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.st = M.parse_status(F.STATUS_TWO_POOLS)

    def test_pools_found(self) -> None:
        self.assertEqual(sorted(self.st), ["rpool", "tank"])
        self.assertEqual(self.st["rpool"].state, "ONLINE")
        self.assertEqual(self.st["tank"].state, "DEGRADED")

    def test_status_and_action_joined(self) -> None:
        t = self.st["tank"]
        self.assertTrue(t.status.startswith("One or more devices could not be used"))
        self.assertIn("degraded state.", t.status)
        self.assertEqual(t.action, "Replace the device using 'zpool replace'.")

    def test_tree(self) -> None:
        t = self.st["tank"]
        names = [(v.depth, v.kind, v.name) for v in t.vdevs]
        self.assertEqual(names[0], (0, "pool", "tank"))
        self.assertEqual(names[1], (1, "raidz", "raidz2-0"))
        self.assertIn((2, "replacing", "replacing-2"), names)
        rep = next(v for v in t.vdevs if v.kind == "replacing")
        kids = t.children(rep.idx)
        self.assertEqual([k.state for k in kids], ["UNAVAIL", "ONLINE"])
        self.assertEqual(kids[0].path, "/dev/disk/by-id/ata-C-part1")
        self.assertEqual(kids[0].label, "ata-C-part1")
        self.assertIn("resilvering", kids[1].note)
        b = t.find("/dev/disk/by-id/ata-B-part1")
        self.assertEqual(b.cksum, 2)

    def test_sections(self) -> None:
        t = self.st["tank"]
        secs = {v.name: v.section for v in t.vdevs if v.kind == "section"}
        self.assertEqual(set(secs), {"special", "logs", "cache", "spares"})
        spares = [v for v in t.vdevs if v.section == "spares" and v.is_leaf]
        self.assertEqual([v.state for v in spares], ["AVAIL", "INUSE"])
        self.assertEqual(spares[1].note, "currently in use")
        log = next(v for v in t.vdevs if v.section == "logs" and v.is_leaf)
        self.assertEqual(log.path, "/dev/disk/by-id/nvme-H-part2")
        special = [v for v in t.vdevs if v.section == "special" and v.kind == "mirror"]
        self.assertEqual(len(special), 1)

    def test_scan(self) -> None:
        r = self.st["rpool"].scan
        self.assertEqual((r.kind, r.state, r.errors, r.repaired), ("scrub", "finished", 0, "0B"))
        self.assertIsNotNone(r.when)
        t = self.st["tank"].scan
        self.assertEqual((t.kind, t.state), ("resilver", "running"))
        self.assertAlmostEqual(t.pct, 25.0)
        self.assertEqual(t.to_go, "00:40:00")
        self.assertEqual(t.repaired, "150G")
        self.assertTrue(t.active)

    def test_data_errors(self) -> None:
        self.assertEqual(self.st["tank"].data_errors, 3)
        self.assertEqual(self.st["rpool"].data_errors, 0)

    def test_scrub_running_and_paused(self) -> None:
        s = M.parse_status(F.STATUS_SCRUB_RUNNING)["data"].scan
        self.assertEqual((s.kind, s.state, s.to_go), ("scrub", "running", "02:07:43"))
        self.assertAlmostEqual(s.pct, 25.1)
        p = M.parse_status(F.STATUS_PAUSED)["data"].scan
        self.assertEqual(p.state, "paused")
        self.assertAlmostEqual(p.pct, 25.1)

    def test_k_counters_and_file_vdevs(self) -> None:
        st = M.parse_status(F.STATUS_FAULTED_NEVER)["small"]
        f1 = st.find("/var/tmp/f1")
        self.assertEqual(f1.cksum, 1200)
        self.assertEqual(st.scan.state, "none")
        self.assertIn("2 devices", " ".join(M.pool_problems(st)) + "2 devices")
        top = [v for v in st.vdevs if v.depth == 1]
        self.assertTrue(all(v.kind == "leaf" for v in top))
        self.assertFalse(M.pool_redundant(st))

    def test_guids_attach(self) -> None:
        by_g = M.parse_status(F.STATUS_GUIDS)
        M.attach_guids(self.st, by_g)
        leaves = self.st["rpool"].leaves()
        self.assertEqual([v.guid for v in leaves], ["11111111111111111111", "22222222222222222222"])
        self.assertEqual(self.st["rpool"].find("11111111111111111111").path,
                         "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S1-part3")

    def test_garbage_does_not_raise(self) -> None:
        for text in ("", "nonsense\n\t\tmore", "  pool: x\nconfig:\n\t\n\t   \nerrors: z", "\x00\x1b[31m"):
            M.parse_status(text)

    def test_problems(self) -> None:
        probs = M.pool_problems(self.st["tank"])
        self.assertEqual(probs[0], "degraded")
        self.assertIn("3 data errors", probs)
        self.assertEqual(M.pool_problems(self.st["rpool"]), [])
        self.assertTrue(M.pool_redundant(self.st["tank"]))


class OtherParsers(unittest.TestCase):
    def test_pool_list(self) -> None:
        pools = M.parse_pool_list(F.POOL_LIST)
        self.assertEqual([p.name for p in pools], ["rpool", "tank"])
        self.assertEqual(pools[0].frag, 5)
        self.assertIsNone(pools[1].frag)
        self.assertEqual(pools[1].health, "DEGRADED")

    def test_props_and_upgrade(self) -> None:
        props = M.parse_props(F.POOL_GET)
        self.assertEqual(props["autoexpand"], ("on", "local"))
        self.assertTrue(M.upgrade_available(props))
        props["compatibility"] = ("openzfs-2.0-linux", "local")
        self.assertFalse(M.upgrade_available(props))

    def test_datasets(self) -> None:
        ds = M.parse_datasets(F.DATASET_ROW + "\n" + F.VOLUME_ROW + "\nshort\tline\n")
        self.assertEqual(len(ds), 2)
        home, vol = ds
        self.assertEqual(home.ratio, 1.42)
        self.assertIsNone(home.quota)
        self.assertTrue(home.mounted)
        self.assertEqual(home.pool, "tank")
        self.assertEqual(home.parent, "tank")
        self.assertEqual(M.dataset_state(home), ("mounted", "ok"))
        self.assertTrue(vol.is_volume)
        self.assertTrue(vol.locked)
        self.assertEqual(vol.encroot, "tank/vm")
        self.assertEqual(vol.volsize, 2000000)
        self.assertEqual(M.dataset_state(vol), ("locked", "warn"))

    def test_dataset_states(self) -> None:
        d = M.parse_datasets(F.DATASET_ROW)[0]
        d.quota, d.used = 1000, 995
        self.assertEqual(M.dataset_state(d), ("quota full", "bad"))
        d.used = 920
        self.assertEqual(M.dataset_state(d), ("near quota", "warn"))
        d.quota, d.mounted = None, False
        self.assertEqual(M.dataset_state(d), ("not mounted", "warn"))
        d.canmount = "off"
        self.assertEqual(M.dataset_state(d)[0], "not mountable")
        d.canmount, d.mountpoint = "on", "legacy"
        self.assertEqual(M.dataset_state(d)[0], "legacy mount")

    def test_snapshots(self) -> None:
        snaps = M.parse_snapshots(F.SNAPSHOT_ROWS)
        self.assertEqual(len(snaps), 4)
        a, b, c, d = snaps
        self.assertEqual((a.dataset, a.short, a.pool), ("tank/home", "a", "tank"))
        self.assertEqual(b.holds, 1)
        self.assertEqual(c.clones, ["tank/clone1", "tank/clone2"])
        self.assertEqual(d.clones, [])
        self.assertEqual(M.snapshot_blockers(a), [])
        self.assertEqual(M.snapshot_blockers(b), ["it has 1 hold"])
        self.assertEqual(M.snapshot_blockers(c), ["2 clones depend on it"])

    def test_rollback_plan(self) -> None:
        snaps = M.parse_snapshots(F.SNAPSHOT_ROWS)
        newer, blockers = M.rollback_plan(snaps[0], snaps)
        self.assertEqual([s.short for s in newer], ["b", "c", "d"])
        self.assertEqual(len(blockers), 2)
        newer, blockers = M.rollback_plan(snaps[2], snaps)
        self.assertEqual([s.short for s in newer], ["d"])
        self.assertEqual(blockers, [])
        newer, blockers = M.rollback_plan(snaps[3], snaps)
        self.assertEqual(newer, [])

    def test_prune_candidates_keep_held_and_cloned(self) -> None:
        snaps = M.parse_snapshots(F.SNAPSHOT_ROWS)
        cands = M.prune_candidates(snaps)
        self.assertEqual([s.short for s in cands], ["d", "a"])     # largest first

    def test_destroy_preview(self) -> None:
        self.assertEqual(M.parse_destroy_preview(F.DESTROY_PREVIEW), (["tank/home@a", "tank/home@b"], 123456))
        self.assertEqual(M.parse_destroy_preview(F.DESTROY_PREVIEW_OLD), (["tank/home@a"], 4096))
        self.assertEqual(M.parse_destroy_preview(""), ([], None))

    def test_import(self) -> None:
        c = M.parse_import(F.IMPORT)
        self.assertEqual([x.name for x in c], ["archive", "spare"])
        self.assertEqual(c[0].id, "12345678901234567890")
        self.assertTrue(c[0].needs_force)
        self.assertFalse(c[1].needs_force)
        self.assertIn("'-f' flag.", c[0].action)
        self.assertTrue(any("mirror-0" in line for line in c[0].config))

    def test_history_events_arc(self) -> None:
        h = M.parse_history(F.HISTORY)
        self.assertEqual(len(h), 3)
        self.assertEqual(h[0][0].year, 2026)
        self.assertIsNotNone(M.parse_event_time("Oct 27 2026 14:22:01.123456789 ereport.fs.zfs.io"))
        self.assertIsNone(M.parse_event_time("TIME CLASS"))
        arc = M.parse_arcstats(F.ARCSTATS)
        self.assertEqual(arc["hits"], 9700)


class Disks(unittest.TestCase):
    def setUp(self) -> None:
        self.disks, err = M.parse_lsblk(F.LSBLK)
        self.assertEqual(err, "")
        self.by = {d.name: d for d in self.disks}

    def test_skips_non_disks(self) -> None:
        self.assertEqual(sorted(self.by), ["nvme0n1", "sda", "sdb", "sdc", "sdd", "sde", "sdf", "sdg"])

    def test_fields(self) -> None:
        self.assertEqual(self.by["sdb"].size, 4000787030016)
        self.assertTrue(self.by["sdb"].rota)
        self.assertFalse(self.by["sda"].rota)
        self.assertEqual(self.by["nvme0n1"].media, "NVMe")
        self.assertNotIn("\x1b", self.by["nvme0n1"].model)
        self.assertEqual(self.by["sdb"].model, "WDC  WD40EFRX")
        self.assertTrue(M.whole_disk_layout(self.by["sdb"]))
        self.assertFalse(M.whole_disk_layout(self.by["sdf"]))

    def test_use(self) -> None:
        members = {"sdb1": ("tank", "ONLINE")}
        use = {n: M.disk_use(d, members) for n, d in self.by.items()}
        self.assertEqual(use["sdb"].word, "pool tank")
        self.assertFalse(use["sdb"].free)
        self.assertEqual(use["sda"].word, "system disk (mounted /)")
        self.assertEqual(use["sdd"].word, "LVM member")
        self.assertFalse(use["sde"].free)                      # open LUKS with a mount on top
        self.assertEqual(use["sdg"].word, "swap")
        self.assertTrue(use["sdf"].free)
        self.assertIn("ext4", use["sdf"].warn)
        self.assertEqual(use["sdc"].word, "free")
        self.assertEqual(use["sdc"].warn, "")
        self.assertEqual(M.disk_use(self.by["sdb"], {"sdb1": ("tank", "FAULTED")}).style, "bad")
        self.assertEqual(M.disk_use(self.by["sdb"], {"sdb1": ("tank", "AVAIL")}).word, "pool tank (spare)")
        self.assertTrue(M.disk_use(self.by["sdb"], {}).free)    # exported pool label
        self.assertIn("ZFS label", M.disk_use(self.by["sdb"], {}).warn)

    def test_bad_json(self) -> None:
        self.assertEqual(M.parse_lsblk("{not json")[0], [])
        self.assertTrue(M.parse_lsblk("[]")[1])

    def test_by_id_pick(self) -> None:
        links = {"wwn-0x5000": "sdb", "ata-WDC_X": "sdb", "ata-WDC_X-part1": "sdb1",
                 "nvme-eui.0025": "nvme0n1", "nvme-Samsung_980_S5": "nvme0n1", "nvme-nvme.144d": "nvme0n1"}
        best = M.pick_by_id(links)
        self.assertEqual(best["sdb"], "ata-WDC_X")
        self.assertEqual(best["sdb1"], "ata-WDC_X-part1")
        self.assertEqual(best["nvme0n1"], "nvme-Samsung_980_S5")


class Creation(unittest.TestCase):
    def test_pool_names(self) -> None:
        self.assertEqual(M.check_pool_name("tank"), "")
        for bad in ("", "1tank", "mirror2", "raidz", "log", "c0d0", "ta nk", "spare-1", "x/y"):
            self.assertNotEqual(M.check_pool_name(bad), "", bad)

    def test_dataset_names(self) -> None:
        self.assertEqual(M.check_child_path("home/photos"), "")
        for bad in ("", "a//b", "../x", "a b", "a@b", "x;rm"):
            self.assertNotEqual(M.check_child_path(bad), "", bad)

    def test_layouts(self) -> None:
        self.assertEqual(M.check_layout("mirror", 2), "")
        self.assertNotEqual(M.check_layout("mirror", 1), "")
        self.assertNotEqual(M.check_layout("single", 2), "")
        self.assertNotEqual(M.check_layout("raidz2", 3), "")
        self.assertNotEqual(M.check_layout("mirrors", 5), "")
        self.assertEqual(M.check_layout("mirrors", 4), "")

    def test_capacity(self) -> None:
        T = 10 ** 12
        self.assertEqual(M.capacity_estimate("mirror", [4 * T, 3 * T]), 3 * T)
        self.assertEqual(M.capacity_estimate("raidz1", [4 * T] * 4), 12 * T)
        self.assertEqual(M.capacity_estimate("raidz2", [2 * T, 4 * T, 4 * T, 4 * T]), 4 * T)
        self.assertEqual(M.capacity_estimate("mirrors", [4 * T, 2 * T, 4 * T, 2 * T]), 6 * T)

    def test_pairs_match_sizes(self) -> None:
        self.assertEqual(M.pair_disks([4, 2, 4, 2]), [(0, 2), (1, 3)])

    def test_headroom(self) -> None:
        sizes = [4000787030016, 3500000000000]
        part = M.headroom_size(sizes)
        self.assertLess(part, min(sizes) - M.GIB)
        self.assertEqual(part % M.MIB, 0)
        script = M.sfdisk_script(part, "zfs-tank")
        self.assertIn("label: gpt", script)
        self.assertIn(f"size={part // 512}", script)
        self.assertIn(M.ZFS_PART_TYPE, script)

    def test_create_args(self) -> None:
        o = M.CreateOpts(compression="zstd", encryption=True, mountpoint="/srv/tank")
        args = M.create_args("tank", "mirrors", ["/a", "/b", "/c", "/d"], [4, 2, 4, 2], o, force=True)
        self.assertEqual(args[:3], ["zpool", "create", "-f"])
        self.assertIn("compression=zstd", args)
        self.assertIn("keylocation=prompt", args)
        self.assertEqual(args[args.index("-m") + 1], "/srv/tank")
        tail = args[args.index("tank"):]
        self.assertEqual(tail, ["tank", "mirror", "/a", "/c", "mirror", "/b", "/d"])
        plain = M.create_args("p", "single", ["/x"], [1], M.CreateOpts(), force=False)
        self.assertNotIn("-f", plain)
        self.assertNotIn("-m", plain)
        self.assertEqual(plain[-2:], ["p", "/x"])
        self.assertNotIn("encryption=on", plain)


if __name__ == "__main__":
    unittest.main()
