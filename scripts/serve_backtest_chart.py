from __future__ import annotations

import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Launch the chart server and time wall-clock cold start until the port is bound."""
    # Start before the heavy import so import + replay + bind are all included.
    started_at = time.perf_counter()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)
    from src.backtest_chart_server import main as serve_main  # noqa: E402

    return serve_main(started_at=started_at)


if __name__ == "__main__":
    raise SystemExit(main())
