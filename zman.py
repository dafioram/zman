#!/usr/bin/env python3
"""zman entry script. Run it; do not import it."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from zmanlib.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
