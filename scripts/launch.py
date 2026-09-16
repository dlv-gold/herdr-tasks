"""Manifest entrypoint: use the plugin's own environment regardless of PATH."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
python = root / ".venv/bin/python"
if not python.exists():
    raise SystemExit("Herdr Tasks is not built. Run: python3 scripts/bootstrap.py")
os.execv(str(python), [str(python), "-m", "herdr_tasks", *sys.argv[1:]])
