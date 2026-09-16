"""Reproducible local environment used by both plugin installs and development."""

import subprocess
import sys
import venv
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if sys.version_info < (3, 11):  # noqa: UP036 - bootstrap can run before package installation.
    raise SystemExit("Herdr Tasks requires Python 3.11 or later")
python = root / ".venv/bin/python"
if not python.exists():
    venv.EnvBuilder(with_pip=True).create(root / ".venv")
subprocess.run(
    [str(python), "-m", "pip", "install", "-r", str(root / "requirements.lock")],
    check=True,
    cwd=root,
)
subprocess.run(
    [str(python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", str(root)],
    check=True,
    cwd=root,
)
