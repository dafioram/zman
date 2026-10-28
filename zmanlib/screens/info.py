"""Live I/O and System info."""
from __future__ import annotations

import os
import platform
import pwd
import shutil
from typing import List, Optional

from .. import zfs
from ..config import CONFIG
from ..runner import TARGET
from ..textfmt import fmt_size, strip_ctl
from ..ui import Col, Fetched, ListView, Msg, Sort, item_header, menu, paint
from . import helptext


def _arc_line() -> str:
    arc = zfs.arcstats()
    if not arc:
        return "not available"
    hits, misses = arc.get("hits", 0), arc.get("misses", 0)
    rate = f", {hits * 100 // (hits + misses)}% hits since boot" if hits + misses else ""
    return f"{fmt_size(arc.get('size'))} of {fmt_size(arc.get('c_max'))} max{rate}"


def live_io_screen() -> None:
    def fetch() -> Fetched:
        print(paint("Measuring for 1 second...", "dim"))
        stats, err = zfs.iostat()
        return Fetched(stats, [], err)

    def rate(n: Optional[int]) -> str:
        return f"{fmt_size(n)}/s" if n is not None else "-"

    cols = [
        Col("POOL", lambda s: s.pool, max=30, min=6, flex=True, want=12),
        Col("READ OPS", lambda s: str(s.rops if s.rops is not None else "-"), right=True, keep=True),
        Col("WRITE OPS", lambda s: str(s.wops if s.wops is not None else "-"), right=True, keep=True),
        Col("READ", lambda s: rate(s.rbw), right=True, min=7),
        Col("WRITE", lambda s: rate(s.wbw), right=True, min=7),
        Col("ALLOC", lambda s: fmt_size(s.alloc), right=True, drop=1),
        Col("FREE", lambda s: fmt_size(s.free), right=True, drop=2),
    ]
    sorts = [Sort("name", lambda s: s.pool),
             Sort("busiest first", lambda s: -((s.rbw or 0) + (s.wbw or 0)))]
    from .pools import pool_view
    ListView("Live I/O", fetch, cols, sorts, lambda s: s.pool, lambda s: pool_view(s.pool),
             help_text=helptext.IOSTAT, empty="No pools.",
             header=lambda: [paint(f"ARC: {_arc_line()}", "dim")], live=True).run()


def _os_name() -> str:
    for line in zfs.read_file("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return strip_ctl(line.split("=", 1)[1].strip().strip('"'))
    return platform.system()


def _user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def _version() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    v = zfs.read_file(os.path.join(here, "VERSION")).strip()
    return v or "?"


def system_info() -> None:
    msg = Msg()
    while True:
        rows: List[tuple] = [("zman", f"version {_version()}")]
        zline = TARGET.userland or "?"
        if TARGET.kmod and TARGET.kmod != TARGET.userland:
            rows.append(("ZFS", (f"tools {zline}, kernel module {TARGET.kmod} (they differ; reboot after "
                                 "ZFS upgrades)", "warn")))
        else:
            rows.append(("ZFS", zline))
        rows.append(("Tools", f"{TARGET.bins.get('zfs', '?')}, {TARGET.bins.get('zpool', '?')}"))
        rows.append(("System", f"{_os_name()}, kernel {platform.release()}"))
        if TARGET.is_root:
            rows.append(("Running as", "root"))
        else:
            rows.append(("Running as", (f"{_user()}: reading works; changes need sudo or 'zfs allow'", "warn")))
        rows.append(("Config", CONFIG.path or "none found; using defaults"))
        rows.append(("ARC", _arc_line()))
        sched = zfs.scrub_schedule()
        rows.append(("Scrubs", sched[0] if sched else ("no scheduled scrub found", "warn")))
        for extra in sched[1:3]:
            rows.append(("", extra))
        tools = [t for t in ("sanoid", "zfs-auto-snapshot", "zrepl", "znapzend") if shutil.which(t)]
        rows.append(("Snapshots", f"automatic snapshots by {', '.join(tools)}" if tools else
                     ("no automatic snapshot tool found (zman does not schedule snapshots)", "dim")))
        header = item_header("System info", "", rows)
        header += [paint(w, "warn") for w in CONFIG.warnings]
        menu(header, [], msg)
