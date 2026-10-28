"""Text helpers, config, runner, system-access functions with a fake runner, and UI pieces."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as F  # noqa: E402
from zmanlib import config as C  # noqa: E402
from zmanlib import runner as R  # noqa: E402
from zmanlib import textfmt as T  # noqa: E402
from zmanlib import ui as U  # noqa: E402
from zmanlib import zfs as Z  # noqa: E402
from zmanlib.errors import Back, Cancel, Quit, ToMain  # noqa: E402


class Text(unittest.TestCase):
    def test_sizes(self) -> None:
        self.assertEqual(T.fmt_size(None), "-")
        self.assertEqual(T.fmt_size(512), "512B")
        self.assertEqual(T.fmt_size(73_600_000), "73.6MB")
        self.assertEqual(T.fmt_size(820 * 10 ** 9), "820GB")
        self.assertEqual(T.fmt_size(4 * 10 ** 12), "4.0TB")

    def test_ages(self) -> None:
        self.assertEqual(T.fmt_ago(95, now=100), "5s ago")
        self.assertEqual(T.fmt_duration(180), "3m")
        self.assertEqual(T.fmt_duration(7200), "2h")
        self.assertEqual(T.fmt_duration(26 * 86400), "26d")

    def test_strip_ctl(self) -> None:
        self.assertEqual(T.strip_ctl("a\x1b[31mb\x07c\td‮"), "a[31mbc d")
        self.assertEqual(T.strip_ctl(None), "")
        self.assertEqual(T.strip_ctl_block("x\x1b\ny\tz"), "x\ny       z")

    def test_amounts(self) -> None:
        self.assertEqual(T.parse_amount("500G"), 500 * 10 ** 9)
        self.assertEqual(T.parse_amount("500GB"), 500 * 10 ** 9)
        self.assertEqual(T.parse_amount("1.5t"), 1_500_000_000_000)
        self.assertEqual(T.parse_amount("2GiB"), 2 * 1024 ** 3)
        self.assertEqual(T.parse_amount("4096"), 4096)
        for bad in ("", "lots", "5X", "iB", "-1G", "١٢"):
            with self.assertRaises(ValueError, msg=bad):
                T.parse_amount(bad)

    def test_selection(self) -> None:
        self.assertEqual(T.parse_selection("1 3-5, 7", 8), [0, 2, 3, 4, 6])
        self.assertEqual(T.parse_selection("all", 3), [0, 1, 2])
        self.assertEqual(T.parse_selection("5-3", 5), [2, 3, 4])
        for bad in ("", "0", "9", "x", "1-", "²"):
            with self.assertRaises(ValueError, msg=bad):
                T.parse_selection(bad, 8)

    def test_since(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, 0)
        self.assertEqual(T.parse_since("15m", now), datetime(2026, 10, 27, 11, 45))
        self.assertEqual(T.parse_since("2h", now), datetime(2026, 10, 27, 10, 0))
        self.assertEqual(T.parse_since("2026-10-22", now), datetime(2026, 10, 22))
        self.assertEqual(T.parse_since("2026-10-22 14:00", now), datetime(2026, 10, 22, 14, 0))
        with self.assertRaises(ValueError):
            T.parse_since("yesterday", now)

    def test_truncate(self) -> None:
        self.assertEqual(T.truncate("abcdef", 4), "abc…")
        self.assertEqual(T.truncate("abcdef", 4, "~"), "abc~")
        self.assertEqual(T.truncate("ab", 4), "ab")


class Config(unittest.TestCase):
    def test_defaults_and_values(self) -> None:
        c = C.parse_config("[zman]\npage_size = 20\nsnapshot_prefix = auto\n")
        self.assertEqual(c.page_size, 20)
        self.assertEqual(c.snapshot_prefix, "auto")
        self.assertEqual(c.warnings, [])

    def test_bad_values_warn(self) -> None:
        c = C.parse_config("[zman]\npage_size = lots\ncommand_timeout = 1\nhold_tag = a b\nfoo = 1\n")
        self.assertEqual(c.page_size, 0)
        self.assertEqual(c.command_timeout, 60)
        self.assertEqual(c.hold_tag, "keep")
        self.assertEqual(len(c.warnings), 4)

    def test_broken_file(self) -> None:
        c = C.parse_config("this is not ini", "/x.conf")
        self.assertEqual(len(c.warnings), 1)
        self.assertEqual(c.page_size, 0)

    def test_search_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            os.makedirs(os.path.join(home, ".config", "zman"))
            etc = os.path.join(d, "etc")
            os.makedirs(etc)
            with open(os.path.join(etc, "zman.conf"), "w") as f:
                f.write("[zman]\npage_size = 7\n")
            env = {"HOME": home, "ZMAN_CONF_DIR": etc}
            self.assertEqual(C.load_config(None, env).page_size, 7)
            with open(os.path.join(home, ".config", "zman", "zman.conf"), "w") as f:
                f.write("[zman]\npage_size = 8\n")
            self.assertEqual(C.load_config(None, env).page_size, 8)
            mine = os.path.join(d, "mine.conf")
            with open(mine, "w") as f:
                f.write("[zman]\npage_size = 9\n")
            env["ZMAN_CONFIG"] = mine
            self.assertEqual(C.load_config(None, env).page_size, 9)
            missing = C.load_config(os.path.join(d, "nope.conf"), env)
            self.assertEqual(missing.page_size, 0)
            self.assertIn("not found", missing.warnings[0])

    def test_inline_comments(self) -> None:
        c = C.parse_config("[zman]\npage_size = 12   # rows per page\n")
        self.assertEqual((c.page_size, c.warnings), (12, []))

    def test_default_text_parses_cleanly(self) -> None:
        self.assertEqual(C.parse_config(C.DEFAULT_TEXT).warnings, [])


class Runner(unittest.TestCase):
    def test_run_captures_and_handles_missing(self) -> None:
        r = R.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((r.rc, r.out.strip(), r.err.strip()), (3, "out", "err"))
        self.assertEqual(R.run(["/nonexistent/zfs-xyz"]).rc, 127)

    def test_run_timeout(self) -> None:
        r = R.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertEqual(r.rc, 124)
        self.assertIn("did not finish", r.reason)

    def test_no_shell(self) -> None:
        r = R.run(["echo", "$HOME; rm -rf /"])
        self.assertEqual(r.out.strip(), "$HOME; rm -rf /")

    def test_c_locale_for_parsing(self) -> None:
        r = R.run([sys.executable, "-c", "import os; print(os.environ['LC_ALL'])"])
        self.assertEqual(r.out.strip(), "C")

    def test_input(self) -> None:
        r = R.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="abc")
        self.assertEqual(r.out.strip(), "ABC")

    def test_reason_is_clean(self) -> None:
        self.assertEqual(R.Result(1, "", "\n  cannot open 'x\x1b[2J': no such pool\n").reason,
                         "cannot open 'x[2J': no such pool")
        self.assertEqual(R.Result(2, "", "").reason, "exit code 2")

    def test_hints(self) -> None:
        self.assertIn("sudo", R.hint_for("cannot create 'x': permission denied"))
        self.assertIn("using it", R.hint_for("cannot unmount '/tank': pool or dataset is busy"))
        self.assertEqual(R.hint_for("something new"), "")

    def test_startup_check(self) -> None:
        bins = {"zfs": "/sbin/zfs", "zpool": "/sbin/zpool", "lsblk": "/bin/lsblk"}
        with mock.patch.object(R.os.path, "realpath", side_effect=lambda p: p):
            ok = lambda args, **kw: R.Result(0, "zfs-2.1.11-1\nzfs-kmod-2.1.12-1\n" if args[1] == "version" else "tank\n", "")
            t, err = R.startup_check(which=bins.get, exists=lambda p: True, runner=ok)
            self.assertEqual(err, "")
            self.assertEqual(t.userland, "2.1.11-1")
            self.assertEqual(t.kmod, "2.1.12-1")
            _, err = R.startup_check(which=lambda n: None if n == "zfs" else bins.get(n), exists=lambda p: True,
                                     runner=ok)
            if not os.path.exists("/usr/sbin/zfs") and not os.path.exists("/sbin/zfs"):
                self.assertIn("zfsutils-linux", err)
            _, err = R.startup_check(which=bins.get, exists=lambda p: False, runner=ok)
            self.assertIn("modprobe zfs", err)
            denied = lambda args, **kw: R.Result(1, "", "Permission denied the ZFS utilities must be run as root.")
            _, err = R.startup_check(which=bins.get, exists=lambda p: True, runner=denied)
            self.assertIn("sudo", err)
            mixed = dict(bins, zpool="/usr/local/sbin/zpool")
            _, err = R.startup_check(which=mixed.get, exists=lambda p: True, runner=ok)
            self.assertIn("different installs", err)


class FakeRunner:
    """Answers zfs/zpool calls from fixtures and records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, timeout=None, cwd=None, input=None):
        self.calls.append(list(args))
        for prefix, result in self.answers:
            if list(args[:len(prefix)]) == prefix:
                return result
        return R.Result(1, "", "unexpected call")


class SystemAccess(unittest.TestCase):
    def test_list_pools_attaches_status(self) -> None:
        fake = FakeRunner([(["zpool", "list"], R.Result(0, F.POOL_LIST, "")),
                           (["zpool", "status"], R.Result(0, F.STATUS_TWO_POOLS, ""))])
        pools, err = Z.list_pools(fake)
        self.assertEqual(err, "")
        self.assertEqual(pools[1].status.state, "DEGRADED")
        self.assertIn("-p", fake.calls[0])
        self.assertIn("-H", fake.calls[0])

    def test_list_pools_error_is_reported(self) -> None:
        fake = FakeRunner([(["zpool", "list"], R.Result(1, "", "permission denied"))])
        pools, err = Z.list_pools(fake)
        self.assertEqual(pools, [])
        self.assertEqual(err, "permission denied")

    def test_get_pool_missing(self) -> None:
        fake = FakeRunner([(["zpool", "list"], R.Result(1, "", "cannot open 'x': no such pool"))])
        self.assertIsNone(Z.get_pool("x", fake))

    def test_snapshots_of_one_dataset(self) -> None:
        fake = FakeRunner([(["zfs", "list"], R.Result(0, F.SNAPSHOT_ROWS, ""))])
        snaps, err = Z.list_snapshots(fake, dataset="tank/home")
        self.assertEqual(len(snaps), 4)
        self.assertEqual(fake.calls[0][-3:], ["-d", "1", "tank/home"])
        self.assertIn("createtxg", fake.calls[0])

    def test_destroy_preview_is_dry_run(self) -> None:
        fake = FakeRunner([(["zfs", "destroy"], R.Result(0, F.DESTROY_PREVIEW, ""))])
        names, reclaim, err = Z.destroy_preview("tank/home@a,b", runner=fake)
        self.assertEqual(reclaim, 123456)
        self.assertIn("-n", fake.calls[0])

    def test_import_none(self) -> None:
        fake = FakeRunner([(["zpool", "import"], R.Result(1, "", "no pools available to import"))])
        self.assertEqual(Z.import_candidates(fake), ([], ""))

    def test_disks_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dev = os.path.join(d, "dev")
            os.makedirs(dev)
            byid = os.path.join(d, "by-id")
            os.makedirs(byid)
            for n in ("sdb", "sdb1", "sdc"):
                open(os.path.join(dev, n), "w").close()
            os.symlink("../dev/sdb", os.path.join(byid, "ata-WDC_1"))
            os.symlink("../dev/sdb", os.path.join(byid, "wwn-0x1"))
            os.symlink("../dev/sdb1", os.path.join(byid, "ata-WDC_1-part1"))
            fake = FakeRunner([(["lsblk"], R.Result(0, F.LSBLK, ""))])
            old = R.TARGET.by_id_dir
            R.TARGET.by_id_dir = byid
            try:
                disks, err = Z.list_disks(fake)
            finally:
                R.TARGET.by_id_dir = old
            sdb = next(x for x in disks if x.name == "sdb")
            self.assertEqual(sdb.by_id, os.path.join(byid, "ata-WDC_1"))
            self.assertEqual(sdb.dev_path, sdb.by_id)
            self.assertEqual(sdb.parts[0].by_id, os.path.join(byid, "ata-WDC_1-part1"))
            sdc = next(x for x in disks if x.name == "sdc")
            self.assertEqual(sdc.dev_path, "/dev/sdc")
            self.assertEqual(Z.kname_of(os.path.join(byid, "ata-WDC_1-part1")), "sdb1")
            self.assertEqual(Z.kname_of(os.path.join(byid, "missing")), "")


class Layout(unittest.TestCase):
    def cols(self):
        return [U.Col("NAME", lambda x: x, max=40, min=6, flex=True, want=12),
                U.Col("STATE", lambda x: x, keep=True),
                U.Col("SIZE", lambda x: x, min=5),
                U.Col("EXTRA", lambda x: x, max=30, min=5, drop=1),
                U.Col("MORE", lambda x: x, max=30, min=5, drop=2)]

    def test_fits_without_change(self) -> None:
        cells = [["a", "ok", "1GB", "x", "y"]]
        self.assertEqual(U.layout_columns(self.cols(), cells, 80), [4, 5, 4, 5, 4])

    def test_drop_before_truncating_state(self) -> None:
        cells = [["n" * 30, "FAULTED, 15 errors (resilvering)", "4.0TB", "e" * 25, "m" * 25]]
        w = U.layout_columns(self.cols(), cells, 60)
        self.assertEqual(w[1], len(cells[0][1]))          # state never cut
        self.assertIsNone(w[3])                           # lowest priority hidden first
        total = sum(x for x in w if x) + 2 * (len([x for x in w if x]) - 1)
        self.assertLessEqual(total, 60)

    def test_shrinks_to_minimum(self) -> None:
        cells = [["n" * 30, "ok", "4.0TB", "e" * 25, "m" * 25]]
        w = U.layout_columns(self.cols(), cells, 20)
        self.assertEqual(w[0], 6)

    def test_slack_given_back(self) -> None:
        cells = [["n" * 30, "ok", "4.0TB", "e" * 30, "m" * 30]]
        w = U.layout_columns(self.cols(), cells, 50)
        self.assertGreater(w[0], 12)

    def test_cells(self) -> None:
        self.assertEqual(U.format_cell("abcdef", 4, False), "abc…")
        self.assertEqual(U.format_cell("ab", 4, True), "  ab")
        cut = U.format_cell("ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1111111-part1", 24, False, middle=True)
        self.assertEqual(len(cut), 24)
        self.assertTrue(cut.endswith("1111111-part1"))

    def test_footer_wraps_between_items(self) -> None:
        U.set_color(False)
        items = ["1-11 open", "s sort", "f filter", "Enter refresh", "h help", "b back", "m main menu", "q quit"]
        lines = U.footer(items, 40)
        self.assertTrue(all(len(x) <= 40 for x in lines))
        joined = " · ".join(lines)
        for it in items:
            self.assertIn(it, joined)
        self.assertFalse(any(x.startswith("·") or x.endswith("·") for x in lines))


class Keys(unittest.TestCase):
    """menu() key handling, with input patched."""

    def run_menu(self, typed, opts, top=False, help_text=""):
        U.set_color(False)
        msg = U.Msg()
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=typed), mock.patch("sys.stdout", out):
            key = U.menu(["Title"], opts, msg, top=top, help_text=help_text)
        return key, msg, out.getvalue()

    def opts(self):
        return [U.Opt("1", "Logs"), U.Opt("3", "Stop", show=False), U.Opt("4", "Restart")]

    def test_number_and_padding(self) -> None:
        self.assertEqual(self.run_menu(["  04 "], self.opts())[0], "4")

    def test_hidden_and_unknown(self) -> None:
        key, msg, _ = self.run_menu(["3"], self.opts())
        self.assertEqual((key, msg.lines[0][0]), ("", "Not available here: 3"))
        key, msg, _ = self.run_menu(["x"], self.opts())
        self.assertEqual(msg.lines[0][0], "Unknown key: x")
        key, msg, _ = self.run_menu(["²"], self.opts())         # isdigit but not isdecimal
        self.assertEqual(msg.lines[0][0], "Unknown key: ²")

    def test_global_keys(self) -> None:
        with self.assertRaises(Quit):
            self.run_menu(["Q"], self.opts())
        with self.assertRaises(ToMain):
            self.run_menu(["m"], self.opts())
        with self.assertRaises(Back):
            self.run_menu(["b"], self.opts())
        self.assertEqual(self.run_menu(["m"], self.opts(), top=True)[0], "")
        with self.assertRaises(Quit):
            self.run_menu(["b", ""], self.opts(), top=True)    # Quit zman? [Y/n] → Enter
        self.assertEqual(self.run_menu(["b", "n"], self.opts(), top=True)[0], "")

    def test_ctrl_c_and_d(self) -> None:
        with self.assertRaises(Back):
            self.run_menu([KeyboardInterrupt()], self.opts())
        with self.assertRaises(Quit):
            self.run_menu([EOFError()], self.opts())

    def test_enter_refreshes(self) -> None:
        self.assertEqual(self.run_menu([""], self.opts())[0], "")

    def test_footer_present(self) -> None:
        _, _, out = self.run_menu([""], self.opts())
        self.assertIn("b back · m main menu · q quit", out)

    def test_confirm_levels(self) -> None:
        with mock.patch("builtins.input", side_effect=[""]), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(U.confirm("Restart?", True))
        with mock.patch("builtins.input", side_effect=[""]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.confirm("Remove?", False))
        with mock.patch("builtins.input", side_effect=["maybe", "y"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(U.confirm("Remove?", False))
        with mock.patch("builtins.input", side_effect=[KeyboardInterrupt()]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.confirm("Remove?", True))
        with mock.patch("builtins.input", side_effect=["web "]), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(U.typed_confirm("web", "delete it"))
        with mock.patch("builtins.input", side_effect=["Web"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.typed_confirm("web", "delete it"))
        with mock.patch("builtins.input", side_effect=[""]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.typed_confirm("web", "delete it"))

    def test_ask_text_cancels_on_ctrl_c(self) -> None:
        with mock.patch("builtins.input", side_effect=[KeyboardInterrupt()]), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(Cancel):
                U.ask_text("Name: ")

    def test_input_is_cleaned(self) -> None:
        with mock.patch("builtins.input", side_effect=["\x1b[A tank \x07"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(U.ask("> "), "[A tank")

    def test_pick_candidates(self) -> None:
        with mock.patch("builtins.input", side_effect=["9", "1 3"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(U.pick_candidates("t", ["a", "b", "c"]), [0, 2])
        with mock.patch("builtins.input", side_effect=["b"]), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(Cancel):
                U.pick_candidates("t", ["a"])


class ListViewLogic(unittest.TestCase):
    def view(self):
        return U.ListView("Things", lambda: U.Fetched([]), [U.Col("NAME", lambda x: x["n"])],
                          [U.Sort("problems first", lambda x: (0 if x["bad"] else 1, x["n"])),
                           U.Sort("name", lambda x: x["n"])],
                          lambda x: x["n"] + " " + x["id"], None)

    def test_filter_and_sort(self) -> None:
        items = [{"n": "web-b", "bad": False, "id": "x1"}, {"n": "db", "bad": True, "id": "x2"},
                 {"n": "web-a", "bad": True, "id": "x3"}]
        v = self.view()
        self.assertEqual([i["n"] for i in v.visible_items(items)], ["db", "web-a", "web-b"])
        v.filter = "web"
        self.assertEqual([i["n"] for i in v.visible_items(items)], ["web-a", "web-b"])
        v.filter = "web x1"                               # fields that are not columns match too
        self.assertEqual([i["n"] for i in v.visible_items(items)], ["web-b"])

    def test_render_counts_and_empty(self) -> None:
        U.set_color(False)
        v = self.view()
        lines, _ = v.render(U.Fetched([]), 80, 24)
        self.assertIn("No items.", lines)
        v.filter = "zzz"
        lines, _ = v.render(U.Fetched([{"n": "a", "bad": False, "id": ""}]), 80, 24)
        self.assertIn("Nothing matches the filter.", lines)
        self.assertIn("0 of 1 shown", lines[0])

    def test_paging(self) -> None:
        U.set_color(False)
        v = self.view()
        items = [{"n": f"i{x:03d}", "bad": False, "id": ""} for x in range(100)]
        lines, page = v.render(U.Fetched(items), 80, 24)
        self.assertLessEqual(len(lines), 23)
        self.assertIn("page 1/", lines[0])
        v.page = 1
        _, page2 = v.render(U.Fetched(items), 80, 24)
        self.assertNotEqual(page[0], page2[0])


if __name__ == "__main__":
    unittest.main()
