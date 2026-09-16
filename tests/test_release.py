"""Exercise the real build with synthetic private files in an isolated checkout."""

import shutil
import subprocess
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path


def test_release_excludes_local_credentials_and_state(tmp_path):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "CHANGELOG.md",
        "AGENTS.md",
        ".gitignore",
        "requirements.lock",
        "herdr-plugin.toml",
    ):
        shutil.copy2(root / name, checkout / name)
    for name in ("src", "scripts", "tests", "docs", "notes"):
        shutil.copytree(
            root / name,
            checkout / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
        )

    sentinel = ("private-release-sentinel-" + uuid.uuid4().hex).encode()
    private_names = (
        ".env",
        ".env.production",
        "auth.json",
        "auth.json.backup",
        ".credentials.json",
        "credentials.json",
        "secrets.json",
        "account.pem",
        "account.key",
        "account.p12",
        "account.pfx",
        "tasks.sqlite3",
        "tasks.sqlite3-wal",
        "tasks.sqlite3.before-spaces",
        "tasks.db",
        "tasks.db-wal",
        "coordinator.log",
        "settings.bak",
    )
    for directory in (checkout, checkout / "src/herdr_tasks"):
        for name in private_names:
            (directory / name).write_bytes(sentinel)
    for name in (
        ".codex/auth.json",
        ".claude/.credentials.json",
        ".config/herdr/plugins/config/herdr-tasks/config.json",
        ".local/state/herdr/plugins/herdr-tasks/tasks.sqlite3",
        "reports/private.md",
    ):
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sentinel)

    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation"],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    with tarfile.open(next((checkout / "dist").glob("*.tar.gz"))) as archive:
        source_files = {
            member.name.split("/", 1)[1]: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }
    with zipfile.ZipFile(next((checkout / "dist").glob("*.whl"))) as archive:
        wheel_files = {name: archive.read(name) for name in archive.namelist()}

    assert {
        "herdr-plugin.toml",
        "scripts/install.py",
        "scripts/bootstrap.py",
    } <= source_files.keys()
    assert "herdr_tasks/providers.py" in wheel_files
    for files in (source_files, wheel_files):
        for name, content in files.items():
            assert Path(name).name not in private_names, name
            assert sentinel not in content, name
            assert not {".codex", ".claude", ".config", ".local", ".venv", "reports"}.intersection(
                Path(name).parts
            ), name
