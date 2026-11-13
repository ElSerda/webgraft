import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()


@pytest.fixture(autouse=True)
def fresh_rescue_state():
    """rescue.py keeps process-wide records (held pages, approvals, a failed search backend)."""
    from webgraft import rescue
    rescue.reset()
    yield
    rescue.reset()
