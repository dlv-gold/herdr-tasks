"""Bounded subprocess execution and Linux process locks."""

from __future__ import annotations

import fcntl
import os
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class ProcessError(RuntimeError):
    pass


def execute(
    argv: list[str],
    *,
    timeout: float = 15,
    input_text: str = "",
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    limit: int = 2_000_000,
) -> str:
    """Drain both pipes with an aggregate cap; terminate the owned process group."""
    with tempfile.TemporaryFile() as source:
        source.write(input_text.encode())
        source.seek(0)
        try:
            child = subprocess.Popen(
                argv,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessError(f"Cannot start {Path(argv[0]).name}: {exc.strerror}") from exc
        output = bytearray()
        total = 0
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (child.stdout, child.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProcessError(f"{Path(argv[0]).name} timed out after {timeout:g}s")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > limit:
                            raise ProcessError(f"{Path(argv[0]).name} exceeded its output limit")
                        if key.fileobj is child.stdout:
                            output.extend(data)
                code = child.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                # Provider stderr can contain authentication data; don't persist it.
                raise ProcessError(
                    f"{Path(argv[0]).name} exited with code {code}; check CLI login/configuration"
                )
            return output.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise ProcessError(f"{Path(argv[0]).name} timed out") from exc
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            child.stdout.close()
            child.stderr.close()


@contextmanager
def lock(path: Path, blocking: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as stream:
        acquired = False
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(stream, fcntl.LOCK_UN)
