"""System access: running the ZFS tools, pinned to the binaries found at startup."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import CONFIG
from .textfmt import strip_ctl

# Extra places to look: on Debian a normal user's PATH has no sbin directories.
SBIN_DIRS = ["/usr/local/sbin", "/usr/sbin", "/sbin"]
REQUIRED = ["zfs", "zpool", "lsblk"]
OPTIONAL = ["sfdisk", "udevadm", "less", "systemctl"]
# Environment variables that change how zpool prints device names; captured
# output must stay in the format the parsers expect.
_STRIP_ENV = (
    "ZPOOL_VDEV_NAME_PATH",
    "ZPOOL_VDEV_NAME_GUID",
    "ZPOOL_VDEV_NAME_FOLLOW_LINKS",
    "ZFS_COLOR",
    "ZFS_ABORT",
)


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def reason(self) -> str:
        """The first meaningful line of what the command said, cleaned for printing."""
        for text in (self.err, self.out):
            for line in text.splitlines():
                line = strip_ctl(line).strip()
                if line:
                    return line[:300]
        return f"exit code {self.rc}"


@dataclass
class Target:
    """The local ZFS install every call is pinned to. Set once at startup."""

    bins: Dict[str, str] = field(default_factory=dict)
    dev_zfs: str = "/dev/zfs"
    by_id_dir: str = "/dev/disk/by-id"
    userland: str = ""
    kmod: str = ""
    is_root: bool = False


TARGET = Target()


def set_target(t: Target) -> None:
    TARGET.__dict__.update(t.__dict__)


def find_bin(name: str, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    found = which(name)
    if found:
        return os.path.realpath(found)
    for d in SBIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.realpath(p)
    return None


def _resolve(args: Sequence[str]) -> List[str]:
    argv = [str(a) for a in args]
    if argv and argv[0] in TARGET.bins:
        argv[0] = TARGET.bins[argv[0]]
    return argv


def _base_env() -> Dict[str, str]:
    env = dict(os.environ)
    for k in _STRIP_ENV:
        env.pop(k, None)
    return env


def capture_env() -> Dict[str, str]:
    env = _base_env()
    # The parsers read dates and numbers in the C locale.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def run(args: Sequence[str], timeout: Optional[float] = None, cwd: Optional[str] = None,
        input: Optional[str] = None) -> Result:
    """Run a command captured, with a timeout, stdin closed, never through a shell."""
    argv = _resolve(args)
    limit = timeout or CONFIG.command_timeout
    try:
        p = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL if input is None else None,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            cwd=cwd,
            env=capture_env(),
        )
        return Result(p.returncode, p.stdout, p.stderr)
    except FileNotFoundError:
        return Result(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{args[0]} did not finish within {int(limit)}s.")
    except KeyboardInterrupt:
        return Result(130, "", "Interrupted.")
    except OSError as e:
        return Result(126, "", f"{args[0]}: {e.strerror or e}")


def _noop(signum: int, frame: object) -> None:  # pragma: no cover - signal handler
    pass


def run_fg(args: Sequence[str], cwd: Optional[str] = None) -> int:
    """Run attached to the terminal. Ctrl-C reaches the child, not zman."""
    argv = _resolve(args)
    old = signal.getsignal(signal.SIGINT)
    # A no-op Python handler (not SIG_IGN) so the child keeps default Ctrl-C behavior.
    signal.signal(signal.SIGINT, _noop)
    try:
        sys.stdout.flush()
        return subprocess.call(argv, cwd=cwd, env=_base_env())
    except FileNotFoundError:
        print(f"{args[0]}: command not found")
        return 127
    except OSError as e:
        print(f"{args[0]}: {e.strerror or e}")
        return 126
    finally:
        signal.signal(signal.SIGINT, old)


_HINTS: List[Tuple[Tuple[str, ...], str]] = [
    (("permission denied", "must be root", "insufficient privileges", "requires root",
      "operation not permitted"),
     "Run zman with sudo, or delegate this with 'zfs allow'."),
    (("did not finish within",),
     "ZFS is slow to answer; raise command_timeout in the config if this keeps happening."),
    (("is busy", "target is busy", "device or resource busy"),
     "Something is using it: close programs, shells or NFS/SMB shares inside it, then try again."),
    (("dataset already exists", "already exists"), "Pick another name."),
    (("too small",), "The new disk must be at least as large as the one it replaces."),
    (("out of space", "no space left"), "The pool is full; free some space first."),
    (("key not loaded", "keys are not loaded", "encryption key not loaded"),
     "Load the encryption key first."),
    (("dependent clones",), "Destroy or promote the clones first."),
    (("snapshot is held", "dataset is busy"), "Release the hold first."),
    (("no such pool", "does not exist", "no such device"),
     "It may have changed outside zman; press Enter to refresh."),
    (("contains a ", "is part of", "in use"),
     "The disk holds or is used by something else; check it on the Disks screen."),
    (("sfdisk: command not found",), "Install it with: apt install fdisk"),
    (("no valid replicas", "insufficient replicas"),
     "The pool needs this device to keep working; replace it instead."),
]


def hint_for(text: str) -> str:
    low = text.lower()
    for needles, hint in _HINTS:
        if any(n in low for n in needles):
            return hint
    return ""


def startup_check(which: Callable[[str], Optional[str]] = shutil.which,
                  exists: Callable[[str], bool] = os.path.exists,
                  runner: Callable[..., Result] = None,
                  base: Optional[Target] = None) -> Tuple[Optional[Target], str]:
    """Find and pin the local ZFS install. Returns (target, "") or (None, one-line problem)."""
    if not sys.platform.startswith("linux"):
        return None, f"zman manages ZFS on Linux; this system is {sys.platform}."
    t = Target(**(base.__dict__ if base else {}))
    t.bins = {}
    for name in REQUIRED:
        p = find_bin(name, which)
        if not p:
            if name == "lsblk":
                return None, "zman: lsblk is missing. Hint: apt install util-linux"
            return None, ("zman: the ZFS tools (zfs, zpool) are not installed. "
                          "Hint: apt install zfsutils-linux")
        t.bins[name] = p
    if os.path.dirname(t.bins["zfs"]) != os.path.dirname(t.bins["zpool"]):
        return None, (f"zman: zfs ({t.bins['zfs']}) and zpool ({t.bins['zpool']}) come from "
                      "different installs. Hint: fix PATH so both come from the same place.")
    for name in OPTIONAL:
        p = find_bin(name, which)
        if p:
            t.bins[name] = p
    if not exists(t.dev_zfs):
        return None, ("zman: the ZFS kernel module is not loaded. "
                      "Hint: sudo modprobe zfs (install zfs-dkms if it is missing)")
    t.is_root = os.geteuid() == 0
    set_target(t)
    runner = runner or run
    r = runner(["zpool", "list", "-H", "-o", "name"])
    if not r.ok:
        low = (r.err + r.out).lower()
        if "permission" in low:
            return None, "zman: cannot talk to ZFS (permission denied). Hint: run with sudo"
        return None, f"zman: zpool did not answer ({r.reason}). Hint: is the zfs module loaded? Try: sudo modprobe zfs"
    v = runner(["zfs", "version"])
    if v.ok:
        for line in v.out.splitlines():
            line = strip_ctl(line).strip()
            if line.startswith("zfs-kmod-"):
                t.kmod = line[len("zfs-kmod-"):]
            elif line.startswith("zfs-"):
                t.userland = line[len("zfs-"):]
    set_target(t)
    return t, ""
