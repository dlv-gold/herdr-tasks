"""Build, link the local checkout, and start its scheduling coordinator."""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from herdr_tasks.executable import resolve_herdr

    try:
        herdr = resolve_herdr()
        subprocess.run([sys.executable, str(root / "scripts/bootstrap.py")], cwd=root, check=True)
        subprocess.run([herdr, "plugin", "link", str(root)], check=True)
        subprocess.run([herdr, "plugin", "action", "invoke", "herdr-tasks.start"], check=True)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Herdr Tasks installation failed: {exc}", file=sys.stderr)
        return 1
    print("Installed. From a Herdr terminal, run: herdr plugin action invoke herdr-tasks.toggle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
