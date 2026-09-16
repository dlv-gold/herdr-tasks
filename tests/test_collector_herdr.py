import copy
import re
import subprocess

from herdr_tasks.collector import Collector, passive_text, report_pattern
from herdr_tasks.config import Config
from herdr_tasks.herdr import HerdrError, agent_key, parent_path, project_root, toggle_panel
from herdr_tasks.store import Store


class FakeHerdr:
    def __init__(self):
        self.agent_data = {
            "pane_id": "w1:p1",
            "terminal_id": "term1",
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "agent": "codex",
            "agent_status": "idle",
            "interactive_ready": True,
            "cwd": "/project",
            "name": "coder",
        }
        self.prompts = []
        self.output = "Project output"
        self.fail_prompt = False
        self.change_agent = False

    def sessions(self):
        return [{"name": "default", "socket_path": "/socket", "running": True}]

    def snapshot(self, endpoint):
        return {
            "workspaces": [{"workspace_id": "w1", "cwd": "/project"}],
            "agents": [copy.deepcopy(self.agent_data)],
        }

    def read(self, *args, **kwargs):
        return self.output

    def agent(self, *args):
        result = copy.deepcopy(self.agent_data)
        if self.change_agent:
            result["terminal_id"] = "replacement"
        return result

    def call(self, endpoint, method, params, **kwargs):
        assert method == "agent.prompt"
        self.prompts.append(params)
        if self.fail_prompt:
            raise HerdrError("timeout", "Uncertain delivery")
        marker = re.search(r"HERDR_TASKS_[a-f0-9]+", params["text"])[0]
        self.output = f"{marker}_BEGIN\nImplemented a feature. Tests passed.\n{marker}_END"
        return {}


def collector(tmp_path, monkeypatch):
    monkeypatch.setattr("herdr_tasks.collector.project_root", lambda *_: ("/project", "Project"))
    store = Store(tmp_path / "tasks.db")
    api = FakeHerdr()
    return Collector(api, store, Config()), store, api


def test_ready_agent_report_is_requested_once_and_captured(tmp_path, monkeypatch):
    service, store, api = collector(tmp_path, monkeypatch)
    agents, _ = service.scan()
    run = store.start_run("a", {})
    updates = service.request_updates(run["id"], agents)
    service.request_updates(run["id"], agents)
    assert len(api.prompts) == 1
    assert updates[0]["status"] == "received"
    assert "Tests passed" in updates[0]["data"]["report"]
    service.scan()
    assert len(store.history(agents[0]["project_id"], "2000", "9999")) == 1


def test_busy_blocked_and_replaced_agents_are_not_prompted(tmp_path, monkeypatch):
    service, store, api = collector(tmp_path, monkeypatch)
    for state in ("working", "blocked", "unknown"):
        api.agent_data["agent_status"] = state
        agents, _ = service.scan()
        run = store.start_run(state, {})
        assert service.request_updates(run["id"], agents)[0]["status"] == "unavailable"
    api.agent_data["agent_status"] = "idle"
    agents, _ = service.scan()
    api.change_agent = True
    run = store.start_run("replacement", {})
    service.request_updates(run["id"], agents)
    assert not api.prompts


def test_ambiguous_prompt_is_never_resent_after_restart(tmp_path, monkeypatch):
    service, store, api = collector(tmp_path, monkeypatch)
    agents, _ = service.scan()
    api.fail_prompt = True
    run = store.start_run("a", {})
    service.request_updates(run["id"], agents)
    second = Collector(api, Store(store.path), Config())
    second.request_updates(run["id"], agents)
    assert len(api.prompts) == 1
    assert store.updates(run["id"])[0]["status"] == "ambiguous"


def test_agent_ids_are_scoped_to_session():
    data = FakeHerdr().agent_data
    assert agent_key("/socket1", data) != agent_key("/socket2", data)


def test_git_worktrees_resolve_to_same_project(tmp_path):
    main, linked = tmp_path / "main", tmp_path / "linked"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Initial",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-qb", "linked", str(linked)], check=True
    )
    assert project_root(str(main)) == project_root(str(linked))


def test_parent_path_tracks_nested_split():
    tree = {
        "type": "split",
        "first": {"type": "pane", "pane_id": "left"},
        "second": {
            "type": "split",
            "first": {"type": "pane", "pane_id": "source"},
            "second": {"type": "pane", "pane_id": "panel"},
        },
    }
    assert parent_path(tree, "panel") == [True]


class PanelHost:
    def __init__(self):
        self.calls = []
        self.panes = [
            {
                "pane_id": "left",
                "terminal_id": "terminal-left",
                "tab_id": "tab",
                "workspace_id": "w1",
            },
            {
                "pane_id": "right",
                "terminal_id": "terminal-right",
                "tab_id": "tab",
                "workspace_id": "w1",
            },
        ]

    def snapshot(self, endpoint):
        return {
            "panes": self.panes,
            "layouts": [
                {
                    "tab_id": "tab",
                    "panes": [
                        {"pane_id": "left", "rect": {"x": 0, "y": 0, "width": 50, "height": 30}},
                        {"pane_id": "right", "rect": {"x": 50, "y": 0, "width": 50, "height": 30}},
                    ],
                }
            ],
        }

    def call(self, endpoint, method, params):
        self.calls.append((method, params))
        if method == "plugin.pane.open":
            pane = {"pane_id": "panel", "terminal_id": "terminal-panel", "tab_id": "tab"}
            self.panes.append(pane)
            return {"plugin_pane": {"plugin_id": "herdr-tasks", "pane": pane}}
        if method == "layout.export":
            return {
                "layout": {
                    "root": {
                        "type": "split",
                        "first": {"type": "pane", "pane_id": "right"},
                        "second": {"type": "pane", "pane_id": "panel"},
                    }
                }
            }
        if method == "plugin.pane.close":
            self.panes = [p for p in self.panes if p["pane_id"] != params["pane_id"]]
        return {}


def test_toggle_panel_preserves_existing_terminals_and_uses_right_edge(tmp_path):
    store, host = Store(tmp_path / "tasks.db"), PanelHost()
    before = copy.deepcopy(host.panes)
    toggle_panel(host, store, Config(), "/socket", "left")
    assert host.calls[0][1]["target_pane_id"] == "right"
    toggle_panel(host, store, Config(), "/socket", "left")
    assert host.panes == before
    assert all(method != "layout.apply" for method, _ in host.calls)


def test_moved_panel_is_found_in_destination_tab(tmp_path):
    store, host = Store(tmp_path / "tasks.db"), PanelHost()
    toggle_panel(host, store, Config(), "/socket", "left")
    host.panes[-1]["tab_id"] = "other-tab"
    host.panes.append(
        {
            "pane_id": "other",
            "terminal_id": "terminal-other",
            "tab_id": "other-tab",
            "workspace_id": "w1",
        }
    )
    toggle_panel(host, store, Config(), "/socket", "other", open_only=True)
    assert host.calls[-1] == ("plugin.pane.focus", {"pane_id": "panel"})
    toggle_panel(host, store, Config(), "/socket", "other")
    assert all(p["pane_id"] != "panel" for p in host.panes)


def test_reporting_blocks_are_excluded_from_later_passive_activity():
    marker = "HERDR_TASKS_" + "a" * 32
    text = f"Earlier work\n• {marker}_BEGIN\nA progress report\n{marker}_END\nLater actual work"
    assert re.findall(report_pattern(marker), text, re.DOTALL) == ["A progress report\n"]
    assert passive_text(text) == "Earlier work\n\nLater actual work"


def test_disconnected_session_does_not_hide_other_session_agents(tmp_path, monkeypatch):
    service, store, api = collector(tmp_path, monkeypatch)
    snapshot = api.snapshot

    def get_snapshot(endpoint):
        if endpoint == "/offline":
            raise HerdrError("disconnected", "Server closed")
        return snapshot(endpoint)

    api.snapshot = get_snapshot
    sessions = [{"name": "closed", "socket_path": "/offline"}] + api.sessions()
    agents, gaps = service.scan(sessions)
    assert len(agents) == 1 and "closed" in gaps[0]
