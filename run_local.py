"""Run one bundled Python script with the package root on sys.path."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if len(sys.argv) < 2:
    raise SystemExit("missing script name")

sys.path.insert(0, str(ROOT))
runpy.run_path(str(ROOT / sys.argv[1]), run_name="__main__")
