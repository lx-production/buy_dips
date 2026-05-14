from __future__ import annotations


PAPER_MODE_WARNING = (
    "PHASE 1 PAPER SIGNAL MODE ONLY: no real trades, no wallets, no private keys, "
    "and no blockchain transactions."
)


def assert_paper_mode_only() -> None:
    """Document the Phase 1 safety boundary in the runtime path."""
    return None
