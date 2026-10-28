#!/usr/bin/env python3
"""Run zman against the mock system, without ZFS installed.

    python3 tests/mock/run_mock.py            # fresh mock in a temporary folder
    python3 tests/mock/run_mock.py DIR        # keep the mock in DIR (built if missing)

Used by the walkthrough, and handy for trying the screens on any machine.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

import scenario  # noqa: E402
import zmanlib.app as app  # noqa: E402
from zmanlib import runner  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1:
        root = os.path.abspath(sys.argv[1])
        cleanup = False
    else:
        root = tempfile.mkdtemp(prefix="zman-mock-")
        cleanup = True
    if not os.path.exists(os.path.join(root, "state.json")):
        env = scenario.build(root)
    else:
        env = {"ZMAN_MOCK_STATE": os.path.join(root, "state.json"),
               "PATH": os.path.join(root, "bin") + os.pathsep + os.environ.get("PATH", "")}
    os.environ.update(env)
    base = runner.Target(dev_zfs=os.path.join(root, "dev", "zfs"), by_id_dir=os.path.join(root, "by-id"))
    real_check = runner.startup_check

    def check(*a, **kw):
        kw.setdefault("base", base)
        return real_check(*a, **kw)

    app.startup_check = check
    os.environ.setdefault("ZMAN_CONFIG", os.path.join(root, "zman.conf"))
    try:
        return app.main(sys.argv[2:] if len(sys.argv) > 2 else [])
    finally:
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
