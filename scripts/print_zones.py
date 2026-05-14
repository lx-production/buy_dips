from __future__ import annotations

import sys
from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["zones"]))
