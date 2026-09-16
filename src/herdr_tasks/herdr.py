"""Explicit-target Herdr 0.9 CLI/socket adapter. No layout replacement."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path

from . import PLUGIN_ID
from .config import Config
from .executable import resolve_herdr
from .process import ProcessError, execute, lock
from .store import Store


class HerdrError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class Herdr:
    def __init__(self, binary: str | None = None):
        self._binary = binary

    @property
    def binary(self) -> str:
        # Resolve at invocation time: Herdr can be replaced while the coordinator runs.
        return resolve_herdr(self._binary)

    def call(
        self, endpoint: str, method: str, params: dict | None = None, timeout: float = 10
    ) -> dict:
        request_id = uuid.uuid4().hex
        message = json.dumps({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        data = bytearray()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(endpoint)
                connection.sendall((message + "\n").encode())
                while b"\n" not in data:
                    connection.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = connection.recv(65536)
                    if not chunk:
                        raise HerdrError("disconnected", "Herdr closed the connection")
                    data.extend(chunk)
                    if len(data) > 4_000_000:
                        raise HerdrError("response_limit", "Herdr response exceeded 4 MB")
            result = json.loads(data.split(b"\n", 1)[0])
            if result.get("id") != request_id:
                raise HerdrError("protocol", "Mismatched response ID")
            if "error" in result:
                raise HerdrError(result["error"]["code"], result["error"]["message"])
            return result["result"]
        except (OSError, ValueError, KeyError) as exc:
            raise HerdrError(
                "unavailable", f"Cannot complete {method}: {type(exc).__name__}"
            ) from exc

    def sessions(self) -> list[dict]:
        data = json.loads(execute([self.binary, "session", "list", "--json"], timeout=15))
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        running = [item for item in sessions if item["running"]]
        inherited = os.environ.get("HERDR_SOCKET_PATH")
        if inherited and all(item["socket_path"] != inherited for item in running):
            try:
                self.call(inherited, "ping")
                running.append({"name": "current", "socket_path": inherited, "running": True})
            except HerdrError:
                pass
        return running

    def snapshot(self, endpoint: str) -> dict:
        return self.call(endpoint, "session.snapshot")["snapshot"]

    def enabled(self, endpoint: str) -> bool:
        plugins = self.call(endpoint, "plugin.list", {"plugin_id": PLUGIN_ID})["plugins"]
        return any(p["plugin_id"] == PLUGIN_ID and p["enabled"] for p in plugins)

    def read(self, endpoint: str, pane_id: str, lines: int = 120) -> str:
        result = self.call(
            endpoint,
            "agent.read",
            {"target": pane_id, "source": "recent_unwrapped", "lines": lines, "format": "text"},
        )
        return result["read"]["text"][-16000:]

    def agent(self, endpoint: str, pane_id: str) -> dict:
        return self.call(endpoint, "agent.get", {"target": pane_id})["agent"]


def agent_key(endpoint: str, agent: dict) -> str:
    identity = [endpoint, agent["terminal_id"], agent.get("agent_session"), agent.get("agent")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def same_agent(first: dict, second: dict) -> bool:
    return all(
        first.get(key) == second.get(key) for key in ("terminal_id", "agent", "agent_session")
    )


def ready(agent: dict) -> bool:
    return (
        agent.get("agent_status") in {"idle", "done"}
        and agent.get("interactive_ready", True)
        and not agent.get("launch_pending", False)
    )


def project_root(cwd: str, provenance: dict | None = None) -> tuple[str, str]:
    path = Path(cwd).expanduser().resolve()
    if provenance and provenance.get("repo_root"):
        root = Path(provenance["repo_root"]).resolve()
        return str(root), provenance.get("repo_name", root.name)
    try:
        common = execute(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=3,
        ).strip()
        root = (
            Path(common).resolve().parent
            if Path(common).name == ".git"
            else Path(
                execute(["git", "-C", str(path), "rev-parse", "--show-toplevel"], timeout=3).strip()
            ).resolve()
        )
        return str(root), root.name
    except ProcessError:
        return str(path), path.name or str(path)


def git_evidence(root: str, since: str) -> dict:
    if not root:
        return {"unavailable": True}
    env = os.environ.copy()
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        status = execute(
            ["git", "-C", root, "--no-pager", "status", "--short", "--untracked-files=no"],
            env=env,
            timeout=5,
            limit=50000,
        )
        commits = execute(
            [
                "git",
                "-C",
                root,
                "--no-pager",
                "log",
                "--max-count=15",
                f"--since={since}",
                "--format=%h %s",
            ],
            env=env,
            timeout=5,
            limit=20000,
        )
        return {"status": status[:8000], "commits": commits[:5000]}
    except ProcessError:
        return {"unavailable": True}


def toggle_panel(
    api: Herdr,
    store: Store,
    config: Config,
    endpoint: str,
    pane_id: str,
    *,
    view: str = "today",
    open_only: bool = False,
) -> dict:
    with lock(store.path.parent / "panel.lock", blocking=True):
        snapshot = api.snapshot(endpoint)
        current = next((p for p in snapshot["panes"] if p["pane_id"] == pane_id), None)
        if not current:
            raise ValueError("The invoking pane is unavailable")
        records = store.panel_records()
        owned_terminals = {r.get("terminal_id") for r in records if r.get("endpoint") == endpoint}
        existing = next(
            (
                p
                for p in snapshot["panes"]
                if p["terminal_id"] in owned_terminals and p["tab_id"] == current["tab_id"]
            ),
            None,
        )
        if existing:
            if open_only:
                api.call(endpoint, "plugin.pane.focus", {"pane_id": existing["pane_id"]})
                store.set_meta("view:" + existing["terminal_id"], view)
                return existing
            # Stored terminal identity and the host's plugin-only close endpoint both guard ownership.
            api.call(endpoint, "plugin.pane.close", {"pane_id": existing["pane_id"]})
            key = (
                "panel:"
                + hashlib.sha256(f"{endpoint}:{existing['terminal_id']}".encode()).hexdigest()
            )
            store.set_meta(key, "{}")
            return {"closed": existing["pane_id"]}
        layout = next(x for x in snapshot["layouts"] if x["tab_id"] == current["tab_id"])
        right = max(
            layout["panes"],
            key=lambda p: (
                p["rect"]["x"] + p["rect"]["width"],
                p["rect"]["height"],
                -p["rect"]["y"],
            ),
        )
        result = api.call(
            endpoint,
            "plugin.pane.open",
            {
                "plugin_id": PLUGIN_ID,
                "entrypoint": "board",
                "placement": "split",
                "target_pane_id": right["pane_id"],
                "direction": "right",
                "focus": True,
                "env": {
                    "HERDR_TASKS_VIEW": view,
                    "HERDR_TASKS_WORKSPACE_ID": current["workspace_id"],
                },
            },
        )["plugin_pane"]
        if result["plugin_id"] != PLUGIN_ID:
            raise ValueError("Unexpected plugin ownership")
        panel = result["pane"]
        key = "panel:" + hashlib.sha256(f"{endpoint}:{panel['terminal_id']}".encode()).hexdigest()
        store.set_meta(key, json.dumps(panel | {"endpoint": endpoint}))
        try:
            exported = api.call(endpoint, "layout.export", {"tab_id": current["tab_id"]})["layout"]
            path = parent_path(exported["root"], panel["pane_id"])
            if path is not None:
                api.call(
                    endpoint,
                    "layout.set_split_ratio",
                    {"tab_id": current["tab_id"], "path": path, "ratio": 1 - config.panel_fraction},
                )
        except (HerdrError, KeyError):
            pass  # A usable host-default split remains open when sizing isn't supported.
        return panel


def parent_path(node: dict, pane_id: str, path: list[bool] | None = None) -> list[bool] | None:
    path = path or []
    if node["type"] != "split":
        return None
    for flag, side in ((False, "first"), (True, "second")):
        child = node[side]
        if child["type"] == "pane" and child.get("pane_id") == pane_id:
            return path
        result = parent_path(child, pane_id, path + [flag])
        if result is not None:
            return result
    return None
