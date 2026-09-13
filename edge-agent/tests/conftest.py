from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bytes():
    def _load(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return _load


def chunked(payload: bytes, size: int) -> list[bytes]:
    """Split a byte string into fixed-size chunks (simulates TCP read boundaries)."""
    return [payload[i : i + size] for i in range(0, len(payload), size)]
