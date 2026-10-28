"""The main menu."""
from __future__ import annotations

from typing import Dict

from .. import model as M
from .. import zfs
from ..config import CONFIG
from ..errors import ToMain
from ..runner import run
from ..textfmt import plural, strip_ctl, truncate
from ..ui import Msg, Opt, ellipsis, menu, nav, paint, term_size
from . import helptext


def summary(width: int) -> str:
    """The live one-line summary after the app name, with problem counts in red."""
    pools, perr = zfs.list_pools()
    r = run(["zfs", "list", "-H", "-o", "type", "-t", "filesystem,volume"])
    types = r.out.split() if r.ok else []
    counts = "ZFS on this host - " + ", ".join([
        plural(len(pools), "pool"), plural(types.count("filesystem"), "dataset"),
        plural(types.count("volume"), "volume")])
    states: Dict[str, int] = {}
    for p in pools:
        probs = M.pool_problems(p.status)
        if not probs:
            continue
        word = p.health.lower() if p.health and p.health != "ONLINE" else "with errors"
        states[word] = states.get(word, 0) + 1
    probs_text = ", ".join(f"{n} {w}" for w, n in states.items())
    if perr:
        probs_text = (probs_text + ", " if probs_text else "") + strip_ctl(perr)
    room = width - len("zman  ")
    if probs_text:
        counts = truncate(counts, max(10, room - len(probs_text) - 2), ellipsis())
        return paint(counts + ", ", "dim") + paint(truncate(probs_text, room - len(counts) - 2, ellipsis()), "bad")
    return paint(truncate(counts, room, ellipsis()), "dim")


def main_menu() -> None:
    from .datasets import datasets_screen, volumes_screen
    from .disks import disks_screen
    from .info import live_io_screen, system_info
    from .pools import pools_screen
    from .snapshots import snapshots_screen
    from .space import space_screen

    targets = {"1": pools_screen, "2": datasets_screen, "3": snapshots_screen, "4": volumes_screen,
               "5": disks_screen, "6": live_io_screen, "7": space_screen, "8": system_info}
    opts = [Opt("1", "Pools"), Opt("2", "Datasets"), Opt("3", "Snapshots"), Opt("4", "Volumes"),
            Opt("5", "Disks"), Opt("6", "Live I/O"), Opt("7", "Space & cleanup"), Opt("8", "System info"),
            Opt("h", "Help")]
    msg = Msg()
    while True:
        try:
            width, _ = term_size()
            header = [paint("zman", "title") + "  " + summary(width)]
            header += [paint(w, "warn") for w in CONFIG.warnings]
            key = menu(header, opts, msg, top=True, help_text=helptext.MAIN)
            if key in targets:
                nav(msg, targets[key])
        except ToMain:
            continue
