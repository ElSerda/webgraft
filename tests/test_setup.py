"""setup's optional step that raises web.extract_char_limit, and uninstall putting it back."""

import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()
cli = importlib.import_module("webgraft.cli")


class FakeHermes:
    """config.yaml as a dict, written through Hermes's config functions as setup does."""

    def __init__(self, monkeypatch, tmp_path, web=None):
        self.config = {"web": dict(web or {})}
        config = types.ModuleType("hermes_cli.config")
        config.set_config_value = self.set
        config.unset_config_value = self.unset
        config.load_config_readonly = lambda: self.config
        pkg = types.ModuleType("hermes_cli")
        pkg.config = config
        monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
        monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path)

    def set(self, key, value):
        section, name = key.split(".", 1)
        self.config.setdefault(section, {})[name] = int(value) if value.isdigit() else value

    def unset(self, key):
        section, name = key.split(".", 1)
        self.config.get(section, {}).pop(name, None)


def args(char_limit=None):
    return types.SimpleNamespace(char_limit=char_limit)


def test_nothing_changes_without_a_yes(monkeypatch, tmp_path):
    hermes = FakeHermes(monkeypatch, tmp_path)
    cli.step_char_limit(args(), cli.state(), interactive=False)  # --yes alone: the default is no
    assert "extract_char_limit" not in hermes.config["web"]
    cli.step_char_limit(args(False), cli.state(), interactive=False)
    assert "extract_char_limit" not in hermes.config["web"]


def test_a_yes_raises_it_and_uninstall_puts_hermes_default_back(monkeypatch, tmp_path):
    hermes = FakeHermes(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.step_char_limit(args(), cli.state(), interactive=True)
    assert hermes.config["web"]["extract_char_limit"] == cli.SUGGESTED_CHAR_LIMIT
    assert cli._previous() == {"web.extract_char_limit": None}
    monkeypatch.setattr(cli, "_rmtree", lambda path: None)  # keep the test folder
    cli.uninstall(interactive=False)
    assert "extract_char_limit" not in hermes.config["web"]  # unset: Hermes's own default again


def test_a_value_set_before_is_restored(monkeypatch, tmp_path):
    hermes = FakeHermes(monkeypatch, tmp_path, {"extract_char_limit": 20_000})
    cli.step_char_limit(args(40_000), cli.state(), interactive=False)
    assert hermes.config["web"]["extract_char_limit"] == 40_000
    monkeypatch.setattr(cli, "_rmtree", lambda path: None)
    cli.uninstall(interactive=False)
    assert hermes.config["web"]["extract_char_limit"] == 20_000


def test_an_already_wide_window_is_left_alone(monkeypatch, tmp_path):
    hermes = FakeHermes(monkeypatch, tmp_path, {"extract_char_limit": 30_000})
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    cli.step_char_limit(args(), cli.state(), interactive=True)
    assert hermes.config["web"]["extract_char_limit"] == 30_000 and cli._previous() == {}


def test_values_are_kept_within_hermes_bounds(monkeypatch, tmp_path):
    hermes = FakeHermes(monkeypatch, tmp_path)
    cli.step_char_limit(args(10_000_000), cli.state(), interactive=False)
    assert hermes.config["web"]["extract_char_limit"] == 500_000


def test_the_focus_setting_is_gone():
    manifest = (Path(cli.__file__).parent / "plugin.yaml").read_text(encoding="utf-8")
    assert "\n  focus:" not in manifest
    assert not (Path(cli.__file__).parent / "focus.py").exists()


def test_not_offered_on_a_model_with_a_small_context(monkeypatch, tmp_path):
    hermes = FakeHermes(monkeypatch, tmp_path)
    hermes.config["model"] = {"context_length": 32_768}
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    cli.step_char_limit(args(), cli.state(), interactive=True)
    assert "extract_char_limit" not in hermes.config["web"]
