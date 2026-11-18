import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()


@pytest.fixture(autouse=True)
def fresh_kanban_state(tmp_path, monkeypatch):
    """Kanban cards recorded by tool_guard: a temporary folder, never the real plugin-data, and no
    dispatcher environment unless a test sets it."""
    import webgraft
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    monkeypatch.setattr(webgraft, "_cards_dir", lambda: tmp_path / "plugin-data" / "webgraft" / "kanban_cards")
    webgraft._kanban_cards.clear()
    webgraft._handoff_files.clear()
    yield
    webgraft._kanban_cards.clear()
    webgraft._handoff_files.clear()


@pytest.fixture(autouse=True)
def fresh_rescue_state():
    """rescue.py keeps process-wide records (held pages, approvals, a failed search backend)."""
    from webgraft import rescue
    rescue.reset()
    yield
    rescue.reset()
