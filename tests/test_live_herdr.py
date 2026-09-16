"""Opt-in smoke test. Every server, pane, and config directory is test-owned."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from herdr_tasks.collector import Collector
from herdr_tasks.config import Config, Paths, Role
from herdr_tasks.herdr import Herdr, HerdrError
from herdr_tasks.schedule import period_keys, utcnow
from herdr_tasks.store import Store

pytestmark = pytest.mark.skipif(
    os.environ.get("HERDR_TASKS_LIVE_TEST") != "1",
    reason="Set HERDR_TASKS_LIVE_TEST=1 for an isolated local Herdr smoke test",
)


def eventually(check, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (HerdrError, FileNotFoundError, KeyError) as exc:
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"Condition did not become true: {last}")


@pytest.fixture
def short_tmp(tmp_path):
    # Herdr creates multiple Unix sockets; long pytest paths exceed sun_path.
    with tempfile.TemporaryDirectory(prefix="ht-") as directory:
        path = Path(directory)
        yield path
        for item in path.rglob("*.log"):
            relative = item.relative_to(path)
            destination = tmp_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, destination)
        if (path / "panel.txt").exists():
            shutil.copyfile(path / "panel.txt", tmp_path / "panel.txt")


def test_owned_session_panel_and_fake_provider_pipeline(short_tmp):
    tmp_path = short_tmp
    binary = shutil.which("herdr")
    assert binary, "Herdr must be installed for the live smoke test"
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
    env.update(
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        SHELL="/bin/sh",
        TERM="xterm-256color",
    )
    config_dir = tmp_path / "config/herdr"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'onboarding = false\n[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n'
    )
    paths = Paths(
        config_dir / "plugins/config/herdr-tasks", tmp_path / "state/herdr/plugins/herdr-tasks"
    )
    paths.create()
    fake = tmp_path / "fake-codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """import json,sys
from pathlib import Path
args=sys.argv[1:]
data=json.loads(sys.stdin.read().split("\\n\\nINPUT\\n",1)[1])
schema=json.loads(Path(args[args.index("--output-schema")+1]).read_text())
if "projects" in schema["properties"]:
 result={"coverage":"Isolated smoke test", "projects":[{"project_id":p["project_id"],"summary":"Smoke report generated","status":"unknown","accomplishments":[],"unfinished":[],"blockers":[],"weekly_summary":"Smoke week" if data["spec"]["weekly"] else None} for p in data["projects"]]}
else:
 result={"tasks":[{"project_id":p["project_id"],"existing_task_id":None,"parent_id":None,"title":"Generated smoke task","notes":"Fake provider output","kind":"daily"} for p in data["projects"]]}
Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(result))
"""
    )
    fake.chmod(0o755)
    Config(enabled=False, summary=Role(binary=str(fake)), planner=Role(binary=str(fake))).save(
        paths
    )
    subprocess.run(
        [binary, "plugin", "link", str(root)], env=env, check=True, capture_output=True, timeout=15
    )
    session = "tasks-smoke"
    endpoint = str(config_dir / "sessions" / session / "herdr.sock")
    api = Herdr(binary)
    server = None
    with (tmp_path / "server.log").open("wb") as log:
        try:
            server = subprocess.Popen(
                [binary, "--session", session, "server"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            eventually(lambda: api.call(endpoint, "ping"))
            created = api.call(endpoint, "workspace.create", {"cwd": str(tmp_path)})
            source = created["root_pane"]
            other = api.call(endpoint, "workspace.create", {"cwd": str(tmp_path)})["root_pane"]
            store = Store(paths.database)
            Collector(api, store, Config()).scan([{"name": session, "socket_path": endpoint}])
            project = store.space_id(endpoint, source["workspace_id"])
            store.add_task(
                project, "Manual smoke task", "daily", period_keys(utcnow(), Config())[0]
            )
            store.add_task(
                store.space_id(endpoint, other["workspace_id"]),
                "Other space task",
                "daily",
                period_keys(utcnow(), Config())[0],
            )
            assert len(store.projects()) == 2
            before = api.snapshot(endpoint)
            context = {
                "workspace_id": source["workspace_id"],
                "tab_id": source["tab_id"],
                "focused_pane_id": source["pane_id"],
                "focused_pane_cwd": str(tmp_path),
            }
            api.call(
                endpoint,
                "plugin.action.invoke",
                {"action_id": "herdr-tasks.toggle", "context": context},
            )
            snapshot = eventually(
                lambda: (
                    (s if len(s["panes"]) == len(before["panes"]) + 1 else None)
                    if (s := api.snapshot(endpoint))
                    else None
                )
            )
            original_terminals = {p["terminal_id"] for p in before["panes"]}
            panel = next(p for p in snapshot["panes"] if p["terminal_id"] not in original_terminals)

            def panel_text():
                text = api.call(
                    endpoint,
                    "pane.read",
                    {"pane_id": panel["pane_id"], "source": "visible", "format": "text"},
                )["read"]["text"]
                return text if "Manual smoke task" in text and "TASKS" in text else None

            rendered = eventually(panel_text)
            assert "Other space task" not in rendered
            (tmp_path / "panel.txt").write_text(rendered)
            completed = subprocess.run(
                [sys.executable, "-m", "herdr_tasks", "run", "--weekly"],
                env=env,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert completed.returncode == 0, completed.stderr
            assert not any(t["title"] == "Generated smoke task" for t in store.tasks())
            run_id = store.runs()[0]["id"]
            suggestions = store.suggestions(run_id)
            assert len(suggestions) == 2
            for item in suggestions:
                store.accept_suggestion(run_id, item["index"])
            assert sum(t["title"] == "Generated smoke task" for t in store.tasks()) == 2
            assert store.reports() and list((paths.state / "reports").rglob("README.md"))
            assert eventually(lambda: store.get_meta("coordinator_heartbeat"))
            # Source pane processes are still the same after report collection and panel creation.
            assert original_terminals <= {p["terminal_id"] for p in api.snapshot(endpoint)["panes"]}
            api.call(
                endpoint,
                "plugin.action.invoke",
                {"action_id": "herdr-tasks.toggle", "context": context},
            )
            after = eventually(
                lambda: (
                    (s if len(s["panes"]) == len(before["panes"]) else None)
                    if (s := api.snapshot(endpoint))
                    else None
                )
            )
            assert {p["terminal_id"] for p in after["panes"]} == original_terminals
            # Reopen from stale panel metadata, then close using the TUI shortcut.
            api.call(
                endpoint,
                "plugin.action.invoke",
                {"action_id": "herdr-tasks.toggle", "context": context},
            )
            reopened = eventually(
                lambda: next(
                    (
                        p
                        for p in api.snapshot(endpoint)["panes"]
                        if p["terminal_id"] not in original_terminals
                    ),
                    None,
                )
            )
            panel = reopened
            eventually(panel_text)
            api.call(endpoint, "pane.send_keys", {"pane_id": reopened["pane_id"], "keys": ["q"]})
            eventually(
                lambda: (
                    {p["terminal_id"] for p in api.snapshot(endpoint)["panes"]}
                    == original_terminals
                )
            )
        finally:
            if server:
                try:
                    api.call(endpoint, "server.stop", timeout=5)
                except HerdrError:
                    pass
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=5)
            # The plugin's owned coordinator observes that its last server stopped.
            if paths.database.exists():
                eventually(
                    lambda: Store(paths.database).get_meta("coordinator_status") == "stopped",
                    timeout=25,
                )
