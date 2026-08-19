from __future__ import annotations

import sys
import time
import threading


class ElapsedTicker:
    """Rewrite one stdout line with whole elapsed seconds until the context exits."""

    def __init__(self, started_at: float) -> None:
        """Bind to a perf_counter timestamp taken at process start."""
        self._started_at = started_at
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def __enter__(self) -> ElapsedTicker:
        """Print Elapsed: 0s, then increment the same line every second."""
        self._write(0)
        self._thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        """Stop the background tick and leave a final elapsed line."""
        self._stop.set()
        self._thread.join(timeout=2.0)
        elapsed = int(time.perf_counter() - self._started_at)
        suffix = " (failed)" if exc_type is not None else ""
        # Pad so a shorter final line cannot leave leftover characters.
        message = f"Elapsed: {elapsed}s{suffix}"
        sys.stdout.write(f"\r{message:<32}\n")
        sys.stdout.flush()

    def _tick(self) -> None:
        """Sleep one second at a time and refresh the line until stop is set."""
        # wait() returns True once stop is set, so the loop exits without an extra write.
        while not self._stop.wait(1.0):
            self._write(int(time.perf_counter() - self._started_at))

    def _write(self, seconds: int) -> None:
        """Overwrite the current terminal line with the whole-second elapsed value."""
        sys.stdout.write(f"\rElapsed: {seconds}s")
        sys.stdout.flush()
