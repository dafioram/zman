"""INI config. A bad value becomes a warning shown in the UI, never a crash."""
from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SECTION = "zman"

DEFAULT_TEXT = """\
# zman configuration. Every setting is optional; delete a line to use the default.
[zman]
# Rows per page in lists. 0 fits the terminal height.
page_size = 0
# Seconds to wait for quick zfs/zpool calls before giving up.
command_timeout = 60
# Seconds to wait for slow reads such as zfs diff or zpool history.
long_timeout = 600
# Seconds between redraws when auto-refresh is on.
refresh_interval = 2
# Suggested name start for snapshots taken from zman (name-YYYY-MM-DD-HHMM).
snapshot_prefix = manual
# Tag used when you put a hold on a snapshot.
hold_tag = keep
"""


@dataclass
class Config:
    page_size: int = 0
    command_timeout: int = 60
    long_timeout: int = 600
    refresh_interval: int = 2
    snapshot_prefix: str = "manual"
    hold_tag: str = "keep"
    path: str = ""
    warnings: List[str] = field(default_factory=list)


_INT_LIMITS: Dict[str, tuple] = {
    "page_size": (0, 500),
    "command_timeout": (5, 3600),
    "long_timeout": (10, 86400),
    "refresh_interval": (1, 60),
}
_NAME_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_TAG_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def candidate_paths(cli_path: Optional[str], env: Dict[str, str]) -> List[str]:
    paths = []
    if cli_path:
        paths.append(cli_path)
    if env.get("ZMAN_CONFIG"):
        paths.append(env["ZMAN_CONFIG"])
    home = env.get("HOME") or os.path.expanduser("~")
    paths.append(os.path.join(home, ".config", "zman", "zman.conf"))
    conf_dir = env.get("ZMAN_CONF_DIR") or "/etc/zman"
    paths.append(os.path.join(conf_dir, "zman.conf"))
    return paths


def parse_config(text: str, path: str = "") -> Config:
    cfg = Config(path=path)
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(text, source=path or "<config>")
    except configparser.Error as e:
        first = str(e).splitlines()[0] if str(e) else "unreadable"
        cfg.warnings.append(f"Config: could not read {path or 'file'} ({first}); using defaults.")
        return cfg
    if not parser.has_section(SECTION):
        if parser.sections():
            cfg.warnings.append(f"Config: no [{SECTION}] section in {path or 'file'}; using defaults.")
        return cfg
    for key, raw in parser.items(SECTION):
        value = raw.strip()
        if key in _INT_LIMITS:
            lo, hi = _INT_LIMITS[key]
            if value.isdecimal() and lo <= int(value) <= hi:
                setattr(cfg, key, int(value))
            else:
                cfg.warnings.append(
                    f"Config: {key} = {value} is not a whole number from {lo} to {hi}; "
                    f"using {getattr(cfg, key)}."
                )
        elif key == "snapshot_prefix":
            if _NAME_OK.match(value):
                cfg.snapshot_prefix = value
            else:
                cfg.warnings.append(f"Config: snapshot_prefix = {value} has characters ZFS does not allow; using manual.")
        elif key == "hold_tag":
            if _TAG_OK.match(value):
                cfg.hold_tag = value
            else:
                cfg.warnings.append(f"Config: hold_tag = {value} is not a simple word; using keep.")
        else:
            cfg.warnings.append(f"Config: unknown setting {key}; ignored.")
    return cfg


def load_config(cli_path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Config:
    env = dict(os.environ) if env is None else env
    paths = candidate_paths(cli_path, env)
    for p in paths:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    return parse_config(f.read(), p)
            except OSError as e:
                cfg = Config(path=p)
                cfg.warnings.append(f"Config: cannot read {p} ({e.strerror}); using defaults.")
                return cfg
        if p == cli_path:
            cfg = Config()
            cfg.warnings.append(f"Config: {p} not found; using defaults.")
            return cfg
    return Config()


# Set once at startup by app.main(); read everywhere else.
CONFIG = Config()


def set_config(cfg: Config) -> None:
    """Copy into the shared object so every `from .config import CONFIG` sees it."""
    CONFIG.__dict__.update(cfg.__dict__)
