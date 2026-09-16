import importlib.util
from pathlib import Path

import pytest

from herdr_tasks.executable import resolve_herdr
from herdr_tasks.herdr import Herdr


@pytest.fixture
def installed_herdr(tmp_path, monkeypatch):
    binary = tmp_path / "herdr"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HERDR_BIN_PATH", str(binary) + " (deleted)")
    return binary


def test_stale_inherited_path_falls_back_for_coordinator(installed_herdr):
    assert resolve_herdr() == str(installed_herdr)
    assert Herdr().binary == str(installed_herdr)


def test_valid_inherited_executable_keeps_precedence(installed_herdr, monkeypatch):
    custom = installed_herdr.with_name("custom-herdr")
    custom.write_text("#!/bin/sh\nexit 0\n")
    custom.chmod(0o755)
    monkeypatch.setenv("HERDR_BIN_PATH", str(custom))
    assert resolve_herdr() == str(custom)


def test_explicit_missing_executable_does_not_silently_fall_back(installed_herdr):
    with pytest.raises(ValueError, match="unavailable"):
        resolve_herdr(str(installed_herdr) + ".missing")


def test_nonexecutable_inherited_path_falls_back(installed_herdr, monkeypatch):
    candidate = installed_herdr.with_name("not-executable")
    candidate.write_text("not a program")
    monkeypatch.setenv("HERDR_BIN_PATH", str(candidate))
    assert resolve_herdr() == str(installed_herdr)


def test_missing_herdr_has_actionable_error(installed_herdr):
    installed_herdr.unlink()
    with pytest.raises(ValueError, match="Add herdr to PATH"):
        resolve_herdr()


def test_installer_links_and_starts_using_current_executable(installed_herdr, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/install.py"
    spec = importlib.util.spec_from_file_location("herdr_tasks_install", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    assert installer.main() == 0
    assert calls[1] == [str(installed_herdr), "plugin", "link", str(path.parents[1])]
    assert calls[2] == [str(installed_herdr), "plugin", "action", "invoke", "herdr-tasks.start"]
