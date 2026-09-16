"""Resolve the installed Herdr executable without trusting stale pane metadata."""

import os
import shutil


def resolve_herdr(binary: str | None = None) -> str:
    if binary is not None:
        found = shutil.which(binary)
        if not found:
            raise ValueError(f"Herdr executable is unavailable: {binary}")
        return found

    inherited = os.environ.get("HERDR_BIN_PATH")
    if inherited and (found := shutil.which(inherited)):
        return found
    if found := shutil.which("herdr"):
        return found
    raise ValueError(
        "Cannot find an executable Herdr. Add herdr to PATH or set HERDR_BIN_PATH to its installed path."
    )
