#!/usr/bin/env python3
"""
Command-line entry for app/strip_carpet.py, which holds the logic (the app
uses it too, for the nightly carpet strip).

    python tools/strip_carpet.py BACKUP.tar.gz [OUT.tar.gz] [--scan-only]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strip_carpet import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
